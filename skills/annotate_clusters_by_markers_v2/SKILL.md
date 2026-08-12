---
name: annotate_clusters_by_markers_v2
supersedes: annotate_clusters_by_markers
description: >-
  Assign a cell-type label to each cluster from a curated marker panel, using signature
  scoring for a first pass and RAW marker expression to make the final call. Use for
  marker-based (reference-free) annotation instead of reference label transfer. Encodes the
  shared-marker rule: a z-scored score argmax will confidently mislabel lineages that share
  markers, so the label is only assigned when the lineage's own defining markers are the
  dominant raw signal — and clusters with no coherent signal stay Unassigned.
---

# Marker-based cell-type annotation (v2)

## What changed from v1, and why

v1 counted how many of a cluster's top-25 DE genes appeared in a marker list and took the
argmax. Two things are wrong with that, and both produce confident wrong labels rather than
visible failures:

- **A set intersection throws away expression.** A gene either is or isn't in the top 25, so
  a lineage with 3 weakly-detected markers beats one with 2 strongly-expressed ones.
- **It has no way to handle shared markers.** `LAMP3` is in both the AT2 and dendritic-cell
  panels; `PDPN` is in both AT1 and lymphatic endothelium. Whichever list happens to overlap
  by one more gene wins, and nothing in the procedure can notice the ambiguity.

v2 replaces the counter with `sc.tl.score_genes` (a background-corrected mean over the whole
panel), then applies the rule that actually settles these calls.

## The load-bearing rule

**Signature scores alone will mislabel shared-marker lineages.** Per-cell-type z-scoring
inflates weak and ambient signal into confident-looking maxima, so the z-scored argmax is a
*first pass only*. The final label comes from raw log-normalized expression of a few
lineage-specific discriminators, and is assigned only when those are the dominant raw signal.

Two consequences that are part of the rule, not optional refinements:

- A cluster whose first-pass call and raw-expression check disagree is **reported as a
  correction**, with both calls kept in the output. That disagreement is evidence about the
  data, and silently keeping only the winner destroys it.
- A cluster with no coherent dominant signal is **`Unassigned`** — likely low-quality or a
  doublet. Do not force a label onto it.

## When to use

Marker-based / manual annotation, after `run_de` has produced `work/adata_de.h5ad`. If the
question is reference label transfer against an annotated atlas, use scGPT instead.

**Annotate per tissue, not across tissues.** Tissue of origin is the dominant axis of
variation, so one global clustering of a multi-tissue object separates by tissue and washes
out intra-tissue subtypes — and each tissue needs its own panel. Split first, run per tissue.

## Choosing the clustering resolution first

Labels inherit whatever partition they are given, so a resolution chosen by default is an
unexamined assumption in every downstream label. Call `run_clustering` with
`select_resolution: true` — it sweeps candidate resolutions, measures bootstrap stability
(mean ARI over resampled re-clusterings), and takes the finest resolution that still
reproduces, writing `tables/resolution_sweep.csv`. Then check the winner against biology: it
should separate the panel's major lineages. The tool reports `resolution_source`; quote it.

## Run

**Prefer the tool: `run_marker_annotation`.** It implements exactly the procedure below, so
the panel is the only thing you have to get right, and the result reports which clusters the
raw check corrected and which stayed `Unassigned`. Pass `panel` and `discriminators`.

Use the template only when you need to change the PROCEDURE (a different first pass, an extra
confirmation step). Fetch it with
`read_skill_reference("annotate_clusters_by_markers_v2", file="reference.py")`, **replace
`PANEL` and `DISCRIMINATORS` with your tissue's markers** (the bundled ones are human retina),
then execute via `run_code`. Reads checkpoints from `BIOAGENT_WORK`, writes under
`BIOAGENT_ARTIFACTS`.

The labels are only as good as the panel. Search the literature for the canonical markers of
the tissue in hand rather than reusing a panel from another tissue, and say in the write-up
which panel was used and where it came from.

## Building the discriminator table

For every lineage in `PANEL`, `DISCRIMINATORS` needs the 2–4 genes that are specific enough
to settle a contested cluster — not the whole panel. The pattern that makes one useful is
"gene X is in two panels, gene Y is in only one": drop X, keep Y.

| Lineage | Confirm with (dominant raw signal) | False positive it overrides |
|---|---|---|
| Rod photoreceptor | RHO / PDE6A / NRL | cone (shared phototransduction genes) |
| Cone photoreceptor | ARR3 / PDE6H / GNAT2 | rod |
| Müller glia | RLBP1 / SLC1A3 / GLUL | astrocyte (shared SLC1A3) |
| Retinal astrocyte | GFAP / PAX2 | Müller glia |
| Microglia | C1QA / P2RY12 / CX3CR1 | infiltrating macrophage |
| Vascular endothelium | PECAM1 / VWF / CLDN5 | — |
| Lymphatic endothelium | PROX1 / CCL21 (+PECAM1) | vascular endothelium |

Replace the whole table for a different tissue. The shape is what transfers, not the genes.

## Output

- `tables/cluster_cell_types.json` — per cluster: final label, first-pass label, whether the
  raw check corrected it, the discriminator means it was decided on, and a confidence grade.
- `tables/celltype_scores_by_cluster.csv` — the raw (un-z-scored) signature score matrix.
- `tables/celltype_composition.csv` — counts and percentages per label.
- `work/adata_annotated.h5ad` — `obs["cell_type"]`, `obs["cluster_note"]`.

## Reporting

State the panel and its source, the resolution and how it was chosen, every cluster the raw
check corrected, and every cluster left `Unassigned` with its size. An annotation whose
corrections and unassigned clusters are not stated cannot be reviewed.
