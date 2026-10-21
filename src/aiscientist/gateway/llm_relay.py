"""The model calls of an HPC3 job, made here with the user's own API key.

A user's key never leaves the Eye Server: no job on HPC3 is given one (``_build_literature_executor``).
A job that needs the user's API model — the corpus scan on a session that runs on its own key —
writes each prompt as a JSON file under ``<relay>/req/`` on dfs3b; :class:`ModelRelay` lists that
folder over SFTP on the transfer host, calls the model, records the call against the user's usage
and budget, and writes the reply to ``<relay>/resp/<same name>``. Both sides write a temporary name
and rename it into place, so neither reads half a file. The job side is
``extract_haploinsufficient_genes.tool.relay_judge_fn``.

A reply is ``{"text", "usage", "cost_usd"}``, or ``{"error", "kind"}`` where ``kind`` tells the job
what to do: ``"stop"`` ends the scan (the spending cap is reached, the run was stopped, or the key is
refused / out of credit / the model unknown — no later call would fare better), ``"transport"``
counts toward the job's dead-endpoint streak (the API stayed unreachable or rate-limited through this
side's own retries), anything else fails that one prompt, which the job retries.
"""

from __future__ import annotations

import json
import posixpath
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

REQ, RESP = "req", "resp"

# Causes (``anthropic_client.classify_error``) after which no further call can succeed.
_FATAL = {"auth", "credit", "model"}
# Causes worth waiting out here before the job hears about them.
_RETRYABLE = {"rate", "endpoint", "network"}
_RETRY_WAITS = (5.0, 15.0, 30.0)


class RelayFS(Protocol):
    def listdir(self, path: str) -> list[str]: ...
    def read(self, path: str) -> bytes: ...
    def write_atomic(self, path: str, data: bytes) -> None: ...
    def remove(self, path: str) -> None: ...
    def close(self) -> None: ...


class LocalFS:
    """The relay folder on a local disk (tests; a gateway that mounts the job's workspace)."""

    def listdir(self, path: str) -> list[str]:
        p = Path(path)
        return sorted(x.name for x in p.iterdir()) if p.is_dir() else []

    def read(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def write_atomic(self, path: str, data: bytes) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def remove(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)

    def close(self) -> None:
        pass


class SFTPFS:
    """The relay folder over one SFTP channel (``SSHExecutor.open_sftp``: the transfer host)."""

    def __init__(self, sftp: Any) -> None:
        self._sftp = sftp

    def listdir(self, path: str) -> list[str]:
        try:
            return sorted(self._sftp.listdir(path))
        except FileNotFoundError:
            return []
        except OSError as exc:              # paramiko reports a missing dir as IOError(errno 2)
            if getattr(exc, "errno", None) == 2:
                return []
            raise

    def read(self, path: str) -> bytes:
        with self._sftp.open(path, "rb") as fh:
            return fh.read()

    def write_atomic(self, path: str, data: bytes) -> None:
        tmp = posixpath.join(posixpath.dirname(path), f".{posixpath.basename(path)}.tmp")
        with self._sftp.open(tmp, "wb") as fh:
            fh.write(data)
        try:
            self._sftp.posix_rename(tmp, path)
        except (OSError, AttributeError):
            self._sftp.rename(tmp, path)

    def remove(self, path: str) -> None:
        try:
            self._sftp.remove(path)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._sftp.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass


@dataclass
class RelayStats:
    calls: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    stopped: str = ""                       # why the relay stopped answering, if it did
    errors: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"calls": self.calls, "failed": self.failed, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cost_usd": round(self.cost_usd, 6),
                **({"stopped": self.stopped} if self.stopped else {}),
                **({"first_errors": self.errors[:3]} if self.errors else {})}


def cause_of(exc: BaseException) -> str:
    """The cause of a failed call: ``GatewayError.stage`` ``anthropic_<cause>`` / ``llm_<cause>``,
    else ``anthropic_client.classify_error``."""
    stage = str(getattr(exc, "stage", "") or "")
    if "_" in stage:
        return stage.split("_", 1)[1]
    try:
        from .anthropic_client import classify_error
        return classify_error(exc)[0]
    except Exception:  # noqa: BLE001
        return "endpoint"


class ModelRelay:
    """Answer one job's prompts until :meth:`stop`.

    ``call(messages, max_tokens) -> (text, usage)`` makes one model call with the user's key.
    ``price(usage) -> USD | None``; ``record(usage, duration_ms, error_type)`` books the call against
    the user's account. ``max_cost_usd``: once this scan's calls have cost that much, every further
    prompt is answered with a ``stop``. ``should_stop``: the run was stopped."""

    def __init__(self, fs_factory: Callable[[], RelayFS], relay_dir: str,
                 call: Callable[[list[dict[str, Any]], int], tuple[str, dict[str, Any]]], *,
                 price: Callable[[dict[str, Any]], float | None] = lambda u: None,
                 record: Callable[[dict[str, Any] | None, int, str | None], None] | None = None,
                 max_cost_usd: float | None = None, concurrency: int = 8, poll_s: float = 1.0,
                 should_stop: Callable[[], bool] | None = None,
                 log: Callable[[str], None] = lambda s: None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._fs_factory = fs_factory
        self._req = posixpath.join(relay_dir, REQ)
        self._resp = posixpath.join(relay_dir, RESP)
        self._call, self._price, self._record = call, price, record
        self._cap = max_cost_usd if max_cost_usd and max_cost_usd > 0 else None
        self._concurrency = max(1, int(concurrency))
        self._poll, self._should_stop, self._log, self._sleep = poll_s, should_stop, log, sleep
        self.stats = RelayStats()
        self._lock = threading.Lock()            # the stats
        self._fs_lock = threading.Lock()         # one SFTP channel: one operation at a time
        self._fs: RelayFS | None = None
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._inflight: set[str] = set()

    # --- lifecycle -------------------------------------------------------------------------------

    def start(self) -> "ModelRelay":
        self._thread = threading.Thread(target=self._loop, name="model-relay", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 30.0) -> RelayStats:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout)
        with self._fs_lock:
            if self._fs is not None:
                self._fs.close()
                self._fs = None
        return self.stats

    # --- the loop --------------------------------------------------------------------------------

    def _fs_do(self, fn: Callable[[RelayFS], Any]) -> Any:
        with self._fs_lock:
            if self._fs is None:
                self._fs = self._fs_factory()
            try:
                return fn(self._fs)
            except Exception:
                # A dropped channel: reopen once and retry; a second failure is the caller's.
                try:
                    self._fs.close()
                finally:
                    self._fs = self._fs_factory()
                return fn(self._fs)

    def _loop(self) -> None:
        pool = ThreadPoolExecutor(max_workers=self._concurrency, thread_name_prefix="model-relay")
        try:
            while not self._halt.is_set():
                try:
                    names = self._fs_do(lambda fs: fs.listdir(self._req))
                except Exception as exc:  # noqa: BLE001 - keep serving; the job waits
                    self._log(f"relay: cannot list {self._req}: {exc}")
                    self._halt.wait(min(10.0, self._poll * 5))
                    continue
                for name in names:
                    if not name.endswith(".json") or name.startswith("."):
                        continue
                    with self._lock:
                        if name in self._inflight:
                            continue
                        self._inflight.add(name)
                    pool.submit(self._answer, name)
                self._halt.wait(self._poll)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def _stop_reason(self) -> str:
        if self.stats.stopped:
            return self.stats.stopped
        if self._should_stop is not None and self._should_stop():
            return "the run was stopped"
        if self._cap is not None and self.stats.cost_usd >= self._cap:
            return f"this scan reached its ${self._cap:.2f} spending cap"
        return ""

    def _answer(self, name: str) -> None:
        try:
            req = json.loads(self._fs_do(lambda fs: fs.read(posixpath.join(self._req, name))))
            reply = self._reply(req)
        except Exception as exc:  # noqa: BLE001 - an unreadable prompt is answered, never dropped
            reply = {"error": f"the gateway could not handle this prompt: {exc}", "kind": "model"}
        try:
            data = json.dumps(reply).encode("utf-8")
            self._fs_do(lambda fs: fs.write_atomic(posixpath.join(self._resp, name), data))
            self._fs_do(lambda fs: fs.remove(posixpath.join(self._req, name)))
        except Exception as exc:  # noqa: BLE001 - the job times out and retries this prompt
            self._log(f"relay: could not answer {name}: {exc}")
        finally:
            with self._lock:
                self._inflight.discard(name)

    def _reply(self, req: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            why = self._stop_reason()
        if why:
            return {"error": why, "kind": "stop"}
        messages = req.get("messages") or []
        max_tokens = int(req.get("max_tokens") or 8000)
        cause, err = "", ""
        for attempt in range(len(_RETRY_WAITS) + 1):
            t0 = time.time()
            try:
                text, usage = self._call(messages, max_tokens)
            except Exception as exc:  # noqa: BLE001 - classified below
                cause, err = cause_of(exc), str(getattr(exc, "message", "") or exc)[:300]
                if self._record is not None:
                    self._record(None, int((time.time() - t0) * 1000), cause)
                if cause in _RETRYABLE and attempt < len(_RETRY_WAITS) and not self._halt.is_set():
                    self._sleep(_RETRY_WAITS[attempt])
                    continue
                break
            usage = dict(usage or {})
            cost = self._price(usage)
            if self._record is not None:
                self._record(usage, int((time.time() - t0) * 1000), None)
            with self._lock:
                self.stats.calls += 1
                self.stats.input_tokens += int(usage.get("prompt_tokens") or 0)
                self.stats.output_tokens += int(usage.get("completion_tokens") or 0)
                self.stats.cost_usd += float(cost or 0)
            keep = {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "model") if k in usage}
            return {"text": text, "usage": keep, "cost_usd": cost}
        with self._lock:
            self.stats.failed += 1
            if len(self.stats.errors) < 5:
                self.stats.errors.append(f"{cause}: {err}")
            if cause in _FATAL and not self.stats.stopped:
                self.stats.stopped = f"the model refused the key or the request ({cause}: {err})"
        kind = "stop" if cause in _FATAL else ("transport" if cause in _RETRYABLE else "model")
        return {"error": f"{cause}: {err}", "kind": kind}
