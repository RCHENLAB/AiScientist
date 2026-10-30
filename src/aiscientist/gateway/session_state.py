"""Where a compute session is, as the console shows it (RCHENLAB/AiScientist issue 6).

A session is one connection to HPC3 (``app.Connection``). The gateway host keeps the user's data;
HPC3 holds a copy only while the session's runs need it. What the session is doing with HPC3 at any
moment is one of::

    open          connected, nothing moving
    staging-in    copying a run's inputs from the server to the session's area on HPC3
    running       a run is executing
    staging-out   copying from HPC3 to the server (a result being saved, a dataset kept only on
                  HPC3 being moved home)
    cleaning-up   the session ended and its cleanup job is queued on HPC3
    closed        the session ended and nothing of it is left to remove (or the cleanup job finished)
    failed        something the session had to bring home or remove could not be: a result whose
                  copy back failed (its HPC3 folder is kept), or a cleanup that could not be queued
                  or did not finish

While the session is open the state follows what is in flight, in that order of precedence:
staging-out, staging-in, running, then failed (a stage-out failure not yet resolved by a later
successful copy of the same thing), else open.

Every copy is a *transfer* with its progress (files and bytes) and, when it fails, the reason. The
gateway pushes the state to the console as it changes (``session_state`` messages, progress at
most once a second), and keeps a record of the account's recent sessions in
``<workspace>/.sessions.json`` so a session that has ended is still shown with how it ended,
including one that ended without a disconnect (the gateway restarted, or the connection was lost):
what it left on HPC3 is brought home and removed at the account's next connect, or by the sweep.

This module knows nothing of SSH or HTTP: the gateway calls it, and the tests drive it directly.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

STATES = ("open", "staging-in", "running", "staging-out", "cleaning-up", "closed", "failed")
ENDED = ("cleaning-up", "closed", "failed")

STORE_NAME = ".sessions.json"
KEEP_SESSIONS = 20          # records kept per account
KEEP_TRANSFERS = 20         # transfers kept per session (the running ones always stay)
KEEP_FAILURES = 10
PUSH_EVERY_S = 1.0          # progress is pushed at most this often; a change of state always is

# A failure, a transfer or a cleanup says what went wrong in at most this many characters.
_ERROR_CHARS = 300

_STORE_LOCK = threading.Lock()   # one gateway process writes every account's file


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short(text: Any) -> str:
    return str(text or "")[:_ERROR_CHARS]


# --- the state of one record --------------------------------------------------------------------

def unresolved_failures(record: dict) -> list[dict]:
    """The record's stage-out transfers that failed and were not copied again since: each is data
    that is still only on HPC3."""
    out: list[dict] = []
    transfers = record.get("transfers") or []
    for i, t in enumerate(transfers):
        if t.get("kind") != "stage-out" or t.get("status") != "failed":
            continue
        key = t.get("key") or t.get("id")
        if any((later.get("key") or later.get("id")) == key and later.get("status") in ("done", "running")
               for later in transfers[i + 1:]):
            continue
        out.append(t)
    return out


def final_state(record: dict) -> str:
    """The state of a session that has ended, read off its record."""
    cleanup = record.get("cleanup") or {}
    if cleanup.get("status") in ("error", "failed") or unresolved_failures(record):
        return "failed"
    if cleanup.get("status") == "queued":
        return "cleaning-up"
    return "closed"


def describe(record: dict) -> dict:
    """A record as the console reads it: the record plus ``state``, ``active`` (the transfers in
    flight, with a percentage) and ``unresolved`` (the stage-out failures still open)."""
    out = dict(record)
    active = []
    for t in record.get("transfers") or []:
        if t.get("status") != "running":
            continue
        total = int(t.get("bytes_total") or 0)
        pct = round(100 * int(t.get("bytes_done") or 0) / total) if total else None
        active.append({**t, "percent": pct})
    out["active"] = active
    out["unresolved"] = unresolved_failures(record)
    if "state" not in out:
        out["state"] = final_state(record) if record.get("closed_at") else "open"
    return out


# --- one live session ---------------------------------------------------------------------------

class SessionTracker:
    """The live record of one session. Thread-safe: copies run in worker threads.

    ``is_running`` says whether a run of the session is executing; ``on_change`` receives the
    described record whenever it should be shown again (from any thread)."""

    def __init__(self, *, session_id: str, connection_id: str, owner: str,
                 is_running: "Callable[[], bool] | None" = None,
                 on_change: "Callable[[dict], None] | None" = None,
                 clock: "Callable[[], float]" = time.monotonic) -> None:
        self._lock = threading.RLock()
        self._is_running = is_running or (lambda: False)
        self._on_change = on_change
        self._clock = clock
        self._last_push = 0.0
        self._cleaning = False
        self.store_dir: "Path | None" = None      # where the record is kept; None = not kept
        self.record: dict[str, Any] = {
            "id": session_id, "connection_id": connection_id, "owner": owner,
            "hpc_user": "", "host": "", "opened_at": _now(), "updated_at": _now(),
            "closed_at": None, "ended": None, "transfers": [], "failures": [], "cleanup": None,
        }

    # -- identity and keeping -------------------------------------------------------------------

    def keep(self, store_dir: Path, *, hpc_user: str = "", host: str = "") -> None:
        """Start keeping this session's record in ``store_dir`` (the account's workspace). Called
        once the session is really on HPC3; a mock or local session is never written down."""
        with self._lock:
            self.store_dir = Path(store_dir)
            if hpc_user:
                self.record["hpc_user"] = hpc_user
            if host:
                self.record["host"] = host
        self._changed(save=True)

    # -- the state ------------------------------------------------------------------------------

    def state(self) -> str:
        with self._lock:
            if self.record.get("closed_at"):
                return final_state(self.record)
            if self._cleaning:
                return "cleaning-up"
            running = [t for t in self.record["transfers"] if t.get("status") == "running"]
            if any(t.get("kind") == "stage-out" for t in running):
                return "staging-out"
            if any(t.get("kind") == "stage-in" for t in running):
                return "staging-in"
        if self._is_running():
            return "running"
        with self._lock:
            return "failed" if unresolved_failures(self.record) else "open"

    def snapshot(self) -> dict:
        state = self.state()
        with self._lock:
            return describe({**json.loads(json.dumps(self.record)), "state": state})

    # -- transfers -------------------------------------------------------------------------------

    def start_transfer(self, kind: str, label: str, *, key: str = "", run_id: str = "",
                       files_total: int = 0, bytes_total: int = 0) -> str:
        """Record a copy that is starting; returns its id. ``kind`` is ``stage-in`` (server to
        HPC3) or ``stage-out`` (HPC3 to server); ``key`` names what it copies, so a later copy of
        the same thing resolves an earlier failure."""
        if kind not in ("stage-in", "stage-out"):
            raise ValueError(f"unknown transfer kind: {kind}")
        tid = uuid.uuid4().hex[:12]
        with self._lock:
            self.record["transfers"].append({
                "id": tid, "kind": kind, "label": str(label), "key": str(key or tid),
                "run_id": str(run_id or ""), "status": "running",
                "files_done": 0, "files_total": int(files_total or 0),
                "bytes_done": 0, "bytes_total": int(bytes_total or 0),
                "started_at": _now(), "ended_at": "", "error": ""})
            self._trim()
        self._changed(save=True)
        return tid

    def progress(self, tid: str, *, bytes_done: "int | None" = None, bytes_total: "int | None" = None,
                 files_done: "int | None" = None, files_total: "int | None" = None) -> None:
        """Update a copy's progress. Pushed at most once a second; never written to disk."""
        with self._lock:
            t = self._transfer(tid)
            if t is None or t["status"] != "running":
                return
            for name, value in (("bytes_done", bytes_done), ("bytes_total", bytes_total),
                                ("files_done", files_done), ("files_total", files_total)):
                if value is not None:
                    t[name] = int(value)
            now = self._clock()
            due = now - self._last_push >= PUSH_EVERY_S
        if due:
            self._changed(save=False)

    def finish_transfer(self, tid: str, *, error: Any = "", cancelled: bool = False) -> None:
        """A copy ended: done, failed (``error``) or stopped by the user (``cancelled``)."""
        with self._lock:
            t = self._transfer(tid)
            if t is None or t["status"] != "running":
                return
            t["ended_at"] = _now()
            if cancelled:
                t["status"] = "cancelled"
            elif error:
                t["status"] = "failed"
                t["error"] = _short(error)
                self._fail(f"{t['label']}: {t['error']}")
            else:
                t["status"] = "done"
                if t["bytes_total"]:
                    t["bytes_done"] = t["bytes_total"]
                if t["files_total"]:
                    t["files_done"] = t["files_total"]
        self._changed(save=True)

    def unsaved_runs(self) -> set[str]:
        """The runs whose result is being copied home or failed to be: their HPC3 folders must not
        be removed with the session (a later session can still save them)."""
        with self._lock:
            out = {t["run_id"] for t in self.record["transfers"]
                   if t.get("kind") == "stage-out" and t.get("status") == "running" and t.get("run_id")}
            out |= {t["run_id"] for t in unresolved_failures(self.record) if t.get("run_id")}
            return out

    # -- the end ---------------------------------------------------------------------------------

    def begin_cleanup(self) -> None:
        with self._lock:
            self._cleaning = True
        self._changed(save=False)

    def end(self, cleanup: dict, *, ended: str = "disconnect") -> dict:
        """The session ended. ``cleanup`` says what became of its HPC3 files: ``status`` is
        ``queued`` (a job id with it), ``nothing``, ``skipped`` (with a ``reason``) or ``error``
        (with the ``error``). Returns the described record."""
        with self._lock:
            self._cleaning = False
            for t in self.record["transfers"]:
                if t.get("status") == "running":           # cut off by the disconnect
                    t["status"] = "failed"
                    t["ended_at"] = _now()
                    t["error"] = "interrupted when the session ended"
            self.record["cleanup"] = {k: v for k, v in cleanup.items() if v not in (None, "", [])}
            if cleanup.get("status") == "error":
                self._fail(f"cleanup: {_short(cleanup.get('error'))}")
            self.record["closed_at"] = _now()
            self.record["ended"] = ended
        self._changed(save=True)
        return self.snapshot()

    # -- internals -------------------------------------------------------------------------------

    def _transfer(self, tid: str) -> "dict | None":
        return next((t for t in self.record["transfers"] if t["id"] == tid), None)

    def _fail(self, text: str) -> None:
        self.record["failures"].append({"at": _now(), "error": _short(text)})
        del self.record["failures"][:-KEEP_FAILURES]

    def _trim(self) -> None:
        transfers = self.record["transfers"]
        while len(transfers) > KEEP_TRANSFERS:
            # The oldest finished one goes first; a copy in flight, or a failure not yet resolved,
            # stays as long as there is anything else to drop.
            open_ = {id(t) for t in unresolved_failures(self.record)}
            victim = next((t for t in transfers if t["status"] != "running" and id(t) not in open_),
                          None) or next((t for t in transfers if t["status"] != "running"), None)
            if victim is None:
                break
            transfers.remove(victim)

    def _changed(self, *, save: bool) -> None:
        with self._lock:
            self.record["updated_at"] = _now()
            self._last_push = self._clock()
            store = self.store_dir
        snap = self.snapshot()
        if save and store is not None:
            try:
                save_record(store, {k: v for k, v in snap.items()
                                    if k not in ("active", "unresolved")})
            except OSError as exc:                      # the console still gets the state
                print(f"[session] could not keep the record of {snap['id']}: {exc}")
        if self._on_change is not None:
            try:
                self._on_change(snap)
            except Exception as exc:  # noqa: BLE001 - showing the state never breaks the session
                print(f"[session] state push failed: {type(exc).__name__}: {exc}")


# --- the account's recent sessions --------------------------------------------------------------

def store_path(store_dir: Path) -> Path:
    return Path(store_dir) / STORE_NAME


def load_records(store_dir: Path) -> list[dict]:
    """The account's kept sessions, oldest first. An unreadable file reads as none."""
    try:
        data = json.loads(store_path(store_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("id")] if isinstance(data, list) else []


def _write(store_dir: Path, records: list[dict]) -> None:
    path = store_path(store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(records[-KEEP_SESSIONS:], indent=1), encoding="utf-8")
    os.replace(tmp, path)


def save_record(store_dir: Path, record: dict) -> None:
    """Replace (or add) one session's record, keeping the newest :data:`KEEP_SESSIONS`."""
    with _STORE_LOCK:
        records = [r for r in load_records(store_dir) if r.get("id") != record.get("id")]
        records.append(record)
        _write(store_dir, records)


def update_records(store_dir: Path, change: "Callable[[dict], bool]") -> int:
    """Apply ``change`` to every kept record (it returns True when it changed one); returns how
    many changed. The file is rewritten only when something did."""
    with _STORE_LOCK:
        records = load_records(store_dir)
        changed = sum(1 for r in records if change(r))
        if changed:
            _write(store_dir, records)
        return changed


def mark_lost(store_dir: Path, live_ids: Iterable[str]) -> int:
    """Records of sessions that are neither live nor closed ended without a disconnect (the gateway
    restarted, or the connection was lost): close them as such. Their HPC3 files are left to the
    Temp sweep. Returns how many were closed."""
    live = set(live_ids)

    def change(r: dict) -> bool:
        if r.get("closed_at") or r.get("connection_id") in live:
            return False
        r["closed_at"] = r.get("updated_at") or _now()
        r["ended"] = "lost"
        for t in r.get("transfers") or []:
            if t.get("status") == "running":
                t["status"] = "failed"
                t["error"] = "interrupted when the session was lost"
        r["cleanup"] = {"status": "skipped", "reason": "sweep"}
        r["state"] = final_state(r)
        return True

    return update_records(store_dir, change)


def list_sessions(store_dir: Path, live: "dict[str, dict] | None" = None) -> list[dict]:
    """The account's recent sessions, newest first: each live one (``live`` maps a connection id to
    its tracker's snapshot) as it is now, every other one as it was kept."""
    live = live or {}
    mark_lost(store_dir, live)
    out: dict[str, dict] = {}
    for r in load_records(store_dir):
        out[r["id"]] = describe(r)
    for snap in live.values():
        out.pop(snap["id"], None)
        out[snap["id"]] = snap
    # Live ones first, then newest first; a record kept later wins a tie (times are to the second).
    order = list(out.values())
    return [r for _, r in sorted(enumerate(order), reverse=True, key=lambda ir: (
        not ir[1].get("closed_at"), ir[1].get("opened_at") or "", ir[0]))]


# --- confirming a cleanup job that was queued at the end of a session ----------------------------

_COMPLETED = ("COMPLETED",)
_ENDED_BADLY = ("FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL",
                "DEADLINE", "PREEMPTED", "REVOKED")


def queued_cleanups(store_dir: Path, hpc_user: str) -> list[str]:
    """Job ids of this HPC3 account's cleanups still recorded as queued."""
    return [str((r.get("cleanup") or {}).get("job_id")) for r in load_records(store_dir)
            if (r.get("cleanup") or {}).get("status") == "queued"
            and (r.get("cleanup") or {}).get("job_id")
            and (not hpc_user or r.get("hpc_user") in ("", hpc_user))]


def queued_cleanup_logs(store_dir: Path, hpc_user: str) -> "dict[str, str]":
    """``{job id: its log on HPC3}`` of this HPC3 account's cleanups still recorded as queued (the
    log is '' for a record from before session folders, whose log was one per user)."""
    return {str((r.get("cleanup") or {}).get("job_id")): str((r.get("cleanup") or {}).get("log") or "")
            for r in load_records(store_dir)
            if (r.get("cleanup") or {}).get("status") == "queued"
            and (r.get("cleanup") or {}).get("job_id")
            and (not hpc_user or r.get("hpc_user") in ("", hpc_user))}


def confirm_cleanups(store_dir: Path, states: "dict[str, str]",
                     last_result: "dict | None" = None, *,
                     results_by_job: "dict[str, dict] | None" = None) -> int:
    """Settle the queued cleanups whose job has ended. ``states`` maps a job id to its Slurm state
    (``sacct``); ``results_by_job`` maps a job id to the ``CLEAN_RESULT`` of its log (each session
    folder keeps its own), and ``last_result`` is the one result of a single shared log, with its
    ``job``. A job still queued or running, or one Slurm has no record of, is left as it is. Returns
    how many records changed."""
    results = dict(results_by_job or {})
    if last_result and last_result.get("job"):
        results.setdefault(str(last_result["job"]), last_result)

    def change(r: dict) -> bool:
        cleanup = r.get("cleanup") or {}
        job = str(cleanup.get("job_id") or "")
        if cleanup.get("status") != "queued" or job not in states:
            return False
        state = (states[job] or "").split()[0].upper() if states[job] else ""
        last_result = results.get(job)
        mine = bool(last_result)
        if mine:
            for k in ("removed", "missing", "kept"):
                if k in last_result:
                    cleanup[k] = last_result[k]
        if state.startswith(_COMPLETED):
            cleanup["status"] = "done"
        elif state.startswith(_ENDED_BADLY):
            cleanup["status"] = "failed"
            if mine and int(last_result.get("kept") or 0):
                n = int(last_result["kept"])
                cleanup["error"] = (f"{n} run folder{'' if n == 1 else 's'} kept on HPC3: a result there "
                                    "did not match its copy on the server; it is copied again at the "
                                    "next connect")
            else:
                cleanup["error"] = f"the cleanup job ended {state}; the files are left to the sweep"
        else:
            return False
        cleanup["confirmed_at"] = _now()
        r["cleanup"] = cleanup
        r["state"] = final_state(r)
        return True

    return update_records(store_dir, change)


def parse_clean_log(text: str) -> "dict | None":
    """The last ``CLEAN_JOB`` / ``CLEAN_RESULT`` pair of a session cleanup log, as
    ``{"job", "removed", "missing", "kept"}``; None when the log has no result line."""
    job, result = "", None
    for line in (text or "").splitlines():
        if line.startswith("CLEAN_JOB "):
            job = line.split(None, 1)[1].strip()
        elif line.startswith("CLEAN_RESULT "):
            result = {"job": job}
            for field in line.split()[1:]:
                key, _, value = field.partition("=")
                if value.isdigit():
                    result[key] = int(value)
    return result
