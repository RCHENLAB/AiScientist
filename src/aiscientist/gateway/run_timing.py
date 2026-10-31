"""Small append-only timing journal used by RunMonitor.

Only transitions are written, never heartbeat/poll traffic. Each append is closed immediately;
complete lines survive process exit and readers ignore a torn final line. Open spans stay unknown
on recovery. Durations overlap and must not be summed as a run's wall time. This operational
journal contains identifiers and timestamps, not research data, prompts, paths or commands.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

_LOG = logging.getLogger(__name__)
_CURRENT: contextvars.ContextVar[Any] = contextvars.ContextVar("run_timing_monitor", default=None)
_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_META = frozenset({"role", "tool", "action", "job_id", "state", "source", "backend", "reason_code",
                   "duration_basis", "observation", "attempt", "kind", "phase"})
_CATEGORIES = {"processing", "user_wait", "observation"}
_BUFFER_LIMIT = 4096


def _identifier(value: Any, fallback: str = "unknown") -> str:
    value = str(value or "")
    return value if _IDENTIFIER.fullmatch(value) else fallback


def _metadata(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if k in _META
            and ((isinstance(v, str) and _IDENTIFIER.fullmatch(v))
                 or (type(v) in (int, float, bool)))}


def _iso(value: float | None) -> str | None:
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _path_lock(path: Path) -> Any:
    key = str(path.absolute())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def enable_journal_logging() -> None:
    """Install one INFO stderr handler when the gateway opts in (journald captures stderr)."""
    with _LOCKS_GUARD:
        if not any(getattr(h, "_run_timing", False) for h in _LOG.handlers):
            handler = logging.StreamHandler()
            handler._run_timing = True
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            _LOG.addHandler(handler)
        _LOG.setLevel(logging.INFO)
        _LOG.propagate = False


def current_monitor() -> Any:
    return _CURRENT.get()


def set_current_monitor(monitor: Any) -> contextvars.Token:
    return _CURRENT.set(monitor)


def reset_current_monitor(token: contextvars.Token) -> None:
    _CURRENT.reset(token)


@contextmanager
def bind_monitor(monitor: Any) -> Iterator[Any]:
    token = set_current_monitor(monitor)
    try:
        yield monitor
    finally:
        reset_current_monitor(token)


class Span:
    """A best-effort span handle; explicit end permits fallback/unknown outcomes."""
    def __init__(self, timing: Any = None, ident: str = "") -> None:
        self.timing, self.id = timing, ident

    def end(self, outcome: str = "success", **metadata: Any) -> None:
        if self.timing is not None and self.id:
            self.timing.end_span(self.id, outcome, **metadata)


@contextmanager
def timed_stage(stage: str, category: str = "processing", **metadata: Any) -> Iterator[Span]:
    monitor = current_monitor()
    handle = Span(monitor, monitor.begin_span(stage, category=category, **metadata)) if monitor else Span()
    try:
        yield handle
    except BaseException as exc:
        handle.end(exception_outcome(exc))
        raise
    else:
        handle.end()


def exception_outcome(exc: BaseException) -> str:
    return "cancelled" if type(exc).__name__ in {"CancelledError", "TransferCancelled", "Cancelled"} else "failed"


class RunTiming:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.run_id = ""
        self.request_id = ""
        self.segment_id = uuid4().hex[:16]
        self.conversation_id: str | None = None
        self._lock = threading.RLock()
        self._file_lock: Any = None
        self._buffer: list[dict[str, Any]] = []
        self._active: dict[str, dict[str, Any]] = {}
        self._dropped = 0

    def bind(self, path: str | Path, run_id: str | None, conversation_id: str | None) -> None:
        try:
            with self._lock:
                path = Path(path)
                if self.path == path:
                    return
                if self.path is not None:
                    self._rebind(path, _identifier(run_id), conversation_id)
                    return
                self.run_id = _identifier(run_id)
                self.request_id = self.run_id
                self.conversation_id = _identifier(conversation_id) if conversation_id is not None else None
                self.path, self._file_lock = path, _path_lock(path)
                pending, self._buffer = self._buffer, []
                with self._file_lock:
                    if not path.exists():
                        self._write({"event": "run_started", "timestamp": time.time(), "schema_version": 1,
                                     "conversation_id": self.conversation_id})
                    for event in pending:
                        self._write(event)
                    if self._dropped:
                        self._write({"event": "buffer_truncated", "timestamp": time.time(),
                                     "dropped_events": self._dropped})
        except Exception:
            # Storage failure never changes the application's result.
            pass

    def _rebind(self, path: Path, run_id: str, conversation_id: str | None) -> None:
        """Attach early routing to the existing run it continues, keeping the original evidence.

        Only this monitor's segment is copied; earlier continuations already in the old artifact
        are never duplicated. Relinking is confined to run folders of the same workspace owner.
        """
        old_path = self.path
        if (old_path is None or tuple(old_path.parts[-3:]) != ("artifacts", "process", "timing.jsonl")
                or tuple(path.parts[-3:]) != ("artifacts", "process", "timing.jsonl")
                or old_path.parents[3].resolve() != path.parents[3].resolve()):
            return
        records = []
        with self._file_lock:
            try:
                with old_path.open() as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if (isinstance(event, dict) and event.get("segment_id") == self.segment_id
                                and event.get("event") not in {"run_started", "run_link", "request_attached"}):
                            records.append(event)
            except OSError:
                pass
            self._write({"event": "run_link", "timestamp": time.time(), "target_run_id": run_id})
        self.path, self.run_id, self._file_lock = path, run_id, _path_lock(path)
        self.conversation_id = _identifier(conversation_id) if conversation_id is not None else None
        with self._file_lock:
            if not path.exists():
                self._write({"event": "run_started", "timestamp": time.time(), "schema_version": 1,
                             "conversation_id": self.conversation_id})
            self._write({"event": "request_attached", "timestamp": time.time()})
            for event in records:
                self._write(event)

    def _write(self, event: dict[str, Any]) -> None:
        event = {**event, "run_id": self.run_id, "request_id": self.request_id, "segment_id": self.segment_id}
        if self.path is None:
            if len(self._buffer) < _BUFFER_LIMIT:
                self._buffer.append(event)
            else:
                self._dropped += 1
            return
        try:
            with self._file_lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # A crashed writer may leave a partial line. Separate the next append so it remains readable.
                with self.path.open("a+b") as stream:
                    stream.seek(0, 2)
                    if stream.tell():
                        stream.seek(-1, 2)
                        if stream.read(1) != b"\n":
                            stream.write(b"\n")
                    stream.write((json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n").encode())
            # The journal has the timestamps/details; production logs contain only safe correlation fields.
            _LOG.info("run_timing run_id=%s stage=%s event=%s action=%s outcome=%s",
                      self.run_id, _identifier(event.get("stage"), "-"), _identifier(event.get("event"), "-"),
                      _identifier(event.get("metadata", {}).get("action"), "-"),
                      _identifier(event.get("outcome", event.get("metadata", {}).get("state")), "-"))
        except Exception:
            pass

    def event(self, event: str, **metadata: Any) -> None:
        try:
            with self._lock:
                self._write({"event": _identifier(event), "timestamp": time.time(), "metadata": _metadata(metadata)})
        except Exception:
            pass

    def begin_span(self, stage: str, category: str = "processing", **metadata: Any) -> str:
        try:
            with self._lock:
                ident = uuid4().hex[:16]
                event = {"event": "span_start", "span_id": ident, "stage": _identifier(stage),
                         "category": category if category in _CATEGORIES else "observation",
                         "timestamp": time.time(), "metadata": _metadata(metadata)}
                self._active[ident] = {**event, "monotonic": time.monotonic()}
                self._write(event)
                return ident
        except Exception:
            return ""

    def end_span(self, ident: str, outcome: str = "success", **metadata: Any) -> None:
        try:
            with self._lock:
                start = self._active.pop(ident, None)
                if start is None:
                    return
                now = time.time()
                mono = start.get("monotonic")
                elapsed = max(0., time.monotonic() - mono) if mono is not None else max(0., now - start["timestamp"])
                basis = "monotonic" if mono is not None else "wall_clock"
                self._write({"event": "span_end", "span_id": ident, "stage": start["stage"],
                             "timestamp": now, "duration_seconds": elapsed, "outcome": _identifier(outcome),
                             "metadata": {**_metadata(metadata), "duration_basis": basis,
                                          **({"source": "restored_wall_clock"} if mono is None else {})}})
        except Exception:
            pass

    def record_span(self, stage: str, *, started_at: float | None, ended_at: float | None,
                    duration_seconds: float | None = None, category: str = "observation",
                    outcome: str = "unknown", **metadata: Any) -> None:
        try:
            with self._lock:
                duration = duration_seconds
                if duration is None and started_at is not None and ended_at is not None and ended_at >= started_at:
                    duration = ended_at - started_at
                self._write({"event": "span_record", "span_id": uuid4().hex[:16], "stage": _identifier(stage),
                             "category": category if category in _CATEGORIES else "observation",
                             "timestamp": time.time(), "started": started_at, "ended": ended_at,
                             "duration_seconds": duration, "outcome": _identifier(outcome),
                             "metadata": _metadata(metadata)})
        except Exception:
            pass

    def finish(self, outcome: str) -> None:
        try:
            with self._lock:
                pending_review = False
                for ident, span in list(self._active.items()):
                    if span["stage"] == "user_review" and outcome == "ended":
                        pending_review = True
                        continue
                    # Run completion/disconnect does not prove that background compute stopped.
                    # Keep these spans open until their actual call finally returns, or unknown after a crash.
                    if span["stage"] == "user_review":
                        self.end_span(ident, outcome if outcome in {"cancelled", "stopped", "failed"} else "unknown")
                self.event("run_ended", state="awaiting_review" if pending_review else outcome)
        except Exception:
            pass

    def decision(self, action: str) -> None:
        try:
            with self._lock:
                # Saved plan approval can arrive after this monitor, or the process, has ended.
                if self.path:
                    for span in read_timing(self.path)["spans"]:
                        if span["stage"] == "user_review" and span["partial"] and span["span_id"] not in self._active:
                            self._active[span["span_id"]] = {"stage": "user_review", "timestamp": span["started"],
                                                            "monotonic": None}
                for ident, span in list(self._active.items()):
                    if span["stage"] == "user_review":
                        self.end_span(ident, "cancelled" if action == "cancel" else "timeout" if action == "timeout" else "success", action=action)
                self.event("plan_decision", action=action)
        except Exception:
            pass

    def save_requested(self, action: str = "manual") -> None:
        try:
            with self._lock:
                timeline = read_timing(self.path) if self.path else {"events": self._buffer}
                ready = [ev for ev in timeline["events"] if ev.get("event") == "result_ready"]
                now = time.time()
                if ready:
                    self.record_span("save_decision_wait", started_at=ready[-1]["timestamp"], ended_at=now,
                                     category="user_wait", outcome="success", action=action,
                                     duration_basis="wall_clock", source="result_ready")
                self.event("save_requested", action=action)
        except Exception:
            pass


def read_timing(path: str | Path | None) -> dict[str, Any]:
    """Reconstruct a timeline. Missing ends remain unknown, with no invented duration."""
    records: list[dict[str, Any]] = []
    ignored = 0
    if path is not None:
        path = Path(path)
        try:
            with _path_lock(path), path.open() as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict):
                            records.append(record)
                    except (ValueError, TypeError):
                        ignored += 1
        except OSError:
            pass
    spans: dict[str, dict[str, Any]] = {}
    events = []
    for ev in records:
        ident = ev.get("span_id")
        if ev.get("event") in {"span_start", "span_record"}:
            recorded = ev["event"] == "span_record"
            start = ev.get("started") if recorded else ev.get("timestamp")
            end = ev.get("ended") if recorded else None
            spans[ident] = {"span_id": ident, "stage": ev.get("stage"), "category": ev.get("category"),
                            "run_id": ev.get("run_id"), "request_id": ev.get("request_id"),
                            "segment_id": ev.get("segment_id"),
                            "started": start, "ended": end, "started_at": _iso(start), "ended_at": _iso(end),
                            "duration_seconds": ev.get("duration_seconds") if recorded else None,
                            "outcome": ev.get("outcome", "unknown"), "partial": not recorded,
                            "metadata": dict(ev.get("metadata") or {})}
        elif ev.get("event") == "span_end" and ident in spans:
            spans[ident].update(ended=ev.get("timestamp"), ended_at=_iso(ev.get("timestamp")),
                               duration_seconds=ev.get("duration_seconds"), outcome=ev.get("outcome", "unknown"),
                               partial=False)
            spans[ident]["metadata"].update(ev.get("metadata") or {})
        else:
            events.append({**ev, "at": _iso(ev.get("timestamp"))})
    header = next((ev for ev in records if ev.get("event") == "run_started"), {})
    return {"schema_version": 1, "run_id": header.get("run_id"), "conversation_id": header.get("conversation_id"),
            "spans": list(spans.values()), "events": events, "partial": any(s["partial"] for s in spans.values()),
            "ignored_lines": ignored, "durations_overlap": True,
            "note": "Spans can overlap; do not sum durations as wall time. Open spans have unknown outcomes and durations."}
