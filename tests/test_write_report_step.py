"""The written report is a plan step of its own, there only when asked for (RCHENLAB/AiScientist#102).

Jin, 2026-11-05: report generation should not be a platform default; a skill that needs a report
includes the step. So a plan gets one ``write_report`` step, last, when the request asks for a written
report or a skill a step uses declares ``writes-report``; the Scientist never runs it, and the
gateway writes the report after the analysis. Without the step a run answers in the chat. Scripted
models throughout.
"""

from __future__ import annotations

import json

import pytest

from aiscientist.agents import research_lab as rl
from aiscientist.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
from aiscientist.agents.research_lab import (
    REPORT_STEP, LabConfig, LabResult, ResearchLab, ResumeState, _is_report_step)
from aiscientist.agents.scope import rules_out_report, wants_written_report
from aiscientist.agents.skills import Skill

QC = "**QC** — Run `run_qc` on the cells."
DRAFTED = "**Report** — Write the methods and results report with `write_report`."


def _ctx() -> HarnessContext:
    return HarnessContext(decisions={}, tunnel_port=1, model="m")


def _lab(**kw) -> ResearchLab:
    return ResearchLab(_ctx(), LabConfig(**kw))


def _events():
    seen: list[dict] = []
    return seen, seen.append


def test_the_report_step_is_recognised_by_its_tool():
    assert _is_report_step(REPORT_STEP)
    assert _is_report_step(DRAFTED)
    assert not _is_report_step(QC)
    assert not _is_report_step("**Write-up** — Summarise the QC table in the chat.")


@pytest.mark.parametrize("note,out", [("drop the report step", True), ("no report please", True),
                                      ("不要报告", True), ("删掉报告这一步", True),
                                      ("add a written report", False), ("use resolution 0.5", False)])
def test_a_note_that_takes_the_report_out(note, out):
    assert rules_out_report(note) is out


def test_no_report_asked_for_drops_the_models_own_report_step():
    seen, emit = _events()
    plan = _lab()._ensure_report_step([QC, DRAFTED], "do the cell qc quality controll", emit=emit)
    assert plan == [QC]
    assert {"type": "steps_pruned", "reason": "report_not_asked", "dropped": [DRAFTED]} in seen


def test_a_report_asked_for_puts_one_report_step_last():
    seen, emit = _events()
    lab = _lab()
    assert lab._ensure_report_step([QC], "Run QC and write a report", emit=emit) == [QC, REPORT_STEP]
    assert seen[-1]["type"] == "report_step_added"
    # the model's own wording is kept, moved last, never doubled
    assert lab._ensure_report_step([DRAFTED, QC], "QC with a PDF report", emit=emit) == [QC, DRAFTED]


def test_a_note_on_the_plan_adds_removes_or_keeps_the_report_step():
    _seen, emit = _events()
    lab = _lab()
    asked = lab._ensure_report_step([QC], "Run QC", feedback="add a written report",
                                    note="add a written report", prior_agenda=[QC], emit=emit)
    assert asked == [QC, REPORT_STEP]
    dropped = lab._ensure_report_step([QC, REPORT_STEP], "Run QC and write a report",
                                      feedback="drop the report step", note="drop the report step",
                                      prior_agenda=[QC, REPORT_STEP], emit=emit)
    assert dropped == [QC]
    # A note about something else: the plan keeps (or keeps out) its report step as it was.
    kept = lab._ensure_report_step([QC], "Run QC and write a report", feedback="use 500 counts",
                                   note="use 500 counts", prior_agenda=[QC, REPORT_STEP], emit=emit)
    assert kept == [QC, REPORT_STEP]
    left_out = lab._ensure_report_step([QC, DRAFTED], "Run QC", feedback="use 500 counts",
                                       note="use 500 counts", prior_agenda=[QC], emit=emit)
    assert left_out == [QC]


def test_a_skill_that_declares_writes_report_gets_the_step(monkeypatch):
    _seen, emit = _events()
    writer = Skill(name="query-papers", metadata={"writes-report": "true"})
    quiet = Skill(name="qc-skill", metadata={})
    monkeypatch.setattr(ResearchLab, "_step_skills",
                        lambda self, step: [writer] if "papers" in step else [quiet])
    lab = _lab()
    papers = "**Papers** — Apply skill `query-papers` with `run_code` to the corpus."
    assert lab._ensure_report_step([papers], "find the genes", emit=emit) == [papers, REPORT_STEP]
    assert lab._ensure_report_step([QC], "find the genes", emit=emit) == [QC]


def test_wants_written_report_is_the_request_rule():
    assert wants_written_report("Run QC and write a report")
    assert not wants_written_report("Run QC, no report")
    assert not wants_written_report("do the cell qc quality controll")


# --- the run: the step is handed over, not executed --------------------------------------------------

def _scientist(briefs):
    def chat(messages, _tools):
        briefs.append(next(m["content"] for m in messages if m.get("role") == "user"))
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"id": "f1", "type": "function", "function": {
                "name": "finish", "arguments": json.dumps({"answer": "QC done"})}}]}
        return {"content": "", "tool_calls": [{"id": "t1", "type": "function", "function": {
            "name": "run_qc", "arguments": "{}"}}]}
    return chat


def _router(agenda):
    def complete(messages, **_kw):
        sys = messages[0]["content"]
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "grounded"})
        if "reviewing a step that JUST completed" in sys:
            return json.dumps({"contribution": "new", "prune": []})
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(agenda)
        return "FINAL: synthesized."
    return complete


def _run(agenda, question):
    briefs: list[str] = []
    events: list[dict] = []
    lab = ResearchLab(_ctx(), LabConfig(planner="linear"), complete_fn=_router(agenda),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist(briefs)))
    result = lab.run(question, on_event=events.append)
    return lab, result, briefs, events


def test_the_report_step_is_handed_to_the_gateway_not_run():
    lab, result, briefs, events = _run([QC, REPORT_STEP], "Run QC and write a report")
    assert lab.report_requested and result.report_requested
    assert briefs and all("Written report" not in b for b in briefs), \
        "the Scientist must not be handed the report step"
    assert REPORT_STEP not in result.agenda
    assert any(e.get("type") == "report_step" for e in events)
    assert result.accepted_steps == 1
    assert LabResult.from_dict(result.to_dict()).report_requested


def test_without_the_step_nothing_asks_for_a_report():
    lab, result, _briefs, _events = _run([QC], "do the cell qc quality controll")
    assert not lab.report_requested and not result.report_requested


def test_a_resumed_run_keeps_the_report_it_was_asked_for():
    state = {"agenda": [QC], "rounds": [], "report_requested": True}
    assert ResumeState.from_run_state(state, 0).report_requested
    assert not ResumeState.from_run_state({"agenda": [QC], "rounds": []}, 0).report_requested


def test_the_planner_sees_write_report_with_its_rule():
    assert "write_report" in rl._REPORT_TOOL_LINE and "only when the request asks" in rl._REPORT_TOOL_LINE
    assert "every run ends with a report" not in rl._PI_SYSTEM
    assert "`write_report` step" in rl._PI_SYSTEM


def test_a_limited_run_keeps_its_report_step():
    """"Only QC, and write a report": the limit is about the analysis, not the report step."""
    from aiscientist.agents.scope import RunScope
    lab = _lab()
    lab._scope = RunScope(keep=("run_cellqc",))
    cellqc = "**QC** — Quality-control the delivery with `run_cellqc`."
    cluster = "**Clustering** — Cluster the cells with `run_clustering`."
    _seen, emit = _events()
    assert lab._apply_scope([cellqc, cluster, REPORT_STEP], emit) == [cellqc, REPORT_STEP]


def test_the_plan_review_neither_drops_nor_adds_the_report_step():
    keep = ResearchLab._keep_report_step
    assert keep([QC, REPORT_STEP], [QC]) == [QC, REPORT_STEP]          # a reviewer that dropped it
    assert keep([QC], [QC, DRAFTED]) == [QC]                          # or added one
    assert keep([QC, REPORT_STEP], [REPORT_STEP, QC]) == [QC, REPORT_STEP]
