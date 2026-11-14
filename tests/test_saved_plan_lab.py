"""A saved plan re-enters review or execution without repeating the planning phase."""

import json
from dataclasses import replace

import pytest

from aiscientist.agents.plan_doc import PlanDoc, PlanStep, parse_plan_yaml
from aiscientist.agents.preset_pipelines import PresetPipeline
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness
from aiscientist.agents.research_lab import LabConfig, LabResult, ResearchLab, Specialist
from aiscientist.agents.scope import RunScope


def _doc(limit=25):
    return PlanDoc(request="QC cells", steps=(PlanStep(
        id="qc", text="Filter cells with `mcp_run_scanpy_qc`.", tool="mcp_run_scanpy_qc",
        settings={"max_pct_mt": limit, "min_genes": 200}, runs_on="hpc:analysis"),))


def _lab(calls, *, complete=None, planner="linear"):
    def run(args, ctx):
        calls.append(dict(args))
        return {"status": "ok"}

    def chat(messages, tools):
        done = any(m.get("role") == "tool" for m in messages)
        return {"content": "", "tool_calls": [{"id": "t", "type": "function", "function": {
            "name": "finish" if done else "mcp_run_scanpy_qc",
            "arguments": json.dumps({"answer": "done"} if done else {"max_pct_mt": 5})}}]}

    tool = HarnessTool("mcp_run_scanpy_qc", "Filter cells", {"type": "object", "properties": {
        "max_pct_mt": {"type": "number"}, "min_genes": {"type": "integer"}}}, run)
    def default_complete(messages):
        system = messages[0]["content"]
        assert "Principal Investigator of a bioinformatics lab" not in system
        if "Critic" in system:
            return '{"verdict":"accept","score":0.9,"critique":"ok"}'
        return "Final report."

    return ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"),
                       LabConfig(plan_yaml=True, mode="single", planner=planner,
                                 auto_select_skill=False, plan_review=False),
                       complete_fn=complete or default_complete,
                       scientist=ResearchHarness(catalog=[tool], chat_fn=chat))


def test_saved_approved_file_executes_pinned_settings_without_planning(tmp_path, monkeypatch):
    path = tmp_path / "v01.yaml"
    path.write_text(_doc(10).to_yaml(), encoding="utf-8")
    path.write_text(_doc(25).to_yaml(), encoding="utf-8")
    calls = []
    lab = _lab(calls)
    lab.config = replace(lab.config, plan_yaml=False)
    monkeypatch.setattr(lab, "_pi_plan", lambda *a, **kw: pytest.fail("planned again"))
    monkeypatch.setattr(lab, "_plan_review", lambda *a, **kw: pytest.fail("reviewed again"))
    doc = parse_plan_yaml(path.read_text(encoding="utf-8"))
    result = lab.run(doc.request, approved_plan=doc)
    assert result.converged
    assert calls == [{"max_pct_mt": 25, "min_genes": 200}]
    assert lab.approved_plan_yaml == doc.to_yaml()


def test_approved_entry_keeps_blocking_in_run_decisions(monkeypatch):
    calls, choices = [], []
    lab = _lab(calls)
    monkeypatch.setattr(lab, "_label_decision", lambda agenda: ("Which labels?", ("provided", "new")))
    def decide(node):
        choices.append(node.id)
        return {"action": "proceed", "choice": "provided"}
    lab.run(_doc().request, approved_plan=_doc(), decision_review=decide,
            plan_review=lambda *args: pytest.fail("reviewed approved plan"))
    assert choices == ["plan_decision"]
    assert calls == [{"max_pct_mt": 25, "min_genes": 200}]


@pytest.mark.parametrize("planner", ["linear", "dag", "langgraph"])
def test_saved_approved_plan_keeps_planner_team_and_post_approval_guards(planner, monkeypatch):
    lab = _lab([], planner=planner)
    team = (Specialist("Saved expert", "Saved persona", ("qc",)),)
    lab._team, lab._mode = team, "team"
    lab._guidance, lab._self_sourced = "Saved guidance", "PI notes"
    lab._later = ["enrichment"]
    lab._scope = RunScope(keep=("qc",))
    lab._skills = [PresetPipeline("qc", "QC", "Saved protocol", ("mcp_run_scanpy_qc",))]
    context = json.loads(json.dumps(lab.plan_context))
    restored = _lab([], planner=planner)
    doc = _doc()
    doc = PlanDoc(request=doc.request, steps=doc.steps + (PlanStep(
        "busy", "Export all computed tables to the artifacts directory for report assembly."),))
    captured = {}

    def execute(question, agenda, *args, **kwargs):
        captured.update(kwargs)
        captured["agenda"] = agenda
        return LabResult(question, _doc().agenda(), [], True, 1, "Done")

    monkeypatch.setattr(restored, "_pi_plan", lambda *a, **kw: pytest.fail("planned again"))
    monkeypatch.setattr(restored, "_structure_agenda_dag", lambda q, agenda, emit: agenda)
    monkeypatch.setattr(restored, {"linear": "_run_loop", "dag": "_run_dag",
                                  "langgraph": "_run_langgraph"}[planner], execute)
    restored.run(doc.request, approved_plan=doc, plan_context=context)
    assert captured["agenda"] == _doc().agenda()
    assert captured["exec_roster"] == team and captured["exec_multi"] is True
    assert json.loads(json.dumps(restored.plan_context)) == context
    assert restored._pinned_settings(_doc().agenda()[0])["mcp_run_scanpy_qc"]["max_pct_mt"] == 25


class WaitingPlan(Exception):
    pass


@pytest.mark.parametrize("action", ["edit", "revise"])
def test_saved_review_uses_loaded_document_and_yields_after_next_card(tmp_path, action, monkeypatch):
    path = tmp_path / "current.yaml"
    path.write_text(_doc(10).to_yaml(), encoding="utf-8")
    path.write_text(_doc(25).to_yaml(), encoding="utf-8")
    seen = []

    def complete(messages):
        seen.append(messages)
        return '{"ops":[{"op":"change","step":"qc","settings":{"min_genes":300}}]}'

    lab = _lab([], complete=complete)
    monkeypatch.setattr(lab, "_pi_plan", lambda *a, **kw: pytest.fail("planned again"))
    doc = parse_plan_yaml(path.read_text(encoding="utf-8"))
    decision = {"action": action, "feedback": "Change min_genes to 300"}
    if action == "edit":
        decision["plan_yaml"] = doc.to_yaml().replace("min_genes: 200", "min_genes: 300")
    cards = []
    def review(kind, payload):
        cards.append(lab._plan_doc)
        raise WaitingPlan()

    with pytest.raises(WaitingPlan):
        lab.run(doc.request, saved_plan=doc, saved_plan_decision=decision, plan_review=review)
    assert len(cards) == 1
    assert cards[0].steps[0].settings == {"max_pct_mt": 25, "min_genes": 300}
    assert bool(seen) is (action == "revise")


def test_saved_question_reads_document_settings_and_preserves_plan():
    prompts = []
    lab = _lab([], complete=lambda m: prompts.append(m) or "25% is the cutoff.")
    doc = _doc(25)
    def wait(kind, payload):
        assert lab._plan_doc == doc
        raise WaitingPlan()
    with pytest.raises(WaitingPlan):
        lab.run(doc.request, saved_plan=doc, plan_review=wait,
                saved_plan_decision={"action": "revise", "feedback": "Why this mitochondrial cutoff?"})
    assert "max_pct_mt: 25" in prompts[0][1]["content"]


def test_saved_clarification_reuses_feedback_without_initial_planner(monkeypatch):
    lab = _lab([])
    calls = []
    def plan(question, emit, **kwargs):
        calls.append(kwargs)
        lab._plan_doc = _doc()
        return "agenda", _doc().agenda()
    monkeypatch.setattr(lab, "_pi_plan", plan)
    def wait(kind, payload):
        assert kind == "agenda"
        raise WaitingPlan()
    with pytest.raises(WaitingPlan):
        lab.run("QC cells", saved_plan_kind="clarify", saved_plan_payload=[{"question": "Which cutoff?"}],
                plan_context={"feedback": ["Use the supplied cells"]}, plan_review=wait,
                saved_plan_decision={"action": "revise", "feedback": "Use 25 percent"})
    assert len(calls) == 1
    assert calls[0]["feedback"] == "Use the supplied cells\nUse 25 percent"


def test_restored_skills_keep_their_declared_outputs():
    from aiscientist.agents.skill_outputs import DeclaredOutput

    lab = _lab([])
    declared = DeclaredOutput(path="tables/qc_summary.csv", kind="table",
                              description="Cells kept per sample", tool="mcp_run_scanpy_qc")
    lab._skills = [PresetPipeline("qc", "QC", "Saved protocol", ("mcp_run_scanpy_qc",),
                                  outputs=(declared,))]
    context = json.loads(json.dumps(lab.plan_context))
    # A field a later deploy no longer has must not stop the plan from being restored.
    context["skills"][0]["retired_field"] = "x"
    restored = _lab([])
    restored._restore_plan_context(context)
    assert restored._skills[0].outputs == (declared,)
    restored._plan_doc = _doc()
    expected, sources = restored._expected_outputs(_doc().agenda()[0])
    assert expected == [declared] and sources == ["pipeline qc"]
