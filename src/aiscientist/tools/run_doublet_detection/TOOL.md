---
name: run_doublet_detection
summary: Scores droplets that probably held two cells (Scrublet) and removes them unless asked only to annotate.
category: qc
runs_on: hpc:analysis
order: 260
line: scrna
owner: analysis
---
# run_doublet_detection

Runs Scrublet on the raw counts after QC and before clustering. Two cells in one droplet express both parents' programmes and can form an intermediate cluster that reads as a new cell type; this step removes them (or, with `filter: false`, only marks them).

## When the agent uses it

- After `run_scanpy_qc`, before `run_clustering`.

## When the agent does NOT use it

- After `run_cellqc`: DoubletFinder and scDblFinder have already called the doublets and the decider's
  were removed. If obs already holds doublet calls (`doubletfinder_class`, `scdblfinder_class`,
  `predicted_doublet`) the tool returns `skipped` without running Scrublet, unless `force: true`.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `expected_doublet_rate` | number | `0.06` | prior doublet rate handed to the detector — roughly the ~0.8% per 1,000 cells that 10x Genomics quotes, so 0.06 suits a ~8,000-cell lane. Set it from YOUR loading, not from this default |
| `flag_rate_above` | number | `0.2` | warn when the detected doublet fraction exceeds this. An ENGINEERING guard, not a literature threshold: it exists to catch a mis-set expected rate or an overloaded lane, and a run above it is a prompt to check the loading, not a finding |
| `filter` | boolean | — | remove the predicted doublets (default) or only annotate them |
| `batch_key` | string | — | obs column to simulate doublets within, per batch |
| `threshold` | number | — | explicit score cutoff; omit to let scrublet choose one |
| `force` | boolean | — | score again even though obs already holds doublet calls (from run_cellqc or an earlier run); off by default |
| `input` | string | `''` | an .h5ad to read INSTEAD of the checkpoint the previous tool wrote — e.g. one a run_code step saved in AISCIENTIST_WORK. A file name or a path inside this run's work/ or artifacts/ directory; nothing outside the run is read. Empty = the usual checkpoint. To feed a variant of an upstream result to the next tool, save it under a NEW name and pass it here — never overwrite a tool's checkpoint. Doublets are scored and filtered IN PLACE, in that file. |
<!-- /generated:parameters -->

## Outputs

- Updates `work/adata_qc.h5ad` in place (filtered or annotated) and writes `tables/doublet_summary.csv`.
- Result: `doublet_rate`, cells before and after.

## Where it runs

<!-- generated:runs-on -->
As a Slurm CPU job inside `analysis.sif` on HPC3, through the analysis line's job entry point `python -m aiscientist.tools.scrna_cli`, reading and writing the run's `work/` and `artifacts/` on dfs3b. When the session has no HPC3 executor it runs in the gateway process.

Needs: `scanpy`.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- Scrublet needs scikit-image, which `analysis.sif` lacks. The job installs it into the run's own workspace when it is reported missing (`run_deps.py`) and removes it after publish.
- A rate above about 20% usually means the threshold, not the biology. `batch_key` simulates doublets within each batch.

## Code and tests

- `tool.py`: `run_doublet_detection` and the record. Shared helpers and the declared-parameter table (`PARAMS`, `TOOL_SUMMARY`) are in `../_lib/scrna.py`; the defaults listed above come from that table, and `tests/test_declared_params.py` checks that the preset protocols state the same values.
- Tests: `tests/test_scrna_advanced.py`, `tests/test_run_deps.py`.

## What the model is told

<!-- generated:model-description -->
> Scrublet doublet scoring on raw counts, run AFTER run_scanpy_qc and BEFORE run_clustering. Two
> cells in one droplet express both parents' programmes and form an 'intermediate' cluster that
> reads as a novel transitional cell type — this is how a single-cell analysis invents a
> population. Filters predicted doublets by default (`filter: false` to annotate only) and returns
> the rate; a rate above ~20% usually means the threshold, not the biology.
<!-- /generated:model-description -->
