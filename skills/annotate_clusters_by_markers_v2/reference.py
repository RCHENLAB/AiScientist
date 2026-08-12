"""Reference template — marker-based cell-type annotation, v2.

Supersedes the v1 top-25 set-intersection counter. Three steps:

  1. score every cluster against a curated panel (`sc.tl.score_genes`, background-corrected);
  2. z-score per cell type across clusters and take the argmax -- a FIRST PASS only;
  3. decide the label on RAW log-normalized expression of a few lineage discriminators.

Step 3 is the point. Per-column z-scoring inflates weak and ambient signal into
confident-looking maxima, so lineages that share markers (RHO/PDE6A across rod and cone,
SLC1A3 across Muller glia and astrocyte) get mislabeled by the argmax alone. A cluster is
labeled only when that lineage's own discriminators are the dominant raw signal; when the
raw check disagrees with the first pass, BOTH are kept in the output so the correction is
visible; and a cluster with no coherent dominant signal stays "Unassigned" rather than being
forced into the nearest label.

ADAPT `PANEL` and `DISCRIMINATORS` to YOUR tissue before running (bundled: human retina).
"""
import json
import os
from pathlib import Path

import numpy as np
import scanpy as sc

# --- CONFIG: adapt to this dataset ------------------------------------------------
CLUSTER_KEY = "leiden"
# A cluster is labeled only if its winning discriminator mean clears this, and beats the
# runner-up lineage by RATIO. Both are deliberately visible knobs: on a shallow snRNA-seq
# object the absolute floor has to come down, and loosening it is a decision worth stating.
MIN_DISCRIMINATOR_MEAN = 0.20
DOMINANCE_RATIO = 1.5

# Keys become the candidate labels. Full panels -- used for signature scoring.
PANEL = {
    "Rod photoreceptor":    ["RHO", "PDE6A", "PDE6B", "NRL", "NR2E3", "GNAT1", "CNGA1"],
    "Cone photoreceptor":   ["ARR3", "PDE6H", "GNAT2", "OPN1SW", "OPN1MW", "PDE6C"],
    "Bipolar cell":         ["VSX2", "GRM6", "TRPM1", "PRKCA", "GRIK1"],
    "Amacrine cell":        ["GAD1", "GAD2", "SLC6A9", "TFAP2B", "CHAT"],
    "Horizontal cell":      ["ONECUT1", "LHX1", "CALB1", "SEPT4"],
    "Retinal ganglion cell": ["RBPMS", "NEFL", "SNCG", "POU4F1", "THY1"],
    "Muller glia":          ["RLBP1", "SLC1A3", "GLUL", "CLU", "APOE", "SOX9"],
    "Retinal astrocyte":    ["GFAP", "PAX2", "S100B"],
    "Microglia":            ["C1QA", "C1QB", "P2RY12", "CX3CR1", "AIF1"],
    "Vascular endothelium": ["PECAM1", "VWF", "CLDN5", "CDH5"],
    "Lymphatic endothelium": ["PROX1", "CCL21", "MMRN1", "FLT4"],
    "Pericyte/SMC":         ["ACTA2", "PDGFRB", "NOTCH3", "MYH11", "TAGLN"],
    "RPE":                  ["RPE65", "BEST1", "TTR", "TYR"],
}

# The 2-4 genes per lineage that actually settle a contested cluster -- SPECIFIC genes only,
# not the whole panel. The rule for building this: if a gene appears in two panels, it cannot
# discriminate between them, so drop it here even though it stays in PANEL for scoring.
DISCRIMINATORS = {
    "Rod photoreceptor":    ["RHO", "NRL", "GNAT1"],
    "Cone photoreceptor":   ["ARR3", "PDE6H", "GNAT2"],
    "Bipolar cell":         ["VSX2", "GRM6", "TRPM1"],
    "Amacrine cell":        ["GAD1", "SLC6A9", "TFAP2B"],
    "Horizontal cell":      ["ONECUT1", "LHX1"],
    "Retinal ganglion cell": ["RBPMS", "SNCG", "POU4F1"],
    "Muller glia":          ["RLBP1", "GLUL"],
    "Retinal astrocyte":    ["GFAP", "PAX2"],
    "Microglia":            ["C1QA", "P2RY12", "CX3CR1"],
    "Vascular endothelium": ["PECAM1", "VWF", "CLDN5"],
    "Lymphatic endothelium": ["PROX1", "CCL21"],
    "Pericyte/SMC":         ["ACTA2", "PDGFRB", "NOTCH3"],
    "RPE":                  ["RPE65", "BEST1"],
}
# ----------------------------------------------------------------------------------

work = Path(os.environ["BIOAGENT_WORK"])
out = Path(os.environ["BIOAGENT_ARTIFACTS"])
(out / "tables").mkdir(parents=True, exist_ok=True)

adata = sc.read_h5ad(work / "adata_de.h5ad")     # obs[CLUSTER_KEY] + rank_genes_groups
if CLUSTER_KEY not in adata.obs:
    raise SystemExit(f"{CLUSTER_KEY!r} not in obs; available: {list(adata.obs.columns)}")

# .raw holds the full log-norm matrix (QC set it before HVG subsetting), so scoring and the
# raw check see every gene -- not just the HVGs clustering was run on.
present = set(adata.raw.var_names if adata.raw is not None else adata.var_names)
use_raw = adata.raw is not None
panel = {ct: [g for g in gs if g in present] for ct, gs in PANEL.items()}
dropped = {ct: sorted(set(gs) - present) for ct, gs in PANEL.items() if set(gs) - present}
panel = {ct: gs for ct, gs in panel.items() if gs}     # a lineage with no detected marker
missing_lineages = sorted(set(PANEL) - set(panel))     # cannot be called; say so, don't guess

# --- step 1: signature scores per cell, averaged per cluster ----------------------
for ct, gs in panel.items():
    sc.tl.score_genes(adata, gs, score_name=f"score_{ct}", use_raw=use_raw)
cols = [f"score_{ct}" for ct in panel]
scores = adata.obs.groupby(CLUSTER_KEY, observed=True)[cols].mean()
scores.columns = [c[len("score_"):] for c in scores.columns]
scores.to_csv(out / "tables" / "celltype_scores_by_cluster.csv")

# --- step 2: z-score per cell type -> FIRST-PASS argmax (not the answer) ----------
z = (scores - scores.mean()) / scores.std().replace(0, np.nan)
first_pass = z.idxmax(axis=1)

# --- step 3: decide on RAW discriminator expression -------------------------------
disc_genes = sorted({g for gs in DISCRIMINATORS.values() for g in gs if g in present})
raw_df = sc.get.obs_df(adata, keys=[*disc_genes, CLUSTER_KEY], use_raw=use_raw)
raw_means = raw_df.groupby(CLUSTER_KEY, observed=True)[disc_genes].mean()

labels: dict[str, dict] = {}
for cl in scores.index:
    cl = str(cl)
    # Mean raw expression of each lineage's OWN discriminators in this cluster.
    per_lineage = {
        ct: float(raw_means.loc[cl, [g for g in gs if g in disc_genes]].mean())
        for ct, gs in DISCRIMINATORS.items()
        if ct in panel and any(g in disc_genes for g in gs)
    }
    ranked = sorted(per_lineage.items(), key=lambda kv: kv[1], reverse=True)
    best, best_val = ranked[0] if ranked else ("", 0.0)
    runner, runner_val = ranked[1] if len(ranked) > 1 else ("", 0.0)

    # Dominance, not just a maximum: a winner that barely edges out the runner-up is exactly
    # the shared-marker case this whole procedure exists to catch.
    dominant = best_val >= MIN_DISCRIMINATOR_MEAN and (
        runner_val <= 0 or best_val >= DOMINANCE_RATIO * runner_val)
    fp = str(first_pass.get(cl, "")) if cl in first_pass.index else ""

    if not dominant:
        final, confidence = "Unassigned", "none"
    else:
        final = best
        confidence = "high" if best_val >= 2 * MIN_DISCRIMINATOR_MEAN else "medium"

    labels[cl] = {
        "cell_type": final,
        "first_pass_label": fp,                       # what the z-argmax alone would have said
        "corrected_by_raw_check": bool(dominant and fp and fp != final),
        "n_cells": int((adata.obs[CLUSTER_KEY].astype(str) == cl).sum()),
        "confidence": confidence,
        "discriminator_mean": round(best_val, 4),
        "runner_up": runner,
        "runner_up_mean": round(runner_val, 4),
        "evidence": ", ".join(
            f"{g}={raw_means.loc[cl, g]:.2f}"
            for g in DISCRIMINATORS.get(final, [])[:3] if g in disc_genes
        ) or "no dominant lineage signal",
    }

adata.obs["cell_type"] = adata.obs[CLUSTER_KEY].map(
    lambda c: labels[str(c)]["cell_type"]).astype("category")
adata.obs["cluster_note"] = adata.obs[CLUSTER_KEY].map(lambda c: labels[str(c)]["evidence"])
# h5py rejects '/' in obs keys (score_Pericyte/SMC) -- sanitize before writing.
adata.obs.columns = [c.replace("/", "_") for c in adata.obs.columns]
adata.write(work / "adata_annotated.h5ad")

comp = adata.obs["cell_type"].value_counts()
comp_path = out / "tables" / "celltype_composition.csv"
comp.rename("n_cells").to_frame().assign(
    pct=(100 * comp / comp.sum()).round(1)).to_csv(comp_path)

table_path = out / "tables" / "cluster_cell_types.json"
summary = {
    "cluster_key": CLUSTER_KEY,
    "n_clusters": len(labels),
    "panel_lineages": sorted(panel),
    # Never silently: a lineage with no detected marker cannot be called at all, and a panel
    # gene missing from the object narrows what the labels could have been.
    "lineages_not_testable": missing_lineages,
    "panel_genes_absent_from_object": dropped,
    "corrected_by_raw_check": sorted(c for c, v in labels.items() if v["corrected_by_raw_check"]),
    "unassigned": sorted(c for c, v in labels.items() if v["cell_type"] == "Unassigned"),
    "thresholds": {"min_discriminator_mean": MIN_DISCRIMINATOR_MEAN,
                   "dominance_ratio": DOMINANCE_RATIO},
    "labels": labels,
}
table_path.write_text(json.dumps(summary, indent=2))
print(json.dumps({k: v for k, v in summary.items() if k != "labels"}, indent=2))
print(f"\ntables: {table_path}, {comp_path}")
