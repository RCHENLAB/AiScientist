---
name: annotate_clusters_by_markers
description: Reference template — assign a cell-type label to each Leiden cluster from its markers.
---

## When to use

The curated tools cover QC -> clustering -> DE, but NOT the final label assignment. This
CodeAct template fills that gap: it reads the DE checkpoint (which carries
``rank_genes_groups``), scores each cluster against a small canonical marker dictionary, and
writes a cluster -> cell-type table. ADAPT the marker dictionary to YOUR tissue before running;
the labels are only as good as the dictionary, so state uncertainty honestly in the write-up.

## Run
Fetch the template with `read_skill_reference("annotate_clusters_by_markers", file="reference.py")`, adapt the CONFIG / marker / threshold values to THIS dataset, then execute it via `run_code` (reads checkpoints from `BIOAGENT_WORK`, writes under `BIOAGENT_ARTIFACTS`). If a purpose-built tool already covers the step, use the tool instead.
