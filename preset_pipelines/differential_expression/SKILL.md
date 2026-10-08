---
name: differential_expression
version: 2
description: Compare an experimental condition vs control (e.g. KO vs WT) per cell type, with pathway interpretation (v2 — pseudobulk when replicates exist, and honest about it when they don't)
tools: run_scanpy_qc, run_cellqc, run_clustering, run_de, run_pseudobulk_de, run_depth_matched_de, run_composition, run_enrichment, run_gsea_prerank, deep_literature, run_code
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
arm.** `run_de` at its default (`reference="rest"`) is for markers — one cluster versus the rest
within a sample — which is what it is valid for. `run_de` can also run a real cell-level contrast
(`reference=<control level>`, `stratify_by=<cell-type column>`), but that inherits the
pseudoreplication above: use it only when there are no replicates, and label it as an exploratory
ranking rather than differential expression.

## Read this before planning the contrast, too: SEQUENCING DEPTH

Replication is not the only way a condition contrast goes wrong. If one arm was sequenced deeper
per cell, EVERY comparison inherits it: a gene detected in more cells because more molecules were
sampled scores as up-regulated, and whole pathways of abundant transcripts — translation,
ribosome, RNA metabolism — move together in the deeper arm. Log-normalisation does NOT remove
this; it rescales totals, while detection is a function of how many molecules were drawn.

The dataset profile gives you the median counts per cell for each arm. **Compare them before you
plan.** If they differ by more than ~1.3x, the plan MUST contain a depth-matched check, because
with one library per arm there is no replication that could separate depth from biology.

`run_depth_matched_de` is that check as a deterministic tool: within each cell type it down-samples
the deeper arm quantile by quantile until the per-cell UMI distributions match (never up-sampling),
re-runs the SAME contrast, and Spearman-correlates the original ranking against the matched one,
separately per direction. High rho = not an artefact. Rho near zero = depth decided the ranking.
NEGATIVE rho = the ranking inverts once depth is equal, which is the strongest evidence that the
finding must not be reported as biology.

Name that tool. Do NOT plan this as free-form `run_code`: it existed only as a step brief before,
and three different models wrote three different wrong versions — one plan asked the executor to
"correlate per-gene logFC against the between-arm median nCount_RNA", which is not computable (a
per-gene vector against a single scalar), and the step failed on every attempt.

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

0. **Establish what the matrix IS, and the rules, before touching it** (`run_code`) — read-only,
   and BEFORE QC. Two outputs, written as tables the later steps read back:
   * *Provenance.* **Not for a 10x Cell Ranger input whose data profile already rules on it**
     (its `.h5` matrices were read at upload: integer UMI counts, checked against Cell Ranger's
     `metrics_summary.csv`). Cite that ruling; do NOT plan a `run_code` step to re-derive it, and
     write the decision rule below into the plan's first step instead. Otherwise: inspect `X`,
     `.raw`, `layers`, value ranges, sparsity and integer-ness, and check whether matrix-derived
     totals reproduce any stored `nCount`/`nFeature` fields. A
     published object often arrives already normalized and log1p'd: normalizing it again has NO
     symptom — every tool succeeds, every figure renders, and every number after it is wrong. So
     rule from the NUMBERS, and say which way you ruled:
     - **Counts.** Every value non-negative and integer-valued to tolerance, the row sums
       reproduce a stored total-count field (`nCount_RNA`, `total_counts`), the per-row non-zero
       counts reproduce the stored detected-feature field, and no `layers`/`.raw` entry carries a
       different scale. **That is sufficient — proceed.** An empty `uns` and a missing
       transformation history are a LIMITATION TO REPORT, not a stop condition: a matrix that
       reproduces its own stored QC fields exactly is not made unusable by the absence of a
       written record, and stopping there forfeits the whole quantitative analysis over a
       provenance note.
     - **Not counts.** Any negative or non-integer value, row sums that do not reproduce the
       stored totals, or a differently-scaled representation present. Then say so and drop the
       count-dependent steps — never recover counts by exponentiating, rounding, or renaming.
     Count-based tests (`run_pseudobulk_de`, `run_depth_matched_de`) need the first ruling; say
     which one you applied and which representation supports which operation.
   * *The decision rule.* Name the primary estimand, the unit of replication, the minimum cells
     per arm per cell type, and the effect-size gate — and write them down NOW, while no DE
     result is visible. A cutoff chosen after seeing which one flatters the answer is not a
     cutoff. Say what happens when a gate FAILS: that stratum is reported unsupported, not
     relaxed until it passes. Most of these numbers already have defaults in the Parameters
     table below — this step is about COMMITTING to them (and to any change, with its reason)
     before the results can influence the choice.
1. **QC** (`run_scanpy_qc` for one matrix; `run_cellqc` INSTEAD when the data profile says the input is a folder of 10x Cell Ranger outputs (raw + filtered matrices): it corrects ambient RNA, removes doublets and writes the same normalised checkpoint): per-cell metrics, filter, normalize + log1p (+ HVG). Report counts.
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
   - **1 sample per arm, OR no sample column at all → there is no replication and no valid
     p-value for the condition.** Both cases are the same finding, and the second is easy to miss:
     a column named like a sample (`orig.ident`, `library`) that holds a SINGLE value across every
     cell means one library, not many, and a 2-level `sampleid` whose levels ARE the two arms is
     the condition column, not the sample column — so the study has no replicate structure
     whatsoever. Read the REPLICATION line in the dataset profile, which states each column's level
     count, before deciding. Do NOT quietly run the cell-level test and report its
     p-values as if they meant something, and do NOT plan a pseudobulk / per-donor aggregation
     step: there are no donors to aggregate over and the step cannot execute. Either state plainly
     that the comparison is DESCRIPTIVE — effect sizes and ranked genes only, no inferential claim,
     because nothing in the data separates the condition from the individual — or, if a cell-level
     ranking is still wanted, run `run_de(groupby=<condition column>, reference=<control level>,
     stratify_by=<cell-type column>)` and label it as exploratory ranking, not differential
     expression. Say the unit of replication in the plan itself, so the reader sees it before the
     result rather than after.
   - Skip / flag any cell type with too few cells in either group. Both tools do this for you
     and return `skipped_groups` — carry that into the report.

   **Never hand-write this contrast in `run_code` when a tool covers it.** `run_de` with
   `reference` + `stratify_by` IS the stratified condition-vs-control comparison; leaving
   `reference` at its default silently gives one-vs-rest MARKERS instead, which look like a DEG
   table and are not one. A dataset that already carries labels needs QC only — do not add a
   clustering step to satisfy the DE tool.

   **Memory** (only if you genuinely fall back to `run_code` for a design no tool expresses —
   paired/covariate, custom shared-signature rule): load the AnnData ONCE, and inside a
   per-cell-type loop subset with a **view** (`adata[mask]`) — do NOT `adata[mask].copy()` every
   cell type. On the local sandbox an over-budget loop is OOM-killed (`returncode == -9`); prefer
   `AISCIENTIST_RUN_CODE_ON_HPC=1` for a real `--mem` cap on large datasets. Keep the template's
   `de_<cell-type column>_all.csv` + `_universe.txt` writes — step 4 discovers DE results by
   exactly those names.

3b. **Composition** (`run_composition`): whether cell-type PROPORTIONS shift between the arms is
   a different question from which genes change, and is often the more visible effect. Same
   replication rule — it needs ≥2 samples per arm to test, and reports proportions without a
   test otherwise.
4. **Pathway interpretation per cell type**: `run_enrichment` (ORA on the changed genes, tested
   against the real universe) and `run_gsea_prerank` (over the whole ranking, signed NES).
   **Name the gene-set libraries in the step** — the defaults are `GO_Biological_Process_2023`,
   `Reactome_2022` and `MSigDB_Hallmark_2020` (KEGG is deliberately not a default: its GMT
   redistribution is licence-restricted). "Run pathway enrichment" without naming a library leaves
   the executor to choose one, which is the PI's call, not the executor's. They
   use different inputs and different nulls — do NOT require them to agree, and keep null
   results. Call both with **no `genes` argument**: step 3 wrote `tables/de_<key>_all.csv` and
   they find it, run ORA per cell type, and split each into its up- and down-regulated halves.
   Pasting a pooled gene list collapses every cell type into one `input` group and drops the
   tested-universe background, which inflates every p-value. Check the result: `background_source`
   must be `tested_universe`, not `constant_fallback`.
5. **Cross-cell-type synthesis.** Find genes changed in the SAME direction across ≥2 cell types
   (a shared / pan-tissue signature) vs cell-type-specific changes.
5b. **Literature grounding** (`deep_literature`): after the DE and pathway results exist, ground
   the surviving findings in published work. The query is built from what the analysis ACCEPTED —
   the actual genes, cell types and pathways — not from the user's original question, so this step
   is planned last among the analysis steps and never first. Ask focused questions ("is <gene> up
   in <cell type> in <disease>?"), not a topic dump.
6. **Figures / tables** (via `run_code`, adapting the template): a per-cell-type DEG-count summary
   table; a volcano plot per cell type; shared up- and down-regulated gene heatmaps; an enrichment
   bar plot per cell type. The final report is assembled automatically — do NOT plan a
   report-writing step.

## When this protocol and the question disagree

A researcher can ask for something this protocol advises against — most often by naming a tool and
its arguments directly ("run `run_de` with groupby=<condition>"). Do NOT silently pick one and
proceed, and do NOT silently plan both and let them contradict each other in the same plan.

Do this instead: **follow the request**, and in the same plan say — in the `SELF-SOURCED:` line —
that it departs from this protocol, which part, and what it costs ("this pools all cell types and
tests across cells, so the p-values are pseudoreplicated and a composition shift is
indistinguishable from a change in expression; reported as a ranking, not as DE"). If the valid
version of the analysis is also worth running, plan it as a SEPARATE step whose text says it
supersedes the first — never as a step whose justification calls an earlier step in the same plan
invalid while that earlier step's output is still reported as a result.

## Parameters

These are the tools' declared defaults — the same values the code applies, checked by
`tests/test_declared_params.py`, which fails if this table and `scrna_pack.PARAMS` disagree.
Leave a parameter alone unless the dataset gives you a reason to change it; when you do change one,
say so and say why in the plan's `SELF-SOURCED:` line, because a number chosen for one run reads
exactly like a protocol-mandated one once it reaches a Methods section.

**`run_scanpy_qc`**

| parameter | default | what it does |
| --- | --- | --- |
| `min_genes` | `200` | drop a cell detecting fewer genes than this — an empty droplet or a dying cell |
| `min_cells` | `3` | drop a gene detected in fewer cells than this — too sparse to support any test |
| `max_pct_mt` | `10.0` | drop a cell whose reads are more than this percent mitochondrial (a stressed or lysed cell). 10 is the common working threshold for tissue; the Seurat and scanpy tutorials use 5 for PBMC, and single NUCLEI need far less (1-5) because a nucleus should carry almost no mitochondrial signal. Raise it only for a tissue known to be mitochondria-rich, and say so |
| `n_top_genes` | `2000` | how many highly-variable genes to keep for the embedding — more genes carry more structure and more noise. 2000 is Seurat's default and the usual starting point |
| `warn_removed_pct` | `50.0` | warn when QC discards more than this percent of cells. An ENGINEERING guard, not a literature threshold: losing half a dataset usually means a threshold is wrong for this tissue, and it should be checked before the result is used |
| `mito_prefix` | `"MT-"` | the name prefix that marks a mitochondrial gene, matched without regard to case, so the default also covers mouse 'mt-'. It is the only way `max_pct_mt` knows which genes to count: a dataset whose genes match nothing gets no mitochondrial filter at all |
| `gene_symbols_key` | `""` | a `var` column holding gene SYMBOLS, for data whose gene names are Ensembl IDs (cellxgene files keep the symbols in `feature_name`). The mitochondrial prefix is then matched against that column. Empty = match the gene names themselves |

**`run_clustering`**

| parameter | default | what it does |
| --- | --- | --- |
| `resolution` | `1.0` | Leiden granularity: higher splits the cells into more, smaller clusters |
| `n_pcs` | `30` | principal components fed into the neighbourhood graph |
| `n_neighbors` | `15` | neighbours per cell in that graph — larger gives smoother, coarser structure |
| `select_resolution` | `false` | choose the resolution by bootstrap stability instead of accepting the default |
| `n_bootstrap` | `10` | resampling rounds per candidate resolution when selecting one |
| `subsample_frac` | `0.8` | fraction of cells per bootstrap round |
| `stability_min` | `0.9` | minimum adjusted Rand index a resolution must clear to be called stable |
| `max_sweep_cells` | `20000` | cap on the cells used for the stability sweep |

**`run_de`**

| parameter | default | what it does |
| --- | --- | --- |
| `groupby` | `"leiden"` | the obs column whose levels are compared |
| `method` | `"wilcoxon"` | the rank test rank_genes_groups uses |
| `n_genes` | `50` | rows kept per group — per DIRECTION when it is a contrast |
| `reference` | `"rest"` | the baseline level. 'rest' gives one-vs-rest MARKERS; a named level (e.g. 'WT') gives a real condition-vs-control contrast |
| `stratify_by` | `""` | an EXISTING cell-type column — runs the contrast separately within each cell type instead of pooling them |
| `min_cells` | `30` | a group with fewer cells than this in either arm is skipped. 30 is the usual rule-of-thumb floor for the rank test's normal approximation — an ENGINEERING guard, not a literature threshold |
| `min_pct` | `0.1` | a gene must be detected in at least this fraction of the cells of one of the two populations compared, or it is not tested at all (Seurat FindMarkers' min.pct). Without it most genes enter the test undetected — on the DDX41 retina object 2/3 did — which triples the BH denominator and floods the ranking with divide-by-zero fold-changes above 2^20. 0 disables |
| `tie_correct` | `true` | tie-correct the Wilcoxon normal approximation. A single-cell matrix is ~90% zeros, so ties dominate every comparison; scanpy's default (False) is anti-conservative on sparse data |
| `padj` | `0.05` | adjusted-p (Benjamini-Hochberg) cutoff for calling a gene significant |
| `lfc` | `0.25` | minimum abs log2 fold-change for calling a gene significant — applied together with `padj`, and drawn as the volcano's vertical line. 0.25 is the single-cell convention (Seurat's FindMarkers threshold); 1.0 is a bulk-RNA habit and hides most real single-cell effects |
| `force` | `false` | run a pooled per-cell test across a CONDITION column anyway. The result is pseudoreplicated and must be reported as non-inferential |

**`run_pseudobulk_de`**

| parameter | default | what it does |
| --- | --- | --- |
| `min_cells_per_sample` | `10` | a sample contributing fewer cells than this to a cell type is dropped from that cell type's test |
| `min_samples_per_condition` | `2` | refuse to test an arm backed by fewer samples than this — below it there is no replication and no valid p-value. 2 is the COMPUTABILITY floor, not a recommendation: the single-cell DE literature (Squair et al. 2021) asks for >=3 replicates per arm, and a 2-vs-2 result should be reported as underpowered |
| `min_count` | `10` | a gene must reach this many summed counts in at least as many samples as the smaller arm, or it is not tested (edgeR filterByExpr's rule of thumb). Genes nobody detected cannot be tested — they only inflate the BH denominator |
| `padj` | `0.05` | adjusted-p (Benjamini-Hochberg) cutoff used to count significant genes |

**`run_enrichment`**

| parameter | default | what it does |
| --- | --- | --- |
| `top_n_genes` | `0` | optional cap on how many genes enter the over-representation test. 0 means no cap — use every gene passing the thresholds below, which is the standard way ORA is run. Set it only to deliberately shorten a very long list, and say that you did |
| `top_n_terms` | `10` | how many enriched terms to report per group |
| `padj` | `0.05` | adjusted-p cutoff a DE gene must clear to enter the test — the same Benjamini-Hochberg cutoff run_de applies |
| `lfc` | `0.25` | minimum |log2 fold-change| a DE gene must clear to enter the test. Pairing an effect-size floor with the p-value cutoff is what keeps a list of thousands of barely-changed genes from returning only large generic terms; 0.25 is the single-cell convention |

## Grounding

Report effect sizes (log fold-change) and ADJUSTED p-values, not just gene names. Ground every
claim in the DE / enrichment statistics the tools returned — never fabricate genes, fold-changes,
or pathways. State the reference group and any cell types skipped for low cell count. Frame biology
as hypotheses to validate, not established fact.

**Pathway analysis: use `run_enrichment` / `run_gsea_prerank` when the design supports them, and
when it does not, still take the gene sets from disk.** Both tools read `.gmt` libraries that are
ALREADY ON DISK next to the tools — GO_Biological_Process_2023, Reactome_2022, MSigDB_Hallmark_2020
(`AISCIENTIST_GENESETS_DIR` overrides the location) — offline, no download, no network. But both also
consume `run_de`'s output table and return ORA/GSEA p-values and FDR. A design with no biological
replication forbids exactly those p-values and therefore often skips `run_de` entirely, which
leaves both tools unusable through no fault of the plan. That is NOT a reason to go to the network:
run 3c5fbc8608a7 hand-rolled a p-value-free pathway score, went looking for a collection to fetch,
hit the confirmation guarding downloads from non-allowlisted hosts, was declined, and recorded
`"collection_status": "no verified authorized species-compatible collection"` — with three verified
libraries sitting in the directory the tools read. When you compute a pathway summary yourself,
read the `.gmt` files from that same local directory and say in the step that you did.

**Sensitivity analyses may not be shopped.** The depth-matched check, an alternative QC cutoff,
a different mixture or standardization — each shows how much the answer depends on a choice. The
PRIMARY estimate stays primary: a variant is never promoted because it produced a larger effect,
a cleaner volcano, or a more attractive biology. Report them side by side with the cell mass each
retains, and let them disagree — a disagreement is the result, not a problem to resolve by
picking a winner.

State the **unit of replication and how many there were** — "n = 3 donors per arm", not "n =
4,812 cells". A reader cannot judge a condition contrast without it, and it is the single number
that distinguishes a real result from a pseudoreplicated one.
