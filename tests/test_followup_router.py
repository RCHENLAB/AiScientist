"""Offline tests for the follow-up intent router (/api/lab -> _dispatch_lab).

A typed follow-up after a completed run is classified by the session LLM and forwarded to the
right existing path — edit the report (A1), re-run one step (A2), or a fresh study — instead of
always minting a new figure-less bundle. The heavy _run_lab / _regenerate_report tasks are
monkeypatched to capturing stubs, and the classifier LLM is a scripted complete_fn, so this
validates routing + guards only (no GPU, no model, no analysis).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway import app as gw_app  # noqa: E402
from bioagent.gateway.settings import HPCSettings  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_conns():
    yield
    gw_app.CONNECTIONS.clear()


def _conn(tmp_path, **attrs):
    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = object()             # warm model by default (classification runs)
    for k, v in attrs.items():
        setattr(conn, k, v)
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


def _seed_bundle(conn, run_id="run123", *, agenda=None, dataset_path="/data/a.h5ad",
                 with_checkpoint=True):
    """A prior completed run: report.md + run_state.json (+ optional analysis checkpoint)."""
    art = conn.workspace / run_id / "artifacts"
    (art / "process").mkdir(parents=True)
    (art / "report").mkdir(parents=True)
    (art / "report" / "report.md").write_text("# Report\n\n![f](figures/umap.png)\n", encoding="utf-8")
    agenda = agenda or ["Run QC and report the metrics", "Cluster the cells",
                        "Differential expression per cluster",
                        "Search literature for contextual grounding"]
    state = {"question": "characterize the DDX41 dataset", "agenda": agenda,
             "rounds": [], "converged": False, "accepted_steps": 2,
             "guidance": "SKILL", "dataset_path": dataset_path}
    (art / "process" / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    if with_checkpoint:
        work = conn.workspace / run_id / "work"
        work.mkdir(parents=True, exist_ok=True)
        (work / "adata_clustered.h5ad").write_bytes(b"x")
    return art


def _fake_run_lab(cap):
    def fake(c, req, *, resume=None, resume_run_id=None, resume_decisions=None):
        cap.update(path="run_lab", question=req.question, resume=resume,
                   run_id=resume_run_id, decisions=resume_decisions)
        async def _noop():
            return None
        return _noop()
    return fake


def _fake_regen(cap):
    def fake(c, run_id, art, basename, instruction):
        cap.update(path="regenerate", run_id=run_id, basename=basename, instruction=instruction)
        async def _noop():
            return None
        return _noop()
    return fake


def _patch_llm(monkeypatch, obj):
    """Force the router's classifier to return `obj` (dict or raw string)."""
    payload = obj if isinstance(obj, str) else json.dumps(obj)
    monkeypatch.setattr(gw_app, "_lab_llm",
                        lambda conn: ((lambda messages: payload), None, "m", "vLLM", None))


def _dispatch(conn, question, **req_kw):
    req = gw_app.LabRequest(connection_id=conn.id, question=question, **req_kw)
    asyncio.run(gw_app._dispatch_lab(conn, req))


# --- pure helpers ------------------------------------------------------------

def test_extract_json_object_tolerates_fences_and_prose():
    assert gw_app._extract_json_object('```json\n{"intent":"new_study"}\n```')["intent"] == "new_study"
    assert gw_app._extract_json_object('sure: {"a": 1} done')["a"] == 1
    assert gw_app._extract_json_object("not json at all") is None


def test_match_agenda_step_exact_substring_and_overlap():
    agenda = ["Run QC and report the metrics", "Cluster the cells", "Differential expression per cluster"]
    assert gw_app._match_agenda_step("Cluster the cells", agenda) == 1        # exact
    assert gw_app._match_agenda_step("differential expression", agenda) == 2  # substring
    assert gw_app._match_agenda_step("do the QC metrics again", agenda) == 0  # token overlap
    assert gw_app._match_agenda_step("", agenda) is None


def test_parse_followup_intent_rejects_bad_and_unlocatable():
    agenda = ["Run QC", "Cluster the cells"]
    assert gw_app._parse_followup_intent('{"intent":"bogus"}', agenda) is None
    # rerun_step naming a step that matches nothing -> ambiguous (None), so the caller asks
    assert gw_app._parse_followup_intent(
        '{"intent":"rerun_step","step":"zzz nonexistent","confidence":0.9}', agenda) is None
    ok = gw_app._parse_followup_intent(
        '{"intent":"rerun_step","step":"Cluster the cells","confidence":0.8}', agenda)
    assert ok["intent"] == "rerun_step" and ok["step_index"] == 1 and ok["confidence"] == 0.8


def test_default_rerun_index_prefers_literature_step():
    st = {"agenda": ["Run QC", "Cluster", "Search literature for grounding"]}
    assert gw_app._default_rerun_index(st) == 2
    assert gw_app._default_rerun_index({"agenda": ["Run QC", "Cluster"]}) == 1   # else last step


def test_followup_target_eligibility(tmp_path):
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_bundle(conn)
    assert gw_app._followup_target(conn, gw_app.LabRequest(connection_id=conn.id, question="x")) is not None
    # plan-mode is CHECKED by default and does NOT disqualify — else every follow-up ("generate the
    # report") re-ran from scratch. The classifier decides intent; a new_study still honors plan_mode.
    assert gw_app._followup_target(conn, gw_app.LabRequest(connection_id=conn.id, question="x", plan_mode=True)) is not None
    # a FORCED skill (preset/presets) or a DIFFERENT dataset DO mean "new study" -> not eligible
    assert gw_app._followup_target(conn, gw_app.LabRequest(connection_id=conn.id, question="x", preset="scrna")) is None
    assert gw_app._followup_target(conn, gw_app.LabRequest(connection_id=conn.id, question="x", presets=["scrna"])) is None
    assert gw_app._followup_target(conn, gw_app.LabRequest(connection_id=conn.id, question="x",
                                                           dataset_path="/data/OTHER.h5ad")) is None
    # no prior run at all
    conn2 = _conn(tmp_path, last_run_id="")
    assert gw_app._followup_target(conn2, gw_app.LabRequest(connection_id=conn2.id, question="x")) is None


# --- dispatch routing --------------------------------------------------------

def test_no_prior_run_is_a_fresh_study(tmp_path, monkeypatch):
    conn = _conn(tmp_path, last_run_id="")
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_run_lab", _fake_run_lab(cap))
    _dispatch(conn, "analyze my new dataset")
    assert cap["path"] == "run_lab" and cap["resume"] is None    # brand-new run


def test_classify_new_study_runs_fresh(tmp_path, monkeypatch):
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_bundle(conn)
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_run_lab", _fake_run_lab(cap))
    _patch_llm(monkeypatch, {"intent": "new_study", "confidence": 0.95})
    _dispatch(conn, "now run a totally different trajectory analysis")
    assert cap["path"] == "run_lab" and cap["resume"] is None


def test_classify_edit_report_regenerates(tmp_path, monkeypatch):
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_bundle(conn)
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_regenerate_report", _fake_regen(cap))
    _patch_llm(monkeypatch, {"intent": "edit_report", "confidence": 0.9})
    _dispatch(conn, "make the discussion shorter and fix the title")
    assert cap["path"] == "regenerate"
    assert cap["run_id"] == "run123" and cap["basename"] == "report"
    assert cap["instruction"] == "make the discussion shorter and fix the title"


def test_plan_mode_followup_still_routes_to_edit(tmp_path, monkeypatch):
    # THE reported regression: "Plan first" is checked by default, so a follow-up like
    # "continue to generate the report" used to fall through to a FRESH full re-run (a new 5-step
    # plan). It must now route to the report path even with plan_mode=True.
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_bundle(conn)
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_regenerate_report", _fake_regen(cap))
    monkeypatch.setattr(gw_app, "_run_lab", _fake_run_lab({}))   # a fresh run here would be the bug
    _patch_llm(monkeypatch, {"intent": "edit_report", "confidence": 0.9})
    _dispatch(conn, "continue to generate the report", plan_mode=True)
    assert cap["path"] == "regenerate" and cap["run_id"] == "run123"


def test_classify_rerun_step_continues_in_place(tmp_path, monkeypatch):
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_bundle(conn)
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_run_lab", _fake_run_lab(cap))
    _patch_llm(monkeypatch, {"intent": "rerun_step",
                             "step": "Search literature for contextual grounding", "confidence": 0.88})
    _dispatch(conn, "please search the literature again with better terms")
    assert cap["path"] == "run_lab"
    assert cap["run_id"] == "run123"                       # SAME run/dir (figures kept)
    assert cap["resume"] is not None
    assert cap["resume"].from_step_index == 3              # the literature step (0-based)
    assert cap["resume"].modify_note == "please search the literature again with better terms"


def test_rerun_degrades_to_edit_when_checkpoints_expired(tmp_path, monkeypatch):
    # A mid-pipeline re-run needs the upstream checkpoints. If they've expired, don't silently
    # produce nothing — degrade to an in-place report edit (A1) so the figures survive.
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_bundle(conn, with_checkpoint=False)
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_regenerate_report", _fake_regen(cap))
    monkeypatch.setattr(gw_app, "_run_lab", _fake_run_lab({}))
    _patch_llm(monkeypatch, {"intent": "rerun_step",
                             "step": "Differential expression per cluster", "confidence": 0.9})
    _dispatch(conn, "redo the differential expression")
    assert cap["path"] == "regenerate"                     # fell back to A1, figures preserved


def test_low_confidence_asks_then_routes_to_choice(tmp_path, monkeypatch):
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_bundle(conn)
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_regenerate_report", _fake_regen(cap))
    _patch_llm(monkeypatch, {"intent": "edit_report", "confidence": 0.2})   # below threshold
    # The clarify card is answered "edit report".
    async def fake_ask(_conn):
        return "edit_report"
    monkeypatch.setattr(gw_app, "_ask_followup_clarify", fake_ask)
    _dispatch(conn, "hmm can you tweak it")
    assert cap["path"] == "regenerate"


def test_cold_model_asks_without_classifying(tmp_path, monkeypatch):
    # No GPU allocated -> don't pay a cold-start to judge one sentence; ask instead.
    conn = _conn(tmp_path, last_run_id="run123", alloc=None)
    _seed_bundle(conn)
    called = {"classified": False}
    monkeypatch.setattr(gw_app, "_lab_llm",
                        lambda c: ((lambda m: called.__setitem__("classified", True) or "{}"),
                                   None, "m", "v", None))
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_run_lab", _fake_run_lab(cap))
    async def fake_ask(_conn):
        return "new_study"
    monkeypatch.setattr(gw_app, "_ask_followup_clarify", fake_ask)
    _dispatch(conn, "do something")
    assert called["classified"] is False                   # classifier never invoked
    assert cap["path"] == "run_lab" and cap["resume"] is None


def test_ask_followup_clarify_maps_chip_answer(tmp_path):
    conn = _conn(tmp_path)

    def answer():
        for _ in range(400):
            if conn.pending_plan is not None:
                break
            time.sleep(0.005)
        conn.plan_value = {"action": "revise",
                           "feedback": "What would you like me to do with this message?: "
                                       "Re-run a step and update the report"}
        conn.plan_event.set()

    t = threading.Thread(target=answer)
    t.start()
    choice = asyncio.run(gw_app._ask_followup_clarify(conn))
    t.join()
    assert choice == "rerun_step"
