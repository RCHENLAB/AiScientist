---
name: score_signature
description: Reference template — score every cell for one or more gene signatures with scanpy `sc.tl.score_genes` (signature mean minus a matched random control set), then summarise the score per group (e.g. cell type x condition).
---

## When to use

No curated tool covers per-cell signature scoring. This template uses scanpy `sc.tl.score_genes`
and summarises each score per group (any number of obs columns, e.g. `majorclass` x `sampleid`).
ADAPT `SIGNATURES` (one coherent programme per entry; symbols in any case) and `GROUP_KEYS`.

What it guards: the gene lookup uses the matrix that is actually scored (`.raw`, all genes,
log-normalised — not the HVG-only X of adata_clustered.h5ad); symbols match case-insensitively
(GFAP finds Gfap) and every remap is reported, but a different gene NAME is not fixed and stays
listed as missing; a signature below `MIN_COVERAGE` (default 50% of its genes found) is not scored;
integer counts are refused. Keep genes expected to move in opposite directions in separate
signatures — averaged together they cancel.

When NOT to use: to ask whether a pathway shifts between conditions within a cell type, preranked
GSEA (`run_gsea_prerank`) over the contrast's full ranking already tests the curated gene sets —
spliceosome and mRNA-splicing sets included. Use this template for a programme no library holds,
or when a per-cell score is the deliverable.

## Run
Fetch the template with `read_skill_reference("score_signature", file="reference.py")`, adapt the CONFIG / marker / threshold values to THIS dataset, then execute it via `run_code` (reads checkpoints from `AISCIENTIST_WORK`, writes under `AISCIENTIST_ARTIFACTS`). If a purpose-built tool already covers the step, use the tool instead.
