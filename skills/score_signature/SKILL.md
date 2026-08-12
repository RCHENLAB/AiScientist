---
name: score_signature
description: Reference template — score every cell for a gene signature and compare across groups.
---

## When to use

No curated tool covers per-cell signature scoring. This template uses scanpy `sc.tl.score_genes`
and summarizes the score per cluster/condition. ADAPT SIGNATURE (gene SYMBOLS) and GROUP_KEY.

## Run
Fetch the template with `read_skill_reference("score_signature", file="reference.py")`, adapt the CONFIG / marker / threshold values to THIS dataset, then execute it via `run_code` (reads checkpoints from `BIOAGENT_WORK`, writes under `BIOAGENT_ARTIFACTS`). If a purpose-built tool already covers the step, use the tool instead.
