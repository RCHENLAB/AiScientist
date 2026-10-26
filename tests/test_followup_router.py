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

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402


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
    def fake(c, req, *, resume=None, resume_run_id=None, resume_decisions=None, prior=None):
        cap.update(path="run_lab", question=req.question, resume=resume,
                   run_id=resume_run_id, decisions=resume_decisions, prior=prior)
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
    # A picked pipeline stays ticked for the whole chat, so it does NOT make a message a new study
    # (it used to, which restarted every later message from the raw input).
    assert gw_app._followup_target(conn, gw_app.LabRequest(connection_id=conn.id, question="x", preset="scrna")) is not None
    assert gw_app._followup_target(conn, gw_app.LabRequest(connection_id=conn.id, question="x", presets=["scrna"])) is not None
    # a DIFFERENT dataset does mean "new study" -> not eligible
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


def test_rerun_without_checkpoints_redoes_the_step_from_the_processed_dataset(tmp_path, monkeypatch):
    # A mid-pipeline re-run needs the upstream checkpoints, and those are deleted the moment the
    # report is written. It used to fall back to the whole study from the raw input; what the run
    # handed on is its processed dataset, so the step is redone from there.
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_bundle(conn, with_checkpoint=False)
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_run_lab", _fake_run_lab(cap))
    _patch_llm(monkeypatch, {"intent": "rerun_step",
                             "step": "Differential expression per cluster", "confidence": 0.9})
    _dispatch(conn, "redo the differential expression")
    assert cap["path"] == "run_lab" and cap["resume"] is None
    assert cap["prior"]["run_id"] == "run123"


def test_redoing_qc_without_checkpoints_starts_again_from_the_input(tmp_path, monkeypatch):
    # The processed dataset holds only the cells the first QC kept, so QC cannot be redone from it.
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_bundle(conn, with_checkpoint=False,
                 agenda=["**Provenance** — Establish what the matrix is with `run_code`.",
                         "**QC** — Filter cells with `run_scanpy_qc` at 10% mitochondrial.",
                         "**Clustering** — Cluster with `run_clustering`."])
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_run_lab", _fake_run_lab(cap))
    _patch_llm(monkeypatch, {"intent": "rerun_step", "confidence": 0.9,
                             "step": "**QC** — Filter cells with `run_scanpy_qc` at 10% mitochondrial."})
    _dispatch(conn, "redo the QC at 20% mitochondrial")
    assert cap["path"] == "run_lab" and cap["prior"] is None and cap["resume"] is None


def test_a_new_analysis_on_the_same_data_continues_from_the_results(tmp_path, monkeypatch):
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_bundle(conn)
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_run_lab", _fake_run_lab(cap))
    _patch_llm(monkeypatch, {"intent": "continue", "confidence": 0.9})
    _dispatch(conn, "now annotate the cell types")
    assert cap["resume"] is None and cap["run_id"] is None          # a NEW run, not a re-entry
    assert cap["prior"]["run_id"] == "run123"
    assert cap["prior"]["question"] == "characterize the DDX41 dataset"
    assert [s["title"] for s in cap["prior"]["steps"]][:2] == ["Run QC and report the metrics",
                                                              "Cluster the cells"]


def test_a_picked_pipeline_continues_unless_the_message_starts_over(tmp_path, monkeypatch):
    """The console keeps a picked pipeline ticked for the whole chat. With one ticked, a later
    message plans that pipeline ON the earlier results; only "start over" goes back to the input."""
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_bundle(conn)
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_run_lab", _fake_run_lab(cap))
    _patch_llm(monkeypatch, {"intent": "rerun_step", "step": "Cluster the cells", "confidence": 0.9})
    _dispatch(conn, "compare with the atlas", presets=["celltype_annotation"])
    assert cap["prior"]["run_id"] == "run123" and cap["resume"] is None   # never a one-step re-run
    cap.clear()
    _patch_llm(monkeypatch, {"intent": "new_study", "confidence": 0.9})
    _dispatch(conn, "start over from the raw data", presets=["celltype_annotation"])
    assert cap["prior"] is None and cap["resume"] is None


def test_the_bound_dataset_is_compared_as_it_was_bound(tmp_path):
    """A bound FOLDER is staged as the matrix inside it, and a continuing run as the processed
    dataset it started from: neither name matches what the browser keeps sending, so comparing
    names called every later message a new study."""
    conn = _conn(tmp_path, last_run_id="run123")
    art = _seed_bundle(conn, dataset_path="/ws/run123/staged/filtered_feature_bc_matrix.h5")
    state_path = art / "process" / "run_state.json"
    state = json.loads(state_path.read_text())
    req = gw_app.LabRequest(connection_id=conn.id, question="x", dataset_path="/data/shared/Sample1_WT")
    assert gw_app._followup_target(conn, req) is None                # names differ, nothing recorded
    state["bound_primary"] = "/data/shared/Sample1_WT"
    state_path.write_text(json.dumps(state))
    assert gw_app._followup_target(conn, req) is not None
    other = gw_app.LabRequest(connection_id=conn.id, question="x", dataset_path="/data/shared/Sample2_KO")
    assert gw_app._followup_target(conn, other) is None


def test_low_confidence_asks_then_routes_to_choice(tmp_path, monkeypatch):
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_bundle(conn)
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_run_lab", _fake_run_lab(cap))
    _patch_llm(monkeypatch, {"intent": "rerun_step", "confidence": 0.2})   # below threshold
    # The clarify card is answered "start a brand-new analysis".
    async def fake_ask(_conn):
        return "new_study"
    monkeypatch.setattr(gw_app, "_ask_followup_clarify", fake_ask)
    _dispatch(conn, "hmm can you tweak it")
    assert cap["path"] == "run_lab"


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


@pytest.mark.parametrize("answer,expected", [
    ("Continue from these results with a new analysis", "continue"),
    ("Start a brand-new analysis", "new_study"),
])
def test_the_clarify_card_offers_continuing_from_the_results(tmp_path, answer, expected):
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append

    def reply():
        for _ in range(400):
            if conn.pending_plan is not None:
                break
            time.sleep(0.005)
        conn.plan_value = {"action": "revise",
                           "feedback": f"What would you like me to do with this message?: {answer}"}
        conn.plan_event.set()

    t = threading.Thread(target=reply)
    t.start()
    choice = asyncio.run(gw_app._ask_followup_clarify(conn))
    t.join()
    assert choice == expected
    card = next(e for e in pushed if e.get("type") == "plan_clarify")
    assert card["questions"][0]["options"][0].startswith("Continue from these results")
