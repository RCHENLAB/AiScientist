---
name: gene_signature_scoring
description: Score cells for a gene signature and compare it across clusters/conditions
tools: run_scanpy_qc, run_cellqc, run_clustering, run_code
data_type: scrna
outputs:
  - path: figures/violin_qc_violin.png
    kind: figure
    tool: run_scanpy_qc
    description: Violin plots of detected genes, total counts and percent mitochondrial counts per cell, drawn before the QC filters were applied.
    required: true
  - path: tables/cellqc_metrics.csv
    kind: table
    tool: run_cellqc
    description: CellQC metrics, one row per library, with the cells kept by each filter, the ambient-RNA contamination estimate, the doublets removed and the two doublet callers' agreement.
    required: true
  - path: figures/violin_cellqc_after_qc*.png
    kind: figure
    tool: run_cellqc
    description: Violin plots of detected genes, total counts and percent mitochondrial counts per library after CellQC's filters, one figure per species when several were QC'd.
    required: false
  - path: figures/umap_clusters.png
    kind: figure
    tool: run_clustering
    description: UMAP of the cells coloured by Leiden cluster.
    required: true
---

Gene-signature scoring protocol. Use when the goal is to quantify how strongly each cell expresses
a defined gene set (a pathway, a program, a published signature) and to compare that score across
clusters or conditions — NOT to discover markers de novo (use `differential_expression`) and NOT to
assign cell types (use `celltype_annotation`). Adapt to THIS dataset; plan ordered steps that:
1. QC the dataset (`run_scanpy_qc` for one matrix; `run_cellqc` INSTEAD when the data profile says the input is a folder of 10x Cell Ranger outputs (raw + filtered matrices): it corrects ambient RNA, removes doublets and writes the same normalised checkpoint): per-cell metrics, filter, normalize + log1p. Report counts.
2. (Optional) Cluster the cells (`run_clustering`) if the comparison is per-cluster rather than per-condition.
3. Score every cell for the signature. No curated tool covers this — adapt the reference template `score_signature.py` via `run_code` (scanpy `sc.tl.score_genes`), then summarize the score per cluster/condition.
4. Produce figures: a UMAP colored by the signature score and a violin/box plot of the score per group.
5. The report is assembled automatically — do NOT plan a report-writing step.

State which genes from the signature were actually present in the data (missing genes weaken the
score). Report the score distribution per group, not a single number; do not claim a group is
"positive" without showing the per-group statistics.
