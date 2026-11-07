"""MCP domain output stays separate from the gateway's requested manuscript step."""
import json
import shlex

import pytest

from aiscientist.agents import research_lab as rl
from aiscientist.agents.research_harness import ResearchHarness
import test_atlas_expression
import test_mcp_bridge
from test_plan_yaml_flow import _lab

release = test_atlas_expression.release
registry = test_mcp_bridge.registry


@pytest.fixture(autouse=True)
def clear_known_names():
    rl._known_tool_names.cache_clear()
    yield
    rl._known_tool_names.cache_clear()


@pytest.mark.parametrize("source", ["resolve_genes", "atlas_write_report"])
def test_mentioning_mcp_output_does_not_allow_code_report_busywork(release, source):
    assert rl._is_report_busywork(f'Use `run_code` to write a report from the `{source}` output.')
    assert rl._is_report_busywork(f'From `{source}` output, use `run_code` to write a report.')


@pytest.mark.parametrize("written_report", [False, True])
def test_atlas_query_report_runs_while_gateway_report_is_handed_off(release, written_report):
    runner = test_atlas_expression.Runner()
    catalog = test_atlas_expression.tools(runner)
    atlas_step = '**Query summary** — Write the query report with `atlas_write_report(query_id="q1")`.'
    resolve_step = '**Resolve** — Use `resolve_genes(genes="LRAT")`.'
    agenda = [resolve_step, atlas_step, rl.REPORT_STEP]
    assert "write_report" not in catalog
    assert not rl._is_report_step(atlas_step) and not rl._is_report_busywork(atlas_step)
    assert "atlas_write_report" in catalog["atlas_write_report"].description

    def scientist(messages, _tools):
        if any(m.get("role") == "tool" for m in messages):
            name, args = "finish", {"answer": "Done"}
        else:
            brief = next(m["content"] for m in messages if m.get("role") == "user")
            step = brief.split("Step to execute now:", 1)[1].split("\n", 1)[0]
            name, args = (("atlas_write_report", {"query_id": "q1"}) if "atlas_write_report" in step
                          else ("resolve_genes", {"genes": "LRAT"}))
        return {"content": "", "tool_calls": [{"id": "t", "type": "function", "function": {
            "name": name, "arguments": json.dumps(args)}}]}

    def complete(messages):
        system = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in system:
            return json.dumps({"agenda": agenda})
        if "rigorous scientific Critic" in system:
            return '{"verdict":"accept","score":0.9,"critique":"ok"}'
        if "filling in a form" in system:
            return "[]"
        return "Done."

    lab = _lab([], {}, scientist=ResearchHarness(catalog=list(catalog.values()), chat_fn=scientist))
    lab._complete_fn = complete
    events, reviews = [], []

    def review(_kind, _steps):
        doc = next(e for e in reversed(events) if e.get("type") == "plan_doc")
        reviews.append(doc)
        if len(reviews) == 1:
            assert (doc["steps"][-1]["tool"] == rl.REPORT_TOOL) is written_report
            if written_report:
                assert not doc["steps"][-1].get("settings")  # no Atlas query_id schema on the gateway step
            return {"action": "edit", "plan_yaml": doc["yaml"].replace("query_id: q1", "query_id: q2")}
        return {"action": "approve"}

    question = "Resolve LRAT and produce the atlas query summary" + (" and a PDF report" if written_report else "")
    result = lab.run(question, events.append, review)
    assert len(reviews) == 2
    assert result.report_requested is written_report
    assert any("atlas_write_report" in step for step in result.agenda)
    assert all(not rl._is_report_step(step) for step in result.agenda)
    commands = [shlex.split(next(line for line in call[0].splitlines() if line.startswith("aiscientist-mcp ")))
                for call in runner.calls]
    assert [command[6] for command in commands] == ["resolve_genes", "write_report"]
    assert json.loads(commands[1][7]) == {"query_id": "q2"}
    assert bool([e for e in events if e.get("type") == "report_step"]) is written_report
    assert not [e for e in events if e.get("type") == "plan_edit_rejected"]
    if written_report:
        runner.calls.clear()
        resumed_lab = _lab([], {}, scientist=ResearchHarness(catalog=list(catalog.values()), chat_fn=scientist))
        resumed_lab._complete_fn = complete
        resumed = resumed_lab.run("Continue", resume=rl.ResumeState(
            [atlas_step, rl.REPORT_STEP], [], 0, report_requested=True))
        assert resumed.report_requested and resumed.agenda == [atlas_step]
        assert len(runner.calls) == 1 and " call write_report " in runner.calls[0][0]


@pytest.mark.parametrize("declared,allowed", [(False, True), (True, True), (True, False)])
def test_generic_mcp_tools_keep_installed_skill_metadata(registry, skill_release, monkeypatch, declared, allowed):
    config, source = registry
    md = source / "check-table" / "SKILL.md"
    value = "true" if declared else "false"
    md.write_text(md.read_text().replace("metadata:\n", f'metadata:\n  exploration: "{value}"\n  writes-report: "{value}"\n'))
    skill_release(source, tag="v0.2.1")
    if not allowed:
        monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_ALLOWED_TIERS", "core")
    test_mcp_bridge.write_config(config.parent, overrides={"aliases": {"check": "inspect-table"}})
    rl._known_tool_names.cache_clear()
    lab = _lab([], {})
    step = 'Inspect with `tables_inspect-table(text="fixture")`.'
    assert lab._explores_after(step) is (declared and allowed)
    assert [skill.name for skill in lab._step_skills(step)] == (["check-table"] if allowed else [])
    plan = lab._ensure_report_step([step], "Inspect the table", emit=lambda _event: None)
    assert plan == ([step, rl.REPORT_STEP] if declared and allowed else [step])
