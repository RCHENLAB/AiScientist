"""Platform catalog ownership after native scientific migration."""
import importlib.util
from aiscientist.agents.registry import build_scientist_catalog, session_catalog
_HAS_SCANPY = importlib.util.find_spec("scanpy") is not None
def test_catalog_has_all_tool_groups_in_order():
    names = [t.name for t in build_scientist_catalog()]
    assert {"finish", "run_code", "describe_environment"} <= set(names)
    assert not {"run_scanpy_qc", "run_clustering", "run_de", "run_enrichment", "make_schematic", "run_cellqc"} & set(names)
    assert names[0] == "finish"
    assert "run_biomni" not in names and "deep_research" not in names


def test_scientific_fallbacks_are_absent_regardless_of_local_scanpy():
    names = {t.name for t in build_scientist_catalog()}
    assert not {"run_qc", "run_de_markers", "run_scanpy_qc", "run_de"} & names


def test_code_executor_is_injected_into_run_code():
    calls = {}

    def fake_executor(code):
        calls["code"] = code
        return {"status": "ok"}

    catalog = build_scientist_catalog(code_executor=fake_executor)
    run_code = next(t for t in catalog if t.name == "run_code")
    out = run_code.executor({"code": "print(1)"}, None)
    # a deterministic seed preamble is prepended for reproducibility (agents/provenance.py),
    # so the model's snippet is the suffix; provenance rides along in the result dict.
    assert out["status"] == "ok" and calls["code"].endswith("print(1)")
    assert isinstance(out.get("provenance"), dict) and out["provenance"]["seed"] == 0

    # no executor → run_code reports not_enabled rather than running anything
    rc2 = next(t for t in build_scientist_catalog() if t.name == "run_code")
    assert rc2.executor({"code": "x"}, None)["status"] == "not_enabled"


def test_every_tool_self_describes():
    for t in build_scientist_catalog():
        assert t.category and isinstance(t.requires, tuple)


def test_scientific_literature_and_phenotype_are_not_gateway_factories():
    from aiscientist.tools import catalog
    names = {m.name for m in catalog.manifests()}
    retired = {"literature_search", "deep_literature", "search_corpus", "extract_haploinsufficient_genes",
               "map_phenotype_to_hpo", "run_lirical", "diagnose_disease"}
    assert not retired & names
    assert not retired & {t.name for t in build_scientist_catalog()}


def test_session_roster_preserves_bound_native_tools_and_order():
    from aiscientist.agents.research_harness import HarnessTool
    calls = []
    native = HarnessTool(name="mcp_deep_literature", description="native fixture", parameters={},
                         executor=lambda args, ctx: calls.append(args) or {"status": "ok"})
    platform = next(t for t in build_scientist_catalog() if t.name == "run_code")
    roster = session_catalog([platform, native])
    assert roster == [platform, native]
    assert roster[1].executor({"question": "public evidence"}, None)["status"] == "ok"
    assert calls == [{"question": "public evidence"}]


def test_plain_quickchat_has_no_direct_scientific_fallback():
    from aiscientist.agents.registry import build_quickchat_catalog
    names = {t.name for t in build_quickchat_catalog()}
    assert not {"literature_search", "deep_literature", "search_corpus", "extract_haploinsufficient_genes"} & names
