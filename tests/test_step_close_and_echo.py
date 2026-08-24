"""The two framework defects run 97dfc89dc5aa exposed: a question-echo pseudo-step in the plan,
and steps that end their turn budget with an EMPTY final answer."""
from __future__ import annotations

from bioagent.agents.research_harness import ResearchHarness
from bioagent.agents.research_lab import _drop_question_echo

Q = "What changes between DDX41 mutant and WT retina?"


def test_question_echo_is_dropped():
    steps = [Q,
             "State the unit of replication: sampleid is the condition ...",
             "Assess QC and depth imbalance: compare distributions of nCount_RNA ..."]
    kept, dropped = _drop_question_echo(steps, Q)
    assert dropped == [Q]
    assert len(kept) == 2


def test_bold_or_punctuated_echo_is_dropped():
    steps = ["**What changes between DDX41 mutant and WT retina?**",
             "Run `run_de` stratified by majorclass with reference WT."]
    kept, dropped = _drop_question_echo(steps, Q)
    assert len(dropped) == 1 and len(kept) == 1


def test_real_steps_survive():
    steps = ["**QC & filtering** — Run `run_scanpy_qc` and report retained cells.",
             "Depth-matched sensitivity analysis: downsample DDX41 to WT median depth and re-rank."]
    kept, dropped = _drop_question_echo(steps, Q)
    assert kept == steps and dropped == []


def test_all_echo_plan_is_left_alone():
    kept, dropped = _drop_question_echo([Q], Q)
    assert kept == [Q] and dropped == []


def test_digest_answer_replaces_empty_final():
    steps = [
        {"tool": "run_de", "args": {}, "ok": True,
         "result": {"status": "ok", "tables": ["tables/de_majorclass_all.csv"], "n_groups": 5}},
        {"tool": "run_code", "args": {}, "ok": False, "result": {"status": "error", "error": "KeyError"},
         "summary": "error"},
    ]
    text = ResearchHarness._digest_answer(steps, "max_steps")
    assert "auto-summary" in text and "max_steps" in text
    assert "run_de: ok" in text
    assert "run_code: ERROR" in text
    assert "tables/de_majorclass_all.csv" in text
