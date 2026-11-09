"""A message after a run that stopped part-way carries that run on.

Chat 195 (2026-11-06/07): run fc393bf52a3b had its plan approved, then failed at its first step
because the lab's shared model did not answer (three ten-minute waits). The user switched the
session to Haiku and wrote "I switch to haiku, can you use haiku continue this session research".
Only a FINISHED run was a follow-up target, so that sentence became the whole question of a new
study: the 18 GB atlas was staged again and a design meeting debated a sentence about models.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

QUESTION = ("Where are LRAT, CRALBP, rho and ABCR expressed in this retina atlas? "
            "Show them across the major retinal cell classes.")
AGENDA = ["**QC and normalization** — Run `run_scanpy_qc` on the atlas.",
          "**Resolve and score genes** — Use `run_code` to score LRAT, RLBP1, RHO, ABCA4.",
          "**Per-class summary** — Use `run_code` to tabulate detection per majorclass.",
          "**Dot plot** — Use `run_code` to draw the dot plot.",
          "**UMAP** — Use `run_code` to draw the UMAP."]
HAIKU_MESSAGE = "I switch to haiku, can you use haiku continue this session research"


@pytest.fixture(autouse=True)
def _clean_conns():
    yield
    gw_app.CONNECTIONS.clear()


def _conn(tmp_path, **attrs):
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = None
    for k, v in attrs.items():
        setattr(conn, k, v)
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


def _seed_stopped(conn, run_id="fc393bf52a3b", conversation_id="195", *, rounds=(), plan_state="approved",
                  report=False):
    """A run whose plan was saved and approved, that stopped with ``rounds`` accepted."""
    art = conn.workspace / run_id / "artifacts"
    (art / "process").mkdir(parents=True)
    (art / "plan").mkdir(parents=True)
    state = {"question": QUESTION, "agenda": AGENDA, "rounds": list(rounds), "converged": False,
             "accepted_steps": len(rounds), "conversation_id": conversation_id,
             "dataset_path": "/hpc/HRCA_snRNA_allcells.h5ad",
             "bound_primary": "/data/Users/shared/jinl14/HECA_testdata/h5ad/HRCA_snRNA_allcells.h5ad"}
    (art / "process" / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (art / "plan" / "v01.yaml").write_text("steps: []\n", encoding="utf-8")
    (art / "plan" / "current.json").write_text(
        json.dumps({"version": 1, "filename": "v01.yaml", "state": plan_state}), encoding="utf-8")
    if report:
        (art / "report").mkdir(parents=True)
        (art / "report" / "summary.md").write_text("done", encoding="utf-8")
    conn.last_planned_run_by_conversation[conversation_id] = run_id
    return art


def _accepted(step):
    no = AGENDA.index(step) + 1
    return {"round_no": no, "step_index": no, "step": step, "specialist": "S",
            "scientist_result": {"final_answer": f"did {step}"},
            "verdict": {"verdict": "accept", "score": 0.9, "critique": "ok"}}


def _capture_run_lab(monkeypatch):
    cap: dict = {}

    def fake(c, req, *, resume=None, resume_run_id=None, resume_decisions=None, prior=None, **kw):
        cap.update(question=req.question, resume=resume, run_id=resume_run_id,
                   conversation_id=req.conversation_id)

        async def _noop():
            return None
        return _noop()
    monkeypatch.setattr(gw_app, "_run_lab", fake)
    return cap


def _dispatch(conn, question, conversation_id="195", **kw):
    req = gw_app.LabRequest(connection_id=conn.id, question=question,
                            conversation_id=conversation_id, **kw)
    asyncio.run(gw_app._dispatch_lab(conn, req))


def _no_routing_model(monkeypatch):
    """Route to the lab and screen nothing, as a session whose model cannot be asked would."""
    monkeypatch.setattr(gw_app, "_model_ready", lambda conn: False)
    monkeypatch.setattr(gw_app, "_corpus_curation", lambda *a, **k: None)


# --- finding the stopped run -----------------------------------------------------------------------

def test_the_chat_s_stopped_run_is_found_with_its_plan_and_where_it_stopped(tmp_path):
    conn = _conn(tmp_path)
    _seed_stopped(conn, rounds=[_accepted(AGENDA[0])])
    req = gw_app.LabRequest(connection_id=conn.id, question="continue", conversation_id="195")
    run_id, _art, state, resume = gw_app._stopped_run_target(conn, req)
    assert run_id == "fc393bf52a3b" and state["question"] == QUESTION
    assert resume.from_step_index == 1 and sorted(resume.redo_indices) == [1, 2, 3, 4]


@pytest.mark.parametrize("why", ["finished", "plan waiting", "plan cancelled", "other chat",
                                 "nothing left", "other dataset", "still running"])
def test_a_run_that_is_not_a_stopped_one_is_left_alone(tmp_path, why):
    conn = _conn(tmp_path)
    _seed_stopped(conn, report=(why == "finished"),
                  plan_state={"plan waiting": "shown", "plan cancelled": "cancelled"}.get(why, "approved"),
                  rounds=[_accepted(s) for s in AGENDA] if why == "nothing left" else ())
    kw = {}
    if why == "other dataset":
        kw["dataset_path"] = "/data/Users/shared/other.h5ad"
    if why == "still running":
        run = conn.begin_run("195", run_id="fc393bf52a3b")
        run.running = True
    req = gw_app.LabRequest(connection_id=conn.id, question="continue",
                            conversation_id="2" if why == "other chat" else "195", **kw)
    assert gw_app._stopped_run_target(conn, req) is None


def test_after_a_restart_the_stopped_run_is_found_in_the_db(tmp_path, monkeypatch):
    """The session map is empty on a fresh process: the DB names the chat's latest run of any status."""
    conn = _conn(tmp_path, app_user_id=7)
    _seed_stopped(conn)
    conn.last_planned_run_by_conversation.clear()
    seen = {}

    def latest(uid, cid, accept=None, statuses=("done", "incomplete")):
        seen.update(uid=uid, cid=cid, statuses=statuses)
        return "fc393bf52a3b" if accept("fc393bf52a3b") else None
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app.auth_routes, "latest_run_id_for_conversation", latest)
    req = gw_app.LabRequest(connection_id=conn.id, question="continue", conversation_id="195")
    assert gw_app._stopped_run_target(conn, req)[0] == "fc393bf52a3b"
    assert seen == {"uid": 7, "cid": "195", "statuses": None}


def test_the_db_lookup_can_take_a_run_of_any_status(tmp_path, monkeypatch):
    from aiscientist.gateway import auth_routes, db
    from aiscientist.gateway.db import session_scope
    from aiscientist.gateway.models import User
    url = f"sqlite:///{(tmp_path / 'r.db').as_posix()}"
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", url)
    db.reset(url)
    db.init_db()
    try:
        with session_scope() as s:
            u = User(username="tester", password_hash="x")
            s.add(u)
            s.commit()
            uid = u.id
        auth_routes.record_run_start(uid, "done1", "q", plan_mode=True, conversation_id="195")
        auth_routes.record_run_finish("done1", "done")
        auth_routes.record_run_start(uid, "err1", "q", plan_mode=True, conversation_id="195")
        auth_routes.record_run_finish("err1", "error")
        assert auth_routes.latest_run_id_for_conversation(uid, "195") == "done1"
        assert auth_routes.latest_run_id_for_conversation(uid, "195", statuses=None) == "err1"
    finally:
        db.reset()


# --- routing a message to it -------------------------------------------------------------------------

def test_the_haiku_message_carries_the_stopped_run_on_with_its_question_and_plan(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_stopped(conn)
    _no_routing_model(monkeypatch)
    monkeypatch.setattr(gw_app, "_analysis_checkpoints_exist", lambda *a, **k: True)
    cap = _capture_run_lab(monkeypatch)
    _dispatch(conn, HAIKU_MESSAGE)
    assert cap["run_id"] == "fc393bf52a3b"
    assert cap["question"] == QUESTION                  # not the sentence about models
    assert cap["resume"].from_step_index == 0 and sorted(cap["resume"].redo_indices) == [0, 1, 2, 3, 4]
    assert cap["conversation_id"] == "195"


def test_finished_steps_are_kept_when_the_run_carries_on(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_stopped(conn, rounds=[_accepted(AGENDA[0]), _accepted(AGENDA[1])])
    _no_routing_model(monkeypatch)
    monkeypatch.setattr(gw_app, "_analysis_checkpoints_exist", lambda *a, **k: True)
    cap = _capture_run_lab(monkeypatch)
    _dispatch(conn, "继续")
    assert cap["run_id"] == "fc393bf52a3b" and cap["resume"].from_step_index == 2
    assert sorted(cap["resume"].redo_indices) == [2, 3, 4]


def test_when_the_checkpoints_are_gone_the_saved_plan_runs_again_from_step_one(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_stopped(conn, rounds=[_accepted(AGENDA[0])])
    _no_routing_model(monkeypatch)
    monkeypatch.setattr(gw_app, "_analysis_checkpoints_exist", lambda *a, **k: False)
    cap = _capture_run_lab(monkeypatch)
    _dispatch(conn, "please continue")
    assert cap["run_id"] == "fc393bf52a3b" and cap["question"] == QUESTION
    assert cap["resume"].from_step_index == 0


def test_a_new_question_after_a_stopped_run_is_a_new_study(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_stopped(conn)
    monkeypatch.setattr(gw_app, "_corpus_curation", lambda *a, **k: None)
    monkeypatch.setattr(gw_app, "_model_ready", lambda conn: True)
    replies = {"stopped": '{"intent": "new_study", "confidence": 0.9, "reason": "new question"}'}

    def complete(messages, **kw):
        sys = messages[0]["content"]
        if "STOPPED" in sys:
            return replies["stopped"]
        return '{"route": "research", "confidence": 0.9}'
    monkeypatch.setattr(gw_app, "_lab_llm", lambda conn: (complete, None, "m", "vLLM", None))
    monkeypatch.setattr(gw_app, "_route_request", lambda *a, **k: SimpleNamespace(
        literature=False, route="research", source="test", confidence=1.0, reason=""))
    cap = _capture_run_lab(monkeypatch)
    q = "Now compare the rods of the fovea with the rods of the periphery in the same atlas, please"
    _dispatch(conn, q)
    assert cap["question"] == q and cap["resume"] is None and cap["run_id"] is None


def test_a_doubt_is_put_to_the_user_and_their_answer_is_followed(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_stopped(conn)
    _no_routing_model(monkeypatch)
    monkeypatch.setattr(gw_app, "_analysis_checkpoints_exist", lambda *a, **k: True)
    cap = _capture_run_lab(monkeypatch)
    asked = {}

    def answer():
        for _ in range(400):
            if conn.pending_plan is not None:
                break
            time.sleep(0.005)
        asked["q"] = conn.pending_plan["payload"][0]
        conn.plan_value = {"action": "revise", "feedback": (
            f"{asked['q']['question']}: Continue the stopped analysis from step 1")}
        conn.plan_event.set()
    t = threading.Thread(target=answer)
    t.start()
    _dispatch(conn, "the shared model was down, I picked another one for this session now")
    t.join()
    assert "stopped at step 1 of 5" in asked["q"]["question"]
    assert cap["run_id"] == "fc393bf52a3b" and cap["question"] == QUESTION


def test_the_card_s_question_text_does_not_decide_the_answer(tmp_path):
    """The answer comes back as "<question>: <option>", and the question says "stopped"."""
    conn = _conn(tmp_path)

    def answer():
        for _ in range(400):
            if conn.pending_plan is not None:
                break
            time.sleep(0.005)
        q = conn.pending_plan["payload"][0]["question"]
        conn.plan_value = {"action": "revise", "feedback": f"{q}: Start a new analysis"}
        conn.plan_event.set()
    t = threading.Thread(target=answer)
    t.start()
    assert asyncio.run(gw_app._ask_resume_clarify(conn, 1, 5)) == "new_study"
    t.join()


# --- the error says how to carry on ------------------------------------------------------------------

def test_a_failed_run_s_error_says_its_plan_is_saved_and_how_to_carry_on(tmp_path):
    conn = _conn(tmp_path)
    art = _seed_stopped(conn, rounds=[_accepted(AGENDA[0])])
    hint = gw_app._resume_hint(art)
    assert "Send “continue” to carry on from step 2 of 5" in hint
    assert gw_app._resume_hint(None) == "" and gw_app._resume_hint(tmp_path / "none") == ""


# --- the shared model ---------------------------------------------------------------------------------

def test_a_down_shared_model_is_not_asked_to_route_a_message(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    conn.llm_choice = {"credential_id": gw_app.shared_llm.ENDPOINT_ID}
    monkeypatch.setattr(gw_app, "_session_credential", lambda c: {"id": "shared"})
    monkeypatch.setattr(gw_app.shared_llm, "endpoint_binding", lambda: None)
    assert gw_app._model_ready(conn) is False
    monkeypatch.setattr(gw_app.shared_llm, "endpoint_binding", lambda: object())
    assert gw_app._model_ready(conn) is True


def test_a_run_waits_for_the_shared_model_once_not_at_every_call(monkeypatch):
    """Run fc393bf52a3b waited ten minutes three times before its error."""
    from aiscientist.gateway import shared_llm_gate
    monkeypatch.setattr(gw_app.shared_llm, "endpoint_binding", lambda: None)
    monkeypatch.setattr(shared_llm_gate, "down_wait_s", lambda: 0.3)
    monkeypatch.setattr(gw_app, "_SHARED_DOWN_POLL_S", 0.05)
    run = SimpleNamespace(chat_stop=threading.Event(), hard_stop=threading.Event())

    class Notice:
        def __init__(self):
            self.run = run

        def __call__(self, *a):
            pass

        def cancelled(self):
            return False

    t0 = time.monotonic()
    with pytest.raises(gw_app.GatewayError):
        gw_app._await_shared_binding(Notice())
    first = time.monotonic() - t0
    t1 = time.monotonic()
    with pytest.raises(gw_app.GatewayError):
        gw_app._await_shared_binding(Notice())
    second = time.monotonic() - t1
    assert first >= 0.25 and second < 0.1
    # once it answers again, the run is back to waiting normally
    monkeypatch.setattr(gw_app.shared_llm, "endpoint_binding", lambda: "bound")
    assert gw_app._await_shared_binding(Notice()) == "bound" and run.shared_llm_waited_out is False
