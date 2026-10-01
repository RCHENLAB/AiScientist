"""The ``run_pseudobulk_de`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/scrna_advanced.py`` by ``scripts/refactor/split_tools.py``: the code is the
old module's text, verbatim, with only the imports rewritten. Helpers that several tools use live
in ``bioagent.tools._lib.scrna``.
"""

from __future__ import annotations

from typing import Any
from .._lib.scrna import (
    _INPUT_SPEC,
    _NO_COUNTS,
    _bh_fdr,
    _counts_matrix,
    _dirs,
    _import_scanpy,
    _missing,
    _obs_series,
    _p,
    _rel,
    _run_rel,
    _schema,
    _slug,
    _step_input,
    _write_table,
)
from ..sdk import HarnessTool


def _latest_checkpoint(work: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        p = work / name
        if p.exists():
            return p
    return None


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
    # adata_qc.h5ad: the full gene set + the counts layer. A caller's `input` must carry both too.
    counts_ckpt, err = _step_input(ctx, args, (work / "adata_qc.h5ad",),
                                   "run_scanpy_qc must run first (adata_qc.h5ad missing)")
    if err:
        return {"status": "error", "step": "pseudobulk_de", "error": err["error"]}

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
        "read_from": _run_rel(ctx, counts_ckpt),
        "raw_data_to_llm": False,
    }


def make_tool() -> HarnessTool:
    """The ``run_pseudobulk_de`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
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
                           "description": "cell-type column to test within, one test per cell type"},
                input={**_INPUT_SPEC, "description": (
                    _INPUT_SPEC["description"] + " It must carry the raw counts in "
                    "layers['counts'], as run_scanpy_qc's checkpoint does.")}),
        run_pseudobulk_de,
        reads_private_data=True, category="analysis", requires=("scanpy",),
    )
