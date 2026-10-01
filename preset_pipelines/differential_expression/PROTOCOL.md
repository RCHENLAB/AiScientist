---
name: differential_expression
description: Compare an experimental condition vs control (e.g. KO vs WT) per cell type, with pathway interpretation
tools: run_scanpy_qc, run_clustering, run_de, run_enrichment, run_code
---

# Differential Expression Protocol

**Purpose.** Take an annotated scRNA-seq dataset with **two experimental groups** (disease vs control,
knockout vs wild-type, treated vs untreated) and find the genes that change **between those groups,
separately within each cell type**, then interpret the changed genes as pathways. The comparison is read
from the dataset's own `obs` metadata — you don't need the user to spell it out. This is a
**condition-comparison** protocol; it does **not** assign cell-type labels (use `celltype_annotation` for
that).

|  |  |
|---|---|
| **Input** | one scRNA-seq AnnData whose `obs` has a **2-group condition column** (+ ideally an existing cell-type / cluster label column) |
| **Output** | per-cell-type DE tables (logFC + adjusted p), per-cell-type enrichment, a DEG-count summary table, volcano plots, shared up/down-regulated heatmaps, enrichment bar plots |
| **Engine** | `scanpy` (`rank_genes_groups`) driven by `run_scanpy_qc` / `run_de` (`reference=` + `stratify_by=`) / `run_enrichment`; `run_clustering` only when the data has no labels |
| **Not for** | assigning / predicting cell-type labels (use the `celltype_annotation` pipeline instead) |

> **How to read the "parameters" in each step.** Two kinds of knob feed these tools, and this protocol
> keeps them separate on purpose:
> - **🔬 Agent-chosen (scientific)** — the PI/Scientist decides these per study and you should audit them:
>   the **condition column** + its two groups, the **reference (control) group**, the **cell-type column**
>   to stratify by, QC filter thresholds, and the enrichment gene sets. Anything the SKILL leaves open is
>   marked *(agent-chosen per study)*.
> - **⚙️ Fixed (infra)** — the harness supplies these; they don't change the science: the `run_code`
>   sandbox image, and the `AISCIENTIST_RUN_CODE_ON_HPC=1` switch that gives the per-cell-type loop a real
>   `--mem` cap on large datasets (see `skills/README.md`). Shown once (Step 3) so you can see the method,
>   not re-audited per run.

---

## At a glance — the 6 steps

| # | Step | Tool / skill | 🔬 Key agent-chosen params | Output |
|---|------|--------------|----------------------------|--------|
| 1 | **QC the cells** | `run_scanpy_qc` | filter thresholds, HVG *(agent-chosen per study)* | filtered + normalized AnnData, reported counts |
| 2 | **Define the comparison** | *(planning, from DATASET PROFILE)* · `run_clustering` **only if no labels** | condition column, reference group, cell-type column | named contrast + stratification |
| 3 | **Per-cell-type DE** | `run_de` with `reference=` + `stratify_by=` | condition-vs-**reference** contrast, cell-type column, `min_cells` | per-cell-type DEG tables + volcanoes |
| 4 | **Pathway enrichment** | `run_enrichment` | up- & down-gene sets per cell type (SYMBOLS) | enrichment per cell type |
| 5 | **Cross-cell-type synthesis** | *(report from Step-3 tables)* | — | shared vs cell-type-specific signatures |
| 6 | **Figures / tables** | `run_code` (adapt template) | *(adapt template)* | DEG-count table, volcano, heatmaps, enrichment bars |

> Plan these as **distinct agenda steps**. If the dataset already carries cell-type labels, **REUSE them**
> and **skip clustering** — do not re-cluster or re-annotate from scratch. The final report is assembled
> automatically — **do not add a report-writing step.**

### Default study (a dataset with a 2-group condition column + existing cell-type labels)

**"condition vs control differential expression within each major cell type, then pathway enrichment on
the changed genes."** Plan that *without* waiting for the user to describe it — even for a vague or empty
question ("analyze this dataset"), the dataset's own metadata defines the experiment.

---

## Step-by-step

<details open>
<summary><b>Step 1 · QC the cells</b> — clean, normalize, and count before comparing</summary>

**What.** Compute per-cell QC metrics, filter, then `normalize` + `log1p` (and optionally select HVGs).
**Report the cell counts.**

**Why.** DE between groups is only meaningful on a filtered, log-normalized matrix; the counts before/after
filtering are the audit trail for what entered the comparison.

**🔬 Agent-chosen:** the filter thresholds and whether to select HVGs *(agent-chosen per study)*. **Honor
any QC columns already in the data** (e.g. `percent.mt`, doublet calls) rather than recomputing over them.

**What runs** — the `run_scanpy_qc` tool call:
```
run_scanpy_qc(...)   # per-cell QC metrics → filter → normalize + log1p (+ HVG); report counts
```

**✅ Verify this step:** cell counts before/after filtering are reported · existing QC columns
(`percent.mt`, doublet flags) were honored, not silently overwritten · the matrix is normalized + log1p'd
before any DE.

<sub>Source: `run_scanpy_qc` · SKILL.md "Ordered plan" §1</sub>
</details>

<details>
<summary><b>Step 2 · Define the comparison</b> — read the experiment out of the metadata</summary>

**What.** From the DATASET PROFILE (`obs` columns + their category values, given at planning time) name
four things: the **condition column**, its **two groups**, the **reference (control) group**, and the
**cell-type column** you will stratify by.

**Why.** The dataset's metadata *is* the experiment — you do not need the user to describe it. Getting the
reference group right is what makes the fold-changes point the correct direction.

**🔬 Agent-chosen:**

| Choice | How to pick it |
|---|---|
| **Condition / group column** | a low-cardinality (usually 2-level) `obs` column naming an experimental variable: `sampleid`, `condition`, `genotype`, `treatment`, `group`, `orig.ident`, `disease`. Its two values **are** the comparison (e.g. `sampleid=[DDX41, WT]` → DDX41 vs WT). The dataset / file name often confirms intent (`*_DEG` = they want DE). |
| **Reference (control) group** | the level whose name looks like the baseline — `WT`, `wild-type`, `control`, `ctrl`, `untreated`, `vehicle`, `DMSO`, `normal`, `sham`. The **other** level is the condition of interest. **State which you chose.** If it is genuinely unclear, ask **ONE** clarify question in plan mode rather than guessing. |
| **Cell-type stratification** | if `obs` already has a cell-type / cluster label column (`majorclass`, `celltype`, a predicted-label column), **REUSE it** and compare **within each cell type**. Do **NOT** re-cluster or re-annotate. **Only if no label column exists** do you `run_clustering` first and compare across clusters. |

**What runs** — nothing heavy: this is a planning decision. `run_clustering` runs **only** in the no-label
fallback:
```
# only if obs has NO cell-type / cluster label column:
run_clustering(...)   # otherwise: reuse the existing label column, skip clustering
```

**✅ Verify this step:** the condition column, both groups, and the **chosen reference group** are stated
explicitly · the cell-type column used for stratification is named · clustering was **skipped** when labels
already existed.

<sub>Source: SKILL.md "Figuring out WHAT to compare from the data" + "Ordered plan" §2 · `run_clustering`</sub>
</details>

<details>
<summary><b>Step 3 · Per-cell-type differential expression</b> — condition vs reference, within each cell type</summary>

**First, pick the test from the DESIGN.** A condition contrast is a statement about the CONDITION, so the
unit of replication is the **sample** (donor / animal / library), never the cell. With **≥2 samples per
arm**, use **`run_pseudobulk_de`** (`sample_key`, `condition_key`, `group_key` = the cell-type column) — a
Wilcoxon test over cells treats cells from one donor as independent observations of that donor's condition
and its p-values are anti-conservative by orders of magnitude. With **1 sample per arm** there is no valid
p-value for the condition at all: say so, and treat what follows as an exploratory ranking.

**What.** For **EACH** cell type that has enough cells in **BOTH** groups, run condition-vs-reference DE.
For the no-replicate / exploratory case `run_de` does this directly — pass `reference` (the control level)
instead of accepting the one-vs-rest default, and `stratify_by` (the cell-type column) to run the contrast
within each cell type. Either tool **skips and reports** cell types with too few cells in an arm
(`skipped_groups`) rather than silently dropping them.

**Why.** A whole-dataset DE would confound cell-type composition with the condition effect; stratifying by
cell type isolates the per-lineage response. Leaving `reference` at its default gives *markers*
(one-vs-rest) — a different question that looks superficially like a DEG table.

**🔬 Agent-chosen:** the condition-vs-**reference** contrast and the cell-type column (both carried from
Step 2). The per-cell-type minimum-cell cutoff (`min_cells`, default 30) is a judgement call
*(agent-chosen per study)*.

**What runs** — the `run_de` tool call:
```
run_de(groupby=<condition column>, reference=<control group>, stratify_by=<cell-type column>,
       min_cells=30)
  # writes tables/de_<cell-type column>_all.csv (BOTH directions), one table + volcano per cell
  # type, the tested universe, and .rnk files for run_gsea_prerank
```
This reads `adata_qc.h5ad` when there is no clustering checkpoint, so a dataset that already carries
labels needs **only** Step 1 — do not add a clustering step to satisfy it.

**Fall back to `run_code`** (template `condition_by_celltype.py`) only for a design the tool cannot
express — a paired/covariate-adjusted comparison, or a custom shared-signature rule. If you do,
**keep the template's `de_<cell-type column>_all.csv` + `_universe.txt` writes**: Step 4 discovers DE
results by those exact names. **⚙️ Memory discipline** for that loop: load the AnnData **ONCE** and subset
with a **view** (`adata[mask]`), never `.copy()` per cell type. On the local sandbox an over-budget loop is
**OOM-killed** (`returncode == -9`); prefer `AISCIENTIST_RUN_CODE_ON_HPC=1` (see `skills/README.md`) for a real
`--mem` cap on large datasets.

**✅ Verify this step:** the result's `comparison` field names the condition, the **reference**, and the
stratification column · `reference` is NOT `"rest"` · `significant_by_group` reports **both** up and down
counts · `skipped_groups` (if any) is carried into the report as cell types the contrast did not cover ·
results carry **logFC + adjusted p-values**.

<sub>Source: SKILL.md "Ordered plan" §3 · `run_de` · template `skills/condition_by_celltype/reference.py`</sub>
</details>

<details>
<summary><b>Step 4 · Pathway enrichment per cell type</b></summary>

**What.** Call `run_enrichment` with **no `genes` argument**. It finds Step 3's `de_<key>_all.csv` itself
and runs ORA per cell type, splitting each into its **up-** and **down-**regulated halves.

**Why.** Enrichment turns each cell type's changed-gene list into interpretable pathways — computed per
cell type so the biology stays lineage-specific, and per direction so an up-regulated and a
down-regulated programme cannot cancel each other into "nothing enriched".

**🔬 Agent-chosen:** the enrichment gene sets *(agent-chosen per study)*. The gene lists are **not**
agent-chosen — they come from the Step-3 table.

**What runs** — the `run_enrichment` tool call:
```
run_enrichment()   # discovers tables/de_<key>_all.csv; ORA per cell type × direction
```
**Do NOT paste a pooled gene list into `genes`** — that collapses every cell type into one `input` group
and drops the tested-universe background, which inflates every p-value.

**✅ Verify this step:** the returned `groups` name each cell type with `(up)` / `(down)` (or the result
says why not) · `background_source` is `tested_universe`, **not** `constant_fallback` · reported pathways
trace back to the DE gene lists from Step 3.

<sub>Source: `run_enrichment` · SKILL.md "Ordered plan" §4</sub>
</details>

<details>
<summary><b>Step 5 · Cross-cell-type synthesis</b> — shared vs cell-type-specific signal (report from Step-3 tables)</summary>

**What.** Find genes changed in the **same direction** across **≥2 cell types** (a shared / pan-tissue
signature) versus changes that are **cell-type-specific**.

**Why.** Separating the shared program from the lineage-specific responses is the biological payoff of
having stratified — it distinguishes a global effect from a cell-type-restricted one.

**✅ Verify this step:** shared (≥2 cell types, same direction) and cell-type-specific gene sets are
reported separately · every gene traces back to a per-cell-type DE result from Step 3 (no new statistics
invented here).

<sub>Source: SKILL.md "Ordered plan" §5</sub>
</details>

<details>
<summary><b>Step 6 · Figures / tables</b> — via run_code, adapting the template</summary>

**What.** Produce, via `run_code` (adapting the template): a per-cell-type **DEG-count summary table**, a
**volcano plot per cell type**, **shared up- and down-regulated gene heatmaps**, and an **enrichment bar
plot per cell type**.

**Why / honesty.** These render the statistics from Steps 3–5; they do not create new results. **The final
report is assembled automatically — do NOT plan a report-writing step.**

**What runs** — the `run_code` tool call:
```
run_code( adapt template )   # DEG-count summary table · per-cell-type volcano ·
                             # shared up/down heatmaps · per-cell-type enrichment bar plot
```

**✅ Verify this step:** each figure/table is backed by a Step 3–5 result (counts, logFC, adjusted p,
enrichment) · no separate report-writing step was planned.

<sub>Source: SKILL.md "Ordered plan" §6 · `run_code`</sub>
</details>

---

## Grounding & honesty (applies to every step)

Report **effect sizes (log fold-change)** and **ADJUSTED p-values**, not just gene names. Ground every
claim in the DE / enrichment statistics the tools actually returned — **never fabricate genes, fold-changes,
or pathways**. State the **reference group** and **any cell types skipped** for low cell count. Frame biology
as **hypotheses to validate**, not established fact.

## Method provenance

scanpy `rank_genes_groups` (per-cell-type condition-vs-reference), driven by `run_scanpy_qc` / `run_de`
(`reference=` + `stratify_by=`) / `run_enrichment`; `run_clustering` only for unlabeled data. Designs the
tool cannot express fall back to `run_code` adapting `skills/condition_by_celltype/reference.py`, and on
large datasets that loop runs with a real `--mem` cap under `AISCIENTIST_RUN_CODE_ON_HPC=1` (see
`skills/README.md`).

<sub>This protocol renders `preset_pipelines/differential_expression/SKILL.md` — regenerate to keep it in
step with the skill.</sub>
