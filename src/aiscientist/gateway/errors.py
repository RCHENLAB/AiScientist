from __future__ import annotations

import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ``src/`` of this checkout: traceback frames under it read as ``aiscientist/gateway/app.py``.
_SRC_DIR = Path(__file__).resolve().parents[2]

_FRAME_FILE = re.compile(r'File "([^"]+)"')
# What may follow a server root in a path: anything up to whitespace, a quote or punctuation that
# closes a path in prose. A root must end there, at a "/" or at a sentence's full stop (so /srv/app
# never matches /srv/app2 or /srv/app.bak).
_PATH_TAIL = r"(?![\w-]|\.[\w.-])(?:/[^\s'\"`<>|;,()\[\]{}]*)?"


def _server_roots() -> list[str]:
    """Directories of THIS host whose paths a user must not see: the code checkout, the Python
    install and its import paths, the working directory, the service account's home, and the
    state, results and data roots. Longest first, so the most specific root wins."""
    found: list[Path] = [_SRC_DIR.parent, Path(sys.prefix), Path(sys.exec_prefix), Path.home()]
    found += [Path(p) for p in sys.path if p]
    try:
        found.append(Path.cwd())
    except OSError:   # the working directory was removed
        pass
    for var in ("AISCIENTIST_STATE_DIR", "AISCIENTIST_RESULTS_DIR", "AISCIENTIST_DATA_ROOT",
                "DATA_ROOT"):
        value = os.environ.get(var, "").strip()
        if value:
            found.append(Path(value).expanduser())
    roots: set[str] = set()
    for path in found:
        if not path.is_absolute():
            continue
        try:
            forms = (path, path.resolve())
        except (OSError, RuntimeError):
            forms = (path,)
        for form in forms:
            # "/", "/usr", "/tmp" or "/home" would swallow every path in a message, HPC3's included.
            if len(form.parts) >= 3:
                roots.add(str(form).rstrip("/"))
    return sorted(roots, key=len, reverse=True)


def _short_frame(match: re.Match) -> str:
    """A traceback frame's file as the module path a developer needs (``aiscientist/x.py``,
    ``paramiko/transport.py``), without the directories it is installed in."""
    name = match.group(1)
    bases = sorted({str(_SRC_DIR), *(p for p in sys.path if p and os.path.isabs(p))},
                   key=len, reverse=True)
    for base in bases:
        base = base.rstrip("/")
        if name.startswith(base + "/"):
            return f'File "{name[len(base) + 1:]}"'
    return match.group(0)


def redact_server_paths(text: str) -> str:
    """``text`` without this host's paths, for anything a user can see (issue 37).

    Error messages and tracebacks carry the paths of the files they failed on — the key store,
    the state dir, the code checkout — which tell a user nothing and show everyone the server's
    layout. A traceback frame keeps its module path; any other server path keeps only its file
    name (``…/run_state.json``), which is all a user can act on. Paths on HPC3 are left alone:
    they are the user's own.
    """
    if not text:
        return text
    text = _FRAME_FILE.sub(_short_frame, text)
    for root in _server_roots():
        text = re.sub(re.escape(root) + _PATH_TAIL,
                      lambda m: "…/" + m.group(0).rstrip("/").rsplit("/", 1)[-1]
                      if len(m.group(0).rstrip("/")) > len(root) else "…", text)
    return text


def public_error(exc: BaseException) -> str:
    """An exception's text as a user may see it: no server paths, never empty."""
    return redact_server_paths(str(exc)) or exc.__class__.__name__


class GatewayError(RuntimeError):
    """Gateway-level failure that carries a user-facing reason plus full detail.

    The whole point of this class is that *nothing is swallowed*: the message is
    short and human-readable, while ``detail`` keeps the underlying command,
    stderr, exit status, or chained exception so the UI can print the complete
    cause on demand.
    """

    def __init__(self, message: str, *, stage: str = "", detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.detail = detail


class VLLMNetworkError(GatewayError):
    """The vLLM /v1 endpoint was unreachable THROUGH THE SESSION TUNNEL (connection
    refused/reset/timeout) — as opposed to a valid HTTP error the server returned.

    This is the recoverable case: the SSH tunnel was reaped while idle, or the GPU
    serve job hit its Slurm ``--time`` limit and was killed, leaving the tunnel
    forwarding to a dead port. Callers can catch THIS specifically to re-establish the
    serve job + tunnel and retry, without also catching genuine model errors (context
    overflow, bad request) which share ``stage="vllm_chat"``.
    """


@dataclass
class CommandFailure:
    """Structured record of a failed remote command, with everything captured."""

    command: str
    exit_status: int
    stdout: str
    stderr: str
    duration_ms: int = 0
    hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exit_status": self.exit_status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "hint": self.hint,
        }


def error_detail(exc: BaseException) -> dict[str, Any]:
    """Render any exception into a JSON-serializable, fully-printed detail block.

    It reaches the browser (events, error responses, the run's event log), so this host's paths
    are taken out of the message and the traceback; the frames keep their module path and line.
    """

    detail: dict[str, Any] = {
        "type": exc.__class__.__name__,
        "message": public_error(exc),
        "traceback": redact_server_paths("".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )),
    }
    if isinstance(exc, GatewayError):
        detail["stage"] = exc.stage
        if isinstance(exc.detail, CommandFailure):
            detail["command_failure"] = exc.detail.as_dict()
        elif exc.detail is not None:
            detail["detail"] = exc.detail
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        detail["cause"] = {
            "type": cause.__class__.__name__,
            "message": public_error(cause),
        }
    return detail


@dataclass
class GatewayEvent:
    """A single timestamped event streamed to the console log panel."""

    level: str  # "info" | "success" | "warning" | "error" | "step" | "stream"
    stage: str
    message: str
    detail: Any = None
    created_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "level": self.level,
            "stage": self.stage,
            "message": self.message,
            "created_at": self.created_at,
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        if self.extra:
            payload["extra"] = self.extra
        return payload
