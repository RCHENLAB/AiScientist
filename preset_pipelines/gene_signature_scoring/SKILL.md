---
name: gene_signature_scoring
description: Score cells for a gene signature and compare it across clusters/conditions
tools: run_scanpy_qc, run_clustering, run_code
data_type: scrna
---

Gene-signature scoring protocol. Use when the goal is to quantify how strongly each cell expresses
a defined gene set (a pathway, a program, a published signature) and to compare that score across
clusters or conditions — NOT to discover markers de novo (use `differential_expression`) and NOT to
assign cell types (use `celltype_annotation`). Adapt to THIS dataset; plan ordered steps that:
1. QC the dataset (`run_scanpy_qc`): per-cell metrics, filter, normalize + log1p. Report counts.
2. (Optional) Cluster the cells (`run_clustering`) if the comparison is per-cluster rather than per-condition.
3. Score every cell for the signature. No curated tool covers this — adapt the reference template `score_signature.py` via `run_code` (scanpy `sc.tl.score_genes`), then summarize the score per cluster/condition.
4. Produce figures: a UMAP colored by the signature score and a violin/box plot of the score per group.
5. The report is assembled automatically — do NOT plan a report-writing step.

State which genes from the signature were actually present in the data (missing genes weaken the
score). Report the score distribution per group, not a single number; do not claim a group is
"positive" without showing the per-group statistics.
