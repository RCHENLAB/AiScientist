---
name: mixscape_escape_filter
description: Reference template — (OPTIONAL) label & drop 'escaping' cells with Mixscape before DE.
---

## When to use

In a real screen a fraction of guide-assigned cells ESCAPE knockdown and look transcriptionally like
control; keeping them dilutes every downstream comparison. Mixscape (Papalexi et al. 2021, via pertpy)
computes a local perturbation signature and classifies each cell as perturbed (KO/KD), non-perturbed
(NP = escaped), or control (NT) — so the NP cells can be removed before `perturbation_de_vs_control.py`.

## Details & adaptation

pertpy is a HEAVY OPTIONAL dependency and its Mixscape API drifts across versions. This template
degrades gracefully: if pertpy is not importable it writes a note and exits 0 (the skill then runs DE
on all guide-assigned cells — a conservative, effect-diluting choice, which you must state). When it
runs, it writes `adata_mixscape.h5ad` (NP cells removed) to BIOAGENT_WORK for the DE step to read, and
reports the per-perturbation NP fraction. ADAPT the CONFIG and the API call to your installed pertpy.

## Run
Fetch the template with `read_skill_reference("mixscape_escape_filter", file="reference.py")`, adapt the CONFIG / marker / threshold values to THIS dataset, then execute it via `run_code` (reads checkpoints from `BIOAGENT_WORK`, writes under `BIOAGENT_ARTIFACTS`). If a purpose-built tool already covers the step, use the tool instead.
