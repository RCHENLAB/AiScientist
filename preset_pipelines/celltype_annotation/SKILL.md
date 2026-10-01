---
name: celltype_annotation
version: 2
description: Single-cell cell-type annotation + report (v2 — stability-selected resolution, raw-expression label confirmation, preranked GSEA)
tools: run_scanpy_qc, run_doublet_detection, run_integration, run_clustering, run_de, run_marker_annotation, run_composition, run_enrichment, run_gsea_prerank, literature_search, run_code
data_type: scrna
---

Canonical marker-based single-cell cell-type annotation protocol. Use when the goal is to
assign a cell-type label to each cluster of an scRNA-seq dataset by reading its marker genes.
Adapt the parameters to THIS dataset; plan ordered steps that:

0. **Establish what the matrix IS, and the rules, before touching it** (`run_code`) — read-only,
   and BEFORE any normalization. Two outputs, both written as tables the later steps read back:
   * *Provenance.* Inspect `X`, `.raw`, `layers`, value ranges, sparsity and integer-ness, and
     check whether matrix-derived totals reproduce any stored `nCount`/`nFeature` fields. A
     published object often arrives already normalized and log1p'd: normalizing it again has NO
     symptom — every tool succeeds, every figure renders, and every number after it is wrong. So
     rule from the NUMBERS, and say which way you ruled:
     - **Counts.** Every value non-negative and integer-valued to tolerance, the row sums
       reproduce a stored total-count field (`nCount_RNA`, `total_counts`), the per-row non-zero
       counts reproduce the stored detected-feature field, and no `layers`/`.raw` entry carries a
       different scale. **That is sufficient — proceed.** An empty `uns` and a missing
       transformation history are a LIMITATION TO REPORT, not a stop condition: a matrix that
       reproduces its own stored QC fields exactly is not made unusable by the absence of a
       written record, and stopping there forfeits the whole quantitative analysis over a
       provenance note.
     - **Not counts.** Any negative or non-integer value, row sums that do not reproduce the
       stored totals, or a differently-scaled representation present. Then say so and drop the
       count-dependent steps — never recover counts by exponentiating, rounding, or renaming.
     State which representation supports which operation (linear-scale fold changes, detection
     fractions, count-based tests) and which of the two rulings you applied.
   * *The decision rule.* Name the primary estimand, the unit of replication, the minimum
     support per group (cells per arm, per cluster), and the effect-size gate — and write them
     down NOW, while no differential or marker result is visible yet. A threshold chosen after
     seeing which one flatters the answer is not a threshold. Say what happens when a gate
     FAILS: the estimate is marked unsupported and omitted, not softened until it passes.
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
   Run BOTH when pathway interpretation matters: ORA tests the SIGNIFICANT set (every gene
   passing `padj` and `lfc` — not a top-N of it, which would make the input size differ per
   cluster and the results incomparable), GSEA walks the entire ranking and returns a signed
   NES, so GSEA can see coordinated shifts that no per-gene cutoff keeps. They use different inputs and different null hypotheses, so **do
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
6b. **Literature grounding** (`literature_search`): search on the cell types, markers and
   pathway terms the analysis actually surfaced. The manuscript's `## References` are built ONLY
   from an ACCEPTED `literature_search` step — with no such step they are silently empty, and the
   labels arrive with nothing tying them to the published biology.
7. Do NOT plan a report-WRITING or rendering step — the methods + results report is assembled
   automatically from the accepted results. That is about writing it, NOT about interpreting:
   the enrichment (step 4) and literature (step 6b) evidence is still required.

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

**Pathway analysis: use `run_enrichment` / `run_gsea_prerank` when the design supports them, and
when it does not, still take the gene sets from disk.** Both tools read `.gmt` libraries that are
ALREADY ON DISK next to the tools — GO_Biological_Process_2023, Reactome_2022, MSigDB_Hallmark_2020
(`BIOAGENT_GENESETS_DIR` overrides the location) — offline, no download, no network. But both also
consume `run_de`'s output table and return ORA/GSEA p-values and FDR. A design with no biological
replication forbids exactly those p-values and therefore often skips `run_de` entirely, which
leaves both tools unusable through no fault of the plan. That is NOT a reason to go to the network:
run 3c5fbc8608a7 hand-rolled a p-value-free pathway score, went looking for a collection to fetch,
hit the confirmation guarding downloads from non-allowlisted hosts, was declined, and recorded
`"collection_status": "no verified authorized species-compatible collection"` — with three verified
libraries sitting in the directory the tools read. When you compute a pathway summary yourself,
read the `.gmt` files from that same local directory and say in the step that you did.

Enrichment is association with an expression programme — not evidence of pathway activity,
and not evidence of causation. Word it that way.

**Sensitivity analyses may not be shopped.** When a step re-runs the analysis under a different
QC cutoff, depth standardization, mixture or annotation, the PRIMARY estimate stays primary. A
variant is never promoted because it produced a larger effect, a cleaner figure, or a more
attractive biology — report the variants side by side with the cell mass each retains, and let
them disagree. The purpose is to show how much the answer depends on a choice, not to find the
choice that gives the best answer.

**The unit of replication is what the DESIGN replicates, not what the object contains.** Step 5b
compares proportions between samples, and a proportion shift is a claim about the condition: with
one library per arm there is nothing that separates the condition from the individual, so report
proportions and their denominators and make no inferential claim. The `differential_expression`
skill carries the measured version of this (a synthetic 4-donor design where the cell-level test
called 310 of 400 genes significant when exactly 1 differed); the same arithmetic applies here.
