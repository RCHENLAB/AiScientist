"""The single-cell steps the analysis line was missing.

``scrna_pack`` covers QC → clustering → DE → pathway. That is a complete-looking line with
five holes in it, each of which silently changes the ANSWER rather than producing an error:

* **Doublets** were never detected, so two cells captured together form an "intermediate"
  cluster that reads as a novel transitional cell type. This is the classic way a
  single-cell paper invents a population.
* **Batch/sample integration** did not exist, so a multi-sample object clusters by donor.
  Every cell-type label downstream is then a label on a donor, not on a lineage — and nothing
  in the pipeline could notice, because the clusters look perfectly clean.
* **Condition contrasts went through ``run_de``**, i.e. a Wilcoxon test over CELLS. Cells from
  one donor are not independent replicates of that donor's condition, so the p-values are
  pseudoreplicated and wildly anti-conservative. :func:`run_pseudobulk_de` aggregates to one
  profile per sample first, and REFUSES rather than degrading when there are too few samples.
* **Composition** — "which cell types shift between conditions" — is one of the most common
  questions asked of this kind of data and had no tool at all.
* **Label assignment** was a ``run_code`` template, i.e. un-versioned code the model rewrote
  each run. :func:`run_marker_annotation` makes it a deterministic, tested step.

Design contract, inherited from ``scrna_pack`` and tightened here: every tool reports what it
ACTUALLY did — ``method_used``, the keys it grouped on, the samples it dropped and why. A
frozen default that nobody can see is how ``run_de``'s 50-gene cap survived seven weeks.
"""

from __future__ import annotations

import json
import math
from typing import Any

from .scrna_pack import (
    _dirs,
    _import_scanpy,
    _missing,
    _p,
    _rel,
    _schema,
    _slug,
    _write_table,
)

ExecutorFn = Any


# --- shared helpers -----------------------------------------------------------


def _bh_fdr(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values, computed here rather than pulled from a library.

    scipy grew ``false_discovery_control`` only in 1.11 and statsmodels is an optional extra;
    six lines of arithmetic is cheaper than either version constraint.
    """
    n = len(pvals)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: pvals[i])
    out = [1.0] * n
    prev = 1.0
    for rank, idx in enumerate(reversed(order), start=1):
        i = n - rank + 1                       # 1-based rank of this p-value, largest first
        prev = min(prev, pvals[idx] * n / i)
        out[idx] = min(1.0, prev)
    return out


def _latest_checkpoint(work: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        p = work / name
        if p.exists():
            return p
    return None


def _obs_series(adata: Any, key: str) -> Any:
    return adata.obs[key].astype(str)


def _counts_matrix(adata: Any) -> Any:
    """The RAW COUNTS layer, or None. Never falls back to ``.X``/``.raw``: after QC both hold
    log-normalized values, and summing logs is not aggregation — it would produce a
    confident, completely meaningless pseudobulk profile."""
    layers = getattr(adata, "layers", None)
    if layers is not None and "counts" in layers:
        return layers["counts"]
    return None


_NO_COUNTS = (
    "this checkpoint carries no raw-count layer. run_scanpy_qc stores counts in "
    "layers['counts']; a checkpoint written before that was added has only log-normalized "
    "values, and aggregating those is meaningless. Re-run run_scanpy_qc to regenerate it."
)


# --- doublet detection --------------------------------------------------------


def run_doublet_detection(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Scrublet doublet scoring on the raw counts, BEFORE clustering.

    Two cells captured in one droplet express both parents' programmes, so they land between
    the parent clusters and read as a plausible "transitional"/"intermediate" population.
    Without this step nothing in the pipeline can tell that apart from real biology.

    Writes the score and call into obs and (by default) FILTERS the predicted doublets, since
    leaving them in is the failure mode this exists to prevent. Set ``filter: false`` to
    annotate only. Reports the rate — an implausible rate (say >20%) usually means the
    threshold, not the data, and is worth reporting rather than acting on silently.
    """
    try:
        sc = _import_scanpy()
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    ckpt = work / "adata_qc.h5ad"
    if not ckpt.exists():
        return {"status": "error", "step": "doublets",
                "error": "run_scanpy_qc must run first (adata_qc.h5ad missing)"}

    do_filter = bool(args.get("filter", True))
    batch_key = str(args.get("batch_key", "")).strip() or None
    threshold = args.get("threshold")
    expected_rate = float(_p("run_doublet_detection", "expected_doublet_rate", args))

    adata = sc.read_h5ad(ckpt)
    counts = _counts_matrix(adata)
    if counts is None:
        return {"status": "error", "step": "doublets", "error": _NO_COUNTS}
    if batch_key and batch_key not in adata.obs:
        return {"status": "error", "step": "doublets",
                "error": f"batch_key '{batch_key}' not in obs; available: {list(adata.obs.columns)}"}

    # Scrublet simulates doublets from the observed counts, so it must see counts, not the
    # log-normalized .X this checkpoint carries.
    scored = adata.copy()
    scored.X = counts.copy()
    kwargs: dict[str, Any] = {"expected_doublet_rate": expected_rate}
    if threshold is not None:
        kwargs["threshold"] = float(threshold)
    if batch_key:
        kwargs["batch_key"] = batch_key          # per-batch simulation; rates differ by run
    try:
        sc.pp.scrublet(scored, **kwargs)
    except ImportError as exc:                   # scanpy's own optional dep for thresholding
        return _missing(getattr(exc, "name", None) or "scikit-image")
    except ValueError as exc:
        # scanpy raises ValueError (not ImportError) when scikit-image is absent and no
        # explicit threshold was given. Report the actionable form rather than the raw text.
        if "scikit-image" in str(exc):
            return {"status": "dependency_missing", "step": "doublets",
                    "dependency": "scikit-image",
                    "note": ("automatic threshold selection needs scikit-image "
                             "(`pip install scanpy[scrublet]`). Alternatively pass an explicit "
                             "`threshold`, but choose it from the score histogram — do not guess.")}
        return {"status": "error", "step": "doublets", "error": f"{type(exc).__name__}: {exc}"}

    adata.obs["doublet_score"] = scored.obs["doublet_score"].values
    adata.obs["predicted_doublet"] = scored.obs["predicted_doublet"].values
    n_before = int(adata.n_obs)
    n_doublets = int(scored.obs["predicted_doublet"].sum())
    rate = n_doublets / n_before if n_before else 0.0

    if do_filter and n_doublets:
        adata = adata[~adata.obs["predicted_doublet"].values].copy()
    adata.write(ckpt)                            # in place: later steps read adata_qc.h5ad

    _write_table(tables / "doublet_summary.csv", [{
        "cells_before": n_before, "predicted_doublets": n_doublets,
        "rate": round(rate, 4), "cells_after": int(adata.n_obs),
        "filtered": do_filter, "expected_doublet_rate": expected_rate,
    }], ["cells_before", "predicted_doublets", "rate", "cells_after", "filtered",
         "expected_doublet_rate"])

    warning = ""
    if rate > float(_p("run_doublet_detection", "flag_rate_above", args)):
        warning = (f"{rate:.0%} of cells called doublets — implausibly high for most protocols. "
                   "Inspect the score histogram before trusting this; the threshold is the "
                   "likelier problem than the data.")
    return {
        "status": "ok",
        "step": "doublets",
        "cells_before": n_before,
        "predicted_doublets": n_doublets,
        "doublet_rate": round(rate, 4),
        "cells_after": int(adata.n_obs),
        "filtered": do_filter,
        "threshold_source": "explicit" if threshold is not None else "scrublet_auto",
        "batch_key": batch_key or "",
        "warning": warning,
        "tables": [_rel(art, tables / "doublet_summary.csv")],
        "checkpoint": "adata_qc.h5ad",
        "raw_data_to_llm": False,
    }


# --- batch / sample integration -----------------------------------------------


def run_integration(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Correct sample/donor/batch effects before clustering, so clusters are cell types.

    Without this, a multi-sample object clusters by donor and every downstream cell-type label
    is really a donor label — with no visible symptom, because donor-driven clusters look just
    as clean as biology-driven ones.

    Prefers Harmony (fast, operates on the PCA embedding, the scRNA default) and falls back to
    ComBat, which ships inside scanpy and needs no extra dependency. Which one ran is REPORTED,
    not implied, because the two are not equivalent. Reports batch silhouette before and after:
    it should DROP (batches mixing). If it doesn't, integration did not work and the run should
    not proceed as though it did.
    """
    try:
        sc = _import_scanpy()
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    ckpt = work / "adata_qc.h5ad"
    if not ckpt.exists():
        return {"status": "error", "step": "integration",
                "error": "run_scanpy_qc must run first (adata_qc.h5ad missing)"}

    batch_key = str(args.get("batch_key", "")).strip()
    if not batch_key:
        return {"status": "error", "step": "integration",
                "error": "batch_key is required — name the obs column holding sample/donor/batch"}
    method = str(args.get("method", "auto")).lower()
    n_pcs = int(args.get("n_pcs", 30))

    adata = sc.read_h5ad(ckpt)
    if batch_key not in adata.obs:
        return {"status": "error", "step": "integration",
                "error": f"batch_key '{batch_key}' not in obs; available: {list(adata.obs.columns)}"}
    batches = _obs_series(adata, batch_key)
    counts_by_batch = {str(k): int(v) for k, v in batches.value_counts().items()}
    if len(counts_by_batch) < 2:
        return {"status": "error", "step": "integration",
                "error": (f"'{batch_key}' has only {len(counts_by_batch)} level "
                          f"({list(counts_by_batch)}) — there is nothing to integrate. Skip this "
                          "step and cluster directly."),
                "batch_sizes": counts_by_batch}

    hvg = (adata.var["highly_variable"].values
           if "highly_variable" in adata.var.columns else None)
    work_ad = adata[:, hvg].copy() if hvg is not None and bool(hvg.any()) else adata.copy()

    def _silhouette(emb: Any) -> float | None:
        """How separated the BATCHES are in the embedding. Lower = better mixed."""
        try:
            import numpy as np
            from sklearn.metrics import silhouette_score
            n = emb.shape[0]
            idx = np.arange(n)
            if n > 5000:                          # silhouette is O(n^2); subsample honestly
                idx = np.random.default_rng(0).choice(n, 5000, replace=False)
            return float(silhouette_score(emb[idx], batches.values[idx]))
        except Exception:                          # noqa: BLE001 - a diagnostic, never fatal
            return None

    sc.pp.scale(work_ad, max_value=10)
    sc.tl.pca(work_ad, n_comps=min(n_pcs + 20, min(work_ad.shape) - 1),
              svd_solver="arpack", random_state=0)
    before = _silhouette(work_ad.obsm["X_pca"][:, :n_pcs])

    method_used, note = "", ""
    if method in ("auto", "harmony"):
        try:
            import scanpy.external as sce
            sce.pp.harmony_integrate(work_ad, batch_key, random_state=0)
            adata.obsm["X_integrated"] = work_ad.obsm["X_pca_harmony"][:, :n_pcs]
            method_used = "harmony"
        except Exception as exc:                   # noqa: BLE001 - fall through to ComBat
            if method == "harmony":
                return {"status": "error", "step": "integration",
                        "error": (f"harmony was requested but is unavailable "
                                  f"({type(exc).__name__}: {exc}). Install harmonypy, or use "
                                  "method='combat' (bundled with scanpy).")}
            note = (f"harmonypy unavailable ({type(exc).__name__}), used ComBat instead. "
                    "ComBat is a linear location/scale correction — for strong donor effects "
                    "Harmony or scVI is the better tool; install harmonypy to get it.")
    if not method_used:
        cb = work_ad.copy()
        cb.X = cb.raw.to_adata()[:, cb.var_names].X.copy() if cb.raw is not None else cb.X
        sc.pp.combat(cb, key=batch_key)
        sc.pp.scale(cb, max_value=10)
        sc.tl.pca(cb, n_comps=min(n_pcs, min(cb.shape) - 1), svd_solver="arpack", random_state=0)
        adata.obsm["X_integrated"] = cb.obsm["X_pca"]
        method_used = "combat"

    after = _silhouette(adata.obsm["X_integrated"])
    adata.uns["integration"] = {"method": method_used, "batch_key": batch_key}
    adata.write(work / "adata_integrated.h5ad")

    rows = [{"batch": b, "n_cells": n} for b, n in sorted(counts_by_batch.items())]
    _write_table(tables / "integration_batches.csv", rows, ["batch", "n_cells"])

    warning = ""
    if before is not None and after is not None and after >= before:
        warning = (f"batch silhouette did NOT improve ({before:.3f} → {after:.3f}). The batches "
                   "are not mixed; treat any cell-type label from this embedding as suspect.")
    return {
        "status": "ok",
        "step": "integration",
        "method_used": method_used,               # which one ACTUALLY ran, not which was asked for
        "batch_key": batch_key,
        "n_batches": len(counts_by_batch),
        "batch_sizes": counts_by_batch,
        "batch_silhouette_before": None if before is None else round(before, 4),
        "batch_silhouette_after": None if after is None else round(after, 4),
        "note": note,
        "warning": warning,
        "embedding": "X_integrated",
        "checkpoint": "adata_integrated.h5ad",
        "tables": [_rel(art, tables / "integration_batches.csv")],
        "raw_data_to_llm": False,
    }


# --- pseudobulk differential expression ---------------------------------------


def _deseq2_contrast(counts_ab: Any, cond_labels: list, sample_names: list,
                     gene_names: list, b_name: str, a_name: str):
    """``(lfc, pval, padj, stat)`` from DESeq2's Wald test on a samples-x-genes count matrix, or
    ``None`` when pydeseq2 is missing or errors — the caller then falls back to Welch's t and says
    so. The reason is left on ``_deseq2_contrast.last_error`` so the warning can name it."""
    import numpy as np
    _deseq2_contrast.last_error = ""
    try:
        from pydeseq2.dds import DeseqDataSet
        from pydeseq2.ds import DeseqStats
    except Exception:
        _deseq2_contrast.last_error = "pydeseq2 is not installed"
        return None
    try:
        import pandas as pd
        cdf = pd.DataFrame(np.rint(np.asarray(counts_ab, dtype=float)).astype(int),
                           index=sample_names, columns=gene_names)
        meta = pd.DataFrame({"condition": list(cond_labels)}, index=sample_names)
        try:
            dds = DeseqDataSet(counts=cdf, metadata=meta, design="~condition", quiet=True)
        except TypeError:                                   # pydeseq2 < 0.5
            dds = DeseqDataSet(counts=cdf, metadata=meta, design_factors=["condition"],
                               quiet=True)
        dds.deseq2()
        st = DeseqStats(dds, contrast=["condition", b_name, a_name], quiet=True)
        st.summary()
        r = st.results_df.reindex(gene_names)
        # DESeq2 leaves padj NaN for genes its independent filtering set aside; for counting and
        # ranking purposes that is "not significant", not "missing".
        return (np.nan_to_num(r["log2FoldChange"].to_numpy(float), nan=0.0),
                np.nan_to_num(r["pvalue"].to_numpy(float), nan=1.0),
                np.nan_to_num(r["padj"].to_numpy(float), nan=1.0),
                np.nan_to_num(r["stat"].to_numpy(float), nan=0.0))
    except Exception as exc:  # noqa: BLE001 - a degenerate matrix must degrade, not crash the tool
        _deseq2_contrast.last_error = f"{type(exc).__name__}: {exc}"
        return None


def run_pseudobulk_de(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Condition contrast done on SAMPLES, not cells.

    ``run_de`` runs a Wilcoxon test over cells. For comparing clusters within one sample that
    is fine. For comparing a CONDITION (disease vs control, treated vs untreated) it is
    pseudoreplication: 5,000 cells from 3 donors is 3 independent observations, not 5,000, and
    treating them as 5,000 produces p-values that are wrong by orders of magnitude. Nearly
    every gene comes out "significant".

    This sums raw counts to one profile per sample (optionally per cell type), drops genes too
    lowly expressed to test (edgeR ``filterByExpr``'s rule of thumb), and tests the condition with
    DESeq2's negative-binomial Wald test (via pydeseq2) — the framework the single-cell DE
    literature (Squair et al. 2021) recommends, because its variance shrinkage is what makes an
    n=2-3 design testable at all. When pydeseq2 is unavailable it falls back to Welch's t-test on
    log2 CPM and SAYS SO in ``warnings``: the fallback still fixes pseudoreplication, but has
    little power at small n.

    It REFUSES when a condition has fewer than ``min_samples_per_condition`` (default 2)
    samples. That refusal is the point: with one sample per side there is no replication and
    no test is valid, and silently falling back to the cell-level test is exactly the error
    this tool exists to prevent.
    """
    try:
        sc = _import_scanpy()
        import numpy as np
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    counts_ckpt = work / "adata_qc.h5ad"          # full gene set + the counts layer
    if not counts_ckpt.exists():
        return {"status": "error", "step": "pseudobulk_de",
                "error": "run_scanpy_qc must run first (adata_qc.h5ad missing)"}

    sample_key = str(args.get("sample_key", "")).strip()
    condition_key = str(args.get("condition_key", "")).strip()
    group_key = str(args.get("group_key", "")).strip()          # optional: per cell type
    min_cells = int(_p("run_pseudobulk_de", "min_cells_per_sample", args))
    min_samples = int(_p("run_pseudobulk_de", "min_samples_per_condition", args))
    min_count = int(_p("run_pseudobulk_de", "min_count", args))
    if not sample_key or not condition_key:
        return {"status": "error", "step": "pseudobulk_de",
                "error": ("sample_key and condition_key are both required: sample_key is the "
                          "replicate unit (donor/library), condition_key is what is contrasted.")}

    adata = sc.read_h5ad(counts_ckpt)
    counts = _counts_matrix(adata)
    if counts is None:
        return {"status": "error", "step": "pseudobulk_de", "error": _NO_COUNTS}

    # Cluster/cell-type labels live on a later checkpoint; carry them over by barcode.
    if group_key and group_key not in adata.obs:
        later = _latest_checkpoint(work, ("adata_annotated.h5ad", "adata_de.h5ad",
                                          "adata_clustered.h5ad", "adata_integrated.h5ad"))
        if later is not None:
            lab = sc.read_h5ad(later).obs
            if group_key in lab.columns:
                adata = adata[adata.obs_names.isin(lab.index)].copy()
                adata.obs[group_key] = lab.loc[adata.obs_names, group_key].values
                counts = _counts_matrix(adata)
    for key in (sample_key, condition_key, *( [group_key] if group_key else [] )):
        if key not in adata.obs:
            return {"status": "error", "step": "pseudobulk_de",
                    "error": f"'{key}' not in obs; available: {list(adata.obs.columns)}"}

    samples = _obs_series(adata, sample_key).values
    conditions = _obs_series(adata, condition_key).values
    groups = _obs_series(adata, group_key).values if group_key else np.array([""] * adata.n_obs)
    genes = list(adata.var_names)

    # sample -> condition, and a hard stop if one sample spans conditions (a mislabeled design;
    # aggregating it would silently mix the arms).
    sample_condition: dict[str, str] = {}
    for s, c in zip(samples, conditions):
        if sample_condition.setdefault(s, c) != c:
            return {"status": "error", "step": "pseudobulk_de",
                    "error": (
                        f"sample '{s}' carries more than one value of '{condition_key}' "
                        f"({sorted(set(str(c2) for s2, c2 in zip(samples, conditions) if s2 == s))}). "
                        "A replicate must belong to exactly one arm. "
                        # The common case is NOT a mislabelled design, and telling a researcher to
                        # "check the metadata" when the metadata is fine sends them looking for a
                        # bug that is not there. A `sample_key` that takes ONE value across the
                        # whole object means one library: the arms are not separated by anything,
                        # so no pseudobulk test exists to run and the finding is the study's, not
                        # the file's.
                        + (f"Here '{sample_key}' takes a single value across all "
                           f"{int(len(samples))} cells, so this object holds ONE library and has "
                           "no biological replication at all — there is no valid p-value for "
                           f"'{condition_key}' to compute. Report the comparison as DESCRIPTIVE "
                           "(effect sizes and rankings only); `run_de` with `reference` + "
                           "`stratify_by` gives that ranking per cell type. Nothing here needs "
                           "fixing in the metadata."
                           if len(set(samples)) == 1 else
                           "Check that the sample column really identifies libraries/donors and "
                           "not something that spans them."))}

    def _pseudobulk(mask: Any) -> tuple[list[str], Any, list[str]]:
        """Sum RAW counts per sample over `mask`; return (samples kept, samples x genes count
        matrix, dropped). Counts, not CPM: DESeq2 models counts, and CPM is derived for reporting
        and for the fallback test only."""
        keep, mats, dropped = [], [], []
        for s in sorted(set(samples[mask])):
            sel = mask & (samples == s)
            n = int(sel.sum())
            if n < min_cells:
                dropped.append(f"{s} ({n} cells < {min_cells})")
                continue
            mats.append(np.asarray(counts[sel].sum(axis=0)).ravel())
            keep.append(s)
        return keep, (np.vstack(mats) if mats else np.empty((0, len(genes)))), dropped

    from scipy import stats as sstats

    all_rows: list[dict[str, Any]] = []
    per_group: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    tested_universe: set[str] = set()
    fallback_warnings: list[str] = []
    for grp in (sorted(set(groups)) if group_key else [""]):
        mask = (groups == grp) if group_key else np.ones(adata.n_obs, dtype=bool)
        kept, mat, dropped = _pseudobulk(mask)
        arms: dict[str, list[int]] = {}
        for i, s in enumerate(kept):
            arms.setdefault(sample_condition[s], []).append(i)
        label = grp or "all_cells"
        if len(arms) < 2 or any(len(v) < min_samples for v in arms.values()):
            sizes = {k: len(v) for k, v in arms.items()}
            skipped[label] = (
                f"needs >={min_samples} samples in each arm, has {sizes or 'none'}"
                + (f"; dropped: {', '.join(dropped)}" if dropped else "")
                + ". No valid test exists at this replication — NOT falling back to a "
                  "cell-level test, which would be pseudoreplicated.")
            continue

        (a_name, a_idx), (b_name, b_idx) = sorted(arms.items())[:2]
        # Low-expression filter BEFORE the test (edgeR filterByExpr's rule of thumb): a gene must
        # reach `min_count` summed counts in at least as many samples as the smaller arm. A gene
        # nobody detected cannot be tested — it only inflates the BH denominator.
        used = np.asarray([*a_idx, *b_idx], dtype=int)
        min_arm = min(len(a_idx), len(b_idx))
        gidx = np.where((mat[used] >= min_count).sum(axis=0) >= min_arm)[0]
        if gidx.size == 0:
            skipped[label] = (f"no gene reached min_count={min_count} summed counts in "
                              f">={min_arm} samples — nothing is testable")
            continue
        sub_genes = [genes[i] for i in gidx]
        n_low = int(len(genes) - gidx.size)
        # log2 CPM on the FULL library (depth normalisation must see every read, including the
        # filtered genes') — reported means, and the fallback test's working scale.
        lib = mat.sum(axis=1, keepdims=True)
        lib[lib == 0] = 1.0
        logcpm = np.log2(mat / lib * 1e6 + 1.0)[:, gidx]
        A, B = logcpm[a_idx], logcpm[b_idx]

        des = _deseq2_contrast(mat[used][:, gidx],
                               [sample_condition[kept[i]] for i in used],
                               [kept[i] for i in used], sub_genes, b_name, a_name)
        if des is not None:
            lfc, pval, padj, tstat = des
            test_used = "DESeq2 Wald (negative binomial, pydeseq2)"
        else:
            with np.errstate(invalid="ignore"):
                tstat, pval = sstats.ttest_ind(B, A, axis=0, equal_var=False)   # B vs A
            lfc = B.mean(axis=0) - A.mean(axis=0)
            pval = np.nan_to_num(np.asarray(pval, dtype=float), nan=1.0)
            padj = np.asarray(_bh_fdr(list(pval)), dtype=float)
            tstat = np.nan_to_num(np.asarray(tstat, dtype=float), nan=0.0)
            test_used = "Welch t on log2 CPM (FALLBACK)"
            fallback_warnings.append(
                f"{label}: DESeq2 unavailable ({_deseq2_contrast.last_error}) — fell back to "
                "Welch's t-test on log2 CPM. The fallback still fixes pseudoreplication but has "
                "little power at n<=3 per arm (no variance shrinkage); install pydeseq2 for the "
                "recommended test.")
        tested_universe.update(sub_genes)
        rows = [{"group": label, "gene": sub_genes[i], "log2fc": round(float(lfc[i]), 4),
                 "pval": float(pval[i]), "pval_adj": round(float(padj[i]), 6),
                 # `score` keeps this table schema-compatible with run_de's, so run_enrichment
                 # and run_gsea_prerank consume a pseudobulk contrast without special-casing it.
                 "score": round(float(tstat[i]), 4),
                 "mean_" + a_name: round(float(A[:, i].mean()), 4),
                 "mean_" + b_name: round(float(B[:, i].mean()), 4)}
                for i in range(len(sub_genes))]
        rows.sort(key=lambda r: r["pval"])
        _write_table(tables / f"pseudobulk_{_slug(label)}.csv", rows[:2000],
                     ["group", "gene", "log2fc", "pval", "pval_adj", "score",
                      f"mean_{a_name}", f"mean_{b_name}"])
        # The ranked list run_gsea_prerank consumes (every tested gene, signed by the t-statistic
        # so a positive NES means "up in the condition arm"). Without it GSEA has no input for a
        # pseudobulk contrast and the protocol's step 4 is half-runnable.
        (tables / f"rank_{_slug(group_key or 'pseudobulk')}_{_slug(label)}.rnk").write_text(
            "".join(f"{r['gene']}\t{r['score']:.6g}\n"
                    for r in sorted(rows, key=lambda r: -r["score"])), encoding="utf-8")
        n_sig = sum(1 for r in rows
                    if r["pval_adj"] < float(_p("run_pseudobulk_de", "padj", args)))
        per_group[label] = {
            "contrast": f"{b_name} vs {a_name}",
            "test": test_used,
            "n_samples": {a_name: len(a_idx), b_name: len(b_idx)},
            "samples_dropped": dropped,
            "n_genes_tested": len(sub_genes),
            "n_genes_low_expression_filtered": n_low,
            "n_significant": n_sig,
            # A preview only — the table on disk is the result.
            "top_genes": [r["gene"] for r in rows[:15]],
        }
        all_rows.extend(rows[:500])

    if not per_group:
        return {"status": "error", "step": "pseudobulk_de",
                "error": ("no group had enough independent samples for a valid condition test."),
                "skipped": skipped,
                "note": ("This is a study-design limit, not a tool failure. Comparing conditions "
                         "needs biological replicates; with one sample per arm nothing "
                         "distinguishes the condition from the individual.")}

    de_table = None
    if all_rows:
        cols = ["group", "gene", "log2fc", "pval", "pval_adj", "score"]
        combined = [{k: r.get(k) for k in cols} for r in all_rows]
        _write_table(tables / "pseudobulk_all.csv", combined, cols)
        # ALSO write the canonical DE names. run_enrichment / run_gsea_prerank discover DE results
        # as `de_<key>_all.csv` + `de_<key>_universe.txt`; a pseudobulk contrast that only wrote
        # `pseudobulk_all.csv` was invisible to them, so the recommended (replicated) path silently
        # lost its per-cell-type enrichment and its ORA background fell back to a 20000 constant.
        de_key = _slug(group_key or "pseudobulk")
        de_table = tables / f"de_{de_key}_all.csv"
        _write_table(de_table, combined, cols)
        (tables / f"de_{de_key}_universe.txt").write_text(
            "\n".join(sorted(tested_universe)) + "\n", encoding="utf-8")
        # Slug -> real label, so a later step reports "Club/Secretory", not "Club_Secretory".
        _write_table(tables / f"rank_{de_key}_index.csv",
                     [{"slug": _slug(g), "group": g} for g in per_group], ["slug", "group"])
    return {
        "status": "ok",
        "step": "pseudobulk_de",
        "unit_of_replication": sample_key,        # the whole point, stated in the result
        "condition_key": condition_key,
        "group_key": group_key,
        "method": ("pseudobulk sum of raw counts -> low-expression filter (min_count) -> "
                   + " / ".join(sorted({g["test"] for g in per_group.values()}))
                   + " -> BH FDR"),
        "results_by_group": per_group,
        "skipped_groups": skipped,                # kept, never dropped: a refusal is a finding
        "min_cells_per_sample": min_cells,
        "min_samples_per_condition": min_samples,
        "min_count": min_count,
        **({"warnings": fallback_warnings} if fallback_warnings else {}),
        "tables": ([_rel(art, tables / "pseudobulk_all.csv"), _rel(art, de_table)]
                   if all_rows else []),
        # Call run_enrichment with NO `genes` argument — it picks this table up on its own and
        # runs ORA per group and per direction.
        "de_table": _rel(art, de_table) if de_table is not None else None,
        "table_columns": ["group", "gene", "log2fc", "pval", "pval_adj", "score"],
        "raw_data_to_llm": False,
    }


# --- cell-type composition ----------------------------------------------------


def run_composition(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Cell-type proportions per sample, and how they shift between conditions.

    "Which populations expand or shrink in disease" is one of the most common questions asked
    of this data and had no tool. Proportions are COMPOSITIONAL — they must sum to 1, so one
    population growing mechanically shrinks every other. The test therefore runs on
    centered-log-ratio values, and the caveat is returned with the result rather than left for
    the reader to remember.
    """
    try:
        sc = _import_scanpy()
        import numpy as np
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    # `adata_qc.h5ad` is a valid starting point, not a fallback. Clustering is required only when
    # the grouping COMES from it; a dataset that arrives with its own cell-type labels is analysed
    # straight off QC, which is precisely what the condition-comparison protocol tells the planner
    # to do ("REUSE it — do NOT re-cluster"). `run_de` had this same defect and was fixed; this
    # tool was missed, so composition — the analysis a labelled two-arm dataset needs FIRST, and
    # the one whose absence let a 3x shift in Cone abundance pass as differential expression —
    # refused to run on exactly the datasets it exists for, and demanded the re-clustering the
    # protocol forbids.
    ckpt = _latest_checkpoint(work, ("adata_annotated.h5ad", "adata_de.h5ad",
                                     "adata_clustered.h5ad", "adata_qc.h5ad"))
    if ckpt is None:
        return {"status": "error", "step": "composition",
                "error": ("no analysis checkpoint found — run run_scanpy_qc first (it writes "
                          "adata_qc.h5ad). run_clustering is needed only when the cell-type "
                          "grouping has to be derived; a dataset that already carries labels "
                          "needs QC only.")}

    adata = sc.read_h5ad(ckpt)
    group_key = str(args.get("group_key", "")).strip() or (
        "cell_type" if "cell_type" in adata.obs else "leiden")
    sample_key = str(args.get("sample_key", "")).strip()
    condition_key = str(args.get("condition_key", "")).strip()
    for key in [k for k in (group_key, sample_key, condition_key) if k]:
        if key not in adata.obs:
            return {"status": "error", "step": "composition",
                    "error": f"'{key}' not in obs; available: {list(adata.obs.columns)}"}

    groups = _obs_series(adata, group_key).values
    labels = sorted(set(groups))
    if not sample_key:
        # No replicate structure: describe, do not test.
        counts = {g: int((groups == g).sum()) for g in labels}
        total = sum(counts.values()) or 1
        rows = [{"group": g, "n_cells": counts[g], "pct": round(100 * counts[g] / total, 2)}
                for g in labels]
        _write_table(tables / "composition.csv", rows, ["group", "n_cells", "pct"])
        return {"status": "ok", "step": "composition", "group_key": group_key,
                "tested": False, "composition": rows,
                "note": ("no sample_key given, so this is a description of one pooled object. "
                         "Comparing conditions needs per-sample proportions."),
                "tables": [_rel(art, tables / "composition.csv")], "raw_data_to_llm": False}

    samples = _obs_series(adata, sample_key).values
    rows: list[dict[str, Any]] = []
    prop: dict[str, dict[str, float]] = {}
    for s in sorted(set(samples)):
        sel = samples == s
        n = int(sel.sum())
        prop[s] = {}
        for g in labels:
            k = int(((groups == g) & sel).sum())
            prop[s][g] = k / n if n else 0.0
            rows.append({"sample": s, "group": g, "n_cells": k,
                         "pct": round(100 * prop[s][g], 3)})
    _write_table(tables / "composition_by_sample.csv", rows,
                 ["sample", "group", "n_cells", "pct"])

    result: dict[str, Any] = {
        "status": "ok", "step": "composition", "group_key": group_key,
        "sample_key": sample_key, "n_samples": len(prop), "tested": False,
        "tables": [_rel(art, tables / "composition_by_sample.csv")],
        "raw_data_to_llm": False,
    }
    if not condition_key:
        result["note"] = "per-sample proportions only; pass condition_key to contrast arms."
        return result

    conditions = _obs_series(adata, condition_key).values
    # Proportions PER ARM, computed straight off the condition column and always reported — the
    # question this tool exists to answer is "does this population shift between the arms", and
    # that number must survive the study being untestable. Deriving it from the sample->condition
    # map instead silently collapses when a study has one library: `orig.ident` held a single
    # value, every cell mapped to one arm, and the tool returned the pooled composition of the
    # whole object. The 3x depletion of Cone cells between DDX41 and WT — the shift that made the
    # original run's pooled DE uninterpretable — was simply absent from the output.
    arm_names = sorted(set(str(c) for c in conditions))
    arm_rows: list[dict[str, Any]] = []
    arm_pct: dict[str, dict[str, float]] = {}
    for a in arm_names:
        sel_a = conditions == a
        n_a = int(sel_a.sum())
        arm_pct[a] = {}
        for g in labels:
            k = int(((groups == g) & sel_a).sum())
            arm_pct[a][g] = round(100 * k / n_a, 3) if n_a else 0.0
            arm_rows.append({"condition": a, "group": g, "n_cells": k, "pct": arm_pct[a][g]})
    _write_table(tables / "composition_by_condition.csv", arm_rows,
                 ["condition", "group", "n_cells", "pct"])
    result["condition_key"] = condition_key
    result["pct_by_condition"] = arm_pct
    result["tables"].append(_rel(art, tables / "composition_by_condition.csv"))
    if len(arm_names) == 2:
        a, b = arm_names
        result["largest_shifts"] = sorted(
            ({"group": g, f"pct_{a}": arm_pct[a][g], f"pct_{b}": arm_pct[b][g],
              "fold": round((arm_pct[b][g] + 1e-9) / (arm_pct[a][g] + 1e-9), 2)} for g in labels),
            key=lambda r: abs(math.log((r["fold"] or 1e-9))), reverse=True)[:6]

    cond = {s: c for s, c in zip(samples, conditions)}
    arms: dict[str, list[str]] = {}
    for s in prop:
        arms.setdefault(cond[s], []).append(s)
    if len(arms) < 2 or any(len(v) < 2 for v in arms.values()):
        one_library = len(set(samples)) == 1
        result["note"] = (
            (f"NOT TESTED — '{sample_key}' takes one value across the whole object, so this study "
             f"has one library and no biological replication; no p-value for '{condition_key}' "
             "exists. " if one_library else
             f"not tested: each arm needs >=2 samples, has {{{', '.join(f'{k}: {len(v)}' for k, v in arms.items())}}}. ")
            + "The per-arm proportions in `pct_by_condition` ARE the descriptive finding and "
              "should be reported — a large shift there means a pooled differential-expression "
              "result cannot be read as expression change. A difference between single samples is "
              "not evidence of an effect, so state it as a description, not a test.")
        return result

    # CLR: proportions are constrained to sum to 1, so testing them raw makes every population
    # look coupled to every other. CLR removes the constraint before the test.
    def _clr(p: dict[str, float]) -> dict[str, float]:
        vals = {g: max(v, 1e-6) for g, v in p.items()}
        gmean = math.exp(sum(math.log(v) for v in vals.values()) / len(vals))
        return {g: math.log(v / gmean) for g, v in vals.items()}

    clr = {s: _clr(p) for s, p in prop.items()}
    from scipy import stats as sstats
    (a_name, a_s), (b_name, b_s) = sorted(arms.items())[:2]
    tests, pvals = [], []
    for g in labels:
        A = [clr[s][g] for s in a_s]
        B = [clr[s][g] for s in b_s]
        t, p = sstats.ttest_ind(B, A, equal_var=False)
        p = 1.0 if (p is None or (isinstance(p, float) and math.isnan(p))) else float(p)
        pvals.append(p)
        tests.append({"group": g,
                      f"mean_pct_{a_name}": round(100 * sum(prop[s][g] for s in a_s) / len(a_s), 3),
                      f"mean_pct_{b_name}": round(100 * sum(prop[s][g] for s in b_s) / len(b_s), 3),
                      "clr_diff": round(sum(B) / len(B) - sum(A) / len(A), 4), "pval": p})
    for row, q in zip(tests, _bh_fdr(pvals)):
        row["pval_adj"] = round(q, 5)
    tests.sort(key=lambda r: r["pval"])
    _write_table(tables / "composition_test.csv", tests,
                 ["group", f"mean_pct_{a_name}", f"mean_pct_{b_name}", "clr_diff",
                  "pval", "pval_adj"])
    result.update({
        "tested": True,
        "contrast": f"{b_name} vs {a_name}",
        "n_samples_per_arm": {a_name: len(a_s), b_name: len(b_s)},
        "method": "per-sample proportions -> centered log-ratio -> Welch t-test -> BH FDR",
        "results": tests,
        "n_significant": sum(1 for r in tests if r["pval_adj"] < 0.05),
        "caveat": ("Proportions are compositional: they sum to 1, so a genuine expansion of one "
                   "population forces every other proportion down. A significant DECREASE is "
                   "not by itself evidence that that population lost cells."),
        "tables": result["tables"] + [_rel(art, tables / "composition_test.csv")],
    })
    return result


# --- marker-based cell-type annotation (promoted from a run_code template) -----


def run_marker_annotation(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Assign a cell type per cluster: signature score first pass, RAW expression decides.

    Previously a ``run_code`` template, i.e. code the model rewrote every run — the single most
    consequential step in the pipeline, and the least reproducible. As a tool the procedure is
    fixed and the judgement stays with the panel, which is where it belongs.

    Per-cell-type z-scoring inflates weak and ambient signal into confident-looking maxima, so
    the z-argmax is a FIRST PASS. The label is assigned only when that lineage's own
    discriminators are the dominant raw signal by ``dominance_ratio``; where the raw check
    disagrees with the first pass BOTH are reported; and a cluster with no dominant coherent
    signal stays ``Unassigned`` instead of being pushed into the nearest label.
    """
    try:
        sc = _import_scanpy()
        import numpy as np
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    ckpt = _latest_checkpoint(work, ("adata_de.h5ad", "adata_clustered.h5ad"))
    if ckpt is None:
        return {"status": "error", "step": "annotation",
                "error": "run_clustering (and ideally run_de) must run first"}

    panel = args.get("panel")
    if not isinstance(panel, dict) or not panel:
        return {"status": "error", "step": "annotation",
                "error": ("`panel` is required: {cell type: [marker symbols]} for THIS tissue. "
                          "There is no safe default — a panel from another tissue produces "
                          "confident wrong labels. Read the `annotate_clusters_by_markers_v2` "
                          "skill for how to build one and its discriminator table.")}
    discriminators = args.get("discriminators") or {}
    cluster_key = str(args.get("cluster_key", "leiden"))
    min_mean = float(args.get("min_discriminator_mean", 0.20))
    ratio = float(args.get("dominance_ratio", 1.5))

    adata = sc.read_h5ad(ckpt)
    if cluster_key not in adata.obs:
        return {"status": "error", "step": "annotation",
                "error": f"cluster_key '{cluster_key}' not in obs; available: {list(adata.obs.columns)}"}

    use_raw = adata.raw is not None
    present = set(adata.raw.var_names if use_raw else adata.var_names)
    panel = {ct: [g for g in gs if g in present] for ct, gs in panel.items()}
    absent = {ct: sorted(set(gs) - present)
              for ct, gs in (args["panel"] or {}).items() if set(gs) - present}
    not_testable = sorted(ct for ct, gs in panel.items() if not gs)
    panel = {ct: gs for ct, gs in panel.items() if gs}
    if not panel:
        return {"status": "error", "step": "annotation",
                "error": "no panel gene is present in the object — check the symbol nomenclature "
                         "(human HGNC vs mouse MGI) before anything else."}
    # No discriminators given: fall back to the panel itself, and SAY so — the disambiguation
    # is weaker without lineage-specific genes, and that changes how the labels should be read.
    disc = {ct: [g for g in (discriminators.get(ct) or panel[ct]) if g in present]
            for ct in panel}
    disc_note = "" if discriminators else (
        "no `discriminators` given, so the full panel was used for the raw check. Shared "
        "markers therefore discriminate less well; supply lineage-specific genes per type.")

    for ct, gs in panel.items():
        sc.tl.score_genes(adata, gs, score_name=f"score_{ct}", use_raw=use_raw)
    cols = [f"score_{ct}" for ct in panel]
    scores = adata.obs.groupby(cluster_key, observed=True)[cols].mean()
    scores.columns = [c[len("score_"):] for c in scores.columns]
    scores.to_csv(tables / "celltype_scores_by_cluster.csv")

    # A cell type whose score is constant across clusters (std 0, or a single cluster) carries
    # no information about which cluster is which; -inf keeps it out of the argmax instead of
    # leaving a NaN row, which pandas warns about and will eventually raise on.
    z = ((scores - scores.mean()) / scores.std().replace(0, np.nan)).fillna(float("-inf"))
    first_pass = z.idxmax(axis=1)

    disc_genes = sorted({g for gs in disc.values() for g in gs})
    raw_means = sc.get.obs_df(adata, keys=[*disc_genes, cluster_key], use_raw=use_raw) \
                  .groupby(cluster_key, observed=True)[disc_genes].mean()

    labels: dict[str, dict[str, Any]] = {}
    for cl in [str(c) for c in scores.index]:
        per_lineage = {ct: float(raw_means.loc[cl, gs].mean()) for ct, gs in disc.items() if gs}
        ranked = sorted(per_lineage.items(), key=lambda kv: kv[1], reverse=True)
        best, best_val = ranked[0] if ranked else ("", 0.0)
        runner, runner_val = ranked[1] if len(ranked) > 1 else ("", 0.0)
        dominant = best_val >= min_mean and (runner_val <= 0 or best_val >= ratio * runner_val)
        fp = str(first_pass.get(cl, "")) if cl in first_pass.index else ""
        labels[cl] = {
            "cell_type": best if dominant else "Unassigned",
            "first_pass_label": fp,
            "corrected_by_raw_check": bool(dominant and fp and fp != best),
            "n_cells": int((adata.obs[cluster_key].astype(str) == cl).sum()),
            "confidence": ("high" if dominant and best_val >= 2 * min_mean
                           else "medium" if dominant else "none"),
            "discriminator_mean": round(best_val, 4),
            "runner_up": runner, "runner_up_mean": round(runner_val, 4),
            "evidence": ", ".join(f"{g}={raw_means.loc[cl, g]:.2f}"
                                  for g in disc.get(best, [])[:3]) if dominant
                        else "no dominant lineage signal",
        }

    adata.obs["cell_type"] = adata.obs[cluster_key].map(
        lambda c: labels[str(c)]["cell_type"]).astype("category")
    adata.obs["cluster_note"] = adata.obs[cluster_key].map(lambda c: labels[str(c)]["evidence"])
    # h5py rejects '/' in obs keys (score_Pericyte/SMC) — sanitize before writing.
    adata.obs.columns = [c.replace("/", "_") for c in adata.obs.columns]
    adata.write(work / "adata_annotated.h5ad")

    comp = adata.obs["cell_type"].value_counts()
    total = int(comp.sum()) or 1
    _write_table(tables / "celltype_composition.csv",
                 [{"cell_type": str(k), "n_cells": int(v), "pct": round(100 * int(v) / total, 2)}
                  for k, v in comp.items()], ["cell_type", "n_cells", "pct"])
    _write_table(tables / "cluster_cell_types.csv",
                 [{"cluster": c, **{k: v for k, v in d.items()}} for c, d in sorted(labels.items())],
                 ["cluster", "cell_type", "first_pass_label", "corrected_by_raw_check",
                  "n_cells", "confidence", "discriminator_mean", "runner_up", "runner_up_mean",
                  "evidence"])
    (tables / "cluster_cell_types.json").write_text(json.dumps(labels, indent=2), encoding="utf-8")

    corrected = sorted(c for c, v in labels.items() if v["corrected_by_raw_check"])
    unassigned = sorted(c for c, v in labels.items() if v["cell_type"] == "Unassigned")
    return {
        "status": "ok",
        "step": "annotation",
        "cluster_key": cluster_key,
        "n_clusters": len(labels),
        "labels": {c: v["cell_type"] for c, v in sorted(labels.items())},
        # These three belong in the write-up. An annotation whose corrections and unassigned
        # clusters are invisible cannot be reviewed by anyone.
        "corrected_by_raw_check": corrected,
        "unassigned_clusters": unassigned,
        "lineages_not_testable": not_testable,
        "panel_genes_absent_from_object": absent,
        "thresholds": {"min_discriminator_mean": min_mean, "dominance_ratio": ratio},
        "note": disc_note,
        "checkpoint": "adata_annotated.h5ad",
        "tables": [_rel(art, tables / "cluster_cell_types.csv"),
                   _rel(art, tables / "celltype_composition.csv"),
                   _rel(art, tables / "celltype_scores_by_cluster.csv")],
        "raw_data_to_llm": False,
    }


# --- catalog ------------------------------------------------------------------


def scrna_advanced_catalog() -> list[Any]:
    """The five missing steps as ``HarnessTool``s. Imported lazily (see ``scrna_pack``)."""
    from ..agents.research_harness import HarnessTool

    return [
        HarnessTool(
            "run_doublet_detection",
            "Scrublet doublet scoring on raw counts, run AFTER run_scanpy_qc and BEFORE "
            "run_clustering. Two cells in one droplet express both parents' programmes and form "
            "an 'intermediate' cluster that reads as a novel transitional cell type — this is "
            "how a single-cell analysis invents a population. Filters predicted doublets by "
            "default (`filter: false` to annotate only) and returns the rate; a rate above ~20% "
            "usually means the threshold, not the biology.",
            _schema("run_doublet_detection",
                    filter={"type": "boolean",
                            "description": "remove the predicted doublets (default) or only annotate them"},
                    batch_key={"type": "string",
                               "description": "obs column to simulate doublets within, per batch"},
                    threshold={"type": "number",
                               "description": "explicit score cutoff; omit to let scrublet choose one"}),
            run_doublet_detection,
            reads_private_data=True, category="qc", requires=("scanpy",),
        ),
        HarnessTool(
            "run_integration",
            "Correct sample/donor/batch effects before clustering. REQUIRED whenever the object "
            "holds more than one sample: without it the cells cluster by donor and every "
            "cell-type label downstream is really a donor label, with no visible symptom. "
            "`batch_key` names the obs column. Uses Harmony when harmonypy is installed and "
            "falls back to ComBat (bundled with scanpy); the method that ACTUALLY ran is "
            "returned as `method_used`. Writes adata_integrated.h5ad and reports batch "
            "silhouette before/after — it must DROP, and a warning is returned if it does not.",
            {"type": "object", "properties": {
                "batch_key": {"type": "string"},
                "method": {"type": "string", "enum": ["auto", "harmony", "combat"]},
                "n_pcs": {"type": "integer"}}},
            run_integration,
            reads_private_data=True, category="analysis", requires=("scanpy",),
        ),
        HarnessTool(
            "run_pseudobulk_de",
            "Differential expression BETWEEN CONDITIONS, aggregated to one profile per sample. "
            "Use this — NOT run_de — whenever the contrast is a condition (disease vs control, "
            "treated vs untreated). run_de tests over cells, and cells from one donor are not "
            "independent replicates of that donor's condition, so its p-values are "
            "pseudoreplicated and nearly every gene comes out significant. Sums raw counts per "
            "`sample_key`, optionally within each `group_key` cell type, then filters untestably-low genes and runs DESeq2 "
            "(negative-binomial Wald, via pydeseq2; Welch t on log2 CPM as a LOUD fallback) + "
            "BH. REFUSES a group with fewer than 2 samples per arm and reports it in "
            "`skipped_groups` rather than falling back to the cell-level test. Writes "
            "`tables/de_<group_key>_all.csv` (+ the tested universe and per-group .rnk files), so "
            "run_enrichment and run_gsea_prerank pick the result up with NO `genes` argument — do "
            "not paste a gene list into them.",
            _schema("run_pseudobulk_de",
                    sample_key={"type": "string",
                                "description": "obs column holding the sample / donor / library id"},
                    condition_key={"type": "string",
                                   "description": "obs column holding the experimental condition"},
                    group_key={"type": "string",
                               "description": "cell-type column to test within, one test per cell type"}),
            run_pseudobulk_de,
            reads_private_data=True, category="analysis", requires=("scanpy",),
        ),
        HarnessTool(
            "run_composition",
            "Cell-type proportions per sample and how they shift between conditions — 'which "
            "populations expand or shrink'. With `sample_key` + `condition_key` it tests on "
            "centered-log-ratio values (proportions sum to 1, so testing them raw makes every "
            "population look coupled) and needs >=2 samples per arm, otherwise it reports "
            "proportions without a test. Returns the compositional caveat with the result.",
            {"type": "object", "properties": {
                "group_key": {"type": "string"}, "sample_key": {"type": "string"},
                "condition_key": {"type": "string"}}},
            run_composition,
            reads_private_data=True, category="analysis", requires=("scanpy",),
        ),
        HarnessTool(
            "run_marker_annotation",
            "Assign a cell type to each cluster from a curated marker `panel` "
            "({cell type: [symbols]}), with optional `discriminators` ({cell type: [2-4 "
            "lineage-SPECIFIC symbols]}). Signature scores give a first-pass z-argmax; the "
            "final label comes from RAW marker expression and is assigned only when that "
            "lineage's discriminators dominate, so shared markers (LAMP3 across AT2 and DC, "
            "SLC1A3 across Muller glia and astrocyte) cannot silently mislabel a cluster. "
            "Clusters with no dominant signal stay 'Unassigned'. `panel` is required and must "
            "match the tissue — see the annotate_clusters_by_markers_v2 skill for how to build "
            "it. Returns which clusters the raw check CORRECTED and which are unassigned; both "
            "belong in the report.",
            {"type": "object", "properties": {
                "panel": {"type": "object"}, "discriminators": {"type": "object"},
                "cluster_key": {"type": "string"},
                "min_discriminator_mean": {"type": "number"},
                "dominance_ratio": {"type": "number"}}},
            run_marker_annotation,
            reads_private_data=True, category="analysis", requires=("scanpy",),
        ),
    ]
