"""The ``run_de`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/scrna_pack.py`` by ``scripts/refactor/split_tools.py``: the code is the old
module's text, verbatim, with only the imports rewritten. Helpers that several tools use live in
``aiscientist.tools._lib.scrna``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any
from .._lib.scrna import (
    _INPUT_SPEC,
    _dirs,
    _import_scanpy,
    _looks_like_celltype_column,
    _looks_like_condition_column,
    _missing,
    _p,
    _rel,
    _run_rel,
    _schema,
    _slug,
    _step_input,
    _write_table,
)
from ..sdk import HarnessTool


def _replication_note(adata: Any, condition_col: str, obs_cols: "list[Any]") -> str:
    """One sentence on how many SAMPLES back each arm of ``condition_col``.

    This is the number a condition contrast cannot be judged without — "n = 3 donors per arm", not
    "n = 4,812 cells" — and it is what decides which test is legal. Reports every obs column that
    could serve as the sample/donor/library id and how many distinct values it takes inside each
    arm. Says so plainly when there is none: "nothing in this object separates the condition from
    the individual" is not a missing detail, it is the finding."""
    cond = adata.obs[condition_col].astype(str)
    arms = sorted(set(cond))
    notes: list[str] = []
    for col in obs_cols:
        col = str(col)
        if col == condition_col or col.startswith("_") or not _looks_like_condition_column(col):
            continue
        vals = adata.obs[col].astype(str)
        total = int(vals.nunique())
        if total > 50:                       # a per-cell barcode, not a sample id
            continue
        per_arm = ", ".join(f"{a}: {int(vals[cond == a].nunique())}" for a in arms)
        notes.append(f"'{col}' takes {total} distinct value(s) overall ({per_arm} within each arm)")
    if not notes:
        return ("No obs column reads like a sample / donor / library id, so this object carries no "
                "replication information at all — nothing in it separates the condition from the "
                "individual. ")
    return "Replication available here: " + "; ".join(notes) + ". "


def _bh(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values, order-preserving. Local so the min_pct pre-filter can
    re-adjust over the genes ACTUALLY tested — reusing scanpy's padj after dropping rows would keep
    a correction computed over a universe two-thirds of which was never detectably expressed."""
    n = len(pvals)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: pvals[i])
    out = [0.0] * n
    prev = 1.0
    for rank_from_end in range(n, 0, -1):
        i = order[rank_from_end - 1]
        prev = min(prev, pvals[i] * n / rank_from_end)
        out[i] = prev
    return out


def _pct_detected(x: Any, mask: Any) -> Any:
    """Fraction of the cells in ``mask`` detecting each gene (any value > 0)."""
    import numpy as np
    n = int(mask.sum())
    if n == 0:
        return np.zeros(x.shape[1])
    nz = (x[np.asarray(mask)] > 0).sum(axis=0)
    return np.asarray(nz).ravel() / n


def _rank_rows(res: Any, key: str, label: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Rows for ONE ``rank_genes_groups`` result group, labelled with ``label``.

    ``label`` is deliberately separate from ``key``: in a stratified contrast the tested key is
    the condition level (``KO``) but the row's ``group`` must be the STRATUM (the cell type), since
    that is what ``run_enrichment`` and the report stratify by."""
    names = res["names"][key]
    n = len(names) if limit is None else min(int(limit), len(names))
    return [{
        "group": label,
        "gene": str(names[i]),
        "log2fc": float(res["logfoldchanges"][key][i]),
        "pval": float(res["pvals"][key][i]),
        "pval_adj": float(res["pvals_adj"][key][i]),
        "score": float(res["scores"][key][i]),
    } for i in range(n)]


def _significant_both_directions(rows: list[dict[str, Any]], padj_max: float, lfc_min: float,
                                 per_direction: int) -> tuple[list[dict], list[dict], dict[str, int]]:
    """The significant rows split UP / DOWN, each capped and ordered by adjusted p, plus the TRUE
    counts before capping.

    A contrast's down-regulated genes are half the biology, but ``rank_genes_groups`` returns
    genes ranked by score (descending), so any "take the first N rows" rule keeps only the
    up-regulated side. Enrichment on that list can only ever report up-regulated pathways.

    The third return value exists because reporting ``len(up)`` as "the number of significant
    genes" is not a count, it is the cap: with the default ``n_genes=50`` a real contrast comes
    back as "50 up, 50 down" for every cell type that has more than 50, and a reader — or a
    report writer — cannot tell 50 from 4,000. Truncating the TABLE is right (nobody needs 20,000
    rows in a preview); truncating the COUNT and still calling it a count is not."""
    # Both gates, matching run_enrichment's selection: an adjusted p AND an effect-size floor.
    # Two definitions of "significant" in one report — 9,483 by padj alone against 8,800 with the
    # lfc floor, on the same DDX41 run — is exactly the inconsistency this line closes.
    sig = [r for r in rows if r["pval_adj"] < padj_max and abs(r["log2fc"]) >= lfc_min]
    up_all = sorted([r for r in sig if r["log2fc"] > 0], key=lambda r: r["pval_adj"])
    down_all = sorted([r for r in sig if r["log2fc"] < 0], key=lambda r: r["pval_adj"])
    totals = {"up": len(up_all), "down": len(down_all)}
    return up_all[:per_direction], down_all[:per_direction], totals


def _volcano(figs: Path, label: str, rows: list[dict[str, Any]], padj_max: float,
             lfc_min: float) -> "Path | None":
    """Deterministic volcano for one contrast group. Optional: matplotlib is not guaranteed in a
    minimal env, and the tables are the real deliverable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    xs = [r["log2fc"] for r in rows]
    ys = [-math.log10(max(r["pval_adj"], 1e-300)) for r in rows]
    keep = [r["pval_adj"] < padj_max and abs(r["log2fc"]) >= lfc_min for r in rows]
    fig, ax = plt.subplots(figsize=(5, 4))
    ax.scatter([x for x, k in zip(xs, keep) if not k], [y for y, k in zip(ys, keep) if not k],
               s=4, c="lightgray")
    ax.scatter([x for x, k in zip(xs, keep) if k], [y for y, k in zip(ys, keep) if k],
               s=6, c="firebrick")
    ax.axvline(lfc_min, ls="--", lw=0.6, c="gray")
    ax.axvline(-lfc_min, ls="--", lw=0.6, c="gray")
    ax.set_xlabel("log2 fold change")
    ax.set_ylabel("-log10(adjusted p-value)")
    ax.set_title(f"Volcano — {label}"[:70])
    fig.tight_layout()
    path = figs / f"volcano_{_slug(label)}.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def run_de(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """``rank_genes_groups`` (Wilcoxon) — markers per group, or a condition-vs-reference contrast.

    ``groupby`` names the obs column whose levels are compared (default ``leiden``).
    ``reference`` is the baseline level; the default ``"rest"`` gives one-vs-rest markers, while a
    named level (e.g. ``"WT"``) gives a real contrast. ``stratify_by`` names an existing cell-type
    column and runs that contrast SEPARATELY within each cell type — the DEG protocol's step 3.
    Writes one ranked table per group, a combined ``de_<key>_all.csv``, the tested universe, and
    per-group ``.rnk`` files for preranked GSEA."""
    try:
        sc = _import_scanpy()
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    # Clustering is required only when the GROUPING comes from it. A dataset that carries its own
    # cell-type labels is analysed straight off the QC checkpoint — the DEG protocol explicitly
    # tells the planner to REUSE those labels and skip clustering, and hard-requiring
    # adata_clustered.h5ad here turned that documented path into an error, which pushed the model
    # into re-clustering the very data it had been told not to re-cluster.
    ckpt, err = _step_input(ctx, args, (work / "adata_clustered.h5ad", work / "adata_qc.h5ad"),
                            "no analysis checkpoint found — run run_scanpy_qc first (it writes "
                            "adata_qc.h5ad). run_clustering is needed only when the dataset has no "
                            "cell-type / cluster label column of its own.")
    if err:
        return err

    groupby = str(_p("run_de", "groupby", args))
    method = str(_p("run_de", "method", args))
    n_genes = int(_p("run_de", "n_genes", args))
    reference = str(_p("run_de", "reference", args)).strip() or "rest"
    stratify_by = str(_p("run_de", "stratify_by", args)).strip()
    min_cells = int(_p("run_de", "min_cells", args))
    min_pct = float(_p("run_de", "min_pct", args))
    tie_correct = bool(_p("run_de", "tie_correct", args))
    padj_max = float(_p("run_de", "padj", args))
    lfc_min = float(_p("run_de", "lfc", args))
    sel = args.get("groups")
    sel = [str(g) for g in sel] if isinstance(sel, list) and sel else None
    contrast = reference != "rest"

    adata = sc.read_h5ad(ckpt)
    obs_cols = list(adata.obs.columns)
    if groupby not in adata.obs:
        return {"status": "error", "error": f"groupby '{groupby}' not in obs; available: {obs_cols}"}
    if stratify_by and stratify_by not in adata.obs:
        return {"status": "error",
                "error": f"stratify_by '{stratify_by}' not in obs; available: {obs_cols}"}
    if stratify_by and stratify_by == groupby:
        return {"status": "error",
                "error": (f"stratify_by and groupby are both '{groupby}' — stratify_by must be the "
                          "CELL-TYPE column and groupby the CONDITION column.")}
    levels = sorted(set(adata.obs[groupby].astype(str)))
    if contrast and reference not in levels:
        return {"status": "error",
                "error": f"reference '{reference}' is not a level of '{groupby}'; levels: {levels}"}
    if sel:
        unknown = [g for g in sel if g not in levels]
        if unknown:
            return {"status": "error",
                    "error": f"groups {unknown} are not levels of '{groupby}'; levels: {levels}"}
    if stratify_by and not contrast:
        return {"status": "error",
                "error": ("stratify_by is only meaningful for a CONTRAST — also pass `reference` "
                          "(the control level of '" + groupby + "', e.g. \"WT\"). Levels: "
                          f"{levels}")}

    # --- pseudoreplication guard -------------------------------------------------------------
    # A per-cell Wilcoxon ACROSS an experimental condition treats each cell as an independent
    # observation of that condition. It is not: every cell from one animal is one observation of
    # that animal. The p-values are then anti-conservative by orders of magnitude — measured on a
    # synthetic 4-donor design where exactly 1 of 400 genes differed, this test called 310 of 400
    # significant and the pseudobulk test called 0.
    #
    # This guard existed once and was dropped in a rewrite. Its absence is how a production run
    # pooled 15,307 retina cells across DDX41/WT — ignoring an 11-level `majorclass` column that
    # was right there — and published 100 "highly significant" markers whose adjusted p-values
    # were reported as indistinguishable from zero.
    #
    # It refuses ONLY the shape that is unambiguously wrong: a condition column with NO cell-type
    # stratification, i.e. every cell type pooled, where a shift in cell-type COMPOSITION between
    # the arms is indistinguishable from a change in expression. The stratified contrast
    # (`reference` + `stratify_by`) is the protocol's documented no-replicates path — still
    # cell-level, so it warns (below) rather than refuses.
    condition_like = _looks_like_condition_column(groupby) and len(levels) <= 3
    if condition_like and not stratify_by and not bool(args.get("force")):
        celltype_cols = [c for c in obs_cols if _looks_like_celltype_column(str(c))]
        return {
            "status": "error",
            "step": "de",
            "error": (
                f"groupby '{groupby}' looks like an experimental CONDITION column ({len(levels)} "
                f"levels: {levels}), not a cell-type/cluster label, and no `stratify_by` was "
                "given — so this would pool every cell type and run a per-cell Wilcoxon across "
                "conditions. That is pseudoreplication, and a difference in cell-type COMPOSITION "
                "between the arms would come out looking exactly like differential expression. "
                + _replication_note(adata, groupby, obs_cols)
                + (f"This dataset already carries cell-type labels: {celltype_cols[:4]}. "
                   if celltype_cols else "")
                + "Choose one: (a) `run_pseudobulk_de(sample_key=..., condition_key="
                f"'{groupby}', group_key=<cell-type column>)` — the valid test, needs >=2 samples "
                f"per arm; (b) `run_de(groupby='{groupby}', reference=<control level>, "
                "stratify_by=<cell-type column>)` — a cell-level ranking WITHIN each cell type, "
                "honest only as an exploratory ranking, never as inferential DE; (c) `force=true` "
                "to run this pooled marker test anyway, which the write-up must then label as "
                "pseudoreplicated and non-inferential."
            ),
            "levels": levels,
            "celltype_columns": celltype_cols,
        }

    # The table/figure key: a stratified contrast is reported PER CELL TYPE, so the cell-type
    # column names the outputs; everything else is keyed by the compared column, as before.
    key = stratify_by or groupby
    fields = ["group", "gene", "log2fc", "pval", "pval_adj", "score"]
    combined: list[dict[str, Any]] = []
    top_by_group: dict[str, list[str]] = {}
    rows_by_group: dict[str, int] = {}
    sig_by_group: dict[str, dict[str, int]] = {}
    rank_tables: dict[str, str] = {}
    universe: set[str] = set()
    figures: list[str] = []
    skipped: list[dict[str, Any]] = []
    wrote_uns = False

    def _emit_group(label: str, rows_full: list[dict[str, Any]]) -> None:
        """Write one group's tables + rank file and fold it into the combined/summary state."""
        # Per-group table: for a contrast the FULL tested table (both directions, ordered by
        # adjusted p) is the analyst's deliverable; for markers keep the historical top-N.
        rows_file = (sorted(rows_full, key=lambda r: r["pval_adj"]) if contrast
                     else rows_full[:n_genes])
        _write_table(tables / f"de_{key}_{_slug(label)}.csv", rows_file, fields)
        rows_by_group[label] = len(rows_file)
        if contrast:
            up, down, totals = _significant_both_directions(rows_full, padj_max, lfc_min, n_genes)
            combined.extend(up + down)
            # The TRUE counts, plus whether the reported table was truncated — so a write-up can
            # say "312 significant, top 50 shown" instead of "50 significant".
            sig_by_group[label] = {"up": totals["up"], "down": totals["down"],
                                   "shown_up": len(up), "shown_down": len(down),
                                   "truncated": totals["up"] > len(up) or totals["down"] > len(down)}
            top_by_group[label] = [r["gene"] for r in (up + down)[:10]]
            fig_path = _volcano(figs, label, rows_full, padj_max, lfc_min)
            if fig_path is not None:
                figures.append(_rel(art, fig_path))
        else:
            combined.extend(rows_file)
            top_by_group[label] = [r["gene"] for r in rows_file[:10]]
        universe.update(r["gene"] for r in rows_full)
        rnk = tables / f"rank_{key}_{_slug(label)}.rnk"
        rnk.write_text("".join(f"{r['gene']}\t{r['score']:.6g}\n" for r in rows_full),
                       encoding="utf-8")
        rank_tables[label] = rnk.name

    dropped_by_min_pct: dict[str, int] = {}
    condition: "str | None" = None       # the one non-reference level of a stratified contrast
    cells_by_group: dict[str, dict[str, int]] = {}

    def _min_pct_prune(rows_full: list, x: Any, m_a: Any, m_b: Any,
                       gene_names: list) -> list:
        """Drop genes detected in fewer than ``min_pct`` of the cells of BOTH populations, then
        re-adjust BH over the genes actually tested. Detection is a property of the data, not of
        the p-values, so this is legitimate independent filtering — what it fixes is the
        denominator (and the divide-by-zero fold-changes of never-detected genes)."""
        if min_pct <= 0:
            return rows_full
        import numpy as np
        pa = _pct_detected(x, np.asarray(m_a))
        pb = _pct_detected(x, np.asarray(m_b))
        ok = {gene_names[i] for i in range(len(gene_names))
              if pa[i] >= min_pct or pb[i] >= min_pct}
        kept = [r for r in rows_full if r["gene"] in ok]
        for r, q in zip(kept, _bh([r["pval"] for r in kept])):
            r["pval_adj"] = q
        label = rows_full[0]["group"] if rows_full else "?"
        dropped_by_min_pct[label] = len(rows_full) - len(kept)
        return kept

    if not stratify_by:
        kwargs: dict[str, Any] = {"method": method, "use_raw": True}
        if method == "wilcoxon":
            kwargs["tie_correct"] = tie_correct
        if sel:
            kwargs["groups"] = sel
        if contrast:
            kwargs["reference"] = reference
        sc.tl.rank_genes_groups(adata, groupby=groupby, **kwargs)
        adata.write(work / "adata_de.h5ad")
        wrote_uns = True
        res = adata.uns["rank_genes_groups"]
        raw_x = adata.raw.X if adata.raw is not None else adata.X
        raw_names = (list(adata.raw.var_names) if adata.raw is not None
                     else list(adata.var_names))
        col = adata.obs[groupby].astype(str).values
        for grp in list(res["names"].dtype.names):
            m_a = col == str(grp)
            m_b = (col == reference) if contrast else ~m_a
            _emit_group(str(grp), _min_pct_prune(_rank_rows(res, grp, str(grp)),
                                                 raw_x, m_a, m_b, raw_names))
    else:
        # Stratified contrast: one comparison per cell type, on that cell type's cells only.
        tested = sel or [lv for lv in levels if lv != reference]
        if len(tested) != 1:
            return {"status": "error",
                    "error": (f"'{groupby}' has {len(tested)} non-reference levels ({tested}); a "
                              "stratified contrast compares exactly ONE condition against the "
                              "reference — pass `groups: [\"<condition>\"]` to choose it.")}
        condition = tested[0]
        cond_col = adata.obs[groupby].astype(str)
        strata = sorted(set(adata.obs[stratify_by].astype(str)))
        for ct in strata:
            in_ct = adata.obs[stratify_by].astype(str) == ct
            n_cond = int((in_ct & (cond_col == condition)).sum())
            n_ref = int((in_ct & (cond_col == reference)).sum())
            cells_by_group[ct] = {condition: n_cond, reference: n_ref}
            if n_cond < min_cells or n_ref < min_cells:
                skipped.append({"group": ct, "n_condition": n_cond, "n_reference": n_ref,
                                "reason": f"fewer than min_cells={min_cells} in one or both arms"})
                continue
            sub = adata[in_ct & cond_col.isin([condition, reference])].copy()
            try:
                sc.tl.rank_genes_groups(
                    sub, groupby=groupby, groups=[condition], reference=reference,
                    method=method, use_raw=True,
                    **({"tie_correct": tie_correct} if method == "wilcoxon" else {}))
            except Exception as exc:  # noqa: BLE001 - one bad stratum must not fail the step
                skipped.append({"group": ct, "n_condition": n_cond, "n_reference": n_ref,
                                "reason": f"{type(exc).__name__}: {exc}"})
                continue
            sub_x = sub.raw.X if sub.raw is not None else sub.X
            sub_names = (list(sub.raw.var_names) if sub.raw is not None
                         else list(sub.var_names))
            sub_col = sub.obs[groupby].astype(str).values
            _emit_group(ct, _min_pct_prune(
                _rank_rows(sub.uns["rank_genes_groups"], condition, ct),
                sub_x, sub_col == condition, sub_col == reference, sub_names))
        # Keep the DE checkpoint's contract: annotation skills read `rank_genes_groups` out of
        # adata_de.h5ad, and a stratified run has no single global result to put there — so do
        # NOT clobber a marker DE that an earlier step may have written.
        de_ckpt = work / "adata_de.h5ad"
        if not de_ckpt.exists():
            adata.write(de_ckpt)

    _write_table(tables / f"de_{key}_all.csv", combined, fields)
    # --- the COMPLETE tested list, alongside the tables above --------------------------
    # The truncated tables can support neither of the two things downstream pathway analysis
    # actually needs, so both are written here in full:
    #   * a preranked GSEA consumes EVERY tested gene in rank order (a top-50 list has no tail,
    #     and the tail is where a coordinated-but-modest pathway shift shows up);
    #   * ORA's background must be the universe that was actually tested, not a round number.
    # Both go to disk only — `raw_data_to_llm` stays False and neither is returned to the model.
    universe_path = tables / f"de_{key}_universe.txt"
    universe_path.write_text("\n".join(sorted(universe)) + "\n", encoding="utf-8")
    # Slug → the real label, so a later step reports "Club/Secretory" and not "Club_Secretory".
    _write_table(tables / f"rank_{key}_index.csv",
                 [{"slug": _slug(g), "group": g} for g in rank_tables], ["slug", "group"])

    sc.settings.figdir = str(figs)
    if wrote_uns:
        # scanpy names this file after the column it grouped by — hardcoding "leiden" pointed the
        # returned evidence path at a file that does not exist for any other groupby.
        sc.pl.rank_genes_groups(adata, n_genes=15, sharey=False, show=False, save="_de.png")
        rank_fig = figs / f"rank_genes_groups_{groupby}_de.png"
        if rank_fig.exists():
            figures.append(_rel(art, rank_fig))
        if not contrast:
            marker_genes = {g: top_by_group[g][:min(5, n_genes)] for g in top_by_group}
            try:
                sc.pl.dotplot(adata, marker_genes, groupby=groupby, show=False, save="_markers.png")
            except Exception:  # noqa: BLE001 - dotplot can fail on degenerate inputs
                pass
            dot_fig = figs / "dotplot__markers.png"
            if dot_fig.exists():
                figures.append(_rel(art, dot_fig))

    out: dict[str, Any] = {
        "status": "ok",
        "step": "de",
        "groupby": groupby,
        "method": method,
        # The comparison in words, so the report states what was actually tested instead of
        # inferring it from the plan text.
        "comparison": (f"{groupby}: each level vs rest" if not contrast
                       else f"{groupby}: {'/'.join(sel or [lv for lv in levels if lv != reference])}"
                            f" vs {reference} (reference={reference})"
                            + (f", stratified by {stratify_by}" if stratify_by else "")),
        "reference": reference,
        # Which level `n_condition` counts, by name. Run 8847d521ba32's answer read the skipped
        # groups' n_condition / n_reference the wrong way round and swapped every pair.
        **({"condition": condition} if condition else {}),
        "stratify_by": stratify_by or None,
        "table_key": key,
        "n_groups": len(rows_by_group),
        # Ground-truth counts so a reviewer/Critic reports the REAL DE size, not the length of
        # the capped ``top_genes_by_group`` preview below (that field is a first-≤10 sample, and
        # a Critic that counted it once mis-stated "10 genes/group" when de_<grp>.csv held 50).
        "n_genes_per_group": int(n_genes),                 # cap per group (per DIRECTION if contrast)
        "de_rows_by_group": rows_by_group,                 # ACTUAL rows written per de_<grp>.csv
        "de_rows_total": len(combined),                    # rows in de_<key>_all.csv
        "top_genes_by_group": top_by_group,                # PREVIEW ONLY: first ≤10 symbols/group
        # The complete tested list — what run_gsea_prerank ranks over and what run_enrichment
        # uses as its ORA background. Reported so the methods section can state the real universe.
        "tested_universe_size": len(universe),
        "min_pct": min_pct,
        "genes_dropped_by_min_pct": dropped_by_min_pct,
        "tie_correct": tie_correct if method == "wilcoxon" else None,
        "universe_table": _rel(art, universe_path),
        "rank_tables": rank_tables,
        "tables": [_rel(art, tables / f"de_{key}_all.csv")],
        # The exact column names, so a follow-up script does not have to guess: a production
        # run_code step indexed the DE table by the CONDITION column ("sampleid") and got a
        # KeyError — the table is keyed by `group` (the stratum), and the condition is implicit
        # in `comparison` above.
        "table_columns": list(fields),
        "figures": figures,
        "read_from": _run_rel(ctx, ckpt),
        "checkpoint": "adata_de.h5ad",
        "raw_data_to_llm": False,
    }
    # --- self-diagnosis -------------------------------------------------------------
    warnings: list[str] = []
    # Every cell-level test across a CONDITION is pseudoreplicated, including the two shapes the
    # guard above deliberately lets through. Neither is a bug — they are the documented paths for
    # "no replicates" and "I meant it" — but a result that reaches a report unlabelled reads as
    # ordinary differential expression, so each carries its own label out.
    if condition_like and stratify_by:
        out["inference"] = "exploratory_ranking"
        warnings.append(
            f"'{groupby}' is a CONDITION column, so this is a per-cell test within each "
            f"'{stratify_by}'. Stratifying removes the composition confound but NOT the "
            "pseudoreplication: report these as an exploratory ranking with effect sizes, not as "
            "differential expression with valid p-values. "
            + _replication_note(adata, groupby, obs_cols))
    elif condition_like:
        out["inference"] = "pseudoreplicated"
        warnings.append(
            f"`force=true` was used to run a pooled per-cell test across the CONDITION column "
            f"'{groupby}'. The p-values are pseudoreplicated AND every cell type is pooled, so a "
            "shift in cell-type composition between the arms is indistinguishable from a change "
            "in expression here. This must be labelled non-inferential wherever it is reported. "
            + _replication_note(adata, groupby, obs_cols))
    if contrast:
        # Same-direction skew in (nearly) every stratum is the signature of a TECHNICAL
        # difference between the arms — depth, capture, quality — not of a biological programme
        # that happens to switch on in every cell type at once. On the DDX41 object the arms
        # differ 1.6x in median depth and every stratum came back with up >> down and translation
        # genes on top; the write-up narrated that as DDX41 biology. Say it here, where the
        # numbers are, so the Critic and the writer see it before the interpretation is written.
        skewed = [(g, v["up"], v["down"]) for g, v in sig_by_group.items()
                  if (v["up"] + v["down"]) >= 100
                  and max(v["up"], v["down"]) >= 5 * max(1, min(v["up"], v["down"]))]
        if len(skewed) >= 2 and len(skewed) >= 0.6 * max(1, len(sig_by_group)):
            same_dir = (all(u > d for _, u, d in skewed) or all(d > u for _, u, d in skewed))
            if same_dir:
                direction = "up" if skewed[0][1] > skewed[0][2] else "down"
                warnings.append(
                    f"DIRECTION BIAS: {len(skewed)} of {len(sig_by_group)} strata show a strong "
                    f"same-direction skew ({direction} >> {'down' if direction == 'up' else 'up'}: "
                    + ", ".join(f"{g} {u}/{d}" for g, u, d in skewed[:5])
                    + "). A shift that points the same way in every cell type is consistent with "
                    "a technical difference between the arms (sequencing depth, capture, cell "
                    "quality) rather than a biological programme; check the per-arm depth in the "
                    "dataset profile before interpreting it, and if the arms differ in depth report "
                    "these as depth-confounded.")
                out["direction_bias"] = {"direction": direction,
                                         "strata": [g for g, _, _ in skewed]}
        # Extreme fold-changes are detection artifacts, not expression changes: |log2FC| >= 5 is
        # a >=32x ratio, which in sparse per-cell data almost always means near-zero detection in
        # one arm (run 97dfc89dc5aa's report quoted "Col25a1 log2FC ~ 8.74" as a lead finding — a
        # reviewer's first catch). Flag them where the numbers are born, so the Critic and the
        # writer see the caveat next to the value.
        extreme = [r for r in combined if abs(r.get("log2fc") or 0) >= 5.0]
        if extreme:
            out["extreme_fc_genes"] = [f"{r['group']}:{r['gene']}" for r in extreme[:20]]
            warnings.append(
                f"EXTREME FOLD-CHANGES: {len(extreme)} ranked gene(s) have |log2FC| >= 5 (>=32x), "
                "e.g. " + ", ".join(f"{r['gene']} in {r['group']} ({r['log2fc']:+.1f})" for r in extreme[:3])
                + ". At this scale the ratio almost always reflects near-zero detection in one arm "
                "(a detection artifact), not a real expression change — do not quote these "
                "fold-changes as findings without checking the per-arm detection fractions.")
        out["significant_by_group"] = sig_by_group          # {group: {up, down}} at padj_max
        out["padj_threshold"] = padj_max
        out["significance"] = {"padj_max": padj_max, "abs_log2fc_min": lfc_min}
        out["n_significant_total"] = sum(v["up"] + v["down"] for v in sig_by_group.values())
        # Each stratum is its own BH-corrected test family; their sum controls no FDR of its own.
        out["n_significant_total_note"] = (
            "a bookkeeping sum over strata that were each BH-corrected independently — quote the "
            "per-stratum counts; this total is not one FDR-controlled family")
        # State the cap explicitly next to the counts. A number that came out of a truncation and
        # a number that came out of the data look identical in a Methods sentence.
        out["n_genes_cap_per_direction"] = int(n_genes)
        if any(v.get("truncated") for v in sig_by_group.values()):
            out["table_truncation_note"] = (
                f"significant_by_group gives the TRUE counts; the written tables and the gene "
                f"lists passed downstream keep only the top {n_genes} per direction per group. "
                "Report the true count and say the table is a top-N view of it.")
        if not combined:
            out["note"] = (f"no gene reached adjusted p < {padj_max} in any group — the contrast "
                           "ran, it simply found nothing significant. Do NOT report DEGs for it.")
            warnings.append(
                f"The contrast found NOTHING significant at adjusted p < {padj_max} in any group. "
                "That is a result: report it as no detectable difference, and do NOT present the "
                "top-ranked genes as if they were differentially expressed.")
        one_sided = [g for g, c in sig_by_group.items() if (c["up"] == 0) != (c["down"] == 0)]
        if one_sided:
            warnings.append(
                f"{len(one_sided)} group(s) are significant in ONE direction only "
                f"({', '.join(one_sided[:6])}). Check this is biology and not a normalisation or "
                "composition artefact before interpreting it as a coherent programme.")
    else:
        # Markers from a handful of cells are unstable, and nothing in the marker path had a
        # cell-count floor at all — the contrast path's min_cells does not apply here.
        try:
            counts = {str(k): int(v) for k, v in adata.obs[groupby].value_counts().items()}
        except Exception:  # noqa: BLE001 - diagnosis must never break the tool
            counts = {}
        thin = {g: n for g, n in counts.items() if g in rows_by_group and n < min_cells}
        if thin:
            warnings.append(
                f"{len(thin)} group(s) have fewer than {min_cells} cells "
                f"({', '.join(f'{g}:{n}' for g, n in list(thin.items())[:8])}). Their marker lists "
                "rest on very few cells — say so rather than reporting them like the rest.")
            out["small_groups"] = thin
    if skipped:
        # Cell types dropped for lack of cells are a RESULT, not an implementation detail: the
        # report must not imply the contrast covered every cell type.
        out["skipped_groups"] = skipped
        warnings.append(
            f"{len(skipped)} group(s) were NOT tested for too few cells in one arm "
            f"({', '.join(str(s['group']) for s in skipped[:6])}). The analysis does not cover them "
            "— the report must say so rather than implying full coverage.")
    out["warnings"] = warnings
    if cells_by_group:
        # Cells per arm in EVERY stratum, tested or skipped: the split a write-up tabulates, and
        # what the Critic's count check (agents/step_numbers.py) holds a stated split to. The count
        # check reads the full result; the models' shortened views keep the warnings wherever they
        # sit (research_harness._is_reporting_key) and drop data from the end, so this is among the
        # first keys a long result loses from them.
        out["cells_by_group_and_arm"] = cells_by_group
    # Persist the inference label NEXT TO the tables. `out["inference"]` reaches the model and the
    # report, but run_enrichment reads the DE tables off DISK and never sees this dict — which is
    # how a plan could declare p-values non-inferential in one step and then gate the enrichment
    # input on padj<0.05 in the next, laundering a pseudoreplicated p-value into "significantly
    # shifted genes". A sidecar makes the label travel with the data.
    if out.get("inference"):
        try:
            (tables / f"de_{key}_inference.json").write_text(
                json.dumps({"inference": out["inference"], "groupby": groupby,
                            "reference": reference, "stratify_by": stratify_by or None}),
                encoding="utf-8")
        except OSError:
            pass    # a sidecar we could not write must never fail the DE step itself
    return out


def make_tool() -> HarnessTool:
    """The ``run_de`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
        "run_de",
        "Differential expression via rank_genes_groups (Wilcoxon), in EITHER of two shapes. "
        "(1) MARKERS (default): each level of `groupby` vs the rest — 'what defines this "
        "cluster'. (2) CONTRAST: pass `reference` (the CONTROL level of `groupby`, e.g. "
        "\"WT\") to compare condition vs control instead of vs rest; add `stratify_by` (an "
        "EXISTING cell-type label column) to run that contrast SEPARATELY WITHIN EACH CELL "
        "TYPE. Use (2) for a KO-vs-WT / disease-vs-control DEG study whose arms have fewer than "
        "2 replicate samples each; with >=2 samples per arm use run_pseudobulk_de instead — "
        "(2) treats cells as independent, so its p-values are pseudoreplicated. Either way do "
        "NOT hand-write the contrast in run_code, and do NOT re-cluster a dataset that already "
        "has labels. "
        "Reads `adata_clustered.h5ad` if present, otherwise `adata_qc.h5ad`, so a labeled "
        "dataset needs run_scanpy_qc ONLY (run_clustering is for unlabeled data). "
        "Writes `work/adata_de.h5ad` and CSV tables `tables/de_<key>_all.csv` (+ one per "
        "group), where <key> is `stratify_by` when stratified, else `groupby`. The tables "
        "have EXACTLY these columns: `group,gene,log2fc,pval,pval_adj,score` — if you ever "
        "read a DE table in run_code, use THOSE names (NOT Seurat-style "
        "`gene_name`/`p_val_adj`/`avg_log2FC`). A contrast also writes a volcano per group and "
        "reports `significant_by_group` (up/down counts) plus `skipped_groups` (cell types "
        "with too few cells in an arm — report those as not covered). `n_genes` caps rows per "
        "group, PER DIRECTION for a contrast.",
        _schema("run_de", groups={
            "type": "array", "items": {"type": "string"},
            "description": "restrict the comparison to these levels of groupby"},
            input=_INPUT_SPEC),
        run_de,
        reads_private_data=True, category="analysis", requires=("scanpy",),
    )
