"""Reference template — score every cell for a gene signature and compare across groups.

No curated tool covers per-cell signature scoring. This template uses scanpy `sc.tl.score_genes`
and summarizes the score per cluster/condition. ADAPT SIGNATURE (gene SYMBOLS) and GROUP_KEY.
"""
import json
import os
from pathlib import Path

import scanpy as sc

work = Path(os.environ["BIOAGENT_WORK"])
art = Path(os.environ["BIOAGENT_ARTIFACTS"])
(art / "tables").mkdir(parents=True, exist_ok=True)
(art / "figures").mkdir(parents=True, exist_ok=True)

ckpt = work / "adata_clustered.h5ad"
if not ckpt.exists():
    ckpt = work / "adata_qc.h5ad"
adata = sc.read_h5ad(ckpt)

# ADAPT: the signature gene symbols, and the obs column to compare the score across.
SIGNATURE = ["IFNG", "STAT1", "GBP1", "CXCL10", "IRF1"]   # e.g. an interferon-response program
GROUP_KEY = "leiden" if "leiden" in adata.obs else None

present = [g for g in SIGNATURE if g in adata.var_names]
missing = [g for g in SIGNATURE if g not in adata.var_names]
if not present:
    raise SystemExit(f"None of the signature genes are in the data: {SIGNATURE}")

sc.tl.score_genes(adata, gene_list=present, score_name="signature_score")
adata.write(work / "adata_scored.h5ad")

import matplotlib
matplotlib.use("Agg")
if GROUP_KEY:
    sc.pl.violin(adata, "signature_score", groupby=GROUP_KEY, rotation=90, show=False,
                 save="_signature_score.png")  # scanpy writes under its figdir
    per_group = adata.obs.groupby(GROUP_KEY)["signature_score"].agg(["mean", "median", "count"])
    per_group.to_csv(art / "tables" / "signature_score_by_group.csv")
    by_group = {str(k): round(float(v), 4) for k, v in per_group["mean"].items()}
else:
    by_group = {}

summary = {
    "signature_genes_used": present,
    "signature_genes_missing": missing,
    "score_mean": round(float(adata.obs["signature_score"].mean()), 4),
    "score_by_group_mean": by_group,
}
(art / "tables" / "signature_summary.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
