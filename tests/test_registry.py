"""The tool registry is the single source of truth for the Scientist's catalog."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import types

from aiscientist.agents.registry import build_scientist_catalog, session_catalog

_HAS_SCANPY = importlib.util.find_spec("scanpy") is not None


def test_catalog_has_all_tool_groups_in_order():
    names = [t.name for t in build_scientist_catalog()]
    # analysis line + schematic + codeact + finish, all assembled from one place
    assert {"finish",
            "run_scanpy_qc", "run_clustering", "run_de", "run_enrichment",
            "literature_search", "make_schematic", "run_code",
            "map_phenotype_to_hpo", "run_lirical"} <= set(names)
    # ordering: the analysis line precedes run_code
    assert names.index("run_scanpy_qc") < names.index("run_code")
    # free text → HPO IDs precedes the tool that consumes them
    assert names.index("map_phenotype_to_hpo") < names.index("run_lirical")
    # the retired Biomni backend tools are no longer exposed to the model
    assert "run_biomni" not in names and "deep_research" not in names


def test_smoke_tools_dropped_when_scanpy_supersedes_them():
    names = {t.name for t in build_scientist_catalog()}
    if _HAS_SCANPY:
        # the real scanpy line supersedes the lightweight smoke QC/DE twins
        assert "run_qc" not in names and "run_de_markers" not in names
        assert {"run_scanpy_qc", "run_de"} <= names
    else:
        # no scanpy → keep the smoke fallbacks
        assert {"run_qc", "run_de_markers"} <= names


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


def test_run_lirical_in_catalog_and_gated_by_default():
    tool = next(t for t in build_scientist_catalog() if t.name == "run_lirical")
    # default (no phenotype_executor) → in-process path reports not_installed (LIRICAL runs only on HPC3)
    out = tool.executor({"hpo_terms": ["HP:0000510"]}, None)
    assert out["status"] == "not_installed"


def test_phenotype_executor_routes_run_lirical():
    seen = {}

    class _FakeExec:
        def run_tool(self, name, args, ctx):
            seen["name"], seen["args"] = name, args
            return {"status": "ok", "candidates": []}

    catalog = build_scientist_catalog(phenotype_executor=_FakeExec())
    tool = next(t for t in catalog if t.name == "run_lirical")
    out = tool.executor({"hpo_terms": ["HP:0000510"]}, None)
    assert out["status"] == "ok" and seen["name"] == "run_lirical"       # routed to the HPC executor
    # other tools are NOT rerouted to the phenotype executor
    assert next(t for t in catalog if t.name == "run_code").name == "run_code"

    # map_phenotype_to_hpo must stay IN-PROCESS on the eye server: it needs the session's served LLM
    # (ctx.tunnel_port), which does not exist inside the LIRICAL container. Routing it would silently
    # degrade every mapping to the curated keyword table.
    mapper = next(t for t in catalog if t.name == "map_phenotype_to_hpo")
    assert mapper.executor({"text": "retinitis pigmentosa"}, None)["hpo_terms"] == ["HP:0000510"]
    assert "map_phenotype_to_hpo" not in seen.get("name", "")


def test_every_tool_self_describes():
    for t in build_scientist_catalog():
        assert t.category and isinstance(t.requires, tuple)


def test_diagnose_disease_binds_the_ROUTED_lirical_and_literature():
    """``diagnose_disease`` composes run_lirical + deep_literature, so it must be bound AFTER routing.
    If it were bound earlier it would keep calling the in-process versions and the adjudicated
    differential would silently stop using HPC3 while the two tools it wraps still did."""
    calls: list[str] = []

    class _Phenotype:
        def run_tool(self, name, args, ctx):
            calls.append(name)
            return {"status": "ok", "candidates": [
                {"disease_name": "RP19", "gene": "ABCA4", "posttest_prob": 0.96,
                 "sources": ["lirical"]}]}

    class _Literature:
        def run_tool(self, name, args, ctx):
            calls.append(name)
            return {"status": "ok",
                    "answer": "Later cohorts failed to replicate this association.",
                    "contexts": [{"citation": "A 2021 PMID: 34000000",
                                  "summary": "the association could not be replicated"}]}

    catalog = build_scientist_catalog(phenotype_executor=_Phenotype(),
                                      literature_executor=_Literature())
    tool = next(t for t in catalog if t.name == "diagnose_disease")
    out = tool.executor({"hpo_terms": ["HP:0000510"]}, None)

    assert calls == ["run_lirical", "deep_literature"]        # BOTH routed executors were used
    assert out["mode"] == "lirical+literature"
    top = out["differential"][0]
    # the literature disputes LIRICAL's 96% call, and outranks it
    assert top["agreement"] == "conflict" and top["evidence_tier"] == "DISPUTED"
    assert top["posttest_prob"] == 0.96                       # LIRICAL's number left intact


def test_diagnose_disease_degrades_to_lirical_only_without_a_literature_executor():
    catalog = build_scientist_catalog()
    tool = next(t for t in catalog if t.name == "diagnose_disease")
    out = tool.executor({"hpo_terms": ["HP:0000510"]}, None)
    # nothing wired is not a FAILURE — same word run_lirical uses, so callers already handle it
    assert out["status"] == "not_installed" and out["mode"] == "lirical_only"
    assert out["differential"] == []
    # deep_literature IS in the catalog (unrouted), so a runner exists — there was simply nothing
    # to ask it about: no LIRICAL candidate and no candidate_genes
    assert any("nothing to ask the literature about" in n for n in out["notes"])


# --- a session that cannot run the HPC3 literature line ------------------------------------------

def _no_paperqa(monkeypatch):
    # sys.modules["paperqa"] = None makes `import paperqa` raise ImportError and find_spec return
    # None, whether or not paper-qa is installed here. The prod gateway venv does not have it.
    monkeypatch.setitem(sys.modules, "paperqa", None)


def test_unrouted_deep_literature_reports_a_missing_package_in_the_gateway_process(monkeypatch):
    """The condition of prod run BioAdmin/1e887ed2bf8e (2026-10-12): the session reasoned on the
    user's own API key, so it had no GPU allocation and the gateway built no literature executor.
    The tool then ran in the gateway process, where paper-qa is not installed, and answered
    ``dependency_missing`` at once. The package is not what was missing: the tool's HPC3 line was."""
    _no_paperqa(monkeypatch)
    tool = next(t for t in build_scientist_catalog(literature_executor=None)
                if t.name == "deep_literature")
    ctx = types.SimpleNamespace(model="claude-haiku-5-5", tunnel_port=None, workspace=None)
    out = tool.executor({"question": "Do rod genes dominate mouse retina scRNA-seq?"}, ctx)
    assert out["status"] == "dependency_missing" and out["dependency"] == "paper-qa"


def test_session_roster_leaves_out_deep_literature_when_nothing_can_run_it(monkeypatch):
    _no_paperqa(monkeypatch)
    bare = build_scientist_catalog(literature_executor=None)
    names = [t.name for t in session_catalog(bare, literature_executor=None)]
    assert "deep_literature" not in names
    # Europe PMC needs neither HPC3 nor a served model, so the literature step still has a tool
    assert "literature_search" in names
    # only that tool goes: the order and the rest of the roster are untouched
    # (`search_corpus` goes with it: its job needs the HPC3 corpus, which only the executor reaches)
    assert names == [t.name for t in bare if t.name not in ("deep_literature", "search_corpus")]
    # the bare catalog still lists it: the System page and the step classifier read that one as
    # "the tools that exist", and a step naming `deep_literature` must stay a literature step
    assert "deep_literature" in [t.name for t in bare]


def test_session_roster_keeps_deep_literature_where_it_can_run(monkeypatch):
    class _Literature:
        def run_tool(self, name, args, ctx):
            return {"status": "ok"}

    # routed to HPC3: kept, whatever the gateway process has installed
    _no_paperqa(monkeypatch)
    routed = build_scientist_catalog(literature_executor=_Literature())
    assert "deep_literature" in [t.name for t in session_catalog(routed, _Literature())]

    # no HPC3 line, but paper-qa is importable here (a development machine): the in-process path
    fake = types.ModuleType("paperqa")
    fake.__spec__ = importlib.machinery.ModuleSpec("paperqa", None)
    monkeypatch.setitem(sys.modules, "paperqa", fake)
    bare = build_scientist_catalog(literature_executor=None)
    assert "deep_literature" in [t.name for t in session_catalog(bare, literature_executor=None)]


def test_session_without_a_served_model_gets_search_corpus_not_deep_literature():
    class _Literature:
        def run_tool(self, name, args, ctx):
            return {"status": "ok"}

    ex = _Literature()
    catalog = build_scientist_catalog(literature_executor=ex)
    no_gpu = [t.name for t in session_catalog(catalog, ex, served_model=False)]
    assert "search_corpus" in no_gpu and "deep_literature" not in no_gpu
    assert "literature_search" in no_gpu
    gpu = [t.name for t in session_catalog(catalog, ex, served_model=True)]
    assert "search_corpus" in gpu and "deep_literature" in gpu


def test_quick_chat_without_a_served_model_gets_search_corpus():
    from aiscientist.agents.registry import build_quickchat_catalog

    class _Literature:
        def run_tool(self, name, args, ctx):
            return {"status": "ok"}

    no_gpu = {t.name for t in build_quickchat_catalog(_Literature(), served_model=False)}
    assert "search_corpus" in no_gpu and "deep_literature" not in no_gpu
    gpu = {t.name for t in build_quickchat_catalog(_Literature(), served_model=True)}
    assert {"search_corpus", "deep_literature"} <= gpu
