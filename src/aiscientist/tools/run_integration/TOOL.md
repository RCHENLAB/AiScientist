---
name: run_integration
summary: Corrects sample or batch effects (Harmony, or ComBat when harmonypy is absent) and checks that the batch signal drops.
category: analysis
runs_on: hpc:analysis
order: 270
line: scrna
owner: analysis
---
# run_integration

Removes systematic differences between samples or batches so that cells group by cell type rather than by library. It uses Harmony when harmonypy is installed and ComBat (bundled with scanpy) otherwise, and measures the batch silhouette before and after.

## When the agent uses it

- Before clustering, whenever the object holds more than one sample; without it cells can cluster by donor, and every downstream label is really a donor label.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `batch_key` | string | — |  |
| `method` | string (`auto`, `harmony`, `combat`) | — |  |
| `n_pcs` | integer | — |  |
| `input` | string | `''` | an .h5ad to read INSTEAD of the checkpoint the previous tool wrote — e.g. one a run_code step saved in AISCIENTIST_WORK. A file name or a path inside this run's work/ or artifacts/ directory; nothing outside the run is read. Empty = the usual checkpoint. To feed a variant of an upstream result to the next tool, save it under a NEW name and pass it here — never overwrite a tool's checkpoint. |
<!-- /generated:parameters -->

## Outputs

- `work/adata_integrated.h5ad` and `tables/integration_batches.csv`.
- Result: `method_used` (the method that actually ran), batch silhouette before and after, and a warning when it did not drop.

## Where it runs

<!-- generated:runs-on -->
As a Slurm CPU job inside `analysis.sif` on HPC3, through the analysis line's job entry point `python -m aiscientist.tools.scrna_cli`, reading and writing the run's `work/` and `artifacts/` on dfs3b. When the session has no HPC3 executor it runs in the gateway process.

Needs: `scanpy`.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- ComBat corrects the expression values themselves, Harmony only the embedding; check `method_used` before comparing results across runs.

## Code and tests

- `tool.py`: `run_integration` and the record. Shared helpers and the declared-parameter table (`PARAMS`, `TOOL_SUMMARY`) are in `../_lib/scrna.py`; the defaults listed above come from that table, and `tests/test_declared_params.py` checks that the preset protocols state the same values.
- Tests: `tests/test_scrna_advanced.py`.

## What the model is told

<!-- generated:model-description -->
> Correct sample/donor/batch effects before clustering. REQUIRED whenever the object holds more
> than one sample: without it the cells cluster by donor and every cell-type label downstream is
> really a donor label, with no visible symptom. `batch_key` names the obs column. Uses Harmony
> when harmonypy is installed and falls back to ComBat (bundled with scanpy); the method that
> ACTUALLY ran is returned as `method_used`. Writes adata_integrated.h5ad and reports batch
> silhouette before/after — it must DROP, and a warning is returned if it does not.
<!-- /generated:model-description -->
