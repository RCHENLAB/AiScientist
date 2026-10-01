---
name: run_marker_annotation
summary: Names clusters from a curated marker panel, with optional lineage-specific discriminators.
category: analysis
runs_on: hpc:analysis
order: 300
line: scrna
owner: analysis
---
# run_marker_annotation

Scores each cluster against a marker panel (`{cell type: [gene symbols]}`), optionally sharpened by a few lineage-specific `discriminators`, and assigns the best-scoring cell type. A cluster without a clear winner stays `Unassigned` instead of being pushed into the nearest label.

## When the agent uses it

- After `run_clustering`, when the cell types are known in advance and a marker panel exists for them.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `panel` | object | — |  |
| `discriminators` | object | — |  |
| `cluster_key` | string | — |  |
| `min_discriminator_mean` | number | — |  |
| `dominance_ratio` | number | — |  |
| `input` | string | `''` | an .h5ad to read INSTEAD of the checkpoint the previous tool wrote — e.g. one a run_code step saved in AISCIENTIST_WORK. A file name or a path inside this run's work/ or artifacts/ directory; nothing outside the run is read. Empty = the usual checkpoint. To feed a variant of an upstream result to the next tool, save it under a NEW name and pass it here — never overwrite a tool's checkpoint. |
<!-- /generated:parameters -->

## Outputs

- `work/adata_annotated.h5ad`.
- `tables/cluster_cell_types.csv` and `.json`, `tables/celltype_scores_by_cluster.csv`, `tables/celltype_composition.csv`.

## Where it runs

<!-- generated:runs-on -->
As a Slurm CPU job inside `analysis.sif` on HPC3, through the analysis line's job entry point `python -m aiscientist.tools.scrna_cli`, reading and writing the run's `work/` and `artifacts/` on dfs3b. When the session has no HPC3 executor it runs in the gateway process.

Needs: `scanpy`.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- Only as good as the panel: a cell type that is missing from it cannot be found.
- Report `Unassigned` clusters as such; do not rename them by hand to complete the picture.

## Code and tests

- `tool.py`: `run_marker_annotation` and the record. Shared helpers and the declared-parameter table (`PARAMS`, `TOOL_SUMMARY`) are in `../_lib/scrna.py`; the defaults listed above come from that table, and `tests/test_declared_params.py` checks that the preset protocols state the same values.
- Tests: `tests/test_scrna_advanced.py`.

## What the model is told

<!-- generated:model-description -->
> Assign a cell type to each cluster from a curated marker `panel` ({cell type: [symbols]}), with
> optional `discriminators` ({cell type: [2-4 lineage-SPECIFIC symbols]}). Signature scores give a
> first-pass z-argmax; the final label comes from RAW marker expression and is assigned only when
> that lineage's discriminators dominate, so shared markers (LAMP3 across AT2 and DC, SLC1A3
> across Muller glia and astrocyte) cannot silently mislabel a cluster. Clusters with no dominant
> signal stay 'Unassigned'. `panel` is required and must match the tissue — see the
> annotate_clusters_by_markers_v2 skill for how to build it. Returns which clusters the raw check
> CORRECTED and which are unassigned; both belong in the report.
<!-- /generated:model-description -->
