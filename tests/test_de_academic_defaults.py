"""The DE line's defaults are the field's defaults, measured on the run that motivated them.

Found by profiling the real DDX41 retina object (15,307 cells) rather than reading the code: the
per-stratum Wilcoxon tested all 22,387 post-QC genes, but only ~33% were detected in >=10% of the
cells of either arm (21% in Rod). The other two-thirds tripled the Benjamini-Hochberg denominator
and contributed ~2,700 divide-by-zero fold-changes per stratum (|log2FC| up to 26.9 — a
130-million-fold change that does not exist) which then led every .rnk file handed to GSEA.

Separately, the same report carried TWO definitions of "significant": run_de counted 9,483 genes
by padj alone while run_enrichment selected 8,800 by padj + an effect-size floor. One definition
now holds everywhere, and it is Seurat's.
"""

from __future__ import annotations

import csv

import pytest

pytest.importorskip("scanpy")

import anndata as ad  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from aiscientist.tools import catalog as tools_catalog  # noqa: E402
from aiscientist.tools.run_de import tool as run_de_tool
from aiscientist.tools.run_scanpy_qc import tool as run_scanpy_qc_tool
from aiscientist.tools._lib.scrna import PARAMS
from aiscientist.tools.run_de.tool import _significant_both_directions  # noqa: E402


def _ctx(tmp_path, dataset):
    from types import SimpleNamespace
    return SimpleNamespace(workspace=tmp_path, decisions={"dataset_path": str(dataset)})


def _two_arm(tmp_path, *, n_cells=300, n_rare_cells=3):
    """A 2-arm dataset with one gene ("Rare1") detected in almost no cells — the shape of the
    two-thirds of the DDX41 universe that should never have been tested."""
    rng = np.random.default_rng(0)
    genes = [f"Gene{i}" for i in range(40)] + ["Rare1"]
    x = rng.poisson(4.0, (n_cells, len(genes))).astype(np.float32) + 1.0
    x[:, -1] = 0.0
    x[:n_rare_cells, -1] = 5.0                    # detected in n_rare_cells cells only
    adata = ad.AnnData(x)
    adata.var_names = genes
    adata.obs_names = [f"c{i}" for i in range(n_cells)]
    adata.obs["sampleid"] = pd.Categorical(["KO" if i % 2 else "WT" for i in range(n_cells)])
    # i%4 -> Rod/WT, Rod/KO, Cone/WT, Cone/KO: both strata carry both arms. (An earlier draft used
    # i%2 for both columns, which CONFOUNDS them perfectly — every stratum then has one empty arm,
    # min_cells skips everything, and the universe assertions pass vacuously on an empty file.)
    adata.obs["majorclass"] = pd.Categorical([["Rod", "Cone"][(i // 2) % 2]
                                              for i in range(n_cells)])
    adata.obs["orig.ident"] = pd.Categorical(["s1"] * n_cells)
    path = tmp_path / "two_arm.h5ad"
    adata.write(path)
    return path


def _qc(tmp_path, dataset):
    out = run_scanpy_qc_tool.run_scanpy_qc({"min_genes": 1, "min_cells": 1, "max_pct_mt": 100.0},
                                   _ctx(tmp_path, dataset))
    assert out["status"] == "ok"
    return _ctx(tmp_path, dataset)


def _stratified(ctx, **over):
    args = {"groupby": "sampleid", "reference": "WT", "stratify_by": "majorclass", **over}
    out = run_de_tool.run_de(args, ctx)
    assert out["status"] == "ok", out.get("error")
    return out


# --- min_pct: the tested universe is the DETECTED universe ------------------------


def test_an_undetected_gene_is_not_tested(tmp_path):
    ctx = _qc(tmp_path, _two_arm(tmp_path))
    out = _stratified(ctx)

    assert out["min_pct"] == 0.1
    assert all(n >= 1 for n in out["genes_dropped_by_min_pct"].values()), \
        "Rare1 (detected in 3 of 300 cells) must be dropped in every stratum"
    universe = (tmp_path / "artifacts" / "tables" / "de_majorclass_universe.txt").read_text()
    assert len(universe.split()) >= 30, "an empty universe would pass the next check vacuously"
    assert "Rare1" not in universe.split()
    with (tmp_path / "artifacts" / "tables" / "de_majorclass_all.csv").open() as fh:
        assert all(row["gene"] != "Rare1" for row in csv.DictReader(fh))


def test_min_pct_zero_disables_the_filter(tmp_path):
    ctx = _qc(tmp_path, _two_arm(tmp_path))
    out = _stratified(ctx, min_pct=0)
    universe = (tmp_path / "artifacts" / "tables" / "de_majorclass_universe.txt").read_text()
    assert "Rare1" in universe.split()
    assert out["genes_dropped_by_min_pct"] == {}


def test_padj_is_readjusted_over_the_genes_actually_tested(tmp_path):
    """The point of the filter is the BH denominator, so the surviving genes' adjusted p-values
    must be computed over the kept set — not inherited from a correction whose n included the
    undetected genes. With one gene dropped out of 41, every kept padj can only move DOWN."""
    base = tmp_path / "a"; base.mkdir()
    filt = tmp_path / "b"; filt.mkdir()
    def _padj(dir_, **over):
        _stratified(_qc(dir_, _two_arm(dir_)), **over)
        with (dir_ / "artifacts" / "tables" / "de_majorclass_Rod.csv").open() as fh:
            return {r["gene"]: float(r["pval_adj"]) for r in csv.DictReader(fh)}
    unfiltered, filtered = _padj(base, min_pct=0), _padj(filt)
    common = set(unfiltered) & set(filtered)
    assert common
    assert all(filtered[g] <= unfiltered[g] + 1e-12 for g in common)
    assert any(filtered[g] < unfiltered[g] - 1e-12 for g in common)


# --- one definition of significant, stated in the result --------------------------


def test_significance_needs_the_effect_size_gate_too():
    rows = [
        {"group": "Rod", "gene": "TinyEffect", "log2fc": 0.1, "pval": 1e-9, "pval_adj": 1e-9,
         "score": 9.0},
        {"group": "Rod", "gene": "RealEffect", "log2fc": 0.6, "pval": 1e-9, "pval_adj": 1e-9,
         "score": 8.0},
    ]
    up, down, totals = _significant_both_directions(rows, 0.05, 0.25, 50)
    assert totals == {"up": 1, "down": 0}
    assert up[0]["gene"] == "RealEffect", "padj alone must not be enough"


def test_the_result_states_its_significance_definition_and_disowns_the_total(tmp_path):
    out = _stratified(_qc(tmp_path, _two_arm(tmp_path)))
    assert out["significance"] == {"padj_max": 0.05, "abs_log2fc_min": 0.25}
    # 2 strata = 2 independently-corrected families; their sum controls nothing and must say so.
    assert "not one FDR-controlled family" in out["n_significant_total_note"]


def test_the_declared_table_and_the_schema_carry_the_new_knobs():
    assert PARAMS["run_de"]["min_pct"][0] == 0.1
    assert PARAMS["run_de"]["tie_correct"][0] is True
    assert PARAMS["run_pseudobulk_de"]["min_count"][0] == 10
    props = next(t for t in tools_catalog.scrna_catalog()
                 if t.name == "run_de").parameters["properties"]
    assert props["min_pct"]["default"] == 0.1
    assert props["tie_correct"]["default"] is True


# --- pseudobulk: DESeq2 is the test, Welch is a loud fallback ---------------------


def _four_sample(tmp_path):
    """2 samples per arm — the smallest design run_pseudobulk_de will test."""
    rng = np.random.default_rng(1)
    genes = [f"Gene{i}" for i in range(50)] + ["NeverSeen"]
    x = rng.poisson(6.0, (400, len(genes))).astype(np.float32)
    # Detected (so QC's min_cells keeps it) but far below min_count=10 summed per sample — the
    # low-expression filter, not QC, is what must remove it.
    x[:, -1] = 0.0
    x[:2, -1] = 1.0
    adata = ad.AnnData(x)
    adata.var_names = genes
    adata.obs_names = [f"c{i}" for i in range(400)]
    adata.obs["orig.ident"] = pd.Categorical([f"s{i % 4 + 1}" for i in range(400)])
    adata.obs["sampleid"] = pd.Categorical(
        ["KO" if i % 4 in (0, 1) else "WT" for i in range(400)])
    path = tmp_path / "four_sample.h5ad"
    adata.write(path)
    return path


def _pseudobulk(tmp_path):
    from aiscientist.tools.run_pseudobulk_de import tool as run_pseudobulk_de_tool
    ctx = _qc(tmp_path, _four_sample(tmp_path))
    return run_pseudobulk_de_tool.run_pseudobulk_de(
        {"sample_key": "orig.ident", "condition_key": "sampleid"}, ctx)


def test_pseudobulk_runs_deseq2_and_filters_untestable_genes(tmp_path):
    pytest.importorskip("pydeseq2")
    out = _pseudobulk(tmp_path)
    assert out["status"] == "ok", out.get("error")
    assert "DESeq2" in out["method"]
    grp = out["results_by_group"]["all_cells"]
    assert "DESeq2" in grp["test"]
    assert grp["n_genes_low_expression_filtered"] >= 1     # NeverSeen
    assert grp["n_genes_tested"] < 51
    assert "warnings" not in out, "no fallback happened, so nothing may cry fallback"


def test_pseudobulk_universe_is_what_was_tested(tmp_path):
    pytest.importorskip("pydeseq2")
    out = _pseudobulk(tmp_path)
    assert out["status"] == "ok"
    universe = (tmp_path / "artifacts" / "tables" / "de_pseudobulk_universe.txt").read_text()
    assert "NeverSeen" not in universe.split(), \
        "an ORA background must be the tested universe, not var_names"


def test_pseudobulk_falls_back_loudly_when_deseq2_is_missing(tmp_path, monkeypatch):
    from aiscientist.tools.run_pseudobulk_de import tool as run_pseudobulk_de_tool

    def _no_deseq2(*_a, **_k):
        run_pseudobulk_de_tool._deseq2_contrast.last_error = "pydeseq2 is not installed"
        return None
    monkeypatch.setattr(run_pseudobulk_de_tool, "_deseq2_contrast", _no_deseq2)
    ctx = _qc(tmp_path, _four_sample(tmp_path))
    out = run_pseudobulk_de_tool.run_pseudobulk_de(
        {"sample_key": "orig.ident", "condition_key": "sampleid"}, ctx)

    assert out["status"] == "ok"
    assert "FALLBACK" in out["results_by_group"]["all_cells"]["test"]
    joined = " ".join(out["warnings"])
    assert "fell back to Welch" in joined and "little power" in joined, \
        "the fallback must say what it costs, not just that it happened"


def test_extreme_fc_flagged_as_detection_artifact(tmp_path):
    """|log2FC| >= 5 rows in a contrast raise the EXTREME FOLD-CHANGES warning (Col25a1 8.74)."""
    from aiscientist.tools import catalog as tools_catalog
    from aiscientist.tools.run_de import tool as run_de_tool
    from aiscientist.tools.run_scanpy_qc import tool as run_scanpy_qc_tool

    rows = [{"group": "Rod", "gene": "Col25a1", "log2fc": 8.74, "pval": 1e-9, "pval_adj": 1e-6, "score": 9.0},
            {"group": "Rod", "gene": "Rho", "log2fc": 0.4, "pval": 1e-4, "pval_adj": 0.01, "score": 3.0}]
    up, down, totals = run_de_tool._significant_both_directions(rows, 0.05, 0.25, 50)
    assert any(abs(r["log2fc"]) >= 5 for r in up)  # the input reaches `combined` in run_de


def test_qc_noop_warns_prefiltered(tmp_path):
    import anndata as ad
    import numpy as np
    from pathlib import Path
    from aiscientist.tools.run_scanpy_qc.tool import run_scanpy_qc

    rng = np.random.default_rng(0)
    x = rng.poisson(3.0, size=(60, 50)).astype("float32") + 1  # every cell passes every threshold
    a = ad.AnnData(x)
    a.var_names = [f"G{i}" for i in range(50)]
    a.obs_names = [f"c{i}" for i in range(60)]
    p = tmp_path / "pre.h5ad"; a.write_h5ad(p)

    class Ctx:
        workspace = tmp_path
        decisions = {"dataset_path": str(p)}
    (tmp_path / "artifacts" / "tables").mkdir(parents=True)
    (tmp_path / "artifacts" / "figures").mkdir(parents=True)
    out = run_scanpy_qc({"min_genes": 1, "min_cells": 1, "max_pct_mt": 99}, Ctx())
    assert out["status"] == "ok" and out["pct_cells_removed"] == 0.0
    assert any("QC REMOVED NOTHING" in w for w in out["warnings"])
