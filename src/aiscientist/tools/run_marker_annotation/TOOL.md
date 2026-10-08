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

Scores each cluster against a marker panel (`{cell type: [gene symbols]}`), sharpened by a few lineage-specific `discriminators`, and assigns the best-scoring cell type. A cluster without a clear winner stays `Unassigned` instead of being pushed into the nearest label.

**The markers come from a curated reference when one exists for the tissue** (`references/<tissue>.json`; today `retina`, human and mouse symbols). Pass `reference: "retina"` and no panel. With the default `reference: "auto"`, a model-written panel whose cell types match a reference (three or more) is replaced by the reference's definitions, and any gene the model put under one lineage that is another lineage's specific marker is dropped with a warning. This exists because a model-written retina panel (2026-10-02) put OPN4/NEFM/MEF2C under cones, RLBP1 under bipolar cells and astrocyte markers under Muller glia: 1,631 Muller glia were labelled "Unassigned" or "ganglion" and the Critic accepted it.

Two checks run after the labels are assigned:

- **Canonical markers.** A label stands only if one of its lineage-specific markers is enriched in the cluster (mean log-normalised expression at least 0.5 above clusters given other labels, detected in at least 20% of the cluster's cells). Otherwise the label is withdrawn (`withdrawn_label`) and the cluster is `Unassigned`.
- **Plausibility.** A lineage every sample of the tissue contains (retina: Muller glia) that no cluster received is a warning, with how often its markers are detected; no photoreceptor, bipolar or amacrine cluster at all is another.

## When the agent uses it

- After `run_clustering` (and ideally `run_de`), to name the clusters. For retina, pass `reference: "retina"`.

## When the agent does NOT use it

- To discover cell types nobody expects: a reference only finds the lineages it lists. Report unassigned clusters as such.
- With a hand-written panel for a tissue that has a reference: the reference replaces it anyway.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `reference` | string (`auto`, `none`, `retina`) | — | curated marker reference for the tissue; 'auto' (default) applies one when your panel's cell types match it |
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
> Assign a cell type to each cluster from marker genes. For a tissue with a curated reference
> (retina) pass `reference` and NO panel: the curated markers are used, in the object's species.
> Otherwise give a `panel` ({cell type: [symbols]}) with `discriminators` ({cell type: [2-4
> lineage-SPECIFIC symbols]}); a panel whose types match a reference uses the reference
> automatically. Signature scores give a first-pass z-argmax; the final label comes from RAW
> marker expression, assigned only when that lineage's discriminators dominate AND at least one of
> them is enriched in the cluster versus other lineages; otherwise 'Unassigned'. Returns which
> clusters the raw check CORRECTED, which are unassigned or WITHDRAWN, and warnings (e.g. a
> lineage every sample of the tissue contains is missing); all belong in the report.
<!-- /generated:model-description -->
