"""Offline tests for the single-cell analysis line (tools/scrna_pack).

scanpy/gseapy are heavy and live only in the `analysis` extra, so these tests do NOT
require them. They assert the two things that must hold on any host:

  1. the module imports + the tools wire into the harness catalog with valid schemas;
  2. each tool degrades gracefully (no crash) when its dependency is absent or the
     pipeline is called out of order — returning a structured status, never raising.

Real scanpy execution is validated on the eye-server (where `analysis` is installed).
"""

from __future__ import annotations

import builtins
import types

from bioagent.tools import scrna_pack


def _ctx(tmp_path, dataset_path=None):
    return types.SimpleNamespace(
        workspace=tmp_path,
        decisions={"dataset_path": str(dataset_path)} if dataset_path else {},
    )


def test_catalog_shape_matches_harness_tools():
    cat = scrna_pack.scrna_catalog()
    names = [t.name for t in cat]
    assert names == ["run_scanpy_qc", "run_clustering", "run_de", "run_enrichment",
                     "run_gsea_prerank",
                     # the steps the line was missing (tools/scrna_advanced)
                     "run_doublet_detection", "run_integration", "run_pseudobulk_de",
                     "run_composition", "run_marker_annotation"]
    for tool in cat:
        schema = tool.schema()                       # HarnessTool.schema() -> OpenAI function shape
        assert schema["type"] == "function"
        assert schema["function"]["name"] == tool.name
        assert schema["function"]["parameters"]["type"] == "object"
    # Only the two pathway tools stay off the private-data path: they see gene SYMBOLS, never
    # the expression matrix. Everything else touches cells and must stay marked.
    off = {t.name for t in cat if not t.reads_private_data}
    assert off == {"run_enrichment", "run_gsea_prerank"}


def test_qc_missing_dependency_is_graceful(tmp_path, monkeypatch):
    # Simulate scanpy/matplotlib not installed: every `import` of them raises ImportError.
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.split(".")[0] in {"scanpy", "anndata", "matplotlib"}:
            raise ImportError(name=name.split(".")[0])
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    out = scrna_pack.run_scanpy_qc({}, _ctx(tmp_path, tmp_path / "x.h5ad"))
    assert out["status"] == "dependency_missing"
    assert out["dependency"] in {"scanpy", "matplotlib", "anndata"}


def test_enrichment_missing_gseapy_is_graceful(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.split(".")[0] == "gseapy":
            raise ImportError(name="gseapy")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    out = scrna_pack.run_enrichment({}, _ctx(tmp_path))
    assert out["status"] == "dependency_missing" and out["dependency"] == "gseapy"


def _install_fake_gseapy(monkeypatch, cap):
    """A minimal offline gseapy: gp.enrich records its call + returns one enriched term.
    Proves run_enrichment goes through the OFFLINE local-GMT path (never the web API)."""
    import sys

    class _Res:
        def __init__(self, rows): self._rows = rows
        def sort_values(self, _k): return self
        def head(self, n): return _Res(self._rows[:n])
        def iterrows(self):
            for i, r in enumerate(self._rows): yield i, r

    def _enrich(gene_list, gene_sets, background=None, outdir=None, verbose=False):
        cap.update(gene_sets=gene_sets, background=background, n_genes=len(gene_list))
        return types.SimpleNamespace(results=_Res([
            {"Term": "regulation of synaptic signaling", "Gene_set": "GO_Biological_Process_2023",
             "Adjusted P-value": 0.002, "Combined Score": 14.5, "Overlap": "4/60"}]))

    fake = types.ModuleType("gseapy")
    fake.enrich = _enrich
    fake.enrichr = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the Enrichr web API"))
    monkeypatch.setitem(sys.modules, "gseapy", fake)


def test_enrichment_runs_offline_against_local_gmt(tmp_path, monkeypatch):
    # A local GMT + a DE table -> gp.enrich is called with the LOCAL .gmt path (no network).
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "GO_Biological_Process_2023.gmt").write_text("term\tdesc\tGRIA4\tDLGAP1\n", encoding="utf-8")
    monkeypatch.setenv("BIOAGENT_GENESETS_DIR", str(gdir))
    tables = tmp_path / "artifacts" / "tables"
    tables.mkdir(parents=True)
    (tables / "de_leiden_all.csv").write_text(
        "group,gene,log2fc,pval,pval_adj,score\n0,GRIA4,6.5,1e-50,1e-48,20\n0,DLGAP1,6.7,1e-40,1e-38,18\n",
        encoding="utf-8")
    cap: dict = {}
    _install_fake_gseapy(monkeypatch, cap)

    out = scrna_pack.run_enrichment(
        {"gene_sets": ["GO_Biological_Process_2023"], "background": 18000}, _ctx(tmp_path))

    assert out["status"] == "ok"
    assert out["gene_sets"] == ["GO_Biological_Process_2023"]
    assert out["top_terms_by_group"]["0"] == ["regulation of synaptic signaling"]
    assert cap["gene_sets"] == [str(gdir / "GO_Biological_Process_2023.gmt")]   # local file, not a URL
    assert cap["background"] == 18000


def test_enrichment_uses_annotated_de_table_and_runs_per_class(tmp_path, monkeypatch):
    # Regression: run_enrichment hard-coded de_leiden_all.csv, so when DE ran on `majorclass`
    # it missed the table and pooled everything into one "input" group. It must now discover the
    # annotated DE table and run ORA PER CLASS.
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "GO_Biological_Process_2023.gmt").write_text("term\tdesc\tRHO\tPDE6A\n", encoding="utf-8")
    monkeypatch.setenv("BIOAGENT_GENESETS_DIR", str(gdir))
    tables = tmp_path / "artifacts" / "tables"
    tables.mkdir(parents=True)
    # No de_leiden_all.csv — only the annotated majorclass DE table (two classes).
    (tables / "de_majorclass_all.csv").write_text(
        "group,gene,log2fc,pval,pval_adj,score\n"
        "Rod,RHO,6.5,1e-50,1e-48,80\nRod,PDE6A,6.0,1e-45,1e-43,79\n"
        "AC,GRIA4,7.0,1e-40,1e-38,60\nAC,DLGAP1,6.8,1e-38,1e-36,59\n",
        encoding="utf-8")
    cap: dict = {}
    _install_fake_gseapy(monkeypatch, cap)

    out = scrna_pack.run_enrichment({"gene_sets": ["GO_Biological_Process_2023"]}, _ctx(tmp_path))

    assert out["status"] == "ok"
    assert set(out["groups"]) == {"Rod", "AC"}          # per-class, not a single pooled "input"
    assert "input" not in out["groups"]
    assert (tables / "enrichment_Rod.csv").exists() and (tables / "enrichment_AC.csv").exists()


def test_enrichment_background_is_the_tested_universe_not_a_round_number(tmp_path, monkeypatch):
    # The ORA p-value is a hypergeometric tail against the background, so the background is a
    # statistical parameter. When run_de has written the tested universe, THAT must be what
    # gseapy sees — not the old constant 20000, which over-states the universe and inflates
    # every term whenever QC/HVG filtering left far fewer genes in the object.
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "GO_Biological_Process_2023.gmt").write_text("term\tdesc\tRHO\tPDE6A\n", encoding="utf-8")
    monkeypatch.setenv("BIOAGENT_GENESETS_DIR", str(gdir))
    tables = tmp_path / "artifacts" / "tables"
    tables.mkdir(parents=True)
    (tables / "de_leiden_all.csv").write_text(
        "group,gene,log2fc,pval,pval_adj,score\n0,RHO,6.5,1e-50,1e-48,20\n", encoding="utf-8")
    (tables / "de_leiden_universe.txt").write_text("RHO\nPDE6A\nGRIA4\n", encoding="utf-8")
    cap: dict = {}
    _install_fake_gseapy(monkeypatch, cap)

    out = scrna_pack.run_enrichment({"gene_sets": ["GO_Biological_Process_2023"]}, _ctx(tmp_path))

    assert out["status"] == "ok"
    assert cap["background"] == ["RHO", "PDE6A", "GRIA4"]     # the real universe, not 20000
    assert out["background_source"] == "tested_universe"
    assert out["background_size"] == 3


def test_enrichment_records_the_constant_fallback_when_no_universe_exists(tmp_path, monkeypatch):
    # No universe file (e.g. a DE table from an older run): the constant is still allowed, but it
    # must be REPORTED as a fallback so a reader never mistakes it for the measured universe.
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "GO_Biological_Process_2023.gmt").write_text("term\tdesc\tRHO\n", encoding="utf-8")
    monkeypatch.setenv("BIOAGENT_GENESETS_DIR", str(gdir))
    tables = tmp_path / "artifacts" / "tables"
    tables.mkdir(parents=True)
    (tables / "de_leiden_all.csv").write_text(
        "group,gene,log2fc,pval,pval_adj,score\n0,RHO,6.5,1e-50,1e-48,20\n", encoding="utf-8")
    cap: dict = {}
    _install_fake_gseapy(monkeypatch, cap)

    out = scrna_pack.run_enrichment({"gene_sets": ["GO_Biological_Process_2023"]}, _ctx(tmp_path))

    assert out["background_source"] == "constant_fallback"
    assert out["background_size"] == 20000 and cap["background"] == 20000


def test_group_labels_with_a_slash_do_not_lose_their_table(tmp_path, monkeypatch):
    # "Club/Secretory" and "Smooth muscle/Pericyte" are ordinary cell-type labels. A raw '/' in
    # the filename reads as a directory, so the write raised and that class's table silently
    # never appeared. The label stays verbatim inside the table; only the filename is slugged.
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "GO_Biological_Process_2023.gmt").write_text("term\tdesc\tSCGB1A1\n", encoding="utf-8")
    monkeypatch.setenv("BIOAGENT_GENESETS_DIR", str(gdir))
    tables = tmp_path / "artifacts" / "tables"
    tables.mkdir(parents=True)
    (tables / "de_cell_type_all.csv").write_text(
        "group,gene,log2fc,pval,pval_adj,score\nClub/Secretory,SCGB1A1,6.5,1e-50,1e-48,20\n",
        encoding="utf-8")
    _install_fake_gseapy(monkeypatch, {})

    out = scrna_pack.run_enrichment({"gene_sets": ["GO_Biological_Process_2023"]}, _ctx(tmp_path))

    assert out["status"] == "ok" and out["groups"] == ["Club/Secretory"]
    written = tables / "enrichment_Club_Secretory.csv"
    assert written.exists()
    assert "Club/Secretory" in written.read_text(encoding="utf-8")   # label preserved in the data


def _install_fake_prerank(monkeypatch, cap):
    """A minimal offline gseapy exposing `prerank`, returning one UP and one DOWN set."""
    import sys

    class _DF:
        columns = ["Term", "ES", "NES", "NOM p-val", "FDR q-val", "Lead_genes"]

        def __init__(self, rows): self._rows = rows
        def iterrows(self):
            for i, r in enumerate(self._rows): yield i, r

    def _prerank(rnk, gene_sets, min_size=15, max_size=500, permutation_num=1000,
                 outdir=None, seed=42, verbose=False, **_kw):
        # Real signature (gseapy 1.2.1): `rnk: Union[DataFrame, Series, str]`. A dict is NOT
        # accepted, so the fake must reject one too — otherwise the test passes on a call the
        # installed library would refuse, which is exactly how this slipped through once.
        assert isinstance(rnk, str), f"gseapy.prerank takes a path/Series, got {type(rnk).__name__}"
        n_ranked = sum(1 for ln in open(rnk, encoding="utf-8") if "\t" in ln)
        cap.setdefault("calls", []).append(
            {"n_ranked": n_ranked, "gene_sets": gene_sets, "seed": seed,
             "permutation_num": permutation_num, "min_size": min_size, "max_size": max_size})
        # Given a LIST of .gmt paths, real gseapy prefixes each Term with the file basename.
        # The fake reproduces that, because unprefixed terms go verbatim into a report.
        lib = "MSigDB_Hallmark_2020.gmt"
        return types.SimpleNamespace(res2d=_DF([
            {"Term": f"{lib}__phototransduction", "ES": 0.7, "NES": 2.1,
             "NOM p-val": 0.001, "FDR q-val": 0.01, "Lead_genes": "RHO;PDE6A"},
            {"Term": f"{lib}__oxidative phosphorylation", "ES": -0.8, "NES": -2.6,
             "NOM p-val": 0.001, "FDR q-val": 0.02, "Lead_genes": "NDUFA1"},
            {"Term": f"{lib}__not significant here", "ES": 0.1, "NES": 0.3,
             "NOM p-val": 0.6, "FDR q-val": 0.9, "Lead_genes": ""},
        ]))

    fake = types.ModuleType("gseapy")
    fake.prerank = _prerank
    fake.enrichr = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the Enrichr web API"))
    monkeypatch.setitem(sys.modules, "gseapy", fake)


def test_prerank_walks_the_whole_ranking_and_keeps_the_nes_sign(tmp_path, monkeypatch):
    # GSEA's whole point is the tail: it ranks EVERY tested gene, and a suppressed programme
    # (negative NES) is as much a finding as an induced one, so it must not be sorted to the
    # bottom or dropped. Terms above the FDR floor are excluded from the returned summary.
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "MSigDB_Hallmark_2020.gmt").write_text("term\tdesc\tRHO\tPDE6A\n", encoding="utf-8")
    monkeypatch.setenv("BIOAGENT_GENESETS_DIR", str(gdir))
    tables = tmp_path / "artifacts" / "tables"
    tables.mkdir(parents=True)
    (tables / "rank_leiden_0.rnk").write_text(
        "".join(f"GENE{i}\t{10 - i * 0.01:.4f}\n" for i in range(500)), encoding="utf-8")
    (tables / "rank_leiden_index.csv").write_text("slug,group\n0,0\n", encoding="utf-8")
    cap: dict = {}
    _install_fake_prerank(monkeypatch, cap)

    out = scrna_pack.run_gsea_prerank({"gene_sets": ["MSigDB_Hallmark_2020"]}, _ctx(tmp_path))

    assert out["status"] == "ok"
    call = cap["calls"][0]
    assert call["n_ranked"] == 500                    # the COMPLETE list, not a top-N slice
    assert call["gene_sets"] == [str(gdir / "MSigDB_Hallmark_2020.gmt")]   # local file, no network
    assert call["seed"] == 42                         # permutation p-values must reproduce
    terms = out["top_terms_by_group"]["0"]
    # ranked by |NES|, so the strongly-DOWN set leads and its direction survives
    assert [t["term"] for t in terms] == ["oxidative phosphorylation", "phototransduction"]
    assert [t["direction"] for t in terms] == ["down", "up"]
    # the library is its own field, not smuggled into the pathway name
    assert {t["gene_set"] for t in terms} == {"MSigDB_Hallmark_2020"}
    assert all(t["fdr"] <= 0.25 for t in terms)       # the FDR>floor term is not reported as a hit
    assert out["ranked_list_size_by_group"]["0"] == 500
    assert out["params"]["ranking_statistic"] == "wilcoxon_z"
    # the full table (including the non-significant term) is still on disk for the reader
    assert "not significant here" in (tables / "gsea_0.csv").read_text(encoding="utf-8")


def test_prerank_needs_run_de_first_and_says_so(tmp_path, monkeypatch):
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "MSigDB_Hallmark_2020.gmt").write_text("term\tdesc\tRHO\n", encoding="utf-8")
    monkeypatch.setenv("BIOAGENT_GENESETS_DIR", str(gdir))
    _install_fake_prerank(monkeypatch, {})
    out = scrna_pack.run_gsea_prerank({"gene_sets": ["MSigDB_Hallmark_2020"]}, _ctx(tmp_path))
    assert out["status"] == "error" and "run_de" in out["error"]


def test_prerank_resolves_the_real_group_label_from_the_slug_index(tmp_path, monkeypatch):
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "MSigDB_Hallmark_2020.gmt").write_text("term\tdesc\tSCGB1A1\n", encoding="utf-8")
    monkeypatch.setenv("BIOAGENT_GENESETS_DIR", str(gdir))
    tables = tmp_path / "artifacts" / "tables"
    tables.mkdir(parents=True)
    (tables / "rank_cell_type_Club_Secretory.rnk").write_text("SCGB1A1\t9.0\n", encoding="utf-8")
    (tables / "rank_cell_type_index.csv").write_text(
        "slug,group\nClub_Secretory,Club/Secretory\n", encoding="utf-8")
    _install_fake_prerank(monkeypatch, {})

    out = scrna_pack.run_gsea_prerank({"gene_sets": ["MSigDB_Hallmark_2020"]}, _ctx(tmp_path))

    assert out["groups"] == ["Club/Secretory"]        # reported as the biologist wrote it
    assert (tables / "gsea_Club_Secretory.csv").exists()


def test_prerank_missing_gseapy_is_graceful(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.split(".")[0] == "gseapy":
            raise ImportError(name="gseapy")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    out = scrna_pack.run_gsea_prerank({}, _ctx(tmp_path))
    assert out["status"] == "dependency_missing" and out["dependency"] == "gseapy"


def test_enrichment_missing_gmt_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOAGENT_GENESETS_DIR", str(tmp_path / "empty"))   # no .gmt files
    (tmp_path / "empty").mkdir()
    _install_fake_gseapy(monkeypatch, {})
    out = scrna_pack.run_enrichment({"gene_sets": ["GO_Biological_Process_2023"]}, _ctx(tmp_path))
    assert out["status"] == "error"
    assert out["missing_libraries"] == ["GO_Biological_Process_2023"]
    assert "fetch_genesets" in out["error"]


def test_pipeline_order_is_enforced(tmp_path, monkeypatch):
    # With scanpy "available" (import succeeds enough to reach the checkpoint guard),
    # calling clustering/DE before their prerequisite returns a clear ordering error,
    # not a crash. We stub _import_scanpy so we don't need the real package.
    monkeypatch.setattr(scrna_pack, "_import_scanpy", lambda: types.SimpleNamespace())
    ctx = _ctx(tmp_path)
    assert scrna_pack.run_clustering({}, ctx)["error"].startswith("run_scanpy_qc must run first")
    assert scrna_pack.run_de({}, ctx)["error"].startswith("run_clustering must run first")


def test_qc_without_dataset_errors_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(scrna_pack, "_import_scanpy", lambda: types.SimpleNamespace())
    out = scrna_pack.run_scanpy_qc({}, _ctx(tmp_path))   # no dataset_path
    assert out["status"] == "error" and "no dataset" in out["error"]


def test_summary_renders_from_step_results():
    md = scrna_pack.scrna_analysis_summary({
        "qc": {"cells_before": 2700, "cells_after": 2600, "genes_before": 32738,
               "genes_after": 13714, "n_hvg": 2000},
        "clustering": {"n_clusters": 8},
        "de": {"n_groups": 8, "groupby": "leiden"},
        "enrichment": {"gene_sets": ["GO_Biological_Process_2021"], "groups": ["0", "1"]},
    })
    assert "Single-cell analysis pipeline" in md
    assert "2700→2600 cells" in md and "8 Leiden clusters" in md
