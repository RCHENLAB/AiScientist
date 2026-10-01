---
name: run_de
summary: Differential expression with a Wilcoxon test over cells: each group against the rest, or condition against control, optionally within each cell type.
category: analysis
runs_on: hpc:analysis
order: 220
line: scrna
owner: analysis
---
# run_de

Tests genes for expression differences with scanpy's `rank_genes_groups` (Wilcoxon by default). In marker mode each level of `groupby` is compared with all other cells. Given `reference` it compares a condition with a control, and adding `stratify_by` repeats that contrast separately within each cell type, listing the cell types it had to skip.

## When the agent uses it

- Markers per cluster after clustering.
- KO vs WT or disease vs control per cell type on labelled data, with no re-clustering.
- When the design has replicate samples, the replicate-aware comparison is `run_pseudobulk_de`; `run_de` treats cells as independent, and its result carries an `inference` label that says so.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `groupby` | string | `'leiden'` | the obs column whose levels are compared |
| `method` | string (`wilcoxon`, `t-test`, `t-test_overestim_var`, `logreg`) | `'wilcoxon'` | the rank test rank_genes_groups uses |
| `n_genes` | integer | `50` | rows kept per group — per DIRECTION when it is a contrast |
| `reference` | string | `'rest'` | the baseline level. 'rest' gives one-vs-rest MARKERS; a named level (e.g. 'WT') gives a real condition-vs-control contrast |
| `stratify_by` | string | `''` | an EXISTING cell-type column — runs the contrast separately within each cell type instead of pooling them |
| `min_cells` | integer | `30` | a group with fewer cells than this in either arm is skipped. 30 is the usual rule-of-thumb floor for the rank test's normal approximation — an ENGINEERING guard, not a literature threshold |
| `min_pct` | number | `0.1` | a gene must be detected in at least this fraction of the cells of one of the two populations compared, or it is not tested at all (Seurat FindMarkers' min.pct). Without it most genes enter the test undetected — on the DDX41 retina object 2/3 did — which triples the BH denominator and floods the ranking with divide-by-zero fold-changes above 2^20. 0 disables |
| `tie_correct` | boolean | `True` | tie-correct the Wilcoxon normal approximation. A single-cell matrix is ~90% zeros, so ties dominate every comparison; scanpy's default (False) is anti-conservative on sparse data |
| `padj` | number | `0.05` | adjusted-p (Benjamini-Hochberg) cutoff for calling a gene significant |
| `lfc` | number | `0.25` | minimum \|log2 fold-change\| for calling a gene significant — applied together with `padj`, and drawn as the volcano's vertical line. 0.25 is the single-cell convention (Seurat's FindMarkers threshold); 1.0 is a bulk-RNA habit and hides most real single-cell effects |
| `force` | boolean | `False` | run a pooled per-cell test across a CONDITION column anyway. The result is pseudoreplicated and must be reported as non-inferential |
| `groups` | array of string | — | restrict the comparison to these levels of groupby |
| `input` | string | `''` | an .h5ad to read INSTEAD of the checkpoint the previous tool wrote — e.g. one a run_code step saved in AISCIENTIST_WORK. A file name or a path inside this run's work/ or artifacts/ directory; nothing outside the run is read. Empty = the usual checkpoint. To feed a variant of an upstream result to the next tool, save it under a NEW name and pass it here — never overwrite a tool's checkpoint. |
<!-- /generated:parameters -->

## Outputs

- `work/adata_de.h5ad`.
- Tables: `tables/de_<key>_all.csv` with exactly the columns `group,gene,log2fc,pval,pval_adj,score`, one CSV per group, `de_<key>_universe.txt` (the tested genes, which `run_enrichment` uses as its background), `rank_<key>_*.rnk` (for `run_gsea_prerank`), and `de_<key>_inference.json` so the inference label travels with the tables.
- Figures: a marker dotplot; in a contrast, one volcano per group.
- Result: `significant_by_group`, `skipped_groups`, `cells_by_group_and_arm`, `inference`, `warnings`.

## Where it runs

<!-- generated:runs-on -->
As a Slurm CPU job inside `analysis.sif` on HPC3, through the analysis line's job entry point `python -m aiscientist.tools.scrna_cli`, reading and writing the run's `work/` and `artifacts/` on dfs3b. When the session has no HPC3 executor it runs in the gateway process.

Needs: `scanpy`.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- A cell-level test. With few donors its p-values are pseudoreplicated; the result says so, and later steps must not report them as inferential.
- `n_genes` caps the rows per group (per direction in a contrast).
- `method` is one of scanpy's backends; DESeq2 lives in `run_pseudobulk_de`.

## Code and tests

- `tool.py`: `run_de`, its ranking, BH and volcano helpers, and the record. Shared helpers and the declared-parameter table (`PARAMS`, `TOOL_SUMMARY`) are in `../_lib/scrna.py`; the defaults listed above come from that table, and `tests/test_declared_params.py` checks that the preset protocols state the same values.
- Tests: `tests/test_scrna_pack.py`, `tests/test_deg_contrast.py`, `tests/test_de_academic_defaults.py`, `tests/test_pseudoreplication_guard.py`, `tests/test_design_by_arm.py`.

## What the model is told

<!-- generated:model-description -->
> Differential expression via rank_genes_groups (Wilcoxon), in EITHER of two shapes. (1) MARKERS
> (default): each level of `groupby` vs the rest — 'what defines this cluster'. (2) CONTRAST: pass
> `reference` (the CONTROL level of `groupby`, e.g. "WT") to compare condition vs control instead
> of vs rest; add `stratify_by` (an EXISTING cell-type label column) to run that contrast
> SEPARATELY WITHIN EACH CELL TYPE. Use (2) for a KO-vs-WT / disease-vs-control DEG study whose
> arms have fewer than 2 replicate samples each; with >=2 samples per arm use run_pseudobulk_de
> instead — (2) treats cells as independent, so its p-values are pseudoreplicated. Either way do
> NOT hand-write the contrast in run_code, and do NOT re-cluster a dataset that already has
> labels. Reads `adata_clustered.h5ad` if present, otherwise `adata_qc.h5ad`, so a labeled dataset
> needs run_scanpy_qc ONLY (run_clustering is for unlabeled data). Writes `work/adata_de.h5ad` and
> CSV tables `tables/de_<key>_all.csv` (+ one per group), where <key> is `stratify_by` when
> stratified, else `groupby`. The tables have EXACTLY these columns:
> `group,gene,log2fc,pval,pval_adj,score` — if you ever read a DE table in run_code, use THOSE
> names (NOT Seurat-style `gene_name`/`p_val_adj`/`avg_log2FC`). A contrast also writes a volcano
> per group and reports `significant_by_group` (up/down counts) plus `skipped_groups` (cell types
> with too few cells in an arm — report those as not covered). `n_genes` caps rows per group, PER
> DIRECTION for a contrast.
<!-- /generated:model-description -->
