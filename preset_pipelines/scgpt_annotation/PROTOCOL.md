---
name: scgpt_annotation
description: scGPT foundation-model per-cell annotation for a .h5ad — transfers reference cell-type labels + calibrated confidence to every cell, AND independently cross-validates ANY existing celltype/majorclass labels the data already carries. Use for scGPT / .sif / foundation-model annotation, per-cell labelling, OR an independent second-opinion check of a dataset's existing cell-type labels.
tools: scgpt_annotate, run_scanpy_qc, run_clustering, run_de, run_code
---

# scGPT Per-Cell Annotation & Cross-Validation Protocol

**Purpose.** Take a scRNA-seq `.h5ad` and give **every cell** a cell-type label + calibrated
confidence using a foundation model (**scGPT**), then **independently validate** those labels against
the data's own structure (Leiden clusters + marker genes) and against any cell-type labels the file
already carries. scGPT does **two jobs** here: it **transfers** reference-atlas labels to unlabelled
cells, AND it acts as an **independent second opinion** that **cross-validates** an existing
`celltype`/`majorclass` column. scGPT is a pretrained gene/expression transformer — a *different*
method from naming clusters by their markers — so its labels are a **strong hypothesis to validate,
not ground truth**.

|  |  |
|---|---|
| **Input** | one AnnData `.h5ad` (scRNA-seq query), passed **as-is** — may already carry a `celltype` / `majorclass` label column |
| **Output** | `data/scgpt_predictions.csv` (per-cell label + confidence, **one row per RAW-upload cell, indexed by barcode**) + an independent Leiden clustering + cross-validation tables + figures |
| **Engine** | **scGPT** — a pretrained gene/expression transformer — run via `scgpt_annotate` as a **short-lived GPU Singularity batch job** (the scGPT `.sif` image); the scanpy structure/figure steps (`run_scanpy_qc` / `run_clustering` / `run_de` / `run_code`) run separately |
| **Not for** | marker-based cluster naming *without* a foundation model (use the `celltype_annotation` skill), or any non-`.h5ad` query |

> **How to read the "parameters" in each step.** Two kinds of knob feed this pipeline, and this
> protocol keeps them separate on purpose:
> - **🔬 Agent-chosen (scientific)** — the PI/Scientist adapts these to THIS dataset and you should
>   audit them: the QC thresholds, the clustering settings (neighbors / Leiden resolution / UMAP),
>   which canonical markers to violin, and — the key branch — **whether the data already carries a
>   `celltype`/`majorclass` column** to cross-validate against. The SKILL gives no fixed numbers, so
>   treat each of these as **"(agent-chosen per study)"**.
> - **⚙️ Fixed (infra)** — the gateway/tool injects these; the model never sets them and they don't
>   change the science: the **pretrained scGPT model + its reference-atlas taxonomy / gene vocabulary**,
>   the scGPT `.sif` image, and the GPU batch job. The reference taxonomy **bounds** the label set
>   (a caveat, not a knob — see Grounding).

---

## At a glance — the 6 steps

| # | Step | Tool / skill | 🔬 Key agent-chosen params | Output |
|---|------|--------------|----------------------------|--------|
| 1 | **Annotate every cell (scGPT)** | `scgpt_annotate` | none — pass the query **as-is** (no pre-norm/HVG) | `data/scgpt_predictions.csv` |
| 2 | **QC the dataset** *(for the independent yardstick, not for scGPT)* | `run_scanpy_qc` | QC thresholds *(agent-chosen)* | filtered analysis `adata` |
| 3 | **Cluster independently** | `run_clustering` | neighbors / Leiden resolution / UMAP *(agent-chosen)* | Leiden clusters + UMAP |
| 4 | **Marker genes per cluster** | `run_de` | DE grouping *(agent-chosen)* | per-cluster markers |
| 5 | **Cross-validate the scGPT labels** ⚑ | `run_code` (adapt `crossvalidate_scgpt_vs_leiden` template) | merge **by barcode** | confusion tables + agreement % + confidence dist |
| 6 | **Figures** | `run_code` / scanpy plotting | canonical markers to violin *(agent-chosen)* | UMAPs + confidence dist + marker violins |

> Plan these as **distinct agenda steps**. Step 1 is the annotation; Steps 2–4 build a **data-driven
> structure that does NOT depend on the scGPT labels**; Steps 5–6 **check** scGPT against that
> structure. **Do NOT normalize / HVG / log1p the data before Step 1** — `scgpt_annotate` preprocesses
> internally. The report is assembled automatically — **do not add a report-writing step.**

### The two jobs — transfer AND independent cross-validation (why the extra steps)

scGPT does exactly one thing: give every cell a reference-atlas label + confidence (**Step 1**).
Everything after it exists to **check** that label, not to produce it:
- **Steps 2–4** (QC → Leiden → markers) build structure derived **purely from the data**, independent
  of the scGPT labels — an unbiased yardstick.
- **Step 5** compares the scGPT labels against that Leiden structure **AND**, when the file already
  carries a `celltype`/`majorclass` column, **directly against those existing labels** — agreement %,
  per-label confidence, and the populations where scGPT **disagrees** or is **low-confidence**.
- A **pre-annotated** `.h5ad` does **NOT** disqualify this pipeline — it is *exactly* the
  cross-validation case: scGPT is the independent foundation-model second opinion on those labels.
  Do **not** swap in marker-only naming just because labels already exist.

scGPT is a **different method** from naming clusters by their markers — treat its labels as a strong
hypothesis to validate, never as ground truth.

---

## Step-by-step

<details open>
<summary><b>Step 1 · Annotate every cell with scGPT</b> — the primary per-cell label + confidence</summary>

**What.** Run `scgpt_annotate` on the loaded `.h5ad`, passed **as-is**. It transfers a reference-atlas
cell-type label + a **data-calibrated confidence** to **EVERY** cell of the RAW upload and writes
`data/scgpt_predictions.csv` — **one row per RAW-upload cell, indexed by the original barcode**
(columns `index`, `predictions`, `confidence`).

**Why.** scGPT is a pretrained gene/expression transformer — a fundamentally different capability from
naming clusters by their markers. It gives **per-cell, reference-consistent** labels (not per-cluster
names) for cells that may be unlabelled, and serves as the independent second opinion on cells that
are already labelled.

**🔬 Agent-chosen:** none for the call itself — **pass the query as-is.** Do **NOT** normalize / HVG /
log1p beforehand: `scgpt_annotate` preprocesses internally (gene-vocabulary alignment, HVG, log1p) on
the GPU job, and pre-processing it yourself corrupts that alignment.

**What runs** — the tool call (heavy lifting is inside the GPU job, not a shell command you write):
```
scgpt_annotate( <the loaded .h5ad, passed as-is — no pre-norm / no HVG / no log1p> )
# → data/scgpt_predictions.csv   columns: index (ORIGINAL barcode), predictions, confidence
#   one row per RAW-upload cell (BEFORE any QC filtering in Step 2)
```
Inference runs as a **short-lived GPU Singularity batch job** (may queue). If scGPT is **not enabled**
(no GPU / image) or errors, the tool reports `not_enabled` / `error` — **say so and fall back to the
marker-based path** (the `celltype_annotation` skill). Do **not** invent labels.

**✅ Verify this step:** `data/scgpt_predictions.csv` exists with **one row per RAW cell** · a label
distribution is reported (from the tool, not fabricated) · the query was **not** pre-normalized/HVG'd
before the call · a `not_enabled` / `error` result is surfaced honestly, not papered over with invented
labels.

<sub>Source: `preset_pipelines/scgpt_annotation/SKILL.md` (step 1) + `src/bioagent/tools/scgpt_annotate.py`</sub>
</details>

<details>
<summary><b>Step 2 · QC the dataset</b> — for the independent yardstick, <i>not</i> for scGPT</summary>

**What.** Standard per-cell / per-gene QC (`run_scanpy_qc`): filter low-quality cells and genes to
produce the clean `adata` used by Steps 3–6.

**Why.** This prepares the data for the **INDEPENDENT** structure and figures below — **not** for scGPT
(scGPT did its own internal preprocessing in Step 1 on the raw upload). QC **removes some cells**, so
the QC'd `adata` has **FEWER** cells than the prediction CSV — that mismatch is **expected** and is
exactly why Step 5 must merge by barcode.

**🔬 Agent-chosen:** the QC thresholds (min genes/counts per cell, % mitochondrial, gene prevalence,
etc.) — *(agent-chosen per study)*.

**What runs:**
```
run_scanpy_qc( <thresholds agent-chosen for this dataset> )   # → a filtered analysis-adata checkpoint
```

**✅ Verify this step:** QC is applied to the **analysis** `adata` only (scGPT's input was the raw
upload) · the cell count **drops** versus the raw upload (this is expected, not a bug) · the thresholds
used are stated.

<sub>Source: `preset_pipelines/scgpt_annotation/SKILL.md` (step 2)</sub>
</details>

<details>
<summary><b>Step 3 · Cluster independently</b> — data-driven structure that does not use scGPT labels</summary>

**What.** `run_clustering`: neighbors → **Leiden** → **UMAP** on the QC'd data.

**Why.** Gives a structure derived **purely from the data**, independent of the scGPT labels, so Step 5
can check scGPT against something it did **not** produce. (If clustering leaned on the scGPT labels the
cross-validation would be circular.)

**🔬 Agent-chosen:** neighbors, Leiden **resolution**, UMAP settings — *(agent-chosen per study)*.

**What runs:**
```
run_clustering( <neighbors, Leiden resolution, UMAP — agent-chosen> )   # → Leiden clusters + UMAP embedding
```

**✅ Verify this step:** clustering used the **data**, not the scGPT labels · Leiden clusters + a UMAP
exist for the QC'd cells.

<sub>Source: `preset_pipelines/scgpt_annotation/SKILL.md` (step 3)</sub>
</details>

<details>
<summary><b>Step 4 · Marker genes per cluster</b> — read the clusters' biology from the data</summary>

**What.** `run_de`: differential expression **per Leiden cluster** to surface each cluster's marker
genes.

**Why.** So the clusters' biology can be read **directly from the data** — which cluster is which cell
type by its own markers. This is the biological anchor the scGPT labels are checked against in Step 5
(and the source of the canonical markers violined in Step 6).

**🔬 Agent-chosen:** the DE grouping / method parameters — *(agent-chosen per study)*.

**What runs:**
```
run_de( <group by Leiden cluster — params agent-chosen> )   # → per-cluster marker genes
```

**✅ Verify this step:** markers are computed **per Leiden cluster** · marker calls come from the tool
output, not fabricated.

<sub>Source: `preset_pipelines/scgpt_annotation/SKILL.md` (step 4)</sub>
</details>

<details>
<summary><b>Step 5 · Cross-validate the scGPT labels</b> — the core check ⚑ <i>merge by barcode</i></summary>

**What.** Adapt the reference template **`crossvalidate_scgpt_vs_leiden`** (via `run_code`) to compare
the scGPT per-cell labels against, **for each reference column available**: **(a)** the independent
Leiden clusters, and **(b)** — when the file carries a `celltype`/`majorclass` column — the **existing
labels directly**. For each it writes a **confusion table** (reference × scGPT-label) + a per-group
**agreement / purity** summary, plus the scGPT **confidence distribution** flagging low-confidence
cells / populations.

**Why / ⚑ honesty.** **The two cell sets DIFFER.** scGPT labelled **every RAW cell** (Step 1); QC
dropped some (Step 2), so the prediction CSV has **MORE** rows than the QC'd `adata` (e.g. 11,977
predictions vs 11,970 cells). **MERGE BY BARCODE, never by row position.** A positional merge — or
assigning a length-N prediction column onto an M-cell `adata` — raises a pandas alignment error and the
**whole step fails**; this is **the single most common way this pipeline stalls**. The same barcode
alignment applies to the Leiden **and** the majorclass/celltype comparison.

**🔬 Agent-chosen:** adapt the CONFIG / column names / thresholds to THIS dataset's CSV.

**What runs** — the barcode-safe merge from the template (adapt it; do **not** hand-roll the merge):
```
# fetch: read_skill_reference("crossvalidate_scgpt_vs_leiden", file="reference.py"); execute via run_code
pred = pd.read_csv(".../data/scgpt_predictions.csv").set_index("index")
adata.obs["scgpt_pred"] = pred["predictions"].reindex(adata.obs_names)   # align BY BARCODE
adata.obs["scgpt_conf"] = pred["confidence"].reindex(adata.obs_names)    # (or obs_names.intersection)
# → confusion(reference × scGPT) for Leiden AND for any existing majorclass/celltype
#   + per-group agreement/purity + the scGPT confidence distribution
```

**✅ Verify this step:** the merge is **by barcode** (`reindex` on `obs_names` / `obs_names.intersection`),
**NOT** row order · **no** length-N-onto-M-cells column assignment · an existing `celltype`/`majorclass`
column, if present, is compared **directly** (agreement %) — not ignored · low-confidence cells /
populations are **flagged**.

<sub>Source: `preset_pipelines/scgpt_annotation/SKILL.md` (step 5 + ⚑ callout) + `skills/crossvalidate_scgpt_vs_leiden/reference.py`</sub>
</details>

<details>
<summary><b>Step 6 · Figures</b> — make the agreement (and the disagreement) visible</summary>

**What.** Produce: a **UMAP coloured by scGPT predictions** AND **by Leiden cluster**, the **confidence
distribution**, and **violins of canonical markers** for the predicted types.

**Why.** Lets a reader see the agreement at a glance (do the scGPT labels tile the Leiden UMAP
cleanly?), where confidence is low, and whether the predicted types' **canonical markers actually
express** — a direct data check on the labels.

**🔬 Agent-chosen:** which canonical markers to violin (for the predicted types) — *(agent-chosen per
study)*.

**What runs** — the figures are produced via `run_code` / scanpy plotting on the **barcode-merged**
`adata` (the SKILL lists the figures, not a fixed command):
- UMAP × scGPT prediction, UMAP × Leiden cluster
- scGPT confidence distribution
- canonical-marker violins for the predicted types

**✅ Verify this step:** all four figure kinds are present · the markers shown are the **canonical
markers for the PREDICTED types** · figures are drawn on the **barcode-merged** `adata` (Step 5), not on
a mis-aligned join.

<sub>Source: `preset_pipelines/scgpt_annotation/SKILL.md` (step 6)</sub>
</details>

---

## Grounding & honesty (applies to every step)

Report **ONLY** cell types, gene names, confidences and numbers that the tools actually returned —
never fabricate them. State the label-transfer caveats honestly: the taxonomy is
**reference-bounded** (scGPT can only emit labels its reference atlas contains), there is **no
fine-tuning** on this query, and **query-vs-reference differences** shift calibration. Treat scGPT
labels as a **strong hypothesis to validate**, not ground truth, and read them **together** with the
independent Leiden structure and (when present) the existing `celltype`/`majorclass` labels. If
`scgpt_annotate` is **not enabled** (no GPU / image) or errors, **say so and fall back to the
marker-based path** (the `celltype_annotation` skill) rather than inventing labels. The report is
assembled automatically — **do not plan a report-writing step.**

## Method provenance

**scGPT** pretrained gene/expression transformer — Route C: a short-lived `gpu:1` **Singularity batch
job** on HPC3, orchestrated by `gateway/scgpt_job`, with `scgpt`/`torch` living only in the image ·
**scanpy** QC / neighbors–Leiden–UMAP / DE for the independent, data-driven structure · **barcode-safe
cross-validation** via the `crossvalidate_scgpt_vs_leiden` reference template. Full workflow and
deployment details: [`docs/scgpt_workflow_integration.md`](../../docs/scgpt_workflow_integration.md).

<sub>This protocol's tool-call and merge excerpts are drawn from the cited SKILL and source files —
regenerate to keep them faithful to what runs.</sub>
