---
name: perturbation_de_vs_control
description: Reference template — per-perturbation differential expression vs a shared non-targeting control.
---

## When to use

The core of a Perturb-seq report: for EACH perturbation, compare its cells against the ONE
non-targeting control group and collect the changed genes. Unlike a 2-group condition study, here
many perturbations share a single reference. The curated `run_de` tool does per-cluster one-vs-rest
DE; this template covers the perturbation-vs-shared-control comparison via scanpy `rank_genes_groups`
with an explicit `reference`, looped over the perturbation label column.

## Details & adaptation

ADAPT the CONFIG values to columns/levels that exist in adata.obs (the DATASET PROFILE in your
planning brief lists them). Set ONLY_PERTURBATIONS to the strong hits from the E-distance step to
skip silent guides. Everything else is generic. Writes a per-perturbation DE table + a summary table
(with a target-self-knockdown positive-control check) and prints a JSON summary for the report.

## Run
Fetch the template with `read_skill_reference("perturbation_de_vs_control", file="reference.py")`, adapt the CONFIG / marker / threshold values to THIS dataset, then execute it via `run_code` (reads checkpoints from `BIOAGENT_WORK`, writes under `BIOAGENT_ARTIFACTS`). If a purpose-built tool already covers the step, use the tool instead.
