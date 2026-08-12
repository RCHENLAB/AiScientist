---
name: perturbation_analysis
description: Analyze a pooled CRISPR / Perturb-seq screen — rank which perturbations change the transcriptome, then per-perturbation DE vs the non-targeting control
tools: run_scanpy_qc, run_clustering, run_de, run_enrichment, run_code, literature_search
data_type: scrna
---

Pooled-perturbation protocol (**Perturb-seq / CROP-seq / CRISPRi-a screens**). Use when each cell
carries a CRISPR **guide / perturbation label** and the goal is to learn WHICH perturbations
reshaped the transcriptome and HOW — not to assign cell types (`celltype_annotation`) and not a
simple two-group condition comparison (`differential_expression`). The defining feature: **many
perturbation groups share ONE non-targeting control**, and a large fraction of guides have **no
detectable effect** — so the first analytical question is "which perturbations actually did
something?", not "run DE on all of them".

## Figuring out the perturbation design from the data (do this even with a vague/empty question)

The dataset's own metadata defines the screen. Read the DATASET PROFILE (obs columns + their
category values are given at planning time) and infer:

1. **The perturbation column.** A (usually high-cardinality) obs column naming the guide/target per
   cell: `perturbation`, `guide`, `gRNA`/`sgRNA`, `target`/`target_gene`/`gene_target`, `knockout`,
   `condition`, `KO`, `feature_call`. Its many levels are the perturbation groups. (High cardinality
   here — dozens/hundreds of guides — is the tell that separates a screen from a 2-group condition
   dataset.)
2. **The non-targeting control level.** Pick the level that names a control guide: `NT`,
   `non-targeting`, `control`, `ctrl`, `NTC`, `sgLacZ`, `sgGFP`, `sgScramble`, `scramble`,
   `unperturbed`, `none`. Every perturbation is compared **against this shared control**. State which
   level you chose; if genuinely ambiguous, ask ONE clarify question in plan mode rather than guessing.
3. **Guides vs targets.** If the column is per-**guide** (multiple sgRNAs per gene, e.g.
   `TP53_sg1`, `TP53_sg2`), you can collapse to the **target gene** for power (concordant sgRNAs of
   the same gene are biological replicates) — do that when guide-level groups are small. Say whether
   you analyzed at guide or target level.
4. **No perturbation column?** If guide identity is NOT in obs (e.g. it lives in a separate guide-count
   layer / `obsm` / a CITE-seq-style feature matrix), the cells must be **assigned to a guide first**
   (max-count / demultiplexing). Flag this as a required pre-step (`run_code`) and do not fabricate a
   `perturbation` column.

## Ordered plan

1. **QC** (`run_scanpy_qc`): per-cell metrics, filter, normalize + log1p (+ HVG). Report counts.
   Honor QC columns already in the data. Do NOT drop the control cells.
2. **Embedding** (`run_clustering`): compute PCA/neighbors/UMAP. The PCA embedding is what the
   effect-size ranking (step 4) measures distances in; a UMAP colored by perturbation is a useful
   overview figure. (Leiden clusters themselves are secondary here — the grouping of interest is the
   perturbation label, not de-novo clusters.)
3. **Define the screen** from the profile (above): name the perturbation column, the control level,
   and whether you work at guide or target level. Count cells per perturbation and **flag the
   perturbations with too few cells** (default < 30) — they give unstable DE and E-distance.
4. **Rank perturbation strength — which guides actually did something** (`run_code`, adapt
   `perturbation_edistance.py`). Compute the **E-distance** (scPerturb energy distance, in PCA space)
   of each perturbation to the non-targeting control, with a **permutation test** for significance.
   Rank perturbations by effect size; the ones that are NOT significantly different from control are
   **silent** (guide may not have knocked down, or the gene is dispensable in this context) — report
   them as silent, don't run downstream biology on them. This focuses the rest of the analysis on the
   perturbations with a real transcriptional phenotype.
5. **(Optional) Remove escaping cells** (`run_code`, adapt `mixscape_escape_filter.py`). Within a
   perturbation, some cells escape knockdown and look like control (dilutes DE). Mixscape (pertpy)
   labels perturbed vs non-perturbed (NP) cells so NP cells can be dropped before DE. **pertpy is a
   heavy optional dependency** — if unavailable, skip this step and say the DE was run on all
   guide-assigned cells (a conservative, effect-diluting choice), don't block on it.
6. **Per-perturbation differential expression vs the control** (`run_code`, adapt
   `perturbation_de_vs_control.py`). For each perturbation **with a real effect and enough cells**,
   run perturbation-vs-non-targeting-control DE (scanpy `rank_genes_groups` with an explicit
   `reference` = the control level, looped over perturbations). `run_de` is per-cluster one-vs-rest,
   so it does NOT do this shared-reference comparison — use the template. **Sanity check:** a bona-fide
   knockout should show its OWN target gene **down** in its DE — report that self-knockdown as a
   positive control, and flag guides where the target does NOT drop.
   **Memory:** load the AnnData ONCE; inside the per-perturbation loop subset with a **view**
   (`adata[mask]`), never `.copy()` every group or hold all subsets at once — an over-budget loop is
   OOM-killed on the local sandbox (`returncode == -9`). Prefer `BIOAGENT_RUN_CODE_ON_HPC=1`
   (see `skills/README.md`) for a real `--mem` cap on large screens.
7. **(Optional) Pathway enrichment per strong perturbation** (`run_enrichment`): on the up-/down-gene
   sets of each perturbation with a substantial DE result. Send gene SYMBOLS only.
8. **(Optional) Group perturbations by transcriptional effect.** Perturbations with correlated DE
   signatures (or small pairwise E-distance) likely act in a **shared pathway / module** — the
   `perturbation_edistance.py` template can also emit a pairwise perturbation×perturbation E-distance
   matrix for this. Frame shared modules as hypotheses.
9. **(Optional) Literature context** (`literature_search`): for the strongest hits, pull real
   citations linking the perturbed gene to the observed program. Cite only what the tool returns.

The final methods + results report is assembled automatically — do NOT plan a report-writing step.

## Grounding

Report effect sizes (E-distance, log fold-change) and ADJUSTED p-values / permutation p-values, never
just gene names. State the control level, whether you worked at guide or target level, and every
perturbation skipped for low cell count. **Silent perturbations are a result, not a failure** —
report which guides had no detectable effect rather than forcing DE on them. Use the target-gene
self-knockdown as a positive control and flag guides that fail it (possible mislabel / no editing).
Never fabricate a gene, fold-change, pathway, or perturbation. Frame biology as hypotheses to
validate, not established fact.
