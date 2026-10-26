"""A per-user record of what happened to each upload.

An upload passes through the browser, this gateway and HPC3, and a failure in any of them used to
leave nothing on the server: the only trace was a message in a browser tab (RCHENLAB/AiScientist
issue 1: an upload "stopped without displaying an error message", and nothing on the server said
where it had got to). Every step is now one JSON line in ``<workspace>/uploads/.upload.log``, and
the same line on stdout, so it is in the service journal too::

    journalctl -u aiscientist.service | grep '\\[upload\\]'

Standard library only; a log that cannot be written never fails the upload.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_NAME = ".upload.log"
_MAX_BYTES = 5 * 1024 * 1024        # then the file becomes ``.upload.log.1`` (one generation kept)
_MAX_TEXT = 500                     # of any one text field: an error message, not a traceback
_lock = threading.Lock()
# job id -> (state, error, monotonic time the copy started): a copy reports progress every second,
# and only a change of state belongs in the log.
_staging_seen: dict[str, tuple[str, str, float]] = {}


def log_path(workspace: "str | Path") -> Path:
    return Path(workspace) / "uploads" / LOG_NAME


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "…"
    if isinstance(value, float):
        return round(value, 3)
    return value


def record(workspace: "str | Path", event: str, **fields: Any) -> dict:
    """Append one event for the user whose workspace this is. Empty fields are left out."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "owner": Path(workspace).name, "event": event,
             # `0 in (None, "", False)` is true: an offset of 0 is a value, so test by identity
             **{k: _clean(v) for k, v in fields.items()
                if v is not None and v is not False and not (isinstance(v, str) and not v)}}
    line = json.dumps(entry, ensure_ascii=False, default=str)
    try:
        path = log_path(workspace)
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.stat().st_size > _MAX_BYTES:
                    os.replace(path, path.with_name(LOG_NAME + ".1"))
            except OSError:
                pass
            with path.open("a", encoding="utf-8") as out:
                out.write(line + "\n")
    except OSError:
        pass
    print(f"[upload] {line}", flush=True)
    return entry


def record_staging(workspace: "str | Path", job: Any) -> "dict | None":
    """Log a copy to HPC3 when its state (or the reason it is waiting) changes; None otherwise."""
    state, error = str(job.state), str(job.error or "")
    now = time.monotonic()
    with _lock:
        seen = _staging_seen.get(job.id)
        if seen is not None and seen[:2] == (state, error):
            return None
        started = now if state == "pushing" or seen is None else seen[2]
        if state in ("done", "failed", "cancelled"):
            _staging_seen.pop(job.id, None)
        else:
            _staging_seen[job.id] = (state, error, started)
    extra: dict[str, Any] = {}
    if state == "verifying" and seen is not None and seen[0] == "pushing" and now > started:
        extra["copy_seconds"] = now - started
        extra["copy_mb_s"] = job.size / 1e6 / (now - started)
    if state in ("done", "failed", "cancelled"):
        extra["total_seconds"] = max(0.0, time.time() - float(job.created_at or time.time()))
    return record(workspace, f"copy_{state}", job_id=job.id, upload_id=job.upload_id, name=job.name,
                  folder=job.folder, size=job.size, sent=job.sent, attempts=job.attempts,
                  verified=job.verified, remote_path=job.remote_path if state == "done" else "",
                  unpacked=(job.unpacked or {}).get("path"), unpack_error=job.unpack_error,
                  error=error, **extra)


def tail(workspace: "str | Path", limit: int = 200) -> list[dict]:
    """The newest ``limit`` events, oldest first."""
    try:
        lines = log_path(workspace).read_text(encoding="utf-8").splitlines()[-max(1, limit):]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
