"""The queue in front of the lab's shared model (RCHENLAB/AiScientist issue 15).

An account has at most ``per_user`` requests in flight, the server at most ``total``; the requests
that wait go in arrival order, except that one whose account is at its limit lets the others pass.
Threads stand in for the calls of several sessions; nothing is sent anywhere.
"""

from __future__ import annotations

import threading
import time

import pytest

from aiscientist.gateway import shared_llm_gate
from aiscientist.gateway.errors import GatewayError


def _gate(per_user=2, total=10):
    return shared_llm_gate.SharedLLMGate(per_user=per_user, total=total, poll_s=0.01)


class _Holder:
    """A request that takes its slot on a thread and holds it until released."""

    def __init__(self, gate, user, order=None, **kw):
        self.gate, self.user, self.order, self.kw = gate, user, order, kw
        self.started, self.release, self.error = threading.Event(), threading.Event(), None
        self.waits: list[tuple[str, int]] = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            with self.gate.slot(self.user, on_wait=lambda r, n: self.waits.append((r, n)), **self.kw):
                if self.order is not None:
                    self.order.append(self.user)
                self.started.set()
                self.release.wait(5)
        except GatewayError as exc:
            self.error = exc

    def done(self):
        self.release.set()
        self.thread.join(5)


def _settle():
    time.sleep(0.08)


def test_an_account_has_at_most_its_limit_in_flight_and_others_are_not_held_up():
    g = _gate(per_user=2)
    a1, a2 = _Holder(g, "alice"), _Holder(g, "alice")
    assert a1.started.wait(1) and a2.started.wait(1)
    a3 = _Holder(g, "alice")
    bob = _Holder(g, "bob")
    assert bob.started.wait(1), "bob's request is not queued behind alice's third"
    _settle()
    assert not a3.started.is_set() and a3.waits == [("account", 0)]
    assert g.snapshot()["in_flight"] == 3 and g.snapshot()["waiting"] == 1
    a1.done()
    assert a3.started.wait(1) and a3.waits[-1] == ("go", 0)
    for h in (a2, a3, bob):
        h.done()
    snap = g.snapshot()
    assert (snap["in_flight"], snap["waiting"], snap["served"], snap["waited"]) == (0, 0, 4, 1)


def test_when_the_server_is_full_requests_go_in_arrival_order_and_are_told_their_place():
    g = _gate(per_user=4, total=1)
    order: list[str] = []
    first = _Holder(g, "alice", order)
    assert first.started.wait(1)
    b = _Holder(g, "bob", order)
    _settle()
    c = _Holder(g, "carol", order)
    _settle()
    assert b.waits == [("server", 0)] and c.waits == [("server", 1)]
    first.done()
    assert b.started.wait(1)
    _settle()
    assert not c.started.is_set(), "one request at a time: carol waits for bob"
    b.done()
    assert c.started.wait(1)
    c.done()
    assert order == ["alice", "bob", "carol"]


def test_a_request_that_waits_too_long_fails_and_says_why():
    g = _gate(per_user=1, total=1)
    hold = _Holder(g, "alice")
    assert hold.started.wait(1)
    late = _Holder(g, "bob", timeout=0.05)
    late.thread.join(2)
    assert late.error is not None and late.error.stage == "shared_llm_busy"
    assert "busy" in late.error.message and g.snapshot()["waiting"] == 0
    hold.done()


def test_a_stop_ends_the_wait():
    g = _gate(per_user=1, total=1)
    hold = _Holder(g, "alice")
    assert hold.started.wait(1)
    stop = threading.Event()
    waiting = _Holder(g, "alice", cancelled=stop.is_set)
    _settle()
    stop.set()
    waiting.thread.join(2)
    assert waiting.error is not None and waiting.error.stage == "shared_llm_stopped"
    hold.done()
    assert g.snapshot()["in_flight"] == 0


def test_a_slot_is_given_back_when_the_request_fails():
    g = _gate(per_user=1, total=1)
    with pytest.raises(RuntimeError):
        with g.slot("alice"):
            raise RuntimeError("the call failed")
    with g.slot("alice"):                 # not stuck at the limit
        assert g.snapshot()["in_flight"] == 1


def test_the_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_SHARED_LLM_PER_USER", "3")
    monkeypatch.setenv("AISCIENTIST_SHARED_LLM_MAX_IN_FLIGHT", "16")
    monkeypatch.setenv("AISCIENTIST_SHARED_LLM_QUEUE_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("AISCIENTIST_SHARED_LLM_DOWN_WAIT_SECONDS", "0")
    shared_llm_gate.reset()
    try:
        g = shared_llm_gate.gate()
        assert (g.per_user, g.total) == (3, 16) and shared_llm_gate.gate() is g
        assert shared_llm_gate.queue_timeout_s() == 60 and shared_llm_gate.down_wait_s() == 0
    finally:
        shared_llm_gate.reset()
    monkeypatch.delenv("AISCIENTIST_SHARED_LLM_PER_USER")
    monkeypatch.delenv("AISCIENTIST_SHARED_LLM_MAX_IN_FLIGHT")
    try:
        assert (shared_llm_gate.gate().per_user, shared_llm_gate.gate().total) == (4, 12)
    finally:
        shared_llm_gate.reset()
