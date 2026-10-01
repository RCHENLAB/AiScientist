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
    assert domain == catalog.names(), "every domain tool comes from a manifest, in manifest order"


def test_hpc_routing_follows_runs_on():
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
        if m.runs_on.startswith("hpc:"):
            seen.clear()
            cat[m.name].executor({}, None)
            assert seen == [(m.runs_on, m.name)], f"{m.name} did not go to {m.runs_on}"
