"""Small loop-agnostic helpers used by the research harness/lab: a tolerant JSON
parser, a thread pool that keeps the caller's context, and a heartbeat checkpoint writer
(start/stop/log + a stall marker). Extracted from the retired eval autonomous-loop so the
live agents don't depend on legacy code.
"""

from __future__ import annotations

import contextvars
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    """A ``ThreadPoolExecutor`` whose tasks run in a copy of the submitting thread's context.

    A plain pool starts its workers with an empty context, so whatever the caller's context says
    is lost inside them — on the gateway, WHICH RUN the work is for, when several chats each have
    a run in flight on one session. Parallel steps and expert calls then emitted events and
    recorded model usage under another chat's run. ``map`` goes through ``submit``, so it is
    covered too."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


def safe_json_loads(text: str) -> dict[str, Any] | None:
    """Parse a JSON object out of model text, tolerating ```json fences. Returns the
    dict, or None if it isn't valid JSON / isn't an object."""
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class CheckpointWriter:
    """Heartbeat + structured log for a long-running agent run. Writes ``loop.log``
    (JSONL) and, if no checkpoint is logged within ``checkpoint_seconds``, a
    ``checkpoint_stalled.md`` marker so a hung model/tool call is visible. Loop-agnostic
    (no coupling to any specific run state)."""

    def __init__(self, workspace: Path, checkpoint_seconds: float) -> None:
        self.workspace = Path(workspace)
        self.checkpoint_seconds = checkpoint_seconds
        self.log_path = self.workspace / "loop.log"
        self.last_checkpoint_time = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.log("loop_started")
        if self.checkpoint_seconds > 0:
            self._thread = threading.Thread(target=self._heartbeat, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.log("loop_stopped")

    def log(self, message: str, **metadata: object) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        record = {"time": time.time(), "message": message, **metadata}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        self.last_checkpoint_time = time.monotonic()

    def _heartbeat(self) -> None:
        while not self._stop.wait(min(self.checkpoint_seconds, 60)):
            elapsed = time.monotonic() - self.last_checkpoint_time
            self.log("heartbeat", seconds_since_checkpoint=round(elapsed, 3))
            if elapsed >= self.checkpoint_seconds:
                (self.workspace / "checkpoint_stalled.md").write_text(
                    "# Checkpoint Stalled\n\n"
                    f"No checkpoint has been logged for `{elapsed:.1f}` seconds. The run may be "
                    "inside a long model/tool call. Existing artifacts remain usable.\n",
                    encoding="utf-8",
                )
