"""Saved plans cannot silently switch implementations or lose approved arguments."""
from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from aiscientist.agents import mcp_bridge
from aiscientist.agents.mcp_plan_migration import PlanMigrationRequired, assert_plan_available
from aiscientist.agents.plan_doc import PlanDoc, PlanError, PlanStep, parse_plan_yaml
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness
from aiscientist.agents.research_lab import LabConfig, LabResult, ResearchLab, ResumeState


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    from aiscientist.agents.research_lab import _known_tool_names
    _known_tool_names.cache_clear()
    servers = [SimpleNamespace(name=domain, adapter="analysis", skill="test", tools=[{"name": wire}],
                               planner_name=lambda name: "mcp_" + name)
               for domain, wire in [("variant-annotation", "annotate_variants"),
                                    ("scgpt-annotation", "scgpt_annotate"),
                                    ("data-utilities", "make_schematic")]]
    monkeypatch.setattr(mcp_bridge, "configured_servers", lambda: tuple(servers))
    yield servers
    _known_tool_names.cache_clear()


def _doc(name="annotate_variants", settings=None):
    return PlanDoc("Original research request", (PlanStep(
        "s1", f"Perform `{name}` with the approved parameters.", tool=name,
        settings=settings or {"assembly": "GRCh38", "limit": 7}),))


def _lab(names):
    tools = [HarnessTool(name, "scientific tool", {"type": "object"},
                         lambda *_: pytest.fail("must not execute a stale plan")) for name in names]
    return ResearchLab(HarnessContext(),
                       LabConfig(auto_select_skill=False, mode="single", plan_yaml=True,
                                 plan_review=False),
                       scientist=ResearchHarness(catalog=tools),
                       complete_fn=lambda *_: pytest.fail("must not reinterpret the saved plan"))


@pytest.mark.parametrize("old,new", [("annotate_variants", "mcp_annotate_variants"),
                                     ("scgpt_annotate", "mcp_scgpt_annotate")])
def test_approved_legacy_plan_requires_review_without_mutating_settings(old, new):
    doc = _doc(old)
    original = doc.to_yaml()
    lab = _lab([new])
    with pytest.raises(PlanMigrationRequired, match=new):
        lab.run(doc.request, approved_plan=doc)
    assert doc.to_yaml() == original
    assert lab._approved_plan is None


def test_saved_plan_cannot_pass_approval_then_fuzzy_rename_a_removed_tool(monkeypatch):
    doc = _doc()
    lab = _lab(["mcp_annotate_variants"])
    monkeypatch.setattr(lab, "_plan_review", lambda _q, agenda, _emit: agenda)
    monkeypatch.setattr(lab, "_present_plan_document", lambda *_: None)
    monkeypatch.setattr(lab, "_check_plan_tooling", lambda *_: pytest.fail("no fuzzy migration"))
    with pytest.raises(PlanMigrationRequired, match="re-planning and review"):
        lab.run(doc.request, saved_plan=doc, plan_review=lambda *_: {"action": "approve"})
    assert doc.steps[0].settings == {"assembly": "GRCh38", "limit": 7}


def test_unmigrated_deployment_keeps_available_legacy_tool():
    doc = _doc()
    assert_plan_available(doc.agenda(), _lab(["annotate_variants"]).scientist.catalog, plan=doc)


def test_replacement_requires_reviewed_domain_and_actual_public_name(registry):
    registry[0].name = "foreign"
    with pytest.raises(PlanMigrationRequired, match="No reviewed replacement"):
        assert_plan_available(_doc().agenda(), _lab(["mcp_annotate_variants"]).scientist.catalog)
    registry[0].name = "variant-annotation"
    registry[0].planner_name = lambda _wire: "mcp_reviewed_variant"
    with pytest.raises(PlanMigrationRequired, match="replacement: mcp_reviewed_variant"):
        assert_plan_available(_doc().agenda(), _lab(["mcp_reviewed_variant"]).scientist.catalog)


def test_no_prefix_stripping_or_tool_name_substring_inference():
    assert_plan_available(["Read my_annotate_variants_notes, then compare results."], [])
    with pytest.raises(PlanMigrationRequired, match="No reviewed replacement"):
        assert_plan_available(["Run `mcp_unregistered_annotate_variants`."], [])


def _accepted(step):
    return {"round_no": 1, "step_index": 1, "step": step, "specialist": "S",
            "scientist_result": {"final_answer": "historical result"},
            "verdict": {"verdict": "accept", "score": 1, "critique": "ok"}}


def test_interrupted_pending_old_tool_stops_before_dispatch_and_preserves_evidence():
    doc = _doc("scgpt_annotate")
    state = {"question": doc.request, "agenda": ["Completed work", *doc.agenda()],
             "rounds": [_accepted("Completed work")]}
    before = copy.deepcopy(state)
    resume = ResumeState.from_interrupted(state)
    with pytest.raises(PlanMigrationRequired, match="scgpt_annotate"):
        _lab(["mcp_scgpt_annotate"]).run(doc.request, resume=resume)
    assert state == before and resume.prior_rounds[0].scientist_result["final_answer"] == "historical result"


def test_completed_old_step_can_be_reused_but_not_rerun(monkeypatch):
    old = _doc().steps[0]
    current = replace(_doc("mcp_make_schematic", {"source": "digraph G { A -> B; }"}).steps[0], id="s2")
    doc = PlanDoc("q", (old, current))
    state = {"question": "q", "agenda": doc.agenda(), "rounds": [_accepted(old.text)],
             "approved_plan_yaml": doc.to_yaml(),
             "plan_context": {"scope": {"source": "explicit_mcp", "keep": [current.tool]}}}
    lab = _lab([current.tool])
    captured = {}
    def execute(question, agenda, *_args, **kwargs):
        captured.update(kwargs)
        assert lab._pinned_settings(current.text) == {current.tool: current.settings}
        assert lab._scope.source == "explicit_mcp"
        assert lab.approved_plan_yaml == doc.to_yaml()
        return LabResult(question, agenda, list(kwargs["kept_by_index"].values()), False, 1, "kept")
    monkeypatch.setattr(lab, "_run_loop", execute)
    lab.run("q", resume=ResumeState.from_interrupted(state))
    assert set(captured["kept_by_index"]) == {0} and captured["redo_indices"] == frozenset({1})
    with pytest.raises(PlanMigrationRequired, match="annotate_variants"):
        lab.run("q", resume=ResumeState.from_run_state(state, 0, redo_indices={0, 1}))


def test_typed_resume_refuses_prose_edit_instead_of_dropping_pins():
    doc = _doc("mcp_make_schematic", {"source": "digraph G { A -> B; }"})
    state = {"question": "q", "agenda": [doc.steps[0].text + " New text."],
             "approved_plan_yaml": doc.to_yaml(), "rounds": []}
    resume = ResumeState.from_interrupted(state)
    with pytest.raises(PlanMigrationRequired, match="normal plan review"):
        _lab(["mcp_make_schematic"]).run("q", resume=resume)
    assert resume.approved_plan.steps[0].settings == doc.steps[0].settings


def test_typed_resume_refuses_new_instructions_without_review():
    doc = _doc("mcp_make_schematic")
    state = {"question": "q", "agenda": doc.agenda(), "approved_plan_yaml": doc.to_yaml(), "rounds": []}
    resume = ResumeState.from_run_state(state, 0, redo_indices={0}, modify_note="Use new parameters")
    with pytest.raises(PlanMigrationRequired, match="normal plan review"):
        _lab(["mcp_make_schematic"]).run("q", resume=resume)


def test_gateway_restores_full_document_and_exact_scope_from_old_checkpoint(tmp_path):
    from aiscientist.gateway import app as gw
    source = "digraph G {\n" + " A -> B;\n" * 1000 + "}\n"
    doc = _doc("mcp_make_schematic", {"source": source, "nested": [[1, 2], [3, 4]]})
    state = {"question": "q", "agenda": doc.agenda(), "rounds": [],
             "plan_context": {"scope": {"source": "explicit_mcp", "keep": ["mcp_make_schematic"]}}}
    (tmp_path / "report").mkdir()
    path = tmp_path / "report/plan.yaml"
    original = doc.to_yaml()
    path.write_text(original)
    assert len(original) > 6000
    resume, _, _, _ = gw._prepare_continue("c", object(), "r", tmp_path, state, 0, interrupted=True)
    assert resume.approved_plan.steps[0].settings == doc.steps[0].settings
    assert resume.plan_context == state["plan_context"]
    assert path.read_text() == original and "approved_plan_yaml" not in state


def test_resume_serialization_context_is_not_shared_with_checkpoint():
    state = {"agenda": ["A"], "rounds": [], "plan_context": {"scope": {"keep": ["one"]}}}
    resume = ResumeState.from_interrupted(state)
    resume.plan_context["scope"]["keep"].append("two")
    assert state["plan_context"]["scope"]["keep"] == ["one"]


def test_explicit_reviewed_replacement_changes_tool_and_pins_without_old_arguments(registry, monkeypatch):
    registry.append(SimpleNamespace(name="cellqc", adapter="analysis", skill="test",
                                    tools=[{"name": "cellqc_job_status"}],
                                    planner_name=lambda name: "mcp_" + name))
    original = _doc("run_cellqc", {"min_genes": 200})
    replacement = _doc("mcp_cellqc_job_status", {"analysis_id": "prior-run", "task_id": "known-task"})
    lab = _lab(["mcp_cellqc_job_status"])
    lab.scientist.catalog[0].parameters.update(properties={
        "analysis_id": {"type": "string"}, "task_id": {"type": "string"}}, additionalProperties=False)
    reviewed = []
    def review(_kind, _payload):
        reviewed.append(lab._plan_doc)
        return {"action": "approve"}
    def execute(question, agenda, *_args, **_kwargs):
        assert reviewed and reviewed[-1].steps[0].settings == replacement.steps[0].settings
        assert lab._pinned_settings(agenda[0]) == {"mcp_cellqc_job_status": replacement.steps[0].settings}
        return LabResult(question, agenda, [], False, 0, "only checking the approved route")
    monkeypatch.setattr(lab, "_run_loop", execute)
    lab.run("q", saved_plan=original, plan_review=review,
            saved_plan_decision={"action": "edit", "plan_yaml": replacement.to_yaml()})
    assert original.steps[0].settings == {"min_genes": 200}
    assert lab._approved_plan.steps[0].tool == "mcp_cellqc_job_status"


@pytest.mark.parametrize("value", [None, False, 0, -0.5, 1e-9, "null", "off", "a,b", "line1\nline2\n",
                                   [], {}, [[1, 2], [3, 4]],
                                   {"中文": [{"source": "digraph G {\nA -> B;\n}"}, None, True]},
                                   {"empty": {}, "nested": {"array": [["1e-05", 0.00001]]}}])
def test_nested_json_settings_roundtrip_without_value_or_type_changes(value):
    import json
    doc = _doc("mcp_make_schematic", {"value": value})
    restored = parse_plan_yaml(doc.to_yaml()).steps[0].settings
    assert json.dumps(restored, sort_keys=True) == json.dumps({"value": value}, sort_keys=True)


@pytest.mark.parametrize("yaml_value", [".nan", ".inf", "-.inf", "!!binary YQ==", "!!set {a: null}",
                                        "{1: value}", "&loop [*loop]", "[" * 34 + "0" + "]" * 34])
def test_non_json_or_excessively_deep_yaml_setting_is_rejected(yaml_value):
    text = ("steps:\n  - id: s1\n    tool: mcp_make_schematic\n    does: Draw the specified graph.\n"
            "    settings:\n      value: " + yaml_value + "\n")
    with pytest.raises(PlanError, match="bounded JSON value"):
        parse_plan_yaml(text)


def test_same_run_natural_change_reviews_exact_status_replacement_then_resumes(registry, monkeypatch):
    import json
    registry.append(SimpleNamespace(name="cellqc", adapter="analysis", skill="test",
                                    tools=[{"name": "cellqc_job_status"}],
                                    planner_name=lambda name: "mcp_" + name))
    original = _doc("run_cellqc", {"min_genes": 200})
    args = {"analysis_id": "web-cellqc", "task_id": "qc-139c848c29239ac899545649"}
    lab = _lab(["mcp_cellqc_job_status"])
    lab.scientist.catalog[0].parameters.update(properties={
        "analysis_id": {"type": "string"}, "task_id": {"type": "string"}}, additionalProperties=False)
    lab._complete_fn = lambda *_a, **_k: json.dumps({"ops": [{"op": "change", "step": "s1",
        "tool": "mcp_cellqc_job_status", "settings": args,
        "does": "Read `mcp_cellqc_job_status` for the existing task; do not run new QC."}]})
    state = {"question": "q", "agenda": original.agenda(), "rounds": [],
             "approved_plan_yaml": original.to_yaml(),
             "plan_context": {"scope": {"source": "rules", "keep": ["mcp_run_cellqc", "run_cellqc"]}}}
    resume = ResumeState.from_run_state(state, 0,
                                       modify_note="Replace only step 1 with the existing task's status call.")
    seen = []
    class Waiting(Exception):
        pass
    def review(_kind, agenda):
        seen.append(lab._plan_doc)
        assert lab._plan_doc.steps[0].settings == args
        assert lab._plan_doc.steps[0].tool == "mcp_cellqc_job_status"
        assert agenda == lab._plan_doc.agenda()
        raise Waiting()
    with pytest.raises(Waiting):
        lab.run("q", resume=resume, plan_review=review)
    # Restart while the normal plan card is waiting. The approved old document and
    # prior rounds survive separately from the edited version being reviewed.
    resumed = ResumeState.from_review_state(json.loads(json.dumps(resume.to_review_state())))
    captured = []
    dependency_notes = []
    def evaluate(_agenda, _index, note, _rounds, _emit):
        dependency_notes.append(note)
        return {0}
    monkeypatch.setattr(lab, "_evaluate_redo_indices", evaluate)
    def decide(*_):
        return {"action": "proceed"}
    def compact():
        return False
    def execute(question, agenda, *_a, **kwargs):
        captured.append(lab._pinned_settings(agenda[0]))
        assert lab._scope.keep == ("mcp_cellqc_job_status",)
        assert kwargs["decision_review"] is decide and kwargs["should_compact"] is compact
        assert kwargs["seed_notes"] == resume.modify_note
        return LabResult(question, agenda, [], False, 0, "status only")
    monkeypatch.setattr(lab, "_run_loop", execute)
    lab.run("q", resume=resumed, approved_plan=seen[0], plan_context=state["plan_context"],
            decision_review=decide, should_compact=compact)
    assert captured == [{"mcp_cellqc_job_status": args}]
    assert dependency_notes == [resume.modify_note]
    assert original.steps[0].settings == {"min_genes": 200}


def test_gateway_same_run_revision_survives_restart_and_approval(lab_gateway):
    import asyncio
    import json
    from starlette.requests import Request
    from aiscientist.gateway import app as gw, saved_plans

    env = lab_gateway
    conn = env.connection()
    rid = "original-run"
    art = conn.workspace / rid / "artifacts"
    doc = replace(_doc("run_scanpy_qc", {"max_pct_mt": 20}), request="q")
    saved_plans.save(art, doc.to_yaml(), source="draft", state="approved")
    state = {"question": "q", "agenda": doc.agenda(), "rounds": [],
             "approved_plan_yaml": doc.to_yaml()}
    resume = ResumeState.from_run_state(state, 0, redo_indices={0}, modify_note="Set min_genes to 300")
    request = gw.LabRequest(connection_id=conn.id, question="q", conversation_id="same-chat")
    asyncio.run(gw._run_lab(conn, request, resume=resume, resume_run_id=rid))
    current = saved_plans.load(art)
    assert current["state"] == "shown" and not env.calls
    assert env.cards[-1]["run_id"] == rid
    checkpoint = json.loads((art / "process/run_state.json").read_text())
    assert checkpoint["resume_review"]["approved_plan_yaml"] == doc.to_yaml()
    assert parse_plan_yaml(current["yaml"]).steps[0].settings == {"max_pct_mt": 20, "min_genes": 300}

    restored = env.connection()
    async def approve():
        response = await gw.lab_plan_review(gw.PlanReviewRequest(
            connection_id=restored.id, run_id=rid, action="approve"),
            Request({"type": "http", "headers": []}))
        assert response.status_code == 200
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks)
    asyncio.run(approve())
    assert env.calls == [{"max_pct_mt": 20, "min_genes": 300}]
    assert saved_plans.load(art)["state"] == "approved"
    assert all(card["run_id"] == rid for card in env.cards)
