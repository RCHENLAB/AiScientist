"""Where each step of a running plan stands, the command it runs, and the record of every tool call.

RCHENLAB/AiScientist#121: both plan cards (the side panel's and the chat's entry) mark each step
**processing** while it runs and **done** when its result is accepted, the run card leads with the
step running now, its Slurm job and the command, and the chat shows no JSON arguments.

- The lab says where a step stands with a ``step_state`` event (``ResearchLab._step_state``). The
  gateway keeps the latest record of each step with the run's stream (:func:`merge_step_state`), so
  a window that reloads mid-run gets the same marks back, and writes them into the run's saved state
  (``process/run_state.json`` ``step_states``) for the saved run.
- A Slurm job submitted while a step runs is that step's job: the record carries its id.
- Every tool call (tool, arguments, result summary) goes to ``process/tool_calls.jsonl`` in the
  run's results (:class:`ToolCallLog`); the chat and the run card say the call in words
  (:func:`tool_call_words`).

Pure, apart from the file :class:`ToolCallLog` appends to.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .errors import redact_server_paths_in

#: The states a step can be in, as the console shows them. ``pending`` is "not started" (a step the
#: run added, or one the run has not reached); ``stopped`` is a step that was running when the run
#: ended without finishing it (Stop, an error).
STEP_STATES = ("pending", "processing", "done", "failed", "skipped", "stopped")

#: The reason a continued run gives for a step it keeps from the run it continues.
KEPT_REASON = "kept from the run this one continues"

#: How many steps one run's record keeps (exploration can add steps; it cannot grow without bound).
STEPS_MAX = 60

#: Tools whose main input is code or a command the model wrote: the run card shows it as code.
CODE_TOOLS = frozenset({"run_code", "run_shell", "run_in_environment"})

#: The longest command the run card is sent (the whole call is in tool_calls.jsonl).
COMMAND_MAX = 20_000


def _flat(text: Any) -> str:
    return " ".join(str(text or "").split())


def step_title(text: str) -> str:
    """A step's title: ``**QC** — run cellqc …`` -> ``QC``; an untitled step's first clause."""
    body = _flat(text)
    if body.startswith("**"):
        end = body.find("**", 2)
        if end > 2:
            return body[2:end].strip()
    for sep in (" — ", " – ", ": ", ". "):
        head, found, _ = body.partition(sep)
        if found and 3 < len(head) <= 90:
            return head.strip()
    return body[:88].rstrip() + ("…" if len(body) > 88 else "")


def step_key(ev: dict[str, Any]) -> str:
    """The one name of a step across its events: the plan document's id, else its number in the
    approved plan, else its text (a step added during the run has neither)."""
    if ev.get("id"):
        return f"id:{ev['id']}"
    if isinstance(ev.get("step"), int):
        return f"n:{ev['step']}"
    return "t:" + _flat(ev.get("text"))[:200]


def merge_step_state(steps: dict[str, dict[str, Any]], update: dict[str, Any]) -> dict[str, Any] | None:
    """Fold one ``step_state`` update into the run's record of that step and return the whole record
    (what the console is sent). An update names its step by ``key`` (a job update) or by the lab's
    fields (id / step / text). None for a state the console does not know, an update for a step not
    yet recorded that carries no state, or past ``STEPS_MAX`` steps."""
    key = str(update.get("key") or "") or step_key(update)
    state = str(update.get("state") or "")
    if state and state not in STEP_STATES:
        return None
    rec = steps.get(key)
    if rec is None:
        if not state or len(steps) >= STEPS_MAX:
            return None
        rec = steps[key] = {"key": key}
    at = update.get("at")
    at = float(at) if isinstance(at, (int, float)) else time.time()
    for field in ("id", "step", "index"):
        if update.get(field) not in (None, ""):
            rec[field] = update[field]
    if update.get("text"):
        rec["text"] = _flat(update["text"])[:600]
        rec["title"] = step_title(update["text"])[:120]
    if state:
        # Each update the lab sends says why the step is where it is now, or nothing: a reason from
        # before ("waiting for your choice") does not outlive it. A job update carries no state.
        rec.pop("reason", None)
        rec["state"] = state
        if state == "processing":
            rec.setdefault("started_at", at)
            rec.pop("ended_at", None)
        elif state in ("done", "failed", "skipped", "stopped"):
            rec["ended_at"] = at
    if update.get("reason"):
        rec["reason"] = _flat(update["reason"])[:300]
    if update.get("job_id"):
        rec["job_id"] = str(update["job_id"])
    rec["updated_at"] = at
    return dict(rec)


def finish_step_states(steps: dict[str, dict[str, Any]], at: float | None = None) -> list[dict[str, Any]]:
    """The run has ended: a step still processing did not finish (Stop, an error, a write-up that was
    skipped). Returns the records that changed."""
    when = at if at is not None else time.time()
    changed = []
    for rec in list(steps.values()):
        if rec.get("state") == "processing":
            rec["state"] = "stopped"
            rec["ended_at"] = rec["updated_at"] = when
            rec.pop("reason", None)
            changed.append(dict(rec))
    return changed


def ordered_steps(steps: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The records in plan order (a step the plan does not hold, after the planned ones)."""
    def order(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        i, rec = item
        n = rec.get("step") if isinstance(rec.get("step"), int) else None
        return (0 if n is not None else 1, n if n is not None else (rec.get("index") or 0), i)
    # A list first: a step running side by side may add its record while a reload reads them.
    return [dict(rec) for _, rec in sorted(enumerate(list(steps.values())), key=order)]


# --- a tool call in words ------------------------------------------------------------------------

#: The inputs that say what a call works on, in the order they are named.
_MAIN_INPUTS = ("dataset", "dataset_path", "input", "input_path", "path", "file", "h5ad", "vcf",
                "reference", "query", "gene", "genes", "species", "groupby", "condition", "contrast",
                "cell_type", "celltype", "resolution", "output", "output_dir", "out_dir", "outdir",
                "environment", "skill")
_SKIP = frozenset({"code", "command", "script"})


def _short_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:g}" if isinstance(value, float) else str(value)
    if isinstance(value, str):
        text = _flat(value)
        if "/" in text and " " not in text:
            text = text.rstrip("/").rsplit("/", 1)[-1] or text   # a path: its last part
        return text if len(text) <= 60 else text[:57].rstrip() + "…"
    if isinstance(value, (list, tuple)):
        items = [_short_value(v) for v in value[:4] if not isinstance(v, (dict, list, tuple))]
        if not items:
            return ""
        more = f" +{len(value) - len(items)}" if len(value) > len(items) else ""
        return ", ".join(items) + more
    return ""


def _code_lines(code: str) -> int:
    return len([line for line in str(code or "").splitlines() if line.strip()])


def tool_call_words(tool: str, args: Any) -> str:
    """A tool call as the chat says it, never its JSON: ``run_cellqc — dataset retina.h5ad ·
    species mouse``; ``run_code — Python, 34 lines``. At most four inputs, the ones that name what
    the call works on first."""
    name = str(tool or "tool")
    a = args if isinstance(args, dict) else {}
    if name in CODE_TOOLS:
        code = str(a.get("code") or a.get("command") or "")
        if name == "run_code":
            what = "Python"
        elif name == "run_shell":
            what = "shell command"
        else:
            what = "code in " + (_short_value(a.get("environment") or a.get("env") or "") or "an environment")
        n = _code_lines(code)
        return f"{name} — {what}" + (f", {n} line{'s' if n != 1 else ''}" if n else "")
    keys = [k for k in _MAIN_INPUTS if k in a] + [k for k in a if k not in _MAIN_INPUTS]
    parts = []
    for k in keys:
        if k in _SKIP:
            continue
        shown = _short_value(a.get(k))
        if not shown:
            continue
        parts.append(f"{k.replace('_', ' ')} {shown}")
        if len(parts) == 4:
            break
    more = len([k for k in keys if k not in _SKIP]) - len(parts)
    return name + (" — " + " · ".join(parts) if parts else "") + (f" · +{more} more" if more > 0 and parts else "")


def command_of(tool: str, args: Any) -> str:
    """The code or command a code tool runs ('' for any other tool)."""
    if str(tool or "") not in CODE_TOOLS or not isinstance(args, dict):
        return ""
    code = str(args.get("code") or args.get("command") or "")
    return code if len(code) <= COMMAND_MAX else code[:COMMAND_MAX] + "\n# … (cut here; the whole call is in process/tool_calls.jsonl)"


# --- the record of every tool call ----------------------------------------------------------------

class ToolCallLog:
    """``process/tool_calls.jsonl``: one JSON line per tool call of the run — the tool, its arguments
    as the tool received them, the step it ran for, and how it ended (its result summary or error).
    A call is written when it ends; one still open when the run ends is written by :meth:`close` as
    ``unfinished``. Calls of steps that run side by side are told apart by their thread. Paths on
    this host are cut to their file name, as everywhere a user reads (issue 37). Best-effort: a
    record that cannot be written never stops the run."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._open: dict[int, list[dict[str, Any]]] = {}
        # A continued run writes into the same results: its calls are numbered on from the last one.
        try:
            self._n = sum(1 for _ in self.path.open(encoding="utf-8")) if self.path.is_file() else 0
        except OSError:
            self._n = 0

    def _write(self, rec: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                # This host's paths are not shown to users (issue 37); HPC3 paths stay.
                fh.write(json.dumps(redact_server_paths_in(rec), default=str, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            print(f"[tool-calls] not written: {type(exc).__name__}: {exc}")

    def observe(self, ev: dict[str, Any], *, step: str = "", by: str = "") -> None:
        """Follow one lab event (``tool_start`` / ``tool_result`` / ``tool_error``, or a meeting
        expert's ``expert_tool``). ``step`` is the step the calling thread works on."""
        t = ev.get("type")
        if t == "expert_tool":
            t = {"start": "tool_start", "done": "tool_result", "error": "tool_error"}.get(str(ev.get("state")))
            by = by or str(ev.get("member") or "")
            ev = {**ev, "summary": ev.get("detail"), "error": ev.get("detail")}
        if t not in ("tool_start", "tool_result", "tool_error"):
            return
        tid = threading.get_ident()
        now = time.time()
        with self._lock:
            stack = self._open.setdefault(tid, [])
            if t == "tool_start":
                self._n += 1
                rec: dict[str, Any] = {"call": self._n, "tool": ev.get("tool"), "started_at": now}
                if step:
                    rec["step"] = step
                if by:
                    rec["by"] = by
                rec["args"] = ev.get("args") if isinstance(ev.get("args"), dict) else {}
                if ev.get("provider"):
                    rec["provider"] = ev.get("provider")
                stack.append(rec)
                return
            i = next((k for k in range(len(stack) - 1, -1, -1)
                      if stack[k].get("tool") == ev.get("tool")), None)
            if i is not None:
                rec = stack.pop(i)
            else:
                # An error before the call started (a refused or malformed call): it has no start.
                self._n += 1
                rec = {"call": self._n, "tool": ev.get("tool"), "started_at": now}
                if step:
                    rec["step"] = step
                if by:
                    rec["by"] = by
                rec["args"] = ev.get("args") if isinstance(ev.get("args"), dict) else {}
        rec["ended_at"] = now
        if t == "tool_result":
            rec["status"] = "cancelled" if ev.get("timing_outcome") == "cancelled" else "ok"
            rec["summary"] = str(ev.get("summary") or "")[:4000]
        else:
            rec["status"] = "error"
            rec["error"] = str(ev.get("error") or "")[:4000]
        with self._lock:
            self._write(rec)

    def close(self) -> None:
        """Write the calls that never ended (the run stopped or failed while they ran)."""
        with self._lock:
            left = [rec for stack in self._open.values() for rec in stack]
            self._open.clear()
            for rec in sorted(left, key=lambda r: r.get("call") or 0):
                self._write({**rec, "status": "unfinished"})
