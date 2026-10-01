"""The ``run_gsea_prerank`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/scrna_pack.py`` by ``scripts/refactor/split_tools.py``: the code is the old
module's text, verbatim, with only the imports rewritten. Helpers that several tools use live in
``aiscientist.tools._lib.scrna``.
"""

from __future__ import annotations

import csv
from typing import Any
from .._lib.scrna import (
    _DEFAULT_GENE_SETS,
    _dirs,
    _genesets_dir,
    _missing,
    _rel,
    _run_files,
    _run_rel,
    _slug,
    _write_table,
)
from ..sdk import HarnessTool


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

    labels: dict[str, str] = {}
    groupby = str(args.get("groupby", "")).strip()
    from_input = bool(str(args.get("input") or "").strip())
    if from_input:
        # Ranked lists the caller names — e.g. written by run_code from its own contrast. Each
        # file is one group, named by its file stem.
        rnk_files, why = _run_files(ctx, args["input"], (".rnk",), pattern_ok=True)
        if not rnk_files:
            return {"status": "error", "step": "gsea_prerank", "error": why}
        prefix = ""
    else:
        # Resolve the grouping from run_de's slug index rather than by splitting filenames: a
        # groupby is routinely itself underscored (`cell_type`, `major_class`), so any positional
        # split of `rank_cell_type_Club_Secretory` guesses the boundary wrong.
        if not groupby:
            idx_files = sorted(tables.glob("rank_*_index.csv"))
            # Same preference as run_enrichment: an ANNOTATED grouping carries more biology than
            # raw leiden ids, so if both exist rank the annotated one.
            idx_files = [p for p in idx_files if p.name != "rank_leiden_index.csv"] or idx_files
            if idx_files:
                groupby = idx_files[0].name[len("rank_"):-len("_index.csv")]
        if groupby:
            index_csv = tables / f"rank_{groupby}_index.csv"
            if index_csv.exists():
                with index_csv.open(encoding="utf-8") as fh:
                    labels = {row["slug"]: row["group"] for row in csv.DictReader(fh)}
        rnk_files = sorted(tables.glob(f"rank_{groupby}_*.rnk") if groupby
                           else tables.glob("rank_*.rnk"))
        prefix = f"rank_{groupby}_" if groupby else "rank_"
        if not rnk_files:
            return {"status": "error", "step": "gsea_prerank",
                    "error": ("no ranked gene lists found — run run_de first (it writes "
                              "tables/rank_<groupby>_<group>.rnk covering every tested gene), or "
                              "pass `input` naming .rnk files of your own.")}

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
                   "seed": seed, "fdr_max": fdr_max,
                   # A caller's own .rnk files carry whatever statistic THEY ranked by.
                   "ranking_statistic": "as supplied in `input`" if from_input else "wilcoxon_z"},
        "read_from": [_run_rel(ctx, p) for p in rnk_files][:40],
        "tables": [_rel(art, tables / f"gsea_{_slug(g)}.csv") for g in results],
        "errors": errors,
        "note": ("Preranked GSEA and ORA (run_enrichment) test different inputs under different "
                 "null hypotheses — they are NOT expected to agree. Enrichment is association "
                 "with an expression programme, not evidence of pathway activity or causation."),
        "raw_data_to_llm": False,
    }


def make_tool() -> HarnessTool:
    """The ``run_gsea_prerank`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
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
            "seed": {"type": "integer"},
            "input": {
                "type": "string", "default": "",
                "description": (
                    "ranked gene list(s) to test INSTEAD of the rank files run_de wrote — a "
                    ".rnk file (gene<TAB>score per line) or a file-name pattern such as "
                    "`my_rank_*.rnk`, inside this run's work/ or artifacts/ directory. Each file "
                    "is one group, named by its file stem, ranked by the score as given. Empty "
                    "= run_de's rank files.")}}},
        run_gsea_prerank,
        reads_private_data=False, category="analysis", requires=("gseapy",),
    )
