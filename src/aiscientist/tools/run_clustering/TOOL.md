---
name: run_clustering
summary: PCA, a neighbourhood graph, Leiden clustering and a UMAP layout; can choose the resolution by bootstrap stability.
category: analysis
runs_on: hpc:analysis
order: 210
line: scrna
owner: analysis
---
# run_clustering

Groups cells into clusters for data that carries no cell-type labels: PCA on the highly variable genes, a nearest-neighbour graph, Leiden clustering and a UMAP layout for display. With `select_resolution` it re-clusters resampled subsets at each candidate resolution and keeps the most stable one (adjusted Rand index) instead of accepting the default 1.0.

## When the agent uses it

- After `run_scanpy_qc` (and `run_doublet_detection` / `run_integration` when used), only when the data has no labels to reuse.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `resolution` | number | `1.0` | Leiden granularity: higher splits the cells into more, smaller clusters |
| `n_pcs` | integer | `30` | principal components fed into the neighbourhood graph |
| `n_neighbors` | integer | `15` | neighbours per cell in that graph — larger gives smoother, coarser structure |
| `select_resolution` | boolean | `False` | choose the resolution by bootstrap stability instead of accepting the default |
| `n_bootstrap` | integer | `10` | resampling rounds per candidate resolution when selecting one |
| `subsample_frac` | number | `0.8` | fraction of cells per bootstrap round |
| `stability_min` | number | `0.9` | minimum adjusted Rand index a resolution must clear to be called stable |
| `max_sweep_cells` | integer | `20000` | cap on the cells used for the stability sweep |
| `resolution_candidates` | array of number | — | resolutions to try when select_resolution is on |
| `input` | string | `''` | an .h5ad to read INSTEAD of the checkpoint the previous tool wrote — e.g. one a run_code step saved in AISCIENTIST_WORK. A file name or a path inside this run's work/ or artifacts/ directory; nothing outside the run is read. Empty = the usual checkpoint. To feed a variant of an upstream result to the next tool, save it under a NEW name and pass it here — never overwrite a tool's checkpoint. |
<!-- /generated:parameters -->

## Outputs

- `work/adata_clustered.h5ad` and `artifacts/figures/umap_clusters.png`; `tables/resolution_sweep.csv` when a resolution was selected.
- Result: `n_clusters`, cluster sizes, `resolution_source` (`default`, `explicit` or `bootstrap_stability`), and warnings, for example when the data already has a cell-type column or the partition collapsed to one cluster.

## Where it runs

<!-- generated:runs-on -->
As a Slurm CPU job inside `analysis.sif` on HPC3, through the analysis line's job entry point `python -m aiscientist.tools.scrna_cli`, reading and writing the run's `work/` and `artifacts/` on dfs3b. When the session has no HPC3 executor it runs in the gateway process.

Needs: `scanpy`.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- Cluster numbers are not cell types. Naming them is `run_marker_annotation` or `scgpt_annotate`.
- It uses scanpy's `leidenalg` backend; scanpy warns that igraph will become the default.

## Code and tests

- `tool.py`: `run_clustering`, `_select_resolution` and the record. Shared helpers and the declared-parameter table (`PARAMS`, `TOOL_SUMMARY`) are in `../_lib/scrna.py`; the defaults listed above come from that table, and `tests/test_declared_params.py` checks that the preset protocols state the same values.
- Tests: `tests/test_scrna_pack.py`, `tests/test_resolution_selection.py`, `tests/test_tool_self_diagnosis.py`.

## What the model is told

<!-- generated:model-description -->
> PCA → neighbors → Leiden clustering → UMAP on the QC'd data. Writes a UMAP figure and returns
> cluster count + sizes. Run AFTER run_scanpy_qc. Set `select_resolution: true` to CHOOSE the
> Leiden resolution by bootstrap stability instead of accepting the 1.0 default: each candidate
> resolution is re-clustered over resampled subsets and scored by adjusted Rand index against the
> full-data partition, and the FINEST resolution still clearing `stability_min` wins. Prefer this
> whenever cell-type labels will be assigned from the clusters — every label inherits the
> partition, so an unexamined resolution is an unexamined assumption in every label. It costs
> n_bootstrap × len(resolution_candidates) re-clusterings, so it is opt-in; the sweep itself is
> capped at `max_sweep_cells`.
<!-- /generated:model-description -->
