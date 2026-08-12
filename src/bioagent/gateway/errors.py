from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any


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
    """Render any exception into a JSON-serializable, fully-printed detail block."""

    detail: dict[str, Any] = {
        "type": exc.__class__.__name__,
        "message": str(exc),
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
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
            "message": str(cause),
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
