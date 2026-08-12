---
name: condition_by_celltype
description: Reference template — condition-vs-control differential expression, STRATIFIED BY CELL TYPE.
---

## When to use

The pattern behind a KO-vs-WT (or disease-vs-control) report: for each cell type, compare the
condition group against the reference group and collect the changed genes, then look for a shared
cross-cell-type signature. The curated `run_de` tool does per-cluster one-vs-rest DE; this template
covers the stratified condition-vs-reference comparison via scanpy `rank_genes_groups` with an
explicit `reference`, looped over an existing cell-type label column.

## Details & adaptation

ADAPT the five CONFIG values to columns/levels that exist in adata.obs (the DATASET PROFILE in your
planning brief lists them). Everything else is generic. Writes per-cell-type DE tables, a summary
table, shared up/down gene lists, and a volcano per cell type; prints a JSON summary for the report.

## Run
Fetch the template with `read_skill_reference("condition_by_celltype", file="reference.py")`, adapt the CONFIG / marker / threshold values to THIS dataset, then execute it via `run_code` (reads checkpoints from `BIOAGENT_WORK`, writes under `BIOAGENT_ARTIFACTS`). If a purpose-built tool already covers the step, use the tool instead.
