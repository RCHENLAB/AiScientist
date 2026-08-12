---
name: gene_signature_scoring
description: Score cells for a gene signature and compare it across clusters/conditions
tools: run_scanpy_qc, run_clustering, run_code
---

# Gene-Signature Scoring Protocol

**Purpose.** Take a single-cell dataset and a **defined gene set** (a pathway, a program, a published
signature) and quantify **how strongly each cell expresses it**, then compare that score **across
clusters or across conditions**. This is a *scoring* question — it does **not** discover markers de novo
(use `differential_expression`) and does **not** assign cell types (use `celltype_annotation`).

|  |  |
|---|---|
| **Input** | one scRNA-seq dataset (AnnData) **+ a signature gene list** the study wants to score |
| **Output** | a per-cell signature score, a per-group score summary, a UMAP coloured by score, and a violin/box plot of the score per group |
| **Engine** | `scanpy` — `run_scanpy_qc` (QC + normalize/log1p) → optional `run_clustering` → `sc.tl.score_genes` adapted via `run_code` |
| **Not for** | de-novo marker discovery (`differential_expression`) · cell-type assignment (`celltype_annotation`) |

> **How to read the "parameters" in each step.** Two kinds of knob feed this pipeline, kept separate on purpose:
> - **🔬 Agent-chosen (scientific)** — the PI/Scientist decides these per study and you should audit them:
>   the **signature gene list**, the **`groupby`** comparison axis (per-cluster vs per-condition), the QC
>   filter thresholds, and the clustering resolution (if Step 2 runs).
> - **⚙️ Fixed (method)** — what the scanpy calls do under the hood: `sc.tl.score_genes`'s
>   mean-minus-random-control formula, the normalize + `log1p` transform. Shown for method transparency,
>   not re-audited per run.

---

## At a glance — the 4 steps

| # | Step | Tool / skill | 🔬 Key agent-chosen params | Output |
|---|------|--------------|----------------------------|--------|
| 1 | **QC the dataset** | `run_scanpy_qc` | filter thresholds | filtered, normalized + log1p'd AnnData + cell/gene counts |
| 2 | **(Optional) Cluster** | `run_clustering` | resolution — *only if comparing per-cluster* | cluster labels + UMAP |
| 3 | **Score the signature** | `run_code` (`score_signature.py` → `sc.tl.score_genes`) | the **signature gene list**, the `groupby` | per-cell score + per-group summary |
| 4 | **Figures** | `run_code` | the group axis for the violin | UMAP coloured by score + violin/box per group |

> Plan these as **distinct agenda steps**. Step 2 is **optional** — run it only when the comparison is
> per-cluster; skip it when comparing across an existing condition/sample label. The report is assembled
> automatically — **do not add a report-writing step.**

---

## Step-by-step

<details open>
<summary><b>Step 1 · QC the dataset</b> — is the matrix in a state you can score?</summary>

**What.** Run `run_scanpy_qc`: compute per-cell metrics, filter cells/genes, then **normalize + `log1p`**.
Report the cell/gene counts (before and after filtering).

**Why.** `sc.tl.score_genes` reads the **normalized log-expression** matrix. Scoring raw counts or an
unfiltered matrix distorts the score (library-size effects, low-quality cells). Reporting the counts tells
the reader how much was dropped before any biology is claimed.

**🔬 Agent-chosen:** the QC filter thresholds (e.g. min genes/cells, mito %) — adapt to THIS dataset.

**What runs** — the `run_scanpy_qc` tool call: per-cell QC metrics → filter → `normalize` + `log1p`.

**✅ Verify this step:** cell/gene counts **before and after** filtering are reported · the matrix is
normalized + log1p'd (not raw counts) before Step 3 scores it.

<sub>Source: `preset_pipelines/gene_signature_scoring/SKILL.md` (step 1) · `run_scanpy_qc`</sub>
</details>

<details>
<summary><b>Step 2 · (Optional) Cluster the cells</b> — <i>only if the comparison is per-cluster</i></summary>

**What.** Run `run_clustering` to get cluster labels — **only when the comparison in Step 3 is per-cluster**.
If you are comparing the score across an existing **condition / sample** label, **skip this step**.

**Why.** A per-cluster comparison needs cluster labels to group by; a per-condition comparison already has
its grouping variable, so clustering adds nothing.

**🔬 Agent-chosen:** whether to run at all (the comparison axis) · the clustering resolution.

**What runs** — the `run_clustering` tool call (produces cluster labels + a UMAP embedding).

**✅ Verify this step:** run **only** for a per-cluster comparison · cluster labels exist for Step 3 · the
step is **skipped** (and said to be skipped) when the comparison is per-condition.

<sub>Source: `preset_pipelines/gene_signature_scoring/SKILL.md` (step 2) · `run_clustering`</sub>
</details>

<details>
<summary><b>Step 3 · Score every cell for the signature</b> — the core (no curated tool covers this)</summary>

**What.** No curated tool scores a signature, so adapt the reference template `score_signature.py` via
`run_code`, using scanpy `sc.tl.score_genes`. Then **summarize the score per cluster/condition**.

**Why.** `sc.tl.score_genes` assigns each cell the **mean expression of the signature genes minus the mean
of a matched random control gene set** — a per-cell score that is comparable across cells.

**🔬 Agent-chosen:**

| Param | What it is |
|---|---|
| the **signature gene list** | the gene set being scored (agent-chosen per study) |
| the **`groupby`** | the axis to summarize over — cluster (Step 2) or condition/sample |

**What runs** — `run_code` adapting `score_signature.py`: `sc.tl.score_genes(adata, gene_list,
score_name=…)`, then a per-group summary of the resulting score column.

> **State which signature genes were actually present in the data.** Genes in the list but absent from the
> matrix are dropped from the score — **missing genes weaken it**, so report present-vs-missing explicitly.

**✅ Verify this step:** the genes present vs missing are stated · a per-cell score column was added · the
per-group summary is a **distribution, not a single number**.

<sub>Source: `preset_pipelines/gene_signature_scoring/SKILL.md` (step 3) · `score_signature.py` (`sc.tl.score_genes`)</sub>
</details>

<details>
<summary><b>Step 4 · Figures</b> — show the score in space and per group</summary>

**What.** A **UMAP coloured by the signature score** and a **violin/box plot of the score per group**.

**Why.** The UMAP shows where the score concentrates on the manifold; the violin/box shows the per-group
distribution — the evidence a group is (or isn't) enriched for the signature.

**🔬 Agent-chosen:** the group axis for the violin/box (the same `groupby` as Step 3).

**What runs** — `run_code`: `sc.pl.umap(color=<score_name>)` + a violin/box of the score grouped by
cluster/condition.

**✅ Verify this step:** the UMAP is coloured by the score · the violin/box is **per group** · the plots
show the **distribution**, never a single summary value standing in for a group.

<sub>Source: `preset_pipelines/gene_signature_scoring/SKILL.md` (step 4)</sub>
</details>

---

## Grounding & honesty (applies to every step)

State **which genes from the signature were actually present** in the data — missing genes weaken the
score. Report the score **distribution per group, not a single number**; do **not** claim a group is
"positive" for the signature without showing the per-group statistics. Report the cell/gene counts from
QC. This pipeline **scores a predefined gene set** — it does not discover markers (`differential_expression`)
or assign cell types (`celltype_annotation`); frame the score as a quantitative comparison, not a label.

## Method provenance

`scanpy` `sc.tl.score_genes` (mean signature expression minus a matched random control set; the
Seurat `AddModuleScore` / Satija et al. 2015 approach), with QC/normalization via `run_scanpy_qc` and
optional Leiden/Louvain clustering via `run_clustering`. Per-cell scoring is adapted from the reference
template `score_signature.py` through `run_code`.

<sub>This protocol renders only what `SKILL.md` specifies — regenerate it if the skill's steps change.</sub>
