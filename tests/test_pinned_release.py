"""The pinned aiscientist-skills release, as installed, against what the platform expects of it.

These tests read the release ``configs/skills_registry.json`` pins, where the loader reads it, so
they run where it is installed (the server, a developer's checkout) and are skipped elsewhere, CI
included. Install it with the configured repository/tag command returned by
``registry.install_command(registry.config())`` (also shown in the skip reason). Every other test builds its own release (``tests/conftest.py``).
"""
from __future__ import annotations

import json

import pytest

from aiscientist.agents import skills as sk
from aiscientist.agents import skills_registry as registry
from aiscientist.tools import catalog

CFG = registry.config()
RELEASE = registry.release_dir(CFG)
pytestmark = pytest.mark.skipif(
    not RELEASE.exists(),
    reason=f"pinned skills release {CFG['release_tag']} is not installed: {registry.install_command(CFG)}")


@pytest.fixture(scope="module")
def lib():
    return registry.load(sk._load_from)


def test_every_catalog_skill_of_an_allowed_tier_loads(lib):
    catalog_rows = json.loads((RELEASE / "catalog.json").read_text(encoding="utf-8"))["skills"]
    wanted = {r["name"] for r in catalog_rows if r["status"] in CFG["allowed_tiers"]}
    assert set(lib) == wanted
    assert {s.registry_release for s in lib.values()} == {CFG["release_tag"]}


def test_the_skills_name_only_tools_that_exist(lib):
    from aiscientist.agents.preset_pipelines import PIPELINES
    from test_tool_contract import PLATFORM_TOOLS, NATIVE_NAMES, toolish_names
    from aiscientist.agents.tool_names import canonical_tool_name

    # Older installed reference documents may discuss a canonical algorithm name;
    # only the actual registered MCP spelling is executable.
    known = (set(catalog.names()) | NATIVE_NAMES | {canonical_tool_name(n) for n in NATIVE_NAMES}
             | PLATFORM_TOOLS | set(PIPELINES) | set(lib)
             | {a for s in lib.values() for a in s.aliases})
    # This optional HTTP-client skill documents its separate VisionKB service, not
    # an executable gateway tool. Its seven names are the published service contract.
    external = {"answer-eye-questions-with-visionkb-kg": {"get_release", "search_entities",
        "get_relationships", "get_evidence", "get_gene_set", "query_atlas_expression", "find_datasets"}}
    bad = []
    for skill in lib.values():
        texts = {"SKILL.md": skill.doc, **{k: v for k, v in skill.files.items() if k.endswith(".md")}}
        bad += [f"{skill.name}/{rel}: `{name}`" for rel, text in texts.items()
                for name in toolish_names(text) if name not in known | external.get(skill.name, set())]
    assert not bad, "skills name tools that do not exist:\n" + "\n".join(sorted(set(bad)))


def test_the_skills_this_repository_names_resolve(lib):
    from test_tool_contract import NAMED_SKILLS

    missing = [f"{n} ({where})" for n, where in sorted(NAMED_SKILLS.items()) if sk.get_skill(n, lib) is None]
    assert not missing, f"named skills the pinned release lacks: {missing}"


def test_native_scientific_skill_images_match_the_pinned_catalog(lib):
    from aiscientist.agents.mcp_bridge import DEFAULT_CONFIG, load_servers

    servers = [s for s in load_servers(DEFAULT_CONFIG) if s.adapter == "analysis"]
    assert len(servers) == 7
    for server in servers:
        skill = sk.get_skill(server.skill, lib)
        assert skill is not None and skill.image, server.skill
        assert skill.image == server.image, server.name
        assert server.name in skill.doc, server.skill


def test_cellqc_default_contract_is_published_with_its_native_service(lib):
    from aiscientist.agents.mcp_bridge import DEFAULT_CONFIG, load_servers

    server = next(s for s in load_servers(DEFAULT_CONFIG) if s.name == "cellqc")
    assert sk.get_skill(server.skill, lib) is not None
    schema = next(t["inputSchema"] for t in server.tools if t["name"] == "run_cellqc")
    assert schema["properties"]["max_pct_mt"]["default"] == 10.0
    assert schema["properties"]["min_counts"]["default"] == 500
    assert schema["properties"]["min_features"]["default"] == 300
    assert schema["properties"]["doublet_decider"]["default"] == "doubletfinder"
