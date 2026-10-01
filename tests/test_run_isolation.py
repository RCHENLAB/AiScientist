"""Offline tests for per-run + per-conversation isolation on a shared SSH/GPU Connection.

One Connection is shared across a user's windows/tabs/conversations, so run state used to collide:
a cancel/approve in one window hit whatever run was live, streamed events landed in the selected
window, and a fresh conversation inherited the connection's stale ``last_run_id`` as a "replan".
These tests pin the fix — per-run ``RunState`` events, event tagging, targeted cancel/approve, a
per-conversation last run, and skipping the report for a cancelled/empty run. No cluster/SSH/GPU.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from bioagent.gateway import app as gw_app  # noqa: E402
from bioagent.gateway.settings import HPCSettings  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_conns():
    yield
    gw_app.CONNECTIONS.clear()


def _conn(tmp_path=None, **attrs):
    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    if tmp_path is not None:
        conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = object()
    for k, v in attrs.items():
        setattr(conn, k, v)
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


def _seed_bundle(conn, run_id, dataset_path="/data/a.h5ad"):
    art = conn.workspace / run_id / "artifacts"
    (art / "process").mkdir(parents=True)
    (art / "report").mkdir(parents=True)
    (art / "report" / "report.md").write_text("# Report\n", encoding="utf-8")
    state = {"question": "q", "agenda": ["Run QC", "Cluster"], "rounds": [],
             "converged": False, "accepted_steps": 2, "guidance": "S", "dataset_path": dataset_path}
    (art / "process" / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    return art


# --- event tagging -----------------------------------------------------------

def test_push_and_emit_stamp_events_with_run_and_conversation_ids():
    c = _conn()
    run = c.begin_run("conv-A")
    c.bind_run_id(run, "runA")
    payload = {"type": "chat_token", "token": "x"}
    c.push(payload)                             # no subscribers -> push just stamps + tracks
    assert payload["run_id"] == "runA" and payload["conversation_id"] == "conv-A"
    c.emit("info", "lab", "hello")
    ev = c.log[-1]
    assert ev["run_id"] == "runA" and ev["conversation_id"] == "conv-A"


def test_no_active_run_leaves_events_untagged():
    # Connect-time events (status / duo) fire before any run — they must not be tagged.
    c = _conn()
    payload = {"type": "status"}
    c.push(payload)
    assert "run_id" not in payload and "conversation_id" not in payload


def test_explicit_tag_is_not_clobbered():
    c = _conn()
    run = c.begin_run("conv-A")
    c.bind_run_id(run, "runA")
    payload = {"type": "run_complete", "run_id": "explicit"}
    c.push(payload)
    assert payload["run_id"] == "explicit"                 # an explicit run_id wins
    assert payload["conversation_id"] == "conv-A"          # conversation still added


def test_stream_replay_carries_conversation_id():
    c = _conn()
    run = c.begin_run("conv-9")
    c.bind_run_id(run, "run9")
    c.push({"type": "chat_start"})
    c.push({"type": "chat_token", "token": "hi"})
    out = c.stream_replay_payloads()
    assert out[0]["type"] == "chat_start"
    assert out[0]["run_id"] == "run9" and out[0]["conversation_id"] == "conv-9"


# --- per-run cancel / plan events --------------------------------------------

def test_each_run_has_independent_cancel_and_plan_events():
    c = _conn()
    a = c.begin_run("conv-A"); c.bind_run_id(a, "runA")
    b = c.begin_run("conv-B"); c.bind_run_id(b, "runB")
    assert a.chat_stop is not b.chat_stop and a.plan_event is not b.plan_event
    a.chat_stop.set()
    assert a.chat_stop.is_set() and not b.chat_stop.is_set()   # cancelling A never touches B


def test_resolve_run_only_matches_the_active_run():
    c = _conn()
    a = c.begin_run("conv-A"); c.bind_run_id(a, "runA")
    b = c.begin_run("conv-B"); c.bind_run_id(b, "runB")        # B is now active
    assert c.resolve_run(run_id="runB") is b
    assert c.resolve_run(conversation_id="conv-B") is b
    assert c.resolve_run() is b                                # no ids -> active (back-compat)
    # A stale interaction that names the FINISHED run A resolves to nothing, not to live run B.
    assert c.resolve_run(run_id="runA") is None
    assert c.resolve_run(conversation_id="conv-A") is None


def test_legacy_proxies_target_the_active_run():
    c = _conn()
    run = c.begin_run("conv-1")
    c.plan_value = {"action": "approve"}       # setter proxies to the active run
    c.pending_plan = {"kind": "agenda"}
    assert run.plan_value == {"action": "approve"} and run.pending_plan == {"kind": "agenda"}
    assert c.chat_stop is run.chat_stop and c.plan_event is run.plan_event


def test_begin_bind_remember_and_end_run():
    c = _conn()
    run = c.begin_run("conv-1")
    assert c.active_run is run and run.run_id is None
    c.bind_run_id(run, "run1")
    assert c.runs["run1"] is run
    gw_app._remember_run(c, run)
    assert c.last_run_id == "run1" and c.last_run_by_conversation["conv-1"] == "run1"
    c.end_run(run)
    assert c.active_run is None                 # no stale pending plan after the run
    assert c.runs["run1"] is run                # but kept so a late approve/cancel is a no-op


# --- targeted stop / plan endpoints ------------------------------------------

def test_stop_endpoint_targets_only_the_named_conversation():
    conn = _conn(chat_running=True)
    run = conn.begin_run("conv-A")
    conn.bind_run_id(run, "runA")
    client = TestClient(gw_app.app)
    # A Stop from a DIFFERENT conversation must not cancel this run.
    r = client.post("/api/chat/stop", json={"connection_id": conn.id, "conversation_id": "conv-OTHER"})
    assert r.json()["status"] == "idle" and not run.chat_stop.is_set()
    # A Stop for the owning conversation cancels it.
    r = client.post("/api/chat/stop", json={"connection_id": conn.id, "conversation_id": "conv-A"})
    assert r.json()["status"] == "stopping" and run.chat_stop.is_set()


def test_stop_endpoint_without_ids_is_back_compat():
    conn = _conn(chat_running=True)
    run = conn.begin_run("conv-A")
    conn.bind_run_id(run, "runA")
    r = TestClient(gw_app.app).post("/api/chat/stop", json={"connection_id": conn.id})
    assert r.json()["status"] == "stopping" and run.chat_stop.is_set()


def test_summary_exposes_active_run_identity():
    # A reconnecting/reloading client re-adopts the run owner from summary.active_run so the Stop
    # button POSTs the matching conversation_id — otherwise resolve_run no-ops the Stop (a client id
    # that drifted across a WS reconnect) and the in-flight job runs on to its Slurm --time.
    c = _conn(alloc=None)                                      # summary() reads alloc.job_id
    assert c.summary()["active_run"] is None                   # no run yet
    run = c.begin_run("conv-A"); c.bind_run_id(run, "runA")
    assert c.summary()["active_run"] == {"run_id": "runA", "conversation_id": "conv-A"}
    c.end_run(run)
    assert c.summary()["active_run"] is None                   # cleared once the run ends


def test_plan_endpoint_ignores_a_stale_mismatched_conversation():
    conn = _conn(chat_running=True)
    run = conn.begin_run("conv-A")
    conn.bind_run_id(run, "runA")
    run.pending_plan = {"kind": "agenda", "payload": ["a", "b"]}
    client = TestClient(gw_app.app)
    # An approve tagged for a different conversation must not unblock this run's plan.
    r = client.post("/api/lab/plan", json={"connection_id": conn.id, "conversation_id": "conv-OTHER",
                                           "action": "approve"})
    assert r.json()["status"] == "stale" and not run.plan_event.is_set()
    # The owning conversation's approve unblocks it.
    r = client.post("/api/lab/plan", json={"connection_id": conn.id, "conversation_id": "conv-A",
                                           "action": "approve"})
    assert r.json()["action"] == "approve" and run.plan_event.is_set()
    assert run.plan_value["action"] == "approve"


# --- fresh study per conversation --------------------------------------------

def test_followup_is_scoped_per_conversation(tmp_path):
    conn = _conn(tmp_path)
    _seed_bundle(conn, "runX")
    conn.last_run_id = "runX"
    conn.last_run_by_conversation["conv-1"] = "runX"
    # The conversation that produced runX gets an amend-eligible follow-up target...
    t = gw_app._followup_target(
        conn, gw_app.LabRequest(connection_id=conn.id, question="tweak it", conversation_id="conv-1"))
    assert t is not None and t[0] == "runX"
    # ...but a DIFFERENT window/conversation (no prior run of its own) starts a FRESH study, even
    # though the connection-wide last_run_id points at runX.
    assert gw_app._followup_target(
        conn, gw_app.LabRequest(connection_id=conn.id, question="new study", conversation_id="conv-2")) is None


def test_followup_without_conversation_id_uses_connection_last_run(tmp_path):
    # Older clients that don't send a conversation_id keep the connection-wide behaviour.
    conn = _conn(tmp_path, last_run_id="runX")
    _seed_bundle(conn, "runX")
    assert gw_app._followup_target(
        conn, gw_app.LabRequest(connection_id=conn.id, question="tweak it")) is not None


# --- prior run recognised across a gateway restart (DB fallback) --------------

@pytest.fixture
def _auth_db(tmp_path, monkeypatch):
    """A temp DB with accounts enabled + a seeded user. Yields the user_id."""
    from bioagent.gateway import db
    url = f"sqlite:///{(tmp_path / 'iso.db').as_posix()}"
    monkeypatch.setenv("BIOAGENT_DATABASE_URL", url)
    db.reset(url)
    db.init_db()
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    from bioagent.gateway.db import session_scope
    from bioagent.gateway.models import User
    with session_scope() as s:
        u = User(username="tester", password_hash="x")
        s.add(u); s.commit(); uid = u.id
    yield uid
    db.reset()   # drop the cached engine so a later test doesn't reuse this temp DB


def _finished_run(uid, run_id, conversation_id, status="done"):
    from bioagent.gateway import auth_routes
    auth_routes.record_run_start(uid, run_id, "q", plan_mode=False, conversation_id=conversation_id)
    auth_routes.record_run_finish(run_id, status)


def test_latest_run_id_for_conversation_picks_latest_completed(_auth_db):
    from bioagent.gateway import auth_routes
    uid = _auth_db
    _finished_run(uid, "old_done", "conv-1", "done")
    _finished_run(uid, "new_done", "conv-1", "incomplete")   # later + still report-bearing
    _finished_run(uid, "cancelled", "conv-1", "cancelled")   # must be ignored
    _finished_run(uid, "other_conv", "conv-2", "done")       # different conversation
    got = auth_routes.latest_run_id_for_conversation(uid, "conv-1")
    assert got == "new_done"
    assert auth_routes.latest_run_id_for_conversation(uid, "conv-empty") is None


def test_latest_run_id_is_scoped_to_the_user(_auth_db):
    from bioagent.gateway import auth_routes
    from bioagent.gateway.db import session_scope
    from bioagent.gateway.models import User
    uid = _auth_db
    with session_scope() as s:
        other = User(username="other", password_hash="x"); s.add(other); s.commit(); other_id = other.id
    _finished_run(other_id, "theirs", "conv-1", "done")
    assert auth_routes.latest_run_id_for_conversation(uid, "conv-1") is None   # not this user's run


def test_conversation_last_run_prefers_memory_then_falls_back_to_db(_auth_db):
    uid = _auth_db
    _finished_run(uid, "db_run", "conv-1", "done")
    conn = _conn(app_user_id=uid)
    # In-memory hit wins (fast path, current session) — no DB read needed.
    conn.last_run_by_conversation["conv-1"] = "mem_run"
    assert gw_app._conversation_last_run(conn, "conv-1") == "mem_run"
    # Fresh process (empty map, e.g. after a gateway restart) -> recover from the DB + warm the cache.
    conn.last_run_by_conversation.clear()
    assert gw_app._conversation_last_run(conn, "conv-1") == "db_run"
    assert conn.last_run_by_conversation["conv-1"] == "db_run"


def test_typed_followup_survives_restart_via_db(_auth_db, tmp_path):
    # THE scenario: gateway restarted (in-memory last-run map empty), the user reopens the chat and
    # types a follow-up. It must recognise the prior run from the DB and route to its bundle, not
    # start a fresh study.
    uid = _auth_db
    conn = _conn(tmp_path, app_user_id=uid)
    _finished_run(uid, "runDB1", "conv-1", "done")
    _seed_bundle(conn, "runDB1")                    # its bundle still on disk after the restart
    assert not conn.last_run_by_conversation        # nothing cached in this fresh process
    t = gw_app._followup_target(
        conn, gw_app.LabRequest(connection_id=conn.id, question="make it shorter", conversation_id="conv-1"))
    assert t is not None and t[0] == "runDB1"


# --- skip report on a cancelled / empty run ----------------------------------

def test_run_produced_nothing_detects_cancel_and_empty():
    from bioagent.agents.research_lab import LabResult
    cancelled = LabResult("q", ["a", "b"], [], False, 0, "cancelled during review")
    assert gw_app._run_produced_nothing(cancelled, plan_cancelled=True) is True
    # 0 accepted AND no rounds executed -> nothing to write up (placeholder, dataless report).
    assert gw_app._run_produced_nothing(cancelled, plan_cancelled=False) is True
    # Executed steps but the critic accepted none: rounds exist -> STILL write up what ran.
    attempted = LabResult("q", ["a"], [object()], False, 0, "x")
    assert gw_app._run_produced_nothing(attempted, plan_cancelled=False) is False
    # A converged run with accepted work -> report.
    ok = LabResult("q", ["a"], [object()], True, 1, "done")
    assert gw_app._run_produced_nothing(ok, plan_cancelled=False) is False


def test_run_lab_skips_report_when_plan_is_cancelled(tmp_path, monkeypatch):
    """End-to-end through _run_lab: a plan cancelled during review renders NO report, writes no
    run_state, and does not become the conversation's last run — so the next message is a fresh,
    dataset-bound study (fixes the timeout→replan dataset-unbind + the placeholder dataless report)."""
    from bioagent.agents import research_lab as rl
    from bioagent.agents.research_lab import LabResult

    class _FakeLab:
        def __init__(self, *a, **k):
            self.guidance = None

        # **_kw so a new optional control callable on ResearchLab.run (should_compact, …) does not
        # break this double — the test is about the cancelled-plan path, not the signature.
        def run(self, question, on_event=None, plan_review=None, should_cancel=None,
                pull_injections=None, resume=None, decision_review=None, **_kw):
            if on_event:
                on_event({"type": "plan_cancelled"})       # user cancelled / review timed out
            return LabResult(question, ["Run QC", "Cluster"], [], False, 0,
                             "Run cancelled by the user during plan review — no tools were executed.")

    monkeypatch.setattr(rl, "ResearchLab", _FakeLab)

    def _boom_report(*a, **k):                              # the report path must NOT run
        raise AssertionError("_build_report should not be called for a cancelled plan")
    monkeypatch.setattr(gw_app, "_build_report", _boom_report)

    conn = _conn(tmp_path)
    published: list = []
    monkeypatch.setattr(conn, "_publish", lambda payload: published.append(dict(payload)))

    req = gw_app.LabRequest(connection_id=conn.id, question="analyze this",
                            conversation_id="conv-1", plan_mode=True)
    conn.begin_run("conv-1")
    asyncio.run(gw_app._run_lab(conn, req))

    types = [p.get("type") for p in published]
    assert "chat_stopped" in types                          # finished as a stop, not a completion
    assert "artifacts" not in types and "run_complete" not in types   # no bundle published
    assert conn.last_run_id is None                         # not remembered as a real run…
    assert "conv-1" not in conn.last_run_by_conversation    # …in this conversation either
    run_id = conn.active_run.run_id if conn.active_run else None
    # No run_state.json written for a cancelled run (nothing to resume/regenerate).
    assert run_id is None or not (conn.workspace / run_id / "artifacts" / "process" / "run_state.json").exists()
    assert conn.chat_running is False


# --- Stop covers the write-up too --------------------------------------------
#
# The first Stop ends the ANALYSIS loop and nothing more: the tail below it (report → literature →
# self-review → PDF/DOCX render → post-render reviews → technical report) always ran, by design, so
# a stopped run still ships a manuscript for the steps that completed. That salvage is worth
# keeping — but it is five more model calls and two renders (run ed4cfce52a2a: 11 minutes on a
# metered model, after a button that said "ending the current run"), and there was no way out of
# it. A second Stop is now that way out.

def test_a_second_stop_escalates_to_abandoning_the_write_up():
    conn = _conn(chat_running=True)
    run = conn.begin_run("conv-A")
    conn.bind_run_id(run, "runA")
    client = TestClient(gw_app.app)

    r = client.post("/api/chat/stop", json={"connection_id": conn.id, "conversation_id": "conv-A"})
    assert r.json()["status"] == "stopping"
    assert run.chat_stop.is_set() and not run.hard_stop.is_set()   # analysis only — write-up still runs

    r = client.post("/api/chat/stop", json={"connection_id": conn.id, "conversation_id": "conv-A"})
    assert r.json()["status"] == "abandoning"
    assert run.hard_stop.is_set()


def _stopped_lab_result(question="analyze this"):
    """A LabResult shaped like a mid-run Stop: one accepted round, not converged. This is the case
    _run_produced_nothing deliberately lets through to the write-up."""
    from bioagent.agents.research_lab import CriticVerdict, LabResult, LabRound
    rnd = LabRound(1, 1, "Run QC", "Scientist",
                   {"final_answer": "3,000 cells passed QC.", "evidence": []},
                   CriticVerdict("accept", 0.9, ""))
    return LabResult(question, ["Run QC", "Cluster"], [rnd], False, 1,
                     "Run stopped by the user before completion — 1/2 planned steps completed:\n"
                     "- Step 1 (Run QC): 3,000 cells passed QC.")


def _lab_stopped_during(conn, monkeypatch, *, presses: int):
    """Install a ResearchLab double whose loop is interrupted by `presses` Stop clicks, exactly as
    the endpoint would set them — mid-run, i.e. AFTER _run_lab has armed the run's flags."""
    from bioagent.agents import research_lab as rl

    class _FakeLab:
        def __init__(self, *a, **k):
            self.guidance = None
            self.protocols = []
            self.self_sourced = ""
            self.execution_profile = {}

        # **_kw so a new optional control callable on ResearchLab.run does not break the double.
        def run(self, question, on_event=None, plan_review=None, should_cancel=None,
                pull_injections=None, resume=None, decision_review=None, **_kw):
            run = conn.active_run
            if presses >= 1:
                run.chat_stop.set()
            if presses >= 2:
                run.hard_stop.set()
            return _stopped_lab_result(question)

    monkeypatch.setattr(rl, "ResearchLab", _FakeLab)
    monkeypatch.setattr(conn, "_publish", lambda payload: None)
    return gw_app.LabRequest(connection_id=conn.id, question="analyze this", conversation_id="conv-1")


def test_run_lab_still_writes_up_a_single_stop(tmp_path, monkeypatch):
    """The salvage path, pinned. Without it the escalation test below could pass because the
    write-up broke rather than because Stop skipped it."""
    conn = _conn(tmp_path)
    req = _lab_stopped_during(conn, monkeypatch, presses=1)
    called: list[str] = []
    monkeypatch.setattr(gw_app, "_build_report", lambda *a, **k: called.append("report") or "# R\n")
    monkeypatch.setattr(gw_app, "_review_and_finalize_report",
                        lambda md, *a, **k: called.append("review") or md)
    conn.begin_run("conv-1")
    asyncio.run(gw_app._run_lab(conn, req))

    assert called == ["report", "review"]                 # the model still wrote the manuscript


def test_a_second_stop_skips_the_model_written_write_up_but_keeps_the_bundle(tmp_path, monkeypatch):
    """Escalated Stop: no report call, no self-review, no render, no technical report — and still a
    report.md and a resumable run_state, because every result they describe was already on disk
    before Stop was pressed. Abandoning costs prose, never data."""
    conn = _conn(tmp_path)
    req = _lab_stopped_during(conn, monkeypatch, presses=2)

    def _boom(name):
        def _fail(*a, **k):
            raise AssertionError(f"{name} must not run once the user abandoned the write-up")
        return _fail

    for fn in ("_build_report", "_review_and_finalize_report", "_build_technical_report"):
        monkeypatch.setattr(gw_app, fn, _boom(fn))
    # _run_lab imports the renderer inside the function, so patch it at its source module.
    from bioagent.reporting import report as report_mod
    monkeypatch.setattr(report_mod, "build_pdf_report", _boom("build_pdf_report"))

    conn.begin_run("conv-1")
    asyncio.run(gw_app._run_lab(conn, req))

    run_id = conn.last_run_id
    assert run_id, "an abandoned write-up still leaves a remembered, resumable run"
    art = conn.workspace / run_id / "artifacts"
    md = (art / "report" / "report.md").read_text()
    assert "write-up skipped" in md.lower()
    assert "1/2 planned steps completed" in md            # what actually ran is still stated
    assert (art / "process" / "run_state.json").exists()  # …and the run can still be resumed


def test_an_abandoned_write_up_does_not_leak_into_the_next_run(tmp_path, monkeypatch):
    """_run_lab REUSES ``conn.active_run`` when one exists, so a stop flag left set by a previous
    run would make the next run skip its own report having never been stopped. Worth pinning: the
    symptom (a missing manuscript) looks nothing like the cause."""
    conn = _conn(tmp_path)
    req = _lab_stopped_during(conn, monkeypatch, presses=0)
    called: list[str] = []
    monkeypatch.setattr(gw_app, "_build_report", lambda *a, **k: called.append("report") or "# R\n")
    monkeypatch.setattr(gw_app, "_review_and_finalize_report", lambda md, *a, **k: md)

    run = conn.begin_run("conv-1")
    run.chat_stop.set()
    run.hard_stop.set()                                   # left over from a PREVIOUS, abandoned run
    asyncio.run(gw_app._run_lab(conn, req))

    assert called == ["report"]                           # armed fresh — this run wrote its report


# --- decision points and Stop ------------------------------------------------------------------


class _Fork:
    goal = "Step failed after 3 attempts: Validate existing labels"
    options = ["Retry with a narrower panel", "Skip this step"]


def test_a_decision_raised_after_stop_is_not_asked():
    """Run f107bcf7b660: Stop x2 ended the step, the failed step then raised a failure fork, and
    the stopped run waited out the full timeout on a card nobody would answer."""
    conn = _conn(chat_running=True)
    run = conn.begin_run("conv-A")
    run.chat_stop.set()
    pushed: list[dict] = []
    conn.push = pushed.append
    out = gw_app._decision_review(conn, lambda *a, **k: None, _Fork(), timeout=30)
    assert out == {"action": "cancel"}
    assert pushed == [] and run.pending_plan is None


def test_a_decision_is_asked_and_answered_when_the_run_is_live():
    import threading
    conn = _conn(chat_running=True)
    run = conn.begin_run("conv-A")
    pushed: list[dict] = []
    conn.push = pushed.append

    def answer():
        for _ in range(200):
            if run.pending_plan is not None:
                run.plan_value = {"action": "approve", "feedback": "Skip this step"}
                run.plan_event.set()
                return
            threading.Event().wait(0.01)

    threading.Thread(target=answer, daemon=True).start()
    out = gw_app._decision_review(conn, lambda *a, **k: None, _Fork(), timeout=5)
    assert out == {"action": "proceed", "choice": "Skip this step"}
    assert pushed and pushed[0]["type"] == "decision_prompt"
    assert run.pending_plan is None


def test_an_unanswered_decision_proceeds_on_the_agents_judgment():
    conn = _conn(chat_running=True)
    conn.begin_run("conv-A")
    conn.push = lambda *_a: None
    out = gw_app._decision_review(conn, lambda *a, **k: None, _Fork(), timeout=0.05)
    assert out == {"action": "proceed", "timed_out": True}
