# Perturbation-analysis methods (reference)

Method notes for the `perturbation_analysis` skill's `run_code` templates. Adapted from the
**k-dense-ai/scientific-agent-skills** perturbation references and the scPerturb / pertpy literature.
The scanpy steps use only what the analysis image already ships (scanpy/numpy/pandas); pertpy is a
heavy **optional** dep (Mixscape only).

## Perturbation labels & the control

Each cell carries a guide/perturbation label in `obs` (`perturbation`, `guide`, `sgRNA`, `target`,
`gene_target`, `knockout`, `feature_call`, …). One level is the **non-targeting control** (`NT`,
`NTC`, `non-targeting`, `control`, `sgLacZ`, `sgGFP`, `sgScramble`, …) and every perturbation is
compared against it. Guide-level labels (`TP53_sg1`, `TP53_sg2`) can be collapsed to the **target
gene** for power when concordant — set `COLLAPSE_GUIDE_TO_TARGET`. If guide identity is not in `obs`
(guide counts in a layer/`obsm`), cells must be **assigned to a guide first** (max-count
demultiplexing) — that is a separate pre-step, not part of these templates.

## E-distance (`perturbation_edistance.py`) — which perturbations are real

scPerturb **E-distance** (Peidli et al., *Nat. Methods* 2024): energy distance with squared-Euclidean
cost between a perturbation P and control C in PCA space —

```
E(P,C) = 2·δ_PC − δ_PP − δ_CC ,   δ_XY = mean over pairs of ‖x − y‖²
```

Computed here in closed form from group means/variances (δ_XX = 2·Var(X);
δ_PC = ‖μ_P − μ_C‖² + Var(P) + Var(C)), so it is **exact and O(n·d)** — no n×n pairwise matrix, scales
to large screens. Significance is a **label-permutation test**: pool P+C, reshuffle the two labels
`N_PERM` times, count how often the permuted E-distance ≥ observed (Davison–Hinkley `+1`), then
Benjamini–Hochberg across perturbations. Perturbations not significant at the chosen FDR are
**silent** — reported as a result, not run through DE. (Note: with squared-Euclidean cost the
statistic is centroid-shift-based; that is the scPerturb definition and what pertpy's `Distance
metric="edistance"` reports. A distribution-shape-sensitive variant needs non-squared Euclidean +
subsampled pairwise distances.)

## Per-perturbation DE (`perturbation_de_vs_control.py`)

scanpy `rank_genes_groups(..., groups=[pert], reference=CONTROL, method="wilcoxon")` looped over
perturbations against the shared control — the shared-reference comparison `run_de` (per-cluster
one-vs-rest) does not do. **Positive control:** a bona-fide knockout shows its own target gene *down*
in its DE; the template checks this per perturbation (`target_self_knockdown`) and flags guides that
fail it (possible mislabel / no editing). Report adjusted p-values + log fold-changes.

## Mixscape (`mixscape_escape_filter.py`, optional) — remove escaping cells

Mixscape (Papalexi et al., *Nat. Genetics* 2021; `pertpy` `pt.tl.Mixscape`): a local perturbation
signature (cell minus nearest control neighbours) + per-perturbation mixture model classifies each
cell as perturbed (KO/KD), **non-perturbed (NP = escaped)**, or NT. Dropping NP cells before DE
removes the escaped-cell dilution. `pertpy` is optional and its API drifts across versions — the
template degrades to "skip + run DE on all cells" if pertpy is unavailable; the stable output key is
`obs["mixscape_class_global"]`.
