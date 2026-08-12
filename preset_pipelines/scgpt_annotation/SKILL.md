---
name: scgpt_annotation
description: scGPT foundation-model per-cell annotation for a .h5ad — transfers reference cell-type labels + calibrated confidence to every cell, AND independently cross-validates ANY existing celltype/majorclass labels the data already carries. Use for scGPT / .sif / foundation-model annotation, per-cell labelling, OR an independent second-opinion check of a dataset's existing cell-type labels.
tools: scgpt_annotate, run_scanpy_qc, run_clustering, run_de, run_code
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
2. QC the dataset (`run_scanpy_qc`) — this prepares the data for the INDEPENDENT structure/figures below, not for scGPT.
3. Cluster independently (`run_clustering`): neighbors -> Leiden -> UMAP, giving data-driven structure that does NOT depend on the scGPT labels.
4. Find marker genes per cluster (`run_de`) so the clusters' biology can be read directly from the data.
5. CROSS-VALIDATE the scGPT per-cell labels: a confusion-style comparison against the independent Leiden cluster structure AND — when the dataset carries a `celltype`/`majorclass` column — directly against those existing labels (agreement %, per-label confidence, the populations where scGPT disagrees), plus the scGPT confidence distribution flagging low-confidence cells/populations. **Merge the predictions by BARCODE, never by row order (see the ⚑ callout below).** No curated tool covers this — adapt the reference template `crossvalidate_scgpt_vs_leiden.py` via `run_code`; it does the barcode-safe merge and covers BOTH the Leiden confusion table AND the existing majorclass/celltype agreement, so adapt it rather than writing the merge from scratch.
6. Produce figures: UMAP colored by scGPT predictions AND by Leiden cluster, the confidence distribution, and violins of canonical markers for the predicted types.
7. The report is assembled automatically — do NOT plan a report-writing step.

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

State the label-transfer caveats honestly (reference-bounded taxonomy, no fine-tuning, query-vs-
reference differences). Do not fabricate cell types, gene names, confidences, or numbers a tool did
not return. If `scgpt_annotate` is not enabled (no GPU/image) or errors, say so and fall back to the
marker-based path (see the `celltype_annotation` skill) rather than inventing labels.
