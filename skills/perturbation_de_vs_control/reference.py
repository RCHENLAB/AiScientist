"""Reference template — per-perturbation differential expression vs a shared non-targeting control.

The core of a Perturb-seq report: for EACH perturbation, compare its cells against the ONE
non-targeting control group and collect the changed genes. Unlike a 2-group condition study, here
many perturbations share a single reference. The curated `run_de` tool does per-cluster one-vs-rest
DE; this template covers the perturbation-vs-shared-control comparison via scanpy `rank_genes_groups`
with an explicit `reference`, looped over the perturbation label column.

ADAPT the CONFIG values to columns/levels that exist in adata.obs (the DATASET PROFILE in your
planning brief lists them). Set ONLY_PERTURBATIONS to the strong hits from the E-distance step to
skip silent guides. Everything else is generic. Writes a per-perturbation DE table + a summary table
(with a target-self-knockdown positive-control check) and prints a JSON summary for the report.
"""
import json
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd
import scanpy as sc

# ----- CONFIG: adapt to THIS dataset (see the DATASET PROFILE in your brief) -----------------
PERT_KEY = "perturbation"      # obs column naming the guide / perturbation per cell
CONTROL = "NT"                # the shared non-targeting control level in PERT_KEY
ONLY_PERTURBATIONS = None      # None = every non-control level; or a list of hits from E-distance
COLLAPSE_GUIDE_TO_TARGET = False  # True: map "TP53_sg1"->"TP53" (strip a trailing _sg\d+/-\d+) for power
MIN_CELLS = 30                 # skip a perturbation with fewer than this many cells (or control < this)
LFC = 1.0                      # |log2FC| threshold for "significant"
PADJ = 0.05                    # adjusted-p threshold
# --------------------------------------------------------------------------------------------

work = Path(os.environ["BIOAGENT_WORK"])
art = Path(os.environ["BIOAGENT_ARTIFACTS"])
tdir = art / "tables" / "perturbation_DE"
tdir.mkdir(parents=True, exist_ok=True)

# Prefer the QC'd checkpoint (preserves the original obs labels); fall back to the raw dataset.
ckpt = work / "adata_qc.h5ad"
adata = sc.read_h5ad(ckpt if ckpt.exists() else os.environ["BIOAGENT_DATASET"])

if PERT_KEY not in adata.obs:
    raise SystemExit(f"obs has no column {PERT_KEY!r}; available: {list(adata.obs.columns)}")

labels = adata.obs[PERT_KEY].astype(str)
if COLLAPSE_GUIDE_TO_TARGET:
    # "TP53_sg1" / "TP53-2" -> "TP53"; leave the control level untouched.
    def _target(g):
        return g if g == CONTROL else re.sub(r"[_-](sg)?\d+$", "", g)
    labels = labels.map(_target)
adata.obs["_pert"] = labels.astype("category")

levels = set(adata.obs["_pert"].astype(str))
if CONTROL not in levels:
    raise SystemExit(f"control {CONTROL!r} not in {PERT_KEY} values {sorted(levels)}")
perts = sorted(levels - {CONTROL})
if ONLY_PERTURBATIONS is not None:
    perts = [p for p in perts if p in set(ONLY_PERTURBATIONS)]

n_ctrl = int((adata.obs["_pert"].astype(str) == CONTROL).sum())
if n_ctrl < MIN_CELLS:
    raise SystemExit(f"control {CONTROL!r} has only {n_ctrl} cells (< MIN_CELLS={MIN_CELLS})")

var_names = set(adata.var_names)
summary_rows, up_by_pert, down_by_pert = [], {}, {}

for pert in perts:
    n_pert = int((adata.obs["_pert"].astype(str) == pert).sum())
    if n_pert < MIN_CELLS:
        summary_rows.append({"perturbation": pert, "n_cells": n_pert, "n_control": n_ctrl,
                             "n_DEG": 0, "n_up": 0, "n_down": 0,
                             "target_self_knockdown": "", "skipped": "too_few_cells"})
        continue

    mask = adata.obs["_pert"].astype(str).isin([pert, CONTROL]).to_numpy()
    sub = adata[mask].copy()   # ONE small subset at a time (two groups); freed each iteration
    sub.obs["_grp"] = sub.obs["_pert"].astype(str).astype("category")
    sc.tl.rank_genes_groups(sub, "_grp", groups=[pert], reference=CONTROL, method="wilcoxon")
    res = sc.get.rank_genes_groups_df(sub, group=pert).sort_values("pvals_adj")
    res.to_csv(tdir / f"DE_{pert}.csv", index=False)

    sig = res[(res["pvals_adj"] < PADJ) & (res["logfoldchanges"].abs() > LFC)]
    up = sig[sig["logfoldchanges"] > 0]["names"].tolist()
    down = sig[sig["logfoldchanges"] < 0]["names"].tolist()
    up_by_pert[pert], down_by_pert[pert] = set(up), set(down)

    # Positive control: a real knockout should show its OWN target gene DOWN. Only meaningful when the
    # perturbation label IS a gene symbol present in var (target-level or single-gene guides).
    self_kd = "n/a"
    if pert in var_names:
        hit = res[res["names"] == pert]
        if len(hit):
            lfc_self = float(hit["logfoldchanges"].iloc[0])
            padj_self = float(hit["pvals_adj"].iloc[0])
            self_kd = "down" if (lfc_self < 0 and padj_self < PADJ) else f"NOT_down(lfc={lfc_self:.2f})"

    summary_rows.append({"perturbation": pert, "n_cells": n_pert, "n_control": n_ctrl,
                         "n_DEG": len(sig), "n_up": len(up), "n_down": len(down),
                         "target_self_knockdown": self_kd, "skipped": ""})
    del sub

summary = pd.DataFrame(summary_rows).sort_values("n_DEG", ascending=False)
summary.to_csv(art / "tables" / "perturbation_DE_summary.csv", index=False)

# Convergent programs: genes changed in the SAME direction across >= 2 perturbations.
def _shared(d):
    counts = {}
    for s in d.values():
        for g in s:
            counts[g] = counts.get(g, 0) + 1
    return sorted([g for g, n in counts.items() if n >= 2], key=lambda g: -counts[g])

shared_up, shared_down = _shared(up_by_pert), _shared(down_by_pert)
pd.DataFrame({"gene": shared_up}).to_csv(art / "tables" / "shared_up_across_perturbations.csv", index=False)
pd.DataFrame({"gene": shared_down}).to_csv(art / "tables" / "shared_down_across_perturbations.csv", index=False)

ran = [r for r in summary_rows if not r["skipped"]]
failed_pc = [r["perturbation"] for r in ran if str(r["target_self_knockdown"]).startswith("NOT_down")]
print(json.dumps({
    "control": CONTROL,
    "perturbation_key": PERT_KEY,
    "level": "target" if COLLAPSE_GUIDE_TO_TARGET else "guide",
    "n_perturbations_tested": len(ran),
    "n_skipped_low_cells": sum(1 for r in summary_rows if r["skipped"]),
    "top_by_n_DEG": summary.head(15)[["perturbation", "n_cells", "n_DEG", "n_up", "n_down",
                                      "target_self_knockdown"]].to_dict("records"),
    "failed_self_knockdown_positive_control": failed_pc,
    "n_shared_up": len(shared_up), "n_shared_down": len(shared_down),
    "shared_up_top": shared_up[:20], "shared_down_top": shared_down[:20],
    "next": "run_enrichment on each strong perturbation's up/down gene SYMBOLS",
}, indent=2))
