"""Per-client limits for the account routes (login, self-registration).

The public site is reachable by anyone, and a probe on 2026-10-25 tried logins and sign-ups from
rotating Tor exit addresses (KrimsonSun/BioAgentPrototype issue 115). Each limit is a sliding window:
at most ``limit`` events per ``window_s`` seconds for one key (an account, an email address or a
client address). The counters live in this process, which is enough for the single gateway worker
and resets on a restart.

The client address is the one the reverse proxy saw. Envoy (or nginx) appends it as the LAST
entry of ``X-Forwarded-For``; anything before it was written by the client and is not trusted. The
header is read only when the request comes straight from a private or loopback address, i.e. from
the proxy; a request that reaches the gateway directly is identified by its own address.
"""

from __future__ import annotations

import ipaddress
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any


def enabled() -> bool:
    """``AISCIENTIST_AUTH_RATE_LIMITS=0`` turns every limit off (an emergency switch, not a setting)."""
    return os.environ.get("AISCIENTIST_AUTH_RATE_LIMITS", "1").strip().lower() not in ("0", "false", "no", "off")


class SlidingWindow:
    """At most ``limit`` events per ``window_s`` seconds for each key. Thread-safe: the FastAPI
    routes that use it are plain functions run in a thread pool."""

    def __init__(self, limit: int, window_s: float, *, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 50_000):
        self.limit, self.window_s, self._clock, self._max_keys = limit, window_s, clock, max_keys
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _live(self, key: str, now: float) -> "deque[float]":
        q = self._events.get(key)
        if q is None:
            return deque()
        while q and q[0] <= now - self.window_s:
            q.popleft()
        if not q:
            self._events.pop(key, None)
        return q

    def retry_after(self, key: str) -> float:
        """0 when another event is allowed now, else the seconds until the oldest one expires."""
        with self._lock:
            now = self._clock()
            q = self._live(key, now)
            if len(q) < self.limit:
                return 0.0
            return max(0.0, q[0] + self.window_s - now)

    def hit(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            q = self._live(key, now)
            if not q:
                if len(self._events) >= self._max_keys:
                    # Bounded memory under a flood of distinct keys: forget the oldest ones.
                    for old in list(self._events)[: max(1, self._max_keys // 10)]:
                        self._events.pop(old, None)
                self._events[key] = q
            q.append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


def _is_proxy(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _strip_port(addr: str) -> str:
    addr = addr.strip().strip('"')
    if addr.startswith("["):                       # [v6]:port
        return addr[1:addr.index("]")] if "]" in addr else addr
    if addr.count(":") == 1:                       # v4:port
        return addr.split(":", 1)[0]
    return addr


def client_ip(request: Any) -> str:
    """The address the reverse proxy saw for this request (see the module docstring)."""
    peer = getattr(getattr(request, "client", None), "host", "") or ""
    forwarded = (request.headers.get("x-forwarded-for") or "") if hasattr(request, "headers") else ""
    if forwarded and _is_proxy(peer):
        last = _strip_port(forwarded.split(",")[-1])
        if last:
            return last
    return peer or "unknown"


def wait_words(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
