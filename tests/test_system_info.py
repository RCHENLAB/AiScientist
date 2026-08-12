"""The live System overview (agents / tools / capabilities / roadmap)."""

from __future__ import annotations

from bioagent.gateway import system_info


def test_overview_lists_agents_tools_capabilities_roadmap():
    o = system_info.system_overview()
    assert {a["name"] for a in o["agents"]} == {"Principal Investigator", "Scientist", "Critic"}

    names = {t["name"] for t in o["tools"]}
    # the real analysis line + figure + codeact are surfaced; finish (loop control) is hidden
    assert {"run_scanpy_qc", "run_de", "run_enrichment", "make_schematic", "run_code"} <= names
    assert "finish" not in names

    by_name = {t["name"]: t for t in o["tools"]}
    assert by_name["run_scanpy_qc"]["category"] == "analysis"
    assert by_name["run_scanpy_qc"]["requires"] == ["scanpy"]
    assert by_name["run_scanpy_qc"]["reads_private_data"] is True
    # availability tracks whether the dep is importable on THIS host
    assert by_name["run_scanpy_qc"]["available"] == system_info._have("scanpy")

    # Biomni was retired; the literature stack is now Europe PMC (built-in) + env-gated remote + optional paper-qa.
    assert set(o["capabilities"]) >= {"scanpy", "gseapy", "pandoc", "graphviz", "europepmc", "paper-qa"}
    assert "biomni" not in o["capabilities"]
    assert o["capabilities"]["europepmc"] is True

    # workflow presets: the lab loop + the scrna pipeline, with code-derived stages
    flows = {w["name"]: w for w in o["workflows"]}
    assert any("Research Lab" in n for n in flows) and any("analysis pipeline" in n for n in flows)
    pipeline = next(w for w in o["workflows"] if w["kind"] == "pipeline")
    assert pipeline["stages"][:2] == ["run_scanpy_qc", "run_clustering"]   # derived from scrna_catalog order
    assert "roadmap" not in o

    # the Scientist's multi-specialist roster is surfaced for dev visibility
    spec_names = {s["name"] for s in o["specialists"]}
    assert any("QC" in n for n in spec_names) and any("Pathway" in n for n in spec_names)

    # the live workflow graph is included in the overview
    assert "graph" in o and o["graph"]["nodes"] and o["graph"]["edges"]


def test_workflow_graph_is_consistent_and_code_derived():
    g = system_info.workflow_graph()
    ids = {n["id"] for n in g["nodes"]}

    # core role loop is always present
    assert {"pi_plan", "scientist", "critic", "pi_synth", "report"} <= ids
    types = {n["type"] for n in g["nodes"]}
    assert {"agent", "output", "persona", "tool"} <= types

    # NO dangling edges — every edge endpoint is a real node (so the UI never breaks)
    for e in g["edges"]:
        assert e["source"] in ids and e["target"] in ids

    # tool nodes mirror the live catalog; finish (loop control) is excluded
    tool_labels = {n["label"] for n in g["nodes"] if n["type"] == "tool"}
    assert "finish" not in tool_labels
    assert {"literature_search", "make_schematic", "run_code"} <= tool_labels

    # the Scientist fans out to every tool with a "tool" edge
    tool_ids = {n["id"] for n in g["nodes"] if n["type"] == "tool"}
    tool_targets = {e["target"] for e in g["edges"] if e["type"] == "tool" and e["source"] == "scientist"}
    assert tool_targets == tool_ids

    # when scanpy is installed the analysis line is an ordered pipeline chain
    if system_info._have("scanpy"):
        pipe = [e for e in g["edges"] if e["type"] == "pipeline"]
        assert any(e["source"] == "tool:run_scanpy_qc" and e["target"] == "tool:run_clustering" for e in pipe)
