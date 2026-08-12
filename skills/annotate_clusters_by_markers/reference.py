"""Reference template — assign a cell-type label to each Leiden cluster from its markers.

The curated tools cover QC -> clustering -> DE, but NOT the final label assignment. This
CodeAct template fills that gap: it reads the DE checkpoint (which carries
``rank_genes_groups``), scores each cluster against a small canonical marker dictionary, and
writes a cluster -> cell-type table. ADAPT the marker dictionary to YOUR tissue before running;
the labels are only as good as the dictionary, so state uncertainty honestly in the write-up.
"""
import json
import os
from pathlib import Path

import scanpy as sc

work = Path(os.environ["BIOAGENT_WORK"])
out = Path(os.environ["BIOAGENT_ARTIFACTS"])
(out / "tables").mkdir(parents=True, exist_ok=True)

adata = sc.read_h5ad(work / "adata_de.h5ad")          # has obs["leiden"] + rank_genes_groups

# ADAPT THIS to your tissue (gene SYMBOLS). Keys become the candidate cell-type labels.
MARKERS = {
    "T cell": ["CD3D", "CD3E", "TRAC"],
    "B cell": ["CD79A", "MS4A1", "CD19"],
    "Myeloid": ["LYZ", "CD14", "FCGR3A"],
    "NK cell": ["NKG7", "GNLY", "KLRD1"],
    "Endothelial": ["PECAM1", "VWF", "CLDN5"],
}

rank = adata.uns["rank_genes_groups"]
clusters = list(rank["names"].dtype.names)
top_n = 25
labels = {}
for cl in clusters:
    top_genes = {str(g).upper() for g in list(rank["names"][cl])[:top_n]}
    scores = {ct: len(top_genes & {g.upper() for g in genes}) for ct, genes in MARKERS.items()}
    best_ct, best_hits = max(scores.items(), key=lambda kv: kv[1])
    labels[cl] = {
        "cell_type": best_ct if best_hits > 0 else "Unknown",
        "marker_hits": best_hits,
        "confidence": "low" if best_hits <= 1 else "medium" if best_hits == 2 else "high",
        "top_genes": list(top_genes)[:10],
    }

adata.obs["cell_type"] = adata.obs["leiden"].map(lambda c: labels[str(c)]["cell_type"]).astype("category")
adata.write(work / "adata_annotated.h5ad")            # checkpoint for downstream figures

table_path = out / "tables" / "cluster_cell_types.json"
table_path.write_text(json.dumps(labels, indent=2))
print(json.dumps({"clusters": len(clusters), "labels": labels, "table": str(table_path)}, indent=2))
