"""The single-cell RNA-seq **analysis production line** — real scanpy QC/clustering/DE
plus gseapy enrichment, emitting deterministic matplotlib/scanpy figures and tables.

This is the one research line AiScientist owns end-to-end (the other two lines are left
for colleague agent development). It turns an uploaded ``.h5ad`` into a Ddx41-style
bundle: QC plots → UMAP → per-cluster differential expression tables → GSEA/ORA
enrichment with bar plots — all rendered LOCALLY and DETERMINISTICALLY (no AI draws
the data figures, so there is no tampering surface).

Design, matching the rest of the codebase:

* **Lazy imports.** ``scanpy`` / ``anndata`` / ``gseapy`` are heavy and live only in
  the ``analysis`` extra (eye-server). They are imported INSIDE the functions, so this
  module imports fine on a laptop with none of them installed; a tool called without
  the dep returns ``{"status": "dependency_missing", ...}`` instead of crashing the
  whole run (same graceful-degrade contract as ``tools/report.py`` for pandoc).
* **Stateful pipeline over disk checkpoints.** Each step reads the previous step's
  ``.h5ad`` checkpoint from the run's ``work/`` dir and writes the next one, so the
  agent can call ``run_scanpy_qc`` → ``run_clustering`` → ``run_de`` → ``run_enrichment``
  in sequence and each builds on the last (no giant object threaded through prompts).
* **Privacy boundary preserved.** Tools take a dataset PATH and return only DERIVED
  metrics / figure paths / gene lists — never the raw expression matrix. They write
  artifacts under ``<workspace>/artifacts`` so the files browser + bundle pick them up.

Exposed as ``HarnessTool``s via :func:`scrna_catalog`, registered alongside the
lightweight QC/DE smoke tools in the Scientist's catalog.
"""

from __future__ import annotations

import csv
import math
import os
from pathlib import Path
from typing import Any, Callable

# Type only; the real HarnessTool/HarnessContext are imported lazily where needed to
# avoid a hard agents→tools→agents import cycle at module load.
ExecutorFn = Callable[[dict[str, Any], Any], dict[str, Any]]


# --- dependency + workspace helpers ------------------------------------------


def _missing(dep: str) -> dict[str, Any]:
    return {
        "status": "dependency_missing",
        "dependency": dep,
        "note": (
            f"`{dep}` is not installed. Install the analysis stack on the server: "
            "`pip install -e .[analysis]` (scanpy/anndata/gseapy/matplotlib/leidenalg)."
        ),
    }


def _import_scanpy() -> Any:
    """Import scanpy with a headless matplotlib backend + deterministic settings."""
    import matplotlib

    matplotlib.use("Agg")  # no display on a server; deterministic raster output
    import scanpy as sc

    sc.settings.verbosity = 1
    sc.settings.figdir = "."  # we pass explicit save names; overridden per-call
    return sc


# Formats scanpy can ingest — not just .h5ad. The upload UI + reader accept all of these.
SUPPORTED_FORMATS = (".h5ad", ".h5", ".loom", ".csv", ".tsv", ".txt", ".mtx", "10x-mtx-dir")


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


def _workspace(ctx: Any) -> Path:
    ws = getattr(ctx, "workspace", None)
    if ws is None:
        raise ValueError("scrna analysis needs ctx.workspace to write artifacts/checkpoints")
    return Path(ws)


def _dirs(ctx: Any) -> tuple[Path, Path, Path, Path]:
    """Return (work, artifacts, figures, tables) dirs for this run, creating them."""
    ws = _workspace(ctx)
    work = ws / "work"
    art = ws / "artifacts"
    figs = art / "figures"
    tables = art / "tables"
    for d in (work, art, figs, tables):
        d.mkdir(parents=True, exist_ok=True)
    return work, art, figs, tables


def _dataset_path(ctx: Any) -> Path | None:
    p = (getattr(ctx, "decisions", None) or {}).get("dataset_path")
    return Path(p) if p else None


def _rel(art: Path, path: Path) -> str:
    """Path relative to the artifacts root (the URL key the files browser uses)."""
    try:
        return path.relative_to(art).as_posix()
    except ValueError:
        return path.name


def _write_table(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _slug(name: str) -> str:
    """Filename-safe form of a group label. Real cell-type labels carry separators —
    ``Club/Secretory``, ``AT2 (alveolar type 2)`` — and a raw ``/`` in a filename makes the
    write fail outright (it reads as a directory that doesn't exist), so the per-group table
    for that class silently never appears. Group labels stay verbatim INSIDE the tables."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(name).strip())
    return safe.strip("_") or "group"


# --- step 1: QC + normalization ----------------------------------------------


def run_scanpy_qc(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Real scanpy QC: compute per-cell metrics, filter cells/genes, normalize +
    log1p + HVG. Writes ``work/adata_qc.h5ad`` and QC figures. Returns pre/post
    cell-gene counts and the QC thresholds used (derived metrics only)."""
    try:
        sc = _import_scanpy()
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    src = _dataset_path(ctx)
    if src is None or not src.exists():
        return {"status": "error", "error": "no dataset loaded (decisions['dataset_path'] missing or not found)"}

    min_genes = int(args.get("min_genes", 200))
    min_cells = int(args.get("min_cells", 3))
    max_pct_mt = float(args.get("max_pct_mt", 20.0))
    n_top_genes = int(args.get("n_top_genes", 2000))

    work, art, figs, _tables = _dirs(ctx)
    adata = _read_anndata(sc, src)
    adata.var_names_make_unique()
    n_cells_0, n_genes_0 = int(adata.n_obs), int(adata.n_vars)

    # Mitochondrial fraction (human "MT-" / mouse "mt-").
    adata.var["mt"] = adata.var_names.str.upper().str.startswith("MT-")
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

    # Normalize → log1p → HVG. `.raw` = the full LOG-NORM matrix, so DE still ranks every gene
    # after HVG subsetting in run_clustering.
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    adata.raw = adata
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
    return {
        "status": "ok",
        "step": "qc",
        "cells_before": n_cells_0,
        "genes_before": n_genes_0,
        "cells_after": int(adata.n_obs),
        "genes_after": int(adata.n_vars),
        "n_hvg": int(adata.var["highly_variable"].sum()),
        "thresholds": {"min_genes": min_genes, "min_cells": min_cells, "max_pct_mt": max_pct_mt},
        # What the checkpoint actually carries, so a later step can tell whether count-based
        # methods (pseudobulk, doublets) are available instead of guessing.
        "layers": ["counts"],
        "raw_slot": "log1p_normalized",
        "checkpoint": "adata_qc.h5ad",
        "figures": figures,
        "raw_data_to_llm": False,
    }


# --- step 2: dimensionality reduction + clustering ---------------------------


def _select_resolution(sc: Any, adata: Any, *, candidates: list[float], n_boot: int,
                       subsample: float, stability_min: float, n_neighbors: int,
                       n_pcs: int, max_cells: int) -> tuple[float, list[dict[str, Any]], str]:
    """Choose a Leiden resolution by BOOTSTRAP STABILITY instead of accepting a default.

    A partition is only trustworthy if it reproduces when the data is resampled: re-cluster
    many subsamples at each candidate resolution and score agreement with the full-data
    partition (adjusted Rand index). Stability falls as resolution rises — over-clustering
    splits cells inconsistently run to run — so the rule is the FINEST resolution that still
    clears the floor, not the most stable one (that would always return the coarsest).

    Returns (resolution, sweep rows, note). Cost is ``n_boot × len(candidates)`` re-clusterings,
    so the sweep runs on at most ``max_cells`` cells; the chosen resolution is then applied to
    the full object by the caller.
    """
    import numpy as np
    from sklearn.metrics import adjusted_rand_score

    note = ""
    base = adata
    if adata.n_obs > max_cells:
        rng = np.random.default_rng(0)
        idx = rng.choice(adata.n_obs, max_cells, replace=False)
        base = adata[idx].copy()
        sc.pp.neighbors(base, n_neighbors=n_neighbors, n_pcs=n_pcs, random_state=0)
        note = (f"stability sweep ran on a random {max_cells}-cell subset of {adata.n_obs} "
                f"(cost is n_boot × n_candidates re-clusterings); the selected resolution was "
                f"then applied to all {adata.n_obs} cells.")

    rng = np.random.default_rng(0)
    n_sub = max(2, int(subsample * base.n_obs))
    sweep: list[dict[str, Any]] = []
    for res in candidates:
        sc.tl.leiden(base, resolution=res, random_state=0, key_added="_ref")
        ref = base.obs["_ref"].astype(str).values
        aris: list[float] = []
        for _ in range(n_boot):
            idx = rng.choice(base.n_obs, n_sub, replace=False)
            sub = base[idx].copy()
            sc.pp.neighbors(sub, n_neighbors=n_neighbors, n_pcs=n_pcs, random_state=0)
            sc.tl.leiden(sub, resolution=res, random_state=0, key_added="_boot")
            aris.append(float(adjusted_rand_score(ref[idx], sub.obs["_boot"].astype(str).values)))
        sweep.append({"resolution": res, "n_clusters": int(base.obs["_ref"].nunique()),
                      "stability": round(float(np.mean(aris)), 4),
                      "stability_sd": round(float(np.std(aris)), 4)})
    if "_ref" in base.obs:
        del base.obs["_ref"]

    # A partition with ONE cluster is trivially reproducible — every resample returns the same
    # single group, so its ARI is exactly 1.0 and it clears any floor. Left in the candidate
    # set it wins whenever the data is weak, and "1 cluster, perfectly stable" is not a
    # clustering; it is the absence of one. Degenerate candidates are excluded from selection
    # but kept in the sweep table, because seeing them is how a reader diagnoses the run.
    usable = [r for r in sweep if r["n_clusters"] >= 2]
    ok = [r for r in usable if r["stability"] >= stability_min]
    if ok:
        best = max(r["resolution"] for r in ok)          # FINEST that still reproduces
    elif usable:
        best = max(usable, key=lambda r: r["stability"])["resolution"]
        note = (note + " " if note else "") + (
            f"no candidate reached the stability floor {stability_min} (best "
            f"{max(r['stability'] for r in usable):.3f}); fell back to the most stable "
            "resolution, so the partition is LESS reproducible than the floor requires and the "
            "cluster boundaries should not be treated as settled.")
    else:
        best = max(r["resolution"] for r in sweep)
        note = (note + " " if note else "") + (
            "every candidate resolution produced a single cluster — the cells do not separate "
            "at any resolution tried. Widen `resolution_candidates` upward, or take this as "
            "evidence there is no population structure to find here.")
    return float(best), sweep, note


def run_clustering(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """PCA → neighbors → Leiden → UMAP on the QC'd checkpoint. Writes
    ``work/adata_clustered.h5ad`` and a UMAP figure. Returns cluster count + sizes.

    With ``select_resolution: true`` the Leiden resolution is CHOSEN by a bootstrap-stability
    sweep rather than taken from the default — see :func:`_select_resolution`. Every downstream
    label inherits the partition, so a resolution nobody examined is an unexamined assumption
    in every cell-type call that follows.
    """
    try:
        sc = _import_scanpy()
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    ckpt = work / "adata_qc.h5ad"
    if not ckpt.exists():
        return {"status": "error", "error": "run_scanpy_qc must run first (adata_qc.h5ad missing)"}

    resolution = float(args.get("resolution", 1.0))
    n_pcs = int(args.get("n_pcs", 30))
    n_neighbors = int(args.get("n_neighbors", 15))
    select = bool(args.get("select_resolution", False))
    candidates = [float(x) for x in (args.get("resolution_candidates")
                                     or [0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0])]
    n_boot = int(args.get("n_bootstrap", 10))
    subsample = float(args.get("subsample_frac", 0.8))
    stability_min = float(args.get("stability_min", 0.90))
    max_sweep_cells = int(args.get("max_sweep_cells", 20000))

    adata = sc.read_h5ad(ckpt)
    # Restrict the memory-heavy scale/PCA to the highly-variable genes (standard scanpy).
    # `sc.pp.scale` densifies X (n_cells x n_genes), so scaling ALL genes is the main OOM
    # culprit on large datasets; the HVG subset cuts that by ~10x. The full normalized matrix
    # stays in `.raw` (set in QC), so downstream DE (`use_raw=True`) still ranks every gene.
    if "highly_variable" in adata.var.columns and bool(adata.var["highly_variable"].any()):
        adata = adata[:, adata.var["highly_variable"]].copy()
    sc.pp.scale(adata, max_value=10)
    sc.tl.pca(adata, svd_solver="arpack", random_state=0)
    sc.pp.neighbors(adata, n_neighbors=n_neighbors, n_pcs=n_pcs, random_state=0)

    sweep: list[dict[str, Any]] = []
    selection_note = ""
    resolution_source = "default" if "resolution" not in args else "explicit"
    if select:
        try:
            resolution, sweep, selection_note = _select_resolution(
                sc, adata, candidates=candidates, n_boot=n_boot, subsample=subsample,
                stability_min=stability_min, n_neighbors=n_neighbors, n_pcs=n_pcs,
                max_cells=max_sweep_cells)
            resolution_source = "bootstrap_stability"
            _write_table(tables / "resolution_sweep.csv", sweep,
                         ["resolution", "n_clusters", "stability", "stability_sd"])
        except ImportError as exc:      # scikit-learn absent → cluster at the given resolution
            selection_note = (f"resolution selection skipped ({getattr(exc, 'name', 'sklearn')} "
                              f"not installed); clustered at resolution={resolution} instead.")
        except Exception as exc:        # noqa: BLE001 - a failed sweep must not lose the run
            selection_note = (f"resolution selection failed ({type(exc).__name__}: {exc}); "
                              f"clustered at resolution={resolution} instead.")

    sc.tl.leiden(adata, resolution=resolution, random_state=0, key_added="leiden")
    sc.tl.umap(adata, random_state=0)

    sc.settings.figdir = str(figs)
    sc.pl.umap(adata, color=["leiden"], show=False, save="_clusters.png", legend_loc="on data")

    adata.write(work / "adata_clustered.h5ad")

    sizes = {str(k): int(v) for k, v in adata.obs["leiden"].value_counts().sort_index().items()}
    return {
        "status": "ok",
        "step": "clustering",
        "n_clusters": len(sizes),
        "cluster_sizes": sizes,
        "params": {"resolution": resolution, "n_pcs": n_pcs, "n_neighbors": n_neighbors},
        # How the resolution was arrived at. Every downstream cell-type label inherits this
        # partition, so "the default" and "the finest reproducible value" are very different
        # claims and the write-up must be able to tell them apart.
        "resolution_source": resolution_source,
        "resolution_sweep": sweep,
        "selection_note": selection_note,
        "checkpoint": "adata_clustered.h5ad",
        "tables": ([_rel(art, tables / "resolution_sweep.csv")] if sweep else []),
        "figures": [_rel(art, figs / "umap_clusters.png")],
        "raw_data_to_llm": False,
    }


# --- step 3: differential expression / markers per cluster -------------------


def run_de(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """``rank_genes_groups`` (Wilcoxon) per group (default: leiden cluster). Writes one
    ranked DE table per group + a combined table + a marker dotplot/rank figure.
    Returns the top genes per group (symbols + scores only)."""
    try:
        sc = _import_scanpy()
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    ckpt = work / "adata_clustered.h5ad"
    if not ckpt.exists():
        return {"status": "error", "error": "run_clustering must run first (adata_clustered.h5ad missing)"}

    groupby = str(args.get("groupby", "leiden"))
    method = str(args.get("method", "wilcoxon"))
    n_genes = int(args.get("n_genes", 50))

    adata = sc.read_h5ad(ckpt)
    if groupby not in adata.obs:
        return {"status": "error", "error": f"groupby '{groupby}' not in obs; available: {list(adata.obs.columns)}"}

    sc.tl.rank_genes_groups(adata, groupby=groupby, method=method, use_raw=True)
    adata.write(work / "adata_de.h5ad")

    res = adata.uns["rank_genes_groups"]
    groups = list(res["names"].dtype.names)
    combined: list[dict[str, Any]] = []
    top_by_group: dict[str, list[str]] = {}
    rows_by_group: dict[str, int] = {}   # ACTUAL rows written per de_<grp>.csv (ground-truth count)
    for grp in groups:
        rows: list[dict[str, Any]] = []
        for i in range(min(n_genes, len(res["names"][grp]))):
            rows.append({
                "group": grp,
                "gene": str(res["names"][grp][i]),
                "log2fc": float(res["logfoldchanges"][grp][i]),
                "pval": float(res["pvals"][grp][i]),
                "pval_adj": float(res["pvals_adj"][grp][i]),
                "score": float(res["scores"][grp][i]),
            })
        _write_table(tables / f"de_{groupby}_{_slug(grp)}.csv", rows,
                     ["group", "gene", "log2fc", "pval", "pval_adj", "score"])
        combined.extend(rows)
        rows_by_group[grp] = len(rows)
        top_by_group[grp] = [r["gene"] for r in rows[:10]]

    _write_table(tables / f"de_{groupby}_all.csv", combined,
                 ["group", "gene", "log2fc", "pval", "pval_adj", "score"])

    # --- the COMPLETE tested list, alongside the top-`n_genes` tables above ------------
    # The truncated tables can support neither of the two things downstream pathway analysis
    # actually needs, so both are written here in full:
    #   * a preranked GSEA consumes EVERY tested gene in rank order (a top-50 list has no tail,
    #     and the tail is where a coordinated-but-modest pathway shift shows up);
    #   * ORA's background must be the universe that was actually tested, not a round number.
    # Both go to disk only — `raw_data_to_llm` stays False and neither is returned to the model.
    universe = sorted({str(g) for grp in groups for g in res["names"][grp]})
    universe_path = tables / f"de_{groupby}_universe.txt"
    universe_path.write_text("\n".join(universe) + "\n", encoding="utf-8")
    rank_tables: dict[str, str] = {}
    for grp in groups:
        names, scores = res["names"][grp], res["scores"][grp]
        rnk = tables / f"rank_{groupby}_{_slug(grp)}.rnk"
        rnk.write_text(
            "".join(f"{names[i]}\t{float(scores[i]):.6g}\n" for i in range(len(names))),
            encoding="utf-8",
        )
        rank_tables[grp] = rnk.name
    # Slug → the real label, so a later step reports "Club/Secretory" and not "Club_Secretory".
    _write_table(tables / f"rank_{groupby}_index.csv",
                 [{"slug": _slug(g), "group": str(g)} for g in groups], ["slug", "group"])

    sc.settings.figdir = str(figs)
    sc.pl.rank_genes_groups(adata, n_genes=15, sharey=False, show=False, save="_de.png")
    n_top_markers = min(5, n_genes)
    marker_genes = {g: top_by_group[g][:n_top_markers] for g in groups}
    try:
        sc.pl.dotplot(adata, marker_genes, groupby=groupby, show=False, save="_markers.png")
    except Exception:  # noqa: BLE001 - dotplot can fail on degenerate inputs; the rank plot still stands
        pass

    return {
        "status": "ok",
        "step": "de",
        "groupby": groupby,
        "method": method,
        "n_groups": len(groups),
        # Ground-truth counts so a reviewer/Critic reports the REAL DE size, not the length of
        # the capped ``top_genes_by_group`` preview below (that field is a first-≤10 sample, and
        # a Critic that counted it once mis-stated "10 genes/group" when de_<grp>.csv held 50).
        "n_genes_per_group": int(n_genes),                 # requested rows per group
        "de_rows_by_group": rows_by_group,                 # ACTUAL rows written per de_<grp>.csv
        "de_rows_total": len(combined),                    # rows in de_<groupby>_all.csv
        "top_genes_by_group": top_by_group,                # PREVIEW ONLY: first ≤10 symbols/group
        # The complete tested list — what run_gsea_prerank ranks over and what run_enrichment
        # uses as its ORA background. Reported so the methods section can state the real universe.
        "tested_universe_size": len(universe),
        "universe_table": _rel(art, universe_path),
        "rank_tables": rank_tables,
        "tables": [_rel(art, tables / f"de_{groupby}_all.csv")],
        "figures": [_rel(art, figs / "rank_genes_groups_leiden_de.png")],
        "checkpoint": "adata_de.h5ad",
        "raw_data_to_llm": False,
    }


# --- step 4: enrichment (OFFLINE ORA via local GMT gene-set files) ------------

# Default gene-set libraries (freely redistributable). KEGG is intentionally NOT a
# default — its GMT redistribution is license-restricted; add "KEGG_2021_Human" via
# args.gene_sets only if you've cleared that locally.
_DEFAULT_GENE_SETS = ("GO_Biological_Process_2023", "Reactome_2022", "MSigDB_Hallmark_2020")


def _genesets_dir() -> Path:
    """Where the local ``.gmt`` gene-set files live. ``BIOAGENT_GENESETS_DIR`` overrides;
    otherwise a ``genesets/`` dir next to this module — which rides along with the dfs3b
    source bind, so the network-OFF analysis container finds it with no extra plumbing."""
    d = os.environ.get("BIOAGENT_GENESETS_DIR")
    return Path(d) if d else Path(__file__).resolve().parent / "genesets"


def run_enrichment(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Over-representation analysis (ORA) on the top DE genes per group — OFFLINE, against
    LOCAL ``.gmt`` gene-set files (``gseapy.enrich``), NOT the Enrichr web API.

    The analysis runs in a network-OFF Slurm/Singularity container, so a web API (Enrichr)
    can never reach out; and local GMTs are also more reproducible (pinned library versions,
    no rate limits). Download the libraries once with ``scripts/fetch_genesets.py`` into
    :func:`_genesets_dir`. Writes an enrichment table + bar plot per group; returns top terms.
    """
    try:
        import gseapy as gp
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "gseapy")

    work, art, figs, tables = _dirs(ctx)

    gene_sets = args.get("gene_sets") or list(_DEFAULT_GENE_SETS)
    if isinstance(gene_sets, str):
        gene_sets = [gene_sets]
    top_n_genes = int(args.get("top_n_genes", 100))
    top_n_terms = int(args.get("top_n_terms", 10))

    # Resolve requested libraries to LOCAL .gmt files (offline — no Enrichr network).
    gdir = _genesets_dir()
    libs: dict[str, str] = {}
    missing_libs: list[str] = []
    for name in gene_sets:
        p = gdir / f"{name}.gmt"
        (libs.__setitem__(name, str(p)) if p.exists() else missing_libs.append(name))
    if not libs:
        return {"status": "error", "step": "enrichment", "genesets_dir": str(gdir),
                "missing_libraries": missing_libs,
                "error": (f"no local gene-set (.gmt) files found in {gdir} for {gene_sets}. "
                          "Offline enrichment needs local GMTs (the analysis container has no "
                          "network) — run scripts/fetch_genesets.py to download them.")}

    # Prefer DE results computed this run — for ANY groupby (leiden, majorclass, cell_type, …),
    # so enrichment runs PER CLASS instead of collapsing to one pooled list. run_de writes
    # ``de_<groupby>_all.csv``; discover it: an explicit ``args.groupby`` wins, otherwise prefer an
    # ANNOTATED grouping (e.g. majorclass) over raw ``leiden`` clusters, which carry more biological
    # meaning. Falling back to a single agent-passed gene list ("input") is the last resort — it is
    # what silently pooled every class together when the DE table couldn't be found.
    groupby = str(args.get("groupby", "")).strip()
    de_all: Path | None = None
    if groupby:
        cand = tables / f"de_{groupby}_all.csv"
        de_all = cand if cand.exists() else None
    if de_all is None:
        cands = sorted(tables.glob("de_*_all.csv"))
        non_leiden = [c for c in cands if c.name != "de_leiden_all.csv"]
        de_all = (non_leiden or cands or [None])[0]

    groups_genes: dict[str, list[str]] = {}
    if de_all is not None and de_all.exists():
        with de_all.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                groups_genes.setdefault(row["group"], [])
                if len(groups_genes[row["group"]]) < top_n_genes and float(row.get("pval_adj", 1) or 1) < 0.05:
                    groups_genes[row["group"]].append(row["gene"])
    elif args.get("genes"):
        genes = args["genes"]
        groups_genes = {"input": list(genes) if isinstance(genes, list) else [genes]}
    if not groups_genes:
        return {"status": "error", "error": "no DE genes available — run run_de first, or pass args.genes"}

    # --- background: the universe that was actually tested ---------------------------
    # ORA's p-value is a hypergeometric tail against the background, so the background is a
    # statistical parameter, not a formality: a generic "~20000 human genes" over-states the
    # universe whenever QC/HVG filtering left far fewer genes in the object, and every term
    # then looks more enriched than it is. Prefer the universe run_de wrote; fall back to an
    # explicit override, and only then to the round number — recorded either way so the
    # methods section can state which one produced the numbers.
    background: Any = None
    background_source = ""
    universe_file = None
    if de_all is not None:
        stem = de_all.name[len("de_"):-len("_all.csv")]
        cand = tables / f"de_{stem}_universe.txt"
        universe_file = cand if cand.exists() else None
    if universe_file is not None:
        symbols = [ln.strip() for ln in universe_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if symbols:
            background, background_source = symbols, "tested_universe"
    if background is None and args.get("background") is not None:
        background, background_source = int(args["background"]), "explicit_override"
    if background is None:
        background, background_source = 20000, "constant_fallback"
    background_n = len(background) if isinstance(background, list) else int(background)

    # matplotlib is OPTIONAL here: the tables + returned top terms are the real output; the bar
    # plots are a nice-to-have. Import lazily so a minimal env without it (CI base, non-analysis
    # extra) still produces enrichment tables instead of hard-failing on the import.
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        plt = None

    enriched: dict[str, list[dict[str, Any]]] = {}
    figures: list[str] = []
    errors: list[str] = []
    gmt_paths = list(libs.values())
    for grp, genes in groups_genes.items():
        if not genes:
            continue
        try:
            # Offline hypergeometric ORA against the local GMTs — no network, no rate limits.
            enr = gp.enrich(gene_list=list(genes), gene_sets=gmt_paths,
                            background=background, outdir=None, verbose=False)
        except Exception as exc:  # noqa: BLE001 - a bad group is reported, never fatal
            errors.append(f"{grp}: {type(exc).__name__}: {exc}")
            continue
        df = enr.results.sort_values("Adjusted P-value").head(top_n_terms)
        rows = [
            {"group": grp, "term": str(r["Term"]), "gene_set": str(r["Gene_set"]),
             "adj_pval": float(r["Adjusted P-value"]), "combined_score": float(r["Combined Score"]),
             "overlap": str(r["Overlap"])}
            for _, r in df.iterrows()
        ]
        _write_table(tables / f"enrichment_{_slug(grp)}.csv", rows,
                     ["group", "term", "gene_set", "adj_pval", "combined_score", "overlap"])
        enriched[grp] = rows[:top_n_terms]

        if rows and plt is not None:
            fig, ax = plt.subplots(figsize=(8, max(2.5, 0.4 * len(rows))))
            terms = [r["term"][:60] for r in rows][::-1]
            scores = [50.0 if r["adj_pval"] <= 0 else -math.log10(r["adj_pval"]) for r in rows][::-1]
            ax.barh(terms, scores, color="#4C72B0")
            ax.set_xlabel("-log10(adjusted p-value)")
            ax.set_title(f"Top enriched terms — group {grp}")
            fig.tight_layout()
            fig_path = figs / f"enrichment_{_slug(grp)}.png"
            fig.savefig(fig_path, dpi=150)
            plt.close(fig)
            figures.append(_rel(art, fig_path))

    if not enriched and errors:
        return {"status": "error", "step": "enrichment", "errors": errors,
                "genesets_dir": str(gdir), "missing_libraries": missing_libs,
                "note": "offline ORA produced no enriched terms — check the local GMT files."}

    return {
        "status": "ok",
        "step": "enrichment",
        "gene_sets": list(libs),
        "genesets_dir": str(gdir),
        "missing_libraries": missing_libs,
        "groups": list(enriched),
        "top_terms_by_group": {g: [r["term"] for r in rows] for g, rows in enriched.items()},
        # State the background the p-values were actually computed against — a reviewer cannot
        # judge an ORA result without it, and a report must not silently imply the whole genome.
        "background_source": background_source,
        "background_size": background_n,
        "tables": [_rel(art, tables / f"enrichment_{_slug(g)}.csv") for g in enriched],
        "figures": figures,
        "errors": errors,
        "note": ("ORA and preranked GSEA (run_gsea_prerank) test different inputs under different "
                 "null hypotheses — they are NOT expected to agree, and disagreement is not a "
                 "failure of either."),
        "raw_data_to_llm": False,
    }


# --- step 4b: preranked GSEA (OFFLINE, over the COMPLETE ranked list) ---------


def run_gsea_prerank(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Preranked GSEA (``gseapy.prerank``) over the complete ranked gene list per group —
    OFFLINE, against the same local ``.gmt`` files ORA uses.

    Complements :func:`run_enrichment` rather than replacing it. ORA asks whether a
    *thresholded* list of top genes over-represents a set; GSEA asks whether a set drifts
    toward one end of the *whole* ranking, so it sees coordinated shifts that never clear a
    per-gene cutoff, and it returns a signed NES (direction) instead of an unsigned overlap.
    The two use different inputs and different nulls — do NOT require them to agree.

    Ranks come from the ``rank_<groupby>_<group>.rnk`` files ``run_de`` writes (the Wilcoxon
    z-score, so a positive NES means "up in this group vs the rest").
    """
    try:
        import gseapy as gp
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "gseapy")

    work, art, figs, tables = _dirs(ctx)

    gene_sets = args.get("gene_sets") or list(_DEFAULT_GENE_SETS)
    if isinstance(gene_sets, str):
        gene_sets = [gene_sets]
    top_n_terms = int(args.get("top_n_terms", 10))
    min_size = int(args.get("min_size", 15))
    max_size = int(args.get("max_size", 500))
    permutations = int(args.get("permutations", 1000))
    fdr_max = float(args.get("fdr_max", 0.25))   # the conventional GSEA significance floor
    seed = int(args.get("seed", 42))             # fixed: permutation p-values must reproduce

    gdir = _genesets_dir()
    libs = {n: str(gdir / f"{n}.gmt") for n in gene_sets if (gdir / f"{n}.gmt").exists()}
    missing_libs = [n for n in gene_sets if n not in libs]
    if not libs:
        return {"status": "error", "step": "gsea_prerank", "genesets_dir": str(gdir),
                "missing_libraries": missing_libs,
                "error": (f"no local gene-set (.gmt) files found in {gdir} for {gene_sets}. "
                          "Offline GSEA needs local GMTs — run scripts/fetch_genesets.py.")}

    # Resolve the grouping from run_de's slug index rather than by splitting filenames: a
    # groupby is routinely itself underscored (`cell_type`, `major_class`), so any positional
    # split of `rank_cell_type_Club_Secretory` guesses the boundary wrong.
    groupby = str(args.get("groupby", "")).strip()
    if not groupby:
        idx_files = sorted(tables.glob("rank_*_index.csv"))
        # Same preference as run_enrichment: an ANNOTATED grouping carries more biology than
        # raw leiden ids, so if both exist rank the annotated one.
        idx_files = [p for p in idx_files if p.name != "rank_leiden_index.csv"] or idx_files
        if idx_files:
            groupby = idx_files[0].name[len("rank_"):-len("_index.csv")]
    labels: dict[str, str] = {}
    if groupby:
        index_csv = tables / f"rank_{groupby}_index.csv"
        if index_csv.exists():
            with index_csv.open(encoding="utf-8") as fh:
                labels = {row["slug"]: row["group"] for row in csv.DictReader(fh)}
    rnk_files = sorted(tables.glob(f"rank_{groupby}_*.rnk") if groupby else tables.glob("rank_*.rnk"))
    prefix = f"rank_{groupby}_" if groupby else "rank_"
    if not rnk_files:
        return {"status": "error", "step": "gsea_prerank",
                "error": ("no ranked gene lists found — run run_de first (it writes "
                          "tables/rank_<groupby>_<group>.rnk covering every tested gene).")}

    def _col(df: Any, *names: str) -> str | None:
        """gseapy has renamed these across releases (`FDR q-val` / `fdr`, `Lead_genes` /
        `ledge_genes`), so resolve by whichever the installed version emits."""
        for n in names:
            if n in df.columns:
                return n
        return None

    results: dict[str, list[dict[str, Any]]] = {}
    ranked_sizes: dict[str, int] = {}
    errors: list[str] = []
    gmt_paths = list(libs.values())
    for rnk in rnk_files:
        slug = rnk.name[len(prefix):-len(".rnk")] if rnk.name.startswith(prefix) else rnk.stem
        grp = labels.get(slug, slug)
        try:
            ranked_sizes[grp] = sum(
                1 for ln in rnk.read_text(encoding="utf-8").splitlines() if "\t" in ln)
            # Hand gseapy the .rnk PATH, which is its documented input alongside a
            # DataFrame/Series. A plain dict is NOT accepted (checked against gseapy 1.2.1 —
            # `rnk: Union[DataFrame, Series, str]`), and passing one fails only at run time.
            pre = gp.prerank(rnk=str(rnk), gene_sets=gmt_paths, min_size=min_size,
                             max_size=max_size, permutation_num=permutations,
                             outdir=None, seed=seed, verbose=False)
        except Exception as exc:  # noqa: BLE001 - a bad group is reported, never fatal
            errors.append(f"{grp}: {type(exc).__name__}: {exc}")
            continue

        df = pre.res2d
        c_term = _col(df, "Term") or "Term"
        c_nes = _col(df, "NES", "nes")
        c_fdr = _col(df, "FDR q-val", "fdr", "FDR")
        c_p = _col(df, "NOM p-val", "pval", "p-val")
        c_lead = _col(df, "Lead_genes", "ledge_genes", "Leading_edge")
        rows = []
        for _, r in df.iterrows():
            nes = float(r[c_nes]) if c_nes else 0.0
            fdr = float(r[c_fdr]) if c_fdr else 1.0
            # Given a LIST of .gmt paths, gseapy prefixes every term with the file basename
            # ("MSigDB_Hallmark_2020.gmt__Notch Signaling"). Unprefixed, that string goes
            # verbatim into a report. Split it back out so `term` is the pathway and the
            # library is its own column — the same shape run_enrichment already returns.
            raw_term = str(r[c_term])
            gene_set, _, pathway = raw_term.rpartition("__")
            term = pathway if gene_set else raw_term
            rows.append({
                "group": grp,
                "term": term,
                "gene_set": gene_set[:-4] if gene_set.endswith(".gmt") else gene_set,
                "nes": nes,
                "pval": float(r[c_p]) if c_p else float("nan"),
                "fdr": fdr,
                "direction": "up" if nes > 0 else "down",
                "leading_edge": str(r[c_lead]) if c_lead else "",
            })
        # Rank by |NES| so a strongly DOWN set is as visible as a strongly up one — sorting by
        # NES alone would bury every suppressed programme at the bottom of the table.
        rows.sort(key=lambda r: abs(r["nes"]), reverse=True)
        _write_table(tables / f"gsea_{_slug(grp)}.csv", rows,
                     ["group", "term", "gene_set", "nes", "pval", "fdr", "direction",
                      "leading_edge"])
        results[grp] = [r for r in rows if r["fdr"] <= fdr_max][:top_n_terms]

    if not results and errors:
        return {"status": "error", "step": "gsea_prerank", "errors": errors,
                "genesets_dir": str(gdir), "missing_libraries": missing_libs}

    return {
        "status": "ok",
        "step": "gsea_prerank",
        "gene_sets": list(libs),
        "genesets_dir": str(gdir),
        "missing_libraries": missing_libs,
        "groups": list(results),
        # Full tables are on disk; only signed summaries come back to the model.
        "top_terms_by_group": {
            g: [{"term": r["term"], "gene_set": r["gene_set"], "nes": round(r["nes"], 3),
                 "fdr": round(r["fdr"], 4), "direction": r["direction"]} for r in rows]
            for g, rows in results.items()
        },
        # Groups whose every term missed the FDR floor land here as an EMPTY list, not as a
        # missing key: "we tested and nothing passed" is a result and belongs in the write-up.
        "ranked_list_size_by_group": ranked_sizes,
        "params": {"min_size": min_size, "max_size": max_size, "permutations": permutations,
                   "seed": seed, "fdr_max": fdr_max, "ranking_statistic": "wilcoxon_z"},
        "tables": [_rel(art, tables / f"gsea_{_slug(g)}.csv") for g in results],
        "errors": errors,
        "note": ("Preranked GSEA and ORA (run_enrichment) test different inputs under different "
                 "null hypotheses — they are NOT expected to agree. Enrichment is association "
                 "with an expression programme, not evidence of pathway activity or causation."),
        "raw_data_to_llm": False,
    }


# --- catalog ------------------------------------------------------------------


def scrna_catalog() -> list[Any]:
    """The analysis-line tools as ``HarnessTool``s, in pipeline order.

    Imported lazily here (not at module top) so ``tools.scrna_pack`` has no import
    dependency on the agents package — the harness imports this, not vice-versa.

    The steps that were missing from this line (doublets, integration, pseudobulk,
    composition, marker annotation) live in ``scrna_advanced`` and are appended below, so
    every caller of ``scrna_catalog()`` gets the whole line rather than half of it.
    """
    from ..agents.research_harness import HarnessTool

    from .scrna_advanced import scrna_advanced_catalog

    return [
        HarnessTool(
            "run_scanpy_qc",
            "REAL scanpy QC on the uploaded single-cell dataset: per-cell metrics, "
            "cell/gene filtering, normalization, log1p, and HVG selection. Writes QC "
            "violin/scatter figures and a checkpoint. Returns pre/post cell-gene counts "
            "and the thresholds used. Run this FIRST.",
            {"type": "object", "properties": {
                "min_genes": {"type": "integer"}, "min_cells": {"type": "integer"},
                "max_pct_mt": {"type": "number"}, "n_top_genes": {"type": "integer"}}},
            run_scanpy_qc,
            reads_private_data=True, category="analysis", requires=("scanpy",),
        ),
        HarnessTool(
            "run_clustering",
            "PCA → neighbors → Leiden clustering → UMAP on the QC'd data. Writes a UMAP "
            "figure and returns cluster count + sizes. Run AFTER run_scanpy_qc. "
            "Set `select_resolution: true` to CHOOSE the Leiden resolution by bootstrap "
            "stability instead of accepting the 1.0 default: each candidate resolution is "
            "re-clustered over resampled subsets and scored by adjusted Rand index against the "
            "full-data partition, and the FINEST resolution still clearing `stability_min` "
            "wins. Prefer this whenever cell-type labels will be assigned from the clusters — "
            "every label inherits the partition, so an unexamined resolution is an unexamined "
            "assumption in every label. It costs n_bootstrap × len(resolution_candidates) "
            "re-clusterings, so it is opt-in; the sweep itself is capped at `max_sweep_cells`.",
            {"type": "object", "properties": {
                "resolution": {"type": "number"}, "n_pcs": {"type": "integer"},
                "n_neighbors": {"type": "integer"},
                "select_resolution": {"type": "boolean"},
                "resolution_candidates": {"type": "array", "items": {"type": "number"}},
                "n_bootstrap": {"type": "integer"}, "subsample_frac": {"type": "number"},
                "stability_min": {"type": "number"}, "max_sweep_cells": {"type": "integer"}}},
            run_clustering,
            reads_private_data=True, category="analysis", requires=("scanpy",),
        ),
        HarnessTool(
            "run_de",
            "Differential expression / marker genes per cluster via rank_genes_groups "
            "(Wilcoxon). Writes the checkpoint `work/adata_de.h5ad` and CSV tables "
            "`tables/de_<groupby>_all.csv` (+ one per group). The tables have EXACTLY these "
            "columns: `group,gene,log2fc,pval,pval_adj,score` — if you ever read a DE table "
            "in run_code, use THOSE names (NOT Seurat-style `gene_name`/`p_val_adj`/`avg_log2FC`). "
            "Returns top genes per cluster. Run AFTER run_clustering.",
            {"type": "object", "properties": {
                "groupby": {"type": "string"}, "method": {"type": "string"},
                "n_genes": {"type": "integer"}}},
            run_de,
            reads_private_data=True, category="analysis", requires=("scanpy",),
        ),
        HarnessTool(
            "run_enrichment",
            "Over-representation / pathway enrichment — OFFLINE ORA against LOCAL gene-set "
            "(.gmt) files (gseapy.enrich), NOT the Enrichr web API (the analysis host has no "
            "network). Automatically reads the top DE genes from this run's DE table "
            "(`tables/de_<groupby>_all.csv`, e.g. de_majorclass_all.csv) and runs ORA PER GROUP, "
            "writing one enrichment table + bar plot per cell class and returning the top enriched "
            "terms per group. Do NOT pass a pooled `genes` list — that collapses every class into a "
            "single 'input' group and loses the per-class pathway biology. Run AFTER run_de; it "
            "picks up whatever groupby run_de used (annotated classes preferred over raw leiden). "
            "`gene_sets` are library names resolved to local .gmt files "
            "(default GO_Biological_Process_2023 / Reactome_2022 / MSigDB_Hallmark_2020).",
            {"type": "object", "properties": {
                "gene_sets": {"type": "array", "items": {"type": "string"}},
                "top_n_genes": {"type": "integer"}, "top_n_terms": {"type": "integer"},
                "background": {"type": "integer"},
                "genes": {"type": "array", "items": {"type": "string"}}}},
            run_enrichment,
            reads_private_data=False, category="analysis", requires=("gseapy",),
        ),
        HarnessTool(
            "run_gsea_prerank",
            "Preranked GSEA (gseapy.prerank) over the COMPLETE ranked gene list per group — "
            "OFFLINE, against the same local .gmt files as run_enrichment. Reads the "
            "`tables/rank_<groupby>_<group>.rnk` files run_de writes (every tested gene, ranked "
            "by Wilcoxon z), so it detects coordinated shifts that no per-gene cutoff would keep, "
            "and returns a SIGNED NES (positive = up in that group) plus FDR and leading-edge "
            "genes. Writes `tables/gsea_<group>.csv` per group. Run AFTER run_de. This is a "
            "COMPLEMENT to run_enrichment, not a replacement: ORA thresholds a top-N list, GSEA "
            "walks the whole ranking, and the two use different null hypotheses — do NOT treat "
            "disagreement between them as an error, and do NOT drop a group because nothing "
            "passed FDR (report the null result).",
            {"type": "object", "properties": {
                "gene_sets": {"type": "array", "items": {"type": "string"}},
                "groupby": {"type": "string"}, "top_n_terms": {"type": "integer"},
                "min_size": {"type": "integer"}, "max_size": {"type": "integer"},
                "permutations": {"type": "integer"}, "fdr_max": {"type": "number"},
                "seed": {"type": "integer"}}},
            run_gsea_prerank,
            reads_private_data=False, category="analysis", requires=("gseapy",),
        ),
        *scrna_advanced_catalog(),
    ]


def scrna_analysis_summary(results: dict[str, Any]) -> str:
    """Render a small markdown section from the step result dicts (for the report)."""
    lines = ["## Single-cell analysis pipeline", ""]
    if "qc" in results:
        q = results["qc"]
        lines.append(f"- **QC**: {q.get('cells_before')}→{q.get('cells_after')} cells, "
                     f"{q.get('genes_before')}→{q.get('genes_after')} genes ({q.get('n_hvg')} HVGs).")
    if "clustering" in results:
        c = results["clustering"]
        p = c.get("params", {})
        how = {"bootstrap_stability": "selected by bootstrap stability (ARI)",
               "explicit": "set explicitly", "default": "default"}.get(c.get("resolution_source", ""), "")
        lines.append(f"- **Clustering**: {c.get('n_clusters')} Leiden clusters at resolution "
                     f"{p.get('resolution')}" + (f" — {how}." if how else "."))
    if "de" in results:
        d = results["de"]
        lines.append(f"- **DE**: top markers for {d.get('n_groups')} groups (by {d.get('groupby')}).")
    if "enrichment" in results:
        e = results["enrichment"]
        lines.append(f"- **Enrichment (ORA)**: {', '.join(e.get('gene_sets', []))} over "
                     f"{len(e.get('groups', []))} groups; background = "
                     f"{e.get('background_size', '?')} genes ({e.get('background_source', 'unspecified')}).")
    if "gsea_prerank" in results:
        g = results["gsea_prerank"]
        p = g.get("params", {})
        lines.append(f"- **Preranked GSEA**: {', '.join(g.get('gene_sets', []))} over "
                     f"{len(g.get('groups', []))} groups; {p.get('permutations', '?')} permutations, "
                     f"seed {p.get('seed', '?')}, set size {p.get('min_size', '?')}–{p.get('max_size', '?')}, "
                     f"FDR ≤ {p.get('fdr_max', '?')}.")
    return "\n".join(lines) + "\n"
