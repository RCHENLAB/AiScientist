"""The ``run_scanpy_qc`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/scrna_pack.py`` by ``scripts/refactor/split_tools.py``: the code is the old
module's text, verbatim, with only the imports rewritten. Helpers that several tools use live in
``aiscientist.tools._lib.scrna``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from .._lib.scrna import (
    _INPUT_SPEC,
    _dirs,
    _import_scanpy,
    _missing,
    _p,
    _rel,
    _run_files,
    _run_rel,
    _schema,
)
from ..sdk import HarnessTool


def _read_anndata(sc: Any, path: Path) -> Any:
    """Read a single-cell dataset into AnnData, dispatching by format. Supports H5AD,
    10x (.h5 or an mtx directory), Loom, and CSV/TSV/text matrices; falls back to
    scanpy's extension auto-detection."""
    p = Path(path)
    ext = p.suffix.lower()
    if p.is_dir():
        return sc.read_10x_mtx(p)                    # a 10x `filtered_feature_bc_matrix/` folder
    if ext == ".h5ad":
        return sc.read_h5ad(p)
    if ext == ".h5":
        return sc.read_10x_h5(str(p))                # 10x CellRanger .h5
    if ext == ".loom":
        return sc.read_loom(p)
    if ext in (".csv", ".tsv", ".txt"):
        delim = "," if ext == ".csv" else "\t"
        return sc.read_text(p, delimiter=delim).transpose()  # text matrices are usually genes×cells
    return sc.read(str(p))                           # let scanpy auto-detect anything else


def _dataset_path(ctx: Any) -> Path | None:
    p = (getattr(ctx, "decisions", None) or {}).get("dataset_path")
    return Path(p) if p else None


def run_scanpy_qc(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Real scanpy QC: compute per-cell metrics, filter cells/genes, normalize +
    log1p + HVG. Writes ``work/adata_qc.h5ad`` and QC figures. Returns pre/post
    cell-gene counts and the QC thresholds used (derived metrics only)."""
    try:
        sc = _import_scanpy()
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, _tables = _dirs(ctx)
    if str(args.get("input") or "").strip():
        found, why = _run_files(ctx, args["input"], (".h5ad",))
        if not found:
            return {"status": "error", "step": "qc", "error": why}
        src, read_from = found[0], _run_rel(ctx, found[0])
    else:
        src = _dataset_path(ctx)
        if src is None or not src.exists():
            return {"status": "error", "error": "no dataset loaded (decisions['dataset_path'] missing or not found)"}
        read_from = f"the bound dataset ({src.name})"

    min_genes = int(_p("run_scanpy_qc", "min_genes", args))
    min_cells = int(_p("run_scanpy_qc", "min_cells", args))
    max_pct_mt = float(_p("run_scanpy_qc", "max_pct_mt", args))
    n_top_genes = int(_p("run_scanpy_qc", "n_top_genes", args))
    mito_prefix = str(_p("run_scanpy_qc", "mito_prefix", args)).strip() or "MT-"
    symbols_key = str(_p("run_scanpy_qc", "gene_symbols_key", args)).strip()

    adata = _read_anndata(sc, src)
    adata.var_names_make_unique()
    n_cells_0, n_genes_0 = int(adata.n_obs), int(adata.n_vars)

    # Mitochondrial fraction: genes whose name starts with `mito_prefix`, case-insensitively (human
    # "MT-" and mouse "mt-" alike), read from `gene_symbols_key` when the gene names are IDs.
    if symbols_key and symbols_key not in adata.var.columns:
        return {"status": "error", "step": "qc",
                "error": (f"gene_symbols_key '{symbols_key}' is not a var column; available: "
                          f"{list(adata.var.columns)}")}
    prefix_u = mito_prefix.upper()
    names = adata.var[symbols_key].astype(str) if symbols_key else adata.var_names.to_series()
    adata.var["mt"] = names.str.upper().str.startswith(prefix_u).to_numpy()
    n_mt_genes = int(adata.var["mt"].sum())
    matched_on = f"var['{symbols_key}']" if symbols_key else "the gene names"
    # When nothing matched, find the var column that WOULD have, so the warning can name the fix
    # instead of asking the reader to go and check the naming.
    mito_hint = ""
    if n_mt_genes == 0:
        hits = []
        for col in adata.var.columns:
            if col in ("mt", symbols_key):
                continue
            try:
                n = int(adata.var[col].astype(str).str.upper().str.startswith(prefix_u).sum())
            except Exception:  # noqa: BLE001 - a column that is not text simply is not a candidate
                continue
            if n:
                hits.append(f"'{col}' ({n} genes)")
        mito_hint = (f" The var column {hits[0]} does start with '{mito_prefix}' — re-run with "
                     f"gene_symbols_key set to it." if len(hits) == 1 else
                     f" These var columns do start with '{mito_prefix}': {', '.join(hits[:4])} — "
                     "re-run with gene_symbols_key set to the one holding gene symbols."
                     if hits else
                     " No var column carries such names either: the genes may be Ensembl IDs with "
                     "no symbol column, or the mitochondrial genes were removed upstream.")
    sc.pp.calculate_qc_metrics(adata, qc_vars=["mt"], percent_top=None, log1p=False, inplace=True)

    sc.settings.figdir = str(figs)
    sc.pl.violin(
        adata, ["n_genes_by_counts", "total_counts", "pct_counts_mt"],
        jitter=0.4, multi_panel=True, show=False, save="_qc_violin.png",
    )
    sc.pl.scatter(adata, x="total_counts", y="pct_counts_mt", show=False, save="_qc_mt.png")
    sc.pl.scatter(adata, x="total_counts", y="n_genes_by_counts", show=False, save="_qc_genes.png")

    # Filter.
    sc.pp.filter_cells(adata, min_genes=min_genes)
    sc.pp.filter_genes(adata, min_cells=min_cells)
    adata = adata[adata.obs["pct_counts_mt"] < max_pct_mt].copy()

    # Keep the RAW COUNTS before normalizing. `.raw` below is set AFTER log1p, so it holds the
    # log-normalized matrix — which is what `rank_genes_groups(use_raw=True)` wants, but it is
    # NOT counts, and an older comment here claimed it was. Without this layer the counts are
    # destroyed at QC, and every count-based method downstream becomes impossible: pseudobulk
    # aggregation (summing log values is meaningless), doublet detection, scVI. Cheap insurance
    # — the layer is the same sparse matrix that was about to be overwritten.
    adata.layers["counts"] = adata.X.copy()

    # Thresholds that empty the object must fail HERE, with the numbers that caused it. Left to
    # scanpy, HVG selection dies on the empty matrix with `ValueError: Cannot cut empty array` —
    # a pandas internal that tells the user nothing about which threshold was wrong, and that
    # aborts before this function can report anything at all.
    if int(adata.n_obs) == 0 or int(adata.n_vars) == 0:
        return {
            "status": "error", "step": "qc",
            "error": (f"QC removed everything: {n_cells_0} cells x {n_genes_0} genes → "
                      f"{int(adata.n_obs)} x {int(adata.n_vars)}. The thresholds do not fit this "
                      "dataset — no checkpoint was written and no downstream step can run."),
            "cells_before": n_cells_0, "genes_before": n_genes_0,
            "cells_after": int(adata.n_obs), "genes_after": int(adata.n_vars),
            "thresholds": {"min_genes": min_genes, "min_cells": min_cells,
                           "max_pct_mt": max_pct_mt},
            "n_mt_genes": n_mt_genes,
        }

    # Normalize → log1p → HVG. `.raw` = the full LOG-NORM matrix, so DE still ranks every gene
    # after HVG subsetting in run_clustering.
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    adata.raw = adata
    n_top_genes_req = n_top_genes
    n_top_genes = min(n_top_genes, int(adata.n_vars))   # HVG can't exceed the gene count
    sc.pp.highly_variable_genes(adata, n_top_genes=n_top_genes)
    sc.pl.highly_variable_genes(adata, show=False, save="_hvg.png")

    adata.write(work / "adata_qc.h5ad")

    figures = [
        _rel(art, figs / "violin_qc_violin.png"),
        _rel(art, figs / "scatter_qc_mt.png"),
        _rel(art, figs / "scatter_qc_genes.png"),
        _rel(art, figs / "filter_genes_dispersion_hvg.png"),
    ]
    # --- self-diagnosis -------------------------------------------------------------
    # The failure class that matters here is the one where the tool SUCCEEDS: every system-level
    # contract holds, the numbers are all present, and the science is still wrong. Raw counts alone
    # do not catch it, because a reader has to notice the anomaly. So the tool says what it knows
    # about its OWN health, and the Critic keys on that instead of re-deriving it.
    warnings: list[str] = []
    if n_mt_genes == 0:
        # The worst one. Mito genes are matched by NAME ("MT-" prefix). Ensembl IDs, a different
        # convention, or an upstream filter that already removed them all give ZERO matches — then
        # pct_counts_mt is 0 for every cell, `pct_counts_mt < max_pct_mt` removes NOTHING, and the
        # result still reports max_pct_mt as if a mitochondrial filter had been applied. The report
        # then states that high-mito cells were removed, which is false.
        warnings.append(
            f"NO mitochondrial genes matched the '{mito_prefix}' prefix on {matched_on}, so "
            f"pct_counts_mt is 0 for every cell and the max_pct_mt={max_pct_mt} filter removed "
            f"NOTHING. Do NOT state that high-mitochondrial cells were filtered.{mito_hint}")
    removed_pct = 100.0 * (n_cells_0 - int(adata.n_obs)) / n_cells_0 if n_cells_0 else 0.0
    if n_cells_0 and int(adata.n_obs) == n_cells_0:
        # 0.0% removed means the input was already filtered upstream of this pipeline — the QC
        # step VALIDATED thresholds rather than applying them. Say so, or the report presents a
        # no-op as an analysis step (run 97dfc89dc5aa reported "QC removed 0.0% of cells" with no
        # interpretation; a reviewer reads that as either an error or an unexamined pipeline).
        warnings.append(
            "QC REMOVED NOTHING: every cell already satisfies these thresholds, so the input was "
            "evidently filtered upstream before it reached this pipeline. Report this step as "
            "validation of an already-filtered object, not as filtering performed here.")
    if removed_pct >= float(_p("run_scanpy_qc", "warn_removed_pct", args)):
        warnings.append(
            f"QC removed {removed_pct:.1f}% of cells ({n_cells_0} → {int(adata.n_obs)}). That is a "
            "large fraction — report it explicitly and consider whether the thresholds fit this data.")
    if int(adata.n_obs) == 0:
        warnings.append("QC removed EVERY cell — no downstream analysis is possible.")
    if n_top_genes_req > int(adata.n_vars):
        warnings.append(
            f"HVG request ({n_top_genes_req}) exceeded the {int(adata.n_vars)} genes present and was "
            f"reduced to {n_top_genes}.")

    return {
        "status": "ok",
        "step": "qc",
        "cells_before": n_cells_0,
        "genes_before": n_genes_0,
        "cells_after": int(adata.n_obs),
        "genes_after": int(adata.n_vars),
        "n_hvg": int(adata.var["highly_variable"].sum()),
        # How many genes the mito filter actually had to work with. 0 means it was a no-op — the
        # single most consequential thing this tool can silently get wrong.
        "n_mt_genes": n_mt_genes,
        "mt_filter_effective": n_mt_genes > 0,
        "mito_rule": f"name starts with '{mito_prefix}' (any case), matched on {matched_on}",
        "pct_cells_removed": round(removed_pct, 2),
        "warnings": warnings,
        "thresholds": {"min_genes": min_genes, "min_cells": min_cells, "max_pct_mt": max_pct_mt},
        # What the checkpoint actually carries, so a later step can tell whether count-based
        # methods (pseudobulk, doublets) are available instead of guessing.
        "layers": ["counts"],
        "raw_slot": "log1p_normalized",
        "read_from": read_from,
        "checkpoint": "adata_qc.h5ad",
        "figures": figures,
        "raw_data_to_llm": False,
    }


def make_tool() -> HarnessTool:
    """The ``run_scanpy_qc`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
        "run_scanpy_qc",
        "REAL scanpy QC on the uploaded single-cell dataset: per-cell metrics, "
        "cell/gene filtering, normalization, log1p, and HVG selection. Writes QC "
        "violin/scatter figures and a checkpoint. Returns pre/post cell-gene counts "
        "and the thresholds used. Run this FIRST on a single matrix; for a folder of 10x Cell "
        "Ranger outputs (raw + filtered matrices) use run_cellqc instead.",
        _schema("run_scanpy_qc", input={
            **_INPUT_SPEC,
            "description": (
                "an .h5ad to QC INSTEAD of the bound dataset — e.g. a subset or merge a "
                "run_code step saved in AISCIENTIST_WORK. A file name or a path inside this run's "
                "work/ or artifacts/ directory; nothing outside the run is read. Empty = the "
                "bound dataset.")}),
        run_scanpy_qc,
        reads_private_data=True, category="analysis", requires=("scanpy",),
    )
