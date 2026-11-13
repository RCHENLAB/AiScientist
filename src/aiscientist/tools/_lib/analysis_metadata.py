"""Historical analysis parameters and report/upload naming helpers.

Scientific execution belongs to the native MCP servers. These metadata records remain
for explaining existing run evidence; new tool schemas come from the pinned catalog.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PARAMS: dict[str, dict[str, tuple[Any, str]]] = {
    "run_cellqc": {
        "species": ("auto", "the reference the libraries were aligned to; it decides the "
                            "mitochondrial, ribosomal and hemoglobin gene sets. 'auto' reads it from "
                            "Cell Ranger's web summary (Transcriptome), else from the gene names"),
        "max_pct_mt": (10.0, "drop a barcode whose counts are more than this percent "
                             "mitochondrial. CellQC's default 10 suits whole cells; single NUCLEI "
                             "should carry almost no mitochondrial signal, and the CellQC reference "
                             "snRNA-seq run used 5"),
        "min_counts": (500, "drop a barcode with fewer total counts than this (counted after "
                            "ambient correction)"),
        "min_features": (300, "drop a barcode detecting fewer genes than this"),
        "ambient_method": ("soupx", "ambient-RNA correction APPLIED to the counts: soupx "
                                    "(estimated from Cell Ranger's clustering), decontx, or none"),
        "ambient_compare": ("decontx", "a second ambient method that is estimated and reported "
                                       "but NOT applied, to show where the methods disagree; '' "
                                       "for none"),
        "chemistry": ("auto", "the 10x chemistry, which sets the expected doublet rate: next_gem "
                              "(3' v2/v3/v3.1, ~0.8% per 1,000 cells) or gem_x (3' v4, about half "
                              "that). 'auto' reads it from Cell Ranger's web summary"),
        "nreaction": (1, "10x reactions (GEM wells) pooled into each library. It only changes the "
                         "expected doublet rate (rate x cells / (nreaction x capacity)), describes "
                         "how the libraries were made and is NOT measured from the data: confirm "
                         "it with whoever prepared them"),
        "doublet_decider": ("doubletfinder", "the doublet caller whose calls REMOVE cells; "
                                             "detailed diagnostics remain in the downloadable QC files"),
        "n_top_genes": (2000, "highly-variable genes kept for the embedding after the libraries "
                              "are merged"),
        "seed": (42, "random seed for CellQC's stochastic steps, so a rerun reproduces"),
        "warn_ambient_frac": (0.25, "flag a library whose mean SoupX contamination is above this. "
                                    "An ENGINEERING guard from the CellQC validation guide, not a "
                                    "literature threshold"),
        "warn_retained_below": (0.5, "flag a library that keeps less than this fraction of its "
                                     "Cell Ranger cells. An ENGINEERING guard: losing half a library "
                                     "usually means a threshold, or the library, is the problem"),
    },
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

TOOL_SUMMARY: dict[str, str] = {
    "run_cellqc":
        "Ran CellQC on each Cell Ranger library: removed ambient RNA from the counts (SoupX), "
        "discarded barcodes below the count, gene and mitochondrial thresholds below, removed "
        "doublets and measured each "
        "barcode's intronic read fraction; the libraries were then merged and normalised.",
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

PARAM_CHOICES: dict[str, dict[str, tuple[str, ...]]] = {
    "run_cellqc": {
        "species": ("auto", "human", "mouse", "macaque"),
        "ambient_method": ("soupx", "decontx", "none"),
        "chemistry": ("auto", "next_gem", "gem_x"),
        "doublet_decider": ("doubletfinder", "scdblfinder"),
    },
    "run_de": {
        # scanpy's rank_genes_groups backends. DESeq2 is deliberately NOT here: a per-cell DESeq2
        # is not a thing scanpy does, and wanting it means wanting `run_pseudobulk_de`.
        "method": ("wilcoxon", "t-test", "t-test_overestim_var", "logreg"),
    },
}

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

_CONDITION_COL_HINTS = ("sample", "donor", "patient", "subject", "individual", "condition",
                        "genotype", "treatment", "status", "disease", "group", "cohort",
                        "timepoint", "orig.ident", "orig_ident", "perturbation", "stim")

def _looks_like_condition_column(name: str) -> bool:
    """True when an obs column name reads like an EXPERIMENTAL CONDITION / library label.

    Same contract as :func:`_looks_like_celltype_column`: a name heuristic, used to decide whether
    to WARN or REFUSE, never to change what a tool computes. Checked before the cell-type
    heuristic because ``orig.ident`` matches both (``ident`` is a cell-type hint) and it is
    virtually always the library/sample id."""
    n = _norm_col(name)
    return any(_norm_col(h) in n for h in _CONDITION_COL_HINTS)


def _genesets_dir() -> Path | None:
    """The explicitly configured native enrichment reference root, when one exists."""
    value = (os.environ.get("SCRNA_GENESETS_DIR") if "SCRNA_GENESETS_DIR" in os.environ
             else os.environ.get("AISCIENTIST_GENESETS_DIR"))
    return Path(value).expanduser() if value else None
