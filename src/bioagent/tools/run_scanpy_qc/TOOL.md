---
name: run_scanpy_qc
summary: Filters cells and genes by quality, normalises and log-transforms the counts, and selects highly variable genes.
category: analysis
runs_on: hpc:analysis
order: 200
line: scrna
owner: analysis
---
# run_scanpy_qc

The first step of the single-cell line. It computes per-cell quality metrics (genes detected, total counts, mitochondrial share), removes low-quality cells and rarely detected genes by the declared thresholds, normalises and log-transforms the counts, and selects highly variable genes. Every later scRNA tool reads the checkpoint it writes.

## When the agent uses it

- First, on any single-cell matrix: `.h5ad`, a 10x `.h5` or `filtered_feature_bc_matrix/` folder, loom, or a text matrix.
- On a subset or merge that a `run_code` step saved, through `input`.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `min_genes` | integer | `200` | drop a cell detecting fewer genes than this — an empty droplet or a dying cell |
| `min_cells` | integer | `3` | drop a gene detected in fewer cells than this — too sparse to support any test |
| `max_pct_mt` | number | `10.0` | drop a cell whose reads are more than this percent mitochondrial (a stressed or lysed cell). 10 is the common working threshold for tissue; the Seurat and scanpy tutorials use 5 for PBMC, and single NUCLEI need far less (1-5) because a nucleus should carry almost no mitochondrial signal. Raise it only for a tissue known to be mitochondria-rich, and say so |
| `n_top_genes` | integer | `2000` | how many highly-variable genes to keep for the embedding — more genes carry more structure and more noise. 2000 is Seurat's default and the usual starting point |
| `warn_removed_pct` | number | `50.0` | warn when QC discards more than this percent of cells. An ENGINEERING guard, not a literature threshold: losing half a dataset usually means a threshold is wrong for this tissue, and it should be checked before the result is used |
| `mito_prefix` | string | `'MT-'` | the name prefix that marks a mitochondrial gene, matched without regard to case, so the default also covers mouse 'mt-'. It is the only way `max_pct_mt` knows which genes to count: a dataset whose genes match nothing gets no mitochondrial filter at all |
| `gene_symbols_key` | string | `''` | a `var` column holding gene SYMBOLS, for data whose gene names are Ensembl IDs (cellxgene files keep the symbols in `feature_name`). The mitochondrial prefix is then matched against that column. Empty = match the gene names themselves |
| `input` | string | `''` | an .h5ad to QC INSTEAD of the bound dataset — e.g. a subset or merge a run_code step saved in BIOAGENT_WORK. A file name or a path inside this run's work/ or artifacts/ directory; nothing outside the run is read. Empty = the bound dataset. |
<!-- /generated:parameters -->

## Outputs

- `work/adata_qc.h5ad`, the checkpoint the rest of the line reads. The raw counts are kept in `layers["counts"]` for the tools that need them (doublets, pseudobulk, depth matching).
- Figures in `artifacts/figures/`: QC violins, genes-vs-counts and mitochondrial scatter plots, the highly-variable-gene plot.
- Result: cells and genes before and after, the thresholds used, `warnings` (for example more than `warn_removed_pct` of cells removed, or no gene matched `mito_prefix`), and `read_from`.

## Where it runs

<!-- generated:runs-on -->
As a Slurm CPU job inside `analysis.sif` on HPC3, through the analysis line's job entry point `python -m bioagent.tools.scrna_cli`, reading and writing the run's `work/` and `artifacts/` on dfs3b. When the session has no HPC3 executor it runs in the gateway process.

Needs: `scanpy`.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- Thresholds are fixed numbers for the whole run, not adaptive per sample. Single-nucleus data needs a much lower `max_pct_mt` than the tissue default.
- Mitochondrial genes are found by name prefix. Data whose gene names are Ensembl IDs needs `gene_symbols_key`, or it gets no mitochondrial filter at all.
- It loads the whole matrix into memory inside the job.

## Code and tests

- `tool.py`: `run_scanpy_qc` and its catalog record. Shared helpers and the declared-parameter table (`PARAMS`, `TOOL_SUMMARY`) are in `../_lib/scrna.py`; the defaults listed above come from that table, and `tests/test_declared_params.py` checks that the preset protocols state the same values.
- Tests: `tests/test_scrna_pack.py`, `tests/test_tool_self_diagnosis.py`, `tests/test_tool_input.py`, `tests/test_declared_params.py`.

## What the model is told

<!-- generated:model-description -->
> REAL scanpy QC on the uploaded single-cell dataset: per-cell metrics, cell/gene filtering,
> normalization, log1p, and HVG selection. Writes QC violin/scatter figures and a checkpoint.
> Returns pre/post cell-gene counts and the thresholds used. Run this FIRST.
<!-- /generated:model-description -->
