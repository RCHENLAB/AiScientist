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
import json
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
    for name, (default, meaning) in PARAMS.get(tool, {}).items():
        props[name] = {"type": _JSON_TYPES.get(type(default), "string"),
                       "default": default, "description": meaning}
    props.update(extra)
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

    min_genes = int(_p("run_scanpy_qc", "min_genes", args))
    min_cells = int(_p("run_scanpy_qc", "min_cells", args))
    max_pct_mt = float(_p("run_scanpy_qc", "max_pct_mt", args))
    n_top_genes = int(_p("run_scanpy_qc", "n_top_genes", args))

    work, art, figs, _tables = _dirs(ctx)
    adata = _read_anndata(sc, src)
    adata.var_names_make_unique()
    n_cells_0, n_genes_0 = int(adata.n_obs), int(adata.n_vars)

    # Mitochondrial fraction (human "MT-" / mouse "mt-").
    adata.var["mt"] = adata.var_names.str.upper().str.startswith("MT-")
    n_mt_genes = int(adata.var["mt"].sum())
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
            f"NO mitochondrial genes matched the 'MT-' name prefix, so pct_counts_mt is 0 for every "
            f"cell and the max_pct_mt={max_pct_mt} filter removed NOTHING. Do NOT state that "
            f"high-mitochondrial cells were filtered. Check the gene naming (Ensembl IDs? already "
            f"filtered upstream?) before trusting this QC.")
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
        "pct_cells_removed": round(removed_pct, 2),
        "warnings": warnings,
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
        "checkpoint": "adata_clustered.h5ad",
        "tables": ([_rel(art, tables / "resolution_sweep.csv")] if sweep else []),
        "figures": [_rel(art, figs / "umap_clusters.png")],
        "raw_data_to_llm": False,
    }


# --- step 3: differential expression -----------------------------------------
# TWO shapes of question run through this one tool, and conflating them is what broke the DEG
# protocol:
#   * MARKERS  — each group vs the REST (``reference="rest"``, the default): "what defines this
#     cluster". This is what the tool did originally, and it is unchanged.
#   * CONTRAST — a group vs a NAMED reference level (``reference="WT"``), optionally STRATIFIED by
#     an existing cell-type column so the comparison runs WITHIN each cell type. This is the DEG
#     protocol's core step ("KO vs WT per cell type"). ``rank_genes_groups`` has always supported
#     it; the tool simply never exposed ``reference``, so the only way to run a contrast was a
#     hand-adapted ``run_code`` template whose output filenames no downstream tool could find
#     (``run_enrichment`` discovers ``de_<key>_all.csv``, the template wrote ``DEG/DEG_<ct>.csv``).


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
    ckpt = next((p for p in (work / "adata_clustered.h5ad", work / "adata_qc.h5ad") if p.exists()),
                None)
    if ckpt is None:
        return {"status": "error",
                "error": ("no analysis checkpoint found — run run_scanpy_qc first (it writes "
                          "adata_qc.h5ad). run_clustering is needed only when the dataset has no "
                          "cell-type / cluster label column of its own.")}

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

# --- step 4: enrichment (OFFLINE ORA via local GMT gene-set files) ------------

# Default gene-set libraries (freely redistributable). KEGG is intentionally NOT a
# default — its GMT redistribution is license-restricted; add "KEGG_2021_Human" via
# args.gene_sets only if you've cleared that locally.
_DEFAULT_GENE_SETS = ("GO_Biological_Process_2023", "Reactome_2022", "MSigDB_Hallmark_2020")
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


def _genesets_dir() -> Path:
    """Where the local ``.gmt`` gene-set files live. ``BIOAGENT_GENESETS_DIR`` overrides;
    otherwise a ``genesets/`` dir next to this module — which rides along with the dfs3b
    source bind, so the network-OFF analysis container finds it with no extra plumbing."""
    d = os.environ.get("BIOAGENT_GENESETS_DIR")
    return Path(d) if d else Path(__file__).resolve().parent / "genesets"


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

        def _rows_for(group: str) -> list[tuple[str, float]]:
            """Every gene of ``group`` clearing BOTH thresholds.

            Read from the PER-GROUP table, not the combined one. `de_<key>_all.csv` holds only what
            `run_de` kept after its own `n_genes` cap (50 per direction by default), so selecting
            from it meant ORA never saw more than 100 genes per group however many were significant
            — a truncation applied upstream, invisible here, and with no basis in how ORA is run.
            The per-group file carries the full tested table for a contrast."""
            src = tables / f"de_{key}_{_slug(group)}.csv"
            fh_path = src if src.exists() else de_all
            with fh_path.open(encoding="utf-8") as fh:
                rows = [r for r in csv.DictReader(fh)
                        if not (fh_path is de_all and r.get("group") != group)]
            return _enrichment_input_rows(rows, lfc_min, padj_max, bool(descriptive_de))

        groups: list[str] = []
        with de_all.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("group") and row["group"] not in groups:
                    groups.append(row["group"])
        by_group = {g: _rows_for(g) for g in groups}
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
                      "genes_selected_by_group": n_input},
        "tables": table_paths,
        "figures": figures,
        "errors": errors,
        "note": ("ORA and preranked GSEA (run_gsea_prerank) test different inputs under different "
                 "null hypotheses — they are NOT expected to agree, and disagreement is not a "
                 "failure of either."),
        **({"warnings": [
            "The differential expression this reads was labelled " + descriptive_de + " by run_de, "
            "so its adjusted p-values are not a valid significance threshold and were NOT used to "
            "choose the input genes; genes entered by |log2 fold-change| >= "
            f"{lfc_min} instead. Report these terms as a DESCRIPTIVE over-representation of an "
            "exploratory ranking. The enrichment's OWN adjusted p-values describe overlap with the "
            "gene sets, conditional on a ranking that is itself unvalidated — they are not "
            "evidence that the genes are differentially expressed."],
            "input_inference": descriptive_de} if descriptive_de else {}),
        "raw_data_to_llm": False,
    }


# --- step 4b: preranked GSEA (OFFLINE, over the COMPLETE ranked list) ---------


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
      4. report which genes keep their place (depth-robust) and which do not (depth-driven).

    A high rho means the ranking is not an artefact of depth. A rho near zero means depth decided
    it. A NEGATIVE rho means the ranking inverts once depth is equalised — the strongest possible
    evidence that the original ordering was a detection artefact, and the finding a report must not
    interpret as biology.

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
    ckpt = next((p for p in (work / "adata_clustered.h5ad", work / "adata_qc.h5ad") if p.exists()),
                None)
    if ckpt is None:
        return {"status": "error",
                "error": ("no analysis checkpoint found — run run_scanpy_qc first (it writes "
                          "adata_qc.h5ad, including the raw `counts` layer this tool needs).")}

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

    def _rank_frame(sub: Any) -> dict[str, float] | None:
        """{gene: log2fc} for `test_level` vs `reference` on this (already log-norm) subset."""
        try:
            sc.tl.rank_genes_groups(sub, groupby=groupby, groups=[test_level],
                                    reference=reference, method="wilcoxon", tie_correct=True,
                                    use_raw=False)
        except Exception:  # noqa: BLE001 - a stratum the test cannot run on is reported, not fatal
            return None
        names = [str(g) for g in sub.uns["rank_genes_groups"]["names"][test_level]]
        lfc = [float(v) for v in sub.uns["rank_genes_groups"]["logfoldchanges"][test_level]]
        return dict(zip(names, lfc))

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

        original = _rank_frame(sub)
        if original is None:
            skipped.append({"group": stratum, "reason": "the Wilcoxon contrast could not be run"})
            continue

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
        matched_rank = _rank_frame(matched_ad)
        if matched_rank is None:
            skipped.append({"group": stratum, "reason": "the matched contrast could not be run"})
            continue

        shared = [g for g in original if g in matched_rank]
        for direction, keep in (("up", lambda v: v > 0), ("down", lambda v: v < 0)):
            top = sorted((g for g in shared if keep(original[g])),
                         key=lambda g: -abs(original[g]))[:n_genes]
            if len(top) < 3:
                continue
            o = [original[g] for g in top]
            m = [matched_rank[g] for g in top]
            rho = _spearman(o, m)
            # "Robust" means the gene KEEPS ITS PLACE, not merely its sign. Sign alone passes a
            # gene whose effect collapses from 3.0 to 0.02 — measured on a synthetic control,
            # a sign-only rule called two thirds of pure background depth-robust. Membership of
            # the matched ranking's own top-N is the question this check exists to answer.
            matched_top = set(sorted((g for g in shared if keep(matched_rank[g])),
                                     key=lambda g: -abs(matched_rank[g]))[:n_genes])
            robust = [g for g in top if g in matched_top]
            verdict = ("preserved" if rho is not None and rho >= 0.5 else
                       "inverted" if rho is not None and rho < 0 else "weak")
            rows.append({"group": stratum, "direction": direction, "n_genes": len(top),
                         "depth_ratio": round(ratio, 3),
                         "deeper_arm": test_level if deeper_is_test else reference,
                         "spearman_rho": None if rho is None else round(rho, 4),
                         "n_kept_top_rank": len(robust),
                         "pct_kept_top_rank": round(100.0 * len(robust) / len(top), 1),
                         "verdict": verdict})
            robust_set = set(robust)
            for g in top:
                gene_rows.append({"group": stratum, "direction": direction, "gene": g,
                                  "log2fc_original": round(original[g], 4),
                                  "log2fc_depth_matched": round(matched_rank[g], 4),
                                  "depth_robust": g in robust_set})
            if verdict == "inverted":
                warnings.append(
                    f"{stratum} {direction}: the ranking INVERTS after depth matching "
                    f"(rho={rho:.2f}) — this ordering is a detection artefact of the "
                    f"{ratio:.2f}x depth imbalance, not biology, and must not be interpreted as "
                    "regulation.")
            elif verdict == "weak":
                warnings.append(
                    f"{stratum} {direction}: little of the ranking survives depth matching "
                    f"(rho={'n/a' if rho is None else f'{rho:.2f}'}) — treat these genes as "
                    "depth-sensitive.")

    if not rows:
        return {"status": "error", "step": "depth_matched_de",
                "error": ("no cell type could be checked — "
                          + ("; ".join(f"{s['group']}: {s['reason']}" for s in skipped[:6])
                             or "no groups were evaluated")),
                "skipped_groups": skipped}

    summary_csv = tables / "depth_matched_summary.csv"
    _write_table(summary_csv, rows,
                 ["group", "direction", "n_genes", "depth_ratio", "deeper_arm", "spearman_rho",
                  "n_kept_top_rank", "pct_kept_top_rank", "verdict"])
    genes_csv = tables / "depth_matched_genes.csv"
    _write_table(genes_csv, gene_rows,
                 ["group", "direction", "gene", "log2fc_original", "log2fc_depth_matched",
                  "depth_robust"])

    figures: list[str] = []
    fig_path = _depth_matched_figure(figs, rows)
    if fig_path:
        figures.append(fig_path)

    preserved = [r for r in rows if r["verdict"] == "preserved"]
    inverted = [r for r in rows if r["verdict"] == "inverted"]
    out: dict[str, Any] = {
        "status": "ok",
        "step": "depth_matched_de",
        "condition": groupby, "reference": reference, "tested": test_level,
        "stratify_by": stratify_by or None,
        "per_group": rows,
        "n_preserved": len(preserved), "n_inverted": len(inverted),
        "tables": [str(summary_csv), str(genes_csv)],
        "figures": figures,
        "skipped_groups": skipped,
        "interpretation": (
            f"{len(preserved)} of {len(rows)} (cell type x direction) rankings survive depth "
            f"matching; {len(inverted)} invert. A preserved ranking may be discussed as a "
            "candidate biological signal; an inverted or weak one is a depth artefact and must be "
            "reported as such."),
    }
    if warnings:
        out["warnings"] = warnings
    return out


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
            _schema("run_scanpy_qc"),
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
            _schema("run_clustering", resolution_candidates={
                "type": "array", "items": {"type": "number"},
                "description": "resolutions to try when select_resolution is on"}),
            run_clustering,
            reads_private_data=True, category="analysis", requires=("scanpy",),
        ),
        HarnessTool(
            "run_de",
            "Differential expression via rank_genes_groups (Wilcoxon), in EITHER of two shapes. "
            "(1) MARKERS (default): each level of `groupby` vs the rest — 'what defines this "
            "cluster'. (2) CONTRAST: pass `reference` (the CONTROL level of `groupby`, e.g. "
            "\"WT\") to compare condition vs control instead of vs rest; add `stratify_by` (an "
            "EXISTING cell-type label column) to run that contrast SEPARATELY WITHIN EACH CELL "
            "TYPE. Use (2) for any KO-vs-WT / disease-vs-control DEG study — do NOT hand-write it "
            "in run_code, and do NOT re-cluster a dataset that already has labels. "
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
                "description": "restrict the comparison to these levels of groupby"}),
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
                    groupby={"type": "string", "description": "which DE table column names the groups"},
                    genes={"type": "array", "items": {"type": "string"},
                           "description": "explicit gene list; omit so the DE tables are found instead"}),
            run_enrichment,
            reads_private_data=False, category="analysis", requires=("gseapy",),
        ),
        HarnessTool(
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
            # what test_declared_params exists to prevent.
            _schema("run_depth_matched_de"),
            run_depth_matched_de,
            # It reads the raw counts layer and re-runs the contrast on cells, so it is on the
            # private-data path — unlike the two pathway tools, which only ever see gene symbols.
            reads_private_data=True, category="analysis",
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
