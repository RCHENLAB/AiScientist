"""Native MCP tools retain QC/planning/report semantics without changing dispatch names."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from aiscientist.agents import mcp_bridge, research_lab
from aiscientist.agents.scope import RunScope, qc_only, read_scope, tools_that_ran
from aiscientist.agents.tool_names import canonical_tool_name
from aiscientist.gateway import app
from aiscientist.reporting.verification import qc_section
from test_mcp_bridge import write_config


@pytest.fixture
def native_names(tmp_path, monkeypatch):
    domains = {
        "cellqc": ["run_cellqc", "cellqc_job_status"],
        "scrna-analysis": ["run_scanpy_qc", "run_de", "run_doublet_detection", "run_marker_annotation",
                           "run_enrichment", "run_gsea_prerank", "scrna_job_status"],
        "scgpt-annotation": ["scgpt_annotate"],
        "literature": ["deep_literature", "literature_search", "search_corpus"],
        "data-utilities": ["inspect_dataset", "make_schematic"],
    }
    rows, names = [], {}
    for server, tools in domains.items():
        folder = tmp_path / server
        folder.mkdir()
        aliases = {"run_cellqc": "qc_folder"} if server == "cellqc" else {}
        cfg = write_config(folder, entries=[{"name": name, "description": "Test tool.",
            "inputSchema": {"type": "object", "properties": {}}} for name in tools],
            overrides={"name": server, "release_tag": server + "-v1.2.3", "prefix": "mcp_native_",
                       "adapter": "analysis", "skill": "native-" + server, "resources": {}, "aliases": aliases})
        row = json.loads(cfg.read_text())["servers"][0]
        row["tools_file"] = server + "/" + row["tools_file"]
        rows.append(row)
        names.update({name: "mcp_native_" + aliases.get(name, name) for name in tools})
    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text(json.dumps({"schema_version": 1, "servers": rows}))
    monkeypatch.setenv("AISCIENTIST_MCP_CONFIG", str(cfg))
    assert len(mcp_bridge.load_servers(cfg)) == len(domains)
    return names, cfg


def round_with(tool, result, *, accept=True):
    return SimpleNamespace(step=f"Use `{tool}`", verdict=SimpleNamespace(verdict="accept" if accept else "revise"),
                           scientist_result={"steps": [{"tool": tool, "ok": True, "result": result}]})


def test_only_validated_domain_owned_registered_names_are_canonical(native_names):
    names, cfg = native_names
    assert canonical_tool_name(names["run_cellqc"]) == "run_cellqc"
    assert canonical_tool_name(names["run_de"]) == "run_de"
    assert canonical_tool_name("mcp_native_run_cellqc") == "mcp_native_run_cellqc"  # alias was explicit
    assert canonical_tool_name("mcp_forged_run_cellqc") == "mcp_forged_run_cellqc"
    assert canonical_tool_name("run_cellqc") == "run_cellqc"
    # A configured server cannot impersonate a domain it does not own.
    doc = json.loads(cfg.read_text())
    doc["servers"][0].update(name="variant-annotation", release_tag="variant-annotation-v1.2.3")
    cfg.write_text(json.dumps(doc))
    assert canonical_tool_name(names["run_cellqc"]) == names["run_cellqc"]
    cfg.write_text("invalid registry")
    assert canonical_tool_name(names["run_de"]) == names["run_de"]


def test_qc_scope_uses_available_mcp_names_without_widening_explicit_selection(native_names):
    names, _ = native_names
    catalog = {name: "tool" for name in names.values()}
    only = read_scope("Only QC; no differential expression, no literature.", catalog)
    assert names["run_cellqc"] in only.keep and names["run_scanpy_qc"] in only.keep
    assert names["run_de"] in only.exclude and names["deep_literature"] in only.exclude
    assert not only.allows_literature()
    assert RunScope(keep=(names["deep_literature"],)).allows_literature()
    assert only.allows(names["run_cellqc"]) and not only.allows(names["run_de"])
    # An exact migration test request must never silently admit a legacy substitute.
    exact = read_scope("Only run " + names["run_cellqc"], catalog | {"run_cellqc": "legacy"})
    assert exact.keep == (names["run_cellqc"],) and not exact.allows("run_cellqc")
    method = read_scope("Please run CellQC and return the result dataset.", catalog)
    assert method.keep == (names["run_cellqc"],)
    assert qc_only([names["inspect_dataset"], names["run_cellqc"], names["cellqc_job_status"]])
    assert not qc_only([names["run_cellqc"], names["run_de"]])
    assert not qc_only(["mcp_forged_run_cellqc"])


@pytest.mark.parametrize("tool", ["run_cellqc", "run_scanpy_qc"])
def test_native_qc_reply_verification_and_scope_preserve_original_round(native_names, tmp_path, tool):
    names, _ = native_names
    record = {"status": "ok", "cells_before": 1000, "cells_after": 900, "genes_after": 100,
              "warnings": [], "counts_layer": "raw UMI counts"}
    rnd = round_with(names[tool], record)
    original = deepcopy(rnd.scientist_result)
    result = SimpleNamespace(rounds=[rnd])
    assert app._round_qc(rnd) == (tool, record)
    assert app._qc_only_run(result)
    assert tools_that_ran([rnd]) == [names[tool]]
    reply = app._qc_chat_reply(tmp_path, result)
    assert "Quality control done" in reply and "900" in reply
    lines, summary = qc_section([rnd])
    assert summary["qc_consistent"] and any("900" in line for line in lines)
    assert summary["qc_tool"] == names[tool]
    scope = app._brief_scope_line(RunScope(keep=(names[tool],)), [names[tool]], True)
    assert "As requested" in scope and names[tool] in scope
    assert rnd.scientist_result == original


def test_raw_input_doublet_and_capability_checks_accept_registered_aliases(native_names):
    names, _ = native_names
    assert app._reads_raw_input("Run `" + names["run_cellqc"] + "`.")
    assert app._reads_raw_input("Run `" + names["run_scanpy_qc"] + "(min_genes=1)`.")
    assert not app._reads_raw_input("Run `mcp_forged_run_cellqc`.")
    doubles = round_with(names["run_doublet_detection"], {"status": "ok", "cells_before": 100,
                                                           "cells_after": 90, "filtered": True})
    assert "Doublets removed: 10" in app._standalone_doublet_lines(SimpleNamespace(rounds=[doubles]))[0]
    scg = round_with(names["scgpt_annotate"], {"status": "ok", "n_cells": 480})
    assert app._scan_tool_invocation(SimpleNamespace(rounds=[scg]), "scgpt_annotate")["n_cells"] == 480
    replay = {"scientist_result": scg.scientist_result}
    assert app._scan_tool_invocation(SimpleNamespace(rounds=[replay]), "scgpt_annotate")["n_cells"] == 480


def test_native_analysis_classifiers_and_marker_provenance(native_names):
    names, _ = native_names
    def step(name):
        return "Run `" + names[name] + "`."
    assert research_lab._is_de_producer_step(step("run_de"))
    assert research_lab._is_enrichment_step(step("run_enrichment"))
    assert research_lab._is_enrichment_step(step("run_gsea_prerank"))
    assert not research_lab._is_enrichment_step(step("run_marker_annotation") + " Assess enriched markers.")
    assert research_lab._is_literature_step(step("deep_literature"))
    assert research_lab._names_tool([step("run_de")], ("run_de", "run_pseudobulk_de"))
    marker = round_with(names["run_marker_annotation"], {"status": "ok", "reference": {"name": "retina"}})
    assert research_lab._marker_reference([marker])["tissue"] == "retina"
    marker.verdict.verdict = "revise"
    assert research_lab._marker_reference([marker]) is None


def test_explicit_native_literature_dispatch_does_not_take_legacy_fallback(native_names, tmp_path):
    from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness, default_catalog

    names, _ = native_names
    calls = []
    native = names["deep_literature"]
    def execute(args, ctx):
        calls.append(native)
        return {"status": "ok", "answer": "fixture"}
    def legacy(args, ctx):
        raise AssertionError("Explicit MCP plan must not execute a legacy substitute")
    responses = iter([
        {"content": "", "tool_calls": [{"id": "native", "type": "function", "function": {
            "name": native, "arguments": "{}"}}]},
        {"content": "", "tool_calls": [{"id": "done", "type": "function", "function": {
            "name": "finish", "arguments": '{"answer":"done"}'}}]},
    ])
    scientist = ResearchHarness(catalog=[
        *(tool for tool in default_catalog() if tool.name == "finish"),
        HarnessTool(native, "Native literature", {"type": "object", "properties": {}}, execute),
        HarnessTool("deep_literature", "Legacy literature", {"type": "object", "properties": {}}, legacy),
    ], chat_fn=lambda messages, tools: next(responses))
    lab = research_lab.ResearchLab(HarnessContext(workspace=tmp_path), research_lab.LabConfig(),
                                   complete_fn=lambda messages: "fixture", scientist=scientist)
    result = lab._scientist("Find literature", "Use `" + native + "`.",
                            research_lab.Specialist("Scientist", "Use approved tool"), "", [], lambda event: None)
    assert calls == [native]
    assert any(step["tool"] == native for step in result.steps)
    assert not any(step["tool"] == "deep_literature" for step in result.steps)
