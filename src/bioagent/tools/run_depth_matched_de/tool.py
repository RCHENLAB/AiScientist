"""The ``run_depth_matched_de`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/scrna_pack.py`` by ``scripts/refactor/split_tools.py``: the code is the old
module's text, verbatim, with only the imports rewritten. Helpers that several tools use live in
``bioagent.tools._lib.scrna``.
"""

from __future__ import annotations

from typing import Any
from .._lib.scrna import (
    _INPUT_SPEC,
    _dirs,
    _import_scanpy,
    _missing,
    _p,
    _run_rel,
    _schema,
    _step_input,
    _write_table,
)
from ..sdk import HarnessTool


# A gene counts as depth-robust when it keeps its sign AND at least this fraction of its original
# WILCOXON Z after the deeper arm is down-sampled. 0.8 is not a taste call: swept against
# experiments/depth_matched_validation, 0.5/0.6/0.8/1.0 keep 30/30, 30/30, 27/30, 7/30 of the
# genuinely-changed genes and 0/30 of background either way, while the pure-depth control passes
# 50%, 43%, 35%, 28%. 0.8 buys the biggest drop in noise for the least real signal.
_DEPTH_ROBUST_MIN_FRACTION = 0.8


def run_depth_matched_de(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Is a ranking biology, or is it the deeper arm being deeper? Down-sample and re-rank.

    When one arm is sequenced deeper per cell, EVERY comparison inherits that: a gene detected in
    more cells simply because more molecules were sampled scores as up-regulated, and whole
    pathways of abundant transcripts (translation, ribosome, RNA metabolism) move together in the
    deeper arm. On a design with one library per arm there is no replication to separate the two,
    so the only honest check is to remove the imbalance and see what survives.

    This is that check, as a deterministic tool. It existed only as a step brief before, and three
    different models wrote three different wrong versions of it: a plan told the executor to
    "correlate per-gene logFC against the between-arm median nCount_RNA", which is not computable
    (a per-gene vector against one scalar has no second variable), and the step failed on every
    attempt. The operation that IS meaningful:

      1. within each cell type, down-sample the deeper arm's counts so its per-cell UMI
         distribution matches the shallower arm's, quantile by quantile (never up-sampling: a cell
         is capped at the counts it actually has);
      2. re-run the SAME Wilcoxon contrast on the matched cells;
      3. Spearman-correlate the original gene ranking against the depth-matched one, separately
         per direction — an up-ranking and a down-ranking fail differently, and averaging them
         hides an inversion;
      4. report which genes keep their sign and most of their effect (depth-robust) and which do
         not (depth-driven).

    THE TWO DIRECTIONS ARE NOT SYMMETRIC, and reading them as if they were is how this check
    produces a confidently wrong answer. Deeper sequencing inflates detection, so the gradient
    manufactures apparent UP-regulation in the deeper arm and nothing else. A ranking that runs
    WITH the gradient is the one depth can fake: there, a low or negative rho is real evidence of
    an artefact. A ranking that runs AGAINST it cannot have been produced by depth at all — those
    genes appeared despite the bias — and down-sampling costs power, so its rho drops for
    statistical reasons whether or not the effect is real. Such a ranking is reported as
    ``against_depth_low_power`` and must NOT be described as a depth artefact; judge it on
    ``n_depth_robust`` instead.

    Selection is by the Wilcoxon z, not by |log2fc| — on sparse data the largest fold-changes
    belong to the least-expressed genes, and a |log2fc| top-N fills with noise the check then
    truthfully reports as unsurvivable.

    A high rho means the ranking is not an artefact of depth. A rho near zero on a with-gradient
    ranking means depth decided it. A NEGATIVE rho there means the ranking inverts once depth is
    equalised — the strongest possible evidence that the original ordering was a detection
    artefact, and the finding a report must not interpret as biology.

    Reads the QC checkpoint's ``layers["counts"]`` (raw integer counts, kept by run_scanpy_qc
    before normalisation) — down-sampling log-normalised values would be meaningless.
    """
    try:
        sc = _import_scanpy()
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy ships with scanpy
        return _missing("numpy")

    work, art, figs, tables = _dirs(ctx)
    ckpt, err = _step_input(ctx, args, (work / "adata_clustered.h5ad", work / "adata_qc.h5ad"),
                            "no analysis checkpoint found — run run_scanpy_qc first (it writes "
                            "adata_qc.h5ad, including the raw `counts` layer this tool needs).")
    if err:
        return err

    groupby = str(_p("run_depth_matched_de", "groupby", args)).strip()
    reference = str(_p("run_depth_matched_de", "reference", args)).strip()
    stratify_by = str(_p("run_depth_matched_de", "stratify_by", args)).strip()
    n_genes = int(_p("run_depth_matched_de", "n_genes", args))
    min_cells = int(_p("run_depth_matched_de", "min_cells", args))
    seed = int(_p("run_depth_matched_de", "seed", args))
    min_ratio = float(_p("run_depth_matched_de", "min_ratio", args))

    adata = sc.read_h5ad(ckpt)
    obs_cols = list(adata.obs.columns)
    if groupby not in adata.obs:
        return {"status": "error", "error": f"groupby '{groupby}' not in obs; available: {obs_cols}"}
    levels = sorted(set(adata.obs[groupby].astype(str)))
    if not reference or reference not in levels:
        return {"status": "error",
                "error": (f"`reference` must name the CONTROL level of '{groupby}'. "
                          f"Levels: {levels}")}
    if stratify_by and stratify_by not in adata.obs:
        return {"status": "error",
                "error": f"stratify_by '{stratify_by}' not in obs; available: {obs_cols}"}
    if "counts" not in getattr(adata, "layers", {}):
        return {"status": "error",
                "error": ("the checkpoint has no raw `counts` layer, so depth cannot be matched — "
                          "down-sampling a log-normalised matrix is meaningless. Re-run "
                          "run_scanpy_qc (it stores counts before normalising).")}

    test = [lv for lv in levels if lv != reference]
    if len(test) != 1:
        return {"status": "error",
                "error": (f"'{groupby}' has levels {levels}; this check compares exactly TWO "
                          f"(reference '{reference}' vs one other). Subset the data or pass a "
                          "two-level condition column.")}
    test_level = test[0]

    strata = ([str(s) for s in sorted(set(adata.obs[stratify_by].astype(str)))]
              if stratify_by else ["all cells"])
    rng_seed = seed

    rows: list[dict[str, Any]] = []          # one per (stratum, direction)
    gene_rows: list[dict[str, Any]] = []     # one per gene kept
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []

    def _rank_frame(sub: Any) -> "tuple[dict[str, float], dict[str, float]] | None":
        """``({gene: log2fc}, {gene: wilcoxon z})`` for `test_level` vs `reference` on this
        (already log-norm) subset. The log2fc gives a gene its DIRECTION and its reported effect;
        the z-statistic is what the top-N is SELECTED on — see the note at the selection site."""
        try:
            sc.tl.rank_genes_groups(sub, groupby=groupby, groups=[test_level],
                                    reference=reference, method="wilcoxon", tie_correct=True,
                                    use_raw=False)
        except Exception:  # noqa: BLE001 - a stratum the test cannot run on is reported, not fatal
            return None
        res = sub.uns["rank_genes_groups"]
        names = [str(g) for g in res["names"][test_level]]
        lfc = [float(v) for v in res["logfoldchanges"][test_level]]
        z = [float(v) for v in res["scores"][test_level]]
        return dict(zip(names, lfc)), dict(zip(names, z))

    for stratum in strata:
        sub = (adata[adata.obs[stratify_by].astype(str) == stratum] if stratify_by else adata).copy()
        n_ref = int((sub.obs[groupby].astype(str) == reference).sum())
        n_test = int((sub.obs[groupby].astype(str) == test_level).sum())
        if min(n_ref, n_test) < min_cells:
            skipped.append({"group": stratum, "reason": f"{n_ref} {reference} / {n_test} "
                                                        f"{test_level} cells < min_cells={min_cells}"})
            continue

        counts = sub.layers["counts"]
        totals = np.asarray(counts.sum(axis=1)).ravel().astype(float)
        is_test = (sub.obs[groupby].astype(str) == test_level).to_numpy()
        med_test = float(np.median(totals[is_test])) if is_test.any() else 0.0
        med_ref = float(np.median(totals[~is_test])) if (~is_test).any() else 0.0
        if med_test <= 0 or med_ref <= 0:
            skipped.append({"group": stratum, "reason": "a zero median library size"})
            continue
        ratio = max(med_test, med_ref) / min(med_test, med_ref)
        deeper_is_test = med_test > med_ref
        if ratio < min_ratio:
            skipped.append({"group": stratum,
                            "reason": f"depth already matched (ratio {ratio:.2f} < {min_ratio})"})
            continue

        ranked = _rank_frame(sub)
        if ranked is None:
            skipped.append({"group": stratum, "reason": "the Wilcoxon contrast could not be run"})
            continue
        original, original_z = ranked

        # Quantile-match the deeper arm onto the shallower arm's library-size distribution. Rank i
        # of the deeper arm is given the same quantile of the shallower distribution, capped at the
        # cell's own total — a cell cannot be sampled up to counts it never had.
        deep_mask = is_test if deeper_is_test else ~is_test
        target = _depth_match_targets(totals[deep_mask], totals[~deep_mask])

        per_cell = totals.copy()
        per_cell[deep_mask] = np.maximum(1.0, np.floor(target))
        matched_ad = sub.copy()
        matched_ad.X = matched_ad.layers["counts"].copy()
        try:
            sc.pp.downsample_counts(matched_ad, counts_per_cell=per_cell.astype(int),
                                    random_state=rng_seed, replace=False)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"group": stratum, "reason": f"down-sampling failed: {exc}"})
            continue
        sc.pp.normalize_total(matched_ad, target_sum=1e4)
        sc.pp.log1p(matched_ad)
        matched = _rank_frame(matched_ad)
        if matched is None:
            skipped.append({"group": stratum, "reason": "the matched contrast could not be run"})
            continue
        matched_rank, matched_z = matched

        shared = [g for g in original if g in matched_rank]
        # Which direction does the depth gradient PUSH? Deeper sequencing inflates detection, so it
        # manufactures apparent up-regulation IN THE DEEPER ARM. log2fc here is test-vs-reference,
        # so the artefact-prone direction is "up" when the test arm is deeper and "down" when the
        # reference arm is. The other direction runs AGAINST the gradient: depth bias cannot
        # produce it, and a gene that shows up there did so despite the bias.
        with_depth_direction = _with_depth_direction(deeper_is_test)
        for direction, keep in (("up", lambda v: v > 0), ("down", lambda v: v < 0)):
            # SELECT BY THE TEST STATISTIC, NOT BY |log2fc|. On sparse single-cell data log2fc is
            # least stable exactly where expression is lowest, so a top-N taken on |log2fc| fills
            # with near-zero-expression genes — on the DDX41 retina run the MG "down" list came
            # back as Gm*/Riken clones plus ambient T-cell transcripts (Cd3e, Cd3d, Fyb), while
            # Glul (padj 1e-100), Crb1, Rlbp1 and Slc1a3 were never examined at all. The check then
            # reported that nothing survived — a true statement about a list of noise genes, which
            # was read as a statement about the dataset. The Wilcoxon z ranks by evidence, which is
            # the question being asked. Direction still comes from log2fc, which is what "up" and
            # "down" mean to a reader and what the gene table reports.
            top = sorted((g for g in shared if keep(original[g])),
                         key=lambda g: -abs(original_z[g]))[:n_genes]
            if len(top) < 3:
                continue
            o = [original[g] for g in top]
            m = [matched_rank[g] for g in top]
            rho = _spearman(o, m)
            # "Robust" = the gene keeps its SIGN and most of its EFFECT. Sign alone is too weak
            # (it passes a gene collapsing from 3.0 to 0.02; on a synthetic control a sign-only
            # rule called two thirds of pure background robust). Membership of the matched top-N
            # was too strong in the other direction: it demands the ORDER survive, which for 50
            # genes of comparable evidence is decided by noise — it returned 0 of 400 on the DDX41
            # run, including genes that kept their sign and two thirds of their effect. Sign plus
            # a magnitude floor answers the counter-example without being unpassable.
            against_depth = direction != with_depth_direction
            # Only meaningful WITH the gradient. Down-sampling pushes every gene toward looking
            # more down in the deeper arm, so against the gradient this count passes background at
            # 73% — reporting it would hand the write-up a number that certifies noise.
            robust = ([] if against_depth else
                      _depth_robust_genes(top, original, matched_rank, original_z, matched_z))
            # Kept as a secondary diagnostic only — it is informative, it is just not the verdict.
            matched_top = set(sorted((g for g in shared if keep(matched_rank[g])),
                                     key=lambda g: -abs(matched_z[g]))[:n_genes])
            kept_rank = [g for g in top if g in matched_top]
            verdict = _depth_verdict(rho, against_depth)
            rows.append({"group": stratum, "direction": direction, "n_genes": len(top),
                         "depth_ratio": round(ratio, 3),
                         "deeper_arm": test_level if deeper_is_test else reference,
                         "depth_direction": "against_depth" if against_depth else "with_depth",
                         "spearman_rho": None if rho is None else round(rho, 4),
                         "n_depth_robust": None if against_depth else len(robust),
                         "pct_depth_robust": (None if against_depth else
                                              round(100.0 * len(robust) / len(top), 1)),
                         "n_kept_top_rank": len(kept_rank),
                         "pct_kept_top_rank": round(100.0 * len(kept_rank) / len(top), 1),
                         "verdict": verdict})
            robust_set = set(robust)
            for g in top:
                gene_rows.append({"group": stratum, "direction": direction, "gene": g,
                                  "wilcoxon_z_original": round(original_z[g], 4),
                                  "log2fc_original": round(original[g], 4),
                                  "log2fc_depth_matched": round(matched_rank[g], 4),
                                  "depth_robust": "" if against_depth else g in robust_set})
            pct = 0.0 if against_depth else round(100.0 * len(robust) / len(top), 1)
            if verdict == "inverted":
                warnings.append(
                    f"{stratum} {direction}: the ranking INVERTS after depth matching "
                    f"(rho={rho:.2f}) and this direction runs WITH the {ratio:.2f}x depth "
                    f"gradient (deeper arm: {test_level if deeper_is_test else reference}) — the "
                    "ordering is a detection artefact, not biology, and must not be interpreted "
                    "as regulation.")
            elif verdict == "weak":
                warnings.append(
                    f"{stratum} {direction}: little of the ranking survives depth matching "
                    f"(rho={'n/a' if rho is None else f'{rho:.2f}'}) and this direction runs WITH "
                    f"the {ratio:.2f}x depth gradient — treat these genes as depth-sensitive. "
                    f"{len(robust)}/{len(top)} ({pct}%) kept their sign and at least "
                    f"{int(_DEPTH_ROBUST_MIN_FRACTION * 100)}% of their Wilcoxon z.")
            elif verdict == "against_depth_untestable":
                warnings.append(
                    f"{stratum} {direction}: this direction runs AGAINST the {ratio:.2f}x depth "
                    f"gradient (deeper arm: {test_level if deeper_is_test else reference}), so "
                    "depth bias CANNOT have manufactured these genes and a low rho "
                    f"(rho={'n/a' if rho is None else f'{rho:.2f}'}) is NOT evidence of an "
                    "artefact. But down-sampling also pushes every gene toward looking more down "
                    "in the deeper arm, so this check cannot confirm them either — it has no "
                    "power in this direction and reports no robustness count for it. These genes "
                    "are NEITHER validated NOR refuted here: say exactly that, and settle them "
                    "with replicated pseudobulk or an orthogonal assay.")

    if not rows:
        return {"status": "error", "step": "depth_matched_de",
                "error": ("no cell type could be checked — "
                          + ("; ".join(f"{s['group']}: {s['reason']}" for s in skipped[:6])
                             or "no groups were evaluated")),
                "skipped_groups": skipped}

    summary_csv = tables / "depth_matched_summary.csv"
    _write_table(summary_csv, rows,
                 ["group", "direction", "n_genes", "depth_ratio", "deeper_arm", "depth_direction",
                  "spearman_rho", "n_depth_robust", "pct_depth_robust",
                  "n_kept_top_rank", "pct_kept_top_rank", "verdict"])
    genes_csv = tables / "depth_matched_genes.csv"
    _write_table(genes_csv, gene_rows,
                 ["group", "direction", "gene", "wilcoxon_z_original", "log2fc_original",
                  "log2fc_depth_matched", "depth_robust"])

    figures: list[str] = []
    fig_path = _depth_matched_figure(figs, rows)
    if fig_path:
        figures.append(fig_path)

    preserved = [r for r in rows if r["verdict"] == "preserved"]
    inverted = [r for r in rows if r["verdict"] == "inverted"]
    against = [r for r in rows if r["verdict"] == "against_depth_untestable"]
    out: dict[str, Any] = {
        "status": "ok",
        "step": "depth_matched_de",
        "condition": groupby, "reference": reference, "tested": test_level,
        "stratify_by": stratify_by or None,
        "per_group": rows,
        "n_preserved": len(preserved), "n_inverted": len(inverted),
        "n_against_depth": len(against),
        "n_genes_depth_robust": sum(int(r["n_depth_robust"] or 0) for r in rows),
        "tables": [str(summary_csv), str(genes_csv)],
        "figures": figures,
        "read_from": _run_rel(ctx, ckpt),
        "skipped_groups": skipped,
        "interpretation": (
            f"{len(preserved)} of {len(rows)} (cell type x direction) rankings survive depth "
            f"matching; {len(inverted)} invert WITH the depth gradient and are artefacts. "
            f"{len(against)} ranking(s) run AGAINST the gradient: depth cannot have manufactured "
            "those, and down-sampling cannot confirm them either, so this tool has NO VERDICT on "
            "them — they are neither validated nor refuted, and a write-up must say so rather "
            "than counting them as failures. Never summarise this tool as 'nothing survived' "
            "without stating, per cell type, which direction ran with the gradient and which "
            "against it."),
    }
    if warnings:
        out["warnings"] = warnings
    return out


def _with_depth_direction(deeper_is_test: bool) -> str:
    """Which of "up"/"down" the depth gradient PUSHES, given which arm is deeper.

    Deeper sequencing inflates detection, so it manufactures apparent up-regulation in the deeper
    arm. log2fc is test-vs-reference, so that shows up as "up" when the TEST arm is the deeper one
    and as "down" when the REFERENCE arm is."""
    return "up" if deeper_is_test else "down"


def _depth_robust_genes(top: "list[str]", original: "dict[str, float]",
                        matched: "dict[str, float]",
                        original_z: "dict[str, float] | None" = None,
                        matched_z: "dict[str, float] | None" = None,
                        min_fraction: float = _DEPTH_ROBUST_MIN_FRACTION) -> "list[str]":
    """The genes in ``top`` that keep their SIGN and at least ``min_fraction`` of their EVIDENCE.

    Three rules were tried against ``experiments/depth_matched_validation`` and only the third
    survives its control:

    * sign alone called two thirds of pure background depth-robust;
    * membership of the matched ranking's own top-N demanded that the ORDER survive, which among
      genes of comparable evidence is decided by noise — 0 of 400 on the DDX41 retina run,
      including genes that kept their sign and two thirds of their effect;
    * sign plus a fraction of the original |log2fc| passes the up direction but is no brake at all
      on the down one: down-sampling the deeper arm removes counts, which pushes log2fc DOWN, so a
      magnitude floor is satisfied for free there. Measured, it certified 57 of 60 pure background
      genes in ``RealBio down``.

    So the floor is on the WILCOXON Z, not on log2fc. The z carries effect size and consistency
    together and regresses toward zero for noise once the down-sampled arm loses power, while a
    real effect holds. log2fc still decides the SIGN, which is what up/down means to a reader.
    ``original_z``/``matched_z`` are optional so the sign-and-effect behaviour stays available for
    callers that have no statistic (and for the unit tests that pin each rule)."""
    if original_z is None or matched_z is None:
        return [g for g in top
                if g in matched
                and (matched[g] > 0) == (original[g] > 0)
                and abs(matched[g]) >= min_fraction * abs(original[g])]
    return [g for g in top
            if g in matched and g in matched_z and g in original_z
            and (matched[g] > 0) == (original[g] > 0)
            and abs(matched_z[g]) >= min_fraction * abs(original_z[g])]


def _depth_verdict(rho: "float | None", against_depth: bool) -> str:
    """The per-(cell type, direction) verdict.

    A ranking that runs AGAINST the depth gradient gets its own verdict, because this check cannot
    adjudicate it in either direction. Depth bias cannot have manufactured it — so a low rho is not
    evidence of an artefact, and calling it "weak" is what let a run conclude "nothing survived"
    about genes depth could never have produced. But down-sampling the deeper arm also removes
    counts, which pushes every gene toward looking MORE down in that arm, so the surviving-effect
    test is satisfied for free: measured on the validation control it passed 73% of pure background
    (92% at the looser 0.5 floor). The honest report is that the question is out of this tool's
    reach — judge those genes on replicated pseudobulk, not on this."""
    if rho is not None and rho >= 0.5:
        return "preserved"
    if against_depth:
        return "against_depth_untestable"
    if rho is not None and rho < 0:
        return "inverted"
    return "weak"


def _depth_match_targets(deep_totals: "Any", shallow_totals: "Any") -> "Any":
    """Per-cell down-sampling targets that map the deeper arm onto the shallower arm's
    library-size distribution.

    QUANTILE matching, not "everyone down to the median": the arms differ in the SHAPE of their
    depth distribution as well as its centre, and flattening every cell to one number would
    destroy the within-arm variation the rank test reads. Cell of rank i in the deeper arm is
    given the same quantile of the shallower arm's distribution, so the two distributions coincide
    afterwards while each cell keeps its relative position.

    Capped at each cell's own total: down-sampling can only discard molecules, never invent them.
    The cap is what makes the result conservative — after it, the matched arm is at most as deep as
    the target, so any residual imbalance runs AGAINST calling a signal depth-driven.
    """
    import numpy as np

    deep = np.asarray(deep_totals, dtype=float)
    shallow = np.asarray(shallow_totals, dtype=float)
    if deep.size == 0 or shallow.size == 0:
        return deep
    order = np.argsort(deep, kind="stable")
    q = (np.arange(deep.size, dtype=float) + 0.5) / deep.size
    matched = np.quantile(np.sort(shallow), q)
    target = np.empty_like(deep)
    target[order] = matched
    return np.minimum(target, deep)


def _spearman(a: "list[float]", b: "list[float]") -> float | None:
    """Spearman's rho without a scipy dependency (ties averaged, as scipy does)."""
    n = len(a)
    if n < 3:
        return None

    def _ranks(v: "list[float]") -> "list[float]":
        order = sorted(range(n), key=lambda i: v[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((y - mb) ** 2 for y in rb) ** 0.5
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def _depth_matched_figure(figs: "Any", rows: "list[dict[str, Any]]") -> str:
    """One bar per (cell type, direction), so an inversion is visible rather than tabulated."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return ""
    usable = [r for r in rows if r.get("spearman_rho") is not None]
    if not usable:
        return ""
    labels = [f"{r['group']}\n{r['direction']}" for r in usable]
    vals = [float(r["spearman_rho"]) for r in usable]
    colors = ["#3b7dd8" if v >= 0.5 else "#c8721f" if v >= 0 else "#b3352e" for v in vals]
    fig, ax = plt.subplots(figsize=(max(4.0, 0.7 * len(usable) + 1.5), 3.6))
    ax.bar(range(len(usable)), vals, color=colors)
    ax.axhline(0.5, ls="--", lw=0.9, color="#666")
    ax.axhline(0.0, lw=0.9, color="#222")
    ax.set_xticks(range(len(usable)))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylim(-1.05, 1.05)
    ax.set_ylabel("Spearman rho\noriginal vs depth-matched")
    ax.set_title("How much of each ranking survives depth matching")
    fig.tight_layout()
    path = figs / "depth_matched_correlation.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return str(path)


def make_tool() -> HarnessTool:
    """The ``run_depth_matched_de`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
        "run_depth_matched_de",
        "Separate biology from sequencing depth. When the two arms differ in reads per cell, "
        "every ranking inherits that bias — a gene detected in more cells because more "
        "molecules were sampled looks up-regulated, and abundant-transcript pathways "
        "(translation, ribosome, RNA metabolism) move together in the deeper arm. This "
        "down-samples the deeper arm within each cell type until the per-cell UMI "
        "distributions match, re-runs the SAME Wilcoxon contrast, and Spearman-correlates the "
        "original ranking against the depth-matched one PER DIRECTION. Use it whenever the "
        "dataset profile flags a depth imbalance, and always before interpreting a "
        "pan-cell-type signature. rho >= 0.5 = preserved; rho < 0 = the ranking INVERTS once "
        "depth is equalised, i.e. a detection artefact that must not be reported as "
        "regulation. Reads the raw `counts` layer run_scanpy_qc stores, and writes "
        "`tables/depth_matched_summary.csv`, `tables/depth_matched_genes.csv` and a figure. "
        "Do NOT hand-write this as run_code: correlating per-gene fold-changes against a "
        "single median library size is not a computable operation.",
        # No ``extra`` here: PARAMS already declares all seven with a default AND a
        # description, and ``_schema`` does ``props.update(extra)`` LAST — so re-listing them
        # as extra silently overwrote the defaults with bare types. The model then saw no
        # default for n_genes/min_cells/seed/min_ratio and had to guess, which is exactly
        # what test_declared_params exists to prevent. (`input` is extra: it has no default
        # worth declaring in a protocol, only "the usual checkpoint".)
        _schema("run_depth_matched_de", input=_INPUT_SPEC),
        run_depth_matched_de,
        # It reads the raw counts layer and re-runs the contrast on cells, so it is on the
        # private-data path — unlike the two pathway tools, which only ever see gene symbols.
        reads_private_data=True, category="analysis",
    )
