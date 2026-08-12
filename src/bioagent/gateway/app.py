from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from ..core.config import load_project_env
from . import gpu, vllm_client
# The endpoint-locality test lives with the data-boundary guard it feeds (a privacy decision,
# and testable without the web stack). Aliased so every call site reads the same as before.
from ..integrations.safety import endpoint_is_off_host as _endpoint_is_off_host
from . import hpc_gc                      # shared-root bootstrap + the HPC3 Temp sweeper
from .errors import GatewayError, VLLMNetworkError, error_detail
from .executor import RemoteExecutor
from .mock_host import MockExecutor
from .settings import HPCSettings
from .ssh_gateway import SSHExecutor

ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = ROOT / "frontend" / "console"

# Load .env early so BIOAGENT_RESULTS_DIR is available at import time.
load_project_env(ROOT)

# Where results live on the gateway host (eyeserver), partitioned per SSH user:
# <results_dir>/<ucinetid>/<run_id>/artifacts/...
# On eyeserver set BIOAGENT_RESULTS_DIR=/data/BioAgent (the big /data disk, not
# /home). Defaults to runs/console for local development.
CONSOLE_RUNS_DIR = Path(os.environ.get("BIOAGENT_RESULTS_DIR") or (ROOT / "runs" / "console")).resolve()


def safe_name(value: str) -> str:
    cleaned = "".join(c for c in (value or "") if c.isalnum() or c in "-_")
    return cleaned or "guest"


def safe_filename(value: str) -> str:
    """Sanitize an uploaded filename (keep the extension; drop path/odd chars)."""
    base = os.path.basename(value or "")
    cleaned = "".join(c for c in base if c.isalnum() or c in "-_.")
    return cleaned.lstrip(".") or "dataset"


def safe_relpath(value: str) -> str:
    """Sanitize a client-supplied RELATIVE path (folder upload) so it can't escape the
    uploads dir. Keeps nested structure; drops empty/./.. segments and odd chars. Returns
    a POSIX relative path like ``mydata/raw/matrix.h5ad`` (or "" if nothing usable)."""
    segments = []
    for seg in (value or "").replace("\\", "/").split("/"):
        seg = seg.strip()
        if not seg or seg in (".", ".."):
            continue
        cleaned = "".join(c for c in seg if c.isalnum() or c in "-_.").lstrip(".")
        if cleaned:
            segments.append(cleaned)
    return "/".join(segments)


def _unique_child(parent: Path, name: str) -> str:
    """A child name under ``parent`` that does NOT collide with an existing file/dir:
    prefer ``name``, else append ' (1)', ' (2)', … before the extension. This is what
    stops a same-named upload from silently OVERWRITING earlier data (e.g. two different
    ``query.h5ad`` files → ``query.h5ad`` and ``query (1).h5ad``)."""
    if not (parent / name).exists():
        return name
    p = Path(name)
    stem, suffix = p.stem, "".join(p.suffixes)   # keep multi-part ext (.tar.gz, .h5ad)
    for k in range(1, 1000):
        alt = f"{stem} ({k}){suffix}"
        if not (parent / alt).exists():
            return alt
    return f"{stem} ({uuid4().hex[:6]}){suffix}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunState:
    """Per-run isolation unit. One SSH/GPU ``Connection`` is shared across a user's windows /
    tabs / conversations, but each RUN (a study, a report regenerate, an A2 continuation) gets
    its OWN cancel + plan-review events and its own identity (run_id + conversation_id). Keeping
    these per-run — instead of one connection-wide ``chat_stop`` / ``plan_event`` — is what stops a
    cancel/approve in one window from reaching another window's run, and lets every streamed WS
    event be tagged so the client can demux it into the OWNING conversation bubble.

    Runs on one connection are serialized (``/api/lab`` returns 409 while ``chat_running``), so at
    most one RunState is ``active`` at a time; the ``Connection.runs`` registry keeps recent ones so
    a late/stale approve or cancel that names a finished run resolves to a no-op instead of hitting
    whatever run happens to be live now."""

    def __init__(self, conn: "Connection", conversation_id: str | None = None,
                 run_id: str | None = None) -> None:
        self.conversation_id = conversation_id
        self.run_id = run_id
        # cooperative cancel for THIS run's in-flight chat/pipeline (set by /api/chat/stop for this
        # run, or by deleting its owning chat); the streaming loops + step callback check it.
        self.chat_stop = threading.Event()
        # A user-requested context compaction for THIS run (POST /api/lab/compact). A control flag
        # rather than a note queued into conn.injections on purpose: mid-run steering notes are
        # PROSE the model reads and may or may not act on, whereas "compact now" must be an
        # instruction to the loop itself. The loop consumes and clears it at the next step boundary.
        self.compact_request = threading.Event()
        # interactive Plan-mode review: the lab worker thread blocks here after the PI plans until
        # the UI posts the approved/edited agenda (or a cancel) for THIS run via /api/lab/plan.
        self.plan_event = threading.Event()
        self.plan_value: Any = None          # approved/edited agenda or decision; None = cancelled
        self.pending_plan: Any = None        # buffered for a late WS subscriber
        # Which ENGINE this run is: "research" (the lab) or "chat" (the answer-first fast path).
        # Endpoints that only make sense against the lab (mid-run steering via /api/chat/inject)
        # check this — a note queued during a chat turn has nothing to consume it and would
        # otherwise sit in conn.injections and leak into the NEXT research run's guidance.
        self.kind = "research"

    def tag(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Stamp a WS payload with this run's identity (without clobbering an explicit tag)."""
        if self.run_id is not None:
            payload.setdefault("run_id", self.run_id)
        if self.conversation_id is not None:
            payload.setdefault("conversation_id", self.conversation_id)
        return payload


class Connection:
    """One live SSH+GPU+vLLM session, with a pub/sub event log."""

    def __init__(self, settings: HPCSettings, *, mock: bool, loop: asyncio.AbstractEventLoop, username: str = "guest") -> None:
        self.id = str(uuid4())
        self.settings = settings
        self.mock = mock
        self.loop = loop
        self.status = "connecting"
        self.executor: RemoteExecutor | None = None
        self.alloc: gpu.GPUAllocation | None = None
        self.gpu_lock = threading.Lock()   # serializes GPU/vLLM work (mid-run heal vs the provisioning thread)
        self.hpc_pysrc: str | None = None  # dfs3b dir the live bioagent source is synced to (bind for analysis jobs)
        # <shared_root> exists and this user's dirs under it are ready (see _prepare_shared_storage).
        # False also gates the periodic Temp sweep, so it never runs against an unprepared root.
        self.shared_root_ok = False
        self.tunnel_port: int | None = None
        self.llm_info: dict[str, Any] = {"installed": False, "version": None}
        self.available_models: list[str] = []
        self.selected_model: str = settings.serving_model()
        self.gpu_health: dict[str, Any] | None = None
        self.log: list[dict[str, Any]] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.monitor_task: asyncio.Task | None = None
        self.created_at = utc_now()
        # per-user folder on eyeserver: runs/console/<ucinetid>/
        self.owner = safe_name(username)
        self.workspace = CONSOLE_RUNS_DIR / self.owner
        # The logged-in AiScientist account that owns this connection's data/history
        # (set at /api/connect when authenticated). None when accounts are disabled.
        self.app_user_id: int | None = None
        # Public metadata of an SSH key freshly minted+deployed on this login (else None),
        # so the UI can refresh its saved-credentials list once ready.
        self.new_credential: dict | None = None
        # interactive Duo: the SSH auth thread blocks on this event until the
        # UI posts the chosen method / passcode.
        self.duo_event = threading.Event()
        self.duo_value: str | None = None
        self.pending_duo: str | None = None  # buffered so a late WS subscriber still sees it
        # Per-run isolation. The cancel + plan-review events used to live directly on the
        # Connection (one shared ``chat_stop`` / ``plan_event`` for the whole SSH session), so a
        # cancel/approve from one window hit whatever run was live. They now live on a per-run
        # :class:`RunState`: ``active_run`` is the one currently streaming, ``runs`` keeps recent
        # ones by run_id so a stale approve/cancel resolves to the right (or no) run, and the legacy
        # ``conn.chat_stop`` / ``conn.plan_event`` / ``conn.plan_value`` / ``conn.pending_plan``
        # attributes are now thin properties that proxy to ``active_run`` (see below).
        self.active_run: RunState | None = None
        self.runs: dict[str, RunState] = {}
        self.chat_running = False
        # Per-CONVERSATION last completed run, so a follow-up ("revise the report") routes to the
        # run its OWN chat produced — and a fresh window/thread never inherits another conversation's
        # run as a stale "replan". ``last_run_id`` (below) stays as the connection-wide fallback for
        # older clients that don't send a conversation_id.
        self.last_run_by_conversation: dict[str, str] = {}
        # Per-CONVERSATION rolling chat memory: {conversation_id: {"summary": str, "through": int}}.
        # The fast chat path compacts older turns into a model-written summary; keeping it here
        # (rather than re-deriving it from the history the client resends) is what makes
        # re-summarization INCREMENTAL — each turn folds in only the newly-evicted messages, so
        # the cost stays flat as the conversation grows. Scoped per conversation so two windows
        # on this shared SSH session never inherit each other's memory, and deliberately
        # in-memory: a lost summary just means the next turn rebuilds one. See
        # agents/chat_context.py.
        self.chat_memory: dict[str, dict[str, Any]] = {}
        # In-flight assistant turn, captured from pushed chat messages so a client that
        # refreshes / navigates back / briefly drops its socket can RE-SUBSCRIBE and see
        # the run continue (the asyncio run task + this Connection both survive a client
        # reload). Reset on each chat_start; replayed by the WS endpoint while a run is live.
        self.stream: dict[str, Any] | None = None
        # Mid-run prompt injection: text the user submits WHILE a run is executing. The
        # lab loop drains these between steps and folds them into the remaining steps'
        # guidance (so a researcher can steer a run without stopping + restarting it).
        self.injections: list[str] = []
        self._inj_lock = threading.Lock()
        # The last COMPLETED run on this connection. Lets a follow-up ("重新生成报告 / 按指示改")
        # rebuild the report from the persisted bundle WITHOUT re-running the PI + analysis
        # (see /api/report/regenerate). Survives across chat messages + client reloads on the
        # same live connection; the frontend also remembers the run_id in localStorage, so a
        # regenerate works even after a refresh reconnects.
        self.last_run_id: str | None = None

    # -- per-run scoping ----------------------------------------------------

    def _ensure_run(self) -> RunState:
        """The active run, creating a bare one on demand. Lets the legacy ``conn.chat_stop`` /
        ``conn.plan_event`` accessors + the pre-run follow-up clarify work before a run_id exists."""
        if self.active_run is None:
            self.active_run = RunState(self)
        return self.active_run

    def begin_run(self, conversation_id: str | None = None,
                  run_id: str | None = None) -> RunState:
        """Open a new run scope and make it active. ``run_id`` is usually assigned later (a fresh
        study mints it inside ``_run_lab``); a resume/regenerate knows it up front."""
        run = RunState(self, conversation_id=conversation_id, run_id=run_id)
        self.active_run = run
        self._register_run(run)
        return run

    def bind_run_id(self, run: RunState, run_id: str) -> None:
        """Assign the final run_id to a run (fresh mint or resume) and index it."""
        run.run_id = run_id
        self.active_run = run
        self._register_run(run)

    def _register_run(self, run: RunState) -> None:
        if not run.run_id:
            return
        self.runs[run.run_id] = run
        # Bound the registry — it only needs enough history to no-op a stale approve/cancel.
        if len(self.runs) > 64:
            for k in list(self.runs)[:-64]:
                self.runs.pop(k, None)

    def end_run(self, run: RunState | None) -> None:
        """Close a run scope: stop treating it as active (so a post-run WS reconnect doesn't see a
        stale pending plan), but keep it in ``runs`` so a late approve/cancel resolves to a no-op."""
        if run is not None and self.active_run is run:
            self.active_run = None

    def resolve_run(self, run_id: str | None = None,
                    conversation_id: str | None = None) -> RunState | None:
        """The run a targeted interaction (cancel / plan approve) should hit. A request that names a
        run_id or conversation_id ONLY matches when it is the currently active run — so an interaction
        meant for a finished run never lands on whatever run is live now. No identifiers → the active
        run (back-compat for older clients)."""
        ar = self.active_run
        if ar is None:
            return None
        if run_id:
            return ar if ar.run_id == run_id else None
        if conversation_id:
            return ar if ar.conversation_id == conversation_id else None
        return ar

    def _tag(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Stamp a streamed payload with the active run's run_id + conversation_id, so the client can
        demux it into the owning conversation bubble. No-op when there is no active run (connect-time
        status/duo events) or when the payload already carries a tag."""
        ar = self.active_run
        return ar.tag(payload) if ar is not None else payload

    # -- event pub/sub ------------------------------------------------------

    def emit(self, level: str, stage: str, message: str, detail: Any = None) -> None:
        event = {
            "type": "event",
            "level": level,
            "stage": stage,
            "message": message,
            "created_at": utc_now(),
        }
        if detail is not None:
            event["detail"] = detail
        self._tag(event)
        self.log.append(event)
        self._publish(event)

    def push(self, payload: dict[str, Any]) -> None:
        """Publish a non-log message (chat token, status, gpu health snapshot)."""
        self._tag(payload)
        self._track_stream(payload)
        self._publish(payload)

    # -- legacy per-run accessors (proxy to the active run) -----------------
    # Kept so the ~30 call sites that historically used one connection-wide event keep working — they
    # now transparently operate on whatever run is active. New targeted call sites (the /api/chat/stop
    # + /api/lab/plan endpoints) use ``resolve_run`` instead so they reach only their own run.

    @property
    def chat_stop(self) -> threading.Event:
        return self._ensure_run().chat_stop

    @property
    def plan_event(self) -> threading.Event:
        return self._ensure_run().plan_event

    @property
    def plan_value(self) -> Any:
        return self.active_run.plan_value if self.active_run is not None else None

    @plan_value.setter
    def plan_value(self, value: Any) -> None:
        self._ensure_run().plan_value = value

    @property
    def pending_plan(self) -> Any:
        return self.active_run.pending_plan if self.active_run is not None else None

    @pending_plan.setter
    def pending_plan(self, value: Any) -> None:
        self._ensure_run().pending_plan = value

    def _track_stream(self, payload: dict[str, Any]) -> None:
        """Accumulate the in-flight assistant turn so a reconnecting client can replay it."""
        t = payload.get("type")
        if t == "chat_start":
            self.stream = {"text": "", "thinking": "", "progress": [], "status": "running",
                           "artifacts": None, "error": "", "status_label": "",
                           "run_id": payload.get("run_id"),
                           "conversation_id": payload.get("conversation_id"), "run_complete": None}
            return
        s = self.stream
        if s is None:
            return
        if t == "chat_token":
            s["text"] += payload.get("token", "")
        elif t == "chat_thinking":
            s["thinking"] += payload.get("token", "")
        elif t == "lab_progress":
            # Ordered feed of key-progress lines AND code blocks, replayed in order.
            s["progress"].append({"kind": "line", "text": payload.get("text", ""),
                                  "level": payload.get("level", "info")})
        elif t == "step_code":
            s["progress"].append({"kind": "code", "code": payload.get("code", "")})
        elif t == "chat_done":
            s["status"] = "done"
        elif t == "chat_stopped":
            s["status"] = "stopped"
        elif t == "chat_error":
            s["status"] = "error"
            s["error"] = payload.get("message", "")
        elif t == "artifacts":
            s["artifacts"] = payload
            m = re.search(r"/api/bundle/[^/]+/([^/]+)", payload.get("bundle_url", "") or "")
            if m:
                s["run_id"] = m.group(1)
        elif t == "run_complete":
            # Terminal marker (run_id + agenda). Kept so a reconnecting client that missed
            # the live completion can recover the run and rebind its regenerate/re-run state.
            s["run_complete"] = payload
            if payload.get("run_id"):
                s["run_id"] = payload["run_id"]
        elif t == "run_status":
            s["status_label"] = payload.get("text", "")

    def stream_replay_payloads(self) -> list[dict[str, Any]]:
        """Rebuild the in-flight assistant turn as an ordered list of WS payloads, so a
        (re)subscribing client restores the live centre bubble (thinking + key progress +
        text). Only meaningful while a run is in flight — see the WS endpoint's guard."""
        s = self.stream
        if not s:
            return []
        start: dict[str, Any] = {"type": "chat_start"}
        if s.get("run_id"):
            start["run_id"] = s["run_id"]
        if s.get("conversation_id"):
            start["conversation_id"] = s["conversation_id"]
        out: list[dict[str, Any]] = [start]
        if s["thinking"]:
            out.append({"type": "chat_thinking", "token": s["thinking"]})
        for p in s["progress"]:
            if p.get("kind") == "code":
                out.append({"type": "step_code", "code": p.get("code", "")})
            else:
                out.append({"type": "lab_progress", "text": p.get("text", ""),
                            "level": p.get("level", "info")})
        if s["text"]:
            out.append({"type": "chat_token", "token": s["text"]})
        if s["status"] == "running" and s.get("status_label"):
            out.append({"type": "run_status", "text": s["status_label"]})
        if s["status"] == "done":
            out.append({"type": "chat_done"})
        elif s["status"] == "stopped":
            out.append({"type": "chat_stopped"})
        elif s["status"] == "error":
            out.append({"type": "chat_error", "message": s.get("error", "")})
        if s.get("artifacts"):
            out.append(s["artifacts"])
        if s.get("run_complete"):
            out.append(s["run_complete"])
        return out

    def _publish(self, payload: dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            self.loop.call_soon_threadsafe(queue.put_nowait, payload)

    def emit_fn(self):
        def _emit(level: str, stage: str, message: str) -> None:
            self.emit(level, stage, message)
        return _emit

    # -- mid-run prompt injection -------------------------------------------

    def add_injection(self, text: str) -> None:
        with self._inj_lock:
            self.injections.append(text)

    def pull_injections(self) -> list[str]:
        """Drain any notes the user submitted since the last call (thread-safe). Called by
        the lab loop between steps; returns [] when there is nothing new."""
        with self._inj_lock:
            if not self.injections:
                return []
            drained = self.injections[:]
            self.injections.clear()
            return drained

    # -- snapshot -----------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "mock": self.mock,
            "host": self.settings.host,
            "username": getattr(self.executor, "username", None),
            "model": self.selected_model,
            "available_models": self.available_models,
            "selected_model": self.selected_model,
            "llm": self.llm_info,
            "gpu": {
                "job_id": self.alloc.job_id if self.alloc else None,
                "node": self.alloc.node if self.alloc else None,
                "gres": self.settings.gres,
                "health": self.gpu_health,
                # The SBATCH --time wall limit. Slurm kills the job at this point (TIMEOUT), so the
                # client can tell when a remembered job CANNOT still be alive instead of asserting it is.
                "time_limit": self.settings.time_limit,
            },
            "tunnel_port": self.tunnel_port,
            "created_at": self.created_at,
            "chat_running": self.chat_running,
            # Identity of the run currently in flight, so a reconnecting/reloading client can re-adopt
            # it as the run OWNER (state.runSessionId) and the Stop button POSTs the matching
            # conversation_id. Without this, a WS reconnect wipes the client's owner id and Stop
            # silently no-ops (resolve_run can't match) while the job runs to its Slurm --time.
            "active_run": ({"run_id": self.active_run.run_id,
                            "conversation_id": self.active_run.conversation_id}
                           if self.active_run is not None else None),
            "new_credential": self.new_credential,
        }

    def broadcast_status(self) -> None:
        self.push({"type": "status", "connection": self.summary()})


CONNECTIONS: dict[str, Connection] = {}


# --- request models --------------------------------------------------------


class ConnectRequest(BaseModel):
    ucinetid: str
    password: str | None = None
    auth_method: str = "password_duo"  # or "ssh_key"
    key_path: str | None = None
    key_passphrase: str | None = None
    host: str | None = None
    model: str | None = None
    duo_passcode: str | None = None  # legacy pre-entered Duo passcode (kept for back-compat)
    duo_method: str = "push"         # "push" | "phone" | "passcode" — how to answer Duo
    credential_id: str | None = None  # use a saved SSH key (auth_method="ssh_key")
    create_key: bool = True          # after a password+Duo login, mint+deploy a reusable key
    campus_network_confirmed: bool = False
    mock: bool = False



# --- provisioning flow -----------------------------------------------------


def _ssh_connect_blocking(conn: Connection, req: ConnectRequest) -> None:
    """Worker thread, phase 1 of :func:`_provision_blocking`: SSH auth (+ interactive Duo) ONLY.
    Sets conn.executor and leaves the session at status 'connecting' — the GPU/vLLM serve job
    follows immediately in :func:`_provision_gpu_blocking`, in the same connect. Split out for
    readability, NOT as a separately reachable state: a session is never handed to the user
    with SSH up but no model."""
    emit = conn.emit_fn()
    settings = conn.settings

    # 1. SSH connect
    if conn.mock:
        conn.executor = MockExecutor(host=settings.host, username=req.ucinetid)
        emit("success", "ssh_auth", f"[MOCK] Connected to {settings.host} as {req.ucinetid}.")
    else:
        emit("step", "ssh_connect", f"Connecting to {settings.host} as {req.ucinetid} ...")

        # Duo is answered by METHOD (no 6-digit code needed for the common case): "push"
        # auto-responds "1" (approve on your phone), "phone" answers "2" (call), and
        # "passcode" pauses for the UI to collect a code. A legacy pre-entered passcode
        # still works. Duo's keyboard-interactive menu is 1=Push, 2=Phone, 3=SMS.
        duo_used = {"prefilled": False}
        duo_method = (req.duo_method or "push").strip().lower()

        def duo_callback(prompt_text: str) -> str:
            if req.duo_passcode and not duo_used["prefilled"]:
                duo_used["prefilled"] = True
                emit("info", "ssh_auth", "Submitting the Duo passcode you entered.")
                return req.duo_passcode.strip()
            if duo_method == "push":
                emit("info", "ssh_auth", "Duo push sent — approve it on your phone (Duo Mobile).")
                return "1"
            if duo_method == "phone":
                emit("info", "ssh_auth", "Duo is calling your phone — answer and approve.")
                return "2"
            # passcode → pause for the UI to collect a code
            conn.duo_value = None
            conn.duo_event.clear()
            conn.pending_duo = prompt_text
            conn.push({"type": "duo_prompt", "prompt": prompt_text})
            emit("warning", "ssh_auth", "Enter your Duo passcode in the login panel.")
            try:
                if not conn.duo_event.wait(timeout=180):
                    raise GatewayError(
                        "Duo approval timed out after 180s. Reconnect and respond to the Duo prompt.",
                        stage="ssh_auth",
                    )
            finally:
                conn.pending_duo = None
            return conn.duo_value or "1"

        # SSH-key login: prefer a SAVED credential (minted on a prior password+Duo login),
        # else a manually-typed key path.
        key_path = req.key_path if req.auth_method == "ssh_key" else None
        if req.auth_method == "ssh_key" and req.credential_id:
            from . import ssh_credentials
            cred = ssh_credentials.get_credential(conn.owner, req.credential_id)
            if cred is None:
                raise GatewayError("That saved SSH key was not found for your account.", stage="ssh_auth")
            key_path = cred["key_path"]
            emit("step", "ssh_auth", f"Using your saved SSH key ({cred.get('label')}) — no password or Duo needed.")

        group = settings.data_group()
        if group:
            emit("info", "ssh_auth", f"Install dir is on lab DFS storage — running remote commands under group '{group}'.")
        conn.executor = SSHExecutor(
            host=settings.host,
            username=req.ucinetid,
            port=settings.ssh_port,
            password=req.password,
            key_path=key_path,
            key_passphrase=req.key_passphrase,
            duo_callback=duo_callback if req.auth_method == "password_duo" else None,
            group=group,
            emit=emit,
            # Bulk staging goes to the dedicated transfer host, never the login node (RCIC rule).
            transfer_host=settings.transfer_host,
        )
        # First password+Duo login → mint a reusable SSH key and deploy its PUBLIC half to
        # HPC3 authorized_keys, so next time the user can pick "SSH key" and skip Duo. Only
        # when asked (create_key) and there isn't already one for this host+user. Best-effort.
        if req.auth_method == "password_duo" and req.create_key:
            try:
                from . import ssh_credentials
                already = any(c["host"] == settings.host and c["hpc_user"] == req.ucinetid
                              for c in ssh_credentials.list_credentials(conn.owner))
                if not already:
                    cred = ssh_credentials.create_and_deploy(
                        conn.owner, conn.executor, host=settings.host, hpc_user=req.ucinetid,
                        passphrase=req.key_passphrase or None)
                    conn.new_credential = cred
                    emit("success", "ssh_auth",
                         "Saved a reusable SSH key on HPC3 — next login can use it (no password or Duo).")
            except Exception as exc:  # noqa: BLE001 - key setup must never fail the login
                emit("warning", "ssh_auth", f"Couldn't set up a reusable SSH key (login still fine): {exc}")
        conn.push({"type": "duo_done"})

    _prepare_shared_storage(conn)

    emit("success", "ssh_connect",
         f"SSH session to {settings.host} is ready — allocating the GPU and starting the model next.")


def _prepare_shared_storage(conn: Connection) -> None:
    """Create this user's dirs under the shared AiScientist root and sweep their cold process
    files, right after SSH comes up (so every later path resolves and Temp doesn't grow forever).

    Non-fatal by design: a session that never offloads anything to HPC3 still works without the
    shared root. But the failure is reported at ERROR level with the exact fix, because every
    HPC-offloaded step *will* fail later without it — that must not read as a mystery."""
    if conn.mock or conn.executor is None:
        return
    emit = conn.emit_fn()
    st = conn.settings
    try:
        ok, message = hpc_gc.ensure_shared_dirs(conn.executor, st.shared_root, _hpc_user(conn))
    except Exception as exc:  # noqa: BLE001 - storage prep must never break the login
        emit("warning", "storage", f"Could not prepare the AiScientist shared storage: {exc}")
        return
    if not ok:
        conn.shared_root_ok = False
        emit("error", "storage", message)
        return
    conn.shared_root_ok = True
    _report_sweep(conn, _submit_temp_sweep(conn))


def _submit_temp_sweep(conn: Connection) -> dict:
    """Queue this user's HPC3 Temp sweep as a Slurm batch job on the free CPU partition."""
    st = conn.settings
    return hpc_gc.sweep_temp(
        conn.executor, st.shared_root, _hpc_user(conn), st.temp_ttl_days,
        partition=st.cpu_partition, account=st.cpu_account or "")


def _report_sweep(conn: Connection, result: dict) -> None:
    """Surface a sweep in the console log. The sweep itself runs as a batch job, so what we can
    report right now is what the PREVIOUS one deleted — good enough for housekeeping, and it keeps
    the login node doing nothing but `sbatch`."""
    if result.get("status") == "error":
        conn.emit("warning", "storage",
                  f"Temp cleanup could not be queued on HPC3: {result.get('error', 'unknown error')}")
    elif result.get("removed"):
        conn.emit("info", "storage",
                  f"HPC3 cleanup removed {result['removed']} finished run folder(s) from "
                  f"{result.get('root', 'Temp')} (untouched for {result.get('ttl_days')} days). "
                  "Your uploads and personal HPC3 folders are never touched.")


async def _hpc_temp_gc_loop() -> None:
    """Queue every live session's HPC3 Temp sweep every 6h, so a long-lived session cleans up
    without waiting for the user to reconnect. The per-connect submit in
    :func:`_prepare_shared_storage` covers everyone else. Both submit the same staged script as a
    batch job, and a session only ever sweeps its OWN user's subtree."""
    while True:
        await asyncio.sleep(6 * 3600)
        for conn in list(CONNECTIONS.values()):
            if conn.mock or conn.executor is None or not getattr(conn, "shared_root_ok", False):
                continue
            try:
                _report_sweep(conn, await asyncio.to_thread(_submit_temp_sweep, conn))
            except Exception as exc:  # noqa: BLE001 - housekeeping never crashes the server
                print(f"[gc] HPC3 temp sweep failed: {type(exc).__name__}: {exc}")


def _provision_gpu_blocking(conn: Connection) -> None:
    """Worker thread, phase 2 of :func:`_provision_blocking`: allocate the GPU vLLM serve job
    (+ tunnel + model + health) for the session SSH phase 1 just logged in."""
    emit = conn.emit_fn()
    settings = conn.settings

    conn.status = "provisioning"
    conn.broadcast_status()

    # 2. Verify the vLLM container image is present (no per-user binary to install)
    conn.llm_info = vllm_client.ensure_installed(conn.executor, settings, emit)
    conn.broadcast_status()

    # 3. GPU allocation running the vLLM serve job (singularity + vllm serve)
    conn.alloc = gpu.ensure_serve_job(conn.executor, settings, emit)
    conn.broadcast_status()

    # 4. Tunnel to the compute node's vLLM port (dynamic per node; see gpu.py)
    if conn.mock:
        conn.tunnel_port = conn.alloc.port
        emit("success", "ssh_tunnel", f"[MOCK] Tunnel to {conn.alloc.node}:{conn.alloc.port} ready.")
    else:
        emit("step", "ssh_tunnel", f"Opening tunnel to {conn.alloc.node}:{conn.alloc.port} ...")
        conn.tunnel_port = conn.executor.open_tunnel(
            conn.alloc.node, conn.alloc.port, local_port=settings.local_tunnel_port
        )
        if settings.local_tunnel_port:
            emit("info", "ssh_tunnel",
                 f"vLLM endpoint pinned to 127.0.0.1:{conn.tunnel_port} (-> node port {conn.alloc.port}) "
                 "— an external tool can reach vLLM here.")

    # 5. Wait for the /v1 server and verify it is serving the requested model
    served = settings.serving_model()
    if conn.mock:
        conn.available_models = [served]
        emit("success", "llm_model", f"[MOCK] vLLM is serving {served} on {conn.alloc.node}.")
    else:
        _wait_for_server(conn, emit)
        vllm_client.ensure_model(conn.tunnel_port, settings, emit)
        # The model is already loaded at serve launch; a tiny ping confirms it is
        # warm so the first chat isn't stuck on a cold load.
        vllm_client.warmup(conn.tunnel_port, served, emit)
        conn.available_models = vllm_client.get_tags(conn.tunnel_port)
    conn.llm_info["models"] = conn.available_models
    # vLLM chat requests must use the EXACT id /v1/models reports — select that.
    conn.selected_model = conn.available_models[0] if conn.available_models else served
    emit("info", "llm_model", f"Active model: {conn.selected_model}.")

    # 6. First GPU health check
    _refresh_health(conn, emit)

    conn.status = "ready"
    emit("success", "ready", f"vLLM ({conn.selected_model}) is live on GPU node {conn.alloc.node}. You can start chatting.")
    conn.broadcast_status()


def _provision_blocking(conn: Connection, req: ConnectRequest) -> None:
    """The ONE connection path: SSH login, then GPU + vLLM, in a single shot.

    Status walks connecting → provisioning → ready and nothing else; there is no
    intermediate state in which the session is handed back with SSH up but no model, so
    every caller downstream can assume ``status == "ready"`` means the whole stack is live.
    """
    _ssh_connect_blocking(conn, req)
    _provision_gpu_blocking(conn)


def _serve_log_tail(conn: Connection, lines: int = 50) -> str:
    """Best-effort tail of the vLLM serve job's log on HPC3, so a timeout is
    diagnosable instead of blind. Empty string if it can't be read."""
    if conn.mock or not getattr(conn, "executor", None):
        return ""
    try:
        res = conn.executor.exec(
            f'tail -n {lines} "$(ls -t $HOME/bioagent-*-*.log 2>/dev/null | head -1)" 2>/dev/null'
        )
        return (getattr(res, "out", "") or "").strip()
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return ""


def _wait_for_server(conn: Connection, emit, attempts: int = 300) -> None:
    # vLLM loads the FULL model into VRAM before /v1 answers; a 24GB AWQ model read
    # from shared DFS can take many minutes on first load, so the window is generous
    # (300 * 2s = 10 min). We report progress and, on timeout, dump the serve log.
    import time as _t

    emit("step", "llm_serve", "Waiting for the vLLM /v1 server to accept connections (the model is loading) ...")
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            vllm_client.get_tags(conn.tunnel_port, timeout=5)
            emit("success", "llm_serve", "vLLM /v1 server is responding.")
            return
        except GatewayError as exc:
            last_err = exc
            if i and i % 15 == 0:   # ~ every 30s so the user sees it's still working
                emit("info", "llm_serve", f"Still loading the model … ({i * 2}s elapsed, up to {attempts * 2}s).")
            _t.sleep(2)
    tail = _serve_log_tail(conn)
    if tail:
        emit("error", "llm_serve", "vLLM serve log (last lines):\n" + tail)
    raise GatewayError(
        "vLLM /v1 server never became reachable through the tunnel.",
        stage="llm_serve",
        detail={"serve_log_tail": tail} if tail else (error_detail(last_err) if last_err else None),
    )


def _vllm_reachable(conn: Connection) -> bool:
    """Quick liveness probe of the session's vLLM /v1 through the current tunnel."""
    if not conn.tunnel_port:
        return False
    try:
        vllm_client.get_tags(conn.tunnel_port, timeout=5)
        return True
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


def _heal_vllm_session(conn: Connection) -> None:
    """Re-establish this session's vLLM serve job + tunnel after a mid-run drop.

    Two things kill a live session's LLM link with no user action: the SSH tunnel gets
    reaped while idle (user walks away), or the GPU serve job hits its Slurm ``--time``
    limit and Slurm kills it — either way the tunnel forwards to a dead port and the next
    call raises ``VLLMNetworkError``. This reattaches to the still-running job when Slurm
    still has it (cheap: just reopen the tunnel) and only resubmits (the ~10-min spin-up)
    if the job is actually gone. Updates ``conn.tunnel_port`` in place.

    Serialized on ``conn.gpu_lock`` so several calls failing at once heal ONCE, not N
    times. Raises ``GatewayError`` if recovery ultimately fails (the run then fails as it
    would have before, but with a clear recovery trail instead of a bare network error).
    """
    emit = conn.emit_fn()
    settings = conn.settings
    with conn.gpu_lock:
        # Another failing call may have already healed us while we waited for the lock.
        if _vllm_reachable(conn):
            return
        emit("warning", "vllm_recover",
             "vLLM connection dropped (idle tunnel or the GPU job hit its time limit) — "
             "recovering the serve job and tunnel …")
        # 1. Reattach to the running serve job; resubmit only if Slurm reaped it.
        conn.alloc = gpu.ensure_serve_job(conn.executor, settings, emit)
        # 2. Reopen the local port-forward to the (possibly new) node/port.
        conn.tunnel_port = conn.executor.open_tunnel(
            conn.alloc.node, conn.alloc.port, local_port=settings.local_tunnel_port
        )
        # 3. Block until /v1 answers again (the model reloads if the job was resubmitted).
        _wait_for_server(conn, emit)
        emit("success", "vllm_recover", "vLLM session recovered — retrying the interrupted call.")


def _refresh_health(conn: Connection, emit) -> dict[str, Any]:
    if conn.mock:
        res = conn.executor.exec("nvidia-smi")
        parts = [p.strip() for p in res.out.split(",")]
        health = {
            "healthy": res.ok,
            "util_percent": int(parts[0]) if res.ok else 0,
            "mem_used_mb": int(parts[1]) if res.ok else 0,
            "mem_total_mb": int(parts[2]) if res.ok else 0,
            "name": parts[3] if len(parts) > 3 else "GPU",
            "reason": "" if res.ok else res.stderr,
        }
    else:
        health = gpu.check_health(conn.executor, conn.settings, conn.alloc).as_dict()
    conn.gpu_health = health
    # Only surface a health failure while the session is live. A poll that lands AFTER
    # the user releases the GPU (status flipped to disconnected) would otherwise emit a
    # spurious "health check failed" / dead-port error for a job we just scancel'd.
    if not health["healthy"] and conn.status == "ready":
        emit("error", "gpu_health", f"GPU health check failed: {health['reason']}")
    conn.push({"type": "gpu_health", "connection_id": conn.id, "health": health})
    return health


async def _monitor_gpu(conn: Connection) -> None:
    """Periodic GPU health watchdog: flags link loss / abnormal utilization."""
    emit = conn.emit_fn()
    poll = max(5, conn.settings.gpu_poll_seconds)
    while conn.status == "ready":
        await asyncio.sleep(poll)
        # Skip the health probe WHILE a run holds the connection. The probe (`srun nvidia-smi` +
        # find_running_job) opens channels on the ONE shared SSH transport, each up to 30s; during a
        # run — every ~poll seconds — that competes with, and under load STARVES, the run's own
        # analysis-job submissions and file transfers. That is the observed
        # "echo $HOME/.bioagent/analysis timed out after 60s" / "Connect failed" (channels hit the
        # server's MaxSessions cap), and the tool-timeout → retry thrash that makes the loop crawl.
        # A live run already exercises the GPU + link, so a separate idle watchdog is redundant here;
        # resume polling once the run finishes.
        if conn.chat_running:
            continue
        try:
            await asyncio.to_thread(_refresh_health, conn, emit)
        except Exception as exc:  # noqa: BLE001 - surface fully, never crash the loop
            if conn.status == "ready":  # silent once the session is being torn down
                conn.emit("error", "gpu_health", "GPU monitor error.", detail=error_detail(exc))
            continue
        # still allocated?
        try:
            running = await asyncio.to_thread(gpu.find_running_job, conn.executor, conn.settings) if not conn.mock else conn.alloc
        except Exception:
            running = None
        if running is None and not conn.mock and conn.status == "ready":
            conn.status = "error"
            conn.emit("error", "gpu_alloc", "GPU job is no longer running — the allocation was lost or ended. Reconnect to request a new GPU.")
            conn.broadcast_status()
            break


# --- FastAPI app -----------------------------------------------------------

app = FastAPI(title="AiScientist HPC3 Console")

# User accounts / login / admin (Phase 1). Mounted only when the `auth` extra
# (sqlalchemy/bcrypt/itsdangerous) is installed — a host without it still runs the
# console, just without accounts (same graceful-degrade contract as scanpy/pandoc).
_AUTH_ENABLED = False
try:
    from . import auth_routes  # noqa: E402  (deferred: optional `auth` extra)

    app.include_router(auth_routes.router)
    _AUTH_ENABLED = True
except Exception as _auth_exc:  # noqa: BLE001 - missing extra must not break the console
    print(f"[auth] accounts disabled (install .[auth]): {type(_auth_exc).__name__}: {_auth_exc}")


@app.on_event("startup")
def _init_accounts() -> None:
    if not _AUTH_ENABLED:
        return
    try:
        from .db import init_db
        init_db()
        created = auth_routes.ensure_bootstrap_admin()
        if created:
            print(f"[auth] bootstrapped admin account: {created}")
        # Public deploy must NOT use the built-in dev secret: it signs session cookies, so a
        # known key = anyone can forge an admin session. Fail loud (set BIOAGENT_SECRET_KEY).
        from . import auth
        if auth.secure_cookies() and auth.using_dev_secret():
            print("[auth] *** SECURITY: BIOAGENT_PUBLIC_HTTPS is set but BIOAGENT_SECRET_KEY is the "
                  "built-in dev secret — session cookies are FORGEABLE. Set a strong "
                  "BIOAGENT_SECRET_KEY before exposing this publicly. ***")
    except Exception as exc:  # noqa: BLE001 - never block the console from starting
        print(f"[auth] account subsystem not initialized: {type(exc).__name__}: {exc}")


@app.on_event("startup")
async def _start_checkpoint_gc() -> None:
    """Launch the two housekeeping sweeps: stale A2 analysis checkpoints in the eyeserver's local
    run bundles (default 7 days), and cold per-run process files in each live session's HPC3
    ``<shared_root>/Temp/<user>`` area (default 3 days)."""
    asyncio.create_task(_checkpoint_gc_loop())
    asyncio.create_task(_hpc_temp_gc_loop())


def _cancel_alloc(conn: Connection, reason: str = "provisioning failed") -> None:
    """Best-effort scancel of THIS connection's own GPU job. Called whenever a connect
    fails after a job was allocated, so a half-provisioned A100 is never left running
    and burning the scarce GPU pool. Ownership-guarded: never touches another user's job."""
    alloc = getattr(conn, "alloc", None)
    if conn.mock or not alloc or not getattr(conn, "executor", None):
        return
    username = getattr(conn.executor, "username", "")
    owner = alloc.owner or username
    if owner and username and owner != username:
        return  # never cancel a job that isn't ours
    try:
        conn.executor.exec(f"scancel {alloc.job_id}")
        conn.emit("warning", "gpu_alloc", f"Released GPU job {alloc.job_id} because {reason} — freed the A100.")
    except Exception as exc:  # noqa: BLE001 - cleanup must never raise
        conn.emit("error", "gpu_alloc", f"Could not scancel job {alloc.job_id} ({reason}): {exc}")


def _optional_user(request: Request):
    """The logged-in AiScientist user, or None (when not authed or accounts disabled)."""
    if not _AUTH_ENABLED:
        return None
    try:
        return auth_routes.current_user(request)
    except Exception:  # noqa: BLE001 - auth must never break the core flow
        return None


@app.post("/api/connect")
async def connect(req: ConnectRequest, request: Request) -> JSONResponse:
    settings = HPCSettings.from_env()
    if req.host:
        settings.host = req.host
    if req.model:
        settings.vllm_model = req.model
    if not req.mock and not req.campus_network_confirmed:
        return JSONResponse(
            {"error": "Confirm you are on the UCI campus network or VPN before connecting.", "stage": "precheck"},
            status_code=400,
        )
    if not req.mock and req.auth_method == "password_duo" and not req.password:
        return JSONResponse(
            {"error": "Password is required for password+Duo login.", "stage": "precheck"},
            status_code=400,
        )

    loop = asyncio.get_running_loop()
    # Two-layer identity: the AiScientist account owns the workspace/history; req.ucinetid
    # is only the HPC3 SSH login (used by the provisioning thread). When logged in, the
    # app user is the workspace owner so datasets/runs bind to the right account.
    app_user = _optional_user(request)
    owner_name = app_user.username if app_user else req.ucinetid
    conn = Connection(settings, mock=req.mock, loop=loop, username=owner_name)
    conn.app_user_id = app_user.id if app_user else None
    CONNECTIONS[conn.id] = conn

    # One shot: SSH login + GPU allocation + vLLM serve, then the health monitor. The session
    # only becomes usable at the end of it ("ready") — there is deliberately no half-connected
    # state where SSH is up but the model is not.
    async def runner() -> None:
        try:
            await asyncio.to_thread(_provision_blocking, conn, req)
            conn.monitor_task = asyncio.create_task(_monitor_gpu(conn))
        except GatewayError as exc:
            conn.status = "error"
            conn.emit("error", exc.stage or "provision", exc.message, detail=error_detail(exc))
            await asyncio.to_thread(_cancel_alloc, conn, "the connection failed")
            conn.broadcast_status()
        except Exception as exc:  # noqa: BLE001
            conn.status = "error"
            conn.emit("error", "provision", f"Unexpected failure: {exc}", detail=error_detail(exc))
            await asyncio.to_thread(_cancel_alloc, conn, "the connection failed")
            conn.broadcast_status()

    asyncio.create_task(runner())
    return JSONResponse({"connection_id": conn.id, "status": conn.status, "mock": conn.mock})


def _cred_owner(request: Request, fallback: str = "") -> str:
    """Whose SSH credentials to touch: the logged-in account when accounts are on (so a
    caller can never read another account's keys), else the given UCInetID."""
    user = _optional_user(request)
    if user is not None:
        return safe_name(user.username)
    return safe_name(fallback or "guest")


@app.get("/api/ssh-credentials")
async def list_ssh_credentials(request: Request, user: str = "") -> JSONResponse:
    """Saved SSH keys (public metadata only) the caller can log in with — powers the
    'SSH key' dropdown so a returning user skips password + Duo."""
    from . import ssh_credentials
    return JSONResponse({"credentials": ssh_credentials.list_credentials(_cred_owner(request, user))})


@app.delete("/api/ssh-credentials/{cred_id}")
async def delete_ssh_credential(cred_id: str, request: Request, user: str = "") -> JSONResponse:
    from . import ssh_credentials
    if not ssh_credentials.delete_credential(_cred_owner(request, user), cred_id):
        return JSONResponse({"error": "Credential not found."}, status_code=404)
    return JSONResponse({"status": "ok"})



@app.get("/api/connections/{connection_id}")
async def get_connection(connection_id: str) -> JSONResponse:
    conn = CONNECTIONS.get(connection_id)
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    return JSONResponse(conn.summary())


@app.get("/api/artifacts/{owner}/{run_id}/{name}")
async def get_artifact(owner: str, run_id: str, name: str) -> Response:
    # Served from disk (not from an active connection) so links keep working
    # after a reconnect/restart. Path is validated to stay under runs/console.
    if not all(part.replace("-", "").isalnum() for part in (owner, run_id)):
        return Response("Bad request", status_code=400)
    base = (CONSOLE_RUNS_DIR / owner / run_id / "artifacts").resolve()
    if not str(base).startswith(str(CONSOLE_RUNS_DIR)) or not base.is_dir():
        return Response("Artifact not found", status_code=404)
    for path in base.glob("*"):
        if path.is_file() and (path.stem == name or path.name == name):
            content_type = mimetypes.guess_type(path.name)[0] or "text/plain; charset=utf-8"
            return Response(
                path.read_bytes(),
                media_type=content_type,
                headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
            )
    return Response("Artifact not found", status_code=404)


_PREVIEW_KIND = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image", ".svg": "image",
    ".csv": "table", ".tsv": "table",
    ".md": "markdown", ".pdf": "pdf",
    ".txt": "text", ".log": "text", ".json": "text", ".py": "text", ".r": "text", ".sh": "text", ".rnk": "text",
}


def _file_kind(name: str) -> str:
    return _PREVIEW_KIND.get(Path(name).suffix.lower(), "other")


# Preference order for picking a folder's "primary" data file — the one handed to the typed tools
# (a matrix → the QC/DE line; a VCF → annotate_variants / run_lirical). The whole folder stays
# reachable to run_code regardless of this pick.
#
# ORDER = specificity, not popularity. The unambiguous formats come first (an .h5ad IS a matrix, a
# .vcf IS a callset); the generic text formats are last because in a real upload they are usually a
# README or a sample sheet sitting next to the actual data — so a folder holding `case.vcf.gz` and
# `notes.txt` must resolve to the VCF, never the note.
_PRIMARY_SUFFIXES = (".h5ad", ".vcf.gz", ".vcf", ".bcf", ".h5", ".loom", ".csv", ".tsv", ".txt")


def _primary_suffix(name: str) -> "str | None":
    """The dataset suffix ``name`` ends with, or None if it is not a recognized dataset file.
    Matched LONGEST-first so a two-part extension wins: ``case.vcf.gz`` is a VCF, and must not be
    read as a bare ``.gz`` (nor missed entirely, which is what last-suffix-only matching did — the
    bgzipped VCF is the normal form for a WGS callset)."""
    low = (name or "").lower()
    for suffix in sorted(_PRIMARY_SUFFIXES, key=len, reverse=True):
        if low.endswith(suffix):
            return suffix
    return None


def _primary_rank(name: str, depth: int, size: int) -> "tuple | None":
    """Sort key for one candidate (lower = better): recognized-format priority → shallowest →
    largest. None when the file is not a dataset at all. ONE ranking, shared by the local and the
    remote finders — they used to implement it twice and had already drifted apart."""
    suffix = _primary_suffix(name)
    if suffix is None:
        return None
    return (_PRIMARY_SUFFIXES.index(suffix), depth, -size)


# --- multi-file bind-set (feature ②) -----------------------------------------
# A run may bind a SET of data files (VCF + BED panel + a 2nd VCF), not just one. This normalizes a
# LabRequest's two dataset slots into ONE ordered list of bound files, primary first. It is the single
# place that reconciles the legacy single ``dataset_path`` with the new ``datasets`` list, so both the
# staging path and any consumer see the same view.
_MAX_BOUND_DATASETS = 12   # a generous cap so a runaway client can't bind an unbounded fan-out


def _bound_primary_key(path: str, order: int) -> tuple:
    """Sort key for one bound file (lower = better): recognized-format priority (unknown last) →
    original client order. No stat/SSH — bind-set ordering must be cheap and deterministic at request
    time (unlike ``_primary_rank``, which ranks files WITHIN one already-local folder by size)."""
    suffix = _primary_suffix(path.rsplit("/", 1)[-1])
    rank = _PRIMARY_SUFFIXES.index(suffix) if suffix is not None else len(_PRIMARY_SUFFIXES)
    return (rank, order)


def _select_bound_datasets(req: "LabRequest") -> list[dict]:
    """The run's bound data files as an ordered ``[{path, name, role}]``, PRIMARY first.

    Backward-compatible by construction:
      * ``req.datasets`` present & non-empty → use it (entries lacking a real ``path`` dropped,
        de-duplicated by path, sorted so the highest-ranked file is primary);
      * else ``req.dataset_path`` set → a single-entry list holding exactly that path (this is the
        legacy single-file behaviour, unchanged);
      * else → ``[]`` (no data bound).

    The primary (index 0) is what every legacy ``decisions["dataset_path"]`` consumer reads; the full
    list is what the multi-file consumers read. Never raises."""
    raw: list[dict] = []
    seen: set[str] = set()
    entries = req.datasets if isinstance(getattr(req, "datasets", None), list) else []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        p = str(e.get("path") or "").strip()
        if not p or p in seen:
            continue
        seen.add(p)
        raw.append({"path": p, "order": i,
                    "name": str(e.get("name") or p.rsplit("/", 1)[-1]),
                    "role": (str(e["role"]).strip() or None) if e.get("role") else None})
    if not raw:
        dp = str(getattr(req, "dataset_path", None) or "").strip()
        if not dp:
            return []
        raw = [{"path": dp, "order": 0, "name": dp.rsplit("/", 1)[-1], "role": None}]
    raw.sort(key=lambda d: _bound_primary_key(d["path"], d["order"]))
    out = [{"path": d["path"], "name": d["name"], "role": d["role"]} for d in raw]
    return out[:_MAX_BOUND_DATASETS]


# A clinical note is prose, not data. The cap is generous for a real referral letter (~10k words) and
# exists so a mis-attached matrix/CSV can't be shovelled into the prompt as a "note".
_MAX_CASE_NOTE_CHARS = 64_000


def _clean_case_note(text: "str | None") -> str:
    """The attached case note, normalized and bounded. Over-long input is TRUNCATED rather than
    rejected: the clinically dense part of a note is the start, and failing a whole run because a
    letter ran long would be worse than analysing its first 64k chars."""
    note = (text or "").strip()
    return note[:_MAX_CASE_NOTE_CHARS] if note else ""


def _find_primary_matrix(folder: Path) -> Path | None:
    """The most likely primary dataset file inside an uploaded folder, or None."""
    best_key, best_path = None, None
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        key = _primary_rank(p.name, len(p.relative_to(folder).parts), size)
        if key is not None and (best_key is None or key < best_key):
            best_key, best_path = key, p
    return best_path


def _find_primary_matrix_remote(conn: Connection, remote_dir: str) -> str | None:
    """The most likely primary dataset file inside a dfs3b folder — the SAME ranking as
    :func:`_find_primary_matrix`, computed remotely via a single ``find -printf``. None if the
    folder holds no recognized dataset file."""
    base = remote_dir.rstrip("/")
    listing = conn.executor.exec(
        f"find {shlex.quote(base)} -type f -printf '%s\\t%p\\n' 2>/dev/null").out or ""
    best_key, best_path = None, None
    for line in listing.splitlines():
        try:
            size_s, path = line.split("\t", 1)
            size = int(size_s)
        except ValueError:
            continue
        depth = len(path[len(base):].strip("/").split("/"))
        key = _primary_rank(path.rsplit("/", 1)[-1], depth, size)
        if key is not None and (best_key is None or key < best_key):
            best_key, best_path = key, path
    return best_path


def _run_artifacts_base(owner: str, run_id: str) -> Path | None:
    """Validated ``<results>/<owner>/<run_id>/artifacts`` dir, or None."""
    if not all(part.replace("-", "").isalnum() for part in (owner, run_id)):
        return None
    base = (CONSOLE_RUNS_DIR / owner / run_id / "artifacts").resolve()
    if not str(base).startswith(str(CONSOLE_RUNS_DIR)) or not base.is_dir():
        return None
    return base


@app.get("/api/files/{owner}/{run_id}")
async def list_files(owner: str, run_id: str) -> JSONResponse:
    """List ALL files of a run (recursively, incl. nested figures/tables) so the UI
    can render a browsable tree + thumbnails + previews — not just the curated items."""
    base = _run_artifacts_base(owner, run_id)
    if base is None:
        return JSONResponse({"files": []})
    files = [
        {
            "path": p.relative_to(base).as_posix(),
            "name": p.name,
            "kind": _file_kind(p.name),
            "size": p.stat().st_size,
            "url": f"/api/file/{owner}/{run_id}/{p.relative_to(base).as_posix()}",
        }
        for p in sorted(base.rglob("*")) if p.is_file()
    ]
    return JSONResponse({"owner": owner, "run_id": run_id, "files": files,
                         "bundle_url": f"/api/bundle/{owner}/{run_id}"})


@app.get("/api/file/{owner}/{run_id}/{path:path}")
async def get_file(owner: str, run_id: str, path: str) -> Response:
    """Serve a single (possibly nested) artifact file INLINE for preview (images,
    PDF, csv, markdown). Path-traversal guarded to stay under the run's artifacts."""
    base = _run_artifacts_base(owner, run_id)
    if base is None:
        return Response("Not found", status_code=404)
    target = (base / path).resolve()
    if base not in target.parents or not target.is_file():
        return Response("Not found", status_code=404)
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return Response(target.read_bytes(), media_type=content_type)


@app.post("/api/results/delete")
async def delete_results(payload: dict, request: Request) -> JSONResponse:
    """Delete a finished run's artifacts on the gateway host.

    STRICT BOUNDARY: a user can only ever delete inside their OWN folder. When
    accounts are enabled the owner is FORCED to the authenticated user — the client's
    ``owner`` value is ignored — so no one can delete another account's runs. The
    run_id is alnum-validated and the resolved path is confined to ``<results>/<owner>``
    via ``is_relative_to``. Idempotent: a missing dir is still a success.
    """
    run_id = str(payload.get("run_id", ""))
    if _AUTH_ENABLED:
        user = _optional_user(request)
        if user is None:
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        owner = safe_name(user.username)              # forced to the caller, never client-supplied
    else:
        owner = safe_name(str(payload.get("owner", "")))   # single-user/dev fallback (accounts off)
    if not owner or not run_id.replace("-", "").isalnum():
        return JSONResponse({"error": "bad owner/run_id"}, status_code=400)
    owner_root = (CONSOLE_RUNS_DIR / owner).resolve()
    target = (owner_root / run_id).resolve()
    # Confine to the owner's own subtree (never the results root, never escaping it).
    if not target.is_relative_to(owner_root) or target in (owner_root, CONSOLE_RUNS_DIR):
        return JSONResponse({"error": "path outside your own results dir"}, status_code=403)
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    return JSONResponse({"ok": True, "deleted": str(target.relative_to(CONSOLE_RUNS_DIR))})


@app.post("/api/disconnect")
async def disconnect(payload: dict) -> JSONResponse:
    conn = CONNECTIONS.get(str(payload.get("connection_id", "")))
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    conn.status = "disconnected"
    if conn.monitor_task:
        conn.monitor_task.cancel()
    if conn.executor:
        await asyncio.to_thread(conn.executor.close)
    conn.emit("info", "disconnect", "Session closed. The GPU job keeps running until its time limit or scancel.")
    conn.broadcast_status()
    return JSONResponse({"status": "disconnected"})


@app.post("/api/auth/duo")
async def auth_duo(payload: dict) -> JSONResponse:
    conn = CONNECTIONS.get(str(payload.get("connection_id", "")))
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    conn.duo_value = (str(payload.get("response", "")).strip() or "1")
    conn.pending_duo = None
    conn.duo_event.set()
    return JSONResponse({"status": "submitted"})


def _release_my_gpu(conn: Connection) -> tuple[list[str], str | None]:
    """scancel THIS user's serve job(s) over the already-authenticated SSH connection.

    Live job ids are discovered with ``squeue --me --name=<jobname>`` rather than the
    cached ``conn.alloc`` (which can be stale, or gone after a reload) — so the user can
    always free their own A100 from the same session WITHOUT re-doing Duo. ``--me`` scopes
    strictly to the caller's own jobs, so this can never touch another lab member's job.
    Returns ``(cancelled_ids, error)``."""
    ex = conn.executor
    username = getattr(ex, "username", "")
    ids: list[str] = []
    try:
        res = ex.exec(
            f"squeue --me --name={gpu.job_name(username)} --states=R,PD,CF,CG "
            "--noheader --format='%i'"
        )
        ids = [ln.strip() for ln in (res.out or "").splitlines() if ln.strip()]
    except Exception as exc:  # noqa: BLE001 - fall back to the cached id below
        if not (conn.alloc and conn.alloc.job_id):
            return [], f"could not query your jobs (squeue): {exc}"
    # Belt-and-suspenders: include the cached allocation id if squeue missed it (also
    # this user's own job, so still ownership-safe).
    if conn.alloc and conn.alloc.job_id and conn.alloc.job_id not in ids:
        ids.append(conn.alloc.job_id)
    if not ids:
        return [], None  # nothing running for this user — already free
    try:
        res = ex.exec("scancel " + " ".join(ids))  # ids came from `--me`; never another user's
    except Exception as exc:  # noqa: BLE001
        return [], f"scancel failed: {exc}"
    # exec() does NOT raise on a non-zero exit, so surface a real scancel error (e.g. exit 210
    # "Access/permission denied") instead of reporting a kill that never happened.
    if not res.ok and (res.stderr or "").strip():
        return [], f"scancel failed: {res.stderr.strip()[:200]}"
    # CONFIRM the kill. `scancel` exits 0 even for a job id that does not exist (verified on HPC3),
    # so its exit status alone can never tell us the job actually died — only re-querying can. Slurm
    # needs a moment to move a job R -> CG -> gone, so poll briefly rather than checking once.
    still: set[str] = set()
    for _ in range(8):
        time.sleep(1.0)
        try:
            chk = ex.exec(
                f"squeue --me --name={gpu.job_name(username)} --states=R,PD,CF,CG "
                "--noheader --format='%i'"
            )
            still = {ln.strip() for ln in (chk.out or "").splitlines() if ln.strip()} & set(ids)
        except Exception:  # noqa: BLE001 - a failed check is not proof the job survived; retry
            still = set()
        if not still:
            break
    if still:
        return [], ("scancel ran but job(s) " + ", ".join(sorted(still))
                    + " are still on HPC3 — verify with `squeue --me`")
    return ids, None


@app.post("/api/stop-gpu")
async def stop_gpu(payload: dict) -> JSONResponse:
    conn = CONNECTIONS.get(str(payload.get("connection_id", "")))
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    if not conn.executor:
        return JSONResponse({"error": "No live HPC3 connection to run scancel on. "
                                      "Reconnect, or run `scancel --me` on HPC3."}, status_code=409)
    # Stop the health monitor FIRST so a poll already in flight can't emit a spurious
    # dead-port / vanished-job error after we cancel; flip status so the loop exits.
    conn.status = "disconnected"
    if conn.monitor_task:
        conn.monitor_task.cancel()
    ids, err = await asyncio.to_thread(_release_my_gpu, conn)
    if err:
        conn.emit("error", "gpu_alloc", f"GPU release failed: {err}")
        return JSONResponse({"error": err}, status_code=500)
    if not ids:
        conn.emit("info", "gpu_alloc", "No running GPU job found for you — nothing to release.")
        conn.broadcast_status()
        return JSONResponse({"status": "no_job", "message": "No running GPU job found for you."})
    conn.emit(
        "warning",
        "gpu_alloc",
        f"Released your GPU job(s) {', '.join(ids)} via scancel — A100 freed, SU charge stopped. "
        "No other lab member's jobs are affected.",
    )
    conn.broadcast_status()
    return JSONResponse({"status": "gpu_stopped", "job_id": ids[0], "job_ids": ids})


def _hpc_user(conn: Connection) -> str:
    """This session's HPC3 account name (the UCInetID we SSH as)."""
    return getattr(conn.executor, "username", None) or conn.owner


def _storage_base(conn: Connection) -> str:
    """The user's PERSONAL HPC3 lab dir. Legacy: everything AiScientist generates used to land
    here. Nothing new is written under it and the sweeper never touches it — it stays readable
    (and browsable/deletable by hand from the storage panel) so old runs aren't lost."""
    return f"{conn.settings.lab_storage}/{_hpc_user(conn)}"


def _shared_dir(conn: Connection, kind: str) -> str:
    """A per-user dir under the SHARED AiScientist project root, e.g. ``uploads``/``pysrc``.
    See ``HPCSettings.shared_root`` for the layout and why process files left personal dirs."""
    return f"{conn.settings.shared_root.rstrip('/')}/{kind}/{_hpc_user(conn)}"


def _temp_base(conn: Connection) -> str:
    """This user's process-file area — ``<shared_root>/Temp/<user>``. EVERYTHING written here is
    disposable and is swept once its subtree goes untouched for ``temp_ttl_days`` (see hpc_gc).
    Never put raw uploads or a deliverable here."""
    return _shared_dir(conn, "Temp")


# --- uploads on HPC3 dfs3b (Phase 2 of the offload; opt-in via uploads_on_hpc) ---

def _hpc_uploads_dir(conn: Connection) -> str:
    """The user's uploads area on HPC3 dfs3b — raw research data, deliberately OUTSIDE Temp so
    the sweeper can never reach it (manual deletion only, from the storage panel)."""
    return _shared_dir(conn, "uploads")


def _hpc_uploads_dirs(conn: Connection) -> tuple[str, ...]:
    """Every uploads dir a dataset of this user's may legitimately live in: the shared one above
    plus the legacy ``<lab_storage>/<user>/uploads`` written before the shared root existed. Used
    as the delete guard so an OLD dataset row is still removable."""
    return (_hpc_uploads_dir(conn), f"{_storage_base(conn)}/uploads")


def _uploads_on_hpc(conn: Connection) -> bool:
    """True when uploads for this session should land on HPC3 dfs3b — the flag is on AND
    there's an SSH executor to put_file with."""
    return bool(conn.settings.uploads_on_hpc and conn.executor is not None)


def _is_remote_dataset(conn: Connection, path: str) -> bool:
    """A dataset path that lives on HPC3 dfs3b (vs the eyeserver's local workspace). Both the lab
    dir and the shared project root count — the latter is normally *inside* the former, but stays
    a separate check so a shared_root pointed elsewhere is still recognised as remote."""
    roots = (conn.settings.lab_storage.rstrip("/"), conn.settings.shared_root.rstrip("/"))
    return bool(path) and any(str(path).startswith(r + "/") for r in roots if r)


def _stage_upload_to_hpc(conn: Connection, local_path: Path, rel: str) -> str:
    """Move a just-received local upload onto the user's HPC3 dfs3b uploads dir and delete the
    local copy (raw data must not linger on the eyeserver). Returns the remote dfs3b path."""
    remote_path = f"{_hpc_uploads_dir(conn)}/{rel}"
    parent = remote_path.rsplit("/", 1)[0]
    conn.executor.exec(f"mkdir -p {shlex.quote(parent)}")
    conn.executor.put_file(str(local_path), remote_path)
    try:
        local_path.unlink()
    except OSError:
        pass
    return remote_path


def _primary_dataset_record(primary: dict, decisions: dict) -> dict:
    """The bind-set record for the PRIMARY file, built from the decisions the primary-staging branches
    already populated (``dataset_path`` = the local readable path or resolved folder-primary;
    ``hpc_primary`` = the dfs3b path when remote). Keeps the whole set uniform with the secondaries."""
    local = str(decisions.get("dataset_path") or primary.get("path") or "")
    return {
        "path": local,
        "name": (local.rsplit("/", 1)[-1] if local else primary.get("name")) or primary.get("name"),
        "role": primary.get("role"),
        "hpc_primary": decisions.get("hpc_primary"),
        "remote": bool(decisions.get("hpc_primary")),
        "primary": True,
    }


def _stage_secondary_dataset(conn: Connection, path: str, name: str, role: "str | None",
                             staged_dir: Path) -> dict:
    """Stage ONE secondary bound file (a BED panel, a 2nd VCF, …) and return its bind-set record.

    A dfs3b file is staged back to a local readable copy (so the still-local tools + the code sandbox
    can read it) while ``hpc_primary`` remembers the in-place dfs3b path for an on-HPC step; a local
    file is used where it already is. No smoke-analysis is run on a secondary — the primary drives the
    scanpy/VEP tools; secondaries are reachable by run_code (via the exposed uploads tree) and by any
    step that reads the recorded paths. SSH-capable, so call via ``asyncio.to_thread``."""
    remote = conn.executor is not None and _is_remote_dataset(conn, path)
    if remote:
        local = _ensure_local_dataset(conn, path, staged_dir)
        return {"path": str(local), "name": name, "role": role,
                "hpc_primary": path, "remote": True, "primary": False}
    return {"path": path, "name": name, "role": role,
            "hpc_primary": None, "remote": False, "primary": False}


def _ensure_local_dataset(conn: Connection, path: str, cache_dir: Path) -> Path:
    """Return a LOCAL readable path for a dataset. If it lives on HPC3 dfs3b, stage it back
    into cache_dir via get_file so the still-local analysis tools can read it; otherwise it is
    already local. (When analysis moves to HPC3 this stage-back is skipped — the batch job
    reads dfs3b in place.)"""
    if conn.executor is not None and _is_remote_dataset(conn, path):
        cache_dir.mkdir(parents=True, exist_ok=True)
        local = cache_dir / path.rsplit("/", 1)[-1]
        conn.executor.get_file(path, str(local))
        return local
    return Path(path)


# --- content-based ingest triage (feature ①: "skim any uploaded file, get the gist") ----------
# Split in time ON PURPOSE, so an upload never depends on a live model:
#   * the DETERMINISTIC peek runs at upload, on the still-local file, needing no model;
#   * the LLM DESCRIBE runs only on demand (/api/dataset/describe) when a model is already up, and
#     NEVER provisions one — it degrades to the peek instead.
_INGEST_PEEK_MAX_BYTES = 262_144


def _peek_local_upload(local_path: Path) -> "dict | None":
    """Deterministic content peek of a just-received LOCAL upload (+ a one-line gist). Best-effort:
    ANY failure returns None so triage can never break an upload. No model is involved."""
    try:
        from ..tools.dataset_inspect import describe_dataset, peek_dataset
        peek = peek_dataset(str(local_path), max_bytes=_INGEST_PEEK_MAX_BYTES)
        peek["gist"] = describe_dataset(peek, chat_fn=None).get("one_line_summary", "")
        return peek
    except Exception as exc:  # noqa: BLE001 - triage is additive; never fail the upload over it
        print(f"[ingest] peek failed for {local_path}: {exc}")
        return None


def _remote_head_bytes(conn: Connection, remote_path: str, max_bytes: int) -> "tuple[bytes, int]":
    """A binary-safe bounded HEAD of a dfs3b file + its true size, read over SFTP.

    Reads only ``max_bytes`` — never pulls a whole WGS VCF back to the gateway. This used to be
    ``head -c … | base64 | tr`` over the SSH channel, which spawned three processes on a LOGIN
    node and inflated every peek by a third; file content is a transfer, so it now rides the
    transfer host with the rest of the staging."""
    return (conn.executor.read_bytes(remote_path, max_bytes),
            conn.executor.remote_size(remote_path))


def _peek_dataset_any(conn: Connection, path: str, name: str) -> dict:
    """Deterministic peek of a dataset that may live LOCALLY on the gateway or on dfs3b. For a remote
    file only a bounded head is fetched (binary-safe) and peeked via a temp file; ``size_hint`` keeps
    the reported size honest. Never raises (peek_dataset itself is total)."""
    from ..tools.dataset_inspect import peek_dataset
    if conn.executor is not None and _is_remote_dataset(conn, path):
        import tempfile
        raw, true_size = _remote_head_bytes(conn, path, _INGEST_PEEK_MAX_BYTES)
        with tempfile.NamedTemporaryFile(prefix="peek_", suffix="_" + safe_filename(name),
                                         delete=False) as tf:
            tf.write(raw)
            tmp = tf.name
        try:
            peek = peek_dataset(tmp, max_bytes=_INGEST_PEEK_MAX_BYTES,
                                size_hint=true_size or None, name=name)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        peek["remote"] = True
        return peek
    return peek_dataset(path, max_bytes=_INGEST_PEEK_MAX_BYTES, name=name)


# The compact slice of a feature-① description we keep per bound file (drops the verbose key_facts).
_TRIAGE_DESC_KEYS = ("file_kind", "format", "assembly", "sample_ids", "likely_modality",
                     "one_line_summary", "confidence", "source")


def _triage_bound_datasets(conn: Connection, decisions: dict, model: str) -> list[str]:
    """Feature ② Phase C — describe each bound file at RUN START, once a served model exists.

    For every ``decisions["datasets"]`` record: peek it (the local staged copy, else a bounded remote
    head) and run feature ①'s :func:`describe_dataset` over this session's tunnel (``think=False``, the
    same load-bearing choice as ``/api/dataset/describe``). The GPU is already up for the run, so this
    adds NO provisioning. Stamps a compact ``description`` onto each record and sets
    ``decisions["content_modality"]`` / ``["content_confidence"]`` from the PRIMARY, so Phase B routes
    on the file's CONTENT rather than its extension. Even with the model unreachable, describe_dataset
    degrades to a deterministic peek-only description — so content routing still improves on the suffix.

    Pure w.r.t. the event loop (no ``conn.push``) so it is safe to run in a worker thread; returns the
    per-file gist lines for the caller to surface. Best-effort: never raises."""
    records = decisions.get("datasets") or []
    if not records:
        return []
    try:
        from ..tools.dataset_inspect import describe_dataset, peek_dataset
    except Exception:  # noqa: BLE001 - triage is additive; its absence must never fail a run
        return []

    def _chat(messages: list[dict]) -> str:
        # think=False is load-bearing (a bounded call to the served reasoning model can spend its whole
        # budget on the thinking trace and return empty content).
        return vllm_client.complete(getattr(conn, "tunnel_port", None), model, messages,
                                    max_tokens=1200, timeout=90.0, think=False)

    gists: list[str] = []
    for rec in records:
        try:
            name = str(rec.get("name") or "")
            local = str(rec.get("path") or "")
            if local and os.path.exists(local):
                peek = peek_dataset(local, max_bytes=_INGEST_PEEK_MAX_BYTES, name=name)
            else:
                peek = _peek_dataset_any(conn, str(rec.get("hpc_primary") or local), name)
            desc = describe_dataset(peek, chat_fn=_chat)
            rec["description"] = {k: desc.get(k) for k in _TRIAGE_DESC_KEYS}
            gists.append(f"🔎 {name}: "
                         + str(desc.get("one_line_summary") or desc.get("file_kind") or "skimmed"))
        except Exception as exc:  # noqa: BLE001 - one bad file must not sink the whole triage
            rec.setdefault("description", {"error": f"{type(exc).__name__}: {exc}"[:200]})
    primary = next((r for r in records if r.get("primary")), records[0])
    pdesc = primary.get("description") or {}
    if pdesc.get("likely_modality"):
        decisions["content_modality"] = pdesc["likely_modality"]
        decisions["content_confidence"] = pdesc.get("confidence") or ""
    return gists


def _remote_path_status(executor: Any, path: str, *, is_dir: bool) -> "bool | None":
    """Does ``path`` exist on the remote host? True/False, or None if the check itself failed (SSH
    error) — so a diagnostic can distinguish "definitely missing" from "couldn't verify"."""
    if not path:
        return None
    flag = "-d" if is_dir else "-e"
    try:
        r = executor.exec(f"test {flag} {shlex.quote(path)} && echo __OK__ || echo __MISSING__")
        text = (getattr(r, "stdout", "") or "")
        if "__OK__" in text:
            return True
        if "__MISSING__" in text:
            return False
        return None
    except Exception:  # noqa: BLE001 - a failed check is "unknown", never fatal
        return None


def _variant_offline_preflight(executor: Any, vep_image: str, cache_dir: str, clinvar: str) -> dict:
    """Cheap HPC3 existence checks for the offline VEP stack (sif image + VEP cache dir + ClinVar VCF).
    Returns ``{name: (path, exists?)}`` where exists? is True / False / None(unverifiable). SSH
    round-trips, so call via ``asyncio.to_thread``. This is the check that turns a silent
    "fell back to REST" into "clinvar_vcf NOT found on HPC3: <path>"."""
    return {
        "vep_image": (vep_image, _remote_path_status(executor, vep_image, is_dir=False)),
        "cache_dir": (cache_dir, _remote_path_status(executor, cache_dir, is_dir=True)),
        "clinvar_vcf": (clinvar, _remote_path_status(executor, clinvar, is_dir=False)),
    }


def _phenotype_preflight(executor: Any, lirical_image: str, data_dir: str,
                         exomiser_dir: str = "") -> dict:
    """Cheap HPC3 existence checks for the LIRICAL stack (sif image + LIRICAL data dir + the optional
    Exomiser data dir for genotype-aware scoring). Returns ``{name: (path, exists?)}`` with exists? =
    True / False / None(unverifiable). SSH round-trips, so call via ``asyncio.to_thread``. Turns a silent
    ``not_installed`` into ``lirical_data_dir NOT found on HPC3: <path>``."""
    checks = {
        "lirical_image": (lirical_image, _remote_path_status(executor, lirical_image, is_dir=False)),
        "lirical_data_dir": (data_dir, _remote_path_status(executor, data_dir, is_dir=True)),
    }
    if exomiser_dir:   # only when genotype-aware is configured (else phenotype-only needs no Exomiser)
        checks["exomiser_dir"] = (exomiser_dir, _remote_path_status(executor, exomiser_dir, is_dir=True))
    return checks


def _detect_vcf_assembly(conn: "Connection", decisions: dict) -> str:
    """Infer the uploaded VCF's genome build ('GRCh37' | 'GRCh38' | '') from its header — the dfs3b
    copy via a bounded remote ``head``, or a local staged copy. '' when unreadable/unknown so the
    caller keeps its configured default. Header-only: never streams the whole (WGS-size) VCF."""
    from ..tools.vcf_offline import detect_assembly
    remote = decisions.get("hpc_primary")
    local = decisions.get("dataset_path")
    try:
        if remote and conn.executor is not None:
            r = conn.executor.exec(
                f"zcat -f {shlex.quote(str(remote))} 2>/dev/null | head -n 3000 | grep '^#' || true")
            return detect_assembly(r.stdout or "")
        if local and os.path.exists(local):
            import gzip
            opener = gzip.open if str(local).endswith(".gz") else open
            buf: list[str] = []
            with opener(local, "rt", encoding="utf-8", errors="replace") as fh:  # type: ignore[call-overload]
                for i, line in enumerate(fh):
                    if not line.startswith("#") or i > 3000:
                        break
                    buf.append(line)
            return detect_assembly("".join(buf))
    except Exception:  # noqa: BLE001 - detection is best-effort; any failure keeps the configured default
        return ""
    return ""


def _sync_bioagent_source_to_hpc(conn: Connection) -> str:
    """Tar the LIVE bioagent package, push it to the user's dfs3b ``pysrc`` dir and untar it — so an
    analysis job can bind + import the CURRENT tools with NO image rebuild (the image carries only
    the heavy deps). Cached per session (``conn.hpc_pysrc``). Returns the PYTHONPATH root on dfs3b
    (the dir that contains ``bioagent/``). Raises on transport failure — caller falls back."""
    if conn.hpc_pysrc:
        return conn.hpc_pysrc
    import bioagent
    import tarfile
    import tempfile

    pkg_dir = Path(bioagent.__file__).resolve().parent           # .../src/bioagent
    # Deliberately NOT under Temp/: jobs bind this read-only for their whole lifetime, and it is
    # rewritten in place on every connect, so it neither grows nor benefits from a sweep.
    pysrc = _shared_dir(conn, "pysrc")
    fd, tmp = tempfile.mkstemp(suffix=".tgz")
    os.close(fd)
    try:
        def _filt(ti: "tarfile.TarInfo"):
            return None if ti.name.endswith(".pyc") or "__pycache__" in ti.name else ti
        with tarfile.open(tmp, "w:gz") as tar:
            tar.add(str(pkg_dir), arcname="bioagent", filter=_filt)
        remote_tgz = f"{pysrc}/bioagent-src.tgz"
        r = conn.executor.exec(f"mkdir -p {shlex.quote(pysrc)}")
        if not r.ok:
            raise RuntimeError(f"mkdir {pysrc} failed: {r.stderr}")
        conn.executor.put_file(tmp, remote_tgz)
        r = conn.executor.exec(
            f"rm -rf {shlex.quote(pysrc)}/bioagent && tar xzf {shlex.quote(remote_tgz)} -C {shlex.quote(pysrc)}")
        if not r.ok:
            raise RuntimeError(f"untar on HPC3 failed: {r.stderr}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    conn.hpc_pysrc = pysrc
    return pysrc


def _build_literature_executor(conn: "Connection") -> Any:
    """Offload deep_literature (PaperQA) to HPC3 (paperqa.sif), or None to keep it disabled.

    PaperQA cannot run in-process on the eyeserver: the PubMedBERT index lives on /dfs3b, which the
    gateway can't read in place. When BIOAGENT_PAPERQA_ON_HPC is set (and there is a live SSH session
    + a GPU serve job whose node hosts Qwen), run paperqa_cli inside paperqa.sif on HPC3, reaching the
    served Qwen at the GPU node's own host:port (the offload branch in paperqa_search._local_endpoint).
    Returns None (deep_literature simply absent) whenever anything required is missing — so the
    feature is fully OPT-IN and its absence never changes existing behaviour.
    """
    st = conn.settings
    image = os.environ.get("BIOAGENT_PAPERQA_IMAGE", "")
    # Opt-in guard: build the executor ONLY when the feature is enabled AND a live HPC3 GPU session
    # exists — else return None so deep_literature is simply absent (no behaviour change).
    if (not os.environ.get("BIOAGENT_PAPERQA_ON_HPC") or not image
            or conn.executor is None or conn.mock or conn.alloc is None):
        return None
    retigene = os.environ.get("BIOAGENT_PAPERQA_ROOT", "")
    index_dir = os.environ.get("BIOAGENT_PAPERQA_INDEX_DIR", "")
    papers = os.environ.get("BIOAGENT_PAPERQA_PAPERS", "")
    manifest = os.environ.get("BIOAGENT_PAPERQA_MANIFEST", "")
    embedding = os.environ.get("BIOAGENT_PAPERQA_EMBEDDING", "st-NeuML/pubmedbert-base-embeddings")
    index_name = os.environ.get("BIOAGENT_PAPERQA_INDEX_NAME", "retigene_full_pubmedbert")
    try:
        pysrc = _sync_bioagent_source_to_hpc(conn)   # live bioagent src, bound + PYTHONPATH'd in-sif
    except Exception:  # noqa: BLE001 - HPC sync failed → feature simply absent this run
        return None
    if not pysrc:
        return None
    # vllm serves --host 0.0.0.0, so any HPC3 node reaches Qwen at the GPU node's own host:port.
    llm_base_url = f"http://{conn.alloc.node}:{conn.alloc.port}/v1"

    def _lit_local_fallback(tool: str, args: dict, ctx: Any) -> dict:
        return {"status": "dependency_missing", "dependency": "paperqa-hpc",
                "note": "deep_literature unavailable this run (HPC down or paperqa.sif not staged)."}

    # Retrieval-breadth / determinism overrides, passed as ARGS rather than env: the paperqa .sif
    # runs with --containall, so the eyeserver's environment never reaches the job. Unset keys are
    # dropped so paperqa_search's in-code defaults stay authoritative — .env only overrides.
    tuning = {key: val for key, val in (
        ("search_count", os.environ.get("BIOAGENT_PAPERQA_SEARCH_COUNT", "")),
        ("evidence_k", os.environ.get("BIOAGENT_PAPERQA_EVIDENCE_K", "")),
        ("max_sources", os.environ.get("BIOAGENT_PAPERQA_MAX_SOURCES", "")),
        ("answer_length", os.environ.get("BIOAGENT_PAPERQA_ANSWER_LENGTH", "")),
        ("concurrency", os.environ.get("BIOAGENT_PAPERQA_CONCURRENCY", "")),
        ("temperature", os.environ.get("BIOAGENT_PAPERQA_TEMPERATURE", "")),
    ) if val}
    from .slurm_analysis import SlurmAnalysisExecutor
    ro_binds = tuple(dict.fromkeys(p for p in (
        retigene, papers, (os.path.dirname(manifest) if manifest else "")) if p))
    return SlurmAnalysisExecutor(
        remote=conn.executor, container_image=image,
        remote_workspace=f"{_temp_base(conn)}/paperqa",
        local_workspace=conn.workspace, source_dir=pysrc,
        entrypoint=(
            f"export HF_HOME={retigene}/hf_cache HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1; "
            "python3 -m bioagent.tools.paperqa_cli"
            if retigene else "python3 -m bioagent.tools.paperqa_cli"
        ),
        job_prefix="bioagent_paperqa",
        scratch_dir=f"{_temp_base(conn)}/scratch/paperqa",
        extra_ro_binds=ro_binds,
        extra_rw_binds=(index_dir,) if index_dir else (),
        # Deploy config the model never sends — merged into the tool-call args before the job runs, so
        # paperqa_cli builds the ctx (Qwen endpoint) and points PaperQA at the pre-built /dfs3b index.
        inject_args={"model": conn.selected_model, "llm_base_url": llm_base_url,
                     "embedding": embedding, "papers": papers, "index_dir": index_dir,
                     "index_name": index_name, "manifest": manifest, **tuning},
        mem_gb=st.run_code_mem_gb, cpus=st.run_code_cpus, partition=st.cpu_partition,
        account=st.cpu_account or "", time_limit=st.run_code_time_limit,
        container_module=st.container_module, container_bin=st.container_bin,
        local_fallback=_lit_local_fallback,
        should_cancel=conn.chat_stop.is_set,
    )


def _active_conn_for_user(user) -> Connection | None:
    """An SSH-connected live session owned by this app user (for remote-file cleanup outside
    a per-connection request). None if the user has no connected session right now."""
    owner = safe_name(getattr(user, "username", "") or "")
    for c in CONNECTIONS.values():
        if c.owner == owner and c.executor is not None:
            return c
    return None


def _storage_areas(conn: Connection) -> list[tuple[str, str]]:
    """The HPC3 dirs this user owns, as ``(label, path)`` — every one of them browsable and
    hand-deletable from the storage panel. ORDER = what a member most likely came here to clean.

    The legacy personal dir is listed LAST and only ever removed by an explicit click: the
    automatic sweeper is confined to ``Temp``, so anything a member put in their own lab folder
    stays until they decide otherwise.
    """
    return [
        ("Temp (auto-cleaned)", _temp_base(conn)),
        ("Uploads", _hpc_uploads_dir(conn)),
        ("Your personal lab folder (never auto-cleaned)", _storage_base(conn)),
    ]


def _list_storage(conn: Connection) -> dict:
    areas = _storage_areas(conn)
    base = areas[0][1]
    if conn.mock:
        deleted = getattr(conn, "mock_deleted", set())
        items = [
            {"name": "analysis/8f2c1d4a", "size": "11G", "path": f"{base}/analysis/8f2c1d4a",
             "area": areas[0][0]},
            {"name": "scratch/runcode", "size": "212M", "path": f"{base}/scratch/runcode",
             "area": areas[0][0]},
            {"name": "old_pbmc_run.h5ad", "size": "880M", "area": areas[2][0],
             "path": f"{areas[2][1]}/old_pbmc_run.h5ad"},
        ]
        return {
            "base": base,
            "used": "28G",
            "quota_raw": "MOCK dfsquotas: /dfs3b/ruic20_lab  used 612G / limit 1.0T (61%)",
            "areas": [{"label": label, "path": path} for label, path in areas],
            "ttl_days": conn.settings.temp_ttl_days,
            "items": [i for i in items if i["path"] not in deleted],
        }
    items: list[dict] = []
    used_parts: list[str] = []
    for label, path in areas:
        # Temp is listed one level deeper (Temp/<kind>/<entry>) because that IS the unit the
        # sweeper works on — showing just "analysis" would hide which runs are actually costing
        # space. The other areas stay at their top level.
        depth = "*/*" if path == base else "*"
        du = conn.executor.exec(
            f"du -sh {shlex.quote(path)}/{depth} 2>/dev/null | sort -rh | head -60")
        for line in du.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].strip():
                item_path = parts[1].strip()
                items.append({"name": item_path[len(path) + 1:] or item_path.rsplit("/", 1)[-1],
                              "size": parts[0].strip(), "path": item_path, "area": label})
        total = conn.executor.exec(f"du -sh {shlex.quote(path)} 2>/dev/null")
        if total.out:
            size = total.out.split("\t")[0].strip()
            used_parts.append(f"{size} {label}")
    quota = conn.executor.exec("dfsquotas 2>/dev/null || echo 'run dfsquotas manually to see limits'")
    return {"base": base, "used": " + ".join(used_parts) or "?",
            "quota_raw": quota.out[:1500],
            "areas": [{"label": label, "path": path} for label, path in areas],
            "ttl_days": conn.settings.temp_ttl_days,
            "items": items}


@app.get("/api/storage/{connection_id}")
async def get_storage(connection_id: str) -> JSONResponse:
    conn = CONNECTIONS.get(connection_id)
    if not conn or not conn.executor:
        return JSONResponse({"error": "Not connected"}, status_code=409)
    try:
        data = await asyncio.to_thread(_list_storage, conn)
    except Exception as exc:  # noqa: BLE001
        conn.emit("error", "storage", f"Could not read HPC3 storage: {exc}", detail=error_detail(exc))
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(data)


@app.post("/api/storage/delete")
async def delete_storage(payload: dict, request: Request) -> JSONResponse:
    conn = CONNECTIONS.get(str(payload.get("connection_id", "")))
    if not conn or not conn.executor:
        return JSONResponse({"error": "Not connected"}, status_code=409)
    # The connection must belong to the calling account (no driving someone else's
    # session). When accounts are enabled, require login + matching ownership.
    if _AUTH_ENABLED:
        user = _optional_user(request)
        if user is None:
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        if conn.app_user_id is not None and conn.app_user_id != user.id:
            return JSONResponse({"error": "This connection belongs to another account."}, status_code=403)
    path = str(payload.get("path", "")).strip()
    # Hard guard: only ever delete inside one of THIS user's own HPC3 dirs. Each area is
    # per-user (…/<ucinetid>), so the shared project root does not widen what a member can
    # reach — Temp/<someone-else> stays out of range exactly like their personal lab dir does.
    allowed = [path_ for _, path_ in _storage_areas(conn)]
    if not path or ".." in path or not any(path.startswith(a + "/") for a in allowed):
        return JSONResponse({"error": "Refusing to delete outside your own HPC3 directories."}, status_code=403)
    try:
        if conn.mock:
            if not hasattr(conn, "mock_deleted"):
                conn.mock_deleted = set()
            conn.mock_deleted.add(path)
        else:
            res = await asyncio.to_thread(conn.executor.exec, f"rm -rf {shlex.quote(path)}")
            if not res.ok:
                return JSONResponse({"error": res.stderr.strip() or "rm failed"}, status_code=500)
        conn.emit("warning", "storage", f"Deleted on HPC3: {path}")
        data = await asyncio.to_thread(_list_storage, conn)
        return JSONResponse({"status": "deleted", **data})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/model")
async def select_model(payload: dict) -> JSONResponse:
    conn = CONNECTIONS.get(str(payload.get("connection_id", "")))
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    model = str(payload.get("model", "")).strip()
    if not model:
        return JSONResponse({"error": "model is required"}, status_code=400)
    leaf = model.rsplit("/", 1)[-1]
    # vLLM loads ONE model into VRAM at serve-launch — it can't pull or hot-swap at
    # runtime. So we can only select among what the server already serves; a
    # different model means reconnecting (which submits a serve job for that model).
    if any(leaf == t.rsplit("/", 1)[-1] for t in conn.available_models):
        conn.selected_model = model
        conn.emit("success", "llm_model", f"Active model switched to {model}.")
        conn.broadcast_status()
        return JSONResponse({"status": "selected", "model": model})
    return JSONResponse(
        {
            "error": f"vLLM serves a single model ({conn.selected_model}) loaded at launch and cannot "
                     f"hot-swap to '{model}'. Set BIOAGENT_VLLM_MODEL and reconnect to serve a different model.",
            "served": conn.available_models,
        },
        status_code=409,
    )


class StopRequest(BaseModel):
    connection_id: str
    conversation_id: str | None = None  # stop only THIS chat's run (not whatever run is live on the
    run_id: str | None = None           # shared session); either identifier resolves the target run


@app.post("/api/chat/stop")
async def chat_stop(req: StopRequest) -> JSONResponse:
    """Cancel the connection's in-flight chat/pipeline run so GPU + compute are
    released — used by the Stop button and by deleting the chat that owns the run."""
    conn = CONNECTIONS.get(req.connection_id)
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    if not conn.chat_running:
        return JSONResponse({"status": "idle"})
    # Target the run named by this request (its own chat/run) — not just "whatever run is live". A
    # Stop from a window whose run already finished must not cancel a DIFFERENT window's run that
    # started since. No identifiers (older client) → the active run, as before.
    target = conn.resolve_run(req.run_id, req.conversation_id)
    if target is None:
        # Keep the isolation guarantee (a Stop that names a DIFFERENT conversation must not cancel the
        # live run — see test_stop_endpoint_targets_only_the_named_conversation), but make a silent
        # no-op DIAGNOSABLE: a "Stop did nothing" almost always means the client sent a conversation_id
        # that drifted from the active run's (e.g. state.runSessionId lost across a WS reconnect). The
        # client now re-adopts the owner id from summary.active_run, but log the miss so any recurrence
        # is visible in journald rather than silent.
        ar = conn.active_run
        print(f"[chat_stop] no-op: req(run_id={req.run_id!r}, conversation_id={req.conversation_id!r}) "
              f"did not match the active run "
              f"({getattr(ar, 'run_id', None)!r}/{getattr(ar, 'conversation_id', None)!r}).")
        return JSONResponse({"status": "idle"})
    target.chat_stop.set()
    conn.emit("info", "chat", "Stop requested — ending the current run.")
    return JSONResponse({"status": "stopping"})


class InjectRequest(BaseModel):
    connection_id: str
    text: str


@app.post("/api/chat/inject")
async def chat_inject(req: InjectRequest) -> JSONResponse:
    """Steer an EXECUTING run without stopping it: queue a note the lab loop folds into
    the remaining steps' guidance. Distinct from plan-review feedback (that re-plans
    before anything runs) — this applies mid-execution."""
    conn = CONNECTIONS.get(req.connection_id)
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    text = (req.text or "").strip()
    if not text:
        return JSONResponse({"error": "Empty note."}, status_code=400)
    if not conn.chat_running:
        return JSONResponse({"error": "No run in progress to steer."}, status_code=409)
    # Only the lab drains injections (between steps). A chat turn has no steps, so accepting one
    # here would queue a note nothing consumes — and it would then be picked up by the NEXT research
    # run, silently steering a study with a sentence the user aimed at a chat message.
    if conn.active_run is not None and conn.active_run.kind == "chat":
        return JSONResponse(
            {"error": "Chat answers can't be steered mid-reply — wait for it to finish, then ask "
                      "a follow-up."}, status_code=409)
    conn.add_injection(text)
    conn.emit("step", "lab", f"Your note is queued for the next step: {text[:80]}")
    return JSONResponse({"status": "queued"})


class CompactRequest(BaseModel):
    connection_id: str
    run_id: str | None = None
    conversation_id: str | None = None


@app.post("/api/lab/compact")
async def lab_compact(req: CompactRequest) -> JSONResponse:
    """The compact command: ask an EXECUTING run to fold its accumulated findings into a summary
    at the next step boundary.

    This is a control flag, not a note. ``/api/chat/inject`` queues PROSE the model reads and may
    or may not act on; "compact now" is an instruction to the loop itself, and the loop — not the
    model — decides which rounds fold and re-attaches the artifact paths afterwards. Routing it
    through the same channel as steering notes would make a deterministic operation depend on the
    model noticing a sentence.
    """
    conn = CONNECTIONS.get(req.connection_id)
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    if not conn.chat_running:
        return JSONResponse({"error": "No run in progress to compact."}, status_code=409)
    target = conn.resolve_run(req.run_id, req.conversation_id) or conn.active_run
    if target is None or target.kind == "chat":
        return JSONResponse({"error": "Only a research run carries a history worth compacting."},
                            status_code=409)
    target.compact_request.set()
    conn.emit("step", "lab", "Compaction requested — it will run at the next step boundary.")
    return JSONResponse({"status": "requested"})


class LabRequest(BaseModel):
    connection_id: str
    question: str
    conversation_id: str | None = None  # the UI chat/thread that owns this run — used to scope the
                                     # WS stream + the "replan vs fresh study" decision to ONE
                                     # conversation, so a different window/tab never inherits it.
    dataset_path: str | None = None  # optional dataset on the gateway host to analyze — the LEGACY
                                     # single-file slot. Still authoritative on its own (old clients,
                                     # resume of old runs); when ``datasets`` is also sent it must hold
                                     # the PRIMARY (see ``_select_bound_datasets`` + ``_followup_target``).
    # The multi-file BIND-SET (feature ②): a run may bind a SET of data files (a VCF + a BED gene
    # panel + a 2nd VCF), not just one. Each entry is ``{path, name?, role?}``. This is ADDITIVE and
    # backward-compatible: when only ``dataset_path`` is sent this stays None and the run behaves
    # exactly as before; when both are sent, the highest-ranked bound file is the primary and drives
    # every legacy ``decisions["dataset_path"]`` consumer, while the whole set is exposed as
    # ``decisions["datasets"]``. Kept SEPARATE from ``case_note`` — that is a TEXT slot, not a data file.
    datasets: list[dict] | None = None
    # The TEXT attachment: the patient's clinical description / case note, carried as TEXT rather
    # than as a file. Its only consumer, map_phenotype_to_hpo, runs IN-PROCESS here on the gateway,
    # never inside a Slurm container. So the note needs neither a dataset row nor a container bind:
    # the browser reads the file's text and posts it with the run. It is deliberately DISTINCT from a
    # data file — a second DATA file (a BED panel, say) now rides in ``datasets`` above (the bind-set),
    # while this stays a text-only clinical note.
    case_note: str | None = None
    plan_mode: bool = False          # if true, pause after the PI plans for user review/edit
    autonomous: bool = False         # BYPASS mode: run end-to-end with NO human gates (no plan
                                     # review, no decision-point pauses). Overrides plan_mode. Stop +
                                     # mid-run notes still work. Default (false) = Manual = gates on.
    preset: str | None = None        # legacy single pre-selected research path (key); see `presets`
    presets: list[str] | None = None  # preset PIPELINE keys to inject for THIS run — the console's
                                     # pipeline multi-select. Their prompts are composed into one
                                     # guidance block; empty/absent = the PI auto-selects.
    skills: list[str] | None = None  # atomic SKILL names (skills/*.py) to REQUIRE for THIS run — the
                                     # console's skill multi-select. The plan must apply each.
    preset_prompt: str | None = None  # the (possibly user-edited) guidance text; overrides preset(s)
    mode: str = "single"             # Axis A: "single" | "team" (Virtual-Lab multi-agent) | "auto"
    planner: str | None = None       # "dag" (default, dependency-DAG + Coordinator) | "linear"
    # Axis B — WHICH ENGINE answers, an axis orthogonal to `mode` (which is about one scientist vs a
    # Virtual-Lab team, and applies only within the research engine):
    #   "research" (default) — the full lab: PI agenda → multi-step execution → assembled report.
    #   "chat"               — the answer-first streaming ReAct loop (agents/quick_chat.py). Tokens
    #                          start immediately, tools are callable, NO plan / report / bundle.
    # Routing is the USER's explicit choice from the composer, never a classifier: a study
    # misrouted into a 4-turn chat loop would return a fluent answer with no analysis behind it,
    # and nothing downstream would flag it. Unknown values fall back to "research" — the safe side.
    route: str = "research"
    # Prior turns of THIS conversation, [{"role": "user"|"assistant", "content": str}, ...].
    # Only the fast path reads it: a chat is a conversation, so "what about the other one?" has to
    # resolve against what was already said. The research path deliberately ignores it — a study is
    # scoped by its question + dataset, and the follow-up router reads the prior run's persisted
    # state instead. Trimmed server-side (see QuickChatConfig.max_history_messages).
    history: list[dict[str, str]] | None = None




class RegenerateReportRequest(BaseModel):
    connection_id: str
    conversation_id: str | None = None  # the chat this regenerate belongs to (scopes the WS stream)
    run_id: str | None = None        # which run's report to rebuild; default = last run on this conn
    instruction: str | None = None   # optional edit directive; None/"" = re-render the existing .md as-is
    basename: str = "report"         # "report" (manuscript) | "technical_report"


class ContinueRunRequest(BaseModel):
    connection_id: str
    conversation_id: str | None = None  # the chat this continuation belongs to (scopes the WS stream)
    run_id: str | None = None        # which run to continue; default = last run on this conn
    from_step_index: int = 0         # 0-based agenda step to REDO (that step + everything after it)
    modify_note: str | None = None   # steering for the redone step ("re-cluster at resolution 1.0")
    edited_step: str | None = None   # optional: replace the redone step's agenda TEXT outright


class PlanReviewRequest(BaseModel):
    connection_id: str
    conversation_id: str | None = None  # target the plan review to THIS chat's run (not whatever run
    run_id: str | None = None           # is live on the shared session); either identifier resolves it
    action: str = "approve"          # "approve" | "revise" | "cancel"
    feedback: str = ""               # natural-language notes for the PI on a "revise"
    # Back-compat: older clients sent {approved, agenda}. `approved=False` maps to cancel.
    approved: bool | None = None
    agenda: list[str] = []


@app.post("/api/upload")
async def upload_dataset(
    connection_id: str = Form(...),
    file: UploadFile = File(...),
    rel_path: str = Form(""),
) -> JSONResponse:
    """Receive a dataset file from the browser and save it under THIS connection's
    per-user workspace on the gateway host (multi-user safe). Returns the server-side
    path the lab/pipeline analyzes — the raw file stays on the server; only derived
    metrics ever reach the LLM.

    When ``rel_path`` is set (a folder upload), the file's NESTED position is preserved
    under uploads/<rel_path> so a whole multi-level folder round-trips intact. Folder
    files are NOT recorded as individual datasets — the folder is registered once via
    /api/upload/register-folder after all its files land."""
    conn = CONNECTIONS.get(connection_id)
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    dest_dir = conn.workspace / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_rel = safe_relpath(rel_path) if rel_path else ""
    if safe_rel:
        dest = dest_dir / safe_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        name = dest.name
    else:
        # Never overwrite an existing upload of the same name — uniquify instead.
        name = _unique_child(dest_dir, safe_filename(file.filename or "dataset"))
        dest = dest_dir / name
    try:
        with open(dest, "wb") as out:
            await asyncio.to_thread(shutil.copyfileobj, file.file, out)
    finally:
        await file.close()
    size = dest.stat().st_size
    recorded_path = str(dest)
    peek: "dict | None" = None
    if safe_rel:  # a folder file: push to HPC3 too (preserving nesting); registered by register-folder
        if _uploads_on_hpc(conn):
            recorded_path = await asyncio.to_thread(_stage_upload_to_hpc, conn, dest, safe_rel)
    else:  # single-file upload: (optionally push to HPC3) log + record as a dataset
        # Skim the file CONTENT (deterministic, no GPU) while it is still local — before staging
        # to dfs3b deletes the local copy. The LLM description is deferred to /api/dataset/describe.
        peek = await asyncio.to_thread(_peek_local_upload, dest)
        where = "the server"
        if _uploads_on_hpc(conn):
            recorded_path = await asyncio.to_thread(_stage_upload_to_hpc, conn, dest, name)
            where = "your HPC3 storage"
        gist = (peek or {}).get("gist") or ""
        msg = f"Uploaded {name} ({size / 1e6:.1f} MB) to {where}."
        conn.emit("success", "upload", msg + (f" Skim: {gist}" if gist else ""))
        if _AUTH_ENABLED and conn.app_user_id:
            try:
                auth_routes.record_dataset(conn.app_user_id, name, recorded_path, size, _file_kind(name))
            except Exception as exc:  # noqa: BLE001 - history is best-effort, never blocks the upload
                print(f"[auth] record_dataset failed: {exc}")
    return JSONResponse({"status": "uploaded", "name": name, "path": recorded_path,
                         "rel_path": safe_rel, "size": size, "peek": peek})


@app.post("/api/upload/reserve-folder")
async def reserve_folder(payload: dict) -> JSONResponse:
    """Reserve a NON-COLLIDING top-level folder name before a folder upload starts, and
    create it as a placeholder. The browser then prefixes every file's relative path with
    the returned name, so uploading a second folder of the same name lands in
    ``data (1)/`` instead of merging into / overwriting the first ``data/``."""
    conn = CONNECTIONS.get(payload.get("connection_id"))
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    raw = safe_relpath(payload.get("name", "")).split("/")[0]
    if not raw:
        return JSONResponse({"error": "bad folder name"}, status_code=400)
    uploads = conn.workspace / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    folder = _unique_child(uploads, raw)
    (uploads / folder).mkdir(parents=True, exist_ok=True)   # placeholder reserves the name
    return JSONResponse({"status": "reserved", "folder": folder})


@app.post("/api/upload/register-folder")
async def register_folder(payload: dict) -> JSONResponse:
    """Record ONE dataset row for a just-uploaded folder (after its files landed via
    /api/upload with rel_path). Sums the tree's size + file count so the Datasets view
    shows the folder as a single entry (kind=folder); the lab reads the folder root."""
    conn = CONNECTIONS.get(payload.get("connection_id"))
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    folder = safe_relpath(payload.get("folder", "")).split("/")[0]
    if not folder:
        return JSONResponse({"error": "bad folder name"}, status_code=400)
    root = (conn.workspace / "uploads" / folder).resolve()
    uploads_root = (conn.workspace / "uploads").resolve()
    if not str(root).startswith(str(uploads_root)) or not root.is_dir():
        return JSONResponse({"error": "folder not found"}, status_code=404)
    if _uploads_on_hpc(conn):
        # Files were streamed to dfs3b (local copies removed); size/count come from the remote tree,
        # and the folder root recorded is the dfs3b path. Drop the now-empty local placeholder.
        remote_root = f"{_hpc_uploads_dir(conn)}/{folder}"
        count = int((conn.executor.exec(f"find {shlex.quote(remote_root)} -type f | wc -l").out or "0").strip() or 0)
        size = int((conn.executor.exec(f"du -sb {shlex.quote(remote_root)} 2>/dev/null | cut -f1").out or "0").strip() or 0)
        recorded_path, where = remote_root, "your HPC3 storage"
        shutil.rmtree(root, ignore_errors=True)
    else:
        files = [p for p in root.rglob("*") if p.is_file()]
        count, size, recorded_path, where = len(files), sum(p.stat().st_size for p in files), str(root), "the server"
    conn.emit("success", "upload",
              f"Uploaded folder {folder}/ ({count} files, {size / 1e6:.1f} MB) to {where}.")
    if _AUTH_ENABLED and conn.app_user_id:
        try:
            auth_routes.record_dataset(conn.app_user_id, folder, recorded_path, size, "folder")
        except Exception as exc:  # noqa: BLE001 - history is best-effort
            print(f"[auth] record_dataset (folder) failed: {exc}")
    return JSONResponse({"status": "registered", "name": folder, "path": recorded_path,
                         "size": size, "count": count, "kind": "folder"})


# --- resumable (chunked) upload for large datasets --------------------------
# Big matrices over a single POST are fragile (no resume; a dropped connection
# restarts from 0). These endpoints append the file chunk-by-chunk to a .part file
# and expose how many bytes are already on disk, so the browser resumes from the
# server's offset instead of re-sending. Raw data still stays on the server; only
# derived metrics ever reach the LLM (same boundary as /api/upload).


def _safe_token(value: str, fallback: str = "upload") -> str:
    """Keep alnum/-/_ from a client-supplied id so a .part path can't escape its dir."""
    cleaned = "".join(c for c in (value or "") if c.isalnum() or c in "-_")
    return cleaned[:64] or fallback


def _upload_part_path(conn: Connection, upload_id: str) -> Path:
    parts = conn.workspace / "uploads" / ".parts"
    parts.mkdir(parents=True, exist_ok=True)
    return parts / f"{_safe_token(upload_id)}.part"


@app.get("/api/upload/status")
async def upload_status(connection_id: str, upload_id: str) -> JSONResponse:
    """How many bytes of a chunked upload are already on the server, so the browser
    can resume from there after an interruption."""
    conn = CONNECTIONS.get(connection_id)
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    part = _upload_part_path(conn, upload_id)
    received = part.stat().st_size if part.exists() else 0
    return JSONResponse({"upload_id": _safe_token(upload_id), "received": received})


@app.post("/api/upload/chunk")
async def upload_chunk(
    connection_id: str = Form(...),
    upload_id: str = Form(...),
    name: str = Form(...),
    offset: int = Form(...),
    done: bool = Form(False),
    chunk: UploadFile = File(...),
) -> JSONResponse:
    """Append one chunk of a resumable dataset upload to a .part file. ``offset`` is
    where this chunk belongs; if it doesn't match what's on disk (a duplicate/old
    chunk) we return 409 + the true ``received`` so the client re-syncs and resumes.
    On ``done`` the .part file is finalized into the per-user uploads dir (same
    destination + dataset-history recording as /api/upload)."""
    conn = CONNECTIONS.get(connection_id)
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    part = _upload_part_path(conn, upload_id)
    current = part.stat().st_size if part.exists() else 0
    if offset != current:
        # out-of-order / duplicate chunk — tell the client where we actually are
        return JSONResponse({"error": "offset_mismatch", "received": current}, status_code=409)
    data = await chunk.read()
    await chunk.close()

    def _append(p: Path, b: bytes) -> None:
        with open(p, "ab") as out:
            out.write(b)

    await asyncio.to_thread(_append, part, data)
    received = current + len(data)
    if not done:
        return JSONResponse({"status": "partial", "received": received})
    # finalize: atomic rename of the .part into the uploads dir
    safe = safe_filename(name or "dataset")
    dest = conn.workspace / "uploads" / safe
    part.replace(dest)
    size = dest.stat().st_size
    recorded_path = str(dest)
    peek = await asyncio.to_thread(_peek_local_upload, dest)   # skim locally before any staging
    where = "the server (resumable)"
    if _uploads_on_hpc(conn):
        recorded_path = await asyncio.to_thread(_stage_upload_to_hpc, conn, dest, safe)
        where = "your HPC3 storage (resumable)"
    gist = (peek or {}).get("gist") or ""
    conn.emit("success", "upload",
              f"Uploaded {safe} ({size / 1e6:.1f} MB) to {where}." + (f" Skim: {gist}" if gist else ""))
    if _AUTH_ENABLED and conn.app_user_id:
        try:
            auth_routes.record_dataset(conn.app_user_id, safe, recorded_path, size, _file_kind(safe))
        except Exception as exc:  # noqa: BLE001 - history is best-effort, never blocks the upload
            print(f"[auth] record_dataset failed: {exc}")
    return JSONResponse({"status": "uploaded", "name": safe, "path": recorded_path, "size": size,
                         "peek": peek})


@app.post("/api/upload/discard")
async def upload_discard(payload: dict) -> JSONResponse:
    """Discard an in-progress chunked upload — delete its ``.part`` file so a **cancelled** upload
    leaves nothing half-written on the server. Idempotent (no error if there is nothing to remove)."""
    conn = CONNECTIONS.get(payload.get("connection_id"))
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    upload_id = str(payload.get("upload_id") or "")
    if not upload_id:
        return JSONResponse({"error": "upload_id required"}, status_code=400)
    part = _upload_part_path(conn, upload_id)
    existed = part.exists()
    part.unlink(missing_ok=True)
    return JSONResponse({"status": "discarded", "existed": existed})


@app.post("/api/dataset/describe")
async def describe_dataset_endpoint(payload: dict) -> JSONResponse:
    """On-demand content triage of an uploaded dataset: deterministic peek + LLM description.

    This is the "skim any file and get the gist" surface for when a model IS available. The LLM step
    runs ONLY if this session already has a reachable served model; it never provisions a GPU (upload
    and triage must not trigger the ~10-min A100 spin-up). With no model up, the deterministic peek
    plus a note is returned so the caller still gets the gist. Reads only a bounded head — a remote
    dfs3b file is never pulled back in full."""
    conn = CONNECTIONS.get(payload.get("connection_id"))
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    path = str(payload.get("path") or "").strip()
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)
    name = str(payload.get("name") or path.rsplit("/", 1)[-1])

    try:
        peek = await asyncio.to_thread(_peek_dataset_any, conn, path, name)
    except Exception as exc:  # noqa: BLE001 - peek is meant to be total; be defensive at the boundary
        return JSONResponse({"error": f"peek failed: {type(exc).__name__}: {exc}"}, status_code=500)

    from ..tools.dataset_inspect import describe_dataset

    model_up = _vllm_reachable(conn)
    if model_up:
        model = conn.selected_model or conn.settings.serving_model()

        def _chat(messages: list[dict]) -> str:
            # think=False is load-bearing (a bounded call to the served reasoning model can spend its
            # whole budget on the thinking trace and return empty content).
            return vllm_client.complete(conn.tunnel_port, model, messages,
                                        max_tokens=1200, timeout=90.0, think=False)

        description = await asyncio.to_thread(describe_dataset, peek, chat_fn=_chat)
    else:
        description = describe_dataset(peek, chat_fn=None)
        description["note"] = ("No served model is up for this session — deterministic description "
                               "only. Start the GPU (or run this at run time) for an LLM description.")
    return JSONResponse({"status": "ok", "peek": peek, "description": description,
                         "model_available": model_up})


@app.post("/api/datasets/delete")
async def delete_dataset(payload: dict, request: Request) -> JSONResponse:
    """Delete one of the caller's uploaded datasets — both the history row AND the raw
    file on the gateway host (eyeserver), so used datasets stop piling up on /data.

    Ownership is enforced two ways: the DB helper only removes a row whose ``user_id``
    matches the caller, and the physical unlink is confined to the caller's OWN
    ``<results>/<owner>/uploads`` subtree (a tampered/legacy path elsewhere is left
    untouched, never deleted). Datasets only exist when accounts are enabled."""
    if not _AUTH_ENABLED:
        return JSONResponse({"error": "Dataset history requires accounts."}, status_code=400)
    user = _optional_user(request)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        dataset_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad dataset id"}, status_code=400)
    stored = auth_routes.delete_dataset_record(user.id, dataset_id)
    if stored is None:
        return JSONResponse({"error": "Dataset not found."}, status_code=404)
    conn = _active_conn_for_user(user)
    if conn is not None and _is_remote_dataset(conn, stored):
        # Dataset lives on HPC3 dfs3b — remove it over the user's SSH session, strictly scoped
        # to their own uploads dir (never rm outside it). Best-effort: if no live session, the
        # row is gone and the file can be cleaned from the HPC3 storage panel.
        if any(stored.startswith(base + "/") for base in _hpc_uploads_dirs(conn)):
            try:                                                   # -rf: also removes folder datasets
                await asyncio.to_thread(conn.executor.exec, f"rm -rf {shlex.quote(stored)}")
            except Exception as exc:  # noqa: BLE001 - history row already removed
                print(f"[datasets] could not rm remote {stored}: {exc}")
    else:
        uploads_root = (CONSOLE_RUNS_DIR / safe_name(user.username) / "uploads").resolve()
        try:
            target = Path(stored).resolve()
            if target.is_relative_to(uploads_root) and target.is_file():
                target.unlink()
        except OSError as exc:  # row is already gone; a stuck file is logged, not fatal
            print(f"[datasets] could not unlink {stored}: {exc}")
    return JSONResponse({"ok": True, "deleted": dataset_id})


@app.get("/api/presets")
async def list_research_presets() -> JSONResponse:
    """Pre-selected research paths (key + label + default editable prompt). Selecting one
    steers the PI's planning; the user can edit the prompt before running (like plan mode)."""
    from ..agents.presets import list_presets
    return JSONResponse({"presets": list_presets()})


@app.get("/api/skills")
async def list_atomic_skills() -> JSONResponse:
    """The atomic-skill library (name + one-line summary) for the console's skill multi-select.
    Checking skills marks them REQUIRED for the run — the plan must apply each."""
    from ..agents.skills import list_skills
    return JSONResponse({"skills": list_skills()})


# --- Follow-up intent router -------------------------------------------------
# The composer always POSTs /api/lab. But after a completed run, a typed follow-up is far
# more often "amend THIS report" than "start a whole new study" — yet the old path always
# minted a fresh run_id and re-planned from scratch, so the follow-up produced a SEPARATE
# figure-less bundle instead of editing the report in place. Rather than hard-code keyword
# rules in the frontend (too brittle), the backend asks the session's own LLM to classify
# the follow-up and forwards it to the right existing path: edit the report (A1), re-run one
# step (A2), or a genuinely new study. On low confidence it ASKS (clarify card) instead of
# guessing; a different dataset / preset / plan-mode short-circuits straight to a new study.

FOLLOWUP_CONFIDENCE = 0.6   # below this the router asks instead of auto-routing

_FOLLOWUP_ROUTER_SYS = (
    "You route a follow-up message in a single-cell bioinformatics console. The user ALREADY "
    "completed a research run (its report + figures are saved) and just sent another message. "
    "Classify what they want, choosing exactly one intent:\n"
    "- \"edit_report\": change the EXISTING report's wording, structure, or references WITHOUT "
    "re-running any analysis (e.g. 'make the discussion shorter', 'fix the title', 're-render the "
    "PDF', 'tone down the claims').\n"
    "- \"rerun_step\": re-run ONE analysis step of the SAME study and let the report follow, reusing "
    "the other steps and their figures (e.g. 're-cluster at resolution 1.0', 'search the literature "
    "again', 'redo the differential expression', 'add pathway enrichment'). Pick the single most "
    "relevant step, copied VERBATIM from the prior agenda.\n"
    "- \"new_study\": a genuinely different question, dataset, or analysis that should start fresh.\n"
    "Return ONLY a JSON object: {\"intent\": \"edit_report|rerun_step|new_study\", "
    "\"step\": \"<verbatim agenda step when intent=rerun_step, else null>\", "
    "\"confidence\": <0.0-1.0>, \"reason\": \"<one short clause>\"}."
)


def _extract_json_object(text: str) -> dict | None:
    """Best-effort parse of the first top-level JSON object in an LLM reply (fence-tolerant)."""
    s = str(text or "").strip()
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _match_agenda_step(step_text: str, agenda: list[str]) -> int | None:
    """Resolve a model-named step string to its 0-based agenda index (exact → substring →
    best token overlap). None when nothing plausibly matches."""
    s = (step_text or "").strip().lower()
    if not s or not agenda:
        return None
    for i, a in enumerate(agenda):
        if a.strip().lower() == s:
            return i
    for i, a in enumerate(agenda):
        al = a.strip().lower()
        if s in al or al in s:
            return i
    sw = set(s.split())
    best_ov, best_i = 0, None
    for i, a in enumerate(agenda):
        ov = len(sw & set(a.lower().split()))
        if ov > best_ov:
            best_ov, best_i = ov, i
    return best_i


def _parse_followup_intent(raw: Any, agenda: list[str]) -> dict | None:
    """Validate the router LLM's JSON into {intent, step_index, confidence, reason}, or None
    if it's unusable (bad intent, or rerun_step with no identifiable agenda step)."""
    obj = _extract_json_object(str(raw))
    if obj is None:
        return None
    intent = str(obj.get("intent") or "").strip().lower()
    if intent not in ("edit_report", "rerun_step", "new_study"):
        return None
    try:
        conf = max(0.0, min(1.0, float(obj.get("confidence"))))
    except (TypeError, ValueError):
        conf = 0.0
    step_index = None
    if intent == "rerun_step":
        step_index = _match_agenda_step(str(obj.get("step") or ""), agenda)
        if step_index is None:
            return None    # can't tell which step → treat as ambiguous, let the caller ask
    return {"intent": intent, "step_index": step_index, "confidence": conf,
            "reason": str(obj.get("reason") or "")[:200]}


def _classify_followup(complete_fn, state: dict, question: str) -> dict | None:
    """Ask the session LLM to route a follow-up against the prior run's agenda. Never raises."""
    agenda = list(state.get("agenda") or [])
    prior_q = str(state.get("question") or "")
    prompt = (
        f"Prior study question: {prior_q}\n"
        "Prior agenda (the steps that produced the current report + figures):\n"
        + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(agenda))
        + f"\n\nThe user's new message:\n{question}\n\nReturn the routing JSON."
    )
    try:
        raw = complete_fn([{"role": "system", "content": _FOLLOWUP_ROUTER_SYS},
                           {"role": "user", "content": prompt}])
    except Exception as exc:  # noqa: BLE001 - a router failure must never break the run; caller asks
        print(f"[followup] classify LLM call failed: {exc}")
        return None
    return _parse_followup_intent(raw, agenda)


def _conversation_last_run(conn: Connection, conversation_id: str) -> str:
    """The last run a conversation produced: the in-memory ``last_run_by_conversation`` map first
    (fast; current session), then the DB (``Run.conversation_id``) so a follow-up still recognises its
    prior run AFTER a gateway restart — the in-memory map is empty on a fresh process, which otherwise
    made every typed follow-up on reconnect a fresh study. A DB hit warms the cache. "" when none.

    Only the disk bundle (checked by the caller) makes a run an actual follow-up TARGET; this just
    resolves WHICH run_id to check. Cancelled/errored runs are excluded (they never reach ``done``/
    ``incomplete``), so the fresh-vs-replan contract is preserved across a restart too."""
    rid = (conn.last_run_by_conversation.get(conversation_id) or "").strip()
    if rid:
        return rid
    if _AUTH_ENABLED and conn.app_user_id:
        try:
            rid = (auth_routes.latest_run_id_for_conversation(conn.app_user_id, conversation_id) or "").strip()
        except Exception as exc:  # noqa: BLE001 - a lookup failure just means "no prior run known"
            print(f"[followup] DB last-run lookup failed: {exc}")
            rid = ""
        if rid:
            conn.last_run_by_conversation[conversation_id] = rid   # warm the cache for this session
    return rid


def _followup_target(conn: Connection, req: LabRequest) -> tuple[str, Path, dict] | None:
    """Follow-up routing is eligible when there's a prior completed run with a bundle on disk, the
    user did not FORCE a new study (a picked skill via preset/presets, or a DIFFERENT dataset), and
    its run_state.json is loadable. Returns (run_id, art, state) or None.

    NB: plan-mode does NOT disqualify. "Plan first" is checked by DEFAULT, so treating it as a
    new-study signal made EVERY follow-up (e.g. "continue and generate the report") fall through to
    a fresh full re-run. The classifier decides the intent instead; a new_study result still runs
    fresh and honors req.plan_mode, while edit_report / rerun_step don't re-plan at all.

    The "prior run" is scoped to THIS conversation: a follow-up amends the run its OWN chat produced,
    and a fresh window/thread (or a message in a conversation that has never run) is a NEW study —
    never a stale replan of whatever the shared SSH session last did. Older clients that don't send a
    conversation_id fall back to the connection-wide last run (unchanged behaviour)."""
    if req.conversation_id:
        run_id = _conversation_last_run(conn, req.conversation_id)
    else:
        run_id = (conn.last_run_id or "").strip()
    if not run_id or req.preset or req.presets or req.skills:
        return None
    art = conn.workspace / safe_name(run_id) / "artifacts"
    state_path = art / "process" / "run_state.json"
    if not (art / "report" / "report.md").exists() or not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not state.get("agenda"):
        return None
    prior_ds = str(state.get("dataset_path") or "")
    # Compare the PRIMARY of the bound set (feature ②) so a multi-file attach is handled too; legacy
    # single-file requests resolve to exactly ``dataset_path``.
    _bound = _select_bound_datasets(req)
    new_primary = _bound[0]["path"] if _bound else ""
    if new_primary and prior_ds and Path(new_primary).name != Path(prior_ds).name:
        return None    # a DIFFERENT dataset was explicitly chosen → this is a new study
    return run_id, art, state


async def _ask_followup_clarify(conn: Connection) -> str | None:
    """Show the amend-vs-new clarify card and block (in a thread) on the shared plan_event —
    reusing the exact PI-clarify round-trip (answered via /api/lab/plan). Returns the chosen
    intent string, or None on timeout/cancel."""
    q = {"question": "What would you like me to do with this message?",
         "options": ["Revise the last report (no re-run)", "Re-run a step and update the report",
                     "Start a brand-new analysis"]}
    conn.plan_value = None
    conn.plan_event.clear()
    conn.pending_plan = {"kind": "clarify", "payload": [q]}
    conn.push({"type": "plan_clarify", "questions": [q]})
    got = await asyncio.to_thread(conn.plan_event.wait, 600)
    conn.pending_plan = None
    conn.push({"type": "plan_done"})
    if not got:
        return None
    fb = str((conn.plan_value or {}).get("feedback", "")).lower()
    conn.plan_value = None
    # Order matters: match the report-edit intent first so "no re-run" isn't read as a re-run.
    if "revise" in fb or "edit" in fb or "no re-run" in fb or "as-is" in fb:
        return "edit_report"
    if "re-run" in fb or "rerun" in fb or "redo" in fb or "step" in fb:
        return "rerun_step"
    if "new" in fb or "fresh" in fb or "brand" in fb:
        return "new_study"
    return None


async def _dispatch_lab(conn: Connection, req: LabRequest) -> None:
    """Entry for a composer message: route a follow-up to edit-report / re-run-step / new-study,
    or fall through to a fresh run. Only engages when a prior bundle exists; classifies with the
    warm session LLM (a cold session asks instead of paying a GPU cold-start to judge a sentence),
    and asks on low confidence rather than guessing."""
    # Open this turn's run scope up front so its cancel/plan events + WS stream are isolated from any
    # OTHER window's run on the shared session, and the pre-run follow-up clarify uses this run's
    # plan_event (not a connection-wide one). The run_id is bound later (a fresh study mints it; a
    # resume/edit reuses the prior one). Downstream _run_lab / _regenerate_report read conn.active_run.
    conn.begin_run(req.conversation_id)
    # Axis B, checked FIRST: the fast path is a different engine, not a lab variant. In particular it
    # must skip the follow-up router below — that router only knows three intents, all of which
    # ("edit the report", "re-run a step", "new study") are research-path outcomes, so letting a chat
    # message reach it would route a question into a report edit.
    if _fast_path(req):
        await _run_quick_chat(conn, req)
        return
    target = _followup_target(conn, req)
    if target is None:
        await _run_lab(conn, req)
        return
    run_id, art, state = target

    intent = None
    if conn.alloc is not None:          # model already warm — cheap to classify
        try:
            complete_fn, *_ = _lab_llm(conn)
            intent = await asyncio.to_thread(_classify_followup, complete_fn, state, req.question)
        except Exception as exc:  # noqa: BLE001
            print(f"[followup] classify failed: {exc}")

    if intent is None or intent["confidence"] < FOLLOWUP_CONFIDENCE:
        choice = await _ask_followup_clarify(conn)    # ambiguous / cold / failed → ask
        if choice in (None, "new_study"):
            await _run_lab(conn, req)
            return
        intent = {"intent": choice, "confidence": 1.0, "reason": "user choice",
                  "step_index": None if choice == "edit_report" else _default_rerun_index(state)}

    kind = intent["intent"]
    if kind == "new_study":
        await _run_lab(conn, req)
        return
    if kind == "edit_report":
        conn.push({"type": "lab_progress",
                   "text": "🧭 Read as “revise the last report” — no re-run; the existing figures are kept.",
                   "level": "info"})
        await _regenerate_report(conn, run_id, art, "report", req.question)
        return
    # rerun_step: re-run the chosen step in place; the checkpoint guard degrades to an edit
    # (A1) when the upstream analysis checkpoints have expired, so figures are never lost.
    step_index = intent.get("step_index")
    if step_index is None:
        step_index = _default_rerun_index(state)
    try:
        resume, resume_decisions, cont_req, idx = _prepare_continue(
            req.connection_id, conn, run_id, art, state, step_index, modify_note=req.question)
    except ValueError:
        conn.push({"type": "lab_progress",
                   "text": "🧭 Analysis checkpoints have expired — updating the existing report instead (figures kept).",
                   "level": "info"})
        await _regenerate_report(conn, run_id, art, "report", req.question)
        return
    conn.push({"type": "lab_progress",
               "text": f"🧭 Read as “re-run step {idx + 1} and update the report” — the other results and figures are kept.",
               "level": "info"})
    await _run_lab(conn, cont_req, resume=resume, resume_run_id=run_id, resume_decisions=resume_decisions)


def _fast_path(req: LabRequest) -> bool:
    """True when this message should take the answer-first chat loop instead of the lab.

    ONE rule: the user picked it (``route == "chat"``). No keyword heuristics, no LLM classifier
    — see ``LabRequest.route``. Anything unrecognised means research, so an older client that
    doesn't send the field, or a typo'd value, keeps today's behaviour exactly."""
    return str(getattr(req, "route", "") or "").strip().lower() == "chat"


def _quickchat_stream_fn(conn: Connection, model: str, base_url: str | None, api_key: str | None):
    """Bind ``vllm_client.chat_tools_stream`` to this session's tunnel.

    Deliberately NOT routed through ``_lab_llm``'s ``_with_recovery``: that helper retries a whole
    call after healing a dropped tunnel, which is right for an atomic completion but wrong for a
    stream whose first half has ALREADY been pushed to the browser — the retry would replay those
    tokens and the user would read the answer's opening twice. A dropped fast-path stream surfaces
    as a chat error instead; the user retypes, which costs seconds here (the whole premise of this
    path) rather than the minutes a lab re-run would."""
    def _stream(messages: list[dict], schemas: list[dict]):
        port = 0 if base_url else (conn.tunnel_port or 0)
        # Bump the output cap from the 2048 default: a long enumeration answer plus its
        # reference list was truncating mid-references (and then the citation-check footer
        # never got appended). 4096 fits these answers with headroom.
        return vllm_client.chat_tools_stream(port, model, messages, schemas,
                                             base_url=base_url, api_key=api_key, think=False,
                                             max_tokens=4096)
    return _stream


def _quickchat_context_fns(conn: Connection, model: str, base_url: str | None, api_key: str | None):
    """The two functions ``run_quick_chat`` needs for context management, bound to this
    session's tunnel — ``(count_tokens_fn, summarize_fn)``.

    Injected rather than imported by ``agents/quick_chat.py`` for the same reason ``stream_fn``
    is: that module is unit-tested on a bare checkout and must not reach the gateway. Mirrors
    how ``_lab_llm`` hands ``count_tokens`` to the research harness.

    Both are BEST-EFFORT by contract and neither is routed through ``_with_recovery``: the
    compactor already degrades (char estimate / drop-oldest) when they can't answer, and a
    tunnel heal + retry here would spend the very seconds this path exists to save."""

    def count_tokens(messages: list[dict], tools: list[dict]) -> int | None:
        # EXACT prompt tokens from vLLM /tokenize — the SAME tokenizer + chat template the
        # server enforces --max-model-len with. Returns None for a remote base_url or any
        # transport error, and never raises (see vllm_client.count_tokens).
        port = 0 if base_url else (conn.tunnel_port or 0)
        return vllm_client.count_tokens(port, model, messages, tools,
                                        base_url=base_url, api_key=api_key)

    def summarize(messages: list[dict], max_tokens: int) -> str:
        # The rolling conversation memory. think=False and a small max_tokens keep it cheap
        # AND correct: with thinking ON, a Qwen3 reasoning trace can eat the whole budget and
        # return EMPTY content (the map_phenotype_to_hpo failure mode). The timeout is short —
        # this runs before the user's first token, so a hung summarizer must fail fast and let
        # the compactor degrade to dropping the oldest turns.
        port = 0 if base_url else (conn.tunnel_port or 0)
        return vllm_client.complete(port, model, messages, timeout=90.0, max_tokens=max_tokens,
                                    base_url=base_url, api_key=api_key, think=False)

    return count_tokens, summarize


async def _run_quick_chat(conn: Connection, req: LabRequest) -> None:
    """The fast path: stream an answer-first ReAct turn straight into the chat bubble.

    Shares the run lifecycle with ``_run_lab`` (``chat_running`` serialization, per-run cancel via
    ``chat_stop``, the ``chat_start`` → ``chat_token`` → ``chat_done`` WS protocol) so the client
    needs no new event types and Stop/reconnect/replay keep working unchanged. What it does NOT
    share: no run_id bundle, no artifacts, no ``run_complete``, no ``_remember_run`` — a chat turn
    is not a run, and registering one as this conversation's "last run" would make the NEXT
    research message try to amend a report that was never written."""
    run = conn.active_run or conn.begin_run(req.conversation_id)
    if req.conversation_id and not run.conversation_id:
        run.conversation_id = req.conversation_id
    run.kind = "chat"               # /api/chat/inject refuses to queue a note against this run
    run.chat_stop.clear()
    conn.pull_injections()          # drop any note left queued by a prior run — see RunState.kind
    conn.chat_running = True
    conn.push({"type": "chat_start"})
    emit = conn.emit_fn()
    # NB run_quick_chat runs in a worker thread (urllib is blocking), and every push below happens
    # from that thread. That is safe: Connection._publish already hops to the event loop via
    # loop.call_soon_threadsafe, which is the same contract _run_lab's callbacks rely on.

    try:
        from ..agents.quick_chat import QuickChatConfig, run_quick_chat
        from ..agents.registry import build_quickchat_catalog
        from ..agents.research_harness import HarnessContext

        base_url = os.environ.get("BIOAGENT_LLM_BASE_URL") or None
        api_key = os.environ.get("BIOAGENT_LLM_API_KEY") or None
        model = (os.environ.get("BIOAGENT_LLM_MODEL") or "qwen/qwen3.6-35b-a3b") if base_url \
            else conn.selected_model
        emit("step", "chat", f"Fast chat turn (model: {model}).")

        tools = build_quickchat_catalog(literature_executor=_build_literature_executor(conn))
        # The case note reaches map_phenotype_to_hpo the same way it does on the research path
        # (via ctx.decisions), so attaching a note and asking a question in chat still works.
        decisions: dict[str, Any] = {}
        note = _clean_case_note(req.case_note)
        if note:
            decisions["case_note"] = note
        ctx = HarnessContext(decisions=decisions, workspace=conn.workspace,
                             model=model, tunnel_port=conn.tunnel_port,
                             llm_is_remote=_endpoint_is_off_host(base_url))

        # Tool chatter goes to the COLLAPSIBLE activity log (chat_thinking), never to the
        # always-visible feed — same split _lab_event_to_chat makes for the research path.
        def on_event(ev: dict[str, Any]) -> None:
            t = ev.get("type")
            if t == "content":
                conn.push({"type": "chat_token", "token": ev.get("token", "")})
            elif t == "thinking":
                conn.push({"type": "chat_thinking", "token": ev.get("token", "")})
            elif t == "tool_start":
                conn.push({"type": "run_status", "text": f"⚙️ {ev.get('tool')}…"})
                conn.push({"type": "chat_thinking",
                           "token": f"→ {ev.get('tool')}({json.dumps(ev.get('args', {}))[:80]})\n"})
            elif t == "tool_result":
                conn.push({"type": "chat_thinking",
                           "token": f"✓ {ev.get('tool')}: {ev.get('summary', '')}\n"})
            elif t == "tool_error":
                conn.push({"type": "chat_thinking",
                           "token": f"⚠ {ev.get('tool')}: {ev.get('error', '')}\n"})
            elif t in ("context_measured", "context_trimmed"):
                # Same event vocabulary the research path emits, so the EXISTING renderer
                # produces the activity lines — no second format to keep in sync.
                for payload in _lab_event_to_chat(ev):
                    conn.push(payload)
                print(f"[budget] chat {ev}")     # → journald, same as the research path
                if t == "context_measured":
                    # …plus the compact live occupancy indicator next to the composer.
                    conn.push({"type": "chat_context",
                               "used": ev.get("exact_tokens"), "allowed": ev.get("allowed"),
                               "exact": bool(ev.get("exact")),
                               "compacted": bool(ev.get("compacted"))})

        memory = conn.chat_memory.get(req.conversation_id or "", {})
        count_tokens_fn, summarize_fn = _quickchat_context_fns(conn, model, base_url, api_key)
        result = await asyncio.to_thread(
            run_quick_chat,
            stream_fn=_quickchat_stream_fn(conn, model, base_url, api_key),
            tools=tools, question=req.question, history=list(req.history or []), context=ctx,
            on_event=on_event, config=QuickChatConfig(verify_citations=True),
            should_cancel=run.chat_stop.is_set,
            count_tokens_fn=count_tokens_fn, summarize_fn=summarize_fn,
            prior_summary=str(memory.get("summary") or ""),
            prior_summary_through=int(memory.get("through") or 0),
        )
        # Carry the rolling memory forward. Stored even on a stopped/empty turn: the summary
        # describes the CONVERSATION, not this turn's answer, and throwing it away would make
        # the next turn re-summarize everything from scratch.
        conn.chat_memory[req.conversation_id or ""] = {
            "summary": result.summary, "through": result.summary_through}
        if result.stopped:
            conn.push({"type": "chat_stopped"})
            return
        if not result.text.strip():
            # An empty turn is a real, previously-observed vLLM failure mode (the reasoning trace
            # eats max_tokens and content comes back ""), and silently finishing on it would show
            # the user a blank bubble with no explanation. Name it instead.
            conn.push({"type": "chat_token",
                       "token": "_The model returned an empty reply. Try rephrasing, or switch to "
                                "Research mode if this needs an analysis._"})
        elif result.hit_turn_limit:
            conn.push({"type": "lab_progress",
                       "text": "⚠ Reached the chat turn limit — the answer above may be incomplete. "
                               "Ask a follow-up, or switch to Research mode for a full study.",
                       "level": "warning"})
        emit("success", "chat",
             f"Fast chat turn done ({result.turns} turn(s), {len(result.tool_calls)} tool call(s)).")
        conn.push({"type": "chat_done"})
    except GatewayError as exc:
        print(f"[chat] fast path failed ({exc.stage or 'chat'}): {exc.message}")
        traceback.print_exc()
        conn.emit("error", exc.stage or "chat", exc.message, detail=error_detail(exc))
        conn.push({"type": "chat_error", "message": exc.message})
    except Exception as exc:  # noqa: BLE001 - a chat failure is reported, never fatal
        print(f"[chat] fast path failed (unhandled): {type(exc).__name__}: {exc}")
        traceback.print_exc()
        conn.emit("error", "chat", f"Chat failed: {exc}", detail=error_detail(exc))
        conn.push({"type": "chat_error", "message": str(exc)})
    finally:
        conn.chat_running = False
        conn.end_run(run)


def _default_rerun_index(state: dict) -> int:
    """A sensible step to re-run when the user picked 'rerun' but named no step: prefer a
    literature step (cheap, no checkpoint), else the last step."""
    from ..agents.research_lab import _is_literature_step
    agenda = list(state.get("agenda") or [])
    for i, s in enumerate(agenda):
        if _is_literature_step(s):
            return i
    return max(0, len(agenda) - 1)


@app.post("/api/lab")
async def lab(req: LabRequest) -> JSONResponse:
    """Run the role-based research lab (PI -> Scientist -> Critic -> converge) over
    this session's vLLM, streaming each role's progress to the log + the final report
    to the chat. A typed follow-up after a completed run is routed by _dispatch_lab (edit /
    re-run-step / new study) instead of always minting a fresh study."""
    conn = CONNECTIONS.get(req.connection_id)
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    # A run needs the whole stack (SSH + GPU + a served model), and /api/connect brings those up
    # together — so "ready" is the only state a run may start from. Anything else (still
    # connecting/provisioning, errored, disconnected) is rejected rather than silently waiting.
    if conn.status != "ready" or conn.executor is None:
        return JSONResponse({"error": f"Connection is not ready (status={conn.status})."}, status_code=409)
    # A run's state (chat_stop / plan_event / last_run_id / the event stream) is single-valued per
    # Connection, so a SECOND run started while one is in-flight (e.g. from another browser tab/window
    # that bypasses the client-side guard) collides with the first: two plans in one window, and a
    # cancel/approve in one hits the other. Reject it, exactly like /api/report/regenerate and
    # /api/lab/continue already do. (A typed follow-up AFTER a run completes has chat_running=False and
    # is allowed; steering an executing run goes through a different endpoint.)
    if conn.chat_running:
        return JSONResponse({"error": "A run is already in progress on this session."}, status_code=409)
    asyncio.create_task(_dispatch_lab(conn, req))
    return JSONResponse({"status": "running"})


def _default_run_id(conn: Connection, explicit: str | None, conversation_id: str | None) -> str:
    """Resolve which run a regenerate / continue targets: the explicit run_id the client sent, else
    the last run THIS conversation produced, else the connection-wide last run (older clients). Scoping
    the default to the conversation stops a follow-up from silently editing another window's run."""
    if explicit and explicit.strip():
        return explicit.strip()
    if conversation_id:
        by_conv = _conversation_last_run(conn, conversation_id)   # in-memory, then DB (survives restart)
        if by_conv:
            return by_conv
    return (conn.last_run_id or "").strip()


@app.post("/api/report/regenerate")
async def report_regenerate(req: RegenerateReportRequest) -> JSONResponse:
    """Rebuild a PRIOR run's report from its persisted bundle — WITHOUT re-running the PI or the
    analysis. Re-renders the existing ``report.md`` (optionally after one LLM edit pass driven by
    ``instruction``) to PDF/DOCX, in place. This is the "重新生成 PDF / 接着上次结果改报告" path: a
    follow-up no longer triggers a full fresh pipeline. Loads the bundle by (owner, run_id) off
    disk, so it works after a page refresh (the client remembers run_id) and even after a reconnect."""
    conn = CONNECTIONS.get(req.connection_id)
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    if conn.chat_running:
        return JSONResponse({"error": "A run is already in progress."}, status_code=409)
    run_id = _default_run_id(conn, req.run_id, req.conversation_id)
    if not run_id:
        return JSONResponse({"error": "No previous report to regenerate."}, status_code=409)
    basename = req.basename if req.basename in ("report", "technical_report") else "report"
    art = conn.workspace / safe_name(run_id) / "artifacts"
    if not (art / "report" / f"{basename}.md").exists():
        return JSONResponse(
            {"error": f"No {basename}.md found for run {run_id} — nothing to regenerate."},
            status_code=404)
    conn.begin_run(req.conversation_id, run_id=run_id)
    asyncio.create_task(_regenerate_report(conn, run_id, art, basename, req.instruction))
    return JSONResponse({"status": "running", "run_id": run_id})


@app.post("/api/lab/continue")
async def lab_continue(req: ContinueRunRequest) -> JSONResponse:
    """A2 continuation: re-run ONE changed analysis step (and everything downstream of it) from a
    prior run, reusing the earlier steps' checkpoints — instead of re-planning + re-running the whole
    pipeline. Loads the run's persisted run_state.json, builds a ResumeState, and re-enters _run_lab
    with the SAME run_id so the report is rebuilt in place. Needs a live session (the redone step +
    synthesis call the model)."""
    conn = CONNECTIONS.get(req.connection_id)
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    if conn.status != "ready" or conn.executor is None:
        return JSONResponse({"error": f"Connection is not ready (status={conn.status})."}, status_code=409)
    if conn.chat_running:
        return JSONResponse({"error": "A run is already in progress."}, status_code=409)
    run_id = _default_run_id(conn, req.run_id, req.conversation_id)
    if not run_id:
        return JSONResponse({"error": "No previous run to continue."}, status_code=409)
    art = conn.workspace / safe_name(run_id) / "artifacts"
    state_path = art / "process" / "run_state.json"
    if not state_path.exists():
        return JSONResponse(
            {"error": f"Run {run_id} has no run_state.json — it predates resumable runs; re-run it once."},
            status_code=404)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": f"run_state.json is unreadable: {exc}"}, status_code=422)
    try:
        resume, resume_decisions, cont_req, idx = _prepare_continue(
            req.connection_id, conn, run_id, art, state, req.from_step_index,
            modify_note=req.modify_note, edited_step=req.edited_step)
    except ValueError as exc:
        # No agenda (422) vs expired checkpoints (409) — distinguish for the caller.
        code = 422 if "no agenda" in str(exc).lower() else 409
        return JSONResponse({"error": str(exc)}, status_code=code)
    conn.begin_run(req.conversation_id, run_id=run_id)
    asyncio.create_task(_run_lab(conn, cont_req, resume=resume, resume_run_id=run_id,
                                 resume_decisions=resume_decisions))
    return JSONResponse({"status": "running", "run_id": run_id, "from_step": idx + 1})


def _prepare_continue(connection_id: str, conn: Connection, run_id: str, art: Path, state: dict,
                      from_step_index: int, *, modify_note: str | None = "",
                      edited_step: str | None = None):
    """Build the (ResumeState, resume_decisions, LabRequest, idx) to re-run ONE step of a prior
    run in place, reusing earlier steps' checkpoints. Shared by /api/lab/continue and the
    follow-up router. Raises ValueError when the run can't be continued (no agenda, or the
    upstream analysis checkpoints for a mid-pipeline step have expired)."""
    agenda = list(state.get("agenda", []))
    if not agenda:
        raise ValueError("Prior run has no agenda to continue.")
    idx = max(0, min(int(from_step_index), len(agenda) - 1))
    # Resuming PAST step 0 reads the prior step's analysis checkpoint. Those expire
    # (checkpoint_ttl_days), so if they're gone the resumed step has no input — raise instead
    # of dispatching a run that would silently produce nothing. (Step 0 reads the raw dataset,
    # not a checkpoint, so it's always resumable.)
    work = conn.workspace / safe_name(run_id) / "work"
    if idx > 0 and not (work.exists() and any(work.glob("adata_*.h5ad"))):
        raise ValueError(
            f"Run {run_id}'s analysis checkpoints have expired — re-run the study once to "
            "continue from a middle step (the report is still available to regenerate).")
    # Optional: replace the redone step's text outright (e.g. "Cluster the cells at resolution 1.0").
    if edited_step and edited_step.strip():
        agenda = [*agenda[:idx], edited_step.strip(), *agenda[idx + 1:]]
        state = {**state, "agenda": agenda}
    from ..agents.research_lab import ResumeState
    resume = ResumeState.from_run_state(state, idx, modify_note=(modify_note or ""),
                                        guidance=state.get("guidance"))
    resume_decisions: dict[str, Any] = {}
    if state.get("dataset_path"):
        resume_decisions["dataset_path"] = state["dataset_path"]
    if state.get("hpc_primary"):
        resume_decisions["hpc_primary"] = state["hpc_primary"]
    if state.get("datasets"):
        # Restore the multi-file BIND-SET so a resumed step sees every bound file, not just the primary.
        resume_decisions["datasets"] = state["datasets"]
    if state.get("content_modality"):
        resume_decisions["content_modality"] = state["content_modality"]
        resume_decisions["content_confidence"] = state.get("content_confidence")
    cont_req = LabRequest(connection_id=connection_id, question=str(state.get("question", "")),
                          dataset_path=state.get("dataset_path"),
                          datasets=state.get("datasets"),
                          case_note=state.get("case_note"))
    return resume, resume_decisions, cont_req, idx


@app.post("/api/lab/plan")
async def lab_plan_review(req: PlanReviewRequest) -> JSONResponse:
    """Receive the user's decision on the PI's proposed plan and unblock the lab worker
    waiting on ``conn.plan_event``. The user no longer text-edits the agenda; they
    APPROVE it, CANCEL, or REVISE it with natural-language ``feedback`` that goes back to
    the PI for a re-draft (also how a clarify question is answered)."""
    conn = CONNECTIONS.get(req.connection_id)
    if not conn:
        return JSONResponse({"error": "Unknown connection id"}, status_code=404)
    action = (req.action or "approve").strip().lower()
    if req.approved is False:                       # back-compat: {approved:false} == cancel
        action = "cancel"
    if action not in ("approve", "revise", "cancel"):
        action = "cancel"
    # Unblock only the run this decision is FOR (its own chat), not whatever run is live on the shared
    # session. A decision that names a run that isn't active (a stale plan card from a finished run) is
    # a no-op. No identifiers (older client) → the active run.
    target = conn.resolve_run(req.run_id, req.conversation_id)
    if target is None:
        return JSONResponse({"status": "stale", "action": "ignored"})
    target.plan_value = {"action": action, "feedback": (req.feedback or "").strip()}
    target.pending_plan = None
    target.plan_event.set()
    conn.push({"type": "plan_done"})
    return JSONResponse({"status": "ok", "action": action})


class LabLLM(NamedTuple):
    """What :func:`_lab_llm` binds for one session.

    ``scientist_remote`` is what the data-boundary guard keys on — it governs the Scientist brief,
    which is where raw tabular data can appear. ``lab_role_remote`` is a SEPARATE exposure: the
    PI/Critic payload (dataset profile, accepted findings, artifact digests) is NOT passed through
    the guard today, so a remote lab endpoint sends that content off-site unguarded. It is surfaced
    so the run can say so out loud rather than leaving it implicit."""

    complete_fn: Any
    scientist_chat: Any
    model: str
    label: str
    count_tokens: Any
    scientist_remote: bool
    lab_role_remote: bool
    lab_label: str


def _lab_llm(conn: Connection) -> "LabLLM":
    """Bind the lab's PI/Critic completion + Scientist tool-chat to this session's
    vLLM tunnel — or, when BIOAGENT_LLM_BASE_URL is set, to any OpenAI-compatible
    endpoint (e.g. OpenRouter) for off-cluster testing without HPC3.

    Also reports whether that endpoint is OFF-HOST, so the caller can stamp
    ``HarnessContext.llm_is_remote``. Only this function knows where the calls actually go: a
    session can hold an open vLLM tunnel while completions are routed to a remote API, so the
    tunnel's existence is not evidence the prompt stays local."""
    base_url = os.environ.get("BIOAGENT_LLM_BASE_URL") or None
    api_key = os.environ.get("BIOAGENT_LLM_API_KEY") or None
    if base_url:
        model = os.environ.get("BIOAGENT_LLM_MODEL") or "qwen/qwen3.6-35b-a3b"
        label = "OpenRouter"
    else:
        model = conn.selected_model
        label = "vLLM"

    def _port() -> int:
        # Read the LIVE tunnel port on every call: a mid-run recovery (_heal_vllm_session)
        # rebinds conn.tunnel_port to a fresh local port, and the retry must use the new one.
        return 0 if base_url else (conn.tunnel_port or 0)

    def _with_recovery(call):
        """Run one vLLM call; if the tunnel/serve DROPPED (VLLMNetworkError, not a model
        error like context-overflow), heal the session once and retry. The OpenRouter test
        path (base_url) and mock sessions have nothing to heal, so they re-raise as before."""
        try:
            return call(_port())
        except VLLMNetworkError:
            if base_url or conn.mock:
                raise
            _heal_vllm_session(conn)   # reattach-or-resubmit + reopen tunnel; raises if it can't
            return call(_port())       # retry ONCE on the recovered tunnel

    # ROLE SPLIT: the reasoning roles (PI / Critic / plan review / exploration / cycle re-planning /
    # synthesis) may run on a DIFFERENT, stronger endpoint than the Scientist's tool-calling. They
    # are the low-volume, long-horizon half, and the half a small model is weakest at; the Scientist
    # is high-volume, fenced by deterministic guards, bound to vLLM's tool-parser contract, and sits
    # next to the data. Unset BIOAGENT_LAB_LLM_BASE_URL => both roles share one endpoint, exactly as
    # before. Setting it is a DATA-EGRESS decision: the PI/Critic payload carries the dataset
    # profile, accepted findings and artifact digests. See _lab_role_egress_note below.
    lab_base_url = os.environ.get("BIOAGENT_LAB_LLM_BASE_URL") or None
    if lab_base_url:
        lab_model = os.environ.get("BIOAGENT_LAB_LLM_MODEL") or model
        lab_key = os.environ.get("BIOAGENT_LAB_LLM_API_KEY") or api_key
        lab_label = f"{lab_model} @ lab-role endpoint"
    else:
        lab_base_url, lab_model, lab_key, lab_label = base_url, model, api_key, label

    def complete_fn(messages):
        # A dedicated lab endpoint is remote by construction — nothing to heal, so no recovery wrap.
        if lab_base_url is not base_url:
            return vllm_client.complete(0, lab_model, messages,
                                        base_url=lab_base_url, api_key=lab_key)
        return _with_recovery(lambda p: vllm_client.complete(p, model, messages, base_url=base_url, api_key=api_key))

    def scientist_chat(messages, tools):
        return _with_recovery(lambda p: vllm_client.chat_tools(p, model, messages, tools, base_url=base_url, api_key=api_key))

    def count_tokens(messages, tools):
        # EXACT prompt token count via vLLM /tokenize — server-side, inside the GPU
        # node's Singularity container. Returns None for a remote base_url (no /tokenize)
        # so the harness falls back to its char estimate. Best-effort: not worth a heal +
        # retry (the harness already degrades to a char estimate if this can't answer).
        return vllm_client.count_tokens(_port(), model, messages, tools,
                                        base_url=base_url, api_key=api_key)

    return LabLLM(complete_fn, scientist_chat, model, label, count_tokens,
                  scientist_remote=_endpoint_is_off_host(base_url),
                  lab_role_remote=_endpoint_is_off_host(lab_base_url),
                  lab_label=lab_label)


def _lab_event_to_chat(ev: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate one lab ``on_event`` dict into chat-stream WebSocket payloads so the
    live run reads like Claude in the centre panel — instead of the bubble sitting at
    "…" until the report lands. Two channels:

    - ``chat_thinking`` tokens → the COLLAPSIBLE activity log (tool calls, critic
      verdicts, raw turns). Verbose, auto-collapses when the run finishes.
    - ``lab_progress`` → the always-visible KEY-PROGRESS feed (plan, each step,
      acceptances, milestones). Concise, one line per real milestone.

    Pure (no I/O) so it is unit/smoke-testable offline. The caller pushes each payload
    over the connection's WebSocket; it does NOT replace the right-hand technical log,
    which keeps its full ``emit()`` feed.
    """
    t = ev.get("type")
    out: list[dict[str, Any]] = []

    def activity(line: str) -> None:
        out.append({"type": "chat_thinking", "token": line + "\n"})

    def progress(text: str, level: str = "info") -> None:
        out.append({"type": "lab_progress", "text": text, "level": level})

    def status(text: str) -> None:
        # Transient "what's happening right now" label for the live working line. NOT a
        # permanent feed entry — the client overwrites it each time and drops it when the
        # run ends. This is what turns the elapsed-timer heartbeat into a real status.
        out.append({"type": "run_status", "text": text})

    def step_code(code: str) -> None:
        # The FINAL successful code of a step, rendered as a formatted (collapsed) block in
        # the conversation — not dumped into the always-visible feed.
        out.append({"type": "step_code", "code": code})

    if t == "pi_agenda":
        agenda = list(ev.get("agenda") or [])
        progress(f"📋 Plan ready — {len(agenda)} step{'s' if len(agenda) != 1 else ''}")
        for i, step in enumerate(agenda, 1):
            progress(f"  {i}. {step}")
    elif t == "scientist_start":
        progress(f"🔬 {ev.get('specialist', 'Scientist')}: {ev.get('step', '')}")
    elif t == "model_call":
        # The long silent window: the GPU model is generating its next action. Surface it
        # as the live status so the run never looks frozen during inference.
        step = ev.get("step")
        status(f"🧠 Model is reasoning{f' — turn {step}' if step else ''}…")
    elif t == "tool_start":
        # Tool chatter (incl. the noisy run_code loop) lives ONLY in the collapsible
        # activity log — it no longer floods the always-visible key-progress feed.
        activity(f"→ {ev.get('tool')}({json.dumps(ev.get('args', {}))[:80]})")
        # ...but DO drive the live status: "running run_code" is the other silent window.
        status(f"⚙️ Running {ev.get('tool')}…")
    elif t == "tool_result":
        tool = ev.get("tool")
        summary = str(ev.get("summary", "")).strip()
        activity(f"✓ {tool}: {summary}")
        # For run_code, surface the FINAL successful snippet as a formatted code block; the
        # last success in a step overwrites the earlier ones on the client.
        if tool == "run_code":
            code = str((ev.get("args") or {}).get("code", "")).strip()
            if code:
                step_code(code)
    elif t == "tool_error":
        err = str(ev.get("error"))[:160]
        activity(f"⚠ {ev.get('tool')}: {err}")
        progress(f"  ⚠ {ev.get('tool')} failed: {err}", "warning")
    elif t == "finish":
        # The Scientist's own summary of what THIS step found — surface it so the user
        # sees the intermediate result, not just a later "✅ Step accepted" line.
        preview = str(ev.get("answer_preview", "")).strip()
        if preview:
            progress(f"  🔎 Result: {preview}")
    elif t == "critic":
        verdict = str(ev.get("verdict", "")).lower()
        score = ev.get("score")
        score_s = f"{score:.2f}" if isinstance(score, (int, float)) else "?"
        step = str(ev.get("step", ""))
        critique = str(ev.get("critique", "")).strip()
        activity(f"Critic: {verdict.upper()} ({score_s}) — {step[:48]}")
        if verdict == "accept":
            # Step summary: result quality (Critic score) + significance/notes (critique).
            progress(f"✅ Step done — {step[:60]} · quality {score_s}", "success")
            if critique:
                # Show the WHOLE critic rationale — the 'However, …' caveat (what's still imperfect /
                # missing) is the most useful half and used to be guillotined at 220 chars. Keep a
                # generous ceiling only so a runaway rationale can't flood the console.
                shown = critique if len(critique) <= 1200 else critique[:1200].rstrip() + " […]"
                progress(f"  ↳ {shown}")
    elif t == "context_measured":
        # ``exact`` is absent on the research path (which only ever measures via the served
        # model's tokenizer). The chat path sets it explicitly, because it also reports the
        # char ESTIMATE when no tokenizer is reachable — and calling an estimate an exact
        # measurement is precisely the kind of quiet lie this codebase keeps designing out.
        source = "estimate" if ev.get("exact") is False else "server tokenizer"
        activity(f"📏 Context measured ({source}): {ev.get('exact_tokens')} / "
                 f"{ev.get('allowed')} input tokens.")
    elif t == "context_trimmed":
        activity(f"🗜 Context trimmed — compressed {ev.get('compressed_turns', 0)} / "
                 f"dropped {ev.get('dropped_turns', 0)} old turn(s).")
    elif t == "context_overflow_retry":
        # The prompt still overran the window after proactive budgeting — surface the
        # reactive recompaction so a stall reads as "recompacting", not "frozen".
        progress(f"  🗜 Context over the model window — recompacting and retrying "
                 f"(attempt {ev.get('attempt', 1)})…", "warning")
    elif t == "user_injection":
        progress(f"📝 Incorporating your note: {str(ev.get('text', ''))[:80]}")
    elif t == "skills_loaded":
        # Which preset pipeline(s) the agent loaded to steer the plan (or none). Each is a full
        # end-to-end workflow that composes the atomic registry tools listed after "composes:".
        skills = list(ev.get("skills") or [])
        if skills:
            names = ", ".join(str(s.get("label") or s.get("key")) for s in skills)
            tools = sorted({tl for s in skills for tl in (s.get("tools") or [])})
            progress(f"📚 Loaded preset pipeline: {names}"
                     + (f" (composes tools: {', '.join(tools)})" if tools else ""))
        else:
            progress("📚 No matching preset pipeline — planned from scratch.")
    elif t == "skills_required":
        # The user REQUIRED specific atomic skills (the console's skill multi-select) — the plan
        # must apply each.
        names = ", ".join(str(s.get("name")) for s in (ev.get("skills") or []))
        if names:
            progress(f"🧩 Required skills (must apply): {names}")
    elif t == "steps_pruned":
        dropped = list(ev.get("dropped") or [])
        if ev.get("reason") == "no_experimental_contrast":
            progress(f"✂️ Dropped {len(dropped)} pathway-enrichment step(s): the dataset is already "
                     f"annotated with no experimental contrast, so enrichment on identity markers "
                     f"would be circular.")
        elif dropped:
            progress(f"✂️ Dropped {len(dropped)} step(s) the data cannot support.")
    elif t == "context_pressure":
        pct = int(float(ev.get("ratio") or 0) * 100)
        progress(f"🧮 Context check — {ev.get('reason')} ({ev.get('tokens')} of "
                 f"{ev.get('budget')} tokens, {pct}%).")
    elif t == "context_compact":
        act = ev.get("action")
        if act == "compacted":
            progress(f"🗜 Compacted the first {ev.get('folded_steps')} step(s) into a summary — "
                     f"{ev.get('tokens_before')} → {ev.get('tokens_after')} tokens carried, and all "
                     f"{ev.get('evidence_kept')} artifact path(s) kept.")
        elif act == "declined":
            progress("🗜 Left the history uncompacted, as you asked.")
        elif act == "failed":
            activity("compaction digest failed; the findings were left uncompacted (harmless)")
    elif t == "skill_induced":
        # The library grew. Say it plainly, with provenance — an auto-written template that shows
        # up unannounced in a later run's skill list is the kind of thing a user should have seen
        # being created.
        progress(f"🧠 Learned a reusable skill '{ev.get('name')}': {str(ev.get('description', ''))[:160]}")
        activity(f"induced skill {ev.get('name')} from step: {str(ev.get('origin_step', ''))[:120]}")
    elif t == "skill_induction_none":
        activity("nothing worth keeping as a skill: " + "; ".join(
            str(r)[:80] for r in (ev.get("reasons") or [])))
    elif t == "skill_induction_error":
        activity(f"skill induction failed (harmless): {ev.get('error')}")
    elif t == "cycle_start":
        progress(f"🔁 Cycle {ev.get('cycle')}/{ev.get('max_cycles')} — "
                 f"{len(list(ev.get('agenda') or []))} step(s).")
    elif t == "cycle_planned":
        # The next cycle exists BECAUSE of what the last one found — show the reason, not just the
        # step count, or a second cycle looks like the run restarting itself for no reason.
        progress(f"🧭 Cycle {ev.get('cycle')} planned from the results: {str(ev.get('reason', ''))[:200]}")
    elif t == "cycle_declined":
        progress(f"🏁 No further cycle: {str(ev.get('reason', ''))[:200]}")
    elif t == "campaign_done":
        why = {"max_cycles": "the cycle budget is spent",
               "nothing_left_to_chase": "no question is left that this data can settle",
               "no_progress": "the next plan repeated the one just run",
               "pi_declined": "the study has answered what it can",
               "replan_failed": "the re-planning call failed",
               "cancelled": "you stopped the run"}.get(str(ev.get("reason")), str(ev.get("reason")))
        progress(f"🔁 {ev.get('cycles')} cycle(s), {ev.get('steps')} step(s) total — stopped because "
                 f"{why}.")
    elif t == "hypothesis_formed":
        # The moment a run stops executing its plan and starts doing research: a result contradicted
        # the plan's premise, so the PI states a FALSIFIABLE claim about the biology. Surfaced in
        # full (not collapsed into the activity log) — this is the thing the user is watching for.
        progress(f"💡 New hypothesis [{ev.get('id')}]: {str(ev.get('statement', ''))[:220]}")
        if ev.get("prediction"):
            progress(f"   ↳ predicts: {str(ev.get('prediction'))[:200]}")
        if ev.get("test"):
            progress(f"   ↳ would be distinguished by: {str(ev.get('test'))[:200]}")
    elif t == "step_added":
        progress(f"➕ Added a step to test [{ev.get('hypothesis_id')}]: "
                 f"{str(ev.get('step', ''))[:200]}")
    elif t == "agenda_extended":
        added = list(ev.get("added") or [])
        progress(f"🧭 The plan grew to {ev.get('agenda')} steps — {len(added)} discovered during the "
                 f"run, not planned up front.")
    elif t == "hypothesis_resolved":
        icon = {"supported": "✅", "refuted": "❌"}.get(str(ev.get("status")), "◑")
        progress(f"{icon} Hypothesis [{ev.get('id')}] {ev.get('status')}: "
                 f"{str(ev.get('evidence') or ev.get('statement', ''))[:220]}")
    elif t == "lab_plan_dag":
        # DAG planner: the reviewed steps have been structured into a dependency graph the
        # scheduler runs by readiness (instead of a fixed order).
        nodes = list(ev.get("nodes") or [])
        n_dep = sum(1 for n in nodes if n.get("depends_on"))
        progress(f"🕸 Structured {len(nodes)} task(s) into a dependency plan "
                 f"({n_dep} with prerequisites) — running them by readiness.")
    elif t == "coordinator_pick":
        ready = list(ev.get("ready") or [])
        activity(f"Coordinator → {ev.get('next')} (from ready {ready})")
        progress(f"🧭 Coordinator chose the next task from {len(ready)} ready option(s).")
    elif t == "node_claim":
        # Real multi-agent: an expert claimed this task by expertise fit.
        progress(f"🙋 {str(ev.get('specialist', 'An expert'))} claimed this task.")
    elif t == "concurrency_batch":
        nodes = list(ev.get("nodes") or [])
        progress(f"⚡ Running {len(nodes)} independent tasks in parallel.")
    elif t == "memory_read":
        activity(f"🧠 {str(ev.get('specialist', 'expert'))} recalled its memory from past runs.")
    elif t == "memory_reflect":
        progress(f"🧠 {str(ev.get('specialist', 'An expert'))} updated its lessons from this run.")
    elif t == "step_failure":
        # A step exhausted its retries; the LLM proposed alternative approaches to pick from.
        n_alt = len(ev.get("alternatives") or [])
        progress(f"⚠️ Step failed — proposed {n_alt} alternative approach(es) for: "
                 f"{str(ev.get('goal', ''))[:60]}", "warning")
    elif t == "step_retry":
        appr = str(ev.get("approach", "")).strip()
        progress("↻ Retrying with: " + (appr[:80] if appr else "an alternative approach") + ".")
    elif t == "decision_point":
        progress(f"🔀 Decision point — waiting for your choice: {str(ev.get('goal', ''))[:70]}")
    elif t == "decision_made":
        progress(f"✅ Your choice: {str(ev.get('choice', ''))[:80]}", "success")
    elif t == "plan_cancelled":
        progress("✋ Plan cancelled — nothing was executed.", "warning")
    elif t == "lab_done":
        progress(
            f"🏁 Analysis done — {ev.get('accepted_steps')}/{ev.get('agenda')} "
            f"step(s) accepted (converged={ev.get('converged')}).",
            "success" if ev.get("converged") else "warning",
        )
    return out


def _build_report_render_fn(conn: "Connection"):
    """The pandoc/xelatex renderer for this connection: a ``SlurmReportRenderer`` (renders on an
    HPC3 CPU Slurm job, keeping texlive off the eyeserver) when report-on-HPC is enabled AND a live
    SSH session exists, else ``None`` (``build_pdf_report`` then shells out to local pandoc). Shared
    by ``_run_lab`` and the report-regenerate endpoint so both render the same way."""
    if conn.settings.report_on_hpc and conn.executor is not None and not conn.mock:
        from .slurm_report import SlurmReportRenderer
        from ..tools.report import _run_pandoc
        st = conn.settings
        return SlurmReportRenderer(
            remote=conn.executor, container_image=st.report_image,
            remote_base=f"{_temp_base(conn)}/reports",
            partition=st.cpu_partition, account=st.cpu_account or "",
            container_module=st.container_module, container_bin=st.container_bin,
            local_fallback=_run_pandoc,
        )
    return None


def _write_run_state(art: Path, result: Any, guidance: str | None, decisions: dict[str, Any]) -> None:
    """Persist everything an A2 resume needs — the agenda + accepted rounds (via ``result.to_dict``)
    plus the resolved skill ``guidance`` and the dataset pointers — to
    ``artifacts/process/run_state.json``. A future ``/api/lab/continue`` loads this to re-enter the
    loop from a chosen step without re-planning (see ``ResumeState.from_run_state``). Best-effort:
    resumability metadata must never fail the run."""
    try:
        state = result.to_dict()
        state["guidance"] = guidance
        state["dataset_path"] = str(decisions.get("dataset_path") or "") or None
        if decisions.get("hpc_primary"):
            state["hpc_primary"] = decisions["hpc_primary"]
        if decisions.get("datasets"):
            # The multi-file BIND-SET (feature ②): persist the whole set of staged records so a resume
            # re-enters with every bound file (not just the primary). Legacy single-file runs have one
            # element here; runs that predate this key resume off dataset_path exactly as before.
            state["datasets"] = decisions["datasets"]
        if decisions.get("content_modality"):
            # Feature ①/② Phase B/C: the run-start content triage's modality routes pipeline selection.
            # Persisting it keeps a resume's routing consistent with the original run.
            state["content_modality"] = decisions["content_modality"]
            state["content_confidence"] = decisions.get("content_confidence")
        if decisions.get("case_note"):
            # The attached note defined the run's phenotype; a resume that lost it would silently
            # re-run the phenotype step against nothing.
            state["case_note"] = decisions["case_note"]
        p = art / "process" / "run_state.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, default=str, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - best-effort resumability metadata
        print(f"[lab] run_state persist failed: {exc}")


def _trim_work_keep_checkpoints(work_dir: Path) -> None:
    """Reclaim scratch after a run while PRESERVING the analysis checkpoints (``adata_*.h5ad``) an
    A2 resume reads as the kept step's input. Deletes every other file/dir under ``work/`` (temp
    figures, densified intermediates). Best-effort — never fails the run."""
    try:
        if not work_dir.exists():
            return
        for p in work_dir.iterdir():
            if p.is_file() and p.name.startswith("adata_") and p.suffix == ".h5ad":
                continue   # a stage checkpoint — keep it for resume
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _expire_old_checkpoints(runs_root: Path, ttl_days: int, *, now: float | None = None) -> int:
    """Delete run analysis checkpoints — the ``<owner>/<run_id>/work/`` dirs A2 resume keeps — whose
    newest file is older than ``ttl_days``. This reclaims the large densified ``adata_*.h5ad``
    matrices while NEVER touching ``artifacts/`` (figures/tables/report/data — the deliverables), so
    the report + ``/api/report/regenerate`` keep working; only step-level ``continue`` needs the run
    re-run once after expiry. ``ttl_days <= 0`` disables. Returns how many ``work/`` dirs were
    removed. Best-effort and path-guarded to stay strictly under ``runs_root``."""
    if ttl_days <= 0 or not runs_root.exists():
        return 0
    import time as _time
    cutoff = (now if now is not None else _time.time()) - ttl_days * 86400
    root = runs_root.resolve()
    removed = 0
    for work in root.glob("*/*/work"):
        try:
            if not work.is_dir() or root not in work.resolve().parents:
                continue
            mtimes = [p.stat().st_mtime for p in work.rglob("*") if p.is_file()]
            newest = max(mtimes, default=work.stat().st_mtime)
            if newest < cutoff:
                shutil.rmtree(work, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


async def _checkpoint_gc_loop() -> None:
    """Periodically expire stale analysis checkpoints (``BIOAGENT_CHECKPOINT_TTL_DAYS``, default 7d).
    Runs once at startup then every 6h; a no-op while the TTL is 0. Reads settings fresh each pass so
    a config change takes effect without a restart."""
    while True:
        try:
            ttl = HPCSettings.from_env().checkpoint_ttl_days
            n = await asyncio.to_thread(_expire_old_checkpoints, CONSOLE_RUNS_DIR, ttl)
            if n:
                print(f"[gc] expired {n} run checkpoint dir(s) older than {ttl}d")
        except Exception as exc:  # noqa: BLE001 - housekeeping must never crash the server
            print(f"[gc] checkpoint sweep failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(6 * 3600)


async def _run_lab(conn: Connection, req: LabRequest, *,
                   resume: "ResumeState | None" = None,
                   resume_run_id: str | None = None,
                   resume_decisions: dict[str, Any] | None = None) -> None:
    # This run's isolation scope — cancel + plan events keyed to THIS run, and its identity stamped on
    # every streamed WS event so the client demuxes it into the owning conversation bubble. The caller
    # (dispatch / continue endpoints) opened it via begin_run; a direct/legacy call gets a fresh one.
    # Each run gets its own workspace subdir so artifacts never clobber prior runs (mirrors the
    # pipeline) and the files browser / bundle key on a run_id. An A2 resume REUSES the prior run's id
    # + dir so its analysis checkpoints (work/adata_*.h5ad) + staged dataset are read in place — only
    # the changed step onward is recomputed. Bind the id BEFORE chat_start so the whole stream is tagged.
    run = conn.active_run or conn.begin_run(getattr(req, "conversation_id", None))
    # Ensure THIS run carries its conversation identity even when we reused a pre-existing (possibly
    # bare, _ensure_run-minted) active run — resolve_run() matches Stop / plan-approve by
    # conversation_id, so leaving it None here silently turns the Stop button into a no-op.
    _cid = getattr(req, "conversation_id", None)
    if _cid and not run.conversation_id:
        run.conversation_id = _cid
    run_id = resume_run_id or run.run_id or uuid4().hex[:12]
    conn.bind_run_id(run, run_id)

    run.chat_stop.clear()
    conn.pull_injections()          # drop any stale notes from a prior run
    conn.chat_running = True
    conn.push({"type": "chat_start"})
    emit = conn.emit_fn()

    # Key milestone → the centre panel's live progress feed (Claude-style). Defined up
    # front so the dataset preflight below can use it too.
    def say_key(text: str, level: str = "info") -> None:
        conn.push({"type": "lab_progress", "text": text, "level": level})

    try:
        from ..agents.registry import build_scientist_catalog
        from ..agents.research_harness import HarnessContext, ResearchHarness
        from ..agents.research_lab import LabConfig, ResearchLab
        from ..agents.sandbox import CodeSandbox
        from ..tools.datasets import run_dataset_smoke_analysis
        from ..tools.report import build_pdf_report
        from ..tools.research_bundle import write_process_artifacts
        from ..tools.schematic import render_dot, workflow_schematic_dot

        art = conn.workspace / run_id / "artifacts"
        art.mkdir(parents=True, exist_ok=True)

        # Record the run in this user's history (when logged in); updated on finish. A resume
        # keeps the SAME run row (it overwrites the same bundle), so don't insert a new one.
        if resume is None and _AUTH_ENABLED and conn.app_user_id:
            try:
                auth_routes.record_run_start(conn.app_user_id, run_id, req.question, req.plan_mode,
                                             conversation_id=run.conversation_id)
            except Exception as exc:  # noqa: BLE001 - history is best-effort
                print(f"[auth] record_run_start failed: {exc}")

        # Load the dataset (path only — the raw matrix never enters a prompt) into the
        # derived dataset_result the QC/DE tools read.
        decisions: dict[str, Any] = dict(resume_decisions or {})
        # The attached case note (see LabRequest.case_note): map_phenotype_to_hpo reads it from here
        # when the model doesn't pass `text` itself. A resume keeps the note it already had.
        note = _clean_case_note(req.case_note)
        if note:
            decisions["case_note"] = note
            emit("step", "lab", f"Case note attached ({len(note)} chars) — the phenotype step will "
                                f"map it to HPO terms.")
        _no_data_warning = (
            "No dataset attached — the analysis tools (QC / clustering / DE / enrichment) "
            "have no data to work on, so the report will contain NO data figures. Attach or "
            "upload a dataset in the Research panel to get a full analysis with figures."
        )
        # The BIND-SET (feature ②): the run's data files, primary first. Legacy single-file requests
        # yield a one-element list holding exactly ``dataset_path`` — so everything below (which stages
        # the PRIMARY) is byte-for-byte today's behaviour. The SECONDARY files are staged after.
        bound = _select_bound_datasets(req)
        primary_path = bound[0]["path"] if bound else None
        if resume is not None:
            # Resume: reuse the prior run's decisions (dataset_path / hpc_primary / datasets already
            # staged); the redone step reads its input from the preserved checkpoint, not the raw matrix.
            say_key(f"↻ 续跑 run {run_id} —— 从第 {resume.from_step_index + 1} 步开始重做,复用已有分析结果。")
            emit("step", "lab", f"Resuming run {run_id} from step {resume.from_step_index + 1} "
                                f"(reusing {len(resume.prior_rounds)} prior step checkpoint(s)).")
        elif primary_path and _is_remote_dataset(conn, primary_path):
            # Data lives on HPC3 dfs3b (uploads_on_hpc). Resolve the matrix there (a file, or the
            # primary matrix inside a folder), stage THAT back for the still-local tools, and remember
            # the remote matrix so analysis-on-HPC reads it in place (no round-trip).
            dp = primary_path
            is_dir = ((await asyncio.to_thread(conn.executor.exec,
                                               f"test -d {shlex.quote(dp)} && echo D")).out or "").strip() == "D"
            remote_matrix = (await asyncio.to_thread(_find_primary_matrix_remote, conn, dp)) if is_dir else dp
            if remote_matrix:
                say_key("⤵ Staging dataset from your HPC3 storage…")
                p = await asyncio.to_thread(
                    _ensure_local_dataset, conn, remote_matrix, conn.workspace / run_id / "staged")
                decisions["hpc_primary"] = remote_matrix    # analysis-on-HPC reads this dfs3b path in place
                emit("step", "lab", f"Loading {p.name} from your HPC3 storage ...")
                say_key(f"📂 Loaded dataset: {p.name}" + (f" (primary in {dp.rsplit('/', 1)[-1]}/)" if is_dir else ""))
                decisions["dataset_result"] = run_dataset_smoke_analysis(p, art / "data")["result"]
                decisions["dataset_path"] = str(p)
            else:
                # No recognized dataset file in the folder. Keep the folder BOUND anyway (parity with
                # the local branch below): run_code can still read the tree, and hpc_primary lets an
                # on-HPC step reach it in place. Leaving dataset_path unset — the old behaviour — made
                # the run silently dataset-less, which reads downstream as "no data was uploaded".
                decisions["hpc_primary"] = dp
                decisions["dataset_path"] = dp
                emit("warning", "lab", f"HPC3 folder {dp} has no recognized dataset file "
                                       f"(.h5ad/.h5/.loom/.csv/.vcf/.vcf.gz) — the tools have no "
                                       f"primary input; the agent can still read the tree via run_code.")
                say_key("📂 HPC3 folder loaded (no primary dataset auto-detected).", "warning")
        elif primary_path:
            p = Path(primary_path)
            if p.is_dir():
                # A folder upload: pick the primary matrix inside (for the QC/DE tools),
                # but the WHOLE tree is still reachable by run_code via BIOAGENT_UPLOADS.
                primary = _find_primary_matrix(p)
                if primary is not None:
                    emit("step", "lab", f"Loading folder {p.name}/ — primary dataset {primary.name} (the whole folder is available to the code sandbox) ...")
                    say_key(f"📂 Loaded folder: {p.name}/ (primary: {primary.name})")
                    decisions["dataset_result"] = run_dataset_smoke_analysis(primary, art / "data")["result"]
                    decisions["dataset_path"] = str(primary)
                else:
                    emit("info", "lab", f"Folder {p.name}/ has no recognized dataset file (.h5ad/.h5/.loom/.csv/.vcf/.vcf.gz) — the agent can still read its files via run_code (BIOAGENT_UPLOADS).")
                    say_key(f"📂 Loaded folder: {p.name}/ (no primary dataset auto-detected)")
                    decisions["dataset_path"] = str(p)
            elif p.exists():
                emit("step", "lab", f"Loading {p.name} (the raw matrix isn't pasted into the prompt — only derived metrics are; the model reads the full data through the code sandbox) ...")
                say_key(f"📂 Loaded dataset: {p.name}")
                # Preflight/input records go in the categorized data/ subdir.
                decisions["dataset_result"] = run_dataset_smoke_analysis(p, art / "data")["result"]
                decisions["dataset_path"] = str(p)
            else:
                emit("warning", "lab", f"Dataset not found: {p} — {_no_data_warning}")
                say_key(f"⚠ Dataset not found: {p.name} — analysis will have no data figures.", "warning")
        else:
            # Silent before — the user saw an empty report and couldn't tell why. Make it loud.
            emit("warning", "lab", _no_data_warning)
            say_key("⚠ No dataset attached — analysis will produce no data figures.", "warning")

        # Assemble the BIND-SET record (feature ②). The PRIMARY was staged above (it drives the legacy
        # dataset_path/hpc_primary the tools read); now stage each SECONDARY (a BED panel, a 2nd VCF, …)
        # so a local readable copy exists for the tools and the dfs3b path is remembered for on-HPC
        # steps, and record the whole set in decisions["datasets"]. On a resume this is already restored
        # in resume_decisions — do not re-stage.
        if resume is None and bound:
            records: list[dict] = [_primary_dataset_record(bound[0], decisions)]
            for entry in bound[1:]:
                try:
                    rec = await asyncio.to_thread(
                        _stage_secondary_dataset, conn, entry["path"], entry["name"],
                        entry.get("role"), conn.workspace / run_id / "staged")
                except Exception as exc:  # noqa: BLE001 - a secondary must never sink the run
                    emit("warning", "lab", f"Could not stage bound file {entry['name']}: "
                                           f"{type(exc).__name__}: {exc}")
                    rec = {"path": entry["path"], "name": entry["name"],
                           "role": entry.get("role"), "error": str(exc)[:200]}
                records.append(rec)
                say_key(f"📎 Bound extra file: {rec['name']}"
                        + (f" ({rec['role']})" if rec.get("role") else ""))
            decisions["datasets"] = records
            if len(records) > 1:
                emit("step", "lab", f"Bind-set: {len(records)} data files "
                                    f"(primary {records[0]['name']} + {len(records) - 1} more).")

        _llm = _lab_llm(conn)
        complete_fn, scientist_chat = _llm.complete_fn, _llm.scientist_chat
        model, llm_label, count_tokens = _llm.model, _llm.label, _llm.count_tokens
        llm_remote = _llm.scientist_remote
        emit("step", "lab", f"Research lab starting (LLM: {llm_label}, model: {model}).")

        # Feature ② Phase C — AUTO-DESCRIBE the bound files against the session's served model (up
        # since connect; no extra provisioning). Records each file's "what is this" and sets
        # the primary's content modality so pipeline routing (Phase B) uses CONTENT, not the extension.
        # Skipped on a resume (routing guidance is already restored). Best-effort — never fails the run.
        if resume is None and decisions.get("datasets"):
            try:
                gists = await asyncio.to_thread(_triage_bound_datasets, conn, decisions, model)
                for g in gists:
                    say_key(g)
                cm = decisions.get("content_modality")
                if cm:
                    emit("step", "lab", f"Run-start triage: the primary file reads as {cm} "
                                        f"(confidence {decisions.get('content_confidence') or '?'}) — "
                                        f"pipeline routing uses the CONTENT, not the filename.")
            except Exception as exc:  # noqa: BLE001 - triage is additive; never fail the run over it
                emit("warning", "lab", f"Run-start triage skipped: {type(exc).__name__}: {exc}")

        ctx = HarnessContext(decisions=decisions, workspace=conn.workspace / run_id, model=model,
                             tunnel_port=conn.tunnel_port, llm_is_remote=llm_remote)
        # Scientist catalog = lightweight smoke tools + the REAL single-cell analysis
        # line (scanpy QC/cluster/DE + gseapy enrichment, emitting figures/tables) +
        # the CodeAct escape hatch. The analysis tools degrade gracefully if the
        # `analysis` extra isn't installed, so this is safe on any host.
        # The CodeAct sandbox can read this run's dataset + checkpoints and write new
        # artifacts (exposed as BIOAGENT_DATASET / BIOAGENT_WORK / BIOAGENT_ARTIFACTS).
        sandbox = CodeSandbox(
            should_cancel=conn.chat_stop.is_set,   # Stop kills the in-flight run_code subprocess
            dataset_path=decisions.get("dataset_path"),
            work_dir=str(conn.workspace / run_id / "work"),
            artifacts_dir=str(art),
            # Expose the WHOLE per-user uploads tree so run_code can reach every uploaded
            # file/folder — incl. folders added after the primary dataset was chosen.
            uploads_dir=str(conn.workspace / "uploads"),
        )
        # Opt-in: run CodeAct snippets as CPU Slurm batch jobs on HPC3 so `#SBATCH --mem` gives a
        # REAL, cgroup-enforced memory cap (the durable fix for the OOM/-9 kills) on effectively
        # unlimited CPU/RAM. Off by default → run_code stays on the local sandbox above. When on,
        # the local sandbox becomes the graceful fallback if HPC submission fails.
        code_executor: Any = sandbox
        if conn.settings.run_code_on_hpc and conn.executor is not None and not conn.mock:
            from .job_store import JobStore
            from .slurm_job import resume_incomplete
            from .slurm_sandbox import SlurmCodeExecutor
            st = conn.settings
            # Durable, per-user Slurm-job registry on the (persistent) workspace, so a gateway
            # restart mid-analysis leaves a reattachable record rather than an orphaned job.
            owner = getattr(conn.executor, "username", "")
            job_store = JobStore(conn.workspace / ".bioagent" / "slurm_jobs.json")
            # This session just (re)connected a live SSH executor — sweep any CodeAct jobs a
            # previous gateway process left in-flight and refresh their state (non-blocking).
            try:
                recovered = resume_incomplete(
                    conn.executor, job_store, owner=owner, kind="runcode", emit=emit
                )
                for r in recovered:
                    emit("info", "lab",
                         f"Recovered Slurm job {r.job_id}: {r.state}"
                         + (" (finished)" if r.completed else " (still in flight)"))
            except Exception as exc:  # noqa: BLE001 - reattach is best-effort, never blocks a run
                emit("warning", "lab", f"Slurm job reattach sweep skipped: {type(exc).__name__}: {exc}")
            # run_code's Slurm jobs run on a COMPUTE NODE, which CANNOT see the eyeserver-local
            # /data/BioAgent run dir — bind-mounting it fails container creation with exit 127 (the
            # bug this replaces). So the dataset + work + artifacts must live on dfs3b, exactly like
            # the analysis/VEP lines. Use the SAME dfs3b workspace the analysis line uses, so a
            # run_code snippet and the scanpy steps share one work dir (a snippet routinely reads the
            # adata checkpoints the analysis line wrote). mkdir is idempotent — the analysis block
            # below ensures the same dir. A setup failure leaves the local-sandbox fallback in place.
            rc_remote_ws = f"{_temp_base(conn)}/analysis/{run_id}"
            rc_remote_ds = None
            try:
                conn.executor.exec(
                    f"mkdir -p {shlex.quote(rc_remote_ws)}/work {shlex.quote(rc_remote_ws)}/artifacts")
                # Dataset: the dfs3b copy in place (uploads_on_hpc) if present, else stage the local
                # dataset up once. The local staged copy (decisions['dataset_path']) is unusable — under
                # uploads_on_hpc it is removed after the push to dfs3b, and it is a /data/BioAgent path
                # the node can't see regardless.
                if decisions.get("hpc_primary"):
                    rc_remote_ds = decisions["hpc_primary"]
                elif decisions.get("dataset_path") and Path(decisions["dataset_path"]).exists():
                    rc_remote_ds = f"{rc_remote_ws}/{Path(decisions['dataset_path']).name}"
                    await asyncio.to_thread(conn.executor.put_file, decisions["dataset_path"], rc_remote_ds)
            except Exception as exc:  # noqa: BLE001 - stay on the local sandbox rather than submit broken jobs
                emit("warning", "lab", f"run_code-on-HPC3 workspace setup failed ({exc}); "
                                       "run_code uses the local sandbox this run.")
                rc_remote_ws = None
            if rc_remote_ws:
                code_executor = SlurmCodeExecutor(
                    remote=conn.executor,
                    container_image=st.analysis_image,
                    # dfs3b paths the compute node CAN bind (env overrides still win for a genuinely
                    # shared-mounted deployment).
                    dataset_path=os.environ.get("BIOAGENT_HPC_DATASET") or rc_remote_ds,
                    work_dir=os.environ.get("BIOAGENT_HPC_WORK") or f"{rc_remote_ws}/work",
                    artifacts_dir=os.environ.get("BIOAGENT_HPC_ARTIFACTS") or f"{rc_remote_ws}/artifacts",
                    local_artifacts=str(art),   # mirror artifacts back for the still-local report bundler
                    # dfs3b scratch, OFF $HOME — Slurm does not expand $HOME in #SBATCH --output, which
                    # would misdirect the job log (matches the variant line's scratch).
                    scratch_dir=f"{_temp_base(conn)}/scratch/runcode",
                    mem_gb=st.run_code_mem_gb, cpus=st.run_code_cpus, partition=st.cpu_partition,
                    account=st.cpu_account or "", time_limit=st.run_code_time_limit,
                    container_module=st.container_module, container_bin=st.container_bin,
                    local_fallback=sandbox, job_store=job_store, owner=owner,
                    should_cancel=conn.chat_stop.is_set,   # Stop scancels the in-flight run_code job
                )
                emit("info", "lab", f"CodeAct runs on HPC3 (CPU Slurm, --mem={st.run_code_mem_gb}G).")
        # scGPT per-cell annotation runs as a GPU batch job on HPC3 (Route C). Wire its
        # runner only for a LIVE session (a real SSH executor) — in mock/offline mode the
        # tool stays present but reports not-enabled (the mock host has no batch lifecycle).
        scgpt_runner = None
        if conn.executor is not None and not conn.mock:
            from .scgpt_runner import build_scgpt_runner
            scgpt_runner = build_scgpt_runner(
                conn.executor, conn.settings, cluster_user_dir=_temp_base(conn)
            )
        # Opt-in: run the scanpy analysis line (QC/clustering/DE/enrichment) as CPU Slurm batch jobs
        # on HPC3, reading the dataset on dfs3b in place. The dataset is used in place if it already
        # lives on dfs3b (uploads_on_hpc), else staged up once. Checkpoints accumulate in the dfs3b
        # run dir; small artifacts sync back for the (still-local) report bundler. On HPC failure the
        # executor falls back to running the SAME tool in-process (scrna_cli), so a run never
        # hard-fails; off / mock → the analysis tools stay in-process unchanged.
        analysis_executor = None
        if conn.settings.analysis_on_hpc and conn.executor is not None and not conn.mock:
            st = conn.settings
            # Sync the CURRENT tools to dfs3b (bind-mounted into the job → no image rebuild on tool
            # edits). If staging fails, stay in-process rather than run jobs that can't import.
            try:
                pysrc = await asyncio.to_thread(_sync_bioagent_source_to_hpc, conn)
            except Exception as exc:  # noqa: BLE001
                emit("warning", "lab", f"Analysis-on-HPC3 disabled: couldn't stage the tools to dfs3b "
                                       f"({exc}). Running analysis in-process.")
                pysrc = None
            if pysrc:
                remote_ws = f"{_temp_base(conn)}/analysis/{run_id}"
                conn.executor.exec(f"mkdir -p {shlex.quote(remote_ws)}")
                if decisions.get("hpc_primary"):
                    remote_ds = decisions["hpc_primary"]           # dfs3b matrix (file or folder-primary) — in place
                elif decisions.get("dataset_path"):
                    remote_ds = f"{remote_ws}/{Path(decisions['dataset_path']).name}"
                    await asyncio.to_thread(conn.executor.put_file, decisions["dataset_path"], remote_ds)
                else:
                    remote_ds = None

                def _analysis_local_fallback(tool: str, args: dict, ctx: Any) -> dict:
                    from ..tools.scrna_cli import run_tool as _cli_run
                    ws = str(getattr(ctx, "workspace", conn.workspace / run_id))
                    ds = (getattr(ctx, "decisions", {}) or {}).get("dataset_path")
                    return _cli_run(tool, ws, ds, args)

                from .slurm_analysis import SlurmAnalysisExecutor
                analysis_executor = SlurmAnalysisExecutor(
                    remote=conn.executor, container_image=st.analysis_image,
                    remote_workspace=remote_ws, remote_dataset=remote_ds,
                    local_workspace=conn.workspace / run_id, source_dir=pysrc,
                    mem_gb=st.run_code_mem_gb, cpus=st.run_code_cpus, partition=st.cpu_partition,
                    account=st.cpu_account or "", time_limit=st.run_code_time_limit,
                    container_module=st.container_module, container_bin=st.container_bin,
                    local_fallback=_analysis_local_fallback,
                    should_cancel=conn.chat_stop.is_set,   # Stop scancels the in-flight analysis job
                )
                emit("info", "lab", f"scanpy analysis runs on HPC3 (CPU Slurm, --mem={st.run_code_mem_gb}G).")

        # Opt-in: run VCF variant annotation OFFLINE on HPC3 (bcftools PASS-filter -> vep --offline
        # --cache --fork over a local cache -> stream-parse), so a WGS-size VCF is handled without the
        # REST tool's whole-file-in-memory read / 500-variant cap / rate limits. Falls back to REST on
        # failure. EVERY branch below is logged AND persisted to process/variant_offline_diagnostics.log
        # so a silent "fell back to REST" is never a mystery: you see the flag state, the resolved paths,
        # and whether the sif / cache / ClinVar actually exist on HPC3.
        variant_executor = None
        st = conn.settings
        _ds_name = str(decisions.get("hpc_primary") or decisions.get("dataset_path") or "")
        _is_vcf = _ds_name.lower().endswith((".vcf", ".vcf.gz"))
        _vdiag: list[str] = []

        def _vlog(level: str, msg: str) -> None:
            _vdiag.append(f"[{level}] {msg}")
            emit(level, "lab", msg)

        # Genome build: start from the configured default, then OVERRIDE it with the build read from the
        # VCF header when detectable. The uploaded VCF is often hg19/GRCh37; annotating those coordinates
        # against the .env-default GRCh38 cache runs cleanly but mis-assigns the gene at ~90% of sites
        # (empirically verified), so we route to the matching cache/ClinVar rather than trust the flag.
        _assembly = str(st.vep_assembly)
        if _is_vcf:
            _detected = await asyncio.to_thread(_detect_vcf_assembly, conn, decisions)
            if _detected and _detected.lower() != _assembly.lower():
                _vlog("info", f"Variant assembly: VCF header indicates {_detected} — overriding the "
                              f"configured {_assembly} so the matching VEP cache/ClinVar is used.")
                _assembly = _detected
            elif not _detected:
                _vlog("info", f"Variant assembly: build not detectable from the VCF header; using the "
                              f"configured default {_assembly}.")
        _grch37 = _assembly.lower() in ("grch37", "hg19")
        _cache_dir = st.vep_cache_dir_grch37 if _grch37 else st.vep_cache_dir_grch38
        _clinvar = st.vep_clinvar_grch37 if _grch37 else st.vep_clinvar_grch38

        if _is_vcf or st.variant_on_hpc:
            _vlog("info",
                  f"Variant-annotation config: BIOAGENT_VARIANT_ON_HPC="
                  f"{'ON' if st.variant_on_hpc else 'OFF'}; vep_image={st.vep_image}; "
                  f"cache={_cache_dir}; clinvar={_clinvar}; assembly={_assembly}; "
                  f"executor={'yes' if conn.executor is not None else 'NONE'}; mock={conn.mock}; "
                  f"dataset={'vcf' if _is_vcf else (_ds_name or 'none')}.")

        # WHY the offline line is / isn't wired — say it out loud, per condition.
        if not st.variant_on_hpc:
            if _is_vcf:
                _vlog("warning",
                      "Offline VEP line OFF: BIOAGENT_VARIANT_ON_HPC is not set to 1/true/yes/on in the "
                      "deployment .env. The VEP_* sif/cache paths ALONE do not enable it — that flag IS the "
                      "switch. Annotating via the capped REST path.")
        elif conn.executor is None or conn.mock:
            _vlog("warning",
                  f"Offline VEP line requested but not runnable this run: "
                  f"executor={'NONE' if conn.executor is None else 'ok'}, mock={conn.mock}. Using REST.")
        else:
            # Flag ON + live executor: PREFLIGHT the sif + cache + ClinVar on HPC3 before wiring, so a
            # wrong/un-staged path is reported HERE instead of as a mystery Slurm-job failure later.
            checks = await asyncio.to_thread(
                _variant_offline_preflight, conn.executor, st.vep_image, _cache_dir, _clinvar)
            for name, (path, ok) in checks.items():
                if not path:
                    _vlog("warning", f"Offline VEP: {name} is not configured (empty path in .env).")
                elif ok is False:
                    _vlog("warning",
                          f"Offline VEP: {name} NOT found on HPC3: {path} — the VEP job would fail and fall "
                          f"back to REST. Stage it (deploy/vep/build_and_stage.sh) or fix the .env path.")
                elif ok is None:
                    _vlog("warning", f"Offline VEP: could NOT verify {name} on HPC3 ({path}) — SSH check failed.")
                else:
                    _vlog("info", f"Offline VEP: {name} present on HPC3 ({path}).")
            try:
                v_pysrc = await asyncio.to_thread(_sync_bioagent_source_to_hpc, conn)
            except Exception as exc:  # noqa: BLE001
                _vlog("warning", f"Offline VEP disabled: couldn't stage the tools to dfs3b ({exc}). Using REST.")
                v_pysrc = None
            if v_pysrc:
                remote_ws = f"{_temp_base(conn)}/variant/{run_id}"
                conn.executor.exec(f"mkdir -p {shlex.quote(remote_ws)}")
                if decisions.get("hpc_primary"):
                    remote_ds = decisions["hpc_primary"]           # dfs3b VCF — annotated in place
                elif decisions.get("dataset_path"):
                    remote_ds = f"{remote_ws}/{Path(decisions['dataset_path']).name}"
                    await asyncio.to_thread(conn.executor.put_file, decisions["dataset_path"], remote_ds)
                else:
                    remote_ds = None
                if remote_ds is None:
                    _vlog("warning", "Offline VEP: no dataset path resolved (no hpc_primary / dataset_path) — "
                                     "the job would have no VCF to annotate.")

                def _variant_local_fallback(tool: str, args: dict, ctx: Any) -> dict:
                    from ..tools.variant_annotation import annotate_variants_rest
                    return annotate_variants_rest(args, ctx)

                def _variant_on_fallback(reason: str) -> None:
                    # Fires the moment the offline VEP job degrades to REST — logs the VERBATIM reason
                    # (Slurm error tail / "no live HPC connection") AND appends it to the diagnostics file.
                    msg = ("Offline VEP job did NOT complete — fell back to REST (capped at 500 variants). "
                           f"Reason: {reason}")
                    emit("warning", "lab", msg)
                    try:
                        _pd = art / "process"
                        _pd.mkdir(parents=True, exist_ok=True)
                        with (_pd / "variant_offline_diagnostics.log").open("a", encoding="utf-8") as fh:
                            fh.write(f"[warning] {msg}\n")
                    except OSError:
                        pass

                # Predictor plugins (CADD/AlphaMissense/REVEL + MANE/HGVS + norm) — opt-in, and now
                # PER-BUILD rather than GRCh38-only. Predictor data is build-specific: handing a GRCh38
                # file to a GRCh37 run is not just useless but unsafe (tabix looks up by coordinate, so
                # a position present in both builds with the same ref/alt returns a DIFFERENT variant's
                # score). So resolve every path for THIS assembly and let the existence check in
                # ``vep_plugin_flags`` drop whatever is not staged for it — a GRCh37 run gets CADD
                # (staged, 80 GB) and simply no AlphaMissense/REVEL, instead of the whole panel being
                # switched off. Inject the paths so they resolve INSIDE vep.sif, and bind each file's
                # parent dir RO (--containall hides everything not bound; a dir bind also exposes the
                # .tbi/.fai).
                #
                # ``_ref_fasta`` is resolved OUT here, not inside the plugin block, because THREE
                # separate features gate on it — `bcftools norm`, VEP `--hgvs`, and SpliceAI — and the
                # last two must still work when the predictor plugins are switched off.
                _b37 = _assembly == "GRCh37"
                # The GRCh37 reference is UCSC hg19, NOT Ensembl GRCh37: the lab's GATK VCFs are
                # chr-prefixed with a 16571 bp chrM (hg19), while Ensembl GRCh37 uses bare `1` and a
                # 16569 bp chrM. Mixing them makes `bcftools norm -f` fail to match contigs, so the
                # staged file is a verified-identical copy of the lab's own hg19 (93/93 contigs match a
                # real WGS VCF's header). Empty ⇒ that build has no reference ⇒ norm/HGVS/SpliceAI stay
                # OFF for it rather than left-aligning against the wrong genome.
                _ref_fasta = st.vep_ref_fasta_grch37 if _b37 else st.vep_ref_fasta
                if _ref_fasta and not os.path.exists(_ref_fasta):
                    _vlog("warning", f"Reference FASTA for {_assembly} is configured but missing "
                                     f"({_ref_fasta}) — normalization, HGVS naming and SpliceAI will be "
                                     "SKIPPED for this run.")
                    _ref_fasta = ""
                _plugin_args: dict = {}
                _plugin_binds: tuple = ()
                if st.vep_plugins_enabled and _assembly in ("GRCh38", "GRCh37"):
                    _cadd_snv = st.vep_cadd_snv_grch37 if _b37 else st.vep_cadd_snv
                    _cadd_indels = st.vep_cadd_indels_grch37 if _b37 else st.vep_cadd_indels
                    _alphamissense = st.vep_alphamissense_grch37 if _b37 else st.vep_alphamissense
                    _revel = st.vep_revel_grch37 if _b37 else st.vep_revel
                    _pf = (_cadd_snv, _cadd_indels, _alphamissense, _revel, _ref_fasta)
                    _dirs = {os.path.dirname(f) for f in _pf if f}
                    if st.vep_plugins_dir:
                        _dirs.add(st.vep_plugins_dir)
                    _plugin_binds = tuple(sorted(d for d in _dirs if d))
                    _plugin_args = {"plugins_enabled": True, "plugins_dir": st.vep_plugins_dir,
                                    "cadd_snv": _cadd_snv, "cadd_indels": _cadd_indels,
                                    "alphamissense": _alphamissense, "revel": _revel,
                                    "ref_fasta": _ref_fasta}
                    _staged = [n for n, p in (("CADD", _cadd_snv), ("AlphaMissense", _alphamissense),
                                              ("REVEL", _revel)) if p and os.path.exists(p)]
                    # Name what actually runs for THIS build, so a blank predictor column is traceable
                    # to "not staged for this assembly" rather than looking like a silent failure.
                    _vlog("info", f"VEP predictor plugins ENABLED for {_assembly}: "
                                  f"{', '.join(_staged) if _staged else 'none staged for this build'}"
                                  f"{' + normalization/HGVS' if _ref_fasta else ''}.")
                # SpliceAI (OpenSpliceAI) — a separate conda-env binary run FROM INSIDE vep.sif (its
                # conda-forge python runs under the container's glibc; validated on HPC3). Bind the env
                # root + model dir + the ref-FASTA dir (RO — the .fai is present so pyfaidx never
                # rebuilds); the ref FASTA is also what norm/HGVS use.
                #
                # Gated on what it ACTUALLY needs (a reference FASTA for this build) rather than on
                # `_assembly == "GRCh38"`. The old assembly check was a proxy for "only GRCh38 has a
                # FASTA", and it silently outlived that fact. The annotation side already handled both
                # builds — `vcf_offline._SPLICEAI_ASSEMBLY` maps GRCh37 → openspliceai's built-in
                # `grch37` annotation — so the FASTA was the only real blocker.
                _spliceai_args: dict = {}
                _spliceai_binds: tuple = ()
                if st.spliceai_enabled and st.spliceai_bin and _ref_fasta:
                    _env_root = os.path.dirname(os.path.dirname(st.spliceai_bin))   # .../envs/openspliceai
                    _spliceai_binds = tuple(sorted({d for d in (
                        _env_root, st.spliceai_models, os.path.dirname(_ref_fasta)) if d}))
                    _spliceai_args = {"spliceai_enabled": True, "spliceai_bin": st.spliceai_bin,
                                      "spliceai_models": st.spliceai_models,
                                      "spliceai_max_variants": st.spliceai_max_variants,
                                      "ref_fasta": _ref_fasta}   # SpliceAI needs the ref FASTA
                    _cap = (f"cap {st.spliceai_max_variants}" if st.spliceai_max_variants else "no cap")
                    _vlog("info", f"SpliceAI (OpenSpliceAI) ENABLED for {_assembly} — splice-disruption "
                                  f"scoring on the post-filter set ({_cap}).")
                elif st.spliceai_enabled and st.spliceai_bin and not _ref_fasta:
                    # Loud, not silent: SpliceAI is switched ON in the environment but cannot run for
                    # this build. A blank splice column must never read as "no splice effect found".
                    _vlog("warning", f"SpliceAI is enabled but no reference FASTA is staged for "
                                     f"{_assembly} — splice scoring SKIPPED (columns will be blank).")
                # Deterministic IRD defaults (no longer dependent on the model choosing to pass them):
                #  - PRE-VEP region restriction: `regions_bed` (the IRD capture panel) — bcftools cuts
                #    the VCF to these regions BEFORE VEP, so VEP annotates a tiny fraction (the compute
                #    saving; ~45-60 min → minutes on a WGS VCF);
                #  - known-gene-first: the default gene panel as `genes` (a POST-VEP precise filter — it
                #    does NOT save annotation time, so `regions_bed` is preferred for scoping/speed);
                #  - rarity floor: the lab's base `max_pop_af` (e.g. 0.005) so common variants drop.
                # All ride in inject_args, so a caller-supplied value overrides them. A panel-load
                # hiccup must never break the run.
                _ird_defaults: dict = {}
                _regions_binds: tuple = ()
                if st.default_regions_bed:
                    _ird_defaults["regions_bed"] = st.default_regions_bed
                    _rd = os.path.dirname(st.default_regions_bed)
                    if _rd:
                        _regions_binds = (_rd,)
                    _vlog("info", "Pre-VEP region restriction ENABLED (regions_bed="
                                  f"{os.path.basename(st.default_regions_bed)}) — bcftools restricts "
                                  "BEFORE VEP, the annotation-time saving. A caller regions_bed overrides.")
                if st.default_gene_panel:
                    try:
                        from ..tools.gene_panels import load_gene_panel
                        _panel_genes = load_gene_panel(st.default_gene_panel)
                        _ird_defaults["genes"] = _panel_genes
                        _vlog("info", f"Known-gene panel '{st.default_gene_panel}' applied by default "
                                      f"({len(_panel_genes)} genes; known-gene-first). A caller-supplied "
                                      "gene list overrides it.")
                    except Exception as exc:  # noqa: BLE001 - a panel miss must not abort the study
                        _vlog("warning", f"Default gene panel '{st.default_gene_panel}' not loaded "
                                         f"({type(exc).__name__}: {exc}); annotating genome-wide.")
                if st.default_max_pop_af and st.default_max_pop_af > 0:
                    _ird_defaults["max_pop_af"] = st.default_max_pop_af
                    _vlog("info", f"Default rarity filter max_pop_af={st.default_max_pop_af} applied "
                                  "(a caller-supplied max_pop_af overrides it).")
                # IRD annotation layers (HGMD / retina-exon / ATAC / dbscSNV) — bind each reference
                # file's DIR into vep.sif and inject the paths so the tool tabixes them on the reduced
                # set. Gated (OFF by default); a layer with an empty path is skipped by the tool.
                _ird_anno_args: dict = {}
                _ird_anno_binds: tuple = ()
                if st.ird_annotate_enabled:
                    _ird_paths = {k: v for k, v in (
                        ("hgmd_path", st.ird_hgmd), ("dbscsnv_template", st.ird_dbscsnv),
                        ("retina_bed", st.ird_retina_exons), ("atac_path", st.ird_atac)) if v}
                    if _ird_paths:
                        _ird_anno_args = {"ird_annotate": True, **_ird_paths}
                        _ird_anno_binds = tuple(sorted({
                            os.path.dirname(v.replace("{chrom}", "chr1")) for v in _ird_paths.values()
                            if os.path.dirname(v.replace("{chrom}", "chr1"))}))
                        _vlog("info", f"IRD annotation layers ENABLED ({', '.join(sorted(_ird_paths))}).")
                from .slurm_analysis import SlurmAnalysisExecutor
                variant_executor = SlurmAnalysisExecutor(
                    remote=conn.executor, container_image=st.vep_image,
                    remote_workspace=remote_ws, remote_dataset=remote_ds,
                    local_workspace=conn.workspace / run_id, source_dir=v_pysrc,
                    entrypoint="python3 -m bioagent.tools.variant_cli",   # vep.sif is Ubuntu (python3)
                    # Scratch (args/result/log + its rw bind target) MUST live on dfs3b, NOT $HOME:
                    # vep.sif ships /data as a symlink to /opt/vep/.vep (VEP's read-only cache mount),
                    # so bind-mounting a $HOME (=/data/homezvol*/...) scratch resolves through that
                    # symlink into the RO cache and singularity dies at container creation with
                    # "destination ... doesn't exist in container" (exit 127, ~1s) — which is exactly
                    # what silently killed every offline annotate_variants job. A /dfs3b path has no
                    # such symlink so the tmpfs overlay creates it cleanly (same as the workspace bind).
                    job_prefix="bioagent_variant",
                    scratch_dir=f"{_temp_base(conn)}/scratch/variant",
                    # Bind the ClinVar .tbi index alongside its .vcf.gz: VEP's --custom does a Tabix
                    # lookup that needs the index right next to the bgzip, and --containall hides every
                    # host path that isn't an explicit bind — without this VEP dies with "Couldn't find
                    # index for file ...clinvar_*.vcf.gz".
                    extra_ro_binds=tuple(dict.fromkeys(
                        tuple(p for p in (_cache_dir, _clinvar,
                                          f"{_clinvar}.tbi" if _clinvar else "") if p)
                        + _plugin_binds + _spliceai_binds + _ird_anno_binds
                        + _regions_binds)),   # + IRD reference dirs + the pre-VEP regions BED dir
                    inject_args={"cache_dir": _cache_dir, "clinvar_vcf": _clinvar,
                                 "assembly": _assembly, "fork": st.vep_fork,
                                 **_ird_defaults, **_plugin_args, **_spliceai_args,
                                 **_ird_anno_args},
                    mem_gb=st.run_code_mem_gb, cpus=st.vep_fork, partition=st.cpu_partition,
                    # time_limit is the SBATCH --time; the gateway job-wait (run_timeout_s) auto-derives
                    # from it (SlurmAnalysisExecutor default), so a healthy ~30-60 min WGS VEP job is
                    # never scancelled early — Slurm's own --time is the ceiling, completion returns at once.
                    account=st.cpu_account or "", time_limit=st.vep_time_limit,
                    container_module=st.container_module, container_bin=st.container_bin,
                    local_fallback=_variant_local_fallback, on_fallback=_variant_on_fallback,
                    should_cancel=conn.chat_stop.is_set,   # Stop scancels the in-flight VEP job
                    # Idempotent within the run: if a later step re-invokes annotate_variants with the
                    # SAME inputs, reuse this run's result instead of re-submitting the ~45-min WGS VEP
                    # job and overwriting the tables (the "model keeps re-annotating" loop that both
                    # wasted compute AND clobbered the good result with a degraded repeat).
                    memoize_result=True,
                    # Gateway-AUTHORITATIVE args the model cannot override: the assembly detected from the
                    # VCF header (a model passing assembly=GRCh38 on a GRCh37 file makes VEP fail on a
                    # cache-assembly mismatch → wrong genes), and max_variants=0 so the offline path
                    # annotates the WHOLE WGS VCF (a model-supplied cap like max_variants=5000 silently
                    # truncated a 4.9M-variant study to the first 5000 = chrM+chr1-start, a wrong cohort).
                    force_args={"assembly": _assembly, "max_variants": 0},
                )
                if any(ok is False for _, (p, ok) in checks.items() if p):
                    _vlog("warning", "Offline VEP wired BUT a required path is missing above — expect a "
                                     "fallback to REST. Fix the path(s) + rerun for true whole-VCF annotation.")
                else:
                    _vlog("info", f"VCF annotation runs OFFLINE on HPC3 (VEP {_assembly}, "
                                  f"--fork {st.vep_fork}, --mem={st.run_code_mem_gb}G).")

        # Final REST-cap warning for a VCF run that did NOT wire the offline line (any reason above).
        if variant_executor is None and _is_vcf:
            _vlog("warning",
                  "VCF annotation will use the REST path — capped at the FIRST 500 variants. Enable + fix "
                  "the offline line (BIOAGENT_VARIANT_ON_HPC=1 + staged sif/cache) to annotate every "
                  "variant; otherwise all counts/distributions describe only the first 500.")

        # Persist the full diagnostic trail to the bundle (downloadable), even after the run log scrolls.
        if _vdiag:
            try:
                _pdir = art / "process"
                _pdir.mkdir(parents=True, exist_ok=True)
                (_pdir / "variant_offline_diagnostics.log").write_text(
                    "# Offline VEP (variant annotation) wiring diagnostics\n\n" + "\n".join(_vdiag) + "\n",
                    encoding="utf-8")
            except OSError:
                pass

        # Opt-in: phenotype -> disease DIFFERENTIAL DIAGNOSIS on HPC3 (LIRICAL). DOWNSTREAM of the variant
        # line: the model calls run_lirical with the patient's HPO terms; when a VCF is loaded it scores
        # GENOTYPE-AWARE (variants sharpen the ranking), else PHENOTYPE-ONLY. Gated by
        # BIOAGENT_PHENOTYPE_ON_HPC; when off/unstaged the tool reports not_installed and the run continues
        # without the differential. Mirrors the variant line's wiring (deploy/lirical/build_and_stage.sh).
        phenotype_executor = None
        if st.phenotype_on_hpc:
            # Genotype-aware needs the assembly-matched Exomiser dir; reuse the build detected above.
            _p_exo = st.lirical_exomiser_hg19 if _grch37 else st.lirical_exomiser_hg38
            if conn.executor is None or conn.mock:
                emit("warning", "lab",
                     f"Phenotype (LIRICAL) line requested but not runnable this run: "
                     f"executor={'NONE' if conn.executor is None else 'ok'}, mock={conn.mock}.")
            else:
                _p_checks = await asyncio.to_thread(
                    _phenotype_preflight, conn.executor, st.lirical_image, st.lirical_data_dir, _p_exo)
                for name, (path, ok) in _p_checks.items():
                    if not path:
                        emit("warning", "lab", f"LIRICAL: {name} is not configured (empty path in .env).")
                    elif ok is False:
                        emit("warning", "lab",
                             f"LIRICAL: {name} NOT found on HPC3: {path} — the phenotype step would report "
                             "not_installed. Stage it (deploy/lirical/build_and_stage.sh).")
                    elif ok is None:
                        emit("warning", "lab",
                             f"LIRICAL: could NOT verify {name} on HPC3 ({path}) — SSH check failed.")
                    else:
                        emit("info", "lab", f"LIRICAL: {name} present on HPC3 ({path}).")
                try:
                    p_pysrc = await asyncio.to_thread(_sync_bioagent_source_to_hpc, conn)
                except Exception as exc:  # noqa: BLE001
                    emit("warning", "lab", f"Phenotype line disabled: couldn't stage the tools to dfs3b "
                                           f"({exc}).")
                    p_pysrc = None
                if p_pysrc:
                    p_remote_ws = f"{_temp_base(conn)}/phenotype/{run_id}"
                    conn.executor.exec(f"mkdir -p {shlex.quote(p_remote_ws)}")
                    # The VCF is OPTIONAL (phenotype-only works without it). Reuse the dfs3b copy the run
                    # already resolved; only stage a local dataset if it isn't a VCF (then None -> phenotype-only).
                    if decisions.get("hpc_primary") and _is_vcf:
                        p_remote_ds = decisions["hpc_primary"]      # dfs3b VCF — scored in place
                    elif decisions.get("dataset_path") and _is_vcf:
                        p_remote_ds = f"{p_remote_ws}/{Path(decisions['dataset_path']).name}"
                        await asyncio.to_thread(conn.executor.put_file, decisions["dataset_path"], p_remote_ds)
                    else:
                        p_remote_ds = None                          # no VCF -> phenotype-only differential

                    def _phenotype_local_fallback(tool: str, args: dict, ctx: Any) -> dict:
                        # No LIRICAL on the eyeserver: a clean not_installed (never a hard error) so the
                        # run continues without the differential, same as the gated in-process default.
                        return {"status": "not_installed", "candidates": [],
                                "note": "LIRICAL unavailable this run (HPC down or not staged)."}

                    _exo_binds = tuple(d for d in (st.lirical_exomiser_hg19, st.lirical_exomiser_hg38) if d)
                    from .slurm_analysis import SlurmAnalysisExecutor
                    phenotype_executor = SlurmAnalysisExecutor(
                        remote=conn.executor, container_image=st.lirical_image,
                        remote_workspace=p_remote_ws, remote_dataset=p_remote_ds,
                        local_workspace=conn.workspace / run_id, source_dir=p_pysrc,
                        entrypoint="python3 -m bioagent.tools.phenotype_cli",   # lirical.sif has python3
                        job_prefix="bioagent_phenotype",
                        scratch_dir=f"{_temp_base(conn)}/scratch/phenotype",
                        # Bind the LIRICAL data dir + the Exomiser data dir(s) read-only (--containall hides
                        # everything not bound); a dir bind exposes the .mv.db / .ser files inside.
                        extra_ro_binds=tuple(dict.fromkeys((st.lirical_data_dir, *_exo_binds))),
                        # Deploy config the model never sends — injected so it resolves inside lirical.sif.
                        inject_args={"data_dir": st.lirical_data_dir,
                                     "exomiser_hg19": st.lirical_exomiser_hg19,
                                     "exomiser_hg38": st.lirical_exomiser_hg38,
                                     "assembly": _assembly},
                        mem_gb=st.lirical_mem_gb, cpus=st.lirical_cpus, partition=st.cpu_partition,
                        account=st.cpu_account or "", time_limit=st.lirical_time_limit,
                        container_module=st.container_module, container_bin=st.container_bin,
                        local_fallback=_phenotype_local_fallback,
                        should_cancel=conn.chat_stop.is_set,   # Stop scancels the in-flight LIRICAL job
                        memoize_result=True,   # deterministic for the same HPO+VCF — don't re-run in a step loop
                    )
                    if any(ok is False for _, (p, ok) in _p_checks.items() if p):
                        emit("warning", "lab", "LIRICAL wired BUT a required path is missing above — the "
                                               "phenotype step will report not_installed. Fix the path(s).")
                    else:
                        _mode = "genotype-aware" if p_remote_ds else "phenotype-only"
                        emit("info", "lab", f"Phenotype differential runs on HPC3 (LIRICAL, {_mode}).")

        # Opt-in: render the report (pandoc/xelatex) as an HPC3 CPU job so texlive stays off the
        # eyeserver. Passed to build_pdf_report as render_fn; falls back to local pandoc on failure.
        report_render_fn = _build_report_render_fn(conn)
        if report_render_fn is not None:
            emit("info", "lab", "Report rendering runs on HPC3 (CPU Slurm, pandoc/xelatex).")
        scientist = ResearchHarness(
            catalog=build_scientist_catalog(code_executor=code_executor, scgpt_runner=scgpt_runner,
                                            analysis_executor=analysis_executor,
                                            variant_executor=variant_executor,
                                            phenotype_executor=phenotype_executor,
                                            literature_executor=_build_literature_executor(conn)),
            chat_fn=scientist_chat,
            count_tokens_fn=count_tokens,   # exact server-side window sensing (vLLM /tokenize)
        )
        # The console's multi-select skills are PINNED (mandatory) for this run; the PI still
        # auto-selects the best-fit skill ON TOP (pinned = must-have, auto augments). Resolve the
        # picked keys to skill objects and pass them as pinned_skills — the lab composes their
        # guidance + the auto pick. preset_prompt now carries only a user-EDITED free-text override
        # (the frontend sends null); the legacy single `preset` is folded into the picked keys.
        from ..agents.presets import get_preset
        _keys = list(req.presets) if req.presets else ([req.preset] if req.preset else [])
        pinned_skills = tuple(p for p in (get_preset(k) for k in _keys) if p is not None)
        # The console's atomic-skill multi-select: names the run MUST apply. Validate against the
        # loaded library (drop unknown names) so a stale client can't inject bad guidance.
        from ..agents.skills import SKILLS as _ATOMIC_SKILLS
        required_skills = tuple(n for n in (req.skills or []) if n in _ATOMIC_SKILLS)
        preset_prompt = req.preset_prompt
        # Axis A — the user picks the MODE (single agent vs Virtual-Lab team); the PI still
        # auto-selects the skill (Axis B). "auto" lets the PI route the mode itself.
        mode = req.mode if req.mode in ("single", "team", "auto") else "single"
        # Planning budgets are env-tunable so the cloud can adjust without a code change. max_steps
        # is only a runaway guard (default 20 — a thorough plan can be 6-20 steps); max_rounds unset
        # -> derive from the agenda so every planned step runs (no artificial 5-step / 8-round cap).
        _max_steps = int(os.environ.get("BIOAGENT_MAX_STEPS", "20"))
        _max_rounds_env = os.environ.get("BIOAGENT_MAX_ROUNDS")
        _max_rounds = int(_max_rounds_env) if _max_rounds_env else None
        # Planner: DAG (dependency graph + Coordinator) is the DEFAULT execution model now — the UI
        # toggle is gone and the console sends planner="dag". The classic linear order stays reachable
        # for a client that asks for it, or server-wide via BIOAGENT_PLANNER=linear (a hidden fallback).
        planner = (req.planner or os.environ.get("BIOAGENT_PLANNER") or "dag").strip().lower()
        planner = planner if planner in ("linear", "dag") else "dag"
        # Real multi-agent (experts CLAIM nodes by expertise) is part of the DAG feature: on when the
        # DAG planner is on, so the single "DAG planner" toggle gives the whole thing.
        multi_agent = planner == "dag"
        # Concurrency stays a SEPARATE, deliberate opt-in (env only, default 1 = sequential) even in
        # DAG mode: it runs independent branches in parallel threads, so it's the riskiest piece. Only
        # nodes with disjoint footprints ever co-run (the scheduler enforces it).
        try:
            max_concurrency = max(1, int(os.environ.get("BIOAGENT_MAX_CONCURRENCY", "1")))
        except ValueError:
            max_concurrency = 1
        # Per-agent evolving memory (Axis C): env opt-in, and only meaningful in DAG mode (the memory
        # read/write lives in the DAG node loop). Persistent root is per-OWNER, OUTSIDE any run_id dir,
        # so lessons survive across runs. Only the eyeserver ever touches it — never the HPC3 containers.
        agent_memory = (os.environ.get("BIOAGENT_AGENT_MEMORY", "").strip().lower() in ("1", "true", "yes")
                        and planner == "dag")
        agent_memory_dir = str(conn.workspace / "_agent_memory") if agent_memory else None
        # Hypothesis-driven exploration: the ONLY path by which the plan grows mid-run (an accepted
        # step whose result contradicts the plan's premise yields a falsifiable hypothesis + the step
        # that tests it). Env opt-in — off = today's execute-the-drafted-plan behaviour exactly.
        # Costs extra LLM turns and extra analysis steps, so it is a deliberate switch, not a default.
        hypothesis_driven = (os.environ.get("BIOAGENT_HYPOTHESIS_DRIVEN", "").strip().lower()
                             in ("1", "true", "yes"))
        try:
            max_new_steps = max(0, int(os.environ.get("BIOAGENT_MAX_NEW_STEPS", "16")))
        except ValueError:
            max_new_steps = 16
        # Multi-CYCLE campaign: after a cycle finishes, the PI re-plans the next one from what the
        # data said. 1 (default) = plan once, execute, write up — today's behaviour. Each extra
        # cycle is a whole extra plan's worth of GPU time, so keep this small and deliberate.
        try:
            max_cycles = max(1, int(os.environ.get("BIOAGENT_MAX_CYCLES", "1")))
        except ValueError:
            max_cycles = 1
        # Skill INDUCTION: at the end of a run, generalize an accepted run_code procedure into a
        # reusable skill. Written OUTSIDE the repo (never the git-tracked skills/). If the operator
        # set BIOAGENT_INDUCED_SKILLS_DIR, write there — that same path is what skills.py loads at
        # import, so induced skills survive a restart and are shared. Otherwise fall back to this
        # connection's workspace: still usable by later runs in THIS process (register_skill), but
        # gone on restart. Env opt-in either way.
        skill_induction = (os.environ.get("BIOAGENT_SKILL_INDUCTION", "").strip().lower()
                           in ("1", "true", "yes"))
        # Run-scope context management: measure the carried findings block against the served window
        # and compact it before it crowds out the work. Off = today's behaviour.
        context_management = (os.environ.get("BIOAGENT_CONTEXT_MANAGEMENT", "").strip().lower()
                              in ("1", "true", "yes"))
        induced_dir = (os.environ.get("BIOAGENT_INDUCED_SKILLS_DIR")
                       or str(conn.workspace / "_induced_skills")) if skill_induction else None
        # Merge of main's planner budgets (max_steps/max_rounds) and the DAG line's planner config.
        lab_obj = ResearchLab(ctx, LabConfig(preset_prompt=preset_prompt, pinned_skills=pinned_skills,
                                             required_skills=required_skills,
                                             mode=mode,
                                             max_steps=_max_steps, max_rounds=_max_rounds,
                                             planner=planner, multi_agent=multi_agent,
                                             max_concurrency=max_concurrency,
                                             agent_memory=agent_memory, agent_memory_dir=agent_memory_dir,
                                             hypothesis_driven=hypothesis_driven,
                                             max_new_steps=max_new_steps,
                                             max_cycles=max_cycles,
                                             skill_induction=skill_induction,
                                             induced_skills_dir=induced_dir,
                                             context_management=context_management),
                              complete_fn=complete_fn, scientist=scientist)
        if _llm.lab_role_remote:
            # Say this every run, not once at boot. The reasoning payload is NOT covered by the
            # data-boundary guard (that guards the Scientist brief), so an operator who set the
            # lab endpoint months ago should still be reminded what leaves the cluster.
            say_key(f"🌐 Reasoning roles run on a REMOTE model ({_llm.lab_label}). The planning, "
                    "critique and write-up prompts — dataset profile, accepted findings, artifact "
                    "digests — leave this cluster. Analysis and your data stay here.")
        if hypothesis_driven:
            say_key("🔬 Hypothesis-driven exploration ON — a result that contradicts the plan can "
                    f"raise a testable hypothesis and add up to {max_new_steps} steps to test it.")
        if max_cycles > 1:
            say_key(f"🔁 Multi-cycle research ON — up to {max_cycles} cycles; after each one the PI "
                    "re-plans from what the data actually showed, and stops early once the "
                    "answerable questions are answered.")
        if planner == "dag":
            extra = f" · up to {max_concurrency} tasks in parallel" if max_concurrency > 1 else ""
            mem = " · experts carry evolving memory across runs" if agent_memory else ""
            say_key("🕸 DAG planner ON — tasks run by dependency readiness, a Coordinator, and experts "
                    f"that claim tasks by expertise{extra}{mem}.")

        # Whether the plan was cancelled/timed-out during review (nothing was executed). Set from
        # on_event so the report tail can skip rendering a placeholder, dataless manuscript for it.
        run_flags = {"plan_cancelled": False}

        def on_event(ev: dict[str, Any]) -> None:
            # Stream the run into the centre chat bubble (collapsible activity + key
            # progress), in addition to the full technical feed on the right.
            for payload in _lab_event_to_chat(ev):
                conn.push(payload)
            t = ev.get("type")
            if t == "pi_agenda":
                emit("step", "lab", "PI agenda: " + " | ".join(ev["agenda"]))
            elif t == "scientist_start":
                emit("step", "lab", f"{ev.get('specialist', 'Scientist')}: {ev['step']}")
            elif t == "tool_start":
                emit("info", "lab", f"  -> {ev['tool']}({json.dumps(ev.get('args', {}))[:80]})")
            elif t == "tool_result":
                emit("info", "lab", f"  = {ev['tool']}: {ev['summary']}")
            elif t == "tool_error":
                emit("warning", "lab", f"  ! {ev.get('tool')}: {str(ev.get('error'))[:160]}")
            elif t == "critic":
                emit("info", "lab", f"  Critic: {ev['verdict'].upper()} ({ev['score']:.2f}) — {ev['step'][:48]}")
            elif t == "user_injection":
                emit("step", "lab", f"Incorporating your mid-run note: {str(ev.get('text', ''))[:80]}")
            elif t == "plan_cancelled":
                run_flags["plan_cancelled"] = True
                emit("warning", "lab", "Plan cancelled by user — nothing was executed.")
            elif t == "lab_done":
                emit("success", "lab",
                     f"Lab done: converged={ev['converged']} accepted={ev['accepted_steps']}/{ev['agenda']}")
            elif t in ("context_measured", "context_trimmed", "context_overflow_retry"):
                # Persist budgeting decisions to STDOUT → systemd journal, so a
                # `journalctl -u bioagent` pull shows exactly where the prompt tokens go
                # (the WS feed alone is in-memory and lost on restart). One line per call.
                print(f"[budget] {ev}")

        # Plan mode: after the PI plans, push the proposal to the UI and block (in this
        # worker thread) until the user decides. The user does NOT edit the agenda text —
        # they Approve / Cancel, or reply in natural language (which re-plans), and a
        # clarify question is answered the same way. Returns the decision dict the lab
        # negotiation loop expects.
        def plan_review(kind: str, payload: Any) -> dict[str, Any]:
            conn.plan_value = None
            conn.plan_event.clear()
            conn.pending_plan = {"kind": kind, "payload": payload}
            if kind == "clarify":
                conn.push({"type": "plan_clarify", "questions": payload})
                emit("step", "lab", "The PI needs a quick decision — pick an option or type your own.")
            else:
                conn.push({"type": "plan_prompt", "agenda": list(payload)})
                emit("step", "lab", "Plan ready — Run it, or tell the PI what to change in chat.")
            if not conn.plan_event.wait(timeout=600):
                conn.pending_plan = None
                emit("warning", "lab", "Plan review timed out (10 min) — cancelling.")
                return {"action": "cancel"}
            conn.pending_plan = None
            return conn.plan_value or {"action": "cancel"}

        # HITL decision point (DAG planner): a decision node pauses mid-run for the user, reusing the
        # SAME plan_event round-trip (answered via /api/lab/plan). Unlike up-front plan review, a
        # TIMEOUT here PROCEEDS (agent's judgment) rather than cancelling — a live analysis shouldn't
        # be thrown away because nobody clicked. The chosen option is returned as ``choice``.
        def decision_review(node: Any) -> dict[str, Any]:
            goal = getattr(node, "goal", str(node))
            options = list(getattr(node, "options", ()) or [])
            conn.plan_value = None
            conn.plan_event.clear()
            conn.pending_plan = {"kind": "decision", "payload": {"goal": goal, "options": options}}
            conn.push({"type": "decision_prompt", "goal": goal, "options": options})
            emit("step", "lab", f"Decision point — {goal[:80]} (pick an option or type your own).")
            if not conn.plan_event.wait(timeout=600):
                conn.pending_plan = None
                emit("info", "lab", "Decision point timed out — proceeding with the agent's judgment.")
                return {"action": "proceed"}
            conn.pending_plan = None
            v = conn.plan_value or {}
            if str(v.get("action", "")).lower() == "cancel":
                return {"action": "cancel"}
            return {"action": "proceed", "choice": str(v.get("feedback", "")).strip()}

        # should_cancel lets the Stop button (which sets conn.chat_stop) halt the run
        # between steps / tool turns — not just abort the final stream.
        # HITL mode gating. Bypass (autonomous) forces BOTH gates off — no plan review, no
        # decision-point pauses — so the loop runs end-to-end; Stop + mid-run notes still apply.
        # Manual (default) keeps plan review (when plan_mode) and DAG decision-point pauses.
        want_plan_review = (resume is None and req.plan_mode and not req.autonomous)
        # Decision points now fire on BOTH planners in manual mode: the DAG path pauses per-node, and
        # the linear path asks a deterministic plan-time fork (labels vs re-cluster) — previously the
        # linear path never surfaced a decision, so a labeled dataset was silently auto-decided.
        want_decisions = (resume is None and not req.autonomous)
        def take_compact_request() -> bool:
            """Consume a user-requested compaction ONCE. Edge-triggered on purpose: a flag that
            stayed set would compact at every step boundary for the rest of the run."""
            if run.compact_request.is_set():
                run.compact_request.clear()
                return True
            return False

        result = await asyncio.to_thread(
            lab_obj.run, req.question, on_event,
            plan_review if want_plan_review else None,
            should_cancel=run.chat_stop.is_set, pull_injections=conn.pull_injections,
            resume=resume,
            decision_review=(decision_review if want_decisions else None),
            should_compact=take_compact_request)

        # A plan cancelled or timed-out during review — or a run that executed and accepted NOTHING —
        # has no results to write up. Rendering a report from it produced a placeholder, DATALESS
        # manuscript ("[Figure 1. Schematic of the intended … workflow]") that read like a real
        # deliverable AND became the connection's "last run", so the NEXT message was misrouted as a
        # follow-up to it (dropping the dataset, reverting the assembly). Finish cleanly with NO report,
        # NO run_state, and NO last_run_id, so the conversation's next message is a fresh, dataset-bound
        # study. (A mid-run Stop that accepted ≥1 step still writes up what ran — that path has rounds.)
        if _run_produced_nothing(result, run_flags["plan_cancelled"]):
            reason = (result.final_answer or "").strip() or \
                "Plan cancelled or timed out during review — no analysis steps were executed."
            say_key("🛑 计划已取消或超时,未执行任何分析步骤 —— 不生成报告。", "warning")
            emit("warning", "lab", "Run ended before any step was accepted — skipping report render.")
            conn.push({"type": "chat_token", "token": reason})
            conn.push({"type": "chat_stopped"})
            _finish_run_record(conn, run_id, "cancelled", None, reason)
            return

        # Persist the approved plan as a human-readable Markdown file (the user asked for
        # the plan to be saved): report/plan.md, an ordered checklist of the agenda steps.
        if result.agenda:
            try:
                plan_md = art / "report" / "plan.md"
                plan_md.parent.mkdir(parents=True, exist_ok=True)
                body = "\n".join(f"{i}. {s}" for i, s in enumerate(result.agenda, 1))
                plan_md.write_text(
                    f"# Research plan\n\n**Question:** {req.question}\n\n{body}\n",
                    encoding="utf-8")
            except OSError:
                pass

        # Categorized bundle: process/ (the Virtual-Lab meeting record), figures/,
        # tables/ (written by the analysis tools), report/ (final deliverables), data/.
        write_process_artifacts(result.to_dict(), art / "process")
        # Persist the resumable run state (agenda + rounds + guidance + dataset) so a follow-up can
        # re-run a single changed step from this run without re-planning (A2 continuation).
        _write_run_state(art, result, lab_obj.guidance, decisions)
        # Mark this run continuable AS SOON AS its state is durably written — BEFORE the report
        # render. Then a follow-up ("continue and generate the report") can route to the bundle even
        # if the render step later hiccups; a run whose analysis finished must never be un-continuable
        # just because the last step (rendering) struggled. Re-affirmed after publish, below. Scoped
        # per-conversation so ONLY this chat's next message treats it as the run to amend.
        _remember_run(conn, run)
        # Always draw a deterministic methods/workflow schematic from the PI agenda
        # (zero AI — the steps are rendered byte-for-byte by graphviz). Graceful: writes
        # only the .dot source if graphviz isn't installed on the host.
        if result.agenda:
            sch = await asyncio.to_thread(
                render_dot, workflow_schematic_dot(result.agenda, title="Research workflow"),
                art / "figures", basename="workflow")
            if sch["status"] == "ok":
                emit("info", "lab", "Rendered workflow schematic (graphviz).")
            elif sch["status"] == "dependency_missing":
                emit("info", "lab", "workflow.dot written — install graphviz on the server for rendered schematics.")
        # Build the report: hand the model the figure inventory + previews of the result
        # tables so it writes a structured, journal-style report with INLINE data tables
        # and contextual figure references (Ddx41-style), then append an authoritative
        # Output Files Index. Falls back to a deterministic gallery if the LLM step fails.
        # The report lives in report/ but figures/tables sit at the bundle root → assets_root=art.
        say_key("📝 Writing the research report…")
        report_md = await asyncio.to_thread(
            _build_report, result.final_answer or "", art, complete_fn, req.question, result)
        # Literature module: fill the manuscript's reserved '## References' slot from citations
        # actually produced by an accepted in-loop literature step. The research pipeline grounds
        # literature via `deep_literature` (PaperQA over the lab's curated corpus); `literature_search`
        # (Europe PMC) is the dev/no-HPC fallback. Reuse whichever ran — do NOT perform a hidden
        # report-time search; if neither was accepted, the References section says so honestly.
        from ..tools.literature_references import (
            degradation_note, empty_references, insert_references,
            strip_intext_citation_markers)

        say_key("📚 Collecting accepted literature citations…")
        # Prefer deep_literature (the corpus tool the pipeline actually runs); fall back to an
        # accepted Europe PMC literature_search step (dev / no-HPC runs).
        lit = _references_from_accepted_deep_literature(result)
        if lit is None:
            lit = _references_from_accepted_literature_search(result)
        if lit is None:
            lit = empty_references("no accepted literature citations from this run")
        # The report writer emits inline citation markers ("[3]", "[1, 2]") that are
        # narrative artifacts: they map to nothing, since the References list below is rebuilt
        # independently from the run's accepted citations. Strip them so the manuscript never
        # shows a citation number that disagrees with the reference list; the real,
        # corpus-backed list is kept.
        report_md = strip_intext_citation_markers(report_md)
        report_md = insert_references(report_md, lit)
        lit_note = degradation_note(lit)
        try:
            (art / "process" / "literature_references.json").write_text(
                json.dumps(lit, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        if lit["tier"] in ("lab_deep_literature", "lab_literature_search"):
            _src = "indexed corpus (deep_literature)" if lit["tier"] == "lab_deep_literature" \
                else "literature-search step"
            emit("success", "lab", f"References: reused {len(lit['citations'])} citation(s) from the accepted {_src}.")
        else:
            emit("warning", "lab", "References: no accepted literature citations in this run (none fabricated; no hidden fallback search).")
        # Self-review the draft BEFORE rendering: strip leftover/placeholder content, fix
        # numbering, validate figure refs, embed tables (best-effort; keeps draft on failure).
        say_key("🔍 Self-reviewing the draft…")
        report_md = await asyncio.to_thread(
            _review_and_finalize_report, report_md, art, complete_fn, req.question, lit)
        emit("info", "lab", "Self-reviewed the report draft before rendering.")
        say_key("📄 Rendering the report (PDF / DOCX)…")
        # Title the document by CONTENT (the report's own H1 / main finding), not a generic
        # constant — promotes the first heading to the pandoc title and drops it from the body.
        report_title, report_md = _promote_doc_title(report_md, "AiScientist Research Report")
        try:
            rep = await asyncio.to_thread(
                build_pdf_report, report_md, art / "report",
                title=report_title, basename="report", assets_root=art, render_fn=report_render_fn)
        except Exception as exc:  # noqa: BLE001 - the report is the LAST step and the analysis is
            # already done + checkpointed; a render crash must NEVER discard the whole run. Degrade to
            # an error result so the bundle (incl. report.md, written first inside build_pdf_report)
            # still ships and the run finishes — the user regenerates the report from the bundle
            # without re-running (see /api/report/regenerate). The renderer already returns instead of
            # throwing (slurm_report), so this is defense-in-depth against any other render crash.
            traceback.print_exc()
            rep = {"status": "error", "error": f"{type(exc).__name__}: {exc}",
                   "md_path": str(art / "report" / "report.md"), "pdf_path": None, "docx_path": None}
        made = [f for f in ("pdf_path", "docx_path") if rep.get(f)]
        if made:
            emit("success", "lab", f"Rendered report.{' + report.'.join(f.split('_')[0] for f in made)} (pandoc).")
        elif rep["status"] == "markdown_only":
            emit("info", "lab", "report.md written — install pandoc (+ texlive-xetex for PDF) on the server.")
        else:
            # Persist the FULL render error (incl. the HPC3 Slurm-log tail with the real LaTeX
            # cause) to the bundle — the chat emit is truncated, so without this the actual reason
            # is lost. Best-effort; the .md always survives regardless.
            full_err = str(rep.get("error", "") or "").strip()
            if full_err:
                try:
                    err_dir = art / "process"
                    err_dir.mkdir(parents=True, exist_ok=True)
                    (err_dir / "report_render_error.log").write_text(full_err, encoding="utf-8")
                except OSError:
                    pass
            emit("warning", "lab",
                 f"Report render failed (full log: process/report_render_error.log): {full_err[:160]}")
        # Post-render text review: read the rendered document back and flag any remaining
        # issues (advisory — saved to process/report_review.md, never blocks the run).
        visual_diag = ""
        if made:
            # Post-render reviews are ADVISORY and must NEVER throw past here: the analysis is done
            # and checkpointed, and the bundle is published BELOW — a review that raises would skip
            # the publish and the user would lose the ENTIRE run (analysis included) to a "Chat
            # error", with no downloadable bundle. The VL review especially runs as an HPC3 vision-
            # model job and can time out / drop the SSH channel. So wrap the whole review block.
            try:
                review_note = await asyncio.to_thread(_postrender_text_check, rep, art, complete_fn)
                if review_note:
                    emit("warning", "lab",
                         f"Post-render review flagged issues (see process/report_review.md): {review_note[:200]}")
                else:
                    emit("info", "lab", "Post-render review: report reads clean.")
                # Post-render VISUAL review (opt-in): a vision model on HPC3 looks at the rendered
                # pages for LAYOUT defects the text checks are blind to (overlap, clipping, broken
                # tables) and RE-RENDERS with escalated format to fix them. Residual defects feed the
                # technical report's Diagnostics only — the manuscript ships clean.
                visual_diag = await asyncio.to_thread(
                    _postrender_visual_check, rep, report_md, report_title, art, conn)
            except Exception as exc:  # noqa: BLE001 - advisory; degrade to a warning, keep the bundle
                traceback.print_exc()
                emit("warning", "lab", f"Post-render review skipped (non-fatal, bundle preserved): {exc}")

        # Second deliverable: the internal TECHNICAL report — built from the FULL run log
        # (every step incl. failures/substitutions), rendered alongside the manuscript as
        # report/technical_report.{pdf,docx}. Best-effort; never blocks the manuscript.
        try:
            tech_md = await asyncio.to_thread(
                _build_technical_report, result, art, complete_fn, req.question, lit_note, visual_diag)
            tech_title, tech_md = _promote_doc_title(tech_md, "AiScientist Technical Report")
            tech_rep = await asyncio.to_thread(
                build_pdf_report, tech_md, art / "report",
                title=tech_title, basename="technical_report", assets_root=art, render_fn=report_render_fn)
            tech_made = [f for f in ("pdf_path", "docx_path") if tech_rep.get(f)]
            if tech_made:
                emit("success", "lab",
                     f"Rendered technical_report.{' + technical_report.'.join(f.split('_')[0] for f in tech_made)} (pandoc).")
            elif tech_rep["status"] == "markdown_only":
                emit("info", "lab", "technical_report.md written — install pandoc for PDF/DOCX.")
            else:
                emit("warning", "lab", f"Technical report render failed: {str(tech_rep.get('error', ''))[:160]}")
        except Exception as exc:  # noqa: BLE001 - the manuscript already shipped; never fail the run here
            emit("warning", "lab", f"Technical report step failed: {exc}")

        say_key("✅ Report ready — see below and the Downloads panel.", "success")
        conn.push({"type": "chat_token", "token": result.final_answer or "(no report produced)"})
        conn.push({"type": "chat_done"})

        # Bundle cleanup — best-effort. Trim scratch but KEEP the analysis checkpoints
        # (work/adata_*.h5ad): an A2 resume ("re-run clustering, keep QC") reads the kept step's
        # checkpoint as its input, so deleting them would force a full re-run. Delete everything
        # else under work/ to reclaim space. Then move any off-script files the model wrote (a
        # self-made qc_report.docx / self-zipped bundle) into extra/ — kept, not deleted. A failure
        # in either MUST NOT lose the bundle (analysis is done + checkpointed), so it's guarded.
        try:
            _trim_work_keep_checkpoints(conn.workspace / run_id / "work")
            strays = _quarantine_strays(art)
            if strays:
                emit("info", "lab",
                     f"Quarantined {len(strays)} off-script file(s) to extra/: {', '.join(strays[:6])}"
                     + (" …" if len(strays) > 6 else ""))
        except Exception as exc:  # noqa: BLE001 - cleanup is cosmetic; never fail the publish over it
            traceback.print_exc()
            emit("warning", "lab", f"Bundle cleanup step failed (non-fatal): {exc}")

        # ALWAYS record whether the optional GPU capabilities (scGPT, VL review) ran this run —
        # invoked or not, and why not — so a bundle never again shows zero trace of them. Emitted
        # BEFORE event_log.txt is written so the summary is captured in that diary too. Guarded so a
        # diagnostic write can't sink the bundle publish below.
        try:
            _write_capability_log(art, result, conn, emit)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

        # The technical event/error log is no longer shown to the researcher in the UI
        # (it duplicated the chat). Persist the FULL feed into the bundle instead, so it
        # stays recoverable for debugging: process/event_log.txt.
        try:
            log_lines = []
            for ev in list(conn.log):
                ts = str(ev.get("created_at", ""))[11:19]
                line = f"{ts} [{ev.get('level', 'info')}] {ev.get('stage', '')}: {ev.get('message', '')}"
                detail = ev.get("detail")
                if detail is not None:
                    line += "\n    detail: " + json.dumps(detail, default=str)[:2000]
                log_lines.append(line)
            log_path = art / "process" / "event_log.txt"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        except OSError:
            pass

        # Publish the run's files (browsable + previewable + "download all") to the UI.
        items = [
            {"name": p.name, "kind": _file_kind(p.name), "summary": _file_kind(p.name),
             "url": f"/api/file/{conn.owner}/{run_id}/{p.relative_to(art).as_posix()}"}
            for p in sorted(art.rglob("*")) if p.is_file()
        ]
        conn.push({"type": "artifacts", "items": items, "bundle_url": f"/api/bundle/{conn.owner}/{run_id}"})
        # Remember this run so a follow-up can regenerate/modify its report without re-running
        # the pipeline (see /api/report/regenerate); also echoed to the UI to persist client-side.
        _remember_run(conn, run)
        # Carry the agenda so the UI can offer "re-run this step" (A2 continuation) per step.
        conn.push({"type": "run_complete", "run_id": run_id,
                   "agenda": list(result.agenda) if result else []})
        # LabResult has no `status`; derive one from convergence.
        run_status = "done" if (result and result.converged) else "incomplete"
        _finish_run_record(conn, run_id, run_status, str(art),
                           result.final_answer if result else None)
    except GatewayError as exc:
        # Also persist to STDOUT → journal: the WS error feed is in-memory and lost on
        # restart, so without this a `journalctl` pull can't see why a run died.
        print(f"[lab] run failed ({exc.stage or 'lab'}): {exc.message}")
        traceback.print_exc()
        conn.emit("error", exc.stage or "lab", exc.message, detail=error_detail(exc))
        conn.push({"type": "chat_error", "message": exc.message})
        _finish_run_record(conn, locals().get("run_id"), "error", None, str(exc.message))
    except Exception as exc:  # noqa: BLE001 - any lab failure is reported, never fatal
        print(f"[lab] run failed (unhandled): {type(exc).__name__}: {exc}")
        traceback.print_exc()
        conn.emit("error", "lab", f"Lab run failed: {exc}", detail=error_detail(exc))
        conn.push({"type": "chat_error", "message": str(exc)})
        _finish_run_record(conn, locals().get("run_id"), "error", None, str(exc))
    finally:
        conn.chat_running = False
        # Close the run scope so a post-run WS reconnect doesn't see a stale pending plan; the run
        # stays in conn.runs so a late approve/cancel that names it resolves to a no-op.
        conn.end_run(run)


def _run_produced_nothing(result: Any, plan_cancelled: bool) -> bool:
    """True when a run has NO results to write up — the plan was cancelled/timed-out during review, or
    the loop executed and accepted NOTHING (no rounds). Rendering a report from either produced a
    placeholder, dataless manuscript AND marked a bogus "last run", so the report tail is skipped for
    it. A mid-run Stop that still accepted ≥1 step has rounds, so it is written up normally."""
    if plan_cancelled:
        return True
    return bool(result) and result.accepted_steps == 0 and not result.rounds


def _remember_run(conn: "Connection", run: "RunState") -> None:
    """Record a completed run as the connection's last run AND its conversation's last run, so a
    follow-up routes to it. Scoping per-conversation is what stops a fresh window/thread from
    inheriting another conversation's run as a stale replan target."""
    if not run.run_id:
        return
    conn.last_run_id = run.run_id
    if run.conversation_id:
        conn.last_run_by_conversation[run.conversation_id] = run.run_id


_REPORT_EDIT_SYSTEM = (
    "You are editing an existing scientific research report written in Markdown. Apply the user's "
    "requested change and return the COMPLETE revised report as Markdown — same document, edited, "
    "not a summary and not a diff. Preserve everything the change does not touch: section order and "
    "numbering, tables, and figure image links. Only reference figures from the provided valid list; "
    "never invent a figure path. Do not add a YAML front-matter block. Keep the scholarly tone."
)


def _split_front_matter(md: str) -> tuple[str, str]:
    """Split a leading ``---\\n...\\n---`` YAML block (holding the pandoc title) from the body, so a
    report edit can rewrite the body while the title metadata is preserved verbatim. Returns
    ``(front_matter_block_without_trailing_newline, body)``; ``("", md)`` when there is no block."""
    if not md.startswith("---\n"):
        return "", md
    end = md.find("\n---", 4)
    if end == -1:
        return "", md
    close = md.find("\n", end + 1)          # end of the closing '---' line
    if close == -1:
        return md.rstrip(), ""
    return md[:close], md[close + 1:].lstrip("\n")


def _edit_report_body(body: str, instruction: str, art: Path, complete_fn) -> str:
    """One LLM pass that applies ``instruction`` to the report body. Mirrors ``_review_report``'s
    safeguards: constrains figure refs to the run's actual figures and keeps the original body if
    the edit fails or comes back degenerate (so a follow-up never destroys the existing report)."""
    figs = sorted((art / "figures").glob("*.png")) if (art / "figures").exists() else []
    fig_list = "\n".join(f"- figures/{f.name}" for f in figs) or "(none)"
    try:
        edited = complete_fn([
            {"role": "system", "content": _REPORT_EDIT_SYSTEM},
            {"role": "user", "content": (
                f"Requested change:\n{instruction}\n\n"
                f"Valid figure paths (any ![](...) must be one of these):\n{fig_list}\n\n"
                f"Current report to edit:\n\n{body}"
            )},
        ])
    except Exception as exc:  # noqa: BLE001 - never lose the report over an edit hiccup
        print(f"[regen] report edit failed ({exc}); keeping the existing report.")
        return body
    edited = (edited or "").strip()
    if len(edited) < min(200, len(body) // 2):   # degenerate edit → keep the original
        return body
    return edited


async def _regenerate_report(conn: Connection, run_id: str, art: Path, basename: str,
                             instruction: str | None) -> None:
    """Re-render a prior run's report from its bundle (optionally after one instruction-driven edit),
    streaming progress into the chat like a normal run — but with NO PI and NO analysis. Overwrites
    ``report.{md,pdf,docx}`` in place and republishes the run's files so the UI refreshes."""
    # Bind the run scope (the caller opened it via begin_run; a direct call gets a fresh one) so this
    # regenerate's stream is tagged with run_id + conversation_id and its Stop targets only this run.
    run = conn.active_run or conn.begin_run(None, run_id=run_id)
    conn.bind_run_id(run, run_id)
    run.chat_stop.clear()
    conn.chat_running = True
    conn.push({"type": "chat_start"})
    emit = conn.emit_fn()

    def say_key(text: str, level: str = "info") -> None:
        conn.push({"type": "lab_progress", "text": text, "level": level})

    try:
        from ..tools.report import build_pdf_report

        md_path = art / "report" / f"{basename}.md"
        md = md_path.read_text(encoding="utf-8")
        front, body = _split_front_matter(md)
        instr = (instruction or "").strip()
        if instr:
            say_key(f"✏️ 按你的指示修改报告(不重跑分析):{instr[:80]}")
            complete_fn, *_ = _lab_llm(conn)
            body = await asyncio.to_thread(_edit_report_body, body, instr, art, complete_fn)
        else:
            say_key("📄 用已有分析结果重新渲染报告(不重跑分析)…")
        new_md = f"{front}\n\n{body}" if front else body

        render_fn = _build_report_render_fn(conn)
        if render_fn is not None:
            emit("info", "lab", "Report rendering runs on HPC3 (CPU Slurm, pandoc/xelatex).")
        say_key("📄 Rendering the report (PDF / DOCX)…")
        # title=None: new_md already carries the original YAML title block, so build_pdf_report
        # writes it back verbatim (basename.md) and renders from it.
        rep = await asyncio.to_thread(
            build_pdf_report, new_md, art / "report", title=None, basename=basename,
            assets_root=art, render_fn=render_fn)

        made = [f for f in ("pdf_path", "docx_path") if rep.get(f)]
        if made:
            fmts = " + ".join(f"{basename}.{f.split('_')[0]}" for f in made)
            emit("success", "lab", f"Regenerated {fmts} (pandoc).")
        else:
            full_err = str(rep.get("error", "") or "").strip()
            if full_err:
                try:
                    (art / "process").mkdir(parents=True, exist_ok=True)
                    (art / "process" / f"{basename}_render_error.log").write_text(full_err, encoding="utf-8")
                except OSError:
                    pass
            emit("warning", "lab",
                 f"Report render failed (full log: process/{basename}_render_error.log): {full_err[:160]}")

        # Republish the run's files so the Downloads panel + file browser refresh with the new render.
        items = [
            {"name": p.name, "kind": _file_kind(p.name), "summary": _file_kind(p.name),
             "url": f"/api/file/{conn.owner}/{run_id}/{p.relative_to(art).as_posix()}"}
            for p in sorted(art.rglob("*")) if p.is_file()
        ]
        conn.push({"type": "artifacts", "items": items, "bundle_url": f"/api/bundle/{conn.owner}/{run_id}"})
        _remember_run(conn, run)
        conn.push({"type": "run_complete", "run_id": run_id})
        note = "报告已按指示更新并重新生成" if instr else "报告已用已有结果重新生成"
        conn.push({"type": "chat_token", "token": f"✅ {note}(run {run_id})。"})
        conn.push({"type": "chat_done"})
    except Exception as exc:  # noqa: BLE001 - a regenerate failure is reported, never fatal
        print(f"[regen] failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        conn.emit("error", "lab", f"Report regenerate failed: {exc}", detail=error_detail(exc))
        conn.push({"type": "chat_error", "message": str(exc)})
    finally:
        conn.chat_running = False
        conn.end_run(run)


def _finish_run_record(conn: Connection, run_id: str | None, status: str,
                       artifacts_path: str | None, summary: str | None) -> None:
    """Best-effort: mark this user's Run row finished (status/artifacts/summary)."""
    if not (_AUTH_ENABLED and conn.app_user_id and run_id):
        return
    try:
        auth_routes.record_run_finish(run_id, status, artifacts_path, summary)
    except Exception as exc:  # noqa: BLE001 - history is best-effort, never fatal
        print(f"[auth] record_run_finish failed: {exc}")


def _promote_doc_title(md: str, fallback: str) -> tuple[str, str]:
    """Pull a CONTENT-derived document title from the report's first top-level ``# ``
    heading, so the rendered PDF/DOCX is titled by its main finding instead of a generic
    constant. Returns ``(title, body_without_that_heading)`` — the H1 line (and one
    trailing blank) is removed so pandoc's title metadata doesn't duplicate it. If there
    is no usable H1, returns ``(fallback, md)`` unchanged (nothing is lost). Pure → testable."""
    lines = md.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("# ") and not s.startswith("## "):
            title = s[2:].strip()
            if not title:
                break
            del lines[i]
            if i < len(lines) and not lines[i].strip():
                del lines[i]
            return title, "\n".join(lines)
    return fallback, md


def _assemble_report_md(synthesis: str, art: Path) -> str:
    """Append a deterministic gallery of the run's figures + a table index to the PI's
    synthesis. Image paths are RELATIVE to the artifacts dir (pandoc renders with that
    as cwd), so the PDF embeds the scanpy/gseapy PNGs the analysis tools emitted."""
    parts = [synthesis.strip() or "_(no synthesis text was produced.)_"]

    figs = sorted((art / "figures").glob("*.png")) if (art / "figures").exists() else []
    if figs:
        parts.append("\n\n# Figures\n")
        for f in figs:
            caption = f.stem.replace("_", " ").strip().capitalize()
            parts.append(f"\n**{caption}**\n\n![{caption}](figures/{f.name}){{width=85%}}\n")

    tables = sorted((art / "tables").glob("*.csv")) if (art / "tables").exists() else []
    if tables:
        parts.append("\n\n# Data tables\n\nThe following result tables are included in the bundle:\n")
        parts.extend(f"\n- `tables/{t.name}`" for t in tables)
    return "".join(parts) + "\n"


# The manuscript/tech-report prompts are ROUTED BY TASK KIND: a variant-annotation run must NOT be
# told to write scRNA-seq Methods (HVG/PCA/UMAP/clustering/DE), which is what forced the earlier
# report to cram VCF results into scanpy stage names. Only the domain framing, the Results-subsection
# examples, and the Methods stage scaffold change; every honesty/table/figure rule is shared.
def _manuscript_domain(kind: str) -> str:
    if kind == "variant":
        return (
            "You are the Principal Investigator writing the FINAL clinical-genomics VARIANT ANNOTATION "
            "study as a PUBLICATION-READY MANUSCRIPT in GitHub-flavored Markdown. Mirror the structure "
            "and Methods granularity of a real variant-interpretation / clinical-genetics article (e.g. "
            "an ACMG/AMP variant-classification or a VEP/ClinVar cohort-annotation paper): a narrative "
            "Results section backed by tables and figures, then a Methods section broken into "
            "fine-grained, parameter-level steps.")
    return (
        "You are the Principal Investigator writing the FINAL single-cell RNA-seq study as a "
        "PUBLICATION-READY MANUSCRIPT in GitHub-flavored Markdown. Mirror the structure and Methods "
        "granularity of a real journal article — model it on a peer-reviewed scRNA-seq atlas paper such "
        "as Menon et al. 2019 (Nat Commun 10:4902, 'Single-cell transcriptomic atlas of the human "
        "retina'): a narrative Results section backed by figures, then a Methods section broken into "
        "fine-grained, parameter-level steps.")


def _manuscript_results_hint(kind: str) -> str:
    if kind == "variant":
        return ("such as '### Variant distribution and functional consequences', '### ClinVar clinical "
                "classification', '### Rare / high-priority candidate variants'")
    return ("such as '### Quality control', '### Cell clustering', '### Differential expression', "
            "'### Pathway enrichment'")


def _manuscript_methods_scaffold(kind: str) -> str:
    if kind == "variant":
        return (
            "Cover, in order, every stage that ran: input VCF (assembly, variant/record count, source), "
            "QC / FILTER (PASS filtering — state the REAL PASS vs non-PASS counts from the authoritative "
            "counts, NEVER 'all pass' unless non-PASS is 0), VEP annotation (assembly GRCh38/GRCh37, "
            "offline cache vs REST, and any variant cap/scope), functional consequence classification, "
            "predicted impact (MODIFIER/LOW/MODERATE/HIGH), ClinVar clinical-significance extraction "
            "(Pathogenic / Likely_Pathogenic), population-frequency rarity (gnomAD AF threshold), "
            "in-silico deleteriousness (SIFT / PolyPhen), and clinical shortlisting (rare + high-impact "
            "or deleterious, not in ClinVar). This is a VARIANT-ANNOTATION study — do NOT describe "
            "single-cell steps (HVG selection, PCA, neighbor graph, UMAP, clustering, differential "
            "expression) or map variant results onto them.")
    return (
        "Cover, in order, every stage that ran: dataset, QC, HVG selection, normalization, "
        "dimensionality reduction (PCA n_comps), neighbor graph (n_neighbors), clustering (algorithm + "
        "resolution + #clusters), UMAP, reference-based annotation (e.g. scGPT, if it produced output), "
        "differential expression (test + top-N), enrichment (databases).")


def _manuscript_annotation_rule(kind: str) -> str:
    # scGPT reference-based annotation only applies to the single-cell path.
    if kind == "variant":
        return ""
    return (
        "- USE the data/ annotation artifacts as REAL results: if a scGPT (or other reference-based) "
        "predictions summary is provided, report the cell-type distribution and confidence as a genuine "
        "finding (a dedicated '### Cell-type annotation' Results subsection), and align it with the "
        "marker/cluster evidence. The presence of a predictions summary means that tool SUCCEEDED — do "
        "NOT describe it as failed or as a fallback.\n")


def _report_writer_system(kind: str = "single_cell") -> str:
    return (
        f"{_manuscript_domain(kind)} The manuscript MUST be complete — every section below is "
        "present and substantive, with NO empty or placeholder sections. Use Markdown headings only "
        "(do NOT prefix them with your own '1.'/'2.' numbers):\n"
        "  # <a concise, informative title summarizing the main finding>\n"
        "  ## Abstract  (ONE paragraph: background, methods, key quantitative results, conclusion)\n"
        "  ## Introduction  (biological background and the study objective / question)\n"
        "  ## Results  (core findings as flowing scientific prose; organize into labelled subsections "
        f"{_manuscript_results_hint(kind)}; lead each subsection with its take-home message; reference "
        "figures to carry the data and keep any inline table small — see the table rule below)\n"
        "  ## Discussion  (interpretation, biological significance, relation to known biology)\n"
        "  ## Limitations  (caveats stated honestly, as a short bulleted or numbered list)\n"
        "  ## Conclusion  (3–4 sentences: what was done, the principal finding, and the next step — "
        "this section is REQUIRED and must not be omitted)\n"
        "  ## Methods  (REQUIRED format, biology-paper style: this section MUST be itemized — NEVER a "
        "running paragraph. Use a NUMBERED LIST where each item is one pipeline stage with its explicit "
        "parameters and outcome — e.g. '1. **Quality control.** min_genes=200/cell, min_cells=3/gene, "
        "max mito%=20; N cells removed, M genes retained.' Detail may vary per stage (some terse, some "
        "rich) but every stage IS its own numbered item. When a stage has sub-steps, use multi-level "
        "numbering or sub-headings — go to 1.1/1.2 or deeper where it aids clarity. "
        f"{_manuscript_methods_scaffold(kind)})\n"
        "  ## References  (ALWAYS include this section as the LAST section, even when empty — it is a "
        "reserved insertion point for the literature module. List only real DOI/PMID-backed citations; "
        "if there are none yet, write the single line '*Citations to be inserted by the literature "
        "module (PaperQA).*' and nothing else. NEVER fabricate a citation, DOI, or PMID.)\n\n"
        "Write in formal scientific prose — past tense for what was done, present tense for established "
        "facts. Hard rules:\n"
        "- Ground EVERY number, gene symbol and statistic ONLY in the provided synthesis and table "
        "previews. Never invent values; if something isn't provided, describe it qualitatively.\n"
        "- AUTHORITATIVE COUNTS: when an 'AUTHORITATIVE COUNTS' block is provided below, every count "
        "you state (variant/record totals, PASS vs non-PASS, n pathogenic, n high-priority, n rare, "
        "n citations) MUST match it EXACTLY — do not compute, infer, round, or restate a different "
        "number, and do not describe a count the block reports as 0 as if it were non-zero.\n"
        "- HONESTY about failed/substituted steps: if the synthesis indicates a planned tool failed or "
        "was substituted (e.g. a reference-based / foundation-model annotation step that did not run), "
        "state this plainly in Methods and Limitations — name the intended method, that it did not run, "
        "and the fallback that was used. Do NOT present a fallback as if it were the original plan.\n"
        "- STAY ON THE ACTUAL QUESTION: address the research question and dataset exactly as provided. "
        "NEVER restate, broaden, or replace the question with a different topic (e.g. do not turn a "
        "variant-annotation study into a single-cell / spatial study because the cited literature is "
        "about that) — the title, Abstract and Introduction must be about the real question.\n"
        "- NO FABRICATION TO FILL SECTIONS: never describe the pipeline as a 'framework' / 'scaffold' / "
        "'parameterized for future deployment', and never claim results were 'deferred' or 'planned for "
        "future work', UNLESS the provided synthesis explicitly says so. Never present an intended-but-"
        "unexecuted method as if it produced findings. If a planned analysis did not run or produced no "
        "result, the Results section states that plainly rather than narrating what it 'would' show.\n"
        "- RUN STATUS: when a 'RUN STATUS' block is provided below indicating the analysis did not fully "
        "complete, HONOUR it — report only the genuine results, state the incompleteness in Results and "
        "Limitations, and keep the manuscript brief. A short truthful report is REQUIRED and OVERRIDES "
        "the 'every section substantive' expectation above (each section is still PRESENT, but do not pad "
        "an empty section with speculative prose).\n"
        "- TABLE rule: PREFER the corresponding figure over a raw table. If a table is genuinely needed, "
        "keep it to AT MOST 5 rows and the few most informative columns; format every p-value / adjusted "
        "p-value in scientific notation with 2 significant figures (e.g. 1.6e-18); round scores to "
        "integers; NEVER paste 10+ digit numbers or full-precision floats. Drop redundant tables whose "
        "data is already shown in a referenced figure.\n"
        "- FIGURE CAPTIONS (required): every embedded figure MUST carry a numbered, descriptive caption "
        "via EXACTLY this form: ![Figure N. <what the panel shows, and what the axes / colours / legend "
        "represent>](figures/NAME.png) with a path from the provided figure list. Number figures "
        "sequentially (Figure 1, Figure 2, …) and refer to them by number in the prose. Do NOT embed a "
        "figure with an empty or one-word caption, and do NOT leave a stray '[Figure N. …]' text line "
        "next to the image. Show the representative figures; the rest stay in the Output Files Index.\n"
        f"{_manuscript_annotation_rule(kind)}"
        "- References: put real DOI/PMID-backed citations in the required '## References' section only; "
        "NEVER invent or fabricate a citation, DOI, or PMID (see the References section spec above). "
        "In the manuscript body, discuss literature in prose with author-year mentions only. Do NOT "
        "write bibliography-style bullet blocks such as 'Title:', 'Authors:', or 'DOI/PMID:' outside "
        "the final References section; full bibliographic metadata belongs only in '## References'. "
        "Do NOT write 'see Figure(s)' callouts in Literature Review / literature-context prose unless "
        "the cited figure is an actual generated analysis figure and the sentence is discussing that "
        "figure's data, not a literature citation.\n"
        "- Do NOT write an 'Output Files Index' section yourself — it is appended automatically."
    )


def _fmt_num(c: str) -> str:
    """Compact a numeric cell so it never reaches the report as a 10–17 digit token
    (those overflow PDF table columns). p-values / very small or very large magnitudes →
    scientific notation with 2 sig figs (``1.6e-18``); moderate floats → ≤3 decimals,
    trailing zeros stripped. Non-numeric strings pass through untouched."""
    try:
        f = float(c)
    except (TypeError, ValueError):
        return c
    if f == 0:
        return "0"
    a = abs(f)
    if a < 1e-3 or a >= 1e5:
        return f"{f:.1e}"
    s = f"{f:.3f}".rstrip("0").rstrip(".")
    return s or "0"


def _csv_preview_md(path: Path, *, max_rows: int = 8, max_cols: int = 8, cell: int = 40) -> str:
    """Render the head of a CSV as a Markdown table (bounded rows/cols/cell width), with
    numeric cells compacted (see :func:`_fmt_num`) so wide stat tables stay legible in the
    rendered PDF instead of overprinting neighbouring columns."""
    import csv

    rows: list[list[str]] = []
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            for i, row in enumerate(csv.reader(fh)):
                if i > max_rows:
                    break
                rows.append(row[:max_cols])
    except Exception:  # noqa: BLE001 - a bad CSV just gets skipped from the preview
        return ""
    if len(rows) < 2:
        return ""

    def fmt(c: str) -> str:
        c = _fmt_num((c or "").replace("|", "\\|").replace("\n", " ").strip())
        return c if len(c) <= cell else c[: cell - 1] + "…"

    header = rows[0]
    width = len(header)
    lines = ["| " + " | ".join(fmt(c) for c in header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    for r in rows[1:]:
        r = (list(r) + [""] * width)[:width]
        lines.append("| " + " | ".join(fmt(c) for c in r) + " |")
    return "\n".join(lines)


def _scgpt_label_summary_md(path: Path, *, top: int = 15) -> str:
    """Compact label-distribution table from a scGPT predictions CSV (index,predictions,
    confidence). This is the KEY scGPT result; it lands in ``data/`` (not ``tables/``), so
    without this it was invisible to the report writer and the model invented a 'fallback'."""
    import csv
    import collections

    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            rows = list(csv.DictReader(fh))
    except Exception:  # noqa: BLE001 - a bad CSV is just skipped
        return ""
    if not rows:
        return ""
    col = next((c for c in rows[0] if c.lower() in ("predictions", "prediction", "cell_type", "label")), None)
    if not col:
        return ""
    counts = collections.Counter((r.get(col) or "").strip() or "unknown" for r in rows)
    total = sum(counts.values())
    conf_col = next((c for c in rows[0] if "conf" in c.lower()), None)
    lines = ["| Cell type | n cells | % |", "| --- | --- | --- |"]
    for label, n in counts.most_common(top):
        lines.append(f"| {label} | {n} | {100 * n / total:.1f} |")
    if len(counts) > top:
        lines.append(f"| (+{len(counts) - top} more types) | … | … |")
    head = f"scGPT reference-based annotation — {total} cells, {len(counts)} cell types"
    if conf_col:
        try:
            confs = [float(r[conf_col]) for r in rows if r.get(conf_col)]
            if confs:
                head += f"; mean confidence {sum(confs) / len(confs):.2f}"
        except (ValueError, TypeError):
            pass
    return f"**{head}.**\n\n" + "\n".join(lines)


def _data_artifacts_block(art: Path) -> str:
    """Surface ``data/`` artifacts to the report writer. scGPT predictions get a label-
    distribution summary; other small data CSVs get a head preview. Returns '' if none."""
    data_dir = art / "data"
    if not data_dir.exists():
        return ""
    blocks: list[str] = []
    pred = data_dir / "scgpt_predictions.csv"
    if pred.exists():
        summ = _scgpt_label_summary_md(pred)
        if summ:
            blocks.append(f"### data/scgpt_predictions.csv\n{summ}")
    for c in sorted(data_dir.glob("*.csv")):
        if c.name == "scgpt_predictions.csv" or c.stat().st_size > 200_000:
            continue
        md = _csv_preview_md(c)
        if md:
            blocks.append(f"### data/{c.name}\n{md}")
    return "\n\n".join(blocks)


def _annotate_variants_result(result: Any) -> "dict[str, Any] | None":
    """The AUTHORITATIVE ``annotate_variants`` tool RESULT from the run — the tool's own numbers
    (n_pass/n_nonpass, n_pathogenic, n_high_priority, truncated, distributions), NOT the model's
    prose or a model-written summary JSON. Returns the last ok annotate_variants result, or None
    (⇒ not a variant run). This is what binds the report's counts to ground truth."""
    best: "dict[str, Any] | None" = None
    for r in getattr(result, "rounds", []) or []:
        rd = r.to_dict() if hasattr(r, "to_dict") else r
        sr = (rd.get("scientist_result") if isinstance(rd, dict) else {}) or {}
        for step in sr.get("steps") or []:
            if not isinstance(step, dict) or step.get("tool") != "annotate_variants":
                continue
            res = step.get("result")
            if isinstance(res, dict) and res.get("status") == "ok" and "by_consequence" in res:
                best = res
    return best


def _accepted_citation_count(art: Path) -> "int | None":
    """The COUNT of accepted citations from process/literature_references.json (written before the
    technical report). None if the file isn't there yet (e.g. during the manuscript pass)."""
    p = art / "process" / "literature_references.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cites = data.get("citations")
    return len(cites) if isinstance(cites, list) else None


def _report_task_kind(art: Path, result: Any = None) -> str:
    """'variant' for a VCF/VEP annotation run, else 'single_cell'. Deterministic: prefer the
    authoritative annotate_variants tool result; fall back to variant artifacts on disk."""
    if result is not None and _annotate_variants_result(result) is not None:
        return "variant"
    data = art / "data"
    if (data / "variant_annotation_summary.json").exists() or (data / "annotated_results_summary.json").exists():
        return "variant"
    tdir = art / "tables"
    if tdir.exists() and (list(tdir.glob("*variant*")) or list(tdir.glob("*clinvar*"))):
        return "variant"
    return "single_cell"


def _variant_filter_summary_facts(art: Path) -> str:
    """AUTHORITATIVE counts from the deterministic PASS-filter pass (``variant_filter_summary.json``) —
    the ONE real result a variant run still produces when VEP annotation itself failed. Lets the
    manuscript report the genuine variant / PASS landscape (and state that annotation did not complete)
    instead of having no real numbers and fabricating a 'framework'. '' when the file is absent/unreadable."""
    try:
        data = json.loads((art / "tables" / "variant_filter_summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    total, npass, nnon = data.get("total_variants"), data.get("n_pass"), data.get("n_nonpass")
    if total is None and npass is None:
        return ""
    lines = ["AUTHORITATIVE COUNTS (from the variant-calling PASS filter — state these EXACT numbers; "
             "do NOT invent or restate different values):"]
    if total is not None:
        lines.append(f"- total variant records: {total}")
    if npass is not None:
        allpass = " (all records PASS)" if not nnon else ""
        lines.append(f"- FILTER: {npass} PASS, {nnon or 0} non-PASS{allpass}")
    lines.append("- per-variant functional annotation (VEP consequence / gene / impact / ClinVar): NOT "
                 "produced this run — the annotation step did not complete, so there are NO "
                 "consequence / impact / ClinVar / shortlist counts. Report the PASS landscape above and "
                 "state that annotation did not complete; do NOT report annotation results that do not exist.")
    return "\n".join(lines)


def _variant_facts_block(art: Path, result: Any) -> str:
    """A deterministic 'AUTHORITATIVE COUNTS' block for a variant run, built from the annotate_variants
    tool result (+ the accepted-citation count). Injected into the report prompts so the model cannot
    fabricate numbers — this is what kills the '165 high-priority' / '6 vs 12 citations' hallucinations.
    When annotation itself failed, falls back to the PASS-filter summary so the real variant/PASS counts
    still reach the report. Returns '' for a non-variant run with no filter summary either."""
    res = _annotate_variants_result(result)
    if not res:
        return _variant_filter_summary_facts(art)
    lines = ["AUTHORITATIVE COUNTS (computed by the tools — state these EXACT numbers; do NOT invent, "
             "infer, or restate different values):"]
    lines.append(f"- variants annotated: {res.get('n_input_variants', '?')}")
    if "n_pass" in res:
        npass, nnon = res.get("n_pass"), res.get("n_nonpass")
        allpass = " (all records PASS)" if not nnon else ""
        lines.append(f"- FILTER: {npass} PASS, {nnon} non-PASS{allpass} — do NOT write 'all PASS' when "
                     f"non-PASS > 0")
    if res.get("truncated"):
        lines.append(f"- TRUNCATED: only the first {res.get('n_annotated', res.get('n_input_variants'))} "
                     f"variants were annotated (cap reached); the counts describe that slice, not the "
                     f"whole VCF — report this as a limitation")
    lines.append(f"- ClinVar Pathogenic/Likely-Pathogenic: {res.get('n_pathogenic', 0)}")
    lines.append(f"- high-priority shortlist: {res.get('n_high_priority', 0)}")
    lines.append(f"- rare variants (below AF cutoff): {res.get('n_rare', '?')}")
    bc = res.get("by_clinical_significance") or {}
    if bc:
        lines.append("- clinical-significance distribution: " + ", ".join(f"{k}={v}" for k, v in bc.items()))
    n_cit = _accepted_citation_count(art)
    if n_cit is not None:
        lines.append(f"- literature citations: {n_cit} (use exactly this many; do not state a different count)")
    return "\n".join(lines)


def _incomplete_step_topics(result: Any) -> list[str]:
    """High-level TOPICS of planned agenda steps whose last round did not pass critique / did not
    finish — for the manuscript's honest RUN STATUS note. Topics only (first clause, truncated); the
    tool-error detail stays in the technical report's Diagnostics, per the silent-degradation design."""
    rounds = getattr(result, "rounds", []) or []
    last_by_step: dict[Any, dict] = {}
    for r in rounds:
        rd = r.to_dict() if hasattr(r, "to_dict") else r
        if isinstance(rd, dict):
            last_by_step[rd.get("step_index")] = rd
    topics: list[str] = []
    seen: set[str] = set()   # dedup by topic text — a step retried across N rounds is ONE failed topic
    for idx in sorted((k for k in last_by_step if k is not None), key=lambda x: (str(type(x)), x)):
        rd = last_by_step[idx]
        sr = rd.get("scientist_result", {}) or {}
        v = rd.get("verdict", {}) or {}
        if sr.get("status") == "incomplete" or sr.get("stop_reason") == "max_steps" \
                or str(v.get("verdict")) == "revise":
            topic = str(rd.get("step", "")).split(",")[0].split(" so that")[0].strip()[:100]
            if topic and topic not in seen:
                seen.add(topic)
                topics.append(topic)
    return topics


def _manuscript_run_status_block(result: Any) -> str:
    """Honest, manuscript-level run-status note — emitted ONLY when the run did NOT converge. It tells
    the writer the analysis did not fully complete and which planned analyses produced no usable
    results, so the manuscript reports the genuine results truthfully instead of fabricating a
    'framework' to fill the mandatory sections. Returns '' for a converged run — the clean manuscript
    is unchanged, so the silent-degradation design (per-step degradations stay in the technical report)
    is preserved for every normal run."""
    if result is None or getattr(result, "converged", True):
        return ""
    accepted = getattr(result, "accepted_steps", None)
    agenda = getattr(result, "agenda", None)
    total = len(agenda) if isinstance(agenda, (list, tuple)) else agenda
    count = (f" ({accepted}/{total} planned steps accepted)"
             if accepted is not None and total is not None else "")
    lines = [f"RUN STATUS — the planned analysis did NOT fully complete{count}."]
    topics = _incomplete_step_topics(result)
    if topics:
        lines.append("Planned analyses that produced NO usable results this run:")
        lines += [f"  - {t}" for t in topics[:8]]
    lines.append(
        "Write the manuscript HONESTLY about this: report ONLY results that genuinely exist in the "
        "counts / synthesis / tables below. If a planned analysis produced no results, say so plainly in "
        "Results and Limitations and move on. Do NOT fabricate findings, do NOT describe intended-but-"
        "unexecuted methods as if they ran, and do NOT invent a 'framework' / 'scaffold' / 'parameterized "
        "for future deployment' narrative to fill space. A brief, truthful manuscript is REQUIRED over a "
        "long speculative one.")
    return "\n".join(lines)


def _build_report(synthesis: str, art: Path, complete_fn, question: str, result: Any = None) -> str:
    """Write a Ddx41-style report: hand the model the figure inventory + previews of the
    smallest (usually summary) result tables, let it write a structured report with inline
    tables and contextual figure references, then append an authoritative Output Files Index
    so nothing is lost. Falls back to the deterministic gallery if the LLM step fails.

    Routed by task kind: a variant-annotation run gets variant Methods/framing (not scanpy) and an
    authoritative-counts block so the model cannot fabricate variant numbers."""
    figs = sorted((art / "figures").glob("*.png")) if (art / "figures").exists() else []
    tables = sorted((art / "tables").glob("*.csv")) if (art / "tables").exists() else []

    kind = _report_task_kind(art, result)
    facts = _variant_facts_block(art, result)
    facts_block = f"{facts}\n\n" if facts else ""
    run_status = _manuscript_run_status_block(result)
    run_status_block = f"{run_status}\n\n" if run_status else ""

    fig_list = "\n".join(f"- figures/{f.name} — {f.stem.replace('_', ' ').strip()}" for f in figs) or "(none)"
    # Preview the smallest tables first — summary/overview tables tend to be small, while
    # per-cluster dumps are large; cap the count + size so the prompt stays bounded.
    preview_tables = sorted((t for t in tables if t.stat().st_size <= 200_000),
                            key=lambda t: t.stat().st_size)[:10]
    previews: list[str] = []
    for t in preview_tables:
        md = _csv_preview_md(t)
        if md:
            previews.append(f"### tables/{t.name}\n{md}")
    previews_block = "\n\n".join(previews) or "(no small tables to preview)"
    # data/ artifacts (e.g. scGPT predictions) — these never appear in tables/, so surface
    # them explicitly or the model can't see a successful annotation result.
    data_block = _data_artifacts_block(art) or "(no data/ artifacts to surface)"

    try:
        body = complete_fn([
            {"role": "system", "content": _report_writer_system(kind)},
            {"role": "user", "content": (
                f"Research question:\n{question}\n\n"
                f"{facts_block}"
                f"{run_status_block}"
                f"Grounded synthesis from the accepted analysis steps (use ONLY these facts):\n"
                f"{synthesis.strip() or '(no synthesis text was produced)'}\n\n"
                f"Figures available (reference inline by the exact path shown):\n{fig_list}\n\n"
                f"Data-table previews (embed the relevant ones as Markdown tables):\n{previews_block}\n\n"
                f"Model/annotation artifacts from data/ (report these as real results, NOT as a "
                f"failed/fallback step):\n{data_block}\n\n"
                "Write the full report now."
            )},
        ])
    except Exception as exc:  # noqa: BLE001 - never lose the run over a report-writer hiccup
        print(f"[lab] report writer failed ({exc}); falling back to deterministic gallery.")
        return _assemble_report_md(synthesis, art)

    if not (body and body.strip()):
        return _assemble_report_md(synthesis, art)

    return _append_output_index(_ensure_references(body.strip()), figs, tables)


_REFERENCES_PLACEHOLDER = "*Citations to be inserted by the literature module (PaperQA).*"


def _ensure_references(body: str) -> str:
    """Guarantee the manuscript ends with a reserved '## References' section — the slot the
    literature module (PaperQA) fills later. If the writer already emitted one, leave it;
    otherwise append the placeholder so the section always exists (and is never fabricated)."""
    import re

    if re.search(r"(?mi)^#{1,6}\s+references\b", body):
        return body
    return body.rstrip() + f"\n\n## References\n\n{_REFERENCES_PLACEHOLDER}\n"


def _append_output_index(body: str, figs: list[Path], tables: list[Path]) -> str:
    """Append an authoritative Output Files Index so every figure/table is listed even if
    the model referenced only a subset inline."""
    parts = [body, "\n\n# Output Files Index\n"]
    if figs:
        parts.append("\n**Figures** (`figures/`):\n")
        parts.extend(f"\n- `figures/{f.name}`" for f in figs)
    if tables:
        parts.append("\n\n**Data tables** (`tables/`):\n")
        parts.extend(f"\n- `tables/{t.name}`" for t in tables)
    return "".join(parts) + "\n"


def _tech_methods_scaffold(kind: str) -> str:
    if kind == "variant":
        return ("input VCF (assembly, record count), FILTER/PASS counts (real n_pass / n_nonpass), VEP "
                "annotation (assembly, offline-cache vs REST, cap/scope), consequence & impact "
                "classification, ClinVar significance source, gnomAD AF rarity threshold, SIFT/PolyPhen, "
                "clinical shortlisting criteria — NOT single-cell stages (HVG/PCA/UMAP/clustering/DE)")
    return ("QC thresholds, HVG n, PCA n_comps, n_neighbors, clustering algorithm/resolution/#clusters, "
            "UMAP, DE test/top-N, enrichment databases")


def _tech_report_writer_system(kind: str = "single_cell") -> str:
    run_kind = "variant-annotation" if kind == "variant" else "single-cell RNA-seq"
    return (
        f"You are a computational biologist writing the INTERNAL TECHNICAL REPORT for a {run_kind} "
        "run — an engineering/analysis document for the team, NOT a journal manuscript. Its job "
        "is completeness and honesty: record exactly what the pipeline did, with all parameters, every "
        "tool output, and — critically — every step that FAILED, was retried, or was substituted, "
        "including the failure reason. Write in clear technical prose + lists/tables. Use these sections "
        "(Markdown headings, no manual '1.'/'2.' prefixes):\n"
        "  # Technical Report — <dataset / question in a few words>\n"
        "  ## Run summary  (the question, the planned agenda, and a one-line PASS/FAIL per step)\n"
        "  ## Pipeline execution log  (one '### Step N — <name>' subsection per agenda step: the tool(s) "
        "invoked, key parameters, the outcome, the Critic verdict + score, and for any failed/revised "
        "step the VERBATIM error and what was done about it)\n"
        "  ## Methods & parameters  (a NUMBERED list: each pipeline stage with its explicit parameters "
        f"and resulting counts — {_tech_methods_scaffold(kind)})\n"
        "  ## Results & artifacts  (the quantitative results grounded in the tool outputs; reference the "
        "figures and embed COMPACT tables — see the table rule)\n"
        "  ## Diagnostics & failures  (a frank account of what did not work this run — e.g. a "
        "foundation-model / reference annotation step that failed on the scheduler, guard-blocked steps, "
        "missing cross-validation — with the concrete error and the downstream impact on the report)\n"
        "  ## Reproducibility & next steps  (what to change to make the failed steps succeed on a rerun)\n\n"
        "Hard rules:\n"
        "- Ground EVERY number, parameter, gene symbol and error string ONLY in the provided run log and "
        "table previews. Never invent values. Quote error strings verbatim.\n"
        "- AUTHORITATIVE COUNTS: when an 'AUTHORITATIVE COUNTS' block is provided below, every count you "
        "state (record/variant totals, PASS vs non-PASS, n pathogenic, n high-priority, n rare, n "
        "citations) MUST match it EXACTLY — never compute, infer, or restate a different number, and "
        "never report a count the block gives as 0 as a non-zero value (e.g. do NOT invent a "
        "high-priority count).\n"
        "- Be explicit about failures: name the intended tool, the verbatim error, the retry count, and "
        "the substitution. Do NOT gloss over a failed step or present a fallback as the original plan.\n"
        "- TABLE rule: format every p-value / adjusted p-value in scientific notation with 2 significant "
        "figures (e.g. 1.6e-18); round scores to integers; keep tables to the most informative rows; "
        "NEVER paste 10+ digit numbers or full-precision floats.\n"
        "- Reference figures inline as ![caption](figures/NAME.png) using a path from the provided list.\n"
        "- Do NOT write an 'Output Files Index' section yourself — it is appended automatically."
    )


def _run_log_digest(result: Any, *, max_answer: int = 1200, max_err: int = 600) -> str:
    """Compact per-round execution log (ALL rounds, incl. failed/revised ones) for the
    technical-report writer — so failures like a scheduler-killed scGPT job are surfaced,
    not dropped the way the accepted-only manuscript synthesis drops them."""
    lines: list[str] = []
    for r in getattr(result, "rounds", []) or []:
        rd = r.to_dict() if hasattr(r, "to_dict") else r
        v = rd.get("verdict", {}) or {}
        sr = rd.get("scientist_result", {}) or {}
        lines.append(
            f"### Step {rd.get('step_index')} — {rd.get('step', '?')}\n"
            f"- specialist: {rd.get('specialist', '?')}\n"
            f"- verdict: {v.get('verdict', '?')} (score {v.get('score', '?')})\n"
            f"- critique: {str(v.get('critique', '')).strip()[:max_err]}"
        )
        for e in sr.get("errors", []) or []:
            lines.append(f"- ERROR [{e.get('tool', '?')}]: {str(e.get('error', '')).strip()[:max_err]}")
        # Per-step tool failures live in steps[].result (returncode/error) — the top-level
        # ``errors`` list is often empty even when a run_code snippet OOM-died, so scan the steps.
        for tool, msg in _step_failures(sr):
            lines.append(f"- FAILED [{tool}]: {msg[:max_err]}")
        for line in _step_provenance(sr):
            lines.append(line)
        ans = str(sr.get("final_answer") or "").strip()
        if ans:
            lines.append(f"- scientist write-up: {ans[:max_answer]}")
        lines.append("")
    return "\n".join(lines) or "(no rounds recorded)"


def _step_provenance(scientist_result: dict) -> list[str]:
    """Compact per-``run_code`` reproducibility lines from ``steps[].result['provenance']``
    (attached at the run_code choke point). One line per code execution so the technical
    report's log carries the seed / code hash / git SHA / dataset hash that reproduce it."""
    out: list[str] = []
    for st in scientist_result.get("steps", []) or []:
        if not isinstance(st, dict):
            continue
        res = st.get("result") if isinstance(st.get("result"), dict) else {}
        pv = res.get("provenance") if isinstance(res, dict) else None
        if not isinstance(pv, dict):
            continue
        seed = pv.get("seed")
        data = pv.get("dataset_sha256") or pv.get("dataset_fingerprint")
        parts = [
            f"code {str(pv.get('code_sha256', ''))[:12]}",
            (f"seed {seed}" if seed is not None else "seed off"),
            f"git {str(pv.get('git_sha') or '?')[:7]}",
            f"data {pv.get('dataset_hash_method', '?')}:{str(data or '?')[:8]}",
            f"{pv.get('execution_mode', '?')} rc={pv.get('returncode')}",
            f"{pv.get('duration_ms', '?')}ms",
        ]
        out.append(f"- repro [{st.get('tool', 'run_code')}]: " + " · ".join(parts))
    return out


def _step_failures(scientist_result: dict) -> list[tuple[str, str]]:
    """Extract ``(tool, message)`` for every failed tool call inside a round — a non-zero
    returncode, a ``status == "error"``, or an explicit ``error`` field. This is where the real
    run_code failures hide (returncode -9 OOM kills, tracebacks); the round's top-level ``errors``
    list does not capture them. Returns [] for a clean round."""
    out: list[tuple[str, str]] = []
    for st in scientist_result.get("steps", []) or []:
        if not isinstance(st, dict):
            continue
        res = st.get("result", {}) if isinstance(st.get("result"), dict) else {}
        rc = res.get("returncode")
        failed = res.get("status") == "error" or bool(res.get("error")) or (isinstance(rc, int) and rc != 0)
        if not failed:
            continue
        tool = str(st.get("tool", "?"))
        detail = str(res.get("error") or "").strip()
        msg = (detail.splitlines()[-1] if detail else f"returncode {rc}")
        if isinstance(rc, int) and rc == -9:
            msg += " [SIGKILL — OUT_OF_MEMORY]"
        out.append((tool, msg))
    return out


def _summarize_pipeline_degradations(result: Any) -> str:
    """First-class list of agenda steps that DEGRADED — ran out of the step budget, were accepted
    without passing critique, or hit tool failures (incl. OOM). Analogous to the literature
    ``degradation_note``: it is threaded ONLY into the technical report's Diagnostics section, so
    the polished manuscript stays silent while the engineering log stays honest. Returns "" when
    every step completed cleanly.

    Keyed by the LAST round per agenda step (the round that determined the outcome), so a failure
    that a later retry recovered is not double-counted as a delivered degradation."""
    rounds = getattr(result, "rounds", []) or []
    last_by_step: dict[Any, dict] = {}
    for r in rounds:
        rd = r.to_dict() if hasattr(r, "to_dict") else r
        last_by_step[rd.get("step_index")] = rd   # later rounds overwrite → keep the last per step

    notes: list[str] = []
    for idx in sorted((k for k in last_by_step if k is not None), key=lambda x: (str(type(x)), x)):
        rd = last_by_step[idx]
        sr = rd.get("scientist_result", {}) or {}
        v = rd.get("verdict", {}) or {}
        reasons: list[str] = []
        status, stop = sr.get("status"), sr.get("stop_reason")
        if status == "incomplete" or stop == "max_steps":
            reasons.append(f"did not finish within the step budget (status={status}, stop_reason={stop})")
        if str(v.get("verdict")) == "revise":
            reasons.append(f"accepted without passing critique (verdict=revise, score={v.get('score')})")
        fails = _step_failures(sr)
        if fails:
            shown = "; ".join(f"{t}: {m}" for t, m in fails[:6])
            more = f" (+{len(fails) - 6} more)" if len(fails) > 6 else ""
            reasons.append(f"{len(fails)} tool call(s) failed → {shown}{more}")
        if reasons:
            step = str(rd.get("step", "?"))[:90]
            notes.append(f"- **Step {idx} — {step}:** " + "; ".join(reasons))

    if not notes:
        return ""
    return (
        "**Pipeline steps that degraded** (ran out of the step budget, were accepted without "
        "passing critique, or hit tool errors incl. OOM). Recorded here ONLY — the academic "
        "manuscript renders the clean accepted results and does not mention these:\n"
        + "\n".join(notes)
    )


def _build_technical_report(result: Any, art: Path, complete_fn, question: str,
                            lit_note: str | None = None, render_diag: str | None = None) -> str:
    """Write the internal technical report from the FULL run log (every step, every failure)
    plus the figure/table inventory. Best-effort: returns a deterministic skeleton if the
    LLM step fails, so the run never breaks over the second report.

    ``lit_note`` is the literature module's degradation note (None on the clean remote path):
    when present it tells the writer that reference retrieval fell back / produced nothing, so
    the 'Diagnostics & failures' section records it — while the academic manuscript stays clean."""
    figs = sorted((art / "figures").glob("*.png")) if (art / "figures").exists() else []
    tables = sorted((art / "tables").glob("*.csv")) if (art / "tables").exists() else []
    fig_list = "\n".join(f"- figures/{f.name} — {f.stem.replace('_', ' ').strip()}" for f in figs) or "(none)"
    preview_tables = sorted((t for t in tables if t.stat().st_size <= 200_000),
                            key=lambda t: t.stat().st_size)[:12]
    previews = [f"### tables/{t.name}\n{md}" for t in preview_tables if (md := _csv_preview_md(t))]
    previews_block = "\n\n".join(previews) or "(no small tables to preview)"
    data_block = _data_artifacts_block(art) or "(no data/ artifacts to surface)"
    log = _run_log_digest(result)
    agenda = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(getattr(result, "agenda", []) or [])) or "(none)"
    # Literature retrieval degradation (fallback/empty) belongs in this report's Diagnostics
    # section — not in the manuscript. None on the clean remote path.
    lit_block = lit_note.strip() if lit_note else "(literature retrieval ran on the primary remote service — no degradation)"
    # Analysis-step degradations (max_steps, revise-accept, tool/OOM failures) — the same
    # tech-report-only channel as the literature note, so the manuscript stays silent.
    degradations = _summarize_pipeline_degradations(result)
    degr_block = degradations or "(all agenda steps completed cleanly — no degradation)"
    # Render-level layout defects the visual review could not fully fix (empty when clean/off) —
    # the same tech-report-only Diagnostics channel; the manuscript stays clean regardless.
    render_block = (render_diag or "").strip() or "(rendered layout passed visual review — no defects)"
    # Task-kind routing + authoritative counts: a variant run gets variant Methods (not scanpy) and a
    # facts block, so the tech report cannot invent numbers (the '165 high-priority' / '6 citations' bug).
    kind = _report_task_kind(art, result)
    facts = _variant_facts_block(art, result)
    facts_block = f"{facts}\n\n" if facts else ""

    try:
        body = complete_fn([
            {"role": "system", "content": _tech_report_writer_system(kind)},
            {"role": "user", "content": (
                f"Research question:\n{question}\n\n"
                f"{facts_block}"
                f"Planned agenda:\n{agenda}\n\n"
                f"Full per-step execution log (ground the report ONLY in these — includes failures):\n"
                f"{log}\n\n"
                f"Pipeline step degradations (report EACH of these in 'Diagnostics & failures' — "
                f"name the step, what degraded, and the downstream impact on the results):\n{degr_block}\n\n"
                f"Literature retrieval status (report this in 'Diagnostics & failures'):\n{lit_block}\n\n"
                f"Render-level review findings (include verbatim in 'Diagnostics & failures' if it "
                f"lists any defects):\n{render_block}\n\n"
                f"Figures available (reference inline by the exact path shown):\n{fig_list}\n\n"
                f"Data-table previews (embed COMPACT versions where relevant):\n{previews_block}\n\n"
                f"Model/annotation artifacts from data/ (report whether each tool actually produced "
                f"output — a predictions file present means that tool SUCCEEDED):\n{data_block}\n\n"
                "Write the full technical report now."
            )},
        ])
    except Exception as exc:  # noqa: BLE001 - never lose the run over the second report
        print(f"[lab] technical-report writer failed ({exc}); writing deterministic skeleton.")
        diag_parts = [p for p in (degradations, (lit_note.strip() if lit_note else ""),
                                  (render_diag.strip() if render_diag else "")) if p]
        diag = "\n\n## Diagnostics & failures\n\n" + "\n\n".join(diag_parts) + "\n" if diag_parts else ""
        body = (f"# Technical Report\n\n## Run summary\n\n{agenda}\n\n"
                f"## Pipeline execution log\n\n{log}\n{diag}")
    if not (body and body.strip()):
        body = f"# Technical Report\n\n## Pipeline execution log\n\n{log}\n"
    return _append_output_index(body.strip(), figs, tables)


def _report_review_system(kind: str = "single_cell") -> str:
  study = "variant-annotation" if kind == "variant" else "single-cell RNA-seq"
  return (
    f"You are a meticulous scientific editor reviewing a {study} MANUSCRIPT draft "
    "BEFORE it is rendered to PDF/DOCX. Return the corrected FULL Markdown, fixing:\n"
    "- Manuscript structure: it should read like a journal article with Title, Abstract, "
    "Introduction, Results, Discussion, Limitations, Conclusion, Methods, and References sections "
    "(References LAST). If a core section is missing or mislabelled, add/rename it. A Conclusion "
    "(3–4 sentences) is REQUIRED — add one if absent. The '## References' section has ALREADY been "
    "populated by the literature module (real DOI/PMID citations, or an honest 'none retrieved' "
    "line) — PRESERVE it VERBATIM: do not add, remove, reorder, reword, or fabricate any citation, "
    "and do not replace it with a placeholder. Methods MUST be an "
    "itemized NUMBERED list of pipeline stages each carrying its parameters (multi-level numbering "
    "like 4.1/4.2 is fine for sub-steps); if Methods is one running paragraph, restructure it into "
    "that numbered form WITHOUT inventing any parameter not present in the draft. Keep the prose "
    "formal and scientific.\n"
    "- Leftover/placeholder/draft content: TODO/FIXME, 'insert here', lorem, duplicated "
    "boilerplate, raw code-runner chatter or stdout dumps, notes-to-self — delete them (but KEEP the "
    "literature-module-populated References section verbatim, per the rule above).\n"
    "- Section numbering: use clean Markdown headings only; remove any manual '1.'/'2.' "
    "prefixes on SECTION headings (Methods step numbering inside the list is fine).\n"
    "- Figure captions: every ![...](figures/NAME.png) must use a path present in the provided "
    "figure list (fix or drop any that don't resolve) AND carry a numbered descriptive caption — "
    "'Figure N. <what it shows; axes/colours/legend>'. If a figure has an empty or one-word caption, "
    "write a proper one from the surrounding text; do NOT invent data not implied by the draft.\n"
    "- Data tables: result tables must be real Markdown tables, never bare filenames. Keep each "
    "table to AT MOST 5 rows and the most informative columns; rewrite any p-value or full-precision "
    "float into scientific notation with 2 significant figures (e.g. 1.6e-18) and round scores to "
    "integers; NEVER leave a 10+ digit number in a table. If a table merely duplicates data already "
    "shown in a referenced figure, drop the table and keep the figure.\n"
    "- Citations: KEEP real citations that carry a DOI or PMID (from the literature_search tool); "
    "DELETE any fabricated citation — one with no DOI/PMID, or not traceable to the findings — and "
    "drop a 'References' section that lists only such fabrications. Outside the final '## References' "
    "section, rewrite literature discussion as prose and remove bibliography-style metadata bullets "
    "such as 'Title:', 'Authors:', and 'DOI/PMID:'; those details belong only in References. Remove "
    "literature-section figure callouts such as '(see Figures 1-2 for cited figure references)' "
    "unless they refer to real generated analysis figures being discussed as data.\n"
    "- Numbers/gene symbols not grounded in the draft's own content: soften or remove.\n"
    "Do NOT add new scientific claims or invent data. If the draft is already clean, return "
    "it unchanged. Return ONLY the corrected Markdown — no commentary, no code fences."
)

_BODY_BIBLIO_METADATA_LINE = re.compile(
    r"(?i)^\s*(?:[-*+]\s+|[0-9]+[.)]\s+)?(?:\*\*)?\s*"
    r"(?:title|authors?|doi/pmid|doi|pmid)\s*:\s*(?:\*\*)?.*$"
)
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LITERATURE_FIGURE_PAREN_RE = re.compile(
    r"\s*\([^)]*\bfig(?:ure)?s?\.?\s*[\w\s,;:./–—-]*[^)]*\)",
    re.I,
)
_LITERATURE_FIGURE_PHRASE_RE = re.compile(
    r"\s*(?:,\s*)?(?:see|refer to)\s+fig(?:ure)?s?\.?\s*[\w\s,–—-]+(?:for\s+[^.;]+)?",
    re.I,
)


def _remove_body_bibliography_metadata(md: str) -> str:
    """Drop bibliography-style metadata bullets from narrative sections.

    The authoritative bibliography is reinserted after review. This cleanup only touches content
    before the final ``## References`` heading, so numbered References entries are preserved.
    """
    match = re.search(r"(?im)^##\s+references\b", md or "")
    body = md[:match.start()] if match else (md or "")
    refs = md[match.start():] if match else ""
    cleaned: list[str] = []
    for line in body.splitlines():
        if _BODY_BIBLIO_METADATA_LINE.match(line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).rstrip() + ("\n\n" + refs.lstrip() if refs else "")


def _remove_literature_figure_callouts(md: str) -> str:
    """Remove hallucinated figure callouts from literature-review prose only.

    Real analysis figure references in Results/Discussion are left alone. The common failure mode
    is the report writer adding text like ``(see Figures 1-2 for cited figure references)`` to a
    literature summary even though literature_search returns citations, not generated figures.
    """
    lines = (md or "").splitlines()
    out: list[str] = []
    in_literature = False
    literature_level = 0
    for line in lines:
        heading = _MD_HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip().lower()
            if in_literature and level <= literature_level:
                in_literature = False
            if "literature" in title and "references" not in title:
                in_literature = True
                literature_level = level
        if in_literature and not line.lstrip().startswith("!["):
            line = _LITERATURE_FIGURE_PAREN_RE.sub("", line)
            line = _LITERATURE_FIGURE_PHRASE_RE.sub("", line)
            line = re.sub(r"\s+([,.;:])", r"\1", line)
            line = re.sub(r" {2,}", " ", line).rstrip()
        out.append(line)
    return "\n".join(out).rstrip()


def _review_report(draft_md: str, art: Path, complete_fn, question: str) -> str:
    """Pre-render self-review: the model edits its OWN draft to strip leftover/placeholder
    content, fix numbering, validate figure refs and embed tables, returning clean Markdown.
    Best-effort — returns the original draft if the review fails or comes back degenerate."""
    figs = sorted((art / "figures").glob("*.png")) if (art / "figures").exists() else []
    fig_list = "\n".join(f"- figures/{f.name}" for f in figs) or "(none)"
    try:
        reviewed = complete_fn([
            {"role": "system", "content": _report_review_system(_report_task_kind(art))},
            {"role": "user", "content": (
                f"Research question:\n{question}\n\n"
                f"Valid figure paths (any ![](...) must be one of these):\n{fig_list}\n\n"
                f"Report draft to review and correct:\n\n{draft_md}"
            )},
        ])
    except Exception as exc:  # noqa: BLE001 - never lose the report over a review hiccup
        print(f"[lab] report self-review failed ({exc}); shipping the unreviewed draft.")
        return draft_md
    reviewed = (reviewed or "").strip()
    # Guard a degenerate review (model returned almost nothing) — keep the original draft.
    if len(reviewed) < min(200, len(draft_md) // 2):
        return draft_md
    return reviewed


def _review_and_finalize_report(
    draft_md: str,
    art: Path,
    complete_fn,
    question: str,
    lit_result: dict[str, Any],
) -> str:
    """Review the manuscript, then re-apply the authoritative literature output.

    The self-review model is instructed to preserve the populated References section, but
    model edits are best-effort and can still delete, reorder, or replace citations. The
    final write path must therefore make the literature module the last writer of
    ``## References``.
    """
    reviewed = _review_report(draft_md, art, complete_fn, question)
    reviewed = _remove_body_bibliography_metadata(reviewed)
    reviewed = _remove_literature_figure_callouts(reviewed)
    from ..tools.literature_references import insert_references

    # Deterministic render-residue cleanup runs AFTER references are inserted (so it also catches any
    # markup in citation text) — this FIXES the residue the post-render review used to only log.
    return _strip_render_residue(insert_references(reviewed, lit_result))


# Stray "[Figure 1. …]" caption text the writer sometimes leaves as a line right beside the real
# ![](figures/…) image (the caption belongs INSIDE the image's alt-text, not as loose text).
_STRAY_FIGURE_CAPTION_LINE = re.compile(r"(?im)^\s*\[\s*figure\s+\d+\s*[.:][^\]\n]*\]\s*$")
# Inline HTML italic/bold tags — raw ("<i>") and HTML-escaped ("&lt;i&gt;") — that render as literal
# markup. Only these formatting tags are targeted, so other entities (&amp;, &lt; in prose) are safe.
_INLINE_HTML_TAG = re.compile(r"(?is)(?:</?\s*(?:i|b|em|strong|sub|sup)\s*>|&lt;\s*/?\s*(?:i|b|em|strong|sub|sup)\s*&gt;)")


def _strip_render_residue(md: str) -> str:
    """Deterministically FIX (not just flag) the render residue a post-render review used to only
    report: (1) drop stray '[Figure N. …]' placeholder caption lines left next to the real image;
    (2) strip inline HTML italic/bold tags — both raw '<i>' and the HTML-escaped '&lt;i&gt;' form —
    that otherwise render as literal markup; (3) collapse 3+ consecutive blank lines to one. Runs on
    EVERY manuscript before rendering, so a reader never sees this residue."""
    if not md:
        return md
    md = _STRAY_FIGURE_CAPTION_LINE.sub("", md)
    md = _INLINE_HTML_TAG.sub("", md)
    md = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", md)   # collapse blank-line runs
    return md.strip() + "\n"


_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
_PMID_RE = re.compile(r"\bPMID\s*:?\s*(\d{4,12})\b", re.I)


def _normalise_doi(value: str) -> str:
    return (value or "").strip().rstrip(".,;:)]}").lower()


def _accepted_round(round_obj: Any) -> bool:
    verdict = getattr(round_obj, "verdict", None)
    if isinstance(round_obj, dict):
        verdict = round_obj.get("verdict", verdict)
    if isinstance(verdict, dict):
        return str(verdict.get("verdict", "")).lower() == "accept"
    return str(getattr(verdict, "verdict", "")).lower() == "accept"


def _round_scientist_result(round_obj: Any) -> dict[str, Any]:
    if isinstance(round_obj, dict):
        sr = round_obj.get("scientist_result") or {}
    else:
        sr = getattr(round_obj, "scientist_result", {}) or {}
    return sr if isinstance(sr, dict) else {}


def _references_from_accepted_deep_literature(
    lab_result: Any,
    *,
    limit: int = 12,
) -> dict[str, Any] | None:
    """Build final References from accepted in-loop ``deep_literature`` tool outputs.

    The corpus counterpart of :func:`_references_from_accepted_literature_search`. The research
    pipeline grounds literature with ``deep_literature`` (PaperQA over the lab's curated PubMedBERT
    corpus on HPC3), NOT Europe PMC — so its context citations are what the report must cite. Each
    accepted deep_literature step carries its PaperQA ``contexts`` (with a ``citation`` string per
    source doc) on ``step['result']``; collect those, de-duplicated, in order. Returns ``None`` when
    no accepted deep_literature step produced any citation, so the caller can fall back.

    No DOI/PMID gate here (unlike the Europe PMC path): the corpus is curated, so every citation
    string names a paper physically in the index — provenance is guaranteed by the corpus itself.
    Nothing is fabricated; only citation strings the tool actually returned are formatted."""
    rounds = getattr(lab_result, "rounds", None)
    if isinstance(lab_result, dict):
        rounds = lab_result.get("rounds", rounds)
    cite_strings: list[str] = []
    seen: set[str] = set()
    queries: list[str] = []
    answer_text = ""

    for round_obj in rounds or []:
        if not _accepted_round(round_obj):
            continue
        scientist_result = _round_scientist_result(round_obj)
        for step in scientist_result.get("steps") or []:
            if not isinstance(step, dict) or step.get("tool") != "deep_literature" or not step.get("ok"):
                continue
            result = step.get("result") if isinstance(step.get("result"), dict) else {}
            query = str((step.get("args") or {}).get("question") or "").strip()
            if query:
                queries.append(query)
            if not answer_text:
                answer_text = str(result.get("formatted_answer") or result.get("answer") or "").strip()
            for ctx in result.get("contexts") or []:
                cit = (ctx.get("citation") or "").strip() if isinstance(ctx, dict) else ""
                if cit and cit.lower() not in seen:
                    seen.add(cit.lower())
                    cite_strings.append(cit)

    if not cite_strings:
        return None
    from ..tools.literature_references import references_from_corpus_citations

    return references_from_corpus_citations(
        cite_strings[:limit],
        query=" | ".join(dict.fromkeys(queries)),
        answer=answer_text,
    )


def _references_from_accepted_literature_search(
    lab_result: Any,
    *,
    limit: int = 12,
) -> dict[str, Any] | None:
    """Build final References from accepted in-loop ``literature_search`` tool outputs.

    The lab must run Europe PMC as an explicit planned analysis step. The final report reuses
    only accepted DOI/PMID-backed results from that step; it does not run a report-time fallback
    search.
    """
    rounds = getattr(lab_result, "rounds", None)
    if isinstance(lab_result, dict):
        rounds = lab_result.get("rounds", rounds)
    raw_citations: list[dict[str, Any]] = []
    mentioned_dois: set[str] = set()
    mentioned_pmids: set[str] = set()
    queries: list[str] = []

    for round_obj in rounds or []:
        if not _accepted_round(round_obj):
            continue
        scientist_result = _round_scientist_result(round_obj)
        final_answer = str(scientist_result.get("final_answer") or "")
        mentioned_dois.update(_normalise_doi(m.group(0)) for m in _DOI_RE.finditer(final_answer))
        mentioned_pmids.update(m.group(1) for m in _PMID_RE.finditer(final_answer))
        for step in scientist_result.get("steps") or []:
            if not isinstance(step, dict) or step.get("tool") != "literature_search" or not step.get("ok"):
                continue
            result = step.get("result") if isinstance(step.get("result"), dict) else {}
            query = str(result.get("query") or (step.get("args") or {}).get("query") or "").strip()
            if query:
                queries.append(query)
            for citation in result.get("results") or []:
                if isinstance(citation, dict):
                    raw_citations.append(citation)

    if not raw_citations:
        return None
    if not (mentioned_dois or mentioned_pmids):
        return None

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for citation in raw_citations:
        doi = _normalise_doi(str(citation.get("doi") or ""))
        pmid = str(citation.get("pmid") or "").strip()
        if not doi and not pmid:
            continue
        # When the scientist summarized specific papers, keep only those papers.
        if (mentioned_dois or mentioned_pmids) and doi not in mentioned_dois and pmid not in mentioned_pmids:
            continue
        key = doi or f"pmid:{pmid}"
        if key in seen:
            continue
        seen.add(key)
        selected.append(dict(citation))
        if len(selected) >= limit:
            break

    if not selected:
        return None
    from ..tools.literature_references import references_from_citations

    return references_from_citations(
        selected,
        query=" | ".join(dict.fromkeys(queries)),
        reason="reused accepted literature_search tool results",
    )


def _postrender_text_check(rep: dict[str, Any], art: Path, complete_fn) -> str:
    """Post-render text review: extract the rendered document's text (pandoc) and ask the
    model for a SHORT list of remaining issues (leftover content, broken/missing tables,
    empty sections). Advisory only — saved to process/report_review.md, never blocks the run.
    Returns the verdict text ('' when clean or unavailable)."""
    src = rep.get("docx_path") or rep.get("pdf_path")
    if not src or not Path(src).exists() or not shutil.which("pandoc"):
        return ""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["pandoc", str(src), "-t", "plain"], capture_output=True, text=True, timeout=120)
        text = (proc.stdout or "")[:16000]
    except Exception:  # noqa: BLE001
        return ""
    if not text.strip():
        return ""
    try:
        verdict = complete_fn([
            {"role": "system", "content": (
                "You are reviewing the FINAL rendered text of a scientific report. List ONLY "
                "concrete problems a reader would notice: leftover/placeholder/draft text, empty "
                "or duplicated sections, tables that look broken or missing, references to files "
                "instead of inline tables, illogical numbering. If it reads clean, reply with "
                "exactly 'OK'. Be terse — a short bullet list, or 'OK'.")},
            {"role": "user", "content": f"Rendered report text:\n\n{text}"},
        ])
    except Exception:  # noqa: BLE001
        return ""
    verdict = (verdict or "").strip()
    if verdict and verdict.upper() != "OK":
        (art / "process").mkdir(parents=True, exist_ok=True)
        (art / "process" / "report_review.md").write_text(
            "# Post-render report review\n\n" + verdict + "\n", encoding="utf-8")
        return verdict
    return ""


def _scan_tool_invocation(result: Any, tool_name: str) -> "dict[str, Any] | None":
    """The outcome of a tool across a run's rounds: ``None`` if it was never invoked, else the LAST
    step-result dict for that tool (carries ``status`` and any note/error). Lets the capability log
    say whether an optional step (scGPT) actually ran and how it ended."""
    if result is None:
        return None
    last: "dict[str, Any] | None" = None
    for r in getattr(result, "rounds", []) or []:
        sr = getattr(r, "scientist_result", None) or {}
        for s in (sr.get("steps") or []):
            if s.get("tool") == tool_name:
                res = s.get("result")
                last = res if isinstance(res, dict) else {"status": s.get("status") or "unknown"}
    return last


def _write_capability_log(art: Path, result: Any, conn: "Connection",
                          emit: "Callable[..., None]") -> None:
    """ALWAYS record the optional GPU capabilities' status for THIS run — invoked or not, and why not
    — into ``process/capabilities.log`` and the persisted event log. So a bundle can always answer
    "did scGPT / the VL review run this time?" instead of showing no trace at all. Best-effort:
    logging must never break a run."""
    try:
        st = conn.settings
        live = getattr(conn, "executor", None) is not None and not getattr(conn, "mock", False)
        lines: list[str] = ["# Optional GPU capabilities — per-run invocation record", ""]

        scg = _scan_tool_invocation(result, "scgpt_annotate")
        if scg is None:
            lines.append(
                "- scGPT annotation: NOT INVOKED this run (no scgpt_annotate step ran). "
                + (f"Available (live session); image={getattr(st, 'scgpt_image', '?')}."
                   if live else "Not available (mock/offline session — no HPC3 batch lifecycle)."))
        else:
            status = str(scg.get("status", "unknown"))
            if status == "ok":
                tail = f" n_cells={scg.get('n_cells')} predictions={scg.get('predictions_csv')}"
            else:
                tail = f" — {scg.get('note') or scg.get('error') or ''}".rstrip()
            lines.append(f"- scGPT annotation: INVOKED, status={status}.{tail} "
                         "(job log fetched to process/scgpt_job.log when the job ran)")

        if not getattr(st, "vlreview_enabled", False):
            lines.append("- VL render review: DISABLED (BIOAGENT_VLREVIEW_ENABLED not set).")
        elif not live:
            lines.append("- VL render review: ENABLED but SKIPPED (no live HPC3 session this run).")
        else:
            # Detect DEGRADATION: the review's first pass reports model="bbox-only" when the vision
            # model never loaded (e.g. torch missing in the container) — a "clean" verdict from that
            # geometric-only fallback is NOT a real visual review, so say so honestly.
            degraded = False
            vp1 = art / "process" / "visual_review_pass1.json"
            try:
                if vp1.exists():
                    degraded = str(json.loads(vp1.read_text(encoding="utf-8")).get("model", "")
                                   ).lower() == "bbox-only"
            except Exception:  # noqa: BLE001 - a logging helper must never break the run
                degraded = False
            if degraded:
                lines.append("- VL render review: ENABLED but DEGRADED — the vision model did NOT "
                             "load (only the geometric pre-check ran); the pages were NOT visually "
                             "audited (see process/visual_review.md). Check the vlreview container / "
                             "BIOAGENT_VLREVIEW_IMAGE (needs PyTorch + Qwen2.5-VL).")
            else:
                ran = (art / "process" / "visual_review.md").exists()
                lines.append("- VL render review: ENABLED and ran; "
                             + ("defects reported (see process/visual_review.md)." if ran
                                else "pages read clean (no re-render needed)."))

        (art / "process").mkdir(parents=True, exist_ok=True)
        (art / "process" / "capabilities.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        emit("info", "lab", "Capability log — " + " | ".join(ln[2:] for ln in lines if ln.startswith("- ")))
    except Exception:  # noqa: BLE001 - a logging helper must never break the run
        pass


def _postrender_visual_check(rep: dict[str, Any], report_md: str, report_title: str | None,
                             art: Path, conn: "Connection") -> str:
    """Post-render VISUAL review: the text checks above cannot see LAYOUT defects (text overlap,
    clipped cells, a caption printed on the figure) — those live only in the rendered pixels and
    need a vision model. When ``vlreview_enabled`` and a live SSH session is present, ship the
    rendered PDF to a short-lived HPC3 GPU job (Qwen2.5-VL), and RE-RENDER with escalated format
    until the pages read clean or the format ladder is exhausted (bioagent.tools.visual_review).

    Deliverable files are overwritten in place with the cleaned render, so nothing downstream
    changes. Returns a Diagnostics markdown block for the TECHNICAL report when defects remain
    ('' when clean/skipped) — per the silent-degradation design the manuscript itself stays clean.
    Advisory: any failure returns '' and never blocks the run."""
    st = conn.settings
    if not getattr(st, "vlreview_enabled", False) or conn.mock or not getattr(conn, "executor", None):
        return ""
    if not rep.get("pdf_path") or not Path(rep["pdf_path"]).exists():
        return ""   # nothing rendered to look at (pandoc/xelatex absent, or PDF not requested)
    try:
        from ..tools.report import build_pdf_report
        from ..tools.visual_review import render_with_visual_review, format_diagnostics
        from .vlreview_runner import build_vlreview_review_fn

        run_id = conn.workspace.name if getattr(conn, "workspace", None) else "run"

        def render_fn(overrides: dict[str, Any]) -> dict:
            return build_pdf_report(report_md, art / "report", title=report_title,
                                    basename="report", assets_root=art, format_overrides=overrides)

        review_fn = build_vlreview_review_fn(
            conn.executor, st, cluster_user_dir=_temp_base(conn), run_id=run_id,
            local_review_dir=art / "process", emit=conn.emit,
        )
        outcome = render_with_visual_review(
            render_fn, review_fn, max_iters=st.vlreview_max_iters,
            initial_render=rep, emit=conn.emit,
        )
    except Exception as exc:  # noqa: BLE001 - visual review is advisory; never break the run
        conn.emit("warning", "lab", f"Visual review step failed: {exc}")
        return ""

    diag = format_diagnostics(outcome)
    if diag:
        (art / "process").mkdir(parents=True, exist_ok=True)
        (art / "process" / "visual_review.md").write_text(diag + "\n", encoding="utf-8")
    return diag or ""


# Top-level entries the categorized bundle is supposed to contain. Anything else at the
# artifacts root is off-script (e.g. a model-made qc_report.docx / self-zipped bundle from
# run_code) and gets quarantined into extra/ rather than shipped as a canonical deliverable.
_CANONICAL_BUNDLE_DIRS = {"report", "figures", "tables", "process", "data", "extra"}


def _quarantine_strays(art: Path) -> list[str]:
    """MOVE (never delete) any non-canonical top-level artifact entries into extra/ so the
    bundle holds only report/figures/tables/process/data. Returns the names moved."""
    moved: list[str] = []
    extra = art / "extra"
    for p in sorted(art.iterdir()):
        if p.name in _CANONICAL_BUNDLE_DIRS:
            continue
        extra.mkdir(exist_ok=True)
        try:
            shutil.move(str(p), str(extra / p.name))
            moved.append(p.name)
        except Exception:  # noqa: BLE001 - hygiene must never break the run
            pass
    return moved


@app.get("/api/bundle/{owner}/{run_id}")
async def get_bundle(owner: str, run_id: str) -> Response:
    """Zip a run's whole artifacts folder for a single 'download all' click."""
    if not all(part.replace("-", "").isalnum() for part in (owner, run_id)):
        return Response("Bad request", status_code=400)
    base = (CONSOLE_RUNS_DIR / owner / run_id / "artifacts").resolve()
    if not str(base).startswith(str(CONSOLE_RUNS_DIR)) or not base.is_dir():
        return Response("Run not found", status_code=404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(base.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(base))
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="bioagent_results_{run_id}.zip"'},
    )


@app.get("/api/system")
async def system_overview_endpoint() -> JSONResponse:
    """Live code-derived overview: agents, tools (with availability), server
    capabilities, and the roadmap. Powers the console's System page."""
    from . import system_info

    data = system_info.system_overview()
    data["capabilities"]["accounts"] = _AUTH_ENABLED
    db_ok = False
    if _AUTH_ENABLED:
        try:
            from sqlalchemy import text

            from .db import get_engine
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            db_ok = True
        except Exception:  # noqa: BLE001 - DB status is informational
            db_ok = False
    data["capabilities"]["database"] = db_ok
    return JSONResponse(data)


@app.websocket("/ws/{connection_id}")
async def ws_endpoint(websocket: WebSocket, connection_id: str) -> None:
    await websocket.accept()
    conn = CONNECTIONS.get(connection_id)
    if not conn:
        await websocket.send_json({"type": "error", "message": "Unknown connection id"})
        await websocket.close()
        return
    queue: asyncio.Queue = asyncio.Queue()
    conn.subscribers.add(queue)
    try:
        # replay history so a late subscriber sees the whole story
        await websocket.send_json({"type": "status", "connection": conn.summary()})
        for event in conn.log:
            await websocket.send_json(event)
        # if a Duo prompt is currently pending, make sure a late subscriber sees it
        if conn.pending_duo and not conn.duo_event.is_set():
            await websocket.send_json({"type": "duo_prompt", "prompt": conn.pending_duo})
        # same for a pending Plan-mode review (reconnect mid-review still shows the plan
        # or the clarify question). pending_plan is {"kind", "payload"}. Tag the replayed prompt
        # with the active run's identity so a client demuxes it into the owning conversation.
        if conn.pending_plan and not conn.plan_event.is_set():
            _pp = conn.pending_plan
            _tag = conn._tag
            if isinstance(_pp, dict) and _pp.get("kind") == "clarify":
                await websocket.send_json(_tag({"type": "plan_clarify", "questions": _pp.get("payload")}))
            elif isinstance(_pp, dict) and _pp.get("kind") == "decision":
                _dp = _pp.get("payload") or {}
                await websocket.send_json(_tag({"type": "decision_prompt",
                                                "goal": _dp.get("goal", ""), "options": _dp.get("options", [])}))
            elif isinstance(_pp, dict):
                await websocket.send_json(_tag({"type": "plan_prompt", "agenda": _pp.get("payload")}))
        # Rebuild the centre bubble for a client that refreshed / navigated back.
        #  • Run still in flight → replay so the live bubble keeps streaming to this queue.
        #  • Run already FINISHED → replay ONCE more, tagged `recover`, so a client that
        #    MISSED the live completion (WS dropped before chat_done/artifacts) can still
        #    recover the report + recap + downloads. The client dedupes by run_id, so a
        #    client that already persisted this run ignores the replay (no duplicate bubble).
        replay = conn.stream_replay_payloads()
        if replay:
            if not conn.chat_running:
                replay[0] = {**replay[0], "recover": True}
            for payload in replay:
                await websocket.send_json(payload)
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    finally:
        conn.subscribers.discard(queue)


# --- static frontend -------------------------------------------------------


# The console code (index.html / app.js / styles.css) is served with ``Cache-Control: no-cache`` so a
# redeploy is never masked by a stale browser copy — the browser revalidates every load and picks up
# a new app.js immediately. The files are tiny, so re-fetching them costs nothing.
_NO_CACHE = {"Cache-Control": "no-cache"}
# Static binary assets (the ~5MB Material Symbols icon font, images) are large and effectively
# immutable, so cache them hard — otherwise no-cache would re-download the font on every page load.
# A content change ships under a new filename or a redeploy, so a long TTL is safe.
_IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}
_CACHEABLE_SUFFIXES = {".woff2", ".woff", ".ttf", ".otf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico"}


@app.get("/")
async def index() -> Response:
    html = STATIC_DIR / "index.html"
    if not html.exists():
        return HTMLResponse("<h1>AiScientist console assets missing</h1>", status_code=500)
    return HTMLResponse(html.read_text(encoding="utf-8"), headers=_NO_CACHE)


@app.get("/static/{filename:path}")
async def static_file(filename: str) -> Response:
    # ``:path`` so nested assets (e.g. assets/material-symbols/*.woff2) resolve; the realpath guard
    # below still confines every request to STATIC_DIR (no ``..`` traversal).
    target = (STATIC_DIR / filename).resolve()
    if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
        return Response("Not found", status_code=404)
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    headers = _IMMUTABLE if target.suffix.lower() in _CACHEABLE_SUFFIXES else _NO_CACHE
    return Response(target.read_bytes(), media_type=content_type, headers=headers)


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description="AiScientist HPC3 SSH + vLLM console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)
    print(f"AiScientist HPC3 console: http://{args.host}:{args.port}/")
    uvicorn.run("bioagent.gateway.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
