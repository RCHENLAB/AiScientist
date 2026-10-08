"""The ``run_enrichment`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/scrna_pack.py`` by ``scripts/refactor/split_tools.py``: the code is the old
module's text, verbatim, with only the imports rewritten. Helpers that several tools use live in
``aiscientist.tools._lib.scrna``.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any
from .._lib.scrna import (
    _DEFAULT_GENE_SETS,
    _dirs,
    _genesets_dir,
    _missing,
    _p,
    _rel,
    _schema,
    _slug,
    _write_table,
)
from ..sdk import HarnessTool


# Standard Enrichr libraries — every one of these has hundreds to thousands of term sets, so a
# tiny file under one of these names is a broken download, not a deliberate small gene set.
_KNOWN_LARGE_LIBS = frozenset({*_DEFAULT_GENE_SETS, "KEGG_2021_Human", "GO_Molecular_Function_2023",
                               "GO_Cellular_Component_2023", "WikiPathways_2024_Human"})


# The size check applies ONLY to libraries we KNOW are large — the standard Enrichr downloads.
# A user-supplied custom .gmt may legitimately hold two curated sets, and refusing that would be
# hostile; a two-line GO_Biological_Process_2023 is always a broken download.
# (Hallmark, the smallest standard library we ship, has 50 term sets.)
_MIN_GMT_TERMS = 10


def _gmt_term_count(path: Path) -> int:
    """Usable term sets in a ``.gmt``: lines with a name, a description, and >=1 gene."""
    n = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = [p for p in line.rstrip("\n").split("\t") if p.strip()]
                if len(parts) >= 3:
                    n += 1
    except OSError:
        return 0
    return n


def _enrichment_input_rows(rows: "list[dict[str, Any]]", lfc_min: float, padj_max: float,
                           descriptive: bool) -> "list[tuple[str, float]]":
    """Which DE genes enter the over-representation test.

    Normally both gates apply: adjusted p below ``padj_max`` AND |log2FC| at or above ``lfc_min``.
    Pairing an effect-size floor with a significance cutoff is what stops a list of thousands of
    barely-changed genes from returning only large generic terms.

    When ``descriptive`` is set — run_de labelled its own output ``exploratory_ranking`` or
    ``pseudoreplicated`` — the adjusted p-value is NOT a significance threshold: it comes from a
    per-cell test across a condition, where cells of one animal are not independent observations
    of that animal. Gating on it would launder a pseudoreplicated number into "significantly
    shifted genes", which is precisely the contradiction a live plan produced (its DE step declared
    p-values descriptive; its enrichment step passed padj=0.05). So the p-value is IGNORED, not
    widened, and selection falls back to effect size alone, strongest first.
    """
    out: list[tuple[str, float]] = []
    for row in rows:
        try:
            padj = float(row.get("pval_adj", 1) or 1)
            lfc = float(row.get("log2fc", 0) or 0)
        except (TypeError, ValueError):
            continue
        if abs(lfc) < lfc_min:
            continue
        if not descriptive and padj >= padj_max:
            continue
        gene = row.get("gene")
        if gene:
            out.append((str(gene), lfc))
    if descriptive:
        # Deepest effect first, so an opt-in top_n_genes cap takes the strongest genes rather
        # than whatever order the table happened to be written in.
        out.sort(key=lambda item: -abs(item[1]))
    return out


def _upstream_truncation(sidecar: Path, selected: "dict[str, int]",
                         rows_read: "dict[str, int]") -> dict[str, Any]:
    """Whether the input genes are the DE table's own top-N rather than the significant set.

    Run f3b8268c4fd4's ORA step read run_de's marker tables, the top 50 genes per cluster by
    score. All 50 cleared the gate in all 25 clusters, so the result said "50 selected by padj
    and |log2FC|, no cap" while 2,300-9,644 genes per cluster were significant. Nothing reported
    the cap, and the Critic accepted the step as an ORA of the significant set.

    run_de's ``de_<key>_significance.json`` gives the true counts. Without it (a run before the
    sidecar existed), the sign is that the gate kept every row it was given, in every group, at
    one common table length."""
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    sig = meta.get("significant_by_group")
    out: dict[str, Any] = {"truncated": False,
                           "upstream_table": str(meta.get("per_group_tables") or "not recorded")}
    if meta.get("per_group_tables") == "complete":
        return out                       # a contrast's per-group tables hold every tested gene
    if isinstance(sig, dict) and str(meta.get("per_group_tables", "")).startswith("top_"):
        true = {g: int(v.get("up", 0)) + int(v.get("down", 0))
                for g, v in sig.items() if isinstance(v, dict) and g in selected}
        cut = {g: n for g, n in true.items() if n > selected[g]}
        if not cut:
            return out
        vals = sorted(cut.values())
        out.update(truncated=True, significant_upstream_by_group=true)
        out["warning"] = (
            f"INPUT IS A TOP-N, NOT THE SIGNIFICANT SET: run_de wrote each group's table as its "
            f"{meta['per_group_tables'].replace('_', ' ')} genes by score. In {len(cut)} of "
            f"{len(selected)} groups more genes are significant than the table holds (median "
            f"{vals[len(vals) // 2]}, range {vals[0]}-{vals[-1]}, at adjusted p < "
            f"{meta.get('padj_max')} and |log2FC| >= {meta.get('abs_log2fc_min')}), so this ORA "
            "tested each group's top-ranked markers. Report the terms as enrichment of the top-N "
            "marker list, never as enrichment of the significant gene set.")
        return out
    lengths = set(rows_read.values())
    if (len(selected) >= 2 and len(lengths) == 1 and min(lengths) >= 10
            and all(selected[g] == rows_read[g] for g in selected)):
        n = lengths.pop()
        out.update(truncated=True, upstream_table=f"top_{n} (inferred)")
        out["warning"] = (
            f"INPUT IS PROBABLY A TOP-N, NOT THE SIGNIFICANT SET: every one of the {n} rows of "
            f"each of the {len(selected)} groups' DE tables passed the gate, so the thresholds "
            f"selected nothing and the input size is the table's length ({n}, run_de's n_genes "
            "cap). This run predates run_de's de_<key>_significance.json, so the true significant "
            "counts are not on disk. Report the terms as enrichment of the top-N marker list.")
    return out


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
    top_n_genes = int(_p("run_enrichment", "top_n_genes", args))
    top_n_terms = int(_p("run_enrichment", "top_n_terms", args))
    padj_max = float(_p("run_enrichment", "padj", args))
    lfc_min = float(_p("run_enrichment", "lfc", args))

    # Resolve requested libraries to LOCAL .gmt files (offline — no Enrichr network).
    # EXISTENCE IS NOT ENOUGH. A truncated or placeholder .gmt passes `p.exists()`, gets handed to
    # gseapy, and yields "no enriched terms" — which a reader takes as a scientific finding about
    # the data rather than a missing library. Production was found (2026-08-10) serving a 20-byte
    # `GO_Biological_Process_2023.gmt` holding one fake line (`term  desc  RHO  PDE6A`), so every
    # GO Biological Process enrichment had silently been tested against a single made-up gene set.
    # Count real terms instead, and report the counts so the methods section can state what was
    # actually searched.
    gdir = _genesets_dir()
    libs: dict[str, str] = {}
    lib_terms: dict[str, int] = {}
    missing_libs: list[str] = []
    degenerate_libs: dict[str, str] = {}
    for name in gene_sets:
        p = gdir / f"{name}.gmt"
        if not p.exists():
            missing_libs.append(name)
            continue
        n_terms = _gmt_term_count(p)
        lib_terms[name] = n_terms
        too_small = n_terms < _MIN_GMT_TERMS if name in _KNOWN_LARGE_LIBS else n_terms < 1
        if too_small:
            degenerate_libs[name] = (
                f"{p.name} holds {n_terms} usable term set(s)"
                + (" — a real one has thousands. It is a truncated download or a leftover test "
                   "stub; re-fetch it with scripts/fetch_genesets.py."
                   if name in _KNOWN_LARGE_LIBS else " — the file is empty."))
            continue
        libs[name] = str(p)
    if not libs:
        return {"status": "error", "step": "enrichment", "genesets_dir": str(gdir),
                "missing_libraries": missing_libs, "unusable_libraries": degenerate_libs,
                "error": (f"no USABLE local gene-set (.gmt) library found in {gdir} for {gene_sets}. "
                          + ("Present but degenerate: "
                             + "; ".join(degenerate_libs.values()) + " " if degenerate_libs else "")
                          + "Offline enrichment needs local GMTs (the analysis container has no "
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

    # (display label, base group, direction, genes) — direction is "" when the table has only one.
    selections: list[tuple[str, str, str, list[str]]] = []
    split_direction = bool(args.get("split_direction", True))
    n_input: dict[str, int] = {}
    warnings: list[str] = []
    upstream: dict[str, Any] = {"truncated": False}
    descriptive_de = ""      # the DE label, when run_de said its p-values are not inferential
    if de_all is not None and de_all.exists():
        key = de_all.name[len("de_"):-len("_all.csv")]
        # A p-value from a pseudoreplicated cell-level test is not a significance threshold, and
        # using it to choose which genes ENTER the enrichment launders it into "significantly
        # shifted genes" — the exact contradiction seen in a live plan, whose DE step declared
        # p-values descriptive and whose enrichment step then passed padj=0.05. When run_de labels
        # its own output non-inferential, select by EFFECT SIZE instead and say so. The gate is not
        # merely relaxed: |log2FC| ranking is the selection ORA is run on when no valid test
        # exists, and `padj` is ignored rather than widened.
        try:
            meta = json.loads((tables / f"de_{key}_inference.json").read_text(encoding="utf-8"))
            if str(meta.get("inference", "")) in ("exploratory_ranking", "pseudoreplicated"):
                descriptive_de = str(meta["inference"])
        except (OSError, ValueError, KeyError):
            descriptive_de = ""
        if descriptive_de:
            warnings.append(
                "The differential expression this reads was labelled " + descriptive_de + " by "
                "run_de, so its adjusted p-values are not a valid significance threshold and were "
                "NOT used to choose the input genes; genes entered by |log2 fold-change| >= "
                f"{lfc_min} instead. Report these terms as a DESCRIPTIVE over-representation of an "
                "exploratory ranking. The enrichment's OWN adjusted p-values describe overlap with "
                "the gene sets, conditional on a ranking that is itself unvalidated — they are not "
                "evidence that the genes are differentially expressed.")

        def _rows_for(group: str) -> tuple[list[tuple[str, float]], int]:
            """Every gene of ``group`` clearing BOTH thresholds, and how many rows were read.

            Read from the PER-GROUP table, not the combined one. `de_<key>_all.csv` holds only what
            `run_de` kept after its own `n_genes` cap (50 per direction by default), so selecting
            from it meant ORA never saw more than 100 genes per group however many were significant
            — a truncation applied upstream, invisible here, and with no basis in how ORA is run.
            The per-group file carries the full tested table for a contrast; for markers it is the
            top n_genes too, which `_upstream_truncation` reports."""
            src = tables / f"de_{key}_{_slug(group)}.csv"
            fh_path = src if src.exists() else de_all
            with fh_path.open(encoding="utf-8") as fh:
                rows = [r for r in csv.DictReader(fh)
                        if not (fh_path is de_all and r.get("group") != group)]
            return _enrichment_input_rows(rows, lfc_min, padj_max, bool(descriptive_de)), len(rows)

        groups: list[str] = []
        with de_all.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("group") and row["group"] not in groups:
                    groups.append(row["group"])
        read = {g: _rows_for(g) for g in groups}
        by_group = {g: items for g, (items, _) in read.items()}
        upstream = _upstream_truncation(
            tables / f"de_{key}_significance.json",
            {g: len(items) for g, items in by_group.items()},
            {g: n for g, (_, n) in read.items()})
        if upstream["truncated"]:
            warnings.append(upstream["warning"])
        for grp, items in by_group.items():
            up = [g for g, lfc in items if lfc > 0]
            down = [g for g, lfc in items if lfc < 0]
            if top_n_genes > 0:                      # opt-in cap only
                up, down = up[:top_n_genes], down[:top_n_genes]
            n_input[grp] = len(up) + len(down)
            # A one-vs-rest MARKER table is ranked by score, so it is up-regulated by construction
            # and there is nothing to split. A CONTRAST table carries both sides, and pooling them
            # into one ORA dilutes each — an up- and a down-regulated pathway can cancel into
            # "nothing enriched", and the report then silently describes only the up half.
            if split_direction and len(up) >= 5 and len(down) >= 5:
                selections.append((f"{grp} (up)", grp, "up", up))
                selections.append((f"{grp} (down)", grp, "down", down))
            else:
                genes = (up + down)[:top_n_genes] if top_n_genes > 0 else (up + down)
                if genes:
                    selections.append((grp, grp, "", genes))
    elif args.get("genes"):
        genes = args["genes"]
        selections = [("input", "input", "", list(genes) if isinstance(genes, list) else [genes])]
    if not selections:
        return {"status": "error",
                "error": ("no significant DE genes available — run run_de first (it writes "
                          "tables/de_<key>_all.csv), or pass args.genes. If run_de DID run, its "
                          "contrast may simply have found nothing at adjusted p < 0.05 — report "
                          "that, do not substitute a gene list.")}

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
    genes_used: dict[str, int] = {}
    table_paths: list[str] = []
    for label, base, direction, genes in selections:
        if not genes:
            continue
        try:
            # Offline hypergeometric ORA against the local GMTs — no network, no rate limits.
            enr = gp.enrich(gene_list=list(genes), gene_sets=gmt_paths,
                            background=background, outdir=None, verbose=False)
        except Exception as exc:  # noqa: BLE001 - a bad group is reported, never fatal
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
        stem = _slug(base) + (f"_{direction}" if direction else "")
        # gseapy returns a DataFrame when anything was enriched, but a plain (often empty) LIST
        # when nothing was — and `.sort_values` on a list is an AttributeError that took the whole
        # tool down instead of reporting the perfectly ordinary result "no enriched terms".
        res = getattr(enr, "results", None)
        if res is None or not hasattr(res, "sort_values"):
            rows = []
        else:
            df = res.sort_values("Adjusted P-value").head(top_n_terms)
            rows = [
                {"group": base, "direction": direction or "both", "term": str(r["Term"]),
                 "gene_set": str(r["Gene_set"]), "adj_pval": float(r["Adjusted P-value"]),
                 "combined_score": float(r["Combined Score"]), "overlap": str(r["Overlap"])}
                for _, r in df.iterrows()
            ]
        table_path = tables / f"enrichment_{stem}.csv"
        _write_table(table_path, rows,
                     ["group", "direction", "term", "gene_set", "adj_pval", "combined_score",
                      "overlap"])
        enriched[label] = rows[:top_n_terms]
        genes_used[label] = len(genes)
        table_paths.append(_rel(art, table_path))

        if rows and plt is not None:
            fig, ax = plt.subplots(figsize=(8, max(2.5, 0.4 * len(rows))))
            terms = [r["term"][:60] for r in rows][::-1]
            scores = [50.0 if r["adj_pval"] <= 0 else -math.log10(r["adj_pval"]) for r in rows][::-1]
            ax.barh(terms, scores, color="#C44E52" if direction == "down" else "#4C72B0")
            ax.set_xlabel("-log10(adjusted p-value)")
            ax.set_title(f"Top enriched terms — {label}")
            fig.tight_layout()
            fig_path = figs / f"enrichment_{stem}.png"
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
        # How many term sets each library ACTUALLY contributed. "No enriched terms" against a
        # 5,000-term library and against a 1-term stub are different statements, and only this
        # field tells them apart.
        "gene_set_terms": lib_terms,
        "unusable_libraries": degenerate_libs,
        "groups": list(enriched),
        "top_terms_by_group": {g: [r["term"] for r in rows] for g, rows in enriched.items()},
        # Whether a group was split into up-/down-regulated halves, and how many genes each ORA
        # actually consumed — a term list means nothing without the size of the list behind it.
        "split_by_direction": any(" (up)" in g or " (down)" in g for g in enriched),
        "genes_per_group": genes_used,
        # State the background the p-values were actually computed against — a reviewer cannot
        # judge an ORA result without it, and a report must not silently imply the whole genome.
        "background_source": background_source,
        "background_size": background_n,
        # WHAT went into the test, and under which thresholds. Without it a reader cannot tell an
        # ORA run on 12 genes from one run on 4,000 — and those return very different kinds of
        # term, for reasons of list length rather than biology.
        "selection": {"padj": None if descriptive_de else padj_max,
                      "abs_log2fc_min": lfc_min,
                      "cap_per_direction": top_n_genes or None,
                      "selected_by": "abs_log2fc" if descriptive_de else "padj_and_abs_log2fc",
                      "genes_selected_by_group": n_input,
                      # The DE table's own cap, which the gate above cannot see past.
                      "upstream_table": upstream.get("upstream_table", "not recorded"),
                      **({"significant_upstream_by_group": upstream["significant_upstream_by_group"]}
                         if upstream.get("significant_upstream_by_group") else {})},
        "tables": table_paths,
        "figures": figures,
        "errors": errors,
        "note": ("ORA and preranked GSEA (run_gsea_prerank) test different inputs under different "
                 "null hypotheses — they are NOT expected to agree, and disagreement is not a "
                 "failure of either."),
        **({"warnings": warnings} if warnings else {}),
        **({"input_inference": descriptive_de} if descriptive_de else {}),
        "raw_data_to_llm": False,
    }


def make_tool() -> HarnessTool:
    """The ``run_enrichment`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
        "run_enrichment",
        "Over-representation / pathway enrichment — OFFLINE ORA against LOCAL gene-set "
        "(.gmt) files (gseapy.enrich), NOT the Enrichr web API (the analysis host has no "
        "network). Automatically reads the top DE genes from this run's DE table "
        "(`tables/de_<groupby>_all.csv`, e.g. de_majorclass_all.csv) and runs ORA PER GROUP, "
        "writing one enrichment table + bar plot per cell class and returning the top enriched "
        "terms per group. Do NOT pass a pooled `genes` list — that collapses every class into a "
        "single 'input' group and loses the per-class pathway biology. Run AFTER run_de; it "
        "picks up whatever key run_de used (annotated classes preferred over raw leiden). "
        "When the DE table is a CONTRAST (it carries both up- and down-regulated significant "
        "genes) each group is split into '<group> (up)' and '<group> (down)' and enriched "
        "separately, so a down-regulated programme cannot be cancelled out by an up-regulated "
        "one; `split_direction: false` disables that. "
        "`gene_sets` are library names resolved to local .gmt files "
        "(default GO_Biological_Process_2023 / Reactome_2022 / MSigDB_Hallmark_2020).",
        _schema("run_enrichment",
                gene_sets={"type": "array", "items": {"type": "string"},
                           "description": "local gene-set libraries to test against"},
                background={"type": "integer",
                            "description": "ORA background size; omit to use the tested universe"},
                split_direction={"type": "boolean",
                                 "description": "test up- and down-regulated genes separately"},
                groupby={"type": "string",
                         "description": ("which DE table to read, by the key in its file name "
                                         "tables/de_<groupby>_all.csv: the column run_de grouped "
                                         "by (e.g. leiden, majorclass), not the `group` column "
                                         "inside the table; omit to find the table "
                                         "automatically")},
                genes={"type": "array", "items": {"type": "string"},
                       "description": "explicit gene list; omit so the DE tables are found instead"}),
        run_enrichment,
        reads_private_data=False, category="analysis", requires=("gseapy",),
    )
