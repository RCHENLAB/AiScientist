"""The environment manifest — "what do we have, and where is it".

Written after run 3c5fbc8608a7, where two capabilities were switched off by facts nobody had
written down anywhere the agent could read: a step that had to verify scGPT's model directory
could not reach it, and a step that needed gene sets went to the network while three verified
`.gmt` libraries sat in the directory the enrichment tools read from. Both steps behaved
correctly and both produced nothing.

The manifest only helps if it is TRUE, so these tests check the properties that make it true:
sources that resolve, paths that come from the live settings, and a readability flag that
actually reflects the session's roots.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bioagent.gateway.environment import (
    asset_inventory,
    environment_manifest,
    render_for_agent,
    render_markdown,
    tool_index,
)

REPO = Path(__file__).resolve().parents[1]


def test_every_tool_resolves_to_a_real_source_location():
    """The half that was missing. `HarnessTool` stores its callable as `executor`; a first version
    of this module read `runner`, got None for all of them, and rendered a tools table with no
    locations at all — which looks fine until you try to use it."""
    tools = tool_index()
    assert tools, "the catalog is empty"
    unresolved = [t["name"] for t in tools if not t["source"]]
    assert not unresolved, f"no source location for: {unresolved}"
    for t in tools:
        path, _, line = t["source"].rpartition(":")
        assert (REPO / path).is_file(), f"{t['name']} -> {path} does not exist"
        assert int(line) > 0


def test_the_gene_sets_that_caused_the_failure_are_in_the_inventory():
    items = {a["label"]: a for a in asset_inventory()}
    gmt = next(a for k, a in items.items() if "Gene-set" in k)
    assert gmt["where"] == "gateway"
    # The note must state the two facts the pathway step lacked: they are read offline, and a
    # hand-computed summary should use these same files instead of fetching a collection.
    note = " ".join(gmt["note"].split())
    assert "no network" in note and "no Enrichr API" in note
    assert "rather than fetch a collection" in note


def test_model_and_container_paths_come_from_the_live_settings():
    from bioagent.gateway.settings import HPCSettings

    st = HPCSettings()
    paths = {a["path"] for a in asset_inventory(st)}
    # If these ever drift from settings, the manifest becomes a second source of truth that is
    # wrong — the failure mode this module exists to prevent, reintroduced one level up.
    assert st.scgpt_model_dir in paths
    assert st.vlreview_model_dir in paths
    assert st.analysis_image in paths
    assert st.scgpt_image in paths


def test_readability_reflects_the_sessions_roots_not_a_guess():
    from bioagent.gateway.settings import HPCSettings

    st = HPCSettings()
    shared = st.shared_root.rstrip("/")
    # A session that may read the containers but NOT the model dir — exactly the prod configuration
    # that made the scGPT step withhold inference.
    m = environment_manifest(st, read_roots=(f"{shared}/containers",), write_roots=())
    by_path = {a["path"]: a for a in m["assets"]}
    assert by_path[st.scgpt_image]["agent_readable"] is True
    assert by_path[st.scgpt_model_dir]["agent_readable"] is False

    text = render_for_agent(m, "assets")
    assert "NOT READABLE" in text          # the agent is told plainly, not left to infer it


def test_no_session_reports_unknown_rather_than_claiming_readable():
    m = environment_manifest()
    assert all(a["agent_readable"] is None for a in m["assets"])
    assert "not a cluster session" in render_for_agent(m, "roots")


def test_remote_paths_never_claim_existence():
    """This host cannot stat /dfs3b. Saying "exists: false" for an unreachable path would be a
    confident wrong answer, which is the one output worse than no answer."""
    for a in asset_inventory():
        if a["where"] == "hpc3":
            assert a["exists"] is None, a["label"]


@pytest.mark.parametrize("section", ["all", "assets", "tools", "roots"])
def test_agent_rendering_is_non_empty_for_every_section(section):
    assert render_for_agent(environment_manifest(), section).strip()


def test_the_markdown_carries_the_tool_locations():
    md = render_markdown(environment_manifest())
    assert "src/bioagent/tools/run_de/tool.py:" in md
    assert "regenerate it" in md            # the doc says not to hand-edit it
    assert "| tool | what it does | source | available |" in md


def test_describe_environment_is_offered_to_the_scientist():
    from bioagent.agents.registry import build_scientist_catalog

    tool = next((t for t in build_scientist_catalog() if t.name == "describe_environment"), None)
    assert tool is not None, "the manifest exists but nothing can reach it"
    out = tool.executor({"section": "assets"}, None)
    assert out["status"] == "ok" and "ASSETS" in out["environment"]
    # The description has to say WHEN to call it, or a step that needs it will not think to.
    desc = " ".join(tool.description.split())
    assert "before downloading anything that might already be on disk" in desc
