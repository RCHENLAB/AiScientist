"""Reference template — pairwise differential expression between two named groups.

The curated `run_de` tool does per-cluster one-vs-rest DE; this template covers an arbitrary
A-vs-B comparison (e.g. disease vs control) via scanpy `rank_genes_groups` with an explicit
`reference`. ADAPT GROUP_KEY / GROUP_A / GROUP_B to a column that exists in adata.obs.
"""
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import scanpy as sc

work = Path(os.environ["BIOAGENT_WORK"])
art = Path(os.environ["BIOAGENT_ARTIFACTS"])
(art / "tables").mkdir(parents=True, exist_ok=True)

# Prefer the clustered checkpoint if present, else the QC'd one.
ckpt = work / "adata_clustered.h5ad"
if not ckpt.exists():
    ckpt = work / "adata_qc.h5ad"
adata = sc.read_h5ad(ckpt)

# ADAPT: the obs column holding the two groups, and the two group values to compare.
GROUP_KEY = "leiden"          # e.g. "condition", "sample", "leiden"
GROUP_A = "0"                 # the group of interest
GROUP_B = "1"                 # the reference group
if GROUP_KEY not in adata.obs:
    raise SystemExit(f"obs has no column {GROUP_KEY!r}; available: {list(adata.obs.columns)}")

sub = adata[adata.obs[GROUP_KEY].astype(str).isin([GROUP_A, GROUP_B])].copy()
sub.obs[GROUP_KEY] = sub.obs[GROUP_KEY].astype(str).astype("category")
sc.tl.rank_genes_groups(sub, GROUP_KEY, groups=[GROUP_A], reference=GROUP_B, method="wilcoxon")

res = sc.get.rank_genes_groups_df(sub, group=GROUP_A)        # names, logfoldchanges, pvals_adj, ...
res = res.sort_values("pvals_adj")
table = art / "tables" / f"de_{GROUP_A}_vs_{GROUP_B}.csv"
res.to_csv(table, index=False)

sig = res[(res["pvals_adj"] < 0.05) & (res["logfoldchanges"].abs() > 1.0)]
summary = {
    "comparison": f"{GROUP_KEY}: {GROUP_A} vs {GROUP_B}",
    "n_cells": {GROUP_A: int((sub.obs[GROUP_KEY] == GROUP_A).sum()),
                GROUP_B: int((sub.obs[GROUP_KEY] == GROUP_B).sum())},
    "n_significant": int(len(sig)),
    "top_up": res.nlargest(10, "logfoldchanges")["names"].tolist(),
    "top_down": res.nsmallest(10, "logfoldchanges")["names"].tolist(),
    "table": str(table),
}
print(json.dumps(summary, indent=2))
