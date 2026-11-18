"""A skill paired with an MCP server is a candidate of the protocol router.

Production run 6bc2b3b32969: "check expression pattern of RPE65 genes in this dataset", on a file
of the shared atlas, was routed to gene-signature scoring (the nearest preset pipeline) and planned
as model-written code, with the atlas expression tools registered and usable.
"""
from types import SimpleNamespace

import pytest

from aiscientist.agents import mcp_skill_routes as routes
from aiscientist.agents.preset_pipelines import PresetPipeline, select_pipeline


def _server(name, skill, tools, adapter="analysis", prefix=""):
    return SimpleNamespace(name=name, skill=skill, adapter=adapter,
                           tools=tuple({"name": t} for t in tools),
                           planner_name=lambda wire: prefix + wire)


ATLAS = _server("atlas-expression-exploration", "explore-atlas", ("resolve_genes", "submit_query"),
                adapter="atlas-expression")
SCRNA = _server("scrna-analysis", "analyze-scrna", ("run_de", "run_clustering"), prefix="mcp_")
RETIGENE = _server("retigene-rag", "query-retigene", ("get_gene", "find_genes"), adapter="retigene")
SKILLS = {
    "explore-atlas": SimpleNamespace(name="explore-atlas", category="single-cell",
                                     summary="Answer a gene expression query on an atlas. More detail here."),
    "analyze-scrna": SimpleNamespace(name="analyze-scrna", category="single-cell", summary="Analyse scRNA."),
    "query-retigene": SimpleNamespace(name="query-retigene", category="literature",
                                      summary="Look genes up in the RetiGene table."),
}
PRESET = PresetPipeline("differential_expression", "Compare a condition with control", "DE guidance",
                        tools=("mcp_run_de", "run_code"), data_type="scrna")
ALL = frozenset({"resolve_genes", "submit_query", "mcp_run_de", "mcp_run_clustering", "get_gene",
                 "find_genes", "run_code"})
DESCRIPTIONS = {"resolve_genes": "Resolve gene names against the atlas genes. It takes seconds.",
                "submit_query": "Start a query."}


@pytest.fixture
def registry(monkeypatch):
    from aiscientist.agents import atlas_expression, mcp_bridge, skills
    monkeypatch.setattr(mcp_bridge, "configured_servers", lambda: (ATLAS, SCRNA, RETIGENE))
    monkeypatch.setattr(skills, "get_skill", lambda name, *a, **k: SKILLS.get(name))
    monkeypatch.setattr(atlas_expression, "configured_atlases", lambda: {"/dfs3b/atlas/HRCA.h5ad": "HRCA/1"})
    monkeypatch.setattr(atlas_expression, "shared_release",
                        lambda path: "heca/v1" if path.startswith("/dfs3b/shared/heca/") else None)


def _keys(*args, **kwargs):
    return [p.key for p in routes.skill_pipelines(*args, **kwargs)]


def test_a_paired_skill_no_preset_covers_is_a_candidate(registry):
    got = routes.skill_pipelines((PRESET,), ALL, DESCRIPTIONS, ["/dfs3b/shared/heca/v1/RPEChoroid.h5ad"])
    assert [p.key for p in got] == ["explore-atlas", "query-retigene"]
    atlas = got[0]
    assert atlas.data_type == "scrna" and atlas.tools == ("resolve_genes", "submit_query")
    assert atlas.label == "Answer a gene expression query on an atlas. More detail here."
    assert "`resolve_genes`: Resolve gene names against the atlas genes. It takes seconds." in atlas.prompt
    assert "do not plan `run_code` for what one of them does" in atlas.prompt
    assert 'read_skill_reference("explore-atlas")' in atlas.prompt
    assert got[1].data_type == ""          # not a data modality: offered when no content routing applies


def test_a_server_a_preset_already_composes_is_not_a_second_candidate(registry):
    # scrna-analysis: the DE preset composes mcp_run_de, so its requests already have a protocol.
    assert "analyze-scrna" not in _keys((PRESET,), ALL, {}, ["/dfs3b/atlas/HRCA.h5ad"])
    assert "analyze-scrna" in _keys((), ALL, {}, [])


def test_the_atlas_skill_is_offered_only_for_a_dataset_its_server_accepts(registry):
    assert "explore-atlas" in _keys((PRESET,), ALL, {}, ["/dfs3b/atlas/HRCA.h5ad"])          # listed
    assert "explore-atlas" in _keys((PRESET,), ALL, {}, ["/x/y.h5ad", "/dfs3b/shared/heca/v1/a.h5ad"])
    assert "explore-atlas" not in _keys((PRESET,), ALL, {}, ["/dfs3b/u/my_upload.h5ad"])
    assert "explore-atlas" not in _keys((PRESET,), ALL, {}, [])
    assert "explore-atlas" not in _keys((PRESET,), ALL, {}, ["/dfs3b/shared/heca/v1/notes.csv"])


def test_no_candidate_whose_tools_cannot_run_here_or_are_unknown(registry):
    assert _keys((PRESET,), frozenset({"run_code"}), {}, ["/dfs3b/atlas/HRCA.h5ad"]) == []
    assert _keys((PRESET,), None, {}, ["/dfs3b/atlas/HRCA.h5ad"]) == []
    # Only the usable tools of a server are named.
    got = routes.skill_pipelines((PRESET,), frozenset({"get_gene"}), {}, [])
    assert [(p.key, p.tools) for p in got] == [("query-retigene", ("get_gene",))]


def test_the_router_can_choose_the_skill_and_its_guidance_reaches_the_plan(registry):
    library = (PRESET, *routes.skill_pipelines((PRESET,), ALL, DESCRIPTIONS, ["/dfs3b/atlas/HRCA.h5ad"]))
    seen: list = []

    def complete(messages):
        seen.append(messages[-1]["content"])
        return "explore-atlas"

    events: list = []
    chosen = select_pipeline(complete, "check expression pattern of RPE65 genes in this dataset", "",
                             library, events.append, content_modality="scrna",
                             content_confidence="high", available_tools=ALL)
    assert chosen is not None and chosen.key == "explore-atlas"
    # Content routing keeps the scRNA candidates: the preset and the atlas skill, not the gene table.
    assert "- explore-atlas: Answer a gene expression query" in seen[0]
    assert "- differential_expression:" in seen[0] and "query-retigene" not in seen[0]
    assert events[-1]["key"] == "explore-atlas"


def test_the_lab_adds_the_candidates_to_its_library(registry):
    from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness
    from aiscientist.agents.research_lab import LabConfig, ResearchLab

    tools = [HarnessTool(n, f"{n} description.", {"type": "object", "properties": {}},
                         lambda a, c: {"status": "ok"}, category="analysis")
             for n in ("resolve_genes", "submit_query", "mcp_run_de")]
    ctx = HarnessContext(decisions={"hpc_primary": "/dfs3b/shared/heca/v1/RPEChoroid.h5ad"},
                         tunnel_port=1, model="m")
    lab = ResearchLab(ctx, LabConfig(skill_library=(PRESET,)), complete_fn=lambda m: "none",
                      scientist=ResearchHarness(catalog=tools, chat_fn=lambda m, t: {}))
    assert [p.key for p in lab._router_library()] == ["differential_expression", "explore-atlas"]
    # A dataset of the user's own: the atlas server would refuse it, so it is not offered.
    ctx.decisions["hpc_primary"] = "/dfs3b/u/mine.h5ad"
    assert [p.key for p in lab._router_library()] == ["differential_expression"]
