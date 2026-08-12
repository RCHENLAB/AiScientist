---
name: perturbation_edistance
description: Reference template — rank perturbations by E-distance to control (which guides actually did something).
---

## When to use

In a pooled screen most guides are SILENT (no editing, or the gene is dispensable here). Before
running DE on everything, rank each perturbation by how far it moved cells away from the non-targeting
control in PCA space, and permutation-test whether that shift is real. Output is the shortlist of
perturbations with a genuine transcriptional phenotype — feed it to ONLY_PERTURBATIONS in
`perturbation_de_vs_control.py`.

## Details & adaptation

The statistic is the scPerturb **E-distance** (energy distance with squared-Euclidean cost, Peidli et
al. 2024): E(P,C) = 2·mean‖p−c‖² − mean‖p−p'‖² − mean‖c−c'‖², computed here in closed form
(O(n·d) memory, no pairwise matrix) so it is exact and scales to large screens. Permutation shuffles
the P-vs-C labels to get a null for "this perturbation is no different from control".

ADAPT the CONFIG. Writes a ranked table + (optional) a pairwise perturbation×perturbation E-distance
matrix (for grouping perturbations that act alike) and prints a JSON summary for the report.

## Run
Fetch the template with `read_skill_reference("perturbation_edistance", file="reference.py")`, adapt the CONFIG / marker / threshold values to THIS dataset, then execute it via `run_code` (reads checkpoints from `BIOAGENT_WORK`, writes under `BIOAGENT_ARTIFACTS`). If a purpose-built tool already covers the step, use the tool instead.
