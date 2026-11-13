"""Console routes consume registered MCP results, including priced failed pilots."""
from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from aiscientist.agents.research_harness import HarnessContext
from aiscientist.gateway import literature_mcp as route
from test_mcp_analysis import AnalysisRunner
from test_mcp_bridge import write_config, write_skill

TASK = "a" * 32 + "-" + "b" * 8


@pytest.fixture
def literature(tmp_path, monkeypatch, skill_release):
    def entry(name, properties):
        return {"name": name, "description": "Fixture literature tool.", "inputSchema": {
            "type": "object", "properties": {"analysis_id": {"type": "string"}, **properties},
            "additionalProperties": False}}
    entries = [entry("literature_search", {"query": {"type": "string"}, "limit": {"type": "integer"},
                                         "abstracts": {"type": "boolean"}, "pubmed_only": {"type": "boolean"}}),
               entry("extract_haploinsufficient_genes", {"spec": {"type": "object"},
                                                        "max_cost_usd": {"type": "number"}}),
               entry("literature_job_status", {"task_id": {"type": "string"}})]
    config = write_config(tmp_path, entries=entries, overrides={
        "name": "literature", "release_tag": "literature-v1.2.3", "entrypoint": "literature-mcp",
        "adapter": "analysis", "prefix": "mcp_staging_", "aliases": {"literature_search": "papers"},
        "resources": {}})
    monkeypatch.setenv("AISCIENTIST_MCP_CONFIG", str(config))
    source = tmp_path / "skills"
    write_skill(source)
    skill_release(source)
    runner = AnalysisRunner(tmp_path / "image runner")
    adapter = route.build_literature_route(runner, runner.artifacts)
    assert adapter is not None
    return adapter, runner, HarnessContext(workspace=tmp_path / "run")


def result(runner, payload):
    artifacts = runner.work / "literature" / "default" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    value = {"status": "ok", "state": "COMPLETED", "analysis_id": "default", "task_id": TASK,
             "artifact_root": str(artifacts), **payload}
    runner.fixture.write_text(json.dumps(value))
    return artifacts


def test_registered_alias_and_abstract_options_use_bridge_and_full_evidence(literature):
    adapter, runner, ctx = literature
    papers = [{"pmid": str(i), "abstract": "public scientific text " * 200} for i in range(12)]
    result(runner, {"results": papers, "count": len(papers)})
    actual = adapter.run_tool("literature_search", {"query": "PRPF31", "limit": 12,
                              "abstracts": True, "pubmed_only": True}, ctx)
    assert actual["status"] == "ok" and actual["results"] == papers
    assert actual["mcp"]["name"] == "literature"
    assert adapter.tools["literature_search"].name == "mcp_staging_papers"
    arguments = json.loads(json.loads(runner.capture.read_text())["argv"][-1])
    assert arguments == {"query": "PRPF31", "limit": 12, "abstracts": True, "pubmed_only": True}
    assert len(runner.calls) == 1


def test_failed_priced_pilot_reads_status_without_resubmitting_extraction(literature):
    adapter, runner, ctx = literature
    pilot = {"read": 5, "calls": 5, "cost_usd": 0.05, "estimated_total_usd": 0.8}
    result(runner, {"status": "failed", "state": "FAILED", "over_budget": True,
                    "pilot": pilot, "api_usage": {"cost_usd": 0.06, "calls": 6},
                    "error": "pilot projects beyond budget", "diagnostic": "evidence " * 2000})
    cli = runner.bin / "aiscientist-mcp"
    cli.write_text(cli.read_text().replace('sys.stdout.write(', f'''if "extract_haploinsufficient_genes" in sys.argv:
    sys.stderr.write("tool error: literature task {TASK} failed: pilot projects beyond budget")
    sys.exit(1)
sys.stdout.write('''))
    actual = adapter.run_tool("extract_haploinsufficient_genes", {"max_cost_usd": 0.1}, ctx)
    assert actual["status"] == "error" and actual["state"] == "FAILED"
    assert actual["pilot"] == pilot and actual["api_usage"]["cost_usd"] == 0.06
    assert actual["failure_recovered"] is True
    assert len(runner.calls) == 2
    recorded = json.loads(runner.capture.read_text())
    assert recorded["argv"][-2] == "literature_job_status"
    assert json.loads(recorded["argv"][-1]) == {"analysis_id": "default", "task_id": TASK}


def test_cancellation_stops_before_call_and_before_failure_recovery(literature):
    adapter, runner, ctx = literature
    adapter.should_cancel = lambda: True
    assert adapter.run_tool("literature_search", {"query": "PRPF31"}, ctx) == {"status": "cancelled"}
    assert not runner.calls
    stopped = {"value": False}
    adapter.should_cancel = lambda: stopped["value"]

    def fail(args, context):
        stopped["value"] = True
        return {"status": "error", "error": f"literature task {TASK} failed: canceled"}
    adapter.tools["extract_haploinsufficient_genes"] = SimpleNamespace(executor=fail)
    actual = adapter.run_tool("extract_haploinsufficient_genes", {}, ctx)
    assert actual["status"] == "error" and not runner.calls


def test_spec_and_cost_pass_unchanged_and_legacy_tables_are_staged(literature):
    adapter, runner, ctx = literature
    spec = {"target": "example", "definition": "A reviewed fixture spec"}
    folder = "curation_1234567890"
    native = result(runner, {"n_genes": 1, "api_usage": {"cost_usd": 0.02}}) / folder
    native.mkdir()
    for name in ("genes.csv", "hits.csv", "requests.csv"):
        (native / name).write_text("gene\nPRPF31\n")
    actual = adapter.run_tool("extract_haploinsufficient_genes", {"spec": spec, "max_cost_usd": 0.1}, ctx)
    args = json.loads(json.loads(runner.capture.read_text())["argv"][-1])
    assert args == {"spec": spec, "max_cost_usd": 0.1}
    target = adapter.stage_scan_tables(actual, folder)
    assert target == runner.artifacts / folder
    assert (target / "genes.csv").read_text() == "gene\nPRPF31\n"
    assert actual["api_usage"]["cost_usd"] == 0.02
    changed = replace(adapter, server=replace(adapter.server, image="docker://example/new@sha256:" + "c" * 64))
    assert adapter.rules_identity(spec) == adapter.rules_identity(dict(reversed(list(spec.items()))))
    assert changed.rules_identity(spec) != adapter.rules_identity(spec)
    assert adapter.rules_identity({**spec, "target": "other"}) != adapter.rules_identity(spec)


@pytest.mark.parametrize("mutation", ["other_analysis", "traversal", "symlink", "missing"])
def test_console_never_hydrates_outside_synced_tool_evidence(literature, mutation):
    adapter, runner, ctx = literature
    path = runner.artifacts / "mcp/literature/default/literature_search.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"status":"ok","results":["private"]}')
    name = "mcp/literature/default/literature_search.json"
    if mutation == "other_analysis":
        name = "mcp/literature/other/literature_search.json"
    elif mutation == "traversal":
        name = "mcp/literature/default/../../secret.json"
    elif mutation == "symlink":
        outside = runner.root / "outside.json"
        outside.write_text(path.read_text())
        path.unlink()
        path.symlink_to(outside)
    else:
        path.unlink()
    adapter.tools["literature_search"] = SimpleNamespace(executor=lambda args, ctx: {
        "status": "ok", "result": {"status": "ok", "full_result": name}})
    actual = adapter.run_tool("literature_search", {"query": "fixture"}, ctx)
    assert actual["status"] == "error" and "results" not in actual


def test_table_symlink_or_failure_cannot_publish_curation(literature):
    adapter, runner, _ = literature
    native = runner.artifacts / "mcp/literature/default/haploinsufficiency"
    native.mkdir(parents=True)
    for name in ("genes.csv", "hits.csv", "requests.csv"):
        (native / name).write_text("fixture")
    (native / "hits.csv").unlink()
    (native / "hits.csv").symlink_to(native / "genes.csv")
    with pytest.raises(ValueError, match="symlinks"):
        adapter.stage_scan_tables({"status": "ok"}, "haploinsufficiency")
    assert not (runner.artifacts / "haploinsufficiency").exists()
    with pytest.raises(ValueError, match="completed"):
        adapter.stage_scan_tables({"status": "failed"}, "haploinsufficiency")


def test_unregistered_or_unavailable_image_runner_has_no_legacy_fallback(tmp_path):
    assert route.build_literature_route(None, tmp_path, servers=()) is None
    assert route.build_literature_route(None, tmp_path, servers=(SimpleNamespace(name="literature"),)) is None


def test_bridge_still_rejects_unknown_parameters(literature):
    adapter, runner, ctx = literature
    result(runner, {})
    actual = adapter.run_tool("literature_search", {"query": "PRPF31", "endpoint": "https://example.test"}, ctx)
    assert actual["status"] == "error" and "Invalid MCP arguments" in actual["error"]
    assert not runner.calls
