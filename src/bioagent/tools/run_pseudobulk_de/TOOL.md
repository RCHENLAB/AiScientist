---
name: run_pseudobulk_de
summary: Between-condition DE on per-sample pseudobulk counts with DESeq2, treating the donor as the replicate.
category: analysis
runs_on: hpc:analysis
order: 280
line: scrna
owner: analysis
---
# run_pseudobulk_de

Sums each sample's raw counts into one profile per sample (per cell type when grouped), then tests conditions across samples with DESeq2 (pydeseq2). This is the comparison that treats the animal or donor, not the cell, as the unit of replication.

## When the agent uses it

- For any condition contrast whose design has replicate samples per arm, instead of reading `run_de`'s cell-level p-values as inference.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `min_cells_per_sample` | integer | `10` | a sample contributing fewer cells than this to a cell type is dropped from that cell type's test |
| `min_samples_per_condition` | integer | `2` | refuse to test an arm backed by fewer samples than this — below it there is no replication and no valid p-value. 2 is the COMPUTABILITY floor, not a recommendation: the single-cell DE literature (Squair et al. 2021) asks for >=3 replicates per arm, and a 2-vs-2 result should be reported as underpowered |
| `min_count` | integer | `10` | a gene must reach this many summed counts in at least as many samples as the smaller arm, or it is not tested (edgeR filterByExpr's rule of thumb). Genes nobody detected cannot be tested — they only inflate the BH denominator |
| `padj` | number | `0.05` | adjusted-p (Benjamini-Hochberg) cutoff used to count significant genes |
| `sample_key` | string | — | obs column holding the sample / donor / library id |
| `condition_key` | string | — | obs column holding the experimental condition |
| `group_key` | string | — | cell-type column to test within, one test per cell type |
| `input` | string | `''` | an .h5ad to read INSTEAD of the checkpoint the previous tool wrote — e.g. one a run_code step saved in BIOAGENT_WORK. A file name or a path inside this run's work/ or artifacts/ directory; nothing outside the run is read. Empty = the usual checkpoint. To feed a variant of an upstream result to the next tool, save it under a NEW name and pass it here — never overwrite a tool's checkpoint. It must carry the raw counts in layers['counts'], as run_scanpy_qc's checkpoint does. |
<!-- /generated:parameters -->

## Outputs

- `tables/pseudobulk_all.csv` and `tables/pseudobulk_<group>.csv`, plus the same hand-off files `run_de` writes (`de_<key>_all.csv`, the universe, `.rnk` files), so `run_enrichment` and `run_gsea_prerank` can follow.
- Result: per-group counts, the test used, `skipped_groups`, `warnings`.

## Where it runs

<!-- generated:runs-on -->
As a Slurm CPU job inside `analysis.sif` on HPC3, through the analysis line's job entry point `python -m bioagent.tools.scrna_cli`, reading and writing the run's `work/` and `artifacts/` on dfs3b. When the session has no HPC3 executor it runs in the gateway process.

Needs: `scanpy`.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- It refuses an arm with too few samples to support a test.
- Without pydeseq2 (provided on HPC3 through `BIOAGENT_HPC_PYDEPS`) it falls back to Welch's t-test on log2 CPM and labels the result as a fallback.

## Code and tests

- `tool.py`: `run_pseudobulk_de`, the DESeq2 contrast and the record. Shared helpers and the declared-parameter table (`PARAMS`, `TOOL_SUMMARY`) are in `../_lib/scrna.py`; the defaults listed above come from that table, and `tests/test_declared_params.py` checks that the preset protocols state the same values.
- Tests: `tests/test_scrna_advanced.py`, `tests/test_de_academic_defaults.py`, `tests/test_pseudoreplication_guard.py`.

## What the model is told

<!-- generated:model-description -->
> Differential expression BETWEEN CONDITIONS, aggregated to one profile per sample. Use this — NOT
> run_de — whenever the contrast is a condition (disease vs control, treated vs untreated). run_de
> tests over cells, and cells from one donor are not independent replicates of that donor's
> condition, so its p-values are pseudoreplicated and nearly every gene comes out significant.
> Sums raw counts per `sample_key`, optionally within each `group_key` cell type, then filters
> untestably-low genes and runs DESeq2 (negative-binomial Wald, via pydeseq2; Welch t on log2 CPM
> as a LOUD fallback) + BH. REFUSES a group with fewer than 2 samples per arm and reports it in
> `skipped_groups` rather than falling back to the cell-level test. Writes
> `tables/de_<group_key>_all.csv` (+ the tested universe and per-group .rnk files), so
> run_enrichment and run_gsea_prerank pick the result up with NO `genes` argument — do not paste a
> gene list into them.
<!-- /generated:model-description -->
