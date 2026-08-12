"""Offline tests for A2 continuation: resume a prior run and re-execute only the changed step
onward, WITHOUT re-planning — the resumable state machine (ResearchLab.run(resume=...)).

Everything is injected (PI/Critic/Synthesize completions + the Scientist's tool-calling chat), so
the whole loop runs with no GPU/LLM. Mirrors tests/test_research_lab.py's harness.
"""

from __future__ import annotations

import json

from bioagent.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
from bioagent.agents.research_lab import (
    CriticVerdict, LabConfig, LabResult, LabRound, ResearchLab, ResumeState)


def _ctx() -> HarnessContext:
    return HarnessContext(decisions={}, tunnel_port=1, model="m")


def _scientist_calls(tool_name, tool_args=None, on_messages=None):
    """A scripted Scientist: call <tool>, then `finish`. Optionally report each turn's messages."""
    def chat(messages, tools):
        if on_messages is not None:
            on_messages(messages)
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [
                {"id": "f1", "type": "function",
                 "function": {"name": "finish", "arguments": json.dumps({"answer": "done this step"})}}]}
        return {"content": "", "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": tool_name, "arguments": json.dumps(tool_args or {})}}]}
    return chat


def _accepting_complete(agenda, *, pi_counter=None):
    """PI returns the agenda, Critic always accepts, else synthesize. ``pi_counter`` (a dict) counts
    PI-plan calls so a test can assert a resume did NOT re-plan."""
    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:   # PLAN phase (not synthesize)
            if pi_counter is not None:
                pi_counter["n"] = pi_counter.get("n", 0) + 1
            return json.dumps(agenda)
        if "Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "grounded"})
        return "FINAL REPORT: synthesized from accepted steps."
    return complete


def _lab(agenda, tool="run_qc", *, pi_counter=None, on_messages=None):
    scientist = ResearchHarness(catalog=default_catalog(),
                                chat_fn=_scientist_calls(tool, on_messages=on_messages))
    return ResearchLab(_ctx(), LabConfig(), complete_fn=_accepting_complete(agenda, pi_counter=pi_counter),
                       scientist=scientist)


# --- serialization round-trip ------------------------------------------------

def test_labresult_round_trips_through_dict():
    res = _lab(["Run QC", "Cluster the cells"]).run("characterize")
    back = LabResult.from_dict(json.loads(json.dumps(res.to_dict())))   # JSON-safe round trip
    assert back.question == res.question
    assert back.agenda == res.agenda
    assert back.accepted_steps == res.accepted_steps and back.converged == res.converged
    assert [r.step for r in back.rounds] == [r.step for r in res.rounds]
    assert back.rounds[0].verdict.verdict == res.rounds[0].verdict.verdict
    assert isinstance(back.rounds[0].verdict, CriticVerdict)


def test_resume_state_keeps_all_accepted_rounds():
    def _round(i, step, verdict="accept"):
        return {"round_no": i, "step_index": i, "step": step, "specialist": "S",
                "scientist_result": {}, "verdict": {"verdict": verdict, "score": 1.0, "critique": ""}}
    state = {"agenda": ["a", "b", "c"],
             "rounds": [_round(1, "a"), _round(2, "b", "revise"), _round(3, "c")]}
    rs = ResumeState.from_run_state(state, from_step_index=1)
    # keep ALL accepted rounds ('b' was a revise → dropped); the run loop decides reuse vs re-run.
    assert [r.step for r in rs.prior_rounds] == ["a", "c"]
    assert rs.from_step_index == 1 and rs.agenda == ["a", "b", "c"] and rs.redo_indices is None


# --- the resume path ---------------------------------------------------------

def test_resume_reruns_from_boundary_without_replanning():
    agenda = ["Run QC", "Cluster the cells", "Differential expression"]
    first = _lab(agenda).run("characterize")
    assert first.accepted_steps == 3 and first.converged

    # Resume: keep step 0 (QC), re-run steps 1..2. A PI-plan call here would be a bug.
    pi = {"n": 0}
    lab2 = _lab(agenda, tool="run_qc", pi_counter=pi)
    resume = ResumeState.from_run_state(first.to_dict(), from_step_index=1,
                                        modify_note="re-cluster at resolution 1.0")
    events: list = []
    result = lab2.run("characterize", on_event=events.append, resume=resume)

    assert pi["n"] == 0                                         # NO re-planning
    assert any(e["type"] == "run_resumed" for e in events)
    assert result.agenda == agenda
    assert len(result.rounds) == 3                             # 1 kept + 2 re-run
    assert result.rounds[0].step == "Run QC"                  # kept verbatim from the prior run
    assert result.rounds[1].step == "Cluster the cells"
    assert result.accepted_steps == 3 and result.converged is True
    assert "FINAL REPORT" in result.final_answer


def test_resume_from_first_step_reruns_everything():
    agenda = ["Run QC", "Cluster the cells"]
    first = _lab(agenda).run("characterize")
    resume = ResumeState.from_run_state(first.to_dict(), from_step_index=0)
    # From step 0, both steps are in the re-run set (nothing upstream to reuse), so all re-run.
    result = _lab(agenda).run("characterize", resume=resume)
    assert len(result.rounds) == 2 and result.accepted_steps == 2 and result.converged


def test_resume_modify_note_steers_the_resumed_step():
    agenda = ["Run QC", "Cluster the cells"]
    first = _lab(agenda).run("characterize")

    seen: list[str] = []
    def capture(messages):
        seen.extend(m["content"] for m in messages if isinstance(m.get("content"), str))
    lab2 = _lab(agenda, tool="run_qc", on_messages=capture)
    resume = ResumeState.from_run_state(first.to_dict(), from_step_index=1,
                                        modify_note="RECLUSTER_AT_RES_1P0")
    lab2.run("characterize", resume=resume)

    assert any("RECLUSTER_AT_RES_1P0" in s for s in seen)     # the note reached the Scientist's brief


# --- gateway persistence ↔ resume-state reconstruction -----------------------

def test_run_state_persist_round_trips_into_resume_state(tmp_path):
    import pytest
    pytest.importorskip("fastapi")
    from bioagent.gateway import app as gw_app

    rounds = [LabRound(1, 1, "Run QC", "S", {"final_answer": "x"}, CriticVerdict("accept", 0.9, "ok")),
              LabRound(2, 2, "Cluster", "S", {"final_answer": "y"}, CriticVerdict("accept", 0.9, "ok"))]
    res = LabResult("characterize", ["Run QC", "Cluster"], rounds, True, 2, "final")
    gw_app._write_run_state(tmp_path, res, "GUIDANCE_X", {"dataset_path": "/data/a.h5ad"})

    state = json.loads((tmp_path / "process" / "run_state.json").read_text())
    assert state["guidance"] == "GUIDANCE_X" and state["dataset_path"] == "/data/a.h5ad"

    rs = ResumeState.from_run_state(state, from_step_index=1)   # redo "Cluster", keep "Run QC"
    assert rs.agenda == ["Run QC", "Cluster"]
    assert [r.step for r in rs.prior_rounds] == ["Run QC", "Cluster"]   # all accepted kept; loop reuses "Run QC"
    assert rs.guidance == "GUIDANCE_X"


# --- dependency evaluation: which downstream steps actually need re-running ---

def _lab_with_complete(complete):
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc"))
    return ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)


def test_evaluate_redo_keeps_independent_literature():
    # Model flags nothing; the deterministic guard still re-runs the analysis step (DE) and keeps
    # only the checkpoint-free literature step.
    lab = _lab_with_complete(lambda m: "[]")
    agenda = ["Run QC", "Cluster the cells", "Differential expression", "Literature review of the markers"]
    redo = lab._evaluate_redo_indices(agenda, 1, "re-cluster", [], lambda e: None)
    assert redo == {1, 2}                       # Cluster + DE re-run; literature (index 3) kept


def test_evaluate_redo_reruns_flagged_literature():
    # Model says the literature step DOES depend on the change (step 4) → it is re-run too.
    lab = _lab_with_complete(lambda m: "[4]")
    agenda = ["Run QC", "Cluster the cells", "Differential expression", "Literature on the found markers"]
    redo = lab._evaluate_redo_indices(agenda, 1, "x", [], lambda e: None)
    assert redo == {1, 2, 3}


def test_evaluate_redo_fallback_when_unparseable():
    lab = _lab_with_complete(lambda m: "sorry, not json")
    redo = lab._evaluate_redo_indices(["Run QC", "Cluster", "DE"], 1, "", [], lambda e: None)
    assert redo == {1, 2}                        # conservative: re-run everything downstream


def test_resume_keeps_independent_literature_end_to_end():
    agenda = ["Run QC", "Cluster the cells", "Differential expression", "Literature search on the topic"]
    def _rd(i, step):
        return {"round_no": i, "step_index": i, "step": step, "specialist": "S",
                "scientist_result": {"final_answer": f"did {step}"},
                "verdict": {"verdict": "accept", "score": 0.9, "critique": "ok"}}
    state = {"question": "q", "agenda": agenda,
             "rounds": [_rd(1, agenda[0]), _rd(2, agenda[1]), _rd(3, agenda[2]), _rd(4, agenda[3])],
             "converged": True, "accepted_steps": 4, "final_answer": "prior"}
    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator deciding" in sys:
            return "[]"                                    # evaluator flags no downstream dep
        if "Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        return "FINAL REPORT."
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc"))
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)

    resume = ResumeState.from_run_state(state, from_step_index=1)   # change "Cluster the cells"
    events: list = []
    result = lab.run("q", on_event=events.append, resume=resume)

    ev = next(e for e in events if e["type"] == "run_resumed")
    assert 4 in ev["kept"]                                  # literature step kept
    assert 2 in ev["redo"] and 3 in ev["redo"]             # Cluster + DE re-run
    # the literature round is preserved verbatim (its prior answer, not a re-run)
    lit = next(r for r in result.rounds if r.step == agenda[3])
    assert lit.scientist_result.get("final_answer") == "did Literature search on the topic"
