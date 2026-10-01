---
name: run_composition
summary: Cell-type proportions per sample and how they shift between conditions.
category: analysis
runs_on: hpc:analysis
order: 290
line: scrna
owner: analysis
---
# run_composition

Counts what fraction of each sample's cells belongs to each cell type and compares those proportions between conditions: whether a population expanded or shrank, as opposed to whether its genes changed.

## When the agent uses it

- In a condition study, usually as the first analysis after QC on labelled data.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `group_key` | string | — |  |
| `sample_key` | string | — |  |
| `condition_key` | string | — |  |
| `input` | string | `''` | an .h5ad to read INSTEAD of the checkpoint the previous tool wrote — e.g. one a run_code step saved in AISCIENTIST_WORK. A file name or a path inside this run's work/ or artifacts/ directory; nothing outside the run is read. Empty = the usual checkpoint. To feed a variant of an upstream result to the next tool, save it under a NEW name and pass it here — never overwrite a tool's checkpoint. |
<!-- /generated:parameters -->

## Outputs

- `tables/composition.csv`, `tables/composition_by_sample.csv`, `tables/composition_by_condition.csv` and `tables/composition_test.csv`.

## Where it runs

<!-- generated:runs-on -->
As a Slurm CPU job inside `analysis.sif` on HPC3, through the analysis line's job entry point `python -m aiscientist.tools.scrna_cli`, reading and writing the run's `work/` and `artifacts/` on dfs3b. When the session has no HPC3 executor it runs in the gateway process.

Needs: `scanpy`.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- Proportions are compositional: one population growing shrinks every other share, so a single shift can look like several.
- The test needs replicate samples per arm; with one sample per arm it can describe, not test.

## Code and tests

- `tool.py`: `run_composition` and the record. Shared helpers and the declared-parameter table (`PARAMS`, `TOOL_SUMMARY`) are in `../_lib/scrna.py`; the defaults listed above come from that table, and `tests/test_declared_params.py` checks that the preset protocols state the same values.
- Tests: `tests/test_scrna_advanced.py`, `tests/test_pseudoreplication_guard.py`.

## What the model is told

<!-- generated:model-description -->
> Cell-type proportions per sample and how they shift between conditions — 'which populations
> expand or shrink'. With `sample_key` + `condition_key` it tests on centered-log-ratio values
> (proportions sum to 1, so testing them raw makes every population look coupled) and needs >=2
> samples per arm, otherwise it reports proportions without a test. Returns the compositional
> caveat with the result.
<!-- /generated:model-description -->
