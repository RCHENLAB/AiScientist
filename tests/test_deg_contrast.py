"""The DEG line: a condition-vs-reference contrast, stratified by an existing cell-type column.

These run REAL scanpy on a small synthetic AnnData (no mocks) because the defects they cover were
all interface defects — the shape of what one tool writes vs what the next tool looks for — and a
mocked tool cannot show that. Skipped when the analysis extra is not installed.

What is pinned here, defect by defect:
  * ``run_de`` reads ``adata_qc.h5ad`` when there is no clustering checkpoint — the DEG protocol
    tells the planner to REUSE existing labels and SKIP clustering, and the old hard requirement
    on ``adata_clustered.h5ad`` turned that documented path into an error.
  * ``run_de`` can compare condition vs a NAMED reference (not just one-vs-rest), stratified per
    cell type, so the contrast no longer has to be hand-written in ``run_code``.
  * what ``run_de`` writes is what ``run_enrichment`` discovers (``de_<key>_all.csv``), and the
    contrast's DOWN-regulated half survives into enrichment instead of being ranked away.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("scanpy")
pytest.importorskip("anndata")

import numpy as np  # noqa: E402
import anndata as ad  # noqa: E402
import scanpy as sc  # noqa: E402

from aiscientist.tools.run_de import tool as run_de_tool  # noqa: E402
from aiscientist.tools.run_enrichment import tool as run_enrichment_tool


N_GENES = 200
UP_IN_KO = ["GENE_0", "GENE_1", "GENE_2", "GENE_3", "GENE_4", "GENE_5"]
DOWN_IN_KO = ["GENE_6", "GENE_7", "GENE_8", "GENE_9", "GENE_10", "GENE_11"]


def _synthetic_qc_checkpoint(work: Path, *, n_per_arm: int = 60) -> None:
    """An ``adata_qc.h5ad`` shaped exactly like ``run_scanpy_qc`` leaves it: log-normalized X,
    ``.raw`` holding the same matrix, plus a 2-level condition column and a cell-type column.

    The signal is deliberately planted in BOTH directions so a direction-blind selection rule is
    visibly wrong rather than merely suboptimal."""
    rng = np.random.default_rng(0)
    cells, conditions, celltypes = [], [], []
    for ct in ("Alpha", "Beta"):
        for cond in ("KO", "WT"):
            for _ in range(n_per_arm):
                x = rng.poisson(5.0, N_GENES).astype(np.float32)
                if cond == "KO":
                    for g in UP_IN_KO:
                        x[int(g.split("_")[1])] += rng.poisson(40.0)
                    for g in DOWN_IN_KO:
                        x[int(g.split("_")[1])] = rng.poisson(0.2)
                cells.append(x)
                conditions.append(cond)
                celltypes.append(ct)

    adata = ad.AnnData(np.vstack(cells))
    adata.var_names = [f"GENE_{i}" for i in range(N_GENES)]
    adata.obs_names = [f"cell_{i}" for i in range(adata.n_obs)]
    adata.obs["condition"] = conditions
    adata.obs["celltype"] = celltypes
    adata.obs["condition"] = adata.obs["condition"].astype("category")
    adata.obs["celltype"] = adata.obs["celltype"].astype("category")
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    adata.raw = adata          # what run_scanpy_qc does; rank_genes_groups(use_raw=True) needs it
    work.mkdir(parents=True, exist_ok=True)
    adata.write(work / "adata_qc.h5ad")


@pytest.fixture()
def ctx(tmp_path: Path) -> SimpleNamespace:
    _synthetic_qc_checkpoint(tmp_path / "work")
    return SimpleNamespace(workspace=tmp_path, decisions={})


def _rows(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --- run_de -------------------------------------------------------------------


def test_contrast_runs_off_the_qc_checkpoint_without_clustering(ctx):
    """No adata_clustered.h5ad exists — the labeled-dataset path the DEG protocol prescribes."""
    assert not (Path(ctx.workspace) / "work" / "adata_clustered.h5ad").exists()

    out = run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "celltype", "min_cells": 10},
        ctx)

    assert out["status"] == "ok", out
    assert out["reference"] == "WT"
    assert out["stratify_by"] == "celltype"
    assert out["table_key"] == "celltype"
    assert set(out["de_rows_by_group"]) == {"Alpha", "Beta"}
    assert "KO" in out["comparison"] and "WT" in out["comparison"]


def test_contrast_keeps_both_directions_in_the_combined_table(ctx):
    out = run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "celltype", "min_cells": 10},
        ctx)

    combined = Path(ctx.workspace) / "artifacts" / "tables" / "de_celltype_all.csv"
    assert combined.exists(), "run_enrichment discovers de_<key>_all.csv — it must be written"
    rows = _rows(combined)
    assert {r["group"] for r in rows} == {"Alpha", "Beta"}

    for group in ("Alpha", "Beta"):
        lfcs = [float(r["log2fc"]) for r in rows if r["group"] == group]
        assert any(v > 0 for v in lfcs), f"{group}: no up-regulated gene survived"
        assert any(v < 0 for v in lfcs), f"{group}: no DOWN-regulated gene survived"
        genes = {r["gene"] for r in rows if r["group"] == group}
        assert genes & set(UP_IN_KO), f"{group}: planted up-regulated genes missing"
        assert genes & set(DOWN_IN_KO), f"{group}: planted down-regulated genes missing"

    assert out["n_significant_total"] > 0
    for counts in out["significant_by_group"].values():
        assert counts["up"] > 0 and counts["down"] > 0


def test_contrast_writes_universe_and_rank_files_under_the_stratified_key(ctx):
    out = run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "celltype", "min_cells": 10},
        ctx)
    tables = Path(ctx.workspace) / "artifacts" / "tables"

    # ORA's background and preranked GSEA both key off these; a missing universe silently
    # downgrades every enrichment p-value to the 20000-gene constant.
    assert (tables / "de_celltype_universe.txt").exists()
    assert out["tested_universe_size"] > 0
    for group in ("Alpha", "Beta"):
        assert (tables / f"rank_celltype_{group}.rnk").exists()
        assert (tables / f"de_celltype_{group}.csv").exists()


def test_a_cell_type_without_enough_cells_is_reported_not_silently_dropped(ctx):
    out = run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "celltype",
         "min_cells": 1000},          # nothing can clear this
        ctx)

    assert out["status"] == "ok"
    assert {s["group"] for s in out["skipped_groups"]} == {"Alpha", "Beta"}
    assert out["de_rows_total"] == 0


def test_contrast_names_its_arms_and_counts_every_stratum(ctx):
    """What a write-up tabulates as "cells per arm", straight from the tool — and by name, so
    n_condition / n_reference cannot be read the wrong way round (run 8847d521ba32 did)."""
    tested = run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "celltype", "min_cells": 10},
        ctx)
    skipped = run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "celltype", "min_cells": 1000},
        ctx)

    every = {"Alpha": {"KO": 60, "WT": 60}, "Beta": {"KO": 60, "WT": 60}}
    assert tested["condition"] == "KO" and tested["cells_by_group_and_arm"] == every
    # A stratum refused for too few cells is still counted, consistently with its skip record.
    assert skipped["cells_by_group_and_arm"] == every
    for s in skipped["skipped_groups"]:
        per = skipped["cells_by_group_and_arm"][s["group"]]
        assert (per["KO"], per["WT"]) == (s["n_condition"], s["n_reference"])
    # Last. The shortened views keep the warnings wherever they sit and drop data from the end, so
    # at the end this table goes before the per-stratum counts do; the count check reads it from
    # the full result.
    assert list(tested)[-1] == "cells_by_group_and_arm"


def test_markers_path_is_unchanged(ctx):
    """One-vs-rest markers — the historical behaviour, keyed by groupby, capped at n_genes."""
    out = run_de_tool.run_de({"groupby": "celltype", "n_genes": 20}, ctx)

    assert out["status"] == "ok"
    assert out["reference"] == "rest"
    assert out["stratify_by"] is None
    assert out["table_key"] == "celltype"
    assert all(n == 20 for n in out["de_rows_by_group"].values())
    assert (Path(ctx.workspace) / "artifacts" / "tables" / "de_celltype_all.csv").exists()
    assert (Path(ctx.workspace) / "work" / "adata_de.h5ad").exists()
    # Every returned evidence path must exist: scanpy names the rank figure after the column it
    # grouped by, and the hardcoded "leiden" pointed the Critic at a nonexistent file whenever
    # the DE was run on anything else.
    art = Path(ctx.workspace) / "artifacts"
    assert out["figures"], "the rank figure is the step's evidence — it must be returned"
    for rel in out["figures"]:
        assert (art / rel).exists(), rel


def test_a_stratified_run_does_not_clobber_an_existing_marker_checkpoint(ctx):
    """Annotation skills read rank_genes_groups out of adata_de.h5ad; a stratified contrast has
    no global result to put there, so it must not overwrite one."""
    run_de_tool.run_de({"groupby": "celltype", "n_genes": 10}, ctx)
    marker_ckpt = Path(ctx.workspace) / "work" / "adata_de.h5ad"
    before = marker_ckpt.read_bytes()

    run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "celltype", "min_cells": 10},
        ctx)

    assert marker_ckpt.read_bytes() == before
    assert "rank_genes_groups" in sc.read_h5ad(marker_ckpt).uns


# --- argument validation ------------------------------------------------------


def test_unknown_reference_level_is_an_error_not_a_silent_one_vs_rest(ctx):
    out = run_de_tool.run_de({"groupby": "condition", "reference": "control"}, ctx)
    assert out["status"] == "error"
    assert "not a level" in out["error"]


def test_stratify_without_reference_is_refused(ctx):
    """Silently running one-vs-rest inside each cell type would look like a contrast and is not."""
    out = run_de_tool.run_de({"groupby": "condition", "stratify_by": "celltype"}, ctx)
    assert out["status"] == "error"
    assert "reference" in out["error"]


def test_missing_qc_checkpoint_names_the_step_that_writes_it(tmp_path):
    out = run_de_tool.run_de({}, SimpleNamespace(workspace=tmp_path, decisions={}))
    assert out["status"] == "error"
    assert "run_scanpy_qc" in out["error"]


# --- run_de -> run_enrichment hand-off ----------------------------------------


def test_enrichment_finds_the_contrast_table_and_splits_by_direction(ctx, tmp_path,
                                                                     monkeypatch):
    pytest.importorskip("gseapy")
    run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "celltype", "min_cells": 10},
        ctx)

    # A tiny local GMT: offline ORA resolves library names to .gmt files in this dir.
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "TestSets.gmt").write_text(
        "UP_PROGRAMME\tna\t" + "\t".join(UP_IN_KO) + "\n"
        "DOWN_PROGRAMME\tna\t" + "\t".join(DOWN_IN_KO) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("AISCIENTIST_GENESETS_DIR", str(gdir))

    out = run_enrichment_tool.run_enrichment({"gene_sets": ["TestSets"], "top_n_terms": 5}, ctx)

    assert out["status"] == "ok", out
    assert out["split_by_direction"] is True
    assert {"Alpha (up)", "Alpha (down)", "Beta (up)", "Beta (down)"} <= set(out["groups"])
    # The whole point: the DOWN half reaches ORA and recovers the down-regulated programme,
    # which a pooled or score-ordered selection would never surface.
    assert "DOWN_PROGRAMME" in out["top_terms_by_group"]["Alpha (down)"]
    assert "UP_PROGRAMME" in out["top_terms_by_group"]["Alpha (up)"]
    # Background must be the tested universe run_de wrote, not the 20000 constant.
    assert out["background_source"] == "tested_universe"
    assert out["background_size"] == N_GENES


def test_pseudobulk_writes_the_table_enrichment_discovers(tmp_path):
    """The DEG skill routes a replicated contrast to run_pseudobulk_de. Its result was written
    ONLY as pseudobulk_all.csv, which run_enrichment does not look for — so the RECOMMENDED path
    silently lost per-cell-type enrichment and its ORA background."""
    pytest.importorskip("scipy")
    from aiscientist.tools.run_pseudobulk_de import tool as run_pseudobulk_de_tool

    work = tmp_path / "work"
    _synthetic_qc_checkpoint(work, n_per_arm=40)
    # Pseudobulk needs raw counts and >=2 samples per arm.
    adata = sc.read_h5ad(work / "adata_qc.h5ad")
    adata.layers["counts"] = adata.raw.X.copy()
    donors = [f"{c}_{i % 2}" for i, c in enumerate(adata.obs["condition"].astype(str))]
    adata.obs["donor"] = donors
    adata.write(work / "adata_qc.h5ad")
    ctx = SimpleNamespace(workspace=tmp_path, decisions={})

    out = run_pseudobulk_de_tool.run_pseudobulk_de(
        {"sample_key": "donor", "condition_key": "condition", "group_key": "celltype",
         "min_cells_per_sample": 5},
        ctx)

    assert out["status"] == "ok", out
    tables = tmp_path / "artifacts" / "tables"
    assert (tables / "de_celltype_all.csv").exists(), "run_enrichment discovers de_<key>_all.csv"
    assert (tables / "de_celltype_universe.txt").exists(), "ORA background must be the real universe"
    assert (tables / "rank_celltype_Alpha.rnk").exists(), "run_gsea_prerank needs a ranked list"
    # Schema-compatible with run_de's table, so enrichment needs no special case.
    header = (tables / "de_celltype_all.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == ["group", "gene", "log2fc", "pval", "pval_adj", "score"]


def test_enrichment_reports_no_significant_genes_instead_of_asking_for_a_gene_list(ctx,
                                                                                  monkeypatch,
                                                                                  tmp_path):
    pytest.importorskip("gseapy")
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "TestSets.gmt").write_text("UP_PROGRAMME\tna\t" + "\t".join(UP_IN_KO) + "\n",
                                       encoding="utf-8")
    monkeypatch.setenv("AISCIENTIST_GENESETS_DIR", str(gdir))
    # A contrast that finds nothing: no cell type clears min_cells, so the combined table is empty.
    run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "celltype", "min_cells": 1000},
        ctx)

    out = run_enrichment_tool.run_enrichment({"gene_sets": ["TestSets"]}, ctx)

    assert out["status"] == "error"
    assert "do not substitute a gene list" in out["error"]


def test_a_stub_gmt_is_refused_not_silently_enriched_against(ctx, monkeypatch, tmp_path):
    """Production shipped a 20-byte GO_Biological_Process_2023.gmt holding one fake line. It
    passed the old `p.exists()` check, so every GO enrichment ran against a single made-up gene
    set and reported "no enriched terms" — which reads as a finding about the data, not a missing
    library. A degenerate library must be called out, never quietly used."""
    pytest.importorskip("gseapy")
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "GO_Biological_Process_2023.gmt").write_text("term\tdesc\tRHO\tPDE6A\n", encoding="utf-8")
    monkeypatch.setenv("AISCIENTIST_GENESETS_DIR", str(gdir))
    run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "celltype", "min_cells": 10},
        ctx)

    out = run_enrichment_tool.run_enrichment({"gene_sets": ["GO_Biological_Process_2023"]}, ctx)

    assert out["status"] == "error"
    assert "GO_Biological_Process_2023" in out["unusable_libraries"]
    assert "fetch_genesets" in out["error"]
    # And it must NOT be reported as merely "missing" — the file is there; it is unusable.
    assert out["missing_libraries"] == []


def test_a_real_library_reports_its_term_count(ctx, monkeypatch, tmp_path):
    """'No enriched terms' against a 5,000-term library and against a stub are different claims."""
    pytest.importorskip("gseapy")
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    lines = [f"SET_{i}\tna\t" + "\t".join(UP_IN_KO) for i in range(20)]
    (gdir / "TestSets.gmt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("AISCIENTIST_GENESETS_DIR", str(gdir))
    run_de_tool.run_de(
        {"groupby": "condition", "reference": "WT", "stratify_by": "celltype", "min_cells": 10},
        ctx)

    out = run_enrichment_tool.run_enrichment({"gene_sets": ["TestSets"]}, ctx)

    assert out["status"] == "ok"
    assert out["gene_set_terms"]["TestSets"] == 20
    assert out["unusable_libraries"] == {}
