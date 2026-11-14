"""Per-step registration facts and observed method use (#110, #111)."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from aiscientist.agents import research_lab as rl, skills_registry
from aiscientist.agents.plan_doc import (
    PlanDoc, PlanStep, PlanError, adherence_markdown, apply_edit, apply_ops, check_adherence, check_plan,
    parse_plan_yaml, sync_plan,
)
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness
from test_mcp_bridge import Runner, build, registry  # noqa: F401 (fixture)
from test_plan_skills import library  # noqa: F401 (fixture)


def _lab(tmp_path, tools):
    return rl.ResearchLab(HarnessContext(workspace=tmp_path / "run", decisions={}),
                          rl.LabConfig(plan_yaml=True, auto_select_skill=False),
                          scientist=ResearchHarness(catalog=tools))


def _mcp(tool="resolve_genes", version="0.0.3"):
    return {"tool": tool, "provider": "mcp", "server": "atlas-expression-exploration", "version": version}


def test_two_atlases_multiple_tools_and_registered_skill_before_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "_tool_skill", lambda _: None)
    monkeypatch.setattr(rl, "_declared_tools", lambda text: {t for t in (
        "resolve_genes", "submit_query", "run_code") if f"`{t}`" in text})
    tools = [HarnessTool(name, "prose deliberately names wrong-server v99", {}, lambda a, c: {},
                         provider={k: v for k, v in _mcp(name).items() if k != "tool"},
                         skills=({"name": "atlas-query", "version": "1.4"},))
             for name in ("resolve_genes", "submit_query")]
    tools.append(HarnessTool("run_code", "Python", {}, lambda a, c: {}))
    lab = _lab(tmp_path, tools)
    doc = PlanDoc("LRAT/CRALBP/rho/ABCR", (
        PlanStep("atlas", "Query attached atlas via `resolve_genes` and `submit_query`.", "resolve_genes"),
        PlanStep("scrna", "Use `run_code` for the second atlas.", "run_code")))
    doc = lab._resolve_plan_provenance(doc, [0, 1])
    assert doc.steps[0].planned_tools() == [_mcp(), _mcp("submit_query")]
    assert doc.steps[0].planned_skills() == [{"name": "atlas-query", "version": "1.4"}]
    assert doc.steps[1].planned_tools()[0]["provider"] == "local"
    assert doc.steps[1].planned_skills() == []
    lab._plan_doc = doc
    events = []
    lab._approve_plan_document(events.append)
    assert "atlas-expression-exploration" in events[0]["yaml"]
    restored = parse_plan_yaml(events[0]["yaml"], request=doc.request)
    assert restored == doc
    # A changed deployment does not silently replace the approved pin on reopen.
    lab.scientist.catalog = []
    assert lab._resolve_plan_provenance(restored) == restored


def test_identity_lists_survive_sync_settings_and_edits():
    step = PlanStep("a", "**Atlas** — Query cells", "resolve_genes", tools=(_mcp(),),
                    skills=({"name": "query", "version": "1"}, {"name": "plot", "version": "2"}))
    doc = PlanDoc("", (step,))
    parsed = parse_plan_yaml(doc.to_yaml())
    assert parsed == doc
    synced, fresh = sync_plan(doc, doc.agenda(), request="", tool_of=lambda _: None,
                              runs_on_of=lambda _: "hpc:utilities")
    assert fresh == [] and synced.steps[0].tools == step.tools and synced.steps[0].skills == step.skills
    renamed = apply_ops(doc, [{"op": "change", "step": "a", "title": "Query atlas"}])
    edited, _, _ = apply_edit(doc, renamed)
    assert edited.steps[0].skills == step.skills and edited.steps[0].tools == step.tools
    assert not apply_ops(doc, [{"op": "change", "step": "a", "skill": None}]).steps[0].skills


@pytest.mark.parametrize("edit", ["yaml", "ops", "ops_with_tool"])
def test_tools_only_edit_checks_settings_against_new_primary(edit):
    before = PlanDoc("q", (PlanStep("s1", "Use tool_a at threshold 3.", "tool_a",
                                    {"threshold": 3}, tools=(_mcp("tool_a"), _mcp("tool_b"))),))
    tools = [_mcp("tool_b"), _mcp("tool_a")]
    if edit == "yaml":
        raw = before.to_public()[0]
        raw["tools"] = tools
        edited = parse_plan_yaml(json.dumps({"steps": [raw]}))
    else:
        op = {"op": "change", "step": "s1", "tools": tools}
        if edit == "ops_with_tool":
            op["tool"] = "tool_a"
        edited = apply_ops(before, [op])
    checked, findings = check_plan(
        edited, {"tool_a": {"threshold": {"type": "integer"}}, "tool_b": {}}, before=before)
    assert checked.steps[0].tool == "tool_b"
    assert [(f["kind"], f["tool"], f["param"]) for f in findings] == [
        ("unknown_setting", "tool_b", "threshold")]
    adopted, _, _ = apply_edit(before, checked)
    assert adopted.steps[0].tool == checked.steps[0].tool


@pytest.mark.parametrize("edit", ["yaml", "ops"])
def test_lab_rejects_tools_edit_with_settings_for_old_primary(tmp_path, edit):
    tools = [HarnessTool("tool_a", "A", {"type": "object", "properties": {
                 "threshold": {"type": "integer"}}}, lambda a, c: {}),
             HarnessTool("tool_b", "B", {}, lambda a, c: {})]
    lab = _lab(tmp_path, tools)
    before = PlanDoc("q", (PlanStep("s1", "Use tool_a at threshold 3.", "tool_a",
                                    {"threshold": 3}, tools=(_mcp("tool_a"), _mcp("tool_b"))),))
    lab._plan_doc = before
    events = []
    identities = [_mcp("tool_b"), _mcp("tool_a")]
    if edit == "yaml":
        raw = before.to_public()[0]
        raw["tools"] = identities
        result = lab._apply_plan_edit("q", json.dumps({"steps": [raw]}), events.append)
        assert events[-1]["type"] == "plan_edit_rejected"
    else:
        result = lab._revise_plan_document("q", "Change tools", events.append, ops=[{
            "op": "change", "step": "s1", "tool": "tool_a", "tools": identities}])
        assert events[-1]["type"] == "plan_revise_skipped"
    assert result is None
    assert lab._plan_doc is before
    assert "threshold" in str(events[-1]) and "tool_b" in str(events[-1])


@pytest.mark.parametrize("tool, tools", [
    ("tool_a", (_mcp("tool_b"), _mcp("tool_a"))),
    ("tool_c", (_mcp("tool_b"), _mcp("tool_a"))),
    ("tool_c", (_mcp("tool_a"),)),
])
def test_primary_tool_edit_preserves_singular_precedence_and_unchanged_lists(tool, tools):
    step = PlanStep("s1", "Query", "tool_a", tools=tools if tool == "tool_a" else (_mcp("tool_a"),))
    before = PlanDoc("q", (step,))
    edited = PlanDoc("q", (replace(step, tool=tool, tools=tools),))
    checked, findings = check_plan(edited, {name: {} for name in ("tool_a", "tool_b", "tool_c")},
                                   before=before)
    assert findings == [] and checked.steps[0].tool == tool
    adopted, _, _ = apply_edit(before, checked)
    assert adopted.steps[0].tool == tool


@pytest.mark.parametrize("text", [
    'plan: 1\nsteps:\n- id: old\n  tool: run_code\n  skill: score\n  does: Score cells\n',
    json.dumps({"plan": 1, "steps": [{"id": "old", "tool": "run_code", "skill": "score", "does": "Score cells"}]}),
])
def test_legacy_yaml_and_json_keep_explicit_unknown_metadata(text):
    doc = parse_plan_yaml(text)
    public = doc.to_public()[0]
    assert public["tool"] == "run_code" and public["skill"] == "score"
    assert public["tools"][0]["provider"] == "unknown"
    assert public["tools"][0]["version"] == public["skills"][0]["version"] == "unknown"
    assert parse_plan_yaml(doc.to_yaml()) == doc


@pytest.mark.parametrize("field,value", [("tools", "run_code"), ("skills", [{"version": "1"}]),
                                        ("tools", [{"tool": "x", "provider": "guessed"}])])
def test_malformed_identities_are_rejected(field, value):
    with pytest.raises(PlanError):
        parse_plan_yaml(json.dumps({"steps": [{"does": "Query", field: value}]}))


def test_observed_provider_changes_failures_and_skips_are_persisted(tmp_path):
    lab = _lab(tmp_path, [])
    lab._approved_plan = PlanDoc("", (
        PlanStep("atlas", "Query", "resolve_genes", tools=(_mcp(),)),
        PlanStep("skipped", "Skipped", "run_code", skill="planned-only")))
    events = []
    actual = _mcp(version="0.0.4")
    lab._record_step_provenance("Query", [{"tool": "resolve_genes", "provider": actual,
                                           "ok": False, "result": {"status": "error"}}], events.append)
    rows = lab.step_provenance()
    assert rows[0]["status"] == "failed" and rows[0]["skills"] == []
    assert rows[0]["provider_changes"] == [{"tool": "resolve_genes", "planned": [_mcp()], "used": actual}]
    assert rows[1]["status"] == "skipped" and rows[1]["skills"] == []
    for row in rows:
        lab._persist_step_provenance(row)
    manifest = json.loads((lab.ctx.workspace / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["step_provenance"]["atlas"] == rows[0]
    assert manifest["step_provenance"]["skipped"] == rows[1]
    assert events[0]["type"] == "step_provenance"


@pytest.mark.parametrize("review_round_trip", [False, True])
def test_stopped_run_continues_without_losing_step_skill_use(tmp_path, review_round_trip):
    from test_research_lab_resume import _lab as resume_lab
    from aiscientist.gateway import app as gw_app

    agenda = ["Run QC", "Cluster the cells"]
    plan = PlanDoc("characterize", tuple(
        PlanStep(f"s{i}", text, "run_qc") for i, text in enumerate(agenda, 1)))
    skill = {"name": "atlas-query", "version": "1.4", "status": "used"}
    first = resume_lab(agenda)
    first.ctx.workspace = tmp_path / "run"
    stopped = False
    run_scientist = first.scientist.run

    def scientist(*args, **kwargs):
        nonlocal stopped
        if first.ctx.decisions.get("step_skill_use"):
            stopped = True  # Stop during step 2, after step 1 was checkpointed.
        else:
            first.ctx.decisions["step_skill_use"] = {agenda[0]: [dict(skill)]}
        return run_scientist(*args, **kwargs)

    first.scientist.run = scientist

    def checkpoint(partial):
        gw_app._write_run_state(tmp_path / "artifacts", partial, None, first.ctx.decisions,
            extra={"approved_plan_yaml": plan.to_yaml(), "step_provenance": first.step_provenance()})

    first.run("characterize", approved_plan=plan, should_cancel=lambda: stopped, checkpoint=checkpoint)
    assert stopped
    state = json.loads((tmp_path / "artifacts/process/run_state.json").read_text(encoding="utf-8"))
    assert state["step_provenance"][0]["skills"] == [skill]
    resume = rl.ResumeState.from_interrupted(state)
    if review_round_trip:
        resume = rl.ResumeState.from_review_state(resume.to_review_state())
        resume = replace(resume, review_requested=False)
    second = resume_lab(agenda)
    second.ctx.workspace = first.ctx.workspace
    assert second.ctx.decisions == {}
    events = []
    result = second.run("characterize", resume=resume, on_event=events.append)
    assert result.converged and result.accepted_steps == 2
    rows = second.step_provenance()
    assert rows[0]["skills"] == [skill]
    assert second.ctx.decisions["step_skill_use"][agenda[0]] == [skill]
    manifest = json.loads((second.ctx.workspace / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["step_provenance"]["s1"]["skills"] == [skill]
    assert all(e["skills"] == [skill] for e in events
               if e["type"] == "step_provenance" and e["id"] == "s1")
    assert "atlas-query @ 1.4 [used]" in gw_app._readable_step_usage(rows)


def test_new_step_skill_evidence_merges_by_identity(tmp_path):
    lab = _lab(tmp_path, [])
    lab._approved_plan = PlanDoc("", (PlanStep("s1", "Query", "resolve_genes"),))
    first = {"name": "atlas-query", "version": "1.4", "status": "failed"}
    other = {"name": "other", "version": "2", "status": "used"}
    lab.ctx.decisions["step_skill_use"] = {"Query": [first, other]}
    lab._record_step_provenance("Query", [], lambda _e: None)
    updated = {**first, "status": "used"}
    lab.ctx.decisions["step_skill_use"]["Query"] = [updated]
    lab._record_step_provenance("Query", [], lambda _e: None)
    assert lab.step_provenance()[0]["skills"] == [updated, other]


@pytest.mark.parametrize("planned,call", [
    # A helper the plan never named is not a provider change.
    ((_mcp(),), {"tool": "run_code", "provider": {"tool": "run_code", "provider": "local", "server": "local",
                                                    "version": "unknown"}, "ok": True}),
    # Unknown metadata on either side establishes nothing.
    ((_mcp(),), {"tool": "resolve_genes", "ok": True}),
    (({"tool": "resolve_genes", "provider": "unknown", "server": "unknown", "version": "unknown"},),
     {"tool": "resolve_genes", "provider": _mcp(), "ok": True}),
])
def test_the_step_ledger_reports_no_change_without_a_known_planned_identity(tmp_path, planned, call):
    lab = _lab(tmp_path, [])
    lab._approved_plan = PlanDoc("", (PlanStep("atlas", "Query", "resolve_genes", tools=planned),))
    lab._record_step_provenance("Query", [call], lambda _e: None)
    assert lab.step_provenance()[0]["provider_changes"] == []


def test_adherence_lists_unused_secondary_tools_and_checks_recorded_provider_changes():
    doc = PlanDoc("", (PlanStep("s", "Query", "resolve_genes", tools=(_mcp(), _mcp("submit_query"))),))
    calls = [{"tool": "resolve_genes", "provider": _mcp(), "ok": True}]
    def report():
        return check_adherence(doc, [{"step": "Query", "accepted": True, "calls": calls}])
    assert report()["ok"]
    assert report()["steps"][0]["unused_planned_tools"] == ["submit_query"]
    assert "unused planned tools: `submit_query`" in adherence_markdown(report())
    calls.append({"tool": "submit_query", "provider": _mcp("submit_query", "0.0.4"), "ok": True})
    assert report()["steps"][0]["status"] == "provider_changed"
    calls[-1]["provider"] = _mcp("submit_query")
    assert report()["ok"]


@pytest.mark.parametrize("used", [None, {}, {"provider": "unknown"},
                                  {"provider": "local", "server": "local", "version": "unknown"}])
def test_report_calls_without_known_provider_are_not_changes(used):
    identity = {"tool": "write_report", "provider": "local", "server": "local", "version": "unknown"}
    doc = PlanDoc("", (PlanStep("report", "Write report", "write_report", tools=(identity,)),))
    call = {"tool": "write_report", "args": {}, "ok": True}
    if used is not None:
        call["provider"] = used
    report = check_adherence(doc, [{"step": "Write report", "accepted": True, "calls": [call]}])
    assert report["ok"]
    assert report["steps"][0]["provider_changes"] == []


@pytest.mark.parametrize("planned,used", [
    ({"provider": "unknown", "server": "unknown", "version": "unknown"}, _mcp()),
    (_mcp(), {"provider": "unknown", "server": "unknown", "version": "unknown"}),
    ({**_mcp(), "version": "unknown"}, _mcp()),
    (_mcp(), {**_mcp(), "version": "unknown"}),
])
def test_unknown_identity_fields_do_not_establish_provider_changes(planned, used):
    doc = PlanDoc("", (PlanStep("s", "Query", "submit_query", tools=({**planned, "tool": "submit_query"},)),))
    report = check_adherence(doc, [{"step": "Query", "accepted": True, "calls": [
        {"tool": "submit_query", "ok": True, "provider": used}]}])
    assert report["ok"] and report["steps"][0]["provider_changes"] == []


def test_failed_optional_tool_is_informational():
    doc = PlanDoc("", (PlanStep("s", "Query", "submit_query", tools=(
        _mcp("submit_query"), _mcp("query_status"))),))
    report = check_adherence(doc, [{"step": "Query", "accepted": True, "calls": [
        {"tool": "submit_query", "ok": True}, {"tool": "query_status", "ok": False}]}])
    assert report["ok"]
    assert report["steps"][0]["failed_optional_tools"] == ["query_status"]
    assert "failed optional tools: `query_status`" in adherence_markdown(report)


@pytest.mark.parametrize("primary,accepted,expected", [
    ({"ok": True, "args": {"n": 3}}, True, "provider_changed"),
    ({"ok": True, "args": {"n": 4}}, True, "settings_differ"),
    ({"ok": True, "args": {"n": 3}}, False, "not_accepted"),
    ({"ok": False, "args": {"n": 3}}, True, "tool_failed"),
    (None, True, "tool_not_called"),
])
def test_provider_changes_do_not_override_primary_settings_or_acceptance(primary, accepted, expected):
    doc = PlanDoc("", (PlanStep("s", "Query", "submit_query", settings={"n": 3}, tools=(
        _mcp("submit_query"), _mcp("query_status"))),))
    calls = [{"tool": "query_status", "ok": True, "provider": _mcp("query_status", "0.0.4")}]
    if primary is not None:
        calls.append({"tool": "submit_query", "provider": _mcp("submit_query"), **primary})
    report = check_adherence(doc, [{"step": "Query", "accepted": accepted, "calls": calls}])
    row = report["steps"][0]
    assert row["status"] == expected and len(row["provider_changes"]) == 1
    assert "provider changes:" in adherence_markdown(report)
    if expected == "settings_differ":
        assert row["detail"] == [{"param": "n", "approved": 3, "used": 4}]


def test_guidance_is_not_use_and_multiple_methods_stay_with_their_step(tmp_path, library):  # noqa: F811 (fixture)
    lab = _lab(tmp_path, [])
    first, second = list(library.values())
    with skills_registry.step_scope(first, step="A"):
        skills_registry.record_use(lab.ctx, first)
    assert lab.ctx.decisions.get("step_skill_use", {}) == {}
    with skills_registry.step_scope(first, step="A"):
        skills_registry.count_call(first, lab.ctx, {"status": "error"}, "dedicated_tool")
        skills_registry.count_call(second, lab.ctx, {"status": "ok"}, "another_tool")
    with skills_registry.step_scope(second, step="B"):
        skills_registry.count_call(second, lab.ctx, {"status": "error"}, "another_tool")
    observed = lab.ctx.decisions["step_skill_use"]
    assert {s["name"]: s["status"] for s in observed["A"]} == {first.name: "failed", second.name: "used"}
    assert observed["B"][0]["status"] == "failed"
    # Retried success updates the same identity without duplicating the step's use.
    with skills_registry.step_scope(first, step="A"):
        skills_registry.count_call(first, lab.ctx, {"status": "ok"}, "dedicated_tool")
    assert len(observed["A"]) == 2 and observed["A"][0]["status"] == "used"


def test_mcp_registration_and_method_outcome_not_transport_success(registry, tmp_path):  # noqa: F811 (fixture)
    runner = Runner()
    tool, = build(runner)
    assert tool.identity() == {"tool": "tables_check", "provider": "mcp", "server": "table-check", "version": "1.2.3"}
    lab = _lab(tmp_path, [tool])
    # A successful job containing an invalid MCP response is a failed method.
    runner.response["stdout"] = 'invalid response'
    with skills_registry.step_scope(step="bad"):
        assert tool.executor({"text": "x"}, lab.ctx)["status"] == "error"
    assert lab.ctx.decisions["step_skill_use"]["bad"][0]["status"] == "failed"
    runner.response["stdout"] = json.dumps({"result": {"rows": 7}})
    with skills_registry.step_scope(step="good"):
        result = tool.executor({"text": "x"}, lab.ctx)
    assert result["status"] == "ok"
    assert lab.ctx.decisions["step_skill_use"]["good"] == [{"name": "check-table", "version": "1.0.0", "status": "used"}]


@pytest.mark.parametrize("status,expected", [("ok", "used"), ("error", "failed")])
def test_dedicated_tool_skill_metadata_and_actual_outcomes(registry, tmp_path, status, expected):  # noqa: F811 (fixture)
    from aiscientist.agents.tool_skills import attach_skill
    tool = attach_skill(SimpleNamespace(name="dedicated", skill="check-table"), HarnessTool(
        "dedicated", "A dedicated method", {}, lambda a, c: {"status": status}))
    assert tool.skills == ({"name": "check-table", "version": "1.0.0"},)
    ctx = HarnessContext(workspace=tmp_path / "dedicated", decisions={})
    with skills_registry.step_scope(step="dedicated-step"):
        tool.executor({}, ctx)
    assert ctx.decisions["step_skill_use"]["dedicated-step"] == [
        {"name": "check-table", "version": "1.0.0", "status": expected}]


def test_secondary_tool_and_skill_are_checked():
    doc = PlanDoc("", (PlanStep("s", "Query", "first", tools=(
        {**_mcp(), "tool": "first"}, {**_mcp(), "tool": "unavailable"}),
        skills=({"name": "known", "version": "1"}, {"name": "missing", "version": "1"})),))
    _, findings = check_plan(doc, {"first": {}}, skill_name=lambda n: n if n == "known" else None)
    assert {f["kind"] for f in findings} == {"unknown_tool", "unknown_skill"}
    assert next(f for f in findings if f["kind"] == "unknown_skill")["param"] == "missing"


def test_harness_records_call_time_identity_even_on_exception(tmp_path):
    def fail(a, c):
        raise RuntimeError("server down")
    tool = HarnessTool("query", "Query", {}, fail, provider={k: v for k, v in _mcp().items() if k != "tool"})
    def chat(messages, tools):
        return {"tool_calls": [{"id": "t", "function": {"name": "query", "arguments": "{}"}}]}
    harness = ResearchHarness(catalog=[tool], chat_fn=chat)
    harness.config = replace(harness.config, max_steps=1)
    result = harness.run("Query", HarnessContext(workspace=tmp_path))
    assert result.steps[0]["provider"] == {**_mcp(), "tool": "query"}
    assert result.steps[0]["ok"] is False


def test_legacy_corpus_tool_records_explicit_unknown_identity():
    from test_search_corpus import _lab, OK_OUT
    result = _lab("PRPF31 haploinsufficiency causes adRP [PMID:11111111].", OK_OUT)()
    assert result.status == "ok"
    assert result.steps[0]["provider"] == {
        "tool": "search_corpus", "provider": "unknown", "server": "unknown", "version": "unknown"}
