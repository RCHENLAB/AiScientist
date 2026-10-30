from __future__ import annotations

import json
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
    install and its import paths, the working directory, the service account's home, the folder
    of the settings file the app reads (``.env``: in a release, a link into the deployment's
    ``config/``), and the state, results and data roots. Longest first, so the most specific root
    wins."""
    found: list[Path] = [_SRC_DIR.parent, Path(sys.prefix), Path(sys.exec_prefix), Path.home()]
    found += [Path(p) for p in sys.path if p]
    app_dirs = [_SRC_DIR.parent]
    try:
        app_dirs.append(Path.cwd())
    except OSError:   # the working directory was removed
        pass
    found += app_dirs
    for base in app_dirs:
        try:
            env = base / ".env"
            if env.is_file():
                found.append(env.resolve().parent)
        except (OSError, RuntimeError):
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


# --- secrets in text -------------------------------------------------------------------------------

# Key-shaped tokens a provider may echo back in an error body even when it is not the key we sent
# (a masked form, a key from another request, an Authorization header copied verbatim).
_KEY_SHAPED = re.compile(
    r"(?i)(?:\bBearer\s+[A-Za-z0-9._~+/=*-]{8,}"
    r"|\b(?:sk|rk|pk)-[A-Za-z0-9_*-]{8,}"
    r"|\bAIza[0-9A-Za-z_-]{20,}"
    r"|\b(?:x-api-key|api[_-]?key|authorization)[\"']?\s*[:=]\s*[\"']?(?!Bearer\b)[^\s\"',}]{8,})")
# The shortest piece of a key that is replaced when it appears on its own: long enough that it
# cannot be an ordinary word, short enough to catch a provider that prints a key's first 20 chars.
_KEY_PIECE = 12


def scrub_secrets(text: Any, *secrets: str | None) -> str:
    """``text`` with every API key in it replaced by ``<key>``: the given ``secrets`` whole, any
    run of 12 or more of their characters, and anything key-shaped (``sk-…``, ``Bearer …``,
    ``api_key=…``).

    For a provider's error body before it goes into an error message, an event, the run's log or
    the browser. Some gateways copy the ``Authorization`` header into a 4xx body, others print a
    masked key or its first characters; none of that may travel further than the HTTP layer
    (RCHENLAB/AiScientist issue 16).
    """
    text = "" if text is None else str(text)
    if not text:
        return text
    for secret in secrets:
        secret = (secret or "").strip()
        if len(secret) < 8:
            continue
        text = text.replace(secret, "<key>")
        if len(secret) > _KEY_PIECE:
            pieces = {secret[i:i + _KEY_PIECE] for i in range(len(secret) - _KEY_PIECE + 1)}
            hits = [p for p in pieces if p in text]
            if hits:
                # Grow each hit to the longest run of the key it belongs to, so "<key>" replaces
                # the whole fragment rather than leaving its ends behind.
                pattern = "|".join(re.escape(p) for p in sorted(hits, key=len, reverse=True))
                text = re.sub(rf"(?:{pattern})+[{re.escape(secret)}]*", "<key>", text)
    return _KEY_SHAPED.sub("<key>", text)


def _key_store_roots() -> list[str]:
    """The API-key and SSH-key stores and the API keys' master key, as path strings (absolute and
    resolved): where the keys are kept, which no log or message may name."""
    roots: set[str] = set()
    try:
        from . import llm_credentials, ssh_credentials
        paths = [*llm_credentials.private_paths(), *ssh_credentials.private_paths()]
    except Exception:  # noqa: BLE001 - a redaction must never fail the message it protects
        return []
    for path in paths:
        try:
            forms = {path.absolute(), path.resolve()}
        except (OSError, RuntimeError):
            forms = {path}
        roots.update(str(p).rstrip("/") for p in forms if str(p) not in ("", "/", "."))
    return sorted(roots, key=len, reverse=True)


def redact_key_store_paths(text: str) -> str:
    """``text`` with every path inside a key store replaced by ``<key store>``: not even the file
    name is kept, because a key file's name is the credential's id."""
    if not text:
        return text
    for root in _key_store_roots():
        text = re.sub(re.escape(root) + _PATH_TAIL, "<key store>", text)
    return text


def redact_server_paths(text: str) -> str:
    """``text`` without this host's paths, for anything a user can see (issue 37).

    Error messages and tracebacks carry the paths of the files they failed on — the key store,
    the state dir, the code checkout — which tell a user nothing and show everyone the server's
    layout. A traceback frame keeps its module path; any other server path keeps only its file
    name (``…/run_state.json``), which is all a user can act on. Paths on HPC3 are left alone:
    they are the user's own. (A run's log goes further: :func:`redact_key_store_paths` first.)
    """
    if not text or "/" not in text:
        return text
    return _redact_paths(text, _server_roots())


def _redact_paths(text: str, roots: list[str]) -> str:
    if not text or "/" not in text:
        return text
    text = _FRAME_FILE.sub(_short_frame, text)
    for root in roots:
        text = re.sub(re.escape(root) + _PATH_TAIL,
                      lambda m, root=root: "…/" + m.group(0).rstrip("/").rsplit("/", 1)[-1]
                      if len(m.group(0).rstrip("/")) > len(root) else "…", text)
    return text


def redact_server_paths_in(value: Any) -> Any:
    """``value`` with :func:`redact_server_paths` applied to every string in it, through dicts,
    lists and tuples (a dict's keys are kept): for a JSON reply or an event's detail. Anything else
    is returned as it is."""
    roots = _server_roots()

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            return _redact_paths(item, roots)
        if isinstance(item, dict):
            return {key: walk(sub) for key, sub in item.items()}
        if isinstance(item, list):
            return [walk(sub) for sub in item]
        if isinstance(item, tuple):
            return tuple(walk(sub) for sub in item)
        return item

    return walk(value)


def public_error(exc: BaseException) -> str:
    """An exception's text as a user may see it: no server paths, never empty."""
    return redact_server_paths(str(exc)) or exc.__class__.__name__


# --- error replies -----------------------------------------------------------------------------------

def _media_type(headers: list[tuple[bytes, bytes]]) -> tuple[str, bool]:
    """A response's media type (lower case, without parameters) and whether its body is encoded."""
    media, encoded = "", False
    for name, value in headers:
        name = name.lower()
        if name == b"content-type":
            media = value.decode("latin-1").split(";", 1)[0].strip().lower()
        elif name == b"content-encoding":
            encoded = value.strip().lower() not in (b"", b"identity")
    return media, encoded


class RedactErrorReplies:
    """ASGI middleware: no error reply carries a path of this host (RCHENLAB/AiScientist issue 71).

    A route that fails answers ``{"error": str(exc)}``, and the console shows that text in a dialog;
    an exception about a file on this server names its folder (``/data/…/users/<account>/datasets/
    <uuid>/…``). Here, below every route, the JSON body of each reply with status 400 or more goes
    through :func:`redact_server_paths_in`, so a path keeps only its file name and a new handler
    cannot skip the rule. HPC3 paths are left as they are. When a reply is changed, the original
    goes to the server's log (``print``: the unit's journal), with key-shaped tokens taken out.
    Every other response, a streamed one included, passes through untouched."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        held: dict | None = None
        chunks: list[bytes] = []

        async def send_public(message: dict) -> None:
            nonlocal held
            kind = message.get("type")
            if kind == "http.response.start":
                media, encoded = _media_type(list(message.get("headers") or []))
                if (int(message.get("status") or 0) >= 400 and not encoded
                        and (media == "application/json" or media.endswith("+json"))):
                    held = message
                    return
            elif kind == "http.response.body" and held is not None:
                chunks.append(bytes(message.get("body") or b""))
                if message.get("more_body"):
                    return
                start, held = held, None
                original = b"".join(chunks)
                chunks.clear()
                body = self._public_body(original, scope, int(start.get("status") or 0))
                if body is not original:
                    headers = [(k, v) for k, v in (start.get("headers") or [])
                               if k.lower() != b"content-length"]
                    start = {**start, "headers": [*headers, (b"content-length", str(len(body)).encode("latin-1"))]}
                await send(start)
                await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        await self.app(scope, receive, send_public)

    @staticmethod
    def _public_body(body: bytes, scope: dict, status: int) -> bytes:
        """``body`` itself when it has no path of this host (or is not JSON), else the filtered copy."""
        try:
            content = json.loads(body)
        except ValueError:   # not JSON after all: sent as it is
            return body
        public = redact_server_paths_in(content)
        if public == content:
            return body
        print(f"[http] {scope.get('method', '')} {scope.get('path', '')} answered {status}; the reply "
              f"lost this host's paths, it was: {scrub_secrets(body.decode('utf-8', 'replace'))[:4000]}")
        return json.dumps(public, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


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
