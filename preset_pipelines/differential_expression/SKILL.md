---
name: differential_expression
version: 2
description: Compare an experimental condition vs control (e.g. KO vs WT) per cell type, with pathway interpretation (v2 — pseudobulk when replicates exist, and honest about it when they don't)
tools: run_scanpy_qc, run_clustering, run_de, run_pseudobulk_de, run_composition, run_enrichment, run_gsea_prerank, run_code
data_type: scrna
---

## Read this before planning step 3

A condition contrast is a statement about the CONDITION, so the unit of replication is the
SAMPLE (donor / animal / library), never the cell. Cells from one donor are not independent
observations of that donor's condition. A Wilcoxon test over cells treats them as if they
were, and the p-values are then anti-conservative by orders of magnitude.

This is measured, not theoretical. On a synthetic 4-donor design where **exactly 1 of 400
genes** was made different between the arms:

| test | called significant (padj<0.05) |
|---|---|
| `run_de`, Wilcoxon over cells | **310 of 400 (78%)** |
| `run_pseudobulk_de`, over donors | 0 (and it ranked the true gene #1) |

So: **use `run_pseudobulk_de` for a condition contrast whenever the data has ≥2 samples per
arm.** `run_de` is for markers — one cluster versus the rest within a sample — which is what
it is valid for.

Condition-comparison protocol. Use when the goal is to find the genes that change between two
experimental GROUPS of cells — disease vs control, knockout vs wild-type, treated vs untreated —
and interpret them. NOT for assigning cell-type labels (use `celltype_annotation` for that).

## Figuring out WHAT to compare from the data (do this even with a vague/empty question)

The user may not know the biology, or may just say "analyze this dataset". You do NOT need them to
spell it out — the dataset's own metadata defines the experiment. Read the DATASET PROFILE (the
obs columns + their category values are given to you at planning time) and infer:

1. **The condition/group column.** Look in obs for a low-cardinality (usually 2-level) column that
   names an experimental variable: `sampleid`, `condition`, `genotype`, `treatment`, `group`,
   `orig.ident`, `disease`, etc. Its two values ARE the comparison (e.g. `sampleid=[DDX41, WT]`
   → compare DDX41 vs WT). The dataset / file name often confirms intent (e.g. `*_DEG` = they
   want differential expression).
2. **The reference (control) group.** Pick the level whose name looks like the baseline —
   `WT`, `wild-type`, `control`, `ctrl`, `untreated`, `vehicle`, `DMSO`, `normal`, `sham`. The
   OTHER level is the condition of interest. State which you chose; if it is genuinely unclear,
   ask ONE clarify question in plan mode rather than guessing.
3. **The cell-type stratification.** If obs already has a cell-type / cluster label column
   (`majorclass`, `celltype`, a predicted-label column), REUSE it — run the comparison
   SEPARATELY WITHIN EACH cell type. Do NOT re-cluster or re-annotate from scratch. Only if no
   label column exists do you `run_clustering` first and compare across clusters.

So the default study for a dataset with a 2-group condition column + existing cell-type labels is:
**"condition vs control differential expression within each major cell type, then pathway
enrichment on the changed genes"** — plan that without waiting for the user to describe it.

## Ordered plan

1. **QC** (`run_scanpy_qc`): per-cell metrics, filter, normalize + log1p (+ HVG). Report counts.
   Honor any QC columns already in the data (e.g. `percent.mt`, doublet calls).
2. **Define the comparison** from the profile (above): name the condition column, the two groups,
   the reference group, and the cell-type column you will stratify by. If labels already exist,
   skip clustering.
3. **Per-cell-type differential expression — choose the test from the DESIGN, not from habit.**
   First find the SAMPLE column (donor / animal / library / `orig.ident`), which is usually a
   different column from the condition. Then:

   - **≥2 samples per arm → `run_pseudobulk_de`** with `sample_key`, `condition_key`, and
     `group_key` = the cell-type column. It aggregates counts per sample, tests across
     samples, and returns `skipped_groups` for any cell type without enough samples. Report
     those skips — "we could not test this cell type" is a finding about the study.
   - **1 sample per arm (common in a two-library pilot) → there is no replication and no valid
     p-value for the condition.** Do NOT quietly run the cell-level test and report its
     p-values as if they meant something. Either state plainly that the comparison is
     DESCRIPTIVE — effect sizes and ranked genes only, no inferential claim, because nothing
     in the data separates the condition from the individual — or, if a cell-level ranking is
     still wanted, run it and label it as exploratory ranking, not differential expression.
   - Skip / flag any cell type with too few cells in either group.

   **Memory** (only relevant if you fall back to `run_code`): load the AnnData ONCE, and inside
   a per-cell-type loop subset with a **view** (`adata[mask]`) — do NOT `adata[mask].copy()`
   every cell type. On the local sandbox an over-budget loop is OOM-killed (`returncode == -9`);
   prefer `BIOAGENT_RUN_CODE_ON_HPC=1` for a real `--mem` cap on large datasets.

3b. **Composition** (`run_composition`): whether cell-type PROPORTIONS shift between the arms is
   a different question from which genes change, and is often the more visible effect. Same
   replication rule — it needs ≥2 samples per arm to test, and reports proportions without a
   test otherwise.
4. **Pathway interpretation per cell type**: `run_enrichment` (ORA on the changed genes, tested
   against the real universe) and `run_gsea_prerank` (over the whole ranking, signed NES). They
   use different inputs and different nulls — do NOT require them to agree, and keep null
   results. Send gene SYMBOLS only.
5. **Cross-cell-type synthesis.** Find genes changed in the SAME direction across ≥2 cell types
   (a shared / pan-tissue signature) vs cell-type-specific changes.
6. **Figures / tables** (via `run_code`, adapting the template): a per-cell-type DEG-count summary
   table; a volcano plot per cell type; shared up- and down-regulated gene heatmaps; an enrichment
   bar plot per cell type. The final report is assembled automatically — do NOT plan a
   report-writing step.

## Grounding

Report effect sizes (log fold-change) and ADJUSTED p-values, not just gene names. Ground every
claim in the DE / enrichment statistics the tools returned — never fabricate genes, fold-changes,
or pathways. State the reference group and any cell types skipped for low cell count. Frame biology
as hypotheses to validate, not established fact.

State the **unit of replication and how many there were** — "n = 3 donors per arm", not "n =
4,812 cells". A reader cannot judge a condition contrast without it, and it is the single number
that distinguishes a real result from a pseudoreplicated one.
