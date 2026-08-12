"""Reference template — condition-vs-control differential expression, STRATIFIED BY CELL TYPE.

The pattern behind a KO-vs-WT (or disease-vs-control) report: for each cell type, compare the
condition group against the reference group and collect the changed genes, then look for a shared
cross-cell-type signature. The curated `run_de` tool does per-cluster one-vs-rest DE; this template
covers the stratified condition-vs-reference comparison via scanpy `rank_genes_groups` with an
explicit `reference`, looped over an existing cell-type label column.

ADAPT the five CONFIG values to columns/levels that exist in adata.obs (the DATASET PROFILE in your
planning brief lists them). Everything else is generic. Writes per-cell-type DE tables, a summary
table, shared up/down gene lists, and a volcano per cell type; prints a JSON summary for the report.
"""
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import scanpy as sc
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ----- CONFIG: adapt to THIS dataset (see the DATASET PROFILE in your brief) -----------------
CONDITION_KEY = "sampleid"     # obs column naming the experimental groups
CONDITION = "DDX41"            # the group of interest (case)
REFERENCE = "WT"              # the baseline / control group
CELLTYPE_KEY = "majorclass"    # existing cell-type label column to stratify by (REUSE it)
MIN_CELLS = 30                 # skip a cell type with fewer than this in EITHER group
LFC = 1.0                      # |log2FC| threshold for "significant"
PADJ = 0.05                    # adjusted-p threshold
# --------------------------------------------------------------------------------------------

work = Path(os.environ["BIOAGENT_WORK"])
art = Path(os.environ["BIOAGENT_ARTIFACTS"])
tdir = art / "tables" / "DEG"
fdir = art / "figures" / "DEG"
tdir.mkdir(parents=True, exist_ok=True)
fdir.mkdir(parents=True, exist_ok=True)

# Prefer the QC'd checkpoint (it preserves the original obs labels); fall back to the raw dataset.
ckpt = work / "adata_qc.h5ad"
adata = sc.read_h5ad(ckpt if ckpt.exists() else os.environ["BIOAGENT_DATASET"])

for col in (CONDITION_KEY, CELLTYPE_KEY):
    if col not in adata.obs:
        raise SystemExit(f"obs has no column {col!r}; available: {list(adata.obs.columns)}")
groups = set(adata.obs[CONDITION_KEY].astype(str))
if not {CONDITION, REFERENCE} <= groups:
    raise SystemExit(f"{CONDITION!r}/{REFERENCE!r} not in {CONDITION_KEY} values {sorted(groups)}")

summary_rows, up_by_ct, down_by_ct = [], {}, {}

for ct in [str(c) for c in adata.obs[CELLTYPE_KEY].cat.categories] if hasattr(
        adata.obs[CELLTYPE_KEY], "cat") else sorted(set(adata.obs[CELLTYPE_KEY].astype(str))):
    cells = adata[adata.obs[CELLTYPE_KEY].astype(str) == ct]
    n_cond = int((cells.obs[CONDITION_KEY].astype(str) == CONDITION).sum())
    n_ref = int((cells.obs[CONDITION_KEY].astype(str) == REFERENCE).sum())
    if n_cond < MIN_CELLS or n_ref < MIN_CELLS:
        summary_rows.append({"celltype": ct, "n_condition": n_cond, "n_reference": n_ref,
                             "n_DEG": 0, "n_up": 0, "n_down": 0, "skipped": "too_few_cells"})
        continue

    sub = cells[cells.obs[CONDITION_KEY].astype(str).isin([CONDITION, REFERENCE])].copy()
    sub.obs["_grp"] = sub.obs[CONDITION_KEY].astype(str).astype("category")
    sc.tl.rank_genes_groups(sub, "_grp", groups=[CONDITION], reference=REFERENCE, method="wilcoxon")
    res = sc.get.rank_genes_groups_df(sub, group=CONDITION).sort_values("pvals_adj")
    res.to_csv(tdir / f"DEG_{ct}.csv", index=False)

    sig = res[(res["pvals_adj"] < PADJ) & (res["logfoldchanges"].abs() > LFC)]
    up = sig[sig["logfoldchanges"] > 0]["names"].tolist()
    down = sig[sig["logfoldchanges"] < 0]["names"].tolist()
    up_by_ct[ct], down_by_ct[ct] = set(up), set(down)
    summary_rows.append({"celltype": ct, "n_condition": n_cond, "n_reference": n_ref,
                         "n_DEG": len(sig), "n_up": len(up), "n_down": len(down), "skipped": ""})

    # Volcano: log2FC vs -log10(padj), significant genes highlighted.
    x = res["logfoldchanges"].to_numpy()
    y = -np.log10(res["pvals_adj"].clip(lower=1e-300).to_numpy())
    keep = (res["pvals_adj"].to_numpy() < PADJ) & (np.abs(x) > LFC)
    plt.figure(figsize=(5, 4))
    plt.scatter(x[~keep], y[~keep], s=4, c="lightgray")
    plt.scatter(x[keep], y[keep], s=6, c="firebrick")
    plt.axvline(LFC, ls="--", lw=0.6, c="gray"); plt.axvline(-LFC, ls="--", lw=0.6, c="gray")
    plt.xlabel(f"log2FC ({CONDITION} vs {REFERENCE})"); plt.ylabel("-log10(adj p)")
    plt.title(f"{ct}: {CONDITION} vs {REFERENCE}"); plt.tight_layout()
    plt.savefig(fdir / f"volcano_{ct}.png", dpi=150); plt.close()

pd.DataFrame(summary_rows).to_csv(art / "tables" / "DEG_summary.csv", index=False)

# Shared signature: genes changed in the SAME direction in >= 2 cell types.
def _shared(d):
    counts = {}
    for s in d.values():
        for g in s:
            counts[g] = counts.get(g, 0) + 1
    return sorted([g for g, n in counts.items() if n >= 2], key=lambda g: -counts[g])

shared_up, shared_down = _shared(up_by_ct), _shared(down_by_ct)
pd.DataFrame({"gene": shared_up}).to_csv(art / "tables" / "shared_up_DEGs.csv", index=False)
pd.DataFrame({"gene": shared_down}).to_csv(art / "tables" / "shared_down_DEGs.csv", index=False)

print(json.dumps({
    "comparison": f"{CONDITION_KEY}: {CONDITION} vs {REFERENCE} (reference={REFERENCE})",
    "stratified_by": CELLTYPE_KEY,
    "per_celltype": summary_rows,
    "n_shared_up": len(shared_up), "n_shared_down": len(shared_down),
    "shared_up_top": shared_up[:20], "shared_down_top": shared_down[:20],
    "next": "run_enrichment on each cell type's up/down gene SYMBOLS",
}, indent=2))
