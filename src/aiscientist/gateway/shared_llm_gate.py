"""Fair access to the lab's shared model: a limit per account, a limit for the server, one queue.

The model server batches requests continuously, so it would take them all; but each request it holds
takes KV cache from the others, and one long research run (a claim audit asks six questions at once)
could fill it. So the gateway decides who goes next:

* an account has at most ``per_user`` requests in flight (``AISCIENTIST_SHARED_LLM_PER_USER``,
  default 4: three runs of one session and a chat turn);
* the server has at most ``total`` (``AISCIENTIST_SHARED_LLM_MAX_IN_FLIGHT``, default 12);
* the requests that wait form one queue in arrival order. The oldest one whose account is under its
  limit goes next, so an account at its limit does not hold up everyone behind it.

A slot is held for ONE request (a completion, a tool turn, a streamed chat turn), never for a run: a
run executing a tool or waiting for a Slurm job holds nothing.

The gateway is one process (``uvicorn`` without workers), so a lock and a condition are enough.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

from .errors import GatewayError

#: ``on_wait(reason, ahead)``: ``"account"`` (this account is at its limit), ``"server"`` (the server
#: is at its limit; ``ahead`` requests came first) or ``"go"`` (the request starts after waiting).
WaitCallback = Callable[[str, int], None]


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def queue_timeout_s() -> float:
    """How long a request waits in the queue before it fails (``..._QUEUE_TIMEOUT_SECONDS``)."""
    return float(_int_env("AISCIENTIST_SHARED_LLM_QUEUE_TIMEOUT_SECONDS", 1800))


def down_wait_s() -> float:
    """How long a request waits for the server to answer again while its job is renewed or restarted
    (``..._DOWN_WAIT_SECONDS``): the keeper moves to a new job within a few minutes of it answering."""
    return float(_int_env("AISCIENTIST_SHARED_LLM_DOWN_WAIT_SECONDS", 600))


@dataclass
class _Ticket:
    seq: int
    user: str
    since: float


class SharedLLMGate:
    def __init__(self, per_user: int = 4, total: int = 12, *, clock: Callable[[], float] = time.monotonic,
                 poll_s: float = 1.0) -> None:
        self.per_user = max(1, per_user)
        self.total = max(1, total)
        self._clock = clock
        self._poll_s = poll_s
        self._cond = threading.Condition()
        self._running: dict[str, int] = {}
        self._in_flight = 0
        self._waiting: list[_Ticket] = []
        self._seq = itertools.count()
        self._served = 0
        self._waited = 0
        self._longest_wait_s = 0.0

    # -- the queue (the caller holds the lock) ----------------------------------------------------

    def _next(self) -> _Ticket | None:
        """The request that goes next: the oldest waiting one whose account is under its limit."""
        if self._in_flight >= self.total:
            return None
        for t in self._waiting:
            if self._running.get(t.user, 0) < self.per_user:
                return t
        return None

    def _why(self, t: _Ticket) -> tuple[str, int]:
        if self._running.get(t.user, 0) >= self.per_user:
            return "account", 0
        return "server", sum(1 for w in self._waiting if w.seq < t.seq)

    # -- what callers use -------------------------------------------------------------------------

    @contextmanager
    def slot(self, user: str, *, on_wait: WaitCallback | None = None, timeout: float | None = None,
             cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
        """Hold one of ``user``'s request slots for the ``with`` body, waiting in the queue first if
        needed. Raises :class:`GatewayError` when the wait passes ``timeout`` or ``cancelled()``."""
        user = user or "?"
        told: tuple[str, int] | None = None
        with self._cond:
            t = _Ticket(next(self._seq), user, self._clock())
            self._waiting.append(t)
            try:
                while self._next() is not t:
                    why = self._why(t)
                    if on_wait is not None and why != told:
                        told = why
                        on_wait(*why)
                    if cancelled is not None and cancelled():
                        raise GatewayError("Stopped while waiting for the shared model.",
                                           stage="shared_llm_stopped")
                    waited = self._clock() - t.since
                    if timeout is not None and waited >= timeout:
                        raise GatewayError(
                            f"The shared model has been busy for {int(waited // 60)} min, so this request "
                            "was not sent. Try again later, or use your own API key.", stage="shared_llm_busy")
                    self._cond.wait(self._poll_s)
            finally:
                self._waiting.remove(t)
                self._cond.notify_all()
            self._running[user] = self._running.get(user, 0) + 1
            self._in_flight += 1
            self._served += 1
            if told is not None:
                self._waited += 1
                self._longest_wait_s = max(self._longest_wait_s, self._clock() - t.since)
        if told is not None and on_wait is not None:
            on_wait("go", 0)
        try:
            yield
        finally:
            with self._cond:
                left = self._running.get(user, 1) - 1
                if left > 0:
                    self._running[user] = left
                else:
                    self._running.pop(user, None)
                self._in_flight -= 1
                self._cond.notify_all()

    def snapshot(self) -> dict:
        """Counts for the status page: limits, requests in flight and waiting, accounts served now."""
        with self._cond:
            return {"per_user_limit": self.per_user, "max_in_flight": self.total,
                    "in_flight": self._in_flight, "waiting": len(self._waiting),
                    "accounts_active": len(self._running), "served": self._served,
                    "waited": self._waited, "longest_wait_s": round(self._longest_wait_s, 1)}


_GATE: SharedLLMGate | None = None
_GATE_LOCK = threading.Lock()


def gate() -> SharedLLMGate:
    """The process-wide gate, made from the environment on first use."""
    global _GATE
    with _GATE_LOCK:
        if _GATE is None:
            _GATE = SharedLLMGate(per_user=_int_env("AISCIENTIST_SHARED_LLM_PER_USER", 4),
                                  total=_int_env("AISCIENTIST_SHARED_LLM_MAX_IN_FLIGHT", 12))
        return _GATE


def reset() -> None:
    """Forget the gate (tests; a changed environment)."""
    global _GATE
    with _GATE_LOCK:
        _GATE = None
