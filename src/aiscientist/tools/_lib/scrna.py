"""Code the scrna tools share.

Split out of ``tools/scrna_advanced.py`` and ``tools/scrna_pack.py`` by
``scripts/refactor/split_tools.py``: each top-level helper or constant that more than one tool
uses moved here verbatim. A tool's own code is in ``aiscientist.tools.<tool>.tool``. The tools:
run_scanpy_qc, run_clustering, run_de, run_enrichment, run_depth_matched_de, run_gsea_prerank,
run_doublet_detection, run_integration, run_pseudobulk_de, run_composition, run_marker_annotation.

The original module docstring:

The single-cell RNA-seq **analysis production line** — real scanpy QC/clustering/DE
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
  whole run (same graceful-degrade contract as ``reporting/report.py`` for pandoc).
* **Stateful pipeline over disk checkpoints.** Each step reads the previous step's
  ``.h5ad`` checkpoint from the run's ``work/`` dir and writes the next one, so the
  agent can call ``run_scanpy_qc`` → ``run_clustering`` → ``run_de`` → ``run_enrichment``
  in sequence and each builds on the last (no giant object threaded through prompts).
* **Privacy boundary preserved.** Tools take a dataset PATH and return only DERIVED
  metrics / figure paths / gene lists — never the raw expression matrix. They write
  artifacts under ``<workspace>/artifacts`` so the files browser + bundle pick them up.

Exposed as ``HarnessTool``s via :func:`scrna_catalog`, registered alongside the
lightweight QC/DE smoke tools in the Scientist's catalog.

The single-cell steps the analysis line was missing.

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

import csv
import os
from pathlib import Path
from typing import Any


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


def _rel(art: Path, path: Path) -> str:
    """Path relative to the artifacts root (the URL key the files browser uses)."""
    try:
        return path.relative_to(art).as_posix()
    except ValueError:
        return path.name


# --- which file a step reads -------------------------------------------------------------------
# Each step reads the file the previous TOOL wrote, by a fixed name (adata_qc.h5ad →
# adata_clustered.h5ad → adata_de.h5ad; run_de's rank_<groupby>_<group>.rnk for GSEA). That chain
# breaks as soon as an upstream step was done in run_code: the result sits under another name, the
# next tool reports its checkpoint missing (3 of the 8 failed tool calls in the archived runs), and
# both ways round it were bad — overwrite the tool's checkpoint (3 archived snippets did, which
# desynchronises the tables already exported from it) or redo the rest of the line in run_code,
# without the tools' guards. `input` names the file instead. It is confined to this run's own work/
# and artifacts/ directories: a path typed for another host, or outside the run, is refused rather
# than guessed at (inspect_dataset honoured model-typed paths and reported the run's own dataset
# "unreadable" 28 times).
_INPUT_SPEC = {
    "type": "string", "default": "",
    "description": (
        "an .h5ad to read INSTEAD of the checkpoint the previous tool wrote — e.g. one a run_code "
        "step saved in AISCIENTIST_WORK. A file name or a path inside this run's work/ or artifacts/ "
        "directory; nothing outside the run is read. Empty = the usual checkpoint. To feed a "
        "variant of an upstream result to the next tool, save it under a NEW name and pass it "
        "here — never overwrite a tool's checkpoint."),
}


def _run_files(ctx: Any, raw: Any, suffixes: tuple[str, ...], *, pattern_ok: bool = False
               ) -> "tuple[list[Path], str]":
    """A tool's ``input`` as existing file(s) inside this run's work/ or artifacts/ directory:
    ``([paths], "")``, or ``([], why)``. A bare name or a relative path means work/<it>; a path
    starting ``work/`` or ``artifacts/`` (or ``$AISCIENTIST_WORK/`` / ``$AISCIENTIST_ARTIFACTS/``, as a
    run_code snippet spells them) names that directory; an absolute path must already lie inside
    one. With ``pattern_ok``, wildcards in the file name select several files."""
    ws = _workspace(ctx).resolve()
    roots = ((ws / "work").resolve(), (ws / "artifacts").resolve())
    text = str(raw or "").strip()
    for var, sub in (("AISCIENTIST_WORK", "work"), ("AISCIENTIST_ARTIFACTS", "artifacts")):
        for form in (f"${var}/", f"${{{var}}}/"):
            if text.startswith(form):
                text = f"{sub}/{text[len(form):]}"
    p = Path(text).expanduser()
    if not p.is_absolute():
        p = ws / p if p.parts and p.parts[0] in ("work", "artifacts") else ws / "work" / p
    if pattern_ok and any(ch in p.name for ch in "*?["):
        candidates = sorted(p.parent.glob(p.name)) if p.parent.is_dir() else []
    else:
        candidates = [p]
    wanted = " or ".join(suffixes)
    found: list[Path] = []
    for c in candidates:
        r = c.resolve()
        if not any(r == root or root in r.parents for root in roots):
            return [], (f"`input` must name a file inside this run's work/ or artifacts/ "
                        f"directory; {text!r} is outside it.")
        if r.suffix.lower() not in suffixes:
            if len(candidates) == 1:
                return [], f"`input` must be a {wanted} file; {text!r} is not."
            continue
        if r.is_file():
            found.append(r)
    if not found:
        near = sorted(q.name for s in suffixes for q in (ws / "work").glob(f"*{s}"))[:20]
        return [], (f"`input` {text!r} matches no {wanted} file in this run. {wanted} files in "
                    f"work/: {', '.join(near) or 'none'}.")
    return found, ""


def _step_input(ctx: Any, args: dict[str, Any], chain: "tuple[Path, ...]", missing: str
                ) -> "tuple[Path | None, dict[str, Any] | None]":
    """The .h5ad this step reads: the caller's ``input`` when given, else the first existing
    checkpoint of ``chain``. ``(path, None)``, or ``(None, error result)`` — ``missing`` is the
    step's own message for an empty chain, kept verbatim."""
    if str(args.get("input") or "").strip():
        found, why = _run_files(ctx, args["input"], (".h5ad",))
        return (found[0], None) if found else (None, {"status": "error", "error": why})
    ckpt = next((c for c in chain if c.exists()), None)
    return (ckpt, None) if ckpt else (None, {"status": "error", "error": missing})


def _run_rel(ctx: Any, path: Path) -> str:
    """``path`` relative to the run's workspace ("work/adata_qc.h5ad"), for a result's ``read_from``."""
    try:
        return Path(path).resolve().relative_to(_workspace(ctx).resolve()).as_posix()
    except ValueError:
        return str(path)


def _write_table(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# --- declared parameters: ONE source of truth --------------------------------------------------
# tool -> param -> (default, plain-English meaning). The numbers live HERE rather than only inside
# each `args.get(name, 200)` because four separate things need the same answer, and they used to
# each carry their own copy:
#
#   * the tool bodies, via :func:`_p` — a running default cannot drift from the advertised one;
#   * the JSON schema handed to the model, via :func:`_schema` — the model reads the default and
#     what it MEANS in the same place it reads the parameter name;
#   * :func:`nondefault_params` — when a call passes something OTHER than the default, the live
#     chat feed and the technical report say so. Without it, a threshold the model invented on the
#     spot (this happened: `max_pct_mt=10` against a declared 20) reaches the manuscript reading
#     exactly like a protocol-mandated one, and no reader can tell the two apart;
#   * `tests/test_declared_params.py`, which asserts each preset's SKILL.md documents these same
#     values — so the protocol a researcher reads and the code that runs cannot disagree.
#
# Changing a number here changes it everywhere, including the protocol text the test checks.
PARAMS: dict[str, dict[str, tuple[Any, str]]] = {
    "run_scanpy_qc": {
        "min_genes": (200, "drop a cell detecting fewer genes than this — an empty droplet or a "
                           "dying cell"),
        "min_cells": (3, "drop a gene detected in fewer cells than this — too sparse to support "
                         "any test"),
        "max_pct_mt": (10.0, "drop a cell whose reads are more than this percent mitochondrial (a "
                             "stressed or lysed cell). 10 is the common working threshold for "
                             "tissue; the Seurat and scanpy tutorials use 5 for PBMC, and single "
                             "NUCLEI need far less (1-5) because a nucleus should carry almost no "
                             "mitochondrial signal. Raise it only for a tissue known to be "
                             "mitochondria-rich, and say so"),
        "n_top_genes": (2000, "how many highly-variable genes to keep for the embedding — more "
                              "genes carry more structure and more noise. 2000 is Seurat's "
                              "default and the usual starting point"),
        "warn_removed_pct": (50.0, "warn when QC discards more than this percent of cells. An "
                                   "ENGINEERING guard, not a literature threshold: losing half a "
                                   "dataset usually means a threshold is wrong for this tissue, "
                                   "and it should be checked before the result is used"),
        "mito_prefix": ("MT-", "the name prefix that marks a mitochondrial gene, matched without "
                               "regard to case, so the default also covers mouse 'mt-'. It is the "
                               "only way `max_pct_mt` knows which genes to count: a dataset whose "
                               "genes match nothing gets no mitochondrial filter at all"),
        "gene_symbols_key": ("", "a `var` column holding gene SYMBOLS, for data whose gene names "
                                 "are Ensembl IDs (cellxgene files keep the symbols in "
                                 "`feature_name`). The mitochondrial prefix is then matched "
                                 "against that column. Empty = match the gene names themselves"),
    },
    "run_clustering": {
        "resolution": (1.0, "Leiden granularity: higher splits the cells into more, smaller "
                            "clusters"),
        "n_pcs": (30, "principal components fed into the neighbourhood graph"),
        "n_neighbors": (15, "neighbours per cell in that graph — larger gives smoother, coarser "
                            "structure"),
        "select_resolution": (False, "choose the resolution by bootstrap stability instead of "
                                     "accepting the default"),
        "n_bootstrap": (10, "resampling rounds per candidate resolution when selecting one"),
        "subsample_frac": (0.8, "fraction of cells per bootstrap round"),
        "stability_min": (0.90, "minimum adjusted Rand index a resolution must clear to be called "
                                "stable"),
        "max_sweep_cells": (20000, "cap on the cells used for the stability sweep"),
    },
    "run_de": {
        "groupby": ("leiden", "the obs column whose levels are compared"),
        "method": ("wilcoxon", "the rank test rank_genes_groups uses"),
        "n_genes": (50, "rows kept per group — per DIRECTION when it is a contrast"),
        "reference": ("rest", "the baseline level. 'rest' gives one-vs-rest MARKERS; a named level "
                              "(e.g. 'WT') gives a real condition-vs-control contrast"),
        "stratify_by": ("", "an EXISTING cell-type column — runs the contrast separately within "
                            "each cell type instead of pooling them"),
        "min_cells": (30, "a group with fewer cells than this in either arm is skipped. 30 is "
                          "the usual rule-of-thumb floor for the rank test's normal approximation "
                          "— an ENGINEERING guard, not a literature threshold"),
        "min_pct": (0.1, "a gene must be detected in at least this fraction of the cells of one "
                         "of the two populations compared, or it is not tested at all (Seurat "
                         "FindMarkers' min.pct). Without it most genes enter the test undetected "
                         "— on the DDX41 retina object 2/3 did — which triples the BH denominator "
                         "and floods the ranking with divide-by-zero fold-changes above 2^20. "
                         "0 disables"),
        "tie_correct": (True, "tie-correct the Wilcoxon normal approximation. A single-cell "
                              "matrix is ~90% zeros, so ties dominate every comparison; scanpy's "
                              "default (False) is anti-conservative on sparse data"),
        "padj": (0.05, "adjusted-p (Benjamini-Hochberg) cutoff for calling a gene significant"),
        "lfc": (0.25, "minimum |log2 fold-change| for calling a gene significant — applied "
                      "together with `padj`, and drawn as the volcano's vertical line. 0.25 is "
                      "the single-cell convention (Seurat's FindMarkers threshold); 1.0 is a "
                      "bulk-RNA habit and hides most real single-cell effects"),
        "force": (False, "run a pooled per-cell test across a CONDITION column anyway. The result "
                         "is pseudoreplicated and must be reported as non-inferential"),
    },
    # Where a number has a citable basis the meaning names it; where it does not, the meaning says
    # so in as many words. "This is an engineering guard, not a literature threshold" is a fact a
    # reader needs, and it is the one thing an undocumented constant can never tell them.
    "run_doublet_detection": {
        "expected_doublet_rate": (0.06, "prior doublet rate handed to the detector — roughly the "
                                        "~0.8% per 1,000 cells that 10x Genomics quotes, so 0.06 "
                                        "suits a ~8,000-cell lane. Set it from YOUR loading, not "
                                        "from this default"),
        "flag_rate_above": (0.20, "warn when the detected doublet fraction exceeds this. An "
                                  "ENGINEERING guard, not a literature threshold: it exists to "
                                  "catch a mis-set expected rate or an overloaded lane, and a run "
                                  "above it is a prompt to check the loading, not a finding"),
    },
    "run_pseudobulk_de": {
        "min_cells_per_sample": (10, "a sample contributing fewer cells than this to a cell type "
                                     "is dropped from that cell type's test"),
        "min_samples_per_condition": (2, "refuse to test an arm backed by fewer samples than this "
                                         "— below it there is no replication and no valid p-value. "
                                         "2 is the COMPUTABILITY floor, not a recommendation: the "
                                         "single-cell DE literature (Squair et al. 2021) asks for "
                                         ">=3 replicates per arm, and a 2-vs-2 result should be "
                                         "reported as underpowered"),
        "min_count": (10, "a gene must reach this many summed counts in at least as many "
                          "samples as the smaller arm, or it is not tested (edgeR filterByExpr's "
                          "rule of thumb). Genes nobody detected cannot be tested — they only "
                          "inflate the BH denominator"),
        "padj": (0.05, "adjusted-p (Benjamini-Hochberg) cutoff used to count significant genes"),
    },
    "run_enrichment": {
        # 0 = no cap, which is the field's practice: ORA is run on THE significant set, defined by
        # the thresholds below, not on an arbitrary top-N of it. The old default of 100 had no
        # basis — and because the combined DE table it read was itself already capped at
        # `run_de.n_genes` per direction, the real truncation was invisible from here.
        "top_n_genes": (0, "optional cap on how many genes enter the over-representation test. 0 "
                           "means no cap — use every gene passing the thresholds below, which is "
                           "the standard way ORA is run. Set it only to deliberately shorten a "
                           "very long list, and say that you did"),
        "top_n_terms": (10, "how many enriched terms to report per group"),
        "padj": (0.05, "adjusted-p cutoff a DE gene must clear to enter the test — the same "
                       "Benjamini-Hochberg cutoff run_de applies"),
        "lfc": (0.25, "minimum |log2 fold-change| a DE gene must clear to enter the test. Pairing "
                      "an effect-size floor with the p-value cutoff is what keeps a list of "
                      "thousands of barely-changed genes from returning only large generic terms; "
                      "0.25 is the single-cell convention"),
    },
    "run_depth_matched_de": {
        "groupby": ("sampleid", "the obs column holding the experimental CONDITION"),
        "reference": ("", "the CONTROL level of `groupby` (e.g. 'WT'). Required — the check is a "
                          "two-level contrast"),
        "stratify_by": ("", "the CELL-TYPE column; the check runs separately within each type, "
                            "because depth imbalance differs per type and a pooled answer hides it"),
        "n_genes": (100, "how many top-ranked genes per direction enter the comparison. Deep enough "
                         "that the correlation is not driven by three genes, shallow enough to stay "
                         "the list a reader would actually look at"),
        "min_cells": (30, "skip a cell type with fewer cells than this in either arm — the usual "
                          "floor for the rank test's normal approximation"),
        "seed": (0, "random seed for the down-sampling, so the check reproduces"),
        "min_ratio": (1.05, "skip a cell type whose arms already differ by less than this ratio in "
                            "median library size — there is no imbalance to correct"),
    },
}


# What each tool DOES, in one sentence a researcher can read. Separate from the catalog
# ``description`` on purpose: that text is written to steer a MODEL — it is long, it names
# checkpoints and column contracts, and it argues about when not to use the tool. A researcher
# reading a finished report needs the other thing entirely, and asking a writer model to
# paraphrase the model-facing text is how "we ran run_de" ends up in a manuscript with no
# statement of what run_de is. Rendered verbatim into the report's pipeline section.
TOOL_SUMMARY: dict[str, str] = {
    "run_scanpy_qc":
        "Measured each cell's quality (how many genes it detects, how much of its signal is "
        "mitochondrial), discarded the cells and genes that fall below the thresholds below, and "
        "normalised the remaining counts so later comparisons reflect biology rather than "
        "sequencing depth.",
    "run_clustering":
        "Reduced the expression matrix to its main axes of variation, built a neighbourhood graph "
        "over the cells, and grouped them into clusters, then laid the cells out on a 2-D UMAP map "
        "for display. Only needed when the data does not already carry cell-type labels.",
    "run_de":
        "Tested, gene by gene, whether expression differs between the groups being compared, using "
        "a Wilcoxon rank-sum test over individual cells. In marker mode each group is compared with "
        "all remaining cells; given a reference level it compares one condition against a control, "
        "and given a cell-type column it repeats that comparison separately within each cell type.",
    "run_pseudobulk_de":
        "Summed each sample's raw counts into one expression profile per sample, then tested for "
        "differences BETWEEN SAMPLES rather than between cells — the comparison that treats the "
        "animal or donor, not the cell, as the unit that was replicated — tested with DESeq2's "
        "negative-binomial model, the field standard for count data with few replicates. It "
        "refuses to run when an "
        "arm has too few samples to support a test.",
    "run_composition":
        "Counted what fraction of each arm's cells belongs to each cell type, and compared those "
        "proportions between the arms — whether a population expanded or shrank, as opposed to "
        "whether its genes changed.",
    "run_enrichment":
        "Took the genes that came out of the comparison and asked which biological pathways and "
        "Gene Ontology terms they over-represent, tested against the set of genes actually "
        "measured in this experiment rather than a generic background.",
    "run_depth_matched_de":
        "Removed the sequencing-depth difference between the two arms by randomly discarding "
        "reads from the deeper arm until both matched, re-ran the same gene comparison, and "
        "measured how much of the original ordering survived. Genes that keep their place are "
        "candidate biological signal; genes whose ordering collapses or reverses were being "
        "ranked by how deeply their cells were sequenced.",
    "run_gsea_prerank":
        "Ranked every tested gene by its effect and asked which pathways are shifted toward the "
        "top or the bottom of that ranking — a whole-ranking view, complementary to enrichment on "
        "a cut list.",
    "run_doublet_detection":
        "Flagged droplets that probably captured two cells at once, which would otherwise look "
        "like a novel intermediate cell type.",
    "run_marker_annotation":
        "Assigned a cell-type label to each cluster by scoring it against known marker panels.",
    "run_integration":
        "Removed the systematic differences between batches or samples so that cells group by cell "
        "type rather than by which library they came from.",
    "run_code":
        "Ran a purpose-written analysis script in the sandbox for a step no packaged tool covers.",
}


# Parameters whose value is drawn from a CLOSED set, declared next to the defaults so the JSON
# schema carries it and one check can cover every tool. Without this the legal values lived only in
# the library the tool forwards to: a plan proposed `run_de(method="DESeq2")`, which reads perfectly
# to a reviewer and to the planner, but `method` is passed straight to
# ``sc.tl.rank_genes_groups`` — scanpy has no DESeq2 backend, so the step could only fail at run
# time, after approval, with the compute already booked (Ziyao, plan_mode_report_v2_5, B-3).
# DESeq2 IS available in this codebase, via `run_pseudobulk_de`; naming the alternative is the
# useful half of the message.
PARAM_CHOICES: dict[str, dict[str, tuple[str, ...]]] = {
    "run_de": {
        # scanpy's rank_genes_groups backends. DESeq2 is deliberately NOT here: a per-cell DESeq2
        # is not a thing scanpy does, and wanting it means wanting `run_pseudobulk_de`.
        "method": ("wilcoxon", "t-test", "t-test_overestim_var", "logreg"),
    },
}


def _p(tool: str, name: str, args: dict[str, Any]) -> Any:
    """This call's value for a declared parameter: the caller's if given, else the declared
    default from :data:`PARAMS`. Raises for an undeclared parameter rather than inventing a
    default, so adding a knob to a tool body forces adding it to the table."""
    if args.get(name) is not None:
        return args[name]
    return PARAMS[tool][name][0]


_JSON_TYPES = {bool: "boolean", int: "integer", float: "number", str: "string", list: "array"}


def _schema(tool: str, **extra: dict[str, Any]) -> dict[str, Any]:
    """The JSON schema for a tool's declared parameters, each carrying its ``default`` and a
    ``description`` of what it means. ``extra`` adds properties that have no meaningful default
    (free-form lists, column names supplied per call)."""
    props: dict[str, Any] = {}
    choices = PARAM_CHOICES.get(tool, {})
    for name, (default, meaning) in PARAMS.get(tool, {}).items():
        props[name] = {"type": _JSON_TYPES.get(type(default), "string"),
                       "default": default, "description": meaning}
        if name in choices:
            props[name]["enum"] = list(choices[name])
    for name, spec in extra.items():
        # MERGE, do not replace: a declared parameter keeps its `default` (and its declared
        # meaning, unless `extra` states a better one) even when a call site also describes it.
        # Replacing was silent — the model then chose values blind for every clobbered param.
        if name in props:
            props[name] = {**props[name], **spec}
        else:
            props[name] = spec
    return {"type": "object", "properties": props}


_CELLTYPE_COL_HINTS = ("celltype", "cell_type", "majorclass", "subclass", "annotation",
                       "cell_label", "predicted_label", "cluster_name", "ident")


def _norm_col(name: str) -> str:
    """An obs column name reduced to one spelling. Separators vary by whoever wrote the file
    (``orig.ident`` / ``orig_ident`` / ``orig-ident`` are one column), and comparing hints
    against the raw name matched some spellings and not others."""
    n = str(name).strip().lower()
    for ch in ("-", " ", "."):
        n = n.replace(ch, "_")
    return n


def _looks_like_celltype_column(name: str) -> bool:
    """True when an obs column name reads like an EXISTING cell-type annotation.

    Deliberately a local copy of the lab's planning-time heuristic rather than an import: tools
    must not depend on ``agents``. Used only to WARN — never to change what the tool computes."""
    n = _norm_col(name)
    if n in {"leiden", "louvain"} or n.startswith(("n_", "pct_", "total_")):
        return False
    if _looks_like_condition_column(n):     # `orig.ident` is a library id, not a cell type
        return False
    return any(h in n for h in _CELLTYPE_COL_HINTS)


_CONDITION_COL_HINTS = ("sampleid", "sample_id", "condition", "genotype", "treatment", "status",
                        "disease", "group", "cohort", "timepoint", "orig.ident", "orig_ident",
                        "perturbation", "stim")


def _looks_like_condition_column(name: str) -> bool:
    """True when an obs column name reads like an EXPERIMENTAL CONDITION / library label.

    Same contract as :func:`_looks_like_celltype_column`: a name heuristic, used to decide whether
    to WARN or REFUSE, never to change what a tool computes. Checked before the cell-type
    heuristic because ``orig.ident`` matches both (``ident`` is a cell-type hint) and it is
    virtually always the library/sample id."""
    n = _norm_col(name)
    return any(_norm_col(h) in n for h in _CONDITION_COL_HINTS)


def _slug(name: str) -> str:
    """Filename-safe form of a group label. Real cell-type labels carry separators —
    ``Club/Secretory``, ``AT2 (alveolar type 2)`` — and a raw ``/`` in a filename makes the
    write fail outright (it reads as a directory that doesn't exist), so the per-group table
    for that class silently never appears. Group labels stay verbatim INSIDE the tables."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(name).strip())
    return safe.strip("_") or "group"


# Default gene-set libraries (freely redistributable). KEGG is intentionally NOT a
# default — its GMT redistribution is license-restricted; add "KEGG_2021_Human" via
# args.gene_sets only if you've cleared that locally.
_DEFAULT_GENE_SETS = ("GO_Biological_Process_2023", "Reactome_2022", "MSigDB_Hallmark_2020")


def _genesets_dir() -> Path:
    """Where the local ``.gmt`` gene-set files live. ``AISCIENTIST_GENESETS_DIR`` overrides;
    otherwise ``tools/genesets/`` — which rides along with the dfs3b source bind, so the
    network-OFF analysis container finds it with no extra plumbing. (This module sits in
    ``tools/_lib/``; the deploy keeps the .gmt files in ``tools/genesets/``.)"""
    d = os.environ.get("AISCIENTIST_GENESETS_DIR")
    return Path(d) if d else Path(__file__).resolve().parents[1] / "genesets"


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
