"""Several chats running at once on one SSH/GPU Connection — offline, no cluster/SSH/GPU.

A session is one SSH login + one served model, and a person often has more than one thing going:
a large upload in one chat, an analysis in another, a quick question in a third. Each chat's run
now keeps its own cancel / plan-review events, its own stream for a reconnecting client and its
own queue of mid-run notes, and the code working for a run is told which run it is
(``_CURRENT_RUN``), so two runs in flight never stamp, stop, steer or replay each other. What stays
refused: a second run in the same chat, and more runs than ``AISCIENTIST_MAX_RUNS_PER_SESSION``.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from aiscientist.agents.loop_utils import ContextThreadPoolExecutor  # noqa: E402
from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_conns(monkeypatch):
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    yield
    gw_app.CONNECTIONS.clear()


def _conn():
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = object()
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


def _start(conn, conversation_id, run_id):
    """Begin a run the way its own asyncio task would: in a context of its own. Returns the run
    and that context, so later calls can be made 'from inside' the run."""
    ctx = contextvars.copy_context()

    def begin():
        run = conn.begin_run(conversation_id)
        conn.bind_run_id(run, run_id)
        run.running = True
        conn.push({"type": "chat_start"})
        return run

    return ctx.run(begin), ctx


def test_two_runs_stamp_their_own_events():
    c = _conn()
    a, in_a = _start(c, "conv-A", "runA")
    b, in_b = _start(c, "conv-B", "runB")
    pa, pb = {"type": "chat_token", "token": "a"}, {"type": "chat_token", "token": "b"}
    in_a.run(c.push, pa)          # A pushes AFTER B started: it must still say A
    in_b.run(c.push, pb)
    assert (pa["run_id"], pa["conversation_id"]) == ("runA", "conv-A")
    assert (pb["run_id"], pb["conversation_id"]) == ("runB", "conv-B")
    in_a.run(c.emit, "info", "lab", "from A")
    assert c.log[-1]["conversation_id"] == "conv-A"
    assert c.chat_running and c.live_run_for("conv-A") is a and c.live_run_for("conv-B") is b


def test_each_run_reads_its_own_legacy_accessors():
    c = _conn()
    a, in_a = _start(c, "conv-A", "runA")
    b, in_b = _start(c, "conv-B", "runB")
    assert in_a.run(lambda: c.chat_stop) is a.chat_stop
    assert in_b.run(lambda: c.plan_event) is b.plan_event
    in_a.run(lambda: setattr(c, "pending_plan", {"kind": "agenda", "payload": ["x"]}))
    assert a.pending_plan and b.pending_plan is None
    # The run that finishes first leaves the other one running.
    in_a.run(lambda: setattr(c, "chat_running", False))
    assert not a.running and b.running and c.chat_running


def test_an_event_with_no_run_context_is_left_untagged_when_two_runs_are_live():
    c = _conn()
    _start(c, "conv-A", "runA")
    _start(c, "conv-B", "runB")
    payload = {"type": "lab_progress", "text": "?"}
    contextvars.Context().run(c.push, payload)                # e.g. the GPU monitor
    assert "conversation_id" not in payload                  # never stamped with a guess


def test_a_thread_pool_inside_a_run_keeps_the_run():
    c = _conn()
    a, in_a = _start(c, "conv-A", "runA")
    _start(c, "conv-B", "runB")                               # newest: a plain pool would pick it

    def parallel_steps():
        with ContextThreadPoolExecutor(max_workers=3) as ex:
            return list(ex.map(lambda i: c.push({"type": "lab_progress", "text": str(i)}) or
                               c.active_run, range(6)))

    seen = in_a.run(parallel_steps)
    assert all(r is a for r in seen)
    assert {e.get("conversation_id") for e in a.stream["progress"] and [
        {"conversation_id": "conv-A"}]} == {"conv-A"}
    assert [p["text"] for p in a.stream["progress"]] == [str(i) for i in range(6)] or \
        sorted(p["text"] for p in a.stream["progress"]) == [str(i) for i in range(6)]


def test_stop_and_inject_reach_only_the_named_chat():
    c = _conn()
    a, _ = _start(c, "conv-A", "runA")
    b, _ = _start(c, "conv-B", "runB")
    client = TestClient(gw_app.app)
    r = client.post("/api/chat/inject", json={"connection_id": c.id, "conversation_id": "conv-A",
                                              "text": "use resolution 1.0"})
    assert r.json()["status"] == "queued"
    assert a.injections == ["use resolution 1.0"] and b.injections == []
    r = client.post("/api/chat/stop", json={"connection_id": c.id, "conversation_id": "conv-B"})
    assert r.json()["status"] == "stopping"
    assert b.chat_stop.is_set() and not a.chat_stop.is_set()
    assert a.pull_injections() == ["use resolution 1.0"] and a.pull_injections() == []


def test_plan_review_answers_only_its_own_chat():
    c = _conn()
    a, _ = _start(c, "conv-A", "runA")
    b, _ = _start(c, "conv-B", "runB")
    a.pending_plan = {"kind": "agenda", "payload": ["qc"]}
    b.pending_plan = {"kind": "agenda", "payload": ["call variants"]}
    r = TestClient(gw_app.app).post("/api/lab/plan", json={
        "connection_id": c.id, "conversation_id": "conv-A", "action": "approve"})
    assert r.json()["action"] == "approve"
    assert a.plan_event.is_set() and not b.plan_event.is_set() and b.pending_plan


def test_lab_admits_another_chat_but_not_a_second_run_in_the_same_chat(monkeypatch):
    c = _conn()
    started = []

    async def fake_dispatch(conn, req):
        started.append(req.conversation_id)

    monkeypatch.setattr(gw_app, "_dispatch_lab", fake_dispatch)
    monkeypatch.setenv("AISCIENTIST_MAX_RUNS_PER_SESSION", "2")
    _start(c, "conv-A", "runA")
    client = TestClient(gw_app.app)
    body = {"connection_id": c.id, "question": "q"}
    r = client.post("/api/lab", json={**body, "conversation_id": "conv-A"})
    assert r.status_code == 409 and "this chat" in r.json()["error"]
    r = client.post("/api/lab", json={**body})                    # an older client: no chat named
    assert r.status_code == 409
    r = client.post("/api/lab", json={**body, "conversation_id": "conv-B"})
    assert r.status_code == 200 and started == ["conv-B"]
    _start(c, "conv-B", "runB")
    r = client.post("/api/lab", json={**body, "conversation_id": "conv-C"})
    assert r.status_code == 409 and r.json()["limit"] == 2       # the session's limit


def test_one_run_per_session_when_the_limit_is_one(monkeypatch):
    c = _conn()

    async def fake_dispatch(conn, req):
        return None

    monkeypatch.setattr(gw_app, "_dispatch_lab", fake_dispatch)
    monkeypatch.setenv("AISCIENTIST_MAX_RUNS_PER_SESSION", "1")
    _start(c, "conv-A", "runA")
    r = TestClient(gw_app.app).post("/api/lab", json={"connection_id": c.id, "question": "q",
                                                      "conversation_id": "conv-B"})
    assert r.status_code == 409


def test_a_reconnecting_client_gets_every_run_and_each_chat_s_last_finished_one():
    c = _conn()
    a, in_a = _start(c, "conv-A", "runA")
    b, in_b = _start(c, "conv-B", "runB")
    in_a.run(c.push, {"type": "chat_token", "token": "A says"})
    in_b.run(c.push, {"type": "lab_progress", "text": "B step 1", "level": "info"})
    in_a.run(c.push, {"type": "chat_done"})
    in_a.run(lambda: setattr(c, "chat_running", False))
    in_a.run(c.end_run, a)
    replays = c.replays()
    starts = [r[0] for r in replays]
    by_conv = {s["conversation_id"]: s for s in starts}
    assert set(by_conv) == {"conv-A", "conv-B"}
    assert not by_conv["conv-B"].get("recover")                  # still running: live replay
    assert by_conv["conv-A"]["recover"] is True                  # finished: recover once
    assert by_conv["conv-A"]["stream_id"] == a.stream_id != b.stream_id == by_conv["conv-B"]["stream_id"]
    # every replayed event names its chat, like the live ones that follow it
    assert all(p.get("conversation_id") == r[0]["conversation_id"] for r in replays for p in r)
    # the summary lists both, so a reloaded page re-adopts each run in its own chat
    c.alloc = None
    assert [(x["conversation_id"], x["running"]) for x in c.summary()["live_runs"]] == [("conv-B", True)]


def test_session_cleanup_keeps_every_running_run():
    c = _conn()
    _start(c, "conv-A", "runA")
    _start(c, "conv-B", "runB")
    c.hpc_run_ids.update({"old111": None})
    targets = gw_app._session_cleanup_targets(c)
    assert targets["runs"] == ["old111"] and targets["kept_busy"] == ["runA", "runB"]


def test_dispatch_closes_a_run_that_never_started(monkeypatch):
    c = _conn()

    async def nothing(conn, req):
        conn.push({"type": "chat_stopped"})                  # e.g. "that reads as a reply"

    monkeypatch.setattr(gw_app, "_dispatch_lab_run", nothing)
    req = gw_app.LabRequest(connection_id=c.id, question="thanks", conversation_id="conv-A")
    asyncio.new_event_loop().run_until_complete(gw_app._dispatch_lab(c, req))
    assert c.live_runs == [] and c.live_run_for("conv-A") is None and not c.chat_running


def test_a_chat_is_busy_from_the_moment_its_run_begins(monkeypatch):
    # The follow-up classifier and a clarify card come BEFORE the run is "running"; a second
    # message in that chat must not start a second run under them.
    c = _conn()

    async def fake_dispatch(conn, req):
        return None

    monkeypatch.setattr(gw_app, "_dispatch_lab", fake_dispatch)
    contextvars.copy_context().run(c.begin_run, "conv-A")
    assert not c.chat_running
    client = TestClient(gw_app.app)
    r = client.post("/api/lab", json={"connection_id": c.id, "question": "q", "conversation_id": "conv-A"})
    assert r.status_code == 409
    r = client.post("/api/lab", json={"connection_id": c.id, "question": "q", "conversation_id": "conv-B"})
    assert r.status_code == 200


def test_each_run_writes_its_job_files_in_its_own_scratch_folder():
    c = _conn()
    a = gw_app._scratch_dir(c, "runcode", run_id="runA")
    b = gw_app._scratch_dir(c, "runcode", run_id="runB")
    base = gw_app._temp_base(c)
    # Each run's job files are in its own folder: bioagent_python_1.out is no longer shared.
    assert a == f"{base}/runs/runA/jobs/runcode" and b == f"{base}/runs/runB/jobs/runcode"


def test_stop_answers_a_clarify_card_before_the_run_is_running():
    c = _conn()
    ctx = contextvars.copy_context()
    run = ctx.run(c.begin_run, "conv-A")                  # the follow-up router asked a question
    run.pending_plan = {"kind": "clarify", "payload": [{"question": "which?", "options": ["a"]}]}
    r = TestClient(gw_app.app).post("/api/chat/stop", json={"connection_id": c.id, "conversation_id": "conv-A"})
    assert r.json()["status"] == "stopping"
    assert run.plan_event.is_set() and run.plan_value == {"action": "cancel"}
