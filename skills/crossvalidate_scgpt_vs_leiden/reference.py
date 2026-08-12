"""Reference template — cross-validate scGPT per-cell labels against the data's own structure.

No curated tool covers this. scGPT labels EVERY cell of the RAW query; QC/Leiden run on a filtered
subset, so the two cell sets differ — ALIGN BY BARCODE, never by row position. A positional merge (or
assigning a length-N prediction column onto an M-cell adata) raises a pandas alignment error and the
whole step fails — the single most common way this skill's runs stall. For EACH reference column
available — the independent Leiden clusters AND any existing majorclass/celltype labels the data
already carries — this writes a confusion table (reference x scGPT-label) + a per-group
agreement/purity summary, plus the scGPT confidence distribution. ADAPT column names if your CSV differs.
"""
import json
import os
from pathlib import Path

import pandas as pd
import scanpy as sc

work = Path(os.environ["BIOAGENT_WORK"])
art = Path(os.environ["BIOAGENT_ARTIFACTS"])
(art / "tables").mkdir(parents=True, exist_ok=True)

pred = pd.read_csv(art / "data" / "scgpt_predictions.csv")
label_col = "predictions" if "predictions" in pred.columns else "prediction"
conf_col = next((c for c in ("confidence", "score", "prob") if c in pred.columns), None)
# Barcode column (tolerant); else fall back to the first column as the index.
bc_col = next((c for c in ("cell", "barcode", "index", pred.columns[0]) if c in pred.columns), None)
pred = pred.set_index(bc_col)

# The clustered checkpoint has obs["leiden"] AND preserves the original majorclass/celltype labels.
adata = sc.read_h5ad(work / "adata_clustered.h5ad")

# BARCODE alignment — the ONLY correct way to line up scGPT (all raw cells) with the analyzed subset.
# The QC-dropped cells simply have no counterpart here; `intersection` handles the N-vs-M difference.
shared = adata.obs_names.intersection(pred.index)
if len(shared) == 0:
    raise SystemExit("No shared barcodes — check the CSV index vs adata.obs_names alignment.")
scgpt = pred.loc[shared, label_col].astype(str)

# Every reference to check scGPT against: the independent Leiden clusters (data-driven structure) +
# any existing atlas labels the data already carries (these make it a true CROSS-VALIDATION).
ref_cols = [c for c in ("leiden", "majorclass", "celltype") if c in adata.obs.columns]


def _crossvalidate(ref_name):
    ref = adata.obs.loc[shared, ref_name].astype(str)
    confusion = pd.crosstab(ref, scgpt)
    confusion.to_csv(art / "tables" / f"scgpt_vs_{ref_name}_confusion.csv")
    per_group = {}
    for g, row in confusion.iterrows():
        per_group[str(g)] = {"top_scgpt_label": str(row.idxmax()),
                             "purity": round(float(row.max() / row.sum()), 3),
                             "n": int(row.sum())}
    # Exact-string agreement is ROUGH: scGPT's reference taxonomy may name a class differently than
    # the data's own labels (e.g. "Rod" vs "rod photoreceptor"), so read the confusion table + purity,
    # not just this number.
    exact = round(float((ref.values == scgpt.values).mean()), 3)
    return {"n_groups": int(confusion.shape[0]), "exact_string_agreement": exact, "per_group": per_group}


summary = {
    "cells_compared": int(len(shared)),
    "cells_scgpt_only(qc_filtered_out)": int(len(pred.index.difference(adata.obs_names))),
    "cells_in_adata_without_a_prediction": int(len(adata.obs_names.difference(pred.index))),
    "cross_validation": {rc: _crossvalidate(rc) for rc in ref_cols},
}
if conf_col:
    c = pred.loc[shared, conf_col].astype(float)
    summary["confidence"] = {"mean": round(float(c.mean()), 3), "median": round(float(c.median()), 3),
                             "low_conf_frac(<0.5)": round(float((c < 0.5).mean()), 3)}
(art / "tables" / "scgpt_crossvalidation.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
