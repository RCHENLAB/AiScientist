---
name: condition_by_celltype
description: Reference template — condition-vs-control differential expression, STRATIFIED BY CELL TYPE.
---

## When to use

**Use the tool first.** `run_de` performs this comparison directly:

```
run_de(groupby="<condition column>", reference="<control level>", stratify_by="<cell-type column>")
```

That runs condition-vs-reference within each cell type, writes the tables `run_enrichment` and
`run_gsea_prerank` discover, reports up/down counts per cell type, and lists the cell types it had
to skip for too few cells. It needs only `run_scanpy_qc` first — do **not** re-cluster a dataset
that already carries labels.

Reach for **this template only when the tool cannot express the design**: a paired or
covariate-adjusted comparison, a custom shared-signature rule, or a figure the tool does not draw.
It is the same `rank_genes_groups(reference=...)` loop, written out so you can modify it.

## Details & adaptation

ADAPT the CONFIG values to columns/levels that exist in adata.obs (the DATASET PROFILE in your
planning brief lists them). Everything else is generic. Writes per-cell-type DE tables, a summary
table, shared up/down gene lists, and a volcano per cell type; prints a JSON summary for the report.

It also writes `tables/de_<CELLTYPE_KEY>_all.csv` + `tables/de_<CELLTYPE_KEY>_universe.txt` — the
exact names `run_enrichment` discovers. **Keep them.** Without them, enrichment falls back to a
single pooled gene list (every cell type collapsed into one "input" group) and to a constant
20000-gene ORA background, which inflates every enrichment p-value. After this step, call
`run_enrichment` with **no** `genes` argument so it picks the table up itself.

## Run
Fetch the template with `read_skill_reference("condition_by_celltype", file="reference.py")`, adapt the CONFIG / marker / threshold values to THIS dataset, then execute it via `run_code` (reads checkpoints from `BIOAGENT_WORK`, writes under `BIOAGENT_ARTIFACTS`). If a purpose-built tool already covers the step, use the tool instead.
