"""The ``run_clustering`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/scrna_pack.py`` by ``scripts/refactor/split_tools.py``: the code is the old
module's text, verbatim, with only the imports rewritten. Helpers that several tools use live in
``aiscientist.tools._lib.scrna``.
"""

from __future__ import annotations

from typing import Any
from .._lib.scrna import (
    _INPUT_SPEC,
    _dirs,
    _import_scanpy,
    _looks_like_celltype_column,
    _missing,
    _p,
    _rel,
    _run_rel,
    _schema,
    _step_input,
    _write_table,
)
from ..sdk import HarnessTool


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
    ckpt, err = _step_input(ctx, args, (work / "adata_qc.h5ad",),
                            "run_scanpy_qc must run first (adata_qc.h5ad missing)")
    if err:
        return err

    resolution = float(_p("run_clustering", "resolution", args))
    n_pcs = int(_p("run_clustering", "n_pcs", args))
    n_neighbors = int(_p("run_clustering", "n_neighbors", args))
    select = bool(_p("run_clustering", "select_resolution", args))
    candidates = [float(x) for x in (args.get("resolution_candidates")
                                     or [0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0])]
    n_boot = int(_p("run_clustering", "n_bootstrap", args))
    subsample = float(_p("run_clustering", "subsample_frac", args))
    stability_min = float(_p("run_clustering", "stability_min", args))
    max_sweep_cells = int(_p("run_clustering", "max_sweep_cells", args))

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

    # --- self-diagnosis -------------------------------------------------------------
    # cluster_sizes and n_clusters are already reported, but reporting a number is not the same as
    # flagging it: a reader has to NOTICE that "n_clusters: 1" invalidates every downstream step.
    warnings: list[str] = []
    if len(sizes) <= 1:
        warnings.append(
            f"Clustering produced {len(sizes)} cluster — the partition is degenerate, so per-cluster "
            "DE, markers and annotation from it are meaningless. Raise the resolution or check that "
            "the neighbourhood graph was built on real variation.")
    tiny = {k: v for k, v in sizes.items() if v < 10}
    if tiny:
        shown = ", ".join(f"{k}:{v}" for k, v in list(tiny.items())[:8])
        warnings.append(
            f"{len(tiny)} of {len(sizes)} clusters have fewer than 10 cells ({shown}). Markers and "
            "DE from clusters that small are unstable — do not report them as findings without "
            "saying how few cells back them.")
    # The recurring failure in this codebase: the data ALREADY carries expert cell-type labels and
    # the run clusters de-novo anyway, then does DE/enrichment on numeric leiden IDs. The lab has a
    # planning-time guard, but nothing told the person reading THIS tool's result.
    existing = [c for c in adata.obs.columns if _looks_like_celltype_column(str(c))]
    if existing:
        warnings.append(
            f"This dataset already carries cell-type label column(s): {', '.join(existing[:4])}. "
            "De-novo leiden clusters were computed anyway — prefer the EXISTING labels for "
            "differential expression and enrichment unless re-clustering is the explicit goal, "
            "because numeric cluster IDs are not biologically interpretable.")

    return {
        "status": "ok",
        "step": "clustering",
        "n_clusters": len(sizes),
        "cluster_sizes": sizes,
        "n_small_clusters": len(tiny),
        "existing_celltype_columns": existing,
        "warnings": warnings,
        "params": {"resolution": resolution, "n_pcs": n_pcs, "n_neighbors": n_neighbors},
        # How the resolution was arrived at. Every downstream cell-type label inherits this
        # partition, so "the default" and "the finest reproducible value" are very different
        # claims and the write-up must be able to tell them apart.
        "resolution_source": resolution_source,
        "resolution_sweep": sweep,
        "selection_note": selection_note,
        "read_from": _run_rel(ctx, ckpt),
        "checkpoint": "adata_clustered.h5ad",
        "tables": ([_rel(art, tables / "resolution_sweep.csv")] if sweep else []),
        "figures": [_rel(art, figs / "umap_clusters.png")],
        "raw_data_to_llm": False,
    }


def make_tool() -> HarnessTool:
    """The ``run_clustering`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
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
        _schema("run_clustering", resolution_candidates={
            "type": "array", "items": {"type": "number"},
            "description": "resolutions to try when select_resolution is on"},
            input=_INPUT_SPEC),
        run_clustering,
        reads_private_data=True, category="analysis", requires=("scanpy",),
    )
