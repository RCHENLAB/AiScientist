"""Reference template — score every cell for one or more gene signatures and compare across groups.

No curated tool covers per-cell signature scoring. This template uses scanpy `sc.tl.score_genes`:
per cell, the mean expression of the signature genes minus the mean of a random CONTROL set drawn
from genes of matched average expression (the genes are binned by mean expression; for each
signature gene `ctrl_size` controls are sampled from its bin). A positive score means the cell
expresses the signature above what its general expression level predicts.
ADAPT the SIGNATURES and GROUP_KEYS in the CONFIG block.

Three things this template guards, because each one silently produced a wrong score before:
  * the gene lookup uses the SAME matrix score_genes scores — `.raw` (all genes, log-normalised)
    when present. adata_clustered.h5ad keeps only the ~2,000 highly variable genes in X, so a
    lookup against X reported most signature genes as "missing" although they were in the data;
  * symbols are matched case-insensitively, so a signature written GFAP finds Gfap on a mouse
    matrix (every remap is reported). A different NAME is not fixed by case — the human-only or
    yeast name of a gene (PRP22 for Dhx8) stays missing, and is listed;
  * a signature whose coverage falls below MIN_COVERAGE is NOT scored: a score built from two of
    fifteen genes is a different signature with the original's name on it.
Score one coherent programme per signature. Genes expected to move in OPPOSITE directions (a
splicing programme and photoreceptor outer-segment genes) belong in SEPARATE signatures — averaged
together they cancel, and the score means nothing.
"""
import json
import os
from pathlib import Path

import numpy as np
import scanpy as sc

work = Path(os.environ["AISCIENTIST_WORK"])
art = Path(os.environ["AISCIENTIST_ARTIFACTS"])
(art / "tables").mkdir(parents=True, exist_ok=True)
(art / "figures").mkdir(parents=True, exist_ok=True)

ckpt = work / "adata_clustered.h5ad"
if not ckpt.exists():
    ckpt = work / "adata_qc.h5ad"
adata = sc.read_h5ad(ckpt)

# ---- CONFIG (ADAPT) --------------------------------------------------------------------------
# One entry per coherent programme: {name: [gene symbols]}. Case does not matter.
SIGNATURES = {
    "interferon_response": ["IFNG", "STAT1", "GBP1", "CXCL10", "IRF1"],
}
# The obs columns to summarise over, e.g. ["majorclass", "sampleid"] for cell type x condition.
GROUP_KEYS = [k for k in ("leiden",) if k in adata.obs]
MIN_COVERAGE = 0.5      # fraction of a signature's genes that must be found to score it
CTRL_SIZE = 50          # control genes per signature gene (scanpy default)
SEED = 0
# -----------------------------------------------------------------------------------------------

# The matrix score_genes will actually read: .raw when present (scanpy's default), else X.
use_raw = adata.raw is not None
pool = adata.raw.var_names if use_raw else adata.var_names
by_fold: dict[str, list[str]] = {}
for name in pool:
    by_fold.setdefault(str(name).casefold(), []).append(str(name))

# score_genes on raw COUNTS is a depth score, not a programme score: the deeper cell wins. The QC
# checkpoint's .raw is log-normalised; refuse anything that looks like integer counts.
_sample = (adata.raw.X if use_raw else adata.X)[: min(500, adata.n_obs)]
_vals = _sample.data if hasattr(_sample, "data") else np.asarray(_sample).ravel()
_vals = np.asarray(_vals[: 200_000], dtype=float)
if _vals.size and float(_vals.max()) > 50 and np.allclose(_vals, np.round(_vals)):
    raise SystemExit("The scoring matrix holds integer counts (max %.0f), not log-normalised values "
                     "— run run_scanpy_qc first and score its checkpoint." % float(_vals.max()))

missing_keys = [k for k in GROUP_KEYS if k not in adata.obs]
if missing_keys:
    raise SystemExit(f"GROUP_KEYS not in obs: {missing_keys}. obs has: {list(adata.obs.columns)}")

report: dict = {"checkpoint": ckpt.name, "matrix": ".raw (all genes, log-normalised)" if use_raw
                else "X", "group_keys": GROUP_KEYS, "signatures": {}}
scored: list[str] = []
for sig_name, genes in SIGNATURES.items():
    used, remapped, missing, ambiguous = [], {}, [], {}
    for g in dict.fromkeys(genes):                      # de-duplicate, keep order
        if g in pool:
            used.append(g)
            continue
        hits = by_fold.get(str(g).casefold(), [])
        if len(hits) == 1:
            used.append(hits[0])
            remapped[g] = hits[0]
        elif hits:
            ambiguous[g] = hits
        else:
            missing.append(g)
    coverage = len(used) / max(1, len(dict.fromkeys(genes)))
    entry = {"requested": len(dict.fromkeys(genes)), "used": used, "remapped_by_case": remapped,
             "missing": missing, "ambiguous": ambiguous, "coverage": round(coverage, 3)}
    if coverage < MIN_COVERAGE or not used:
        entry["status"] = (f"NOT SCORED: only {len(used)} of {entry['requested']} genes found "
                           f"(coverage {coverage:.0%} < {MIN_COVERAGE:.0%}) — check the symbols "
                           "against this species before scoring")
        report["signatures"][sig_name] = entry
        continue
    col = f"score_{sig_name}"
    sc.tl.score_genes(adata, gene_list=used, score_name=col, ctrl_size=CTRL_SIZE,
                      random_state=SEED, use_raw=use_raw)
    entry["status"] = "scored"
    report["signatures"][sig_name] = entry
    scored.append(col)

if not scored:
    print(json.dumps(report, indent=2))
    raise SystemExit("No signature reached MIN_COVERAGE — nothing was scored (see the report above).")

adata.write(work / "adata_scored.h5ad")
cells = adata.obs[GROUP_KEYS + scored].copy()
cells.to_csv(art / "tables" / "signature_scores_per_cell.csv")

# Per-group distribution (not one number): n, mean, median and IQR for every signature.
rows = []
if GROUP_KEYS:
    for keys, sub in cells.groupby(GROUP_KEYS, observed=True):
        keys = keys if isinstance(keys, tuple) else (keys,)
        for col in scored:
            v = sub[col].to_numpy(dtype=float)
            rows.append({**dict(zip(GROUP_KEYS, map(str, keys))), "signature": col[6:],
                         "n_cells": int(v.size), "mean": round(float(v.mean()), 4),
                         "median": round(float(np.median(v)), 4),
                         "q25": round(float(np.quantile(v, 0.25)), 4),
                         "q75": round(float(np.quantile(v, 0.75)), 4)})
import pandas as pd  # noqa: E402  (scanpy already imported it)
per_group = pd.DataFrame(rows)
per_group.to_csv(art / "tables" / "signature_score_by_group.csv", index=False)

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
if GROUP_KEYS:
    label = cells[GROUP_KEYS].astype(str).agg(" | ".join, axis=1)
    order = sorted(label.unique())
    for col in scored:
        fig, ax = plt.subplots(figsize=(max(6, 0.45 * len(order)), 4))
        ax.boxplot([cells.loc[label == o, col].to_numpy(dtype=float) for o in order],
                   showfliers=False)
        ax.set_xticks(range(1, len(order) + 1), order, rotation=90, fontsize=7)
        ax.set_ylabel(f"{col[6:]} score (score_genes)")
        ax.set_title(f"{col[6:]} by {' x '.join(GROUP_KEYS)}"[:80])
        fig.tight_layout()
        fig.savefig(art / "figures" / f"signature_{col[6:]}_by_group.png", dpi=150)
        plt.close(fig)

report["n_cells"] = int(adata.n_obs)
report["tables"] = ["tables/signature_score_by_group.csv", "tables/signature_scores_per_cell.csv"]
(art / "tables" / "signature_summary.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
if not per_group.empty:
    print(per_group.to_string(index=False, max_rows=60))
