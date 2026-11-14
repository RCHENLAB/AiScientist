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


def test_a_tool_that_opts_out_of_research_reaches_no_model(tmp_path, monkeypatch):
    """A generic hidden manifest stays callable by an operator but is never offered to a model."""
    from aiscientist.agents.registry import build_quickchat_catalog, build_scientist_catalog
    from aiscientist.agents.research_harness import HarnessTool
    from aiscientist.agents.research_lab import _known_tool_names

    hidden = catalog.ToolManifest(name="operator_fixture", summary="Operator only", category="fixture",
                                  runs_on="inprocess", order=1, folder=tmp_path / "operator_fixture",
                                  research=False, chat=False)
    visible = catalog.ToolManifest(name="platform_fixture", summary="Read-only platform fixture", category="fixture",
                                   runs_on="inprocess", order=2, folder=tmp_path / "platform_fixture", chat=True)
    monkeypatch.setattr(catalog, "manifests", lambda: (hidden, visible))
    monkeypatch.setattr(catalog.ToolManifest, "factory", lambda m: lambda: HarnessTool(
        name=m.name, description=m.summary, parameters={}, executor=lambda args, ctx: {"status": "ok"}))
    for built in (build_scientist_catalog(), build_quickchat_catalog()):
        names = {t.name for t in built}
        assert "operator_fixture" not in names and "platform_fixture" in names
    assert "operator_fixture" not in _known_tool_names()
    assert catalog.build(hidden).executor({}, None) == {"status": "ok"}


def test_hpc_routing_follows_manifest_without_a_scientific_factory(tmp_path):
    from types import SimpleNamespace
    from aiscientist.agents.registry import _router
    from aiscientist.agents.research_harness import HarnessTool

    seen = []
    line = SimpleNamespace(run_tool=lambda name, args, ctx: seen.append((name, args, ctx)) or {"status": "ok"})
    m = catalog.ToolManifest(name="routed_fixture", summary="Routing fixture", category="fixture",
                             runs_on="hpc:analysis", order=1, folder=tmp_path / "routed_fixture")
    original = HarnessTool(name=m.name, description=m.summary, parameters={},
                           executor=lambda args, ctx: {"status": "local"})
    bound = _router({"hpc:analysis": line})(m, original)
    args, ctx = {"nested": {"flag": False}}, object()
    assert bound.executor(args, ctx) == {"status": "ok"}
    assert seen == [(m.name, args, ctx)]
    assert bound.executor.__wrapped__ is original.executor
    assert _router({})(m, original).executor(args, ctx) == {"status": "local"}
