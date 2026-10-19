"""The live System overview (agents / pipelines / tools / skills / capabilities / graph)."""

from __future__ import annotations

from aiscientist.gateway import system_info


def test_overview_lists_agents_tools_capabilities_roadmap():
    o = system_info.system_overview()
    assert {a["name"] for a in o["agents"]} >= {"Principal Investigator", "Scientist", "Critic",
                                                "Claim auditor", "Writer"}

    names = {t["name"] for t in o["tools"]}
    # the real analysis line + figure + codeact are surfaced; finish (loop control) is hidden
    assert {"run_scanpy_qc", "run_de", "run_enrichment", "make_schematic", "run_code"} <= names
    assert "finish" not in names

    by_name = {t["name"]: t for t in o["tools"]}
    assert by_name["run_scanpy_qc"]["category"] == "analysis"
    assert by_name["run_scanpy_qc"]["requires"] == ["scanpy"]
    assert by_name["run_scanpy_qc"]["reads_private_data"] is True
    # A tool that runs on HPC3 is not checked against this host's modules: its job's container has
    # its software. An in-process tool is.
    assert by_name["run_scanpy_qc"]["runs_on"] == "hpc:analysis"
    assert by_name["run_scanpy_qc"]["available"] is None
    assert by_name["run_scanpy_qc"]["where"] == system_info.PLACES["hpc:analysis"][0]
    assert by_name["run_code"]["runs_on"] == "sandbox"
    ins = by_name["inspect_dataset"]
    assert ins["runs_on"] == "inprocess" and ins["available"] == all(system_info._have(d) for d in ins["requires"])

    # Only what this host itself uses is checked. D2 and Mermaid CLIs (never installed; chat diagrams
    # are drawn by the browser), the retired remote literature service, and paper-qa (the corpus
    # tools run on HPC3) used to show as permanent crosses.
    assert set(o["capabilities"]) == {"scanpy", "anndata", "gseapy", "matplotlib", "pandoc", "graphviz",
                                      "europepmc"}
    assert o["capabilities"]["europepmc"] is True
    assert "workflows" not in o and "roadmap" not in o

    # the Scientist's multi-specialist roster is surfaced for dev visibility
    spec_names = {s["name"] for s in o["specialists"]}
    assert any("QC" in n for n in spec_names) and any("Pathway" in n for n in spec_names)

    # the live workflow graph is included in the overview
    assert "graph" in o and o["graph"]["nodes"] and o["graph"]["edges"]


def test_tools_have_a_one_line_summary_and_their_full_description():
    from aiscientist.tools import catalog as tool_catalog

    tools = {t["name"]: t for t in system_info.system_overview()["tools"]}
    for name, t in tools.items():
        m = tool_catalog.manifest(name)
        assert t["summary"] and "\n" not in t["summary"], name
        assert t["description"], name
        if m is not None:
            assert t["summary"] == m.summary and t["code"] == f"src/aiscientist/tools/{m.folder.name}/"
        else:                                          # a registry tool: its description's first sentence
            assert t["code"] == "" and t["description"].startswith(t["summary"].rstrip("…"))
    assert tools["run_de"]["summary"] != tools["run_de"]["description"]


def test_pipelines_list_every_preset_with_a_title_and_a_summary():
    from aiscientist.agents.presets import list_presets

    rows = system_info.system_overview()["pipelines"]
    assert [p["key"] for p in rows] == [p["key"] for p in list_presets()]
    by_key = {p["key"]: p for p in rows}
    for p in rows:
        assert p["title"] and p["summary"] and p["description"] and p["tools"], p["key"]
        assert len(p["summary"]) <= 160 and "\n" not in p["summary"]
    # Title from the PROTOCOL.md heading, summary from its description; "e.g." ends no sentence.
    de = by_key["differential_expression"]
    assert de["title"] == "Differential Expression"
    assert de["summary"].startswith("Compare an experimental condition vs control (e.g. KO vs WT) per cell type")
    assert "pseudobulk" in de["description"]                    # the router's full text, when opened
    assert by_key["scgpt_annotation"]["summary"].startswith("scGPT")


def test_skills_are_the_ones_the_models_are_offered():
    from aiscientist.agents.skills import SKILLS, superseded_names

    rows = system_info.system_overview()["skills"]
    names = {s["name"] for s in rows}
    assert names == set(SKILLS) - superseded_names(SKILLS)
    for s in rows:
        assert s["summary"] and not s["summary"].lower().startswith("reference template"), s["name"]
        assert s["summary"][0] == s["summary"][0].upper() or not s["summary"][0].isalpha()
    by_name = {s["name"]: s for s in rows}
    if "pairwise_de" in by_name:
        assert by_name["pairwise_de"]["use"]                  # its "When to use" section


def test_first_sentence():
    fs = system_info._first_sentence
    assert fs("Finds papers. Then cites them.") == "Finds papers."
    assert fs("Compare A vs B (e.g. KO vs WT) per cell type. More.") == "Compare A vs B (e.g. KO vs WT) per cell type."
    assert fs("**Bold** and `code` stay as words.") == "Bold and code stay as words."
    long = fs("word " * 80)
    assert len(long) <= 160 and long.endswith("…")


def test_workflow_graph_is_consistent_and_code_derived():
    from aiscientist.tools import catalog as tool_catalog

    g = system_info.workflow_graph()
    ids = {n["id"] for n in g["nodes"]}
    types = {n["type"] for n in g["nodes"]}
    assert types == {"stage", "agent", "place", "tool"}
    assert {"request", "plan", "execute", "writeup", "report", "done", "scientist", "critic", "model"} <= ids

    # NO dangling edges — every edge endpoint is a real node (so the UI never breaks)
    for e in g["edges"]:
        assert e["source"] in ids and e["target"] in ids

    # tool nodes mirror the live catalog; finish (loop control) is excluded
    tools = [n for n in g["nodes"] if n["type"] == "tool"]
    tool_labels = {n["label"] for n in tools}
    assert "finish" not in tool_labels
    assert {"literature_search", "make_schematic", "run_code"} <= tool_labels
    # the literature corpus place names both corpus tools, not PaperQA alone
    assert "search_corpus" in system_info.PLACES["hpc:literature"][1]

    # each tool sits in the place its manifest names, and the Scientist reaches every place
    places = {n["id"]: n for n in g["nodes"] if n["type"] == "place"}
    for t in tools:
        assert t["meta"]["place"] in places
        m = tool_catalog.manifest(t["label"])
        if m is not None:
            assert t["meta"]["runs_on"] == m.runs_on
    assert {e["target"] for e in g["edges"] if e["type"] == "tool" and e["source"] == "scientist"} == set(places)
    # every place a manifest may name has a label of its own
    assert set(tool_catalog.RUNS_ON) <= set(system_info.PLACES)

    # when scanpy is installed the analysis line is an ordered pipeline chain
    if system_info._have("scanpy"):
        pipe = [e for e in g["edges"] if e["type"] == "pipeline"]
        assert any(e["source"] == "tool:run_scanpy_qc" and e["target"] == "tool:run_clustering" for e in pipe)
        assert any(e["source"] == "tool:run_cellqc" and e["target"] == "tool:run_clustering" for e in pipe)
        assert not any(e["source"] == "tool:run_scanpy_qc" and e["target"] == "tool:run_cellqc" for e in pipe)
