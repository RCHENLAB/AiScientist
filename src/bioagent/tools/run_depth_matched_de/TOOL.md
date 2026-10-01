---
name: run_depth_matched_de
summary: Downsamples the deeper-sequenced arm to the other's depth and re-runs the comparison, to separate biology from sequencing depth.
category: analysis
runs_on: hpc:analysis
order: 240
line: scrna
owner: analysis
---
# run_depth_matched_de

Checks whether a DE result survives equal sequencing depth. It randomly removes reads from the deeper arm until both arms match, re-runs the same gene comparison, and reports how much of the original ordering survives (Spearman correlation) and which genes keep their place.

## When the agent uses it

- After `run_de`, when the arms differ in depth (the dataset profile reports the per-arm depth), and before a global up or down shift is read as biology.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `groupby` | string | `'sampleid'` | the obs column holding the experimental CONDITION |
| `reference` | string | `''` | the CONTROL level of `groupby` (e.g. 'WT'). Required — the check is a two-level contrast |
| `stratify_by` | string | `''` | the CELL-TYPE column; the check runs separately within each type, because depth imbalance differs per type and a pooled answer hides it |
| `n_genes` | integer | `100` | how many top-ranked genes per direction enter the comparison. Deep enough that the correlation is not driven by three genes, shallow enough to stay the list a reader would actually look at |
| `min_cells` | integer | `30` | skip a cell type with fewer cells than this in either arm — the usual floor for the rank test's normal approximation |
| `seed` | integer | `0` | random seed for the down-sampling, so the check reproduces |
| `min_ratio` | number | `1.05` | skip a cell type whose arms already differ by less than this ratio in median library size — there is no imbalance to correct |
| `input` | string | `''` | an .h5ad to read INSTEAD of the checkpoint the previous tool wrote — e.g. one a run_code step saved in BIOAGENT_WORK. A file name or a path inside this run's work/ or artifacts/ directory; nothing outside the run is read. Empty = the usual checkpoint. To feed a variant of an upstream result to the next tool, save it under a NEW name and pass it here — never overwrite a tool's checkpoint. |
<!-- /generated:parameters -->

## Outputs

- `tables/depth_matched_genes.csv`, `tables/depth_matched_summary.csv` and `figures/depth_matched_correlation.png`.
- Result: the correlation, a verdict per group, the depth-robust genes, `skipped_groups`, `warnings`.

## Where it runs

<!-- generated:runs-on -->
As a Slurm CPU job inside `analysis.sif` on HPC3, through the analysis line's job entry point `python -m bioagent.tools.scrna_cli`, reading and writing the run's `work/` and `artifacts/` on dfs3b. When the session has no HPC3 executor it runs in the gateway process.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- One seeded downsample per call. A low correlation says the original ordering depended on depth; it does not say which individual genes are artefacts.

## Code and tests

- `tool.py`: `run_depth_matched_de` with its matching, correlation and figure helpers, and the record. Shared helpers and the declared-parameter table (`PARAMS`, `TOOL_SUMMARY`) are in `../_lib/scrna.py`; the defaults listed above come from that table, and `tests/test_declared_params.py` checks that the preset protocols state the same values.
- Tests: `tests/test_depth_matched_de.py`, `tests/test_scrna_pack.py`.

## What the model is told

<!-- generated:model-description -->
> Separate biology from sequencing depth. When the two arms differ in reads per cell, every
> ranking inherits that bias — a gene detected in more cells because more molecules were sampled
> looks up-regulated, and abundant-transcript pathways (translation, ribosome, RNA metabolism)
> move together in the deeper arm. This down-samples the deeper arm within each cell type until
> the per-cell UMI distributions match, re-runs the SAME Wilcoxon contrast, and Spearman-
> correlates the original ranking against the depth-matched one PER DIRECTION. Use it whenever the
> dataset profile flags a depth imbalance, and always before interpreting a pan-cell-type
> signature. rho >= 0.5 = preserved; rho < 0 = the ranking INVERTS once depth is equalised, i.e. a
> detection artefact that must not be reported as regulation. Reads the raw `counts` layer
> run_scanpy_qc stores, and writes `tables/depth_matched_summary.csv`,
> `tables/depth_matched_genes.csv` and a figure. Do NOT hand-write this as run_code: correlating
> per-gene fold-changes against a single median library size is not a computable operation.
<!-- /generated:model-description -->
