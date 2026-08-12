---
name: celltype_annotation
description: Single-cell cell-type annotation + report
tools: run_scanpy_qc, run_clustering, run_de, run_enrichment, run_code
---

# Single-cell Cell-type Annotation Protocol

**Purpose.** Take an scRNA-seq dataset and assign a **cell-type label to each cluster** by reading the
marker genes that actually distinguish it. The flow is the canonical scanpy one — QC → cluster → find
per-cluster markers → (optionally) enrich → interpret the markers into a label → draw the figures — and
every label is grounded in the genes the tools returned, with confidence and uncertainty stated honestly.
Adapt every parameter to **this** dataset.

|  |  |
|---|---|
| **Input** | one scRNA-seq dataset (single-cell expression matrix, scanpy / AnnData) |
| **Output** | a **cell-type label per cluster** (with confidence) + figures (UMAP by cluster & by cell type, marker violin/dot plots) |
| **Engine** | `scanpy` — QC/normalize/HVG, PCA→neighbors→Leiden→UMAP, `rank_genes` per cluster; optional enrichment; optional `run_code` for label assignment |
| **Not for** | VCF / variant interpretation (use the `variant_annotation` pipeline instead) |

> **How to read the "parameters" in each step.** Two kinds of knob feed these tools, and this protocol
> keeps them separate on purpose:
> - **🔬 Agent-chosen (scientific)** — the PI/Scientist decides these per study and you should audit them:
>   the QC filter thresholds (Step 1), the **Leiden resolution** (Step 2), the DE `groupby` (Step 3), the
>   top-marker set + gene-set libraries (Step 4), and — most important — the **marker → cell-type mapping**
>   and stated confidence (Step 5). The SKILL leaves the concrete values to the agent per dataset; where it
>   does, this protocol writes *(agent-chosen per study)* rather than inventing a number.
> - **⚙️ Fixed method / infra** — baked into the tool, not reinvented per run and not changing the science:
>   the `normalize_total → log1p → highly-variable-genes` recipe inside QC, the `PCA → neighbors → Leiden →
>   UMAP` order inside clustering, and the compute backend. Shown so you can see the method, not re-audited
>   per step.

---

## At a glance — the 6 steps

| # | Step | Tool / skill | 🔬 Key agent-chosen params | Output |
|---|------|--------------|----------------------------|--------|
| 1 | **QC the dataset** | `run_scanpy_qc` | QC filter thresholds *(agent-chosen)* | filtered + normalized AnnData, pre/post counts |
| 2 | **Cluster the cells** | `run_clustering` | **Leiden resolution** *(agent-chosen)* | clusters + UMAP embedding |
| 3 | **Markers per cluster** | `run_de` | `groupby` = cluster | per-cluster marker / rank-genes tables |
| 4 | **(Optional) Enrichment** | `run_enrichment` | top markers + gene-set libraries | pathway / marker enrichment |
| 5 | **Assign cell-type labels** | `run_code` (`annotate_clusters_by_markers.py`) | marker → cell-type mapping + confidence | a label per cluster |
| 6 | **Figures** | scanpy plotting / `run_code` | which canonical markers to plot | UMAP (cluster & cell type) + violin/dot |

> Plan these as **distinct agenda steps** — real cell-type annotation is not one call. Steps 5–6 read from
> the marker tables the earlier tools already wrote; ground the labels in those, don't re-derive markers by
> hand. **The methods + results report is assembled automatically — do NOT add a report-writing step.**

---

## Step-by-step

<details open>
<summary><b>Step 1 · QC the dataset</b> — is this matrix clean enough to cluster?</summary>

**What.** Compute per-cell QC metrics, filter out low-quality cells and genes, then
`normalize_total → log1p → highly-variable genes`. **Report pre/post cell & gene counts.**

**Why.** Clustering and marker detection inherit whatever noise survives QC — empty droplets, dying
(high-mito) cells, and lowly-detected genes create spurious clusters and spurious "markers". Filtering and
normalizing first is what makes the downstream labels trustworthy; the pre/post counts are the audit trail.

**🔬 Agent-chosen:** the QC filter thresholds (which cells/genes to drop) — *(agent-chosen per study)*; the
SKILL does not pin values, so adapt them to this dataset's depth and mito profile.

**What runs** — the `run_scanpy_qc` tool call:
```
run_scanpy_qc(dataset, <QC filter thresholds — agent-chosen per study>)
#   → per-cell QC metrics
#   → filter low-quality cells + genes
#   → normalize_total → log1p → highly-variable genes     (fixed recipe)
#   → report pre/post cell & gene counts
```

**✅ Verify this step:** pre/post cell **and** gene counts are both reported · the drop looks sane (not
near-total, not near-zero) · normalization + HVG ran before any clustering.

<sub>Source: `run_scanpy_qc` · `SKILL.md` step 1</sub>
</details>

<details>
<summary><b>Step 2 · Cluster the cells</b> — group cells so each cluster can get one label</summary>

**What.** `PCA → neighbors → Leiden → UMAP` at a **sensible resolution**, producing the clusters that Steps
3–5 will label and the UMAP that Step 6 will color.

**Why.** Cell-type assignment here is **per cluster**, so the resolution directly sets granularity: too low
merges distinct types into one label, too high shatters one type across clusters. This is the knob to audit.

**🔬 Agent-chosen:** the **Leiden `resolution`** — *(agent-chosen per study)*; pick a sensible value for this
dataset and be ready to justify it.

**What runs** — the `run_clustering` tool call:
```
run_clustering(<Leiden resolution — agent-chosen per study>)
#   PCA → neighbors → Leiden → UMAP        (fixed order)
```

**✅ Verify this step:** the chosen resolution is stated · the cluster count is biologically plausible for
the tissue · the UMAP shows separated (not one smeared blob) structure.

<sub>Source: `run_clustering` · `SKILL.md` step 2</sub>
</details>

<details>
<summary><b>Step 3 · Find marker genes per cluster</b> — the evidence every label rests on</summary>

**What.** Differential expression / `rank_genes` **per cluster** — the ranked genes that distinguish each
cluster from the rest. This table is the ground truth for Step 5's labels.

**Why.** A cell-type call is only as good as the markers behind it. Computing per-cluster markers explicitly
(rather than eyeballing a UMAP) is what lets the label be defended — and lets a reviewer check it.

**🔬 Agent-chosen:** `groupby` = the cluster labels from Step 2.

**What runs** — the `run_de` tool call:
```
run_de(groupby = cluster)
#   differential expression / rank_genes per cluster
```

**✅ Verify this step:** every cluster gets a marker list · the top markers are recognisable genes (not all
mito/ribo artefacts) · the table is what Step 5 actually reads.

<sub>Source: `run_de` · `SKILL.md` step 3</sub>
</details>

<details>
<summary><b>Step 4 · (Optional) Pathway / marker enrichment</b> — extra support for the interpretation</summary>

**What.** Run enrichment on the **top markers** of each cluster to support (not replace) the label call.

**Why.** When the top markers alone are ambiguous, pathway/marker-set enrichment can corroborate a
cell-type or lineage. It is **optional** — a confirmatory aid, not a required step, and never a substitute
for reading the markers themselves.

**🔬 Agent-chosen:** the top-marker set fed in and the gene-set libraries used — *(agent-chosen per study)*.

**What runs** — the `run_enrichment` tool call:
```
run_enrichment(top markers per cluster, <gene-set libraries — agent-chosen per study>)
```

**✅ Verify this step:** enrichment is used to *corroborate* a label, not to overrule the actual markers ·
if skipped, that is fine and should simply not be claimed.

<sub>Source: `run_enrichment` · `SKILL.md` step 4</sub>
</details>

<details>
<summary><b>Step 5 · Assign a cell-type label to each cluster</b> — the interpretation, stated honestly</summary>

**What.** Read each cluster's top markers and assign a cell-type label, **stating confidence and
uncertainty honestly**. If no curated tool covers label assignment, adapt the reference template
`annotate_clusters_by_markers.py` via `run_code`.

**Why / honesty.** This is the interpretive core. Ground every label in the marker genes the tools actually
returned — do **not** fabricate cell types, gene names, or numbers a tool did not return. Where the markers
are ambiguous, say so (e.g. an "unknown" or low-confidence label) rather than forcing a confident call.

**🔬 Agent-chosen:** the **marker → cell-type mapping** (which canonical markers imply which type) and the
**confidence** attached to each cluster — *(agent-chosen per study)*.

**What runs** — interpretation, then (if no curated label tool exists) `run_code`:
```
# preferred: a curated label-assignment tool, if one covers this
# fallback:  run_code, adapting the reference template annotate_clusters_by_markers.py
#            → map each cluster's top markers → a cell-type label + a confidence
```

**✅ Verify this step:** every label cites the markers that justify it · confidence/uncertainty is stated
per cluster · ambiguous clusters are labelled honestly (not force-fit) · no invented cell types or genes.

<sub>Source: `run_code` + `annotate_clusters_by_markers.py` · `SKILL.md` step 5</sub>
</details>

<details>
<summary><b>Step 6 · Produce the figures</b> — show the clusters and the labels</summary>

**What.** A **UMAP colored by cluster** and **by assigned cell type**, plus **violin / dot plots** of the
canonical marker genes.

**Why.** The figures are how a reviewer sanity-checks the labels: the UMAP shows whether a labelled type is
a coherent region, and the marker violin/dot plots show whether the genes that named a cluster are actually
enriched there.

**🔬 Agent-chosen:** which **canonical marker genes** to plot — *(agent-chosen per study)*, drawn from the
Step-3 markers that drove each label.

**What runs** — the SKILL lists the figures but does not pin a dedicated plotting tool; the UMAP typically
comes from the clustering step's embedding and the marker violin/dot plots from the standard scanpy plotting
(via `run_code` when needed):
```
# UMAP colored by cluster
# UMAP colored by assigned cell type
# violin / dot plots of the canonical marker genes
```

**✅ Verify this step:** both UMAP colorings are present · the marker plots use genes that actually justify
the labels · the figures match the labels in Step 5 (no drift).

<sub>Source: `SKILL.md` step 6</sub>
</details>

---

## Grounding & honesty (applies to every step)

Ground every label in the marker genes the tools **actually returned**. Do **not** fabricate cell types,
gene names, or numbers a tool did not return. State confidence and uncertainty honestly — an ambiguous
cluster gets an honest low-confidence or "unknown" label, not a forced call. If a step's tool errors,
**report the error honestly rather than inventing a result**. Report pre/post QC counts and the clustering
resolution so the run is reproducible. Frame cell-type calls as marker-based interpretations, not ground
truth.

## Method provenance

scanpy — per-cell QC + `normalize_total`/`log1p`/HVG · `PCA → neighbors → Leiden → UMAP` · `rank_genes`
per-cluster differential expression · (optional) marker/pathway enrichment · optional `run_code` adaptation
of the `annotate_clusters_by_markers.py` reference template for label assignment. Tools: `run_scanpy_qc`,
`run_clustering`, `run_de`, `run_enrichment`, `run_code` (see this pipeline's `SKILL.md`). The methods +
results report is assembled automatically — do not plan a report-writing step.

<sub>This protocol renders the `celltype_annotation` SKILL.md; where the SKILL leaves a knob to the agent it
is marked *(agent-chosen per study)* rather than given a fabricated value.</sub>
