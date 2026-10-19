"""Where every run is right now: the live half of the console's System page.

Each run (a research study, a chat answer, a corpus scan) carries a :class:`RunMonitor`. It reads
what the run already produces — the lab's ``on_event`` dicts, the WebSocket payloads its connection
pushes, and the start and end of each model call — and keeps one small picture of the run: the
pipeline stage it is in, the steps that are running (several at once when the DAG scheduler runs
independent steps side by side), and the tool, model or Critic each step is waiting on.
``GET /api/system/live`` serves these pictures and the System page draws them as moving markers on
the workflow graph.

The graph's stage nodes are :data:`STAGES`, the list this module moves runs through, so the graph
cannot show a stage no run reaches or miss one they do (``tests/test_run_monitor.py``).

An observation is a few dict updates under a lock, with no I/O, and an error inside one is
swallowed: watching a run must never slow it down or break it.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

KINDS = ("research", "chat", "corpus")


@dataclass(frozen=True)
class Stage:
    """One stage of the pipeline. ``kinds`` are the run kinds that pass through it: ``research``
    (the lab), ``chat`` (the answer-first loop), ``corpus`` (the corpus scan). ``roles`` are the
    model roles that work in it (the ``role=`` of their model calls)."""

    id: str
    label: str
    kinds: tuple[str, ...]
    detail: str
    where: str
    roles: tuple[str, ...] = ()


STAGES: tuple[Stage, ...] = (
    Stage("request", "Request", KINDS,
          "The message arrives with its bound dataset. A follow-up is routed to the run it continues; "
          "a new study gets its own folder and history row.",
          "gateway/app.py", ("classify",)),
    Stage("data", "Data & compute", ("research",),
          "The dataset is staged to HPC3 and profiled, and the tools' executors are wired: HPC3 Slurm "
          "jobs per tool line, the code sandbox, the scGPT GPU job, the report renderer.",
          "gateway/app.py"),
    Stage("plan", "Plan", ("research",),
          "The request's scope and the matching pipeline are read, the PI drafts the plan as a task "
          "graph, and the Critic reviews it before anyone sees it.",
          "agents/research_lab.py", ("plan", "critic")),
    Stage("approve", "Your review", ("research",),
          "Plan first: the run waits for you to run, edit or cancel the plan, or answer its question.",
          "gateway/app.py"),
    Stage("execute", "Run steps", ("research",),
          "The scheduler hands each ready step to the Scientist in the persona of the specialist it "
          "needs; the Scientist calls tools, the Critic accepts the result or sends it back.",
          "agents/research_lab.py", ("scientist", "critic")),
    Stage("audit", "Claim audit", ("research",),
          "Each claim the accepted steps support is checked on its own before anything is written.",
          "agents/claim_audit.py", ("audit",)),
    Stage("writeup", "Write-up", ("research",),
          "The writer drafts the report from the accepted steps only; citations are attached from the "
          "accepted literature steps and the draft is self-reviewed and its numbers checked.",
          "agents/research_lab.py", ("writer",)),
    Stage("report", "Report", ("research",),
          "The report is rendered to PDF / DOCX (on HPC3 when configured) and reviewed; a run that "
          "only did quality control answers in the chat instead. The files go to Downloads.",
          "reporting/"),
    Stage("chat", "Chat answer", ("chat",),
          "The answer-first loop: the model answers directly and calls a tool only when it needs one.",
          "agents/quick_chat.py", ("chat",)),
    Stage("corpus", "Corpus scan", ("corpus",),
          "One literature job on HPC3 over the whole indexed corpus; its two tables are the result.",
          "gateway/app.py"),
    Stage("done", "Done", KINDS,
          "The run ended: finished, stopped, cancelled or failed.",
          "gateway/app.py"),
)
STAGE_IDS = tuple(s.id for s in STAGES)
_LABEL = {s.id: s.label for s in STAGES}

# What a running step is doing: the model is choosing its next action, a tool is running, or the
# Critic is judging the step's result.
PHASES = ("model", "tool", "critic")
OUTCOMES = ("finished", "stopped", "cancelled", "failed", "ended")

# Lab events that put a research run into a stage. Anything else leaves the stage alone.
_STAGE_OF_EVENT = {
    "scope": "plan", "skills_loaded": "plan", "mode_selected": "plan", "team_meeting_start": "plan",
    "planning": "plan", "pi_agenda": "plan", "pi_clarify": "plan", "plan_review": "plan",
    "cycle_planned": "plan",
    "plan_approved": "execute", "lab_plan_dag": "execute", "coordinator_pick": "execute",
    "node_claim": "execute", "scientist_start": "execute", "concurrency_batch": "execute",
    "cycle_start": "execute",
}
# WebSocket payloads that make a research run wait for its owner (pushed by the gateway).
_WAITS_FOR_PLAN = ("plan_prompt", "plan_clarify", "decision_prompt")
# Model roles that put a research run into a stage when one of their calls starts.
_STAGE_OF_ROLE = {"audit": "audit", "writer": "writeup"}

RETAIN_SECONDS = 600        # how long a finished run stays on the page
MAX_MONITORS = 48           # and how many runs are kept at most
_RECENT = 5                 # milestones kept per run
_TEXT = 160                 # characters kept of a step, a milestone or the question


def _clip(text: Any, n: int = _TEXT) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


class RunMonitor:
    """The current picture of one run. Fed by :meth:`observe` (lab events), :meth:`observe_push`
    (WebSocket payloads), :meth:`llm_begin` / :meth:`llm_end` (model calls) and :meth:`enter`
    (the gateway's own stages); read by :meth:`snapshot`."""

    def __init__(self, *, kind: str = "research", owner_id: int | None = None, owner: str = "",
                 conversation_id: str | None = None, run_id: str | None = None,
                 question: str = "", model: str = "") -> None:
        self.key = uuid4().hex[:12]
        self.kind = kind if kind in KINDS else "research"
        self.owner_id = owner_id
        self.owner = owner
        self.conversation_id = conversation_id
        self.run_id = run_id
        self.question = _clip(question, 240)
        self.model = _clip(model, 80)
        now = time.time()
        self.started = now
        self.updated = now
        self.stage = "request"
        self.stage_since = now
        self.outcome = ""
        self.ended: float | None = None
        self.waiting = ""                   # "plan" | "confirm" while the run waits for its owner
        self.steps_total = 0
        self.accepted = 0
        self.tools_called = 0
        self.tool_errors = 0
        self.llm_calls = 0
        # The running steps, keyed by the thread that runs them: the lab calls on_event in the
        # thread that emitted the event, and each step the DAG scheduler runs side by side has its own.
        self._active: dict[int, dict[str, Any]] = {}
        self._nodes: list[dict[str, str]] = []          # the plan's steps: [{id, goal}]
        self._claimed: dict[int, str] = {}              # thread -> node id it claimed
        self._llm: dict[int, str] = {}                  # thread -> role of its model call in flight
        self.recent: list[dict[str, Any]] = []
        self._plan_cancelled = False
        self._lock = threading.Lock()

    # -- feeding ----------------------------------------------------------------------------------

    def observe(self, ev: dict[str, Any]) -> None:
        """One lab ``on_event`` dict. Never raises."""
        self._guarded(self._observe, ev)

    def observe_push(self, payload: dict[str, Any]) -> None:
        """One WebSocket payload pushed for this run. Never raises."""
        self._guarded(self._observe_push, payload)

    def enter(self, stage: str) -> None:
        """Put the run in ``stage`` from the gateway's side (staging and the report tail send no lab
        event). Never raises."""
        self._guarded(lambda s: self._to(s) if s in STAGE_IDS else None, stage)

    def llm_begin(self, role: str | None) -> None:
        """A model call starts in the calling thread."""
        self._guarded(self._llm_begin, str(role or "reason"))

    def llm_end(self) -> None:
        self._guarded(lambda _: self._llm.pop(threading.get_ident(), None), None)

    def end(self, outcome: str = "ended") -> None:
        """The run is over. Idempotent: the first outcome wins."""
        self._guarded(self._finish, outcome)

    def _guarded(self, fn: Any, arg: Any) -> None:
        try:
            with self._lock:
                if self.ended is None:
                    fn(arg)
                    self.updated = time.time()
        except Exception:  # noqa: BLE001 - watching a run never breaks it
            pass

    # -- internals (under the lock) ---------------------------------------------------------------

    def _to(self, stage: str) -> None:
        if stage != self.stage:
            self.stage = stage
            self.stage_since = time.time()
            if stage != "approve":
                self.waiting = ""
            if stage not in ("execute", "chat", "corpus"):
                self._active.clear()

    def _finish(self, outcome: str) -> None:
        self.outcome = outcome if outcome in OUTCOMES else "ended"
        self.ended = time.time()
        self.waiting = ""
        self._active.clear()
        self._llm.clear()
        self._to("done")

    def _note(self, text: str, level: str = "info") -> None:
        text = _clip(text)
        if text:
            self.recent.append({"t": time.time(), "text": text, "level": level})
            del self.recent[:-_RECENT]

    def _llm_begin(self, role: str) -> None:
        self.llm_calls += 1
        self._llm[threading.get_ident()] = role
        stage = _STAGE_OF_ROLE.get(role)
        if stage and self.kind == "research":
            self._to(stage)

    def _node_n(self, node_id: str) -> int | None:
        for i, n in enumerate(self._nodes, 1):
            if n["id"] == node_id:
                return i
        return None

    def _entry_for_critic(self, step: str) -> dict[str, Any] | None:
        """The running step a Critic verdict is about. In DAG mode the verdict names the step's goal
        while ``scientist_start`` named its brief, so match the goal through the plan's nodes first."""
        node_id = next((n["id"] for n in self._nodes if n["goal"] == step), None)
        for entry in self._active.values():
            if (node_id and entry.get("node") == node_id) or entry["step"] == step:
                return entry
        return self._active.get(threading.get_ident())

    def _drop(self, entry: dict[str, Any]) -> None:
        for k, v in list(self._active.items()):
            if v is entry:
                del self._active[k]

    def _observe(self, ev: dict[str, Any]) -> None:
        t = str(ev.get("type") or "")
        now = time.time()
        tid = threading.get_ident()
        stage = _STAGE_OF_EVENT.get(t)
        if stage and self.kind == "research":
            self._to(stage)
        if t == "pi_agenda":
            agenda = [_clip(s) for s in ev.get("agenda") or []]
            self.steps_total = len(agenda)
            self._nodes = [{"id": f"s{i}", "goal": g} for i, g in enumerate(agenda, 1)]
        elif t == "lab_plan_dag":
            nodes = [n for n in ev.get("nodes") or [] if isinstance(n, dict)]
            if nodes:
                self._nodes = [{"id": str(n.get("id") or f"s{i}"), "goal": _clip(n.get("goal"))}
                               for i, n in enumerate(nodes, 1)]
                self.steps_total = len(self._nodes)
        elif t == "agenda_extended":
            added = ev.get("added")
            self.steps_total += len(added) if isinstance(added, list) and added else 1
        elif t == "plan_cancelled":
            self._plan_cancelled = True
        elif t == "node_claim":
            self._claimed[tid] = str(ev.get("node") or "")
        elif t == "scientist_start":
            node = self._claimed.pop(tid, "")
            step = _clip(ev.get("step"))
            n = self._node_n(node) if node else None
            if n is None:
                n = next((i for i, x in enumerate(self._nodes, 1) if x["goal"] == step), None)
            goal = self._nodes[n - 1]["goal"] if n else step
            self._active[tid] = {"step": goal, "n": n, "node": node,
                                 "specialist": _clip(ev.get("specialist") or "Scientist", 60),
                                 "phase": "model", "tool": "", "since": now}
        elif t == "model_call":
            entry = self._active.get(tid)
            if entry is not None and entry["phase"] != "model":
                entry.update(phase="model", tool="", since=now)
        elif t == "tool_start":
            self.tools_called += 1
            entry = self._active.get(tid)
            if entry is None and self.kind != "research":
                # The chat and corpus lines have no steps: one implicit step holds the tool.
                entry = self._active.setdefault(tid, {"step": "", "n": None, "node": "", "specialist": "",
                                                      "phase": "model", "tool": "", "since": now})
            if entry is not None:
                entry.update(phase="tool", tool=_clip(ev.get("tool"), 60), since=now)
        elif t in ("tool_result", "tool_error"):
            if t == "tool_error":
                self.tool_errors += 1
            entry = self._active.get(tid)
            if entry is not None:
                if self.kind == "research":
                    entry.update(phase="model", tool="", since=now)
                else:
                    self._drop(entry)
        elif t == "finish":
            entry = self._active.get(tid)
            if entry is not None:
                entry.update(phase="critic", tool="", since=now)
        elif t == "critic":
            step = _clip(ev.get("step"))
            entry = self._entry_for_critic(step)
            if str(ev.get("verdict") or "").lower() == "accept":
                self.accepted += 1
                if entry is not None:
                    self._drop(entry)
            elif entry is not None:
                entry.update(phase="model", tool="", since=now)
        elif t == "step_force_advance":
            entry = self._entry_for_critic(_clip(ev.get("step")))
            if entry is not None:
                self._drop(entry)
        elif t == "synthesize" and self.kind == "research":
            # A quality-control-only run skips the audit and the writer: the gateway answers it.
            self._to("report" if ev.get("qc_only") else "audit")

    def _observe_push(self, payload: dict[str, Any]) -> None:
        t = str(payload.get("type") or "")
        if t in _WAITS_FOR_PLAN and self.kind == "research":
            self._to("approve")
            self.waiting = "plan"
        elif t == "confirm_request":
            self.waiting = "confirm"
        elif t in ("confirm_done", "plan_done") and self.waiting:
            self.waiting = ""
        elif t == "lab_progress":
            self._note(payload.get("text") or "", str(payload.get("level") or "info"))
        elif t == "chat_stopped":
            self._finish("cancelled" if self._plan_cancelled else "stopped")
        elif t == "chat_error":
            self._finish("failed")
        elif t == "run_complete":
            self._finish("finished")
        elif t == "chat_done" and self.kind != "research":
            self._finish("finished")

    # -- reading ----------------------------------------------------------------------------------

    def snapshot(self, *, full: bool) -> dict[str, Any]:
        """The run as the System page draws it. ``full`` = the viewer owns it (or is an admin);
        otherwise only where it is, never what it is about: no question, no step text."""
        with self._lock:
            active = sorted(self._active.values(), key=lambda e: e["since"])
            roles = sorted(set(self._llm.values()))
            out: dict[str, Any] = {
                "key": self.key, "kind": self.kind, "stage": self.stage,
                "stage_label": _LABEL.get(self.stage, self.stage), "stage_since": self.stage_since,
                "started": self.started, "updated": self.updated, "ended": self.ended,
                "outcome": self.outcome, "waiting": self.waiting,
                "steps": {"total": self.steps_total, "accepted": self.accepted},
                "tools_called": self.tools_called, "tool_errors": self.tool_errors,
                "llm": {"calls": self.llm_calls, "thinking": roles},
                "active": [{"phase": e["phase"], "tool": e["tool"], "since": e["since"],
                            **({"step": e["step"], "n": e["n"], "specialist": e["specialist"]}
                               if full else {})} for e in active],
            }
            if full:
                out.update(question=self.question, model=self.model, run_id=self.run_id,
                           conversation_id=self.conversation_id, recent=list(self.recent))
            return out


# -- the registry the live endpoint reads ---------------------------------------------------------

_MONITORS: "OrderedDict[str, RunMonitor]" = OrderedDict()
_REG_LOCK = threading.Lock()


def start(**kw: Any) -> RunMonitor:
    """A new monitor, registered so the System page sees it."""
    m = RunMonitor(**kw)
    with _REG_LOCK:
        _MONITORS[m.key] = m
        _prune_locked()
    return m


def _prune_locked() -> None:
    now = time.time()
    for key, m in list(_MONITORS.items()):
        if m.ended is not None and now - m.ended > RETAIN_SECONDS:
            del _MONITORS[key]
    while len(_MONITORS) > MAX_MONITORS:
        # The oldest finished run goes first; a running one only past the cap.
        victim = next((k for k, m in _MONITORS.items() if m.ended is not None), next(iter(_MONITORS)))
        del _MONITORS[victim]


def monitors() -> list[RunMonitor]:
    with _REG_LOCK:
        _prune_locked()
        return list(_MONITORS.values())


def live_view(viewer_id: int | None, *, admin: bool, accounts: bool) -> dict[str, Any]:
    """Every run the page draws, newest first. With accounts on, a viewer sees their own runs in
    full and other accounts' runs only as positions; an admin sees all of them in full, with the
    owner. With accounts off there is one operator, who sees everything."""
    runs = []
    for m in reversed(monitors()):
        mine = (not accounts) or (viewer_id is not None and m.owner_id == viewer_id)
        snap = m.snapshot(full=mine or admin)
        snap["mine"] = mine
        if admin and accounts and not mine:
            snap["owner"] = m.owner
        runs.append(snap)
    return {"now": time.time(), "runs": runs,
            "running": sum(1 for r in runs if r["ended"] is None)}


def _reset_for_tests() -> None:
    with _REG_LOCK:
        _MONITORS.clear()
