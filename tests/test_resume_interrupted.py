"""An interrupted run continues from its next step.

Run 78a707cd79e9 (local gateway, 2026-10-03) died after 7 accepted steps when its SSH session
dropped. Its ``artifacts/process/run_state.json`` WAS on disk — the lab checkpoints after every
round (8d93adb) — but three things stood between that file and a resumed run:

* nothing could say "continue where it stopped": ``/api/lab/continue`` only re-runs a step the
  caller names (default step 0, i.e. the whole run again);
* it was a DAG run, and DAG / multi-cycle rounds are numbered in completion order, not by agenda
  position (round 5 is agenda step 4 there). A resume keyed by ``step_index`` handed steps each
  other's results and re-ran a finished one;
* its checkpoints were in the run's HPC3 workspace, and the endpoint looked only in the gateway's
  local ``work/``, so it called every HPC3 run "expired".

A resumed run also went without the decisions run-start staging sets (the dataset profile, the
bound folder), because a resume skips staging and run_state.json did not carry them.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

from aiscientist.agents.research_harness import HarnessContext, HarnessResult
from aiscientist.agents.research_lab import LabConfig, LabResult, ResearchLab, ResumeState

AGENDA = ["Run QC", "Cluster the cells", "Marker genes", "ORA over marker sets",
          "Annotate cell types", "Cell-type composition", "Literature search on retina cell types"]


def _round(no: int, step: str, verdict: str = "accept") -> dict:
    return {"round_no": no, "step_index": no, "step": step, "specialist": "S",
            "scientist_result": {"final_answer": f"did {step}"},
            "verdict": {"verdict": verdict, "score": 0.9, "critique": "ok"}}


def _dag_state() -> dict:
    """The shape of 78a707cd79e9's snapshot: rounds in completion order, numbered by round — a
    revise round shifts every later number, and annotation ran before ORA."""
    return {
        "question": "QC, cluster and annotate this retina sample",
        "agenda": list(AGENDA),
        "rounds": [_round(1, "Run QC"), _round(2, "Cluster the cells", "revise"),
                   _round(3, "Cluster the cells"), _round(4, "Marker genes"),
                   _round(5, "Annotate cell types")],
        "converged": False, "accepted_steps": 4, "final_answer": "",
        "guidance": "SKILL", "dataset_path": "/data/sample3",
    }


# -- which steps are done ------------------------------------------------------------------


def test_from_interrupted_keeps_the_finished_steps_and_runs_the_rest():
    resume = ResumeState.from_interrupted(_dag_state())

    assert resume.from_step_index == 3, "ORA is the first step with no accepted result"
    assert resume.redo_indices == frozenset({3, 5, 6})
    assert resume.guidance == "SKILL"


def test_from_interrupted_refuses_a_run_with_nothing_left():
    state = _dag_state()
    state["agenda"] = ["Run QC", "Cluster the cells", "Marker genes", "Annotate cell types"]

    with pytest.raises(ValueError, match="nothing left to continue"):
        ResumeState.from_interrupted(state)


# -- the resumed lab run ---------------------------------------------------------------------


def _lab() -> tuple[ResearchLab, list[str]]:
    """A lab whose Critic accepts everything, recording which steps the Scientist is sent."""
    def complete_fn(messages):
        system = messages[0]["content"]
        if "Principal Investigator" in system and "final research report" in system:
            return "The report."
        if "Critic" in system:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "fine"})
        return json.dumps({"agenda": AGENDA})

    class _Scientist:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

        def run(self, *_a, **_k):
            return HarnessResult(
                status="ok", stop_reason=None, final_answer="did it",
                steps=[{"tool": "run_scanpy_qc", "args": {}, "ok": True, "summary": "done",
                        "result": {"status": "ok", "cells_after": 100,
                                   "figures": ["figures/violin_qc_violin.png"]}}],
                errors=[])

    lab = ResearchLab(HarnessContext(decisions={}, workspace=Path(".")),
                      LabConfig(max_steps=len(AGENDA), auto_select_skill=False),
                      complete_fn=complete_fn, scientist=_Scientist())
    ran: list[str] = []
    real = lab._scientist

    def _recording(question, step, *a, **k):
        ran.append(step)
        return real(question, step, *a, **k)

    lab._scientist = _recording
    return lab, ran


def test_an_interrupted_dag_run_resumes_with_each_result_on_its_own_step():
    lab, ran = _lab()

    result = lab.run("q", resume=ResumeState.from_interrupted(_dag_state()))

    assert ran == ["ORA over marker sets", "Cell-type composition",
                   "Literature search on retina cell types"], "only the unfinished steps run"
    assert [r.step for r in result.rounds] == AGENDA, "every kept result sits on its own step"
    assert result.accepted_steps == len(AGENDA)


def test_redoing_a_named_step_of_a_dag_run_keeps_the_right_upstream_results():
    """The same mis-keying hit an ordinary A2 re-run of a DAG run: re-running annotation re-ran
    clustering and handed ORA the marker-gene result."""
    lab, ran = _lab()

    result = lab.run("q", resume=ResumeState.from_run_state(_dag_state(), 4))

    assert ran == ["ORA over marker sets", "Annotate cell types", "Cell-type composition",
                   "Literature search on retina cell types"]
    assert [r.step for r in result.rounds] == AGENDA


def test_a_resumes_checkpoints_still_hold_the_kept_steps_it_has_not_reached():
    """Live resume of 78a707cd79e9: after the first redone step, run_state.json held only the rounds
    walked so far, so annotation (kept, but later in the agenda) was missing — a second crash there
    would have made the next resume redo it."""
    lab, _ = _lab()
    snapshots: list[LabResult] = []

    lab.run("q", resume=ResumeState.from_interrupted(_dag_state()), checkpoint=snapshots.append)

    first = snapshots[0]                                  # right after ORA, the first redone step
    assert "Annotate cell types" in [r.step for r in first.rounds]
    assert first.accepted_steps == 5, "QC, clustering, markers, annotation, and ORA itself"
    again = ResumeState.from_interrupted(json.loads(json.dumps(first.to_dict())))
    assert again.redo_indices == frozenset({5, 6}), "a second resume redoes only what is still open"


def test_a_crash_mid_run_then_resume_finishes_without_redoing_work():
    """The whole path: the run dies inside a Critic call (as 78a707cd79e9 did, in _heal_vllm_session),
    its last checkpoint is the run_state, and the resume runs only what was left."""
    calls = {"critic": 0}

    def crashing_complete(messages):
        system = messages[0]["content"]
        if "Critic" in system:
            calls["critic"] += 1
            if calls["critic"] == 3:
                raise RuntimeError("SSH session is no longer active.")
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "fine"})
        if "Principal Investigator" in system and "final research report" in system:
            return "The report."
        return json.dumps({"agenda": AGENDA})

    lab, _ = _lab()
    lab._complete_fn = crashing_complete
    snapshots: list[LabResult] = []
    with pytest.raises(RuntimeError):
        lab.run("q", checkpoint=snapshots.append)
    state = json.loads(json.dumps(snapshots[-1].to_dict()))      # as it sits on disk
    done = [r["step"] for r in state["rounds"] if r["verdict"]["verdict"] == "accept"]
    assert done, "the crash came after at least one accepted step"

    lab2, ran = _lab()
    result = lab2.run("q", resume=ResumeState.from_interrupted(state))

    assert not set(ran) & set(done), "a finished step is never run again"
    assert ran == AGENDA[len(done):]
    assert result.accepted_steps == len(AGENDA)


# -- the gateway ---------------------------------------------------------------------------------


@pytest.fixture()
def gw():
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw_app
    yield gw_app
    gw_app.CONNECTIONS.clear()


def _conn(gw_app, tmp_path, **attrs):
    from aiscientist.gateway.settings import HPCSettings

    conn = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    for k, v in attrs.items():
        setattr(conn, k, v)
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


def _seed(conn, state: dict, run_id: str = "78a707cd79e9", local_checkpoints: bool = True) -> Path:
    art = conn.workspace / run_id / "artifacts"
    (art / "process").mkdir(parents=True)
    (art / "process" / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    if local_checkpoints:
        work = conn.workspace / run_id / "work"
        work.mkdir(parents=True)
        (work / "adata_clustered.h5ad").write_bytes(b"x")
    return art


def _capture(gw_app, monkeypatch) -> dict:
    captured: dict = {}

    def fake(c, req, *, resume=None, resume_run_id=None, resume_decisions=None):
        captured.update(resume=resume, run_id=resume_run_id, decisions=resume_decisions)

        async def _noop():
            return None
        return _noop()

    monkeypatch.setattr(gw_app, "_run_lab", fake)
    return captured


def _post(gw_app, body: dict):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app).post("/api/lab/continue", json=body)


def test_continue_resume_interrupted_runs_from_the_next_step(gw, tmp_path, monkeypatch):
    conn = _conn(gw, tmp_path)
    _seed(conn, _dag_state())
    cap = _capture(gw, monkeypatch)

    r = _post(gw, {"connection_id": conn.id, "run_id": "78a707cd79e9", "resume_interrupted": True,
                   "from_step_index": 0})                      # ignored in this mode

    assert r.status_code == 200, r.text
    assert r.json()["from_step"] == 4 and r.json()["steps_to_run"] == [4, 6, 7]
    assert cap["run_id"] == "78a707cd79e9"
    assert cap["resume"].redo_indices == frozenset({3, 5, 6})
    assert cap["decisions"]["dataset_path"] == "/data/sample3"


def test_continue_resume_interrupted_on_a_finished_run_is_refused(gw, tmp_path, monkeypatch):
    conn = _conn(gw, tmp_path)
    state = _dag_state()
    state["agenda"] = ["Run QC", "Cluster the cells", "Marker genes", "Annotate cell types"]
    _seed(conn, state)
    _capture(gw, monkeypatch)

    r = _post(gw, {"connection_id": conn.id, "run_id": "78a707cd79e9", "resume_interrupted": True})

    assert r.status_code == 409 and "nothing left" in r.json()["error"]


def _hpc_conn(gw_app, tmp_path, *, has_checkpoints: bool):
    from aiscientist.gateway.executor import ExecResult

    sent: list[str] = []

    def _exec(command, timeout=60.0):
        sent.append(command)
        return ExecResult(command=command, exit_status=0 if has_checkpoints else 2, stdout="",
                          stderr="", duration_ms=1)

    conn = _conn(gw_app, tmp_path, mock=False,
                 executor=types.SimpleNamespace(username="tester", exec=_exec))
    conn.settings.analysis_on_hpc = True
    return conn, sent


def test_checkpoints_of_a_run_that_analysed_on_hpc3_are_found_there(gw, tmp_path, monkeypatch):
    conn, sent = _hpc_conn(gw, tmp_path, has_checkpoints=True)
    _seed(conn, _dag_state(), local_checkpoints=False)         # nothing in the local work/
    _capture(gw, monkeypatch)

    r = _post(gw, {"connection_id": conn.id, "run_id": "78a707cd79e9", "resume_interrupted": True})

    assert r.status_code == 200, r.text
    assert len(sent) == 1 and "/Temp/tester/analysis/78a707cd79e9/work/adata_*.h5ad" in sent[0]


def test_a_run_whose_hpc3_checkpoints_were_swept_is_still_refused(gw, tmp_path, monkeypatch):
    conn, _ = _hpc_conn(gw, tmp_path, has_checkpoints=False)
    _seed(conn, _dag_state(), local_checkpoints=False)
    _capture(gw, monkeypatch)

    r = _post(gw, {"connection_id": conn.id, "run_id": "78a707cd79e9", "resume_interrupted": True})

    assert r.status_code == 409 and "expired" in r.json()["error"]


def test_the_chat_follow_up_path_still_looks_only_locally(gw, tmp_path):
    """Re-running a step in place on HPC3 checkpoints is new and unverified live, so the automatic
    chat follow-up keeps its old behaviour: no local checkpoints → start fresh."""
    conn, sent = _hpc_conn(gw, tmp_path, has_checkpoints=True)
    art = _seed(conn, _dag_state(), local_checkpoints=False)

    with pytest.raises(ValueError, match="expired"):
        gw._prepare_continue(conn.id, conn, "78a707cd79e9", art, _dag_state(), 2)
    assert sent == []


def test_staging_decisions_ride_in_run_state_and_come_back_on_resume(gw, tmp_path):
    """A resume skips run-start staging, so whatever staging decided must come from run_state.json:
    without the profile the report loses its pre-QC counts, without the bound folder run_cellqc
    and run_code lose the libraries next to the primary file."""
    decisions = {
        "dataset_path": "/data/sample3/filtered_feature_bc_matrix.h5",
        "dataset_result": {"dataset_kind": "single_cell_other", "n_obs": 12000},
        "dataset_root": "/data/sample3",
        "hpc_dataset_root": "/dfs3b/u/server-data/sample3",
        "cellranger_layout": {"n_libraries": 1, "libraries": [{"sample": "Sample3"}]},
    }
    art = tmp_path / "tester" / "78a707cd79e9" / "artifacts"
    snapshot = LabResult("q", list(AGENDA), [], False, 0, "")
    gw._write_run_state(art, snapshot, "SKILL", decisions)
    state = json.loads((art / "process" / "run_state.json").read_text())

    conn = _conn(gw, tmp_path)
    _, restored, _, _ = gw._prepare_continue(conn.id, conn, "78a707cd79e9", art, state, 0)

    for key in ("dataset_result", "dataset_root", "hpc_dataset_root", "cellranger_layout"):
        assert restored[key] == decisions[key], key
