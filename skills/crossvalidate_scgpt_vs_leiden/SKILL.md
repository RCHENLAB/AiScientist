---
name: crossvalidate_scgpt_vs_leiden
description: Reference template — cross-validate scGPT per-cell labels against the data's own structure.
---

## When to use

No curated tool covers this. scGPT labels EVERY cell of the RAW query; QC/Leiden run on a filtered
subset, so the two cell sets differ — ALIGN BY BARCODE, never by row position. A positional merge (or
assigning a length-N prediction column onto an M-cell adata) raises a pandas alignment error and the
whole step fails — the single most common way this skill's runs stall. For EACH reference column
available — the independent Leiden clusters AND any existing majorclass/celltype labels the data
already carries — this writes a confusion table (reference x scGPT-label) + a per-group
agreement/purity summary, plus the scGPT confidence distribution. ADAPT column names if your CSV differs.

## Run
Fetch the template with `read_skill_reference("crossvalidate_scgpt_vs_leiden", file="reference.py")`, adapt the CONFIG / marker / threshold values to THIS dataset, then execute it via `run_code` (reads checkpoints from `BIOAGENT_WORK`, writes under `BIOAGENT_ARTIFACTS`). If a purpose-built tool already covers the step, use the tool instead.
