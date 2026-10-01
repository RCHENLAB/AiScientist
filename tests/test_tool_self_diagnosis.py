"""A tool must report the defects in its OWN output.

The failure class that matters in this system is the one where the tool SUCCEEDS: every
system-level contract holds, every number is present, and the science is still wrong. Raw counts do
not catch it, because catching it requires *noticing* an anomaly — and a reviewer given only the
result cannot see what should have been there.

The worst example in the single-cell line, and the one these tests are built around: mitochondrial
genes are matched by NAME (`MT-` prefix). Ensembl IDs, a different naming convention, or an upstream
filter that already removed them all yield ZERO matches. Then `pct_counts_mt` is 0.0 for every cell,
`pct_counts_mt < max_pct_mt` removes NOTHING, and the result still reports `max_pct_mt` as though a
mitochondrial filter had been applied — so the manuscript states that high-mito cells were removed,
which is false. Nothing in the old return value could contradict it.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("scanpy")

import anndata as ad  # noqa: E402
import numpy as np  # noqa: E402

from bioagent.tools.run_clustering import tool as run_clustering_tool  # noqa: E402
from bioagent.tools.run_de import tool as run_de_tool
from bioagent.tools.run_scanpy_qc import tool as run_scanpy_qc_tool


def _dataset(tmp_path, gene_names, *, n_cells=200, mito_frac=0.0):
    """A small AnnData written to disk, with `mito_frac` of each cell's counts in the FIRST 5
    genes (which are mitochondrial only if `gene_names` says so)."""
    rng = np.random.default_rng(0)
    x = rng.poisson(4.0, (n_cells, len(gene_names))).astype(np.float32) + 1.0
    if mito_frac:
        x[:, :5] *= (mito_frac / (1 - mito_frac)) * (x.sum(axis=1, keepdims=True) / x[:, :5].sum(
            axis=1, keepdims=True))
    adata = ad.AnnData(x)
    adata.var_names = list(gene_names)
    adata.obs_names = [f"c{i}" for i in range(n_cells)]
    p = tmp_path / "ds.h5ad"
    adata.write(p)
    return p


def _ctx(tmp_path, dataset):
    from types import SimpleNamespace
    return SimpleNamespace(workspace=tmp_path, decisions={"dataset_path": str(dataset)})


SYMBOLS = ["MT-CO1", "MT-ND1", "MT-CYB", "MT-ATP6", "MT-CO2"] + [f"GENE{i}" for i in range(95)]
ENSEMBL = [f"ENSG{i:08d}" for i in range(100)]


def test_zero_matched_mito_genes_is_reported_as_a_no_op_filter(tmp_path):
    """The headline defect: the filter silently did nothing, and the result used to look identical
    to a run where it worked."""
    out = run_scanpy_qc_tool.run_scanpy_qc(
        {"min_genes": 1, "min_cells": 1, "max_pct_mt": 20.0},
        _ctx(tmp_path, _dataset(tmp_path, ENSEMBL)))

    assert out["status"] == "ok"
    assert out["n_mt_genes"] == 0
    assert out["mt_filter_effective"] is False
    joined = " ".join(out["warnings"])
    assert "NO mitochondrial genes matched" in joined
    assert "removed NOTHING" in joined
    assert "Do NOT state that" in joined, "the warning must forbid the false claim, not just note it"


def test_a_dataset_with_real_mito_genes_reports_the_filter_as_effective(tmp_path):
    out = run_scanpy_qc_tool.run_scanpy_qc(
        {"min_genes": 1, "min_cells": 1, "max_pct_mt": 90.0},
        _ctx(tmp_path, _dataset(tmp_path, SYMBOLS)))

    assert out["n_mt_genes"] == 5
    assert out["mt_filter_effective"] is True
    assert not [w for w in out["warnings"] if "mitochondrial genes matched" in w]


# --- the mitochondrial rule is a parameter, and the warning names the fix -------------------------


def _ensembl_keyed(tmp_path, *, n_cells=200, mito_rich=100):
    """Gene names are Ensembl IDs, symbols live in var['feature_name'] (how cellxgene ships
    files), and the first `mito_rich` cells carry ~40% of their counts in the 5 mito genes."""
    rng = np.random.default_rng(1)
    x = rng.poisson(4.0, (n_cells, len(ENSEMBL))).astype(np.float32) + 1.0
    x[:mito_rich, :5] += 60.0
    adata = ad.AnnData(x)
    adata.var_names = ENSEMBL
    adata.var["feature_name"] = SYMBOLS
    adata.obs_names = [f"c{i}" for i in range(n_cells)]
    p = tmp_path / "ens.h5ad"
    adata.write(p)
    return p


def test_ensembl_keyed_data_gets_a_warning_that_names_the_symbol_column(tmp_path):
    out = run_scanpy_qc_tool.run_scanpy_qc({"min_genes": 1, "min_cells": 1, "max_pct_mt": 20.0},
                                   _ctx(tmp_path, _ensembl_keyed(tmp_path)))

    assert out["n_mt_genes"] == 0 and out["cells_after"] == 200     # nothing was filtered
    joined = " ".join(out["warnings"])
    assert "'feature_name' (5 genes)" in joined and "gene_symbols_key" in joined


def test_gene_symbols_key_makes_the_filter_work_on_ensembl_keyed_data(tmp_path):
    out = run_scanpy_qc_tool.run_scanpy_qc(
        {"min_genes": 1, "min_cells": 1, "max_pct_mt": 20.0, "gene_symbols_key": "feature_name"},
        _ctx(tmp_path, _ensembl_keyed(tmp_path)))

    assert out["n_mt_genes"] == 5 and out["mt_filter_effective"] is True
    assert out["cells_after"] == 100, "the 100 mitochondria-rich cells are the ones removed"
    assert "var['feature_name']" in out["mito_rule"]


def test_another_naming_convention_needs_only_the_prefix(tmp_path):
    fly = ["mt:CoI", "mt:ND1", "mt:Cyt-b", "mt:ATPase6", "mt:CoII"] + [f"CG{i}" for i in range(95)]
    ctx = _ctx(tmp_path, _dataset(tmp_path, fly))
    base = {"min_genes": 1, "min_cells": 1, "max_pct_mt": 90.0}

    assert run_scanpy_qc_tool.run_scanpy_qc(base, ctx)["n_mt_genes"] == 0
    assert run_scanpy_qc_tool.run_scanpy_qc({**base, "mito_prefix": "mt:"}, ctx)["n_mt_genes"] == 5


def test_an_unknown_symbol_column_is_an_error_that_names_the_real_ones(tmp_path):
    out = run_scanpy_qc_tool.run_scanpy_qc(
        {"min_genes": 1, "min_cells": 1, "gene_symbols_key": "gene_name"},
        _ctx(tmp_path, _ensembl_keyed(tmp_path)))

    assert out["status"] == "error" and "feature_name" in out["error"]


def test_heavy_cell_loss_is_flagged(tmp_path):
    """A reader seeing "cells_after: 40" out of 200 has to notice the loss. The tool says it."""
    # Half the cells get a huge mito fraction, so a 20% cutoff removes exactly those.
    ds = _dataset(tmp_path, SYMBOLS, n_cells=200)
    import anndata as _ad
    a = _ad.read_h5ad(ds)
    a.X[:120, :5] = a.X[:120, :5] * 200.0          # 60% of cells become mito-dominated
    a.write(ds)

    out = run_scanpy_qc_tool.run_scanpy_qc({"min_genes": 1, "min_cells": 1, "max_pct_mt": 20.0},
                                   _ctx(tmp_path, ds))

    assert out["status"] == "ok"
    assert out["pct_cells_removed"] >= 50.0
    assert any("QC removed" in w for w in out["warnings"])


def test_thresholds_that_empty_the_object_fail_with_the_numbers_that_caused_it(tmp_path):
    """Left to scanpy this dies inside pandas with "Cannot cut empty array", which names neither
    the threshold nor the tool — and aborts before any diagnosis can be reported."""
    out = run_scanpy_qc_tool.run_scanpy_qc(
        {"min_genes": 99999, "min_cells": 1, "max_pct_mt": 100.0},
        _ctx(tmp_path, _dataset(tmp_path, SYMBOLS)))

    assert out["status"] == "error"
    assert "QC removed everything" in out["error"]
    assert out["cells_before"] == 200 and out["cells_after"] == 0
    assert out["thresholds"]["min_genes"] == 99999, "the offending threshold must be in the result"


def test_a_clamped_hvg_request_is_disclosed(tmp_path):
    out = run_scanpy_qc_tool.run_scanpy_qc(
        {"min_genes": 1, "min_cells": 1, "max_pct_mt": 100.0, "n_top_genes": 5000},
        _ctx(tmp_path, _dataset(tmp_path, SYMBOLS)))

    assert any("HVG request" in w and "reduced to" in w for w in out["warnings"])


def test_a_healthy_run_carries_no_warnings(tmp_path):
    """The signal is only useful if it stays quiet when nothing is wrong."""
    out = run_scanpy_qc_tool.run_scanpy_qc(
        {"min_genes": 1, "min_cells": 1, "max_pct_mt": 100.0, "n_top_genes": 50},
        _ctx(tmp_path, _dataset(tmp_path, SYMBOLS)))

    # Permissive thresholds on clean data remove nothing, which since the run-97dfc89dc5aa
    # fix legitimately raises the pre-filtered/no-op notice — the only acceptable warning
    # for a healthy run on already-clean data.
    assert [w for w in out["warnings"] if "QC REMOVED NOTHING" not in w] == []


# --- the warnings must reach the reviewer ---------------------------------------


def test_the_critic_sees_tool_warnings_first(tmp_path):
    from bioagent.agents.research_harness import HarnessContext, HarnessResult
    from bioagent.agents.research_lab import LabConfig, ResearchLab

    seen: list = []

    class _Stub:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

    lab = ResearchLab(
        HarnessContext(decisions={}, workspace=tmp_path), LabConfig(auto_select_skill=False),
        complete_fn=lambda m: (seen.append(m), json.dumps(
            {"verdict": "accept", "score": 0.9, "critique": "ok"}))[1],
        scientist=_Stub())
    events: list = []

    lab._critic("q", "QC the cells", HarnessResult(
        status="ok", stop_reason=None, final_answer="filtered",
        steps=[{"tool": "run_scanpy_qc", "ok": True,
                "result": {"status": "ok", "cells_after": 100,
                           "warnings": ["NO mitochondrial genes matched the 'MT-' name prefix"]}}],
        errors=[]), events.append)

    payload = json.loads(seen[0][1]["content"])
    keys = list(payload)
    assert "TOOL_SELF_REPORTED_PROBLEMS" in payload
    assert keys.index("TOOL_SELF_REPORTED_PROBLEMS") < keys.index("tool_results"), \
        "a warning read after a wall of successful numbers is a warning nobody acts on"
    assert payload["TOOL_SELF_REPORTED_PROBLEMS"] == [
        "run_scanpy_qc: NO mitochondrial genes matched the 'MT-' name prefix"]
    assert "unsupported" in payload["tool_warning_note"]
    assert any(e.get("type") == "tool_warnings" for e in events), "and a human must be told too"


def test_a_clean_step_adds_no_warning_block(tmp_path):
    from bioagent.agents.research_harness import HarnessContext, HarnessResult
    from bioagent.agents.research_lab import LabConfig, ResearchLab

    seen: list = []

    class _Stub:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

    lab = ResearchLab(
        HarnessContext(decisions={}, workspace=tmp_path), LabConfig(auto_select_skill=False),
        complete_fn=lambda m: (seen.append(m), json.dumps(
            {"verdict": "accept", "score": 0.9, "critique": "ok"}))[1],
        scientist=_Stub())

    lab._critic("q", "s", HarnessResult(
        status="ok", stop_reason=None, final_answer="done",
        steps=[{"tool": "run_scanpy_qc", "ok": True,
                "result": {"status": "ok", "warnings": []}}], errors=[]), lambda _e: None)

    assert "TOOL_SELF_REPORTED_PROBLEMS" not in json.loads(seen[0][1]["content"])


# --- run_clustering / run_de --------------------------------------------------


def _qc_checkpoint(tmp_path, *, obs_cols=None, n_cells=120, structured=True):
    """An adata_qc.h5ad shaped like run_scanpy_qc leaves it, optionally with extra obs columns."""
    rng = np.random.default_rng(0)
    n_genes = 60
    x = rng.poisson(2.0, (n_cells, n_genes)).astype(np.float32) + 1.0
    if structured:                      # two clearly separated populations
        x[: n_cells // 2, :20] += 25.0
        x[n_cells // 2:, 20:40] += 25.0
    a = ad.AnnData(x)
    a.var_names = [f"G{i}" for i in range(n_genes)]
    a.obs_names = [f"c{i}" for i in range(n_cells)]
    for col, vals in (obs_cols or {}).items():
        a.obs[col] = vals
        a.obs[col] = a.obs[col].astype("category")
    import scanpy as sc
    sc.pp.normalize_total(a, target_sum=1e4)
    sc.pp.log1p(a)
    a.raw = a
    a.layers["counts"] = a.raw.X.copy()
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    a.write(tmp_path / "work" / "adata_qc.h5ad")
    from types import SimpleNamespace
    return SimpleNamespace(workspace=tmp_path, decisions={})


def test_clustering_warns_when_the_data_already_has_cell_type_labels(tmp_path):
    """The recurring failure in this codebase: labels exist, the run clusters de-novo anyway, and
    DE/enrichment then run on numeric leiden IDs that mean nothing biologically."""
    ctx = _qc_checkpoint(tmp_path, obs_cols={"majorclass": ["Rod"] * 60 + ["Cone"] * 60})

    out = run_clustering_tool.run_clustering({"resolution": 1.0}, ctx)

    assert out["status"] == "ok"
    assert out["existing_celltype_columns"] == ["majorclass"]
    assert any("already carries cell-type label" in w for w in out["warnings"])
    assert any("prefer the EXISTING labels" in w for w in out["warnings"])


def test_clustering_without_labels_says_nothing_about_them(tmp_path):
    out = run_clustering_tool.run_clustering({"resolution": 1.0}, _qc_checkpoint(tmp_path))
    assert out["existing_celltype_columns"] == []
    assert not [w for w in out["warnings"] if "cell-type label" in w]


def test_a_degenerate_single_cluster_partition_is_flagged(tmp_path):
    out = run_clustering_tool.run_clustering({"resolution": 0.001}, _qc_checkpoint(tmp_path))
    if out["n_clusters"] <= 1:
        assert any("degenerate" in w for w in out["warnings"])


def test_de_warns_when_a_contrast_finds_nothing(tmp_path):
    """"No significant genes" is a RESULT. The failure mode is presenting the top-ranked genes as
    if they were differentially expressed anyway."""
    ctx = _qc_checkpoint(
        tmp_path, structured=False,
        obs_cols={"condition": ["KO"] * 60 + ["WT"] * 60, "ct": ["A"] * 120})

    out = run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "ct", "min_cells": 10}, ctx)

    assert out["status"] == "ok"
    if out["n_significant_total"] == 0:
        assert any("found NOTHING significant" in w for w in out["warnings"])
        assert any("do NOT present the top-ranked genes" in w.lower() or
                   "do NOT present the top-ranked genes" in w for w in out["warnings"])


def test_de_reports_untested_groups_as_a_coverage_gap(tmp_path):
    ctx = _qc_checkpoint(
        tmp_path, obs_cols={"condition": ["KO"] * 60 + ["WT"] * 60, "ct": ["A"] * 120})

    out = run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "ct", "min_cells": 10000}, ctx)

    assert any("NOT tested" in w and "full coverage" in w for w in out["warnings"])


def test_marker_de_flags_groups_backed_by_very_few_cells(tmp_path):
    """The marker path had no cell-count floor at all — min_cells only guarded the contrast path."""
    ctx = _qc_checkpoint(
        tmp_path, obs_cols={"ct": ["A"] * 114 + ["Rare"] * 6})

    out = run_de_tool.run_de({"groupby": "ct", "n_genes": 10, "min_cells": 30}, ctx)

    assert out["status"] == "ok"
    assert "Rare" in out.get("small_groups", {})
    assert any("fewer than 30 cells" in w for w in out["warnings"])


def test_a_healthy_marker_run_carries_no_warnings(tmp_path):
    ctx = _qc_checkpoint(tmp_path, obs_cols={"ct": ["A"] * 60 + ["B"] * 60})
    out = run_de_tool.run_de({"groupby": "ct", "n_genes": 10, "min_cells": 30}, ctx)
    assert out["warnings"] == []
