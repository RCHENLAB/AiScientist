---
name: scgpt_annotation
description: scGPT foundation-model per-cell annotation for a .h5ad — transfers reference cell-type labels + calibrated confidence to every cell, AND independently cross-validates ANY existing celltype/majorclass labels the data already carries. Use for scGPT / .sif / foundation-model annotation, per-cell labelling, OR an independent second-opinion check of a dataset's existing cell-type labels.
tools: scgpt_annotate, run_scanpy_qc, run_clustering, run_de, run_enrichment, literature_search, run_code
data_type: scrna
---

Rigorous scGPT foundation-model annotation protocol. scGPT is a pretrained gene/expression
transformer — a DIFFERENT method from naming clusters by their markers — so treat its labels as a
strong hypothesis to validate, not ground truth.

**WHEN TO USE this skill — pick it whenever ANY of these holds:**
- The query is an AnnData `.h5ad` and the ask involves scGPT, a `.sif`/foundation model, per-cell
  cell-type annotation, or label transfer from a reference atlas.
- Per-cell labels + calibrated confidence for EVERY cell are wanted (not just per-cluster names).
- **The dataset ALREADY carries a `celltype` / `majorclass` / predicted-label column.** Existing
  labels do NOT disqualify this skill — they make it a CROSS-VALIDATION task. scGPT is the
  independent foundation-model second opinion on those labels: agreement rate, per-cell confidence,
  and the cells/populations where scGPT DISAGREES or is low-confidence. Reusing the existing labels
  and merely naming them by markers CANNOT provide this. Do NOT skip scGPT just because labels
  exist — a pre-annotated `.h5ad` is exactly the cross-validation case this skill exists for.

**When NOT to use:** the dataset is not an `.h5ad` query, or the user explicitly wants marker-based
naming without a foundation model (use the `celltype_annotation` skill instead).

Adapt parameters to THIS dataset; plan ordered steps that:
1. Run `scgpt_annotate` for a per-cell label + confidence for EVERY cell (the primary annotation). It preprocesses internally (gene-vocabulary alignment, HVG, log1p) on a GPU batch job, so do NOT normalize/HVG the data yourself beforehand — pass the query as-is. It writes `data/scgpt_predictions.csv` — **one row per RAW-upload cell, indexed by the original cell barcode** (columns `index, predictions, confidence`).
1b. **Establish what the matrix IS, and the rules** (`run_code`) — read-only, and before the QC
   step below. scGPT (step 1) preprocesses internally from the raw upload, so this audits the
   object for everything AFTER it. Two outputs, written as tables the later steps read back:
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
     State which representation supports which operation and which ruling you applied. `scgpt_annotate`
     preprocesses the raw upload itself, so the first ruling is what lets it run at all.
   * *The decision rule.* Name the minimum support per label (cells per group), the confidence
     handling, and the crosswalk rule for comparing a reference taxonomy against the existing
     labels — and write them down NOW, before any agreement percentage is visible. An agreement
     threshold chosen after seeing the confusion table is not a threshold. Say what happens when
     a gate fails: the comparison is marked unsupported, not relaxed until it passes.
2. QC the dataset (`run_scanpy_qc`) — this prepares the data for the INDEPENDENT structure/figures below, not for scGPT.
3. Cluster independently (`run_clustering`): neighbors -> Leiden -> UMAP, giving data-driven structure that does NOT depend on the scGPT labels.
4. Find marker genes per cluster (`run_de`) so the clusters' biology can be read directly from the data.
5. CROSS-VALIDATE the scGPT per-cell labels: a confusion-style comparison against the independent Leiden cluster structure AND — when the dataset carries a `celltype`/`majorclass` column — directly against those existing labels (agreement %, per-label confidence, the populations where scGPT disagrees), plus the scGPT confidence distribution flagging low-confidence cells/populations. **Merge the predictions by BARCODE, never by row order (see the ⚑ callout below).** No curated tool covers this — adapt the reference template `crossvalidate_scgpt_vs_leiden.py` via `run_code`; it does the barcode-safe merge and covers BOTH the Leiden confusion table AND the existing majorclass/celltype agreement, so adapt it rather than writing the merge from scratch.
6. Produce figures: UMAP colored by scGPT predictions AND by Leiden cluster, the confidence distribution, and violins of canonical markers for the predicted types.
7. **GROUND THE BIOLOGY — plan these, they are what the write-up is allowed to draw on.**
   `run_enrichment` over the step-4 cluster markers (and over any condition contrast the study
   carries), and `literature_search` on the cell types and genes the analysis actually surfaced.
   The report writer may name ONLY pathways, enrichment terms and citations that appear in an
   ACCEPTED step result — so with no enrichment step the report cannot name a single pathway, and
   with no literature step `## References` is silently empty. Annotation is not by itself a
   finding: state what the labels and their disagreements MEAN for the question that was asked.
8. Do NOT plan a report-WRITING or rendering step — the manuscript is assembled automatically from
   the accepted results. That is about writing it, NOT about interpreting: step 7 is still required.

**⚑ THE CELL SETS DIFFER — always merge by barcode.** scGPT labels EVERY cell of the RAW upload
(step 1); QC (step 2) filters some out, so the QC'd / clustered `adata` has FEWER cells than the
prediction CSV (e.g. 11,977 predictions vs 11,970 QC'd cells). Merge scGPT predictions into the
analyzed `adata` by cell BARCODE — `pred = pd.read_csv(".../scgpt_predictions.csv").set_index("index");
adata.obs["scgpt_pred"] = pred["predictions"].reindex(adata.obs_names)` (confidence likewise), or the
`adata.obs_names.intersection(pred.index)` form the template uses. **NEVER assign a length-11977
column onto an 11970-cell `adata` or merge by row position** — the N-vs-M mismatch raises a pandas
alignment error and the whole cross-validation step fails (this is the single most common way this
skill's runs stall). The same barcode alignment applies to the Leiden AND the majorclass/celltype
comparison.

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

**Confidence is not calibration, and agreement is not accuracy.** A returned confidence is
calibrated only where independent, donor- or study-separated evaluation on a comparable query
says so; without that, use the scores to RANK cells for review, never as an error probability, and
never equate scores from two different models. Agreement with the existing labels measures
concordance between two annotation systems — neither of which is ground truth — so a high number
is not validation of either. Report the populations where they disagree; that is the finding.

State the label-transfer caveats honestly (reference-bounded taxonomy, no fine-tuning, query-vs-
reference differences). Do not fabricate cell types, gene names, confidences, or numbers a tool did
not return. If `scgpt_annotate` is not enabled (no GPU/image) or errors, say so and fall back to the
marker-based path (see the `celltype_annotation` skill) rather than inventing labels.
