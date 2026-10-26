"""The platform's catalog is built from the tool manifests (agents/registry.py + tools/catalog.py).

Every domain tool reaches the Scientist through its TOOL.md, in manifest order, and its executor
goes to the HPC3 job line its ``runs_on`` names. (The manifests' agreement with the tool code itself
is tests/test_tool_manifests.py, which moves with AiScientist-tools.)"""
from __future__ import annotations

from aiscientist.tools import catalog

# The platform's own tools (assembled in agents/registry.py, not discovered from manifests).
PLATFORM_TOOLS = {"finish", "run_qc", "run_de_markers", "run_code", "describe_environment",
                  "search_skills", "read_skill_reference", "read_tool_source",
                  "list_dir", "stat_path", "find_files", "read_text", "disk_usage", "run_shell",
                  "fetch_url", "install_package"}


def test_the_scientist_catalog_is_the_manifests_plus_the_platform_tools():
    from aiscientist.agents.registry import build_scientist_catalog

    built = [t.name for t in build_scientist_catalog()]
    domain = [n for n in built if n not in PLATFORM_TOOLS]
    offered = [m.name for m in catalog.manifests() if m.research]
    assert domain == offered, "every domain tool comes from a manifest, in manifest order"


def test_a_tool_that_opts_out_of_research_reaches_no_model():
    """``research: false`` = the tool runs only from its own console line. The PI plans from the
    Scientist's catalog and the lab recognises a step's tool by it, so absence there is what keeps
    an hour-long corpus scan out of a study; with a literature executor wired it stays absent."""
    from aiscientist.agents.registry import build_quickchat_catalog, build_scientist_catalog
    from aiscientist.agents.research_lab import _known_tool_names

    class _Line:
        def run_tool(self, name, args, ctx):
            return {"status": "ok"}

    hidden = {m.name for m in catalog.manifests() if not m.research}
    assert hidden, "the corpus scan declares research: false; if it no longer does, drop this test"
    for built in (build_scientist_catalog(), build_scientist_catalog(literature_executor=_Line()),
                  build_quickchat_catalog(literature_executor=_Line())):
        assert not hidden & {t.name for t in built}
    assert not hidden & _known_tool_names()
    # Still a tool of the package: its job line finds its resources and code by name.
    assert hidden <= set(catalog.names())


def test_hpc_routing_follows_runs_on(cellqc_release):     # run_cellqc runs the CellQC skill's method
    from aiscientist.agents.registry import build_scientist_catalog

    seen = []

    class _Line:
        def __init__(self, line):
            self.line = line

        def run_tool(self, name, args, ctx):
            seen.append((self.line, name))
            return {"status": "ok"}

    lines = {"analysis_executor": "hpc:analysis", "variant_executor": "hpc:variant",
             "phenotype_executor": "hpc:phenotype", "literature_executor": "hpc:literature"}
    cat = {t.name: t for t in build_scientist_catalog(**{k: _Line(v) for k, v in lines.items()})}
    for m in catalog.manifests():
        if m.runs_on.startswith("hpc:") and m.research:
            seen.clear()
            cat[m.name].executor({}, None)
            assert seen == [(m.runs_on, m.name)], f"{m.name} did not go to {m.runs_on}"
