"""A confirmation the agent waits on can be answered (docs/agent_proposed_environments.md, step 0).

Before the agent's HPC3 shell acts at an edge (a package installed into the lab-shared area, a
write outside the user's folders, a download, a delete), it asks: ``_hitl_confirm`` pushes
``confirm_request`` and blocks the run's worker until ``POST /api/confirm`` answers, or until its
timeout counts as no. The console had no card for it, so on the website every such request waited
out its 10 minutes and was declined. The server side pinned here:

- the request says how long it has (``timeout_s``), and ``confirm_done`` says what became of it
  (``approved``, ``declined``, ``timeout``, ``stopped``), for every window that shows the card;
- Stop answers a waiting confirmation with no at once, instead of leaving the run blocked until
  the timeout;
- a browser that subscribes while a confirmation waits (a reconnect, a reload) gets it again,
  with the time it has left.

The card itself is pinned in ``test_console_confirm_card_js.py``.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app          # noqa: E402
from aiscientist.gateway.settings import HPCSettings   # noqa: E402


class _Req:
    kind = "install"
    summary = "Install “pyranges” on HPC3 so this analysis can continue"
    detail = "pyranges into the lab-shared package area"
    reversible = True

    def as_dict(self):
        return {"kind": self.kind, "summary": self.summary, "detail": self.detail,
                "reversible": self.reversible}


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    loop = asyncio.new_event_loop()
    c = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    gw_app.CONNECTIONS[c.id] = c
    yield c
    gw_app.CONNECTIONS.pop(c.id, None)
    loop.close()


def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app)


def _record_pushes(monkeypatch, c) -> list[dict]:
    """The pushes, tagged as the real push tags them (with the run they name)."""
    pushed: list[dict] = []

    def push(payload, *, run=None):
        pushed.append(run.tag(dict(payload)) if run is not None else dict(payload))

    monkeypatch.setattr(c, "push", push)
    return pushed


def _ask(c, run) -> "tuple[threading.Thread, list[bool]]":
    """The agent's shell asks, on a worker thread, as a run does; returns once the card is up."""
    answer: list[bool] = []
    t = threading.Thread(target=lambda: answer.append(gw_app._hitl_confirm(c, run)(_Req())), daemon=True)
    t.start()
    for _ in range(500):
        if run.pending_confirm is not None:
            break
        time.sleep(0.01)
    assert run.pending_confirm is not None, "the confirmation never came up"
    return t, answer


def _done(pushed: list[dict]) -> dict:
    return next(p for p in pushed if p["type"] == "confirm_done")


@pytest.mark.parametrize("approved,outcome", [(True, "approved"), (False, "declined")])
def test_the_card_says_how_long_it_has_and_done_says_the_answer(conn, monkeypatch, approved, outcome):
    monkeypatch.setenv("AISCIENTIST_CONFIRM_TIMEOUT_S", "30")
    pushed = _record_pushes(monkeypatch, conn)
    run = conn.begin_run("conv-1")
    conn.bind_run_id(run, "run-1")
    t, answer = _ask(conn, run)

    card = next(p for p in pushed if p["type"] == "confirm_request")
    assert card["timeout_s"] == 30 and card["kind"] == "install" and card["reversible"] is True
    assert (card["run_id"], card["conversation_id"]) == ("run-1", "conv-1")

    r = _client().post("/api/confirm", json={"connection_id": conn.id, "approved": approved,
                                             "run_id": "run-1", "conversation_id": "conv-1"})
    assert r.json() == {"status": "ok", "approved": approved}
    t.join(timeout=5)
    assert answer == [approved]
    done = _done(pushed)
    assert done["outcome"] == outcome
    assert (done["run_id"], done["conversation_id"]) == ("run-1", "conv-1"), "so it closes the OWNING chat's card"
    assert run.pending_confirm is None and run.confirm_expires_at is None


def test_no_answer_in_time_is_reported_as_a_timeout(conn, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_CONFIRM_TIMEOUT_S", "0.2")
    pushed = _record_pushes(monkeypatch, conn)
    run = conn.begin_run("conv-1")
    t, answer = _ask(conn, run)
    t.join(timeout=5)
    assert answer == [False] and _done(pushed)["outcome"] == "timeout"


def test_stop_answers_a_waiting_confirmation_with_no_at_once(conn, monkeypatch):
    """The run's worker waits on confirm_event, not on chat_stop: a Stop clicked while the card was
    up used to hold the run until the confirmation's timeout (10 minutes by default)."""
    monkeypatch.setenv("AISCIENTIST_CONFIRM_TIMEOUT_S", "600")
    pushed = _record_pushes(monkeypatch, conn)
    run = conn.begin_run("conv-1")
    conn.bind_run_id(run, "run-1")
    t, answer = _ask(conn, run)

    started = time.monotonic()
    _client().post("/api/chat/stop", json={"connection_id": conn.id, "run_id": "run-1"})
    t.join(timeout=5)
    assert not t.is_alive() and time.monotonic() - started < 5
    assert answer == [False] and _done(pushed)["outcome"] == "stopped"
    assert run.pending_confirm is None


def test_a_browser_that_subscribes_while_it_waits_gets_it_again(conn, monkeypatch):
    """A reload or a dropped socket: the card comes back with the time actually left, tagged with
    its run so the page puts it in the owning chat."""
    monkeypatch.setenv("AISCIENTIST_CONFIRM_TIMEOUT_S", "300")
    run = conn.begin_run("conv-1")
    conn.bind_run_id(run, "run-1")
    t, answer = _ask(conn, run)
    try:
        with _client().websocket_connect(f"/ws/{conn.id}") as ws:
            got = ws.receive_json()
            while got.get("type") != "confirm_request" or "seconds_left" not in got:
                got = ws.receive_json()
        assert got["summary"] == _Req.summary and got["detail"] == _Req.detail
        assert (got["run_id"], got["conversation_id"]) == ("run-1", "conv-1")
        assert 290 <= got["seconds_left"] <= 300
    finally:
        run.confirm_value = False
        run.confirm_event.set()
        t.join(timeout=5)
    assert answer == [False]
