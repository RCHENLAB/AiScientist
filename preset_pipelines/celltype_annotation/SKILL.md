---
name: celltype_annotation
version: 2
description: Single-cell cell-type annotation + report (v2 — stability-selected resolution, raw-expression label confirmation, preranked GSEA)
tools: run_scanpy_qc, run_doublet_detection, run_integration, run_clustering, run_de, run_marker_annotation, run_composition, run_enrichment, run_gsea_prerank, run_code
data_type: scrna
---

Canonical marker-based single-cell cell-type annotation protocol. Use when the goal is to
assign a cell-type label to each cluster of an scRNA-seq dataset by reading its marker genes.
Adapt the parameters to THIS dataset; plan ordered steps that:

1. QC the dataset (`run_scanpy_qc`): per-cell metrics, filter low-quality cells/genes,
   normalize + log1p + HVG. Report pre/post counts. For snRNA-seq, a few hundred UMIs per
   nucleus is EXPECTED — do not treat it as a failed run and do not filter it away.
1b. **Doublets** (`run_doublet_detection`): two cells in one droplet express both parents'
   programmes and form an "intermediate" cluster that reads as a novel transitional cell type.
   Run this before clustering. Report the rate.
1c. **Integration** (`run_integration`) — REQUIRED when the object holds more than one sample.
   Check the obs profile for a donor/sample/batch column first. Without it the cells cluster by
   donor and every label below is really a donor label, with no visible symptom. Report
   `method_used` and the before/after batch silhouette; if the tool returns a warning that the
   batches did not mix, say so rather than proceeding as if they had.
2. Cluster the cells (`run_clustering`) with **`select_resolution: true`**. Every label
   assigned later inherits this partition, so the resolution is chosen by bootstrap stability
   (the finest resolution whose clusters still reproduce under resampling, scored by ARI),
   not left at the default. Report the selected resolution AND how it was selected
   (`resolution_source`), and sanity-check it against biology: the major lineages of the
   panel should come apart.
3. Find marker genes per cluster (`run_de`): Wilcoxon `rank_genes_groups`. This also writes
   the complete tested universe and a full ranked list per cluster, which steps 4-5 need.
4. Pathway context — `run_enrichment` (ORA over the significant markers, against the tested
   universe as background) and `run_gsea_prerank` (preranked GSEA over the whole ranking).
   Run BOTH when pathway interpretation matters: ORA thresholds a top-N list, GSEA walks the
   entire ranking and returns a signed NES, so GSEA can see coordinated shifts that no
   per-gene cutoff keeps. They use different inputs and different null hypotheses, so **do
   not require them to agree**, and do not report disagreement as an error in either.
5. Assign a cell-type label to each cluster with **`run_marker_annotation`**, passing a `panel`
   and `discriminators` built for THIS tissue (the `annotate_clusters_by_markers_v2` skill
   explains how to build them and why the discriminator list is not just the panel again).
   Signature scores are a first pass only: the z-scored argmax confidently mislabels lineages
   that share markers, so the final call comes from raw marker expression, and a cluster with
   no dominant coherent signal stays `Unassigned` rather than being forced into the nearest
   label. Report which clusters the raw check CORRECTED and which stayed unassigned.
5b. **Composition** (`run_composition`): the proportion each label makes up, per sample.
6. Produce figures: a UMAP colored by cluster and by assigned cell type, and violin/dot plots
   of canonical marker genes.
7. The methods + results report is assembled automatically — do NOT plan a report-writing step.

Ground every label in the marker genes the tools actually returned. Do not fabricate cell
types, gene names, or numbers a tool did not return; if a step's tool errors, report it
honestly rather than inventing a result.

Reporting discipline — these belong in the write-up, not only in the logs:

- the marker panel used and where it came from;
- the resolution and how it was chosen;
- every cluster the raw-expression check corrected away from its first-pass label;
- every `Unassigned` cluster, with its cell count;
- the ORA background actually used (the tested universe, not a round number) and the GSEA
  parameters (set-size limits, permutations, seed);
- **null findings.** A group where nothing cleared FDR is a result and stays in the report.

Enrichment is association with an expression programme — not evidence of pathway activity,
and not evidence of causation. Word it that way.
