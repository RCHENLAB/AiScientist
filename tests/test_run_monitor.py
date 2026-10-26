"""Where each run is right now (gateway/run_monitor.py), behind the System page's live graph.

The monitor follows what a run already produces: the lab's ``on_event`` dicts, the WebSocket
payloads its connection pushes, and the start of each model call. These pin that it puts a run in
the stage it is really in (a research run replayed event by event through ``Connection.push``), keeps
steps the DAG scheduler runs side by side apart, never shows another account what a run is about,
and that the graph's stages are exactly the stages it moves runs through.
"""

from __future__ import annotations

import asyncio
import threading
import types

import pytest

from aiscientist.gateway import run_monitor, system_info
from aiscientist.gateway.run_monitor import RunMonitor

ALICE = types.SimpleNamespace(id=1, username="alice", role="user")
MALLORY = types.SimpleNamespace(id=2, username="mallory", role="user")
ADMIN = types.SimpleNamespace(id=3, username="boss", role="admin")


@pytest.fixture(autouse=True)
def _clean_registry():
    run_monitor._reset_for_tests()
    yield
    run_monitor._reset_for_tests()


def _where(m: RunMonitor) -> tuple[str, list[tuple]]:
    snap = m.snapshot(full=True)
    return snap["stage"], [(a["phase"], a["tool"], a.get("n")) for a in snap["active"]]


# --- one research run, replayed through the gateway's own hooks ----------------------------------

@pytest.fixture
def lab_conn(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw_app
    from aiscientist.gateway.settings import HPCSettings

    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="alice")
    conn.app_user_id = ALICE.id
    try:
        yield gw_app, conn
    finally:
        loop.close()


def test_a_research_run_moves_through_the_stages_it_really_runs(lab_conn):
    gw_app, conn = lab_conn
    run = conn.begin_run("chat-1")
    conn.bind_run_id(run, "r1")
    req = types.SimpleNamespace(question="Compare DDX41 KO with WT per cell type", route="")
    gw_app._watch_run(conn, run, req)
    conn.push({"type": "chat_start"})
    m = run.monitor
    assert m is not None and m.kind == "research" and m.run_id == "r1" and m.owner_id == ALICE.id
    assert _where(m) == ("request", [])

    gw_app._watch_stage(run, "data")
    assert m.stage == "data"
    for ev in ({"type": "scope"}, {"type": "planning"},
               {"type": "pi_agenda", "agenda": ["QC the matrix", "Cluster", "DE per cell type"]}):
        m.observe(ev)
    assert m.stage == "plan" and m.snapshot(full=True)["steps"] == {"total": 3, "accepted": 0}

    # Plan first: the gateway pushes the plan card and the run waits for its owner.
    conn.push({"type": "plan_prompt", "agenda": []})
    assert m.stage == "approve" and m.waiting == "plan"
    m.observe({"type": "plan_approved"})
    assert m.stage == "execute" and m.waiting == ""

    # The DAG plan, then one step: claimed, started (its brief, not its goal), a tool, the Critic.
    m.observe({"type": "lab_plan_dag", "nodes": [{"id": "s1", "goal": "QC the matrix"},
                                                 {"id": "s2", "goal": "Cluster"},
                                                 {"id": "s3", "goal": "DE per cell type"}]})
    m.observe({"type": "node_claim", "node": "s1", "specialist": "QC & preprocessing specialist"})
    m.observe({"type": "scientist_start", "step": "QC the matrix\n\nReuse: none",
               "specialist": "QC & preprocessing specialist"})
    assert _where(m) == ("execute", [("model", "", 1)])
    m.observe({"type": "tool_start", "tool": "run_cellqc", "args": {}})
    assert _where(m) == ("execute", [("tool", "run_cellqc", 1)])
    snap = m.snapshot(full=True)
    assert snap["active"][0]["step"] == "QC the matrix"           # the goal, as the plan names it
    m.observe({"type": "tool_result", "tool": "run_cellqc", "summary": "ok"})
    assert _where(m) == ("execute", [("model", "", 1)])
    m.observe({"type": "finish", "answer_preview": "kept 9,812 cells"})
    assert _where(m) == ("execute", [("critic", "", 1)])
    m.observe({"type": "critic", "step": "QC the matrix", "verdict": "accept", "score": 0.9})
    assert _where(m) == ("execute", [])
    assert m.snapshot(full=True)["steps"] == {"total": 3, "accepted": 1}

    # A step sent back goes back to the model.
    m.observe({"type": "node_claim", "node": "s2", "specialist": "Clustering & cell-type specialist"})
    m.observe({"type": "scientist_start", "step": "Cluster", "specialist": "Clustering & cell-type specialist"})
    m.observe({"type": "finish"})
    m.observe({"type": "critic", "step": "Cluster", "verdict": "revise", "score": 0.4})
    assert _where(m) == ("execute", [("model", "", 2)])
    m.observe({"type": "step_force_advance", "step": "Cluster"})
    assert _where(m) == ("execute", [])

    # The write-up: the claim audit and the writer have no event, their model calls mark them.
    complete, _sci = gw_app._watch_llm(run, lambda messages, role=None: role, lambda messages, tools: {})
    m.observe({"type": "synthesize", "accepted": 1})
    assert m.stage == "audit"
    seen = []
    complete_probe, _ = gw_app._watch_llm(
        run, lambda messages, role=None: seen.append(m.snapshot(full=True)["llm"]["thinking"]) or "x",
        lambda messages, tools: {})
    complete_probe([], role="audit")
    assert seen == [["audit"]] and m.snapshot(full=True)["llm"]["thinking"] == []
    assert complete([], role="writer") == "writer" and m.stage == "writeup"
    gw_app._watch_stage(run, "report")
    assert m.stage == "report"

    conn.push({"type": "lab_progress", "text": "📄 Rendering the report (PDF / DOCX)…"})
    conn.push({"type": "run_complete", "run_id": "r1", "agenda": []})
    snap = m.snapshot(full=True)
    assert snap["stage"] == "done" and snap["outcome"] == "finished" and snap["ended"] is not None
    assert snap["recent"][-1]["text"].startswith("📄 Rendering")

    conn.end_run(run)                   # the first outcome wins
    assert m.outcome == "finished"


def test_a_run_that_never_says_how_it_ended_is_ended_by_end_run(lab_conn):
    gw_app, conn = lab_conn
    run = conn.begin_run("chat-2")
    gw_app._watch_run(conn, run, types.SimpleNamespace(question="q"))
    run.monitor.observe({"type": "planning"})
    conn.end_run(run)
    assert run.monitor.stage == "done" and run.monitor.outcome == "ended"


def test_a_cancelled_plan_reads_cancelled_not_stopped(lab_conn):
    gw_app, conn = lab_conn
    run = conn.begin_run("chat-3")
    gw_app._watch_run(conn, run, types.SimpleNamespace(question="q"))
    run.monitor.observe({"type": "plan_cancelled", "reason": "user"})
    conn.push({"type": "chat_stopped"})
    assert run.monitor.outcome == "cancelled"


def test_watching_never_breaks_the_run(lab_conn):
    gw_app, conn = lab_conn
    run = conn.begin_run("chat-4")
    gw_app._watch_run(conn, run, types.SimpleNamespace(question="q"))
    m = run.monitor
    for junk in ({}, {"type": None}, {"type": "pi_agenda", "agenda": None}, {"type": "lab_plan_dag", "nodes": "x"},
                 {"type": "critic", "verdict": None}, {"type": "tool_start"}, {"type": "agenda_extended", "added": 3}):
        m.observe(junk)
    m.observe_push({"type": object()})
    m.enter("no-such-stage")
    assert m.stage in run_monitor.STAGE_IDS
    # A monitor whose own code fails must not take push down with it.
    m._observe_push = lambda payload: 1 / 0
    conn.push({"type": "lab_progress", "text": "still delivered"})


# --- the other two run kinds -----------------------------------------------------------------------

def test_a_chat_answer_holds_its_tool_and_ends_on_chat_done():
    m = run_monitor.start(kind="chat", owner_id=1, question="what is DDX41?")
    m.enter("chat")
    m.observe({"type": "tool_start", "tool": "literature_search"})
    assert _where(m) == ("chat", [("tool", "literature_search", None)])
    m.observe({"type": "tool_result", "tool": "literature_search", "summary": "3 papers"})
    assert _where(m) == ("chat", [])
    m.observe_push({"type": "chat_done"})
    assert m.stage == "done" and m.outcome == "finished"


def test_a_research_run_does_not_end_on_chat_done():
    # The research path sends chat_done with the report, then cleans up and publishes the files.
    m = run_monitor.start(kind="research")
    m.enter("report")
    m.observe_push({"type": "chat_done"})
    assert m.ended is None and m.stage == "report"


def test_a_qc_only_run_goes_from_its_steps_straight_to_the_report():
    m = run_monitor.start(kind="research")
    m.observe({"type": "scientist_start", "step": "QC"})
    m.observe({"type": "synthesize", "accepted": 1, "qc_only": True})
    assert _where(m) == ("report", [])


# --- steps the DAG scheduler runs side by side ---------------------------------------------------

def test_parallel_steps_are_kept_apart_by_their_thread():
    m = run_monitor.start(kind="research")
    m.observe({"type": "lab_plan_dag", "nodes": [{"id": "s1", "goal": "DE"}, {"id": "s2", "goal": "Literature"}]})
    m.observe({"type": "concurrency_batch", "nodes": ["s1", "s2"]})
    started = threading.Barrier(2)
    release = threading.Event()

    def step(node: str, goal: str, tool: str) -> None:
        m.observe({"type": "node_claim", "node": node, "specialist": "x"})
        m.observe({"type": "scientist_start", "step": f"{goal} (brief)", "specialist": "x"})
        m.observe({"type": "tool_start", "tool": tool})
        started.wait()
        release.wait()
        m.observe({"type": "tool_result", "tool": tool})
        m.observe({"type": "finish"})

    threads = [threading.Thread(target=step, args=("s1", "DE", "run_de")),
               threading.Thread(target=step, args=("s2", "Literature", "deep_literature"))]
    for t in threads:
        t.start()
    for _ in range(200):
        if len(m.snapshot(full=True)["active"]) == 2 and all(a["tool"] for a in m.snapshot(full=True)["active"]):
            break
        threading.Event().wait(0.01)
    active = {(a["n"], a["tool"]) for a in m.snapshot(full=True)["active"]}
    assert active == {(1, "run_de"), (2, "deep_literature")}
    release.set()
    for t in threads:
        t.join()
    # The Critic judges by goal, from this (another) thread: the right step leaves.
    m.observe({"type": "critic", "step": "Literature", "verdict": "accept"})
    assert [(a["n"], a["phase"]) for a in m.snapshot(full=True)["active"]] == [(1, "critic")]


# --- who sees what ---------------------------------------------------------------------------------

def _two_runs() -> tuple[RunMonitor, RunMonitor]:
    a = run_monitor.start(kind="research", owner_id=ALICE.id, owner="alice", question="alice's secret study")
    a.observe({"type": "scientist_start", "step": "DE between KO and WT"})
    a.observe({"type": "tool_start", "tool": "run_de"})
    a.observe_push({"type": "lab_progress", "text": "🔬 DE between KO and WT"})
    b = run_monitor.start(kind="chat", owner_id=MALLORY.id, owner="mallory", question="mallory's question")
    return a, b


def test_another_accounts_run_is_a_position_only():
    a, _ = _two_runs()
    view = run_monitor.live_view(MALLORY.id, admin=False, accounts=True)
    theirs = next(r for r in view["runs"] if r["key"] == a.key)
    mine = next(r for r in view["runs"] if r["key"] != a.key)
    assert mine["mine"] and mine["question"] == "mallory's question"
    assert not theirs["mine"] and "owner" not in theirs
    assert theirs["stage"] == "execute" and theirs["active"] == [
        {"phase": "tool", "tool": "run_de", "since": theirs["active"][0]["since"]}]
    flat = repr(theirs)
    assert "secret" not in flat and "KO and WT" not in flat and "recent" not in theirs
    assert view["running"] == 2


def test_an_admin_sees_every_run_with_its_owner_and_no_accounts_means_one_operator():
    a, _ = _two_runs()
    admin = next(r for r in run_monitor.live_view(ADMIN.id, admin=True, accounts=True)["runs"] if r["key"] == a.key)
    assert admin["owner"] == "alice" and admin["question"] == "alice's secret study" and not admin["mine"]
    solo = run_monitor.live_view(None, admin=False, accounts=False)["runs"]
    assert all(r["mine"] and "question" in r for r in solo)


def test_finished_runs_leave_after_the_retention_window(monkeypatch):
    m = run_monitor.start(kind="chat")
    m.end("finished")
    assert run_monitor.monitors() == [m]
    monkeypatch.setattr(run_monitor, "RETAIN_SECONDS", -1)
    assert run_monitor.monitors() == []


def test_the_live_endpoint_needs_a_login_and_keeps_runs_to_their_owner(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from aiscientist.gateway import app as gw_app

    from aiscientist.gateway.settings import HPCSettings

    a, _ = _two_runs()
    who = {"user": None}
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: who["user"])
    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="alice")
    conn.app_user_id, conn.status = ALICE.id, "ready"
    monkeypatch.setitem(gw_app.CONNECTIONS, conn.id, conn)
    client = TestClient(gw_app.app)
    assert client.get("/api/system/live").status_code == 401
    who["user"] = MALLORY
    res = client.get("/api/system/live")
    body = res.json()
    theirs = next(r for r in body["runs"] if r["key"] == a.key)
    assert "question" not in theirs and body["running"] == 2
    # Sessions are counted, never named: no connection id, no account of theirs, in the answer.
    assert body["sessions"] == 1 and conn.id not in res.text and "alice" not in res.text
    loop.close()
    who["user"] = ALICE
    mine = next(r for r in client.get("/api/system/live").json()["runs"] if r["key"] == a.key)
    assert mine["question"] == "alice's secret study" and mine["active"][0]["step"] == "DE between KO and WT"


# --- the graph draws exactly these stages ----------------------------------------------------------

def test_the_graph_stages_are_the_monitors_stages():
    g = system_info.workflow_graph()
    stage_nodes = [n for n in g["nodes"] if n["type"] == "stage"]
    assert [n["id"] for n in stage_nodes] == list(run_monitor.STAGE_IDS)
    ids = {n["id"] for n in g["nodes"]}
    flow = {(e["source"], e["target"]) for e in g["edges"] if e["type"] == "flow"}
    for kind in run_monitor.KINDS:
        chain = [s.id for s in run_monitor.STAGES if kind in s.kinds]
        assert chain[0] == "request" and chain[-1] == "done"
        assert all((a, b) in flow for a, b in zip(chain, chain[1:]))
    # Every stage the monitor can put a run in is drawn.
    reachable = set(run_monitor._STAGE_OF_EVENT.values()) | set(run_monitor._STAGE_OF_ROLE.values()) \
        | {"request", "approve", "data", "report", "literature", "chat", "corpus", "done"}
    assert reachable <= ids
    # Every role a stage names is explained on the Model node, and every agent's role is at work somewhere.
    stage_roles = {r for s in run_monitor.STAGES for r in s.roles}
    assert stage_roles <= set(system_info.MODEL_ROLES)
    assert {a["role"] for a in system_info.AGENTS} <= stage_roles
