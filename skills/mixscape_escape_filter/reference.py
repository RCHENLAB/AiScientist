"""Reference template — (OPTIONAL) label & drop 'escaping' cells with Mixscape before DE.

In a real screen a fraction of guide-assigned cells ESCAPE knockdown and look transcriptionally like
control; keeping them dilutes every downstream comparison. Mixscape (Papalexi et al. 2021, via pertpy)
computes a local perturbation signature and classifies each cell as perturbed (KO/KD), non-perturbed
(NP = escaped), or control (NT) — so the NP cells can be removed before `perturbation_de_vs_control.py`.

pertpy is a HEAVY OPTIONAL dependency and its Mixscape API drifts across versions. This template
degrades gracefully: if pertpy is not importable it writes a note and exits 0 (the skill then runs DE
on all guide-assigned cells — a conservative, effect-diluting choice, which you must state). When it
runs, it writes `adata_mixscape.h5ad` (NP cells removed) to BIOAGENT_WORK for the DE step to read, and
reports the per-perturbation NP fraction. ADAPT the CONFIG and the API call to your installed pertpy.
"""
import json
import os
from pathlib import Path

import scanpy as sc

# ----- CONFIG: adapt to THIS dataset ----------------------------------------------------------
PERT_KEY = "perturbation"      # obs column naming the guide / perturbation per cell
CONTROL = "NT"                # the shared non-targeting control level in PERT_KEY
# ----------------------------------------------------------------------------------------------

work = Path(os.environ["BIOAGENT_WORK"])
ckpt = work / "adata_qc.h5ad"
adata = sc.read_h5ad(ckpt if ckpt.exists() else os.environ["BIOAGENT_DATASET"])

if PERT_KEY not in adata.obs:
    raise SystemExit(f"obs has no column {PERT_KEY!r}; available: {list(adata.obs.columns)}")

try:
    import pertpy as pt
except Exception as e:   # noqa: BLE001 — any import/dep failure means "skip, don't block"
    print(json.dumps({
        "mixscape": "SKIPPED",
        "reason": f"pertpy not available ({type(e).__name__}: {e})",
        "consequence": "DE will run on ALL guide-assigned cells (escaping cells not removed) — "
                       "state this dilution in the report",
    }, indent=2))
    raise SystemExit(0)

# --- Mixscape (adapt to your pertpy version; the class-global obs key is the stable output) ---
ms = pt.tl.Mixscape()
# Local perturbation signature: each cell minus its nearest control neighbours in PCA space.
ms.perturbation_signature(adata, pert_key=PERT_KEY, control=CONTROL)
# Classify KO/KD vs NP (escaped) vs NT; writes adata.obs["mixscape_class_global"].
ms.mixscape(adata, control=CONTROL, labels=PERT_KEY)

CLASS_KEY = "mixscape_class_global"
if CLASS_KEY not in adata.obs:
    raise SystemExit(f"expected Mixscape to write obs[{CLASS_KEY!r}] — check your pertpy version's API")

glob = adata.obs[CLASS_KEY].astype(str)
np_frac = {}
for pert in sorted(set(adata.obs[PERT_KEY].astype(str)) - {CONTROL}):
    m = adata.obs[PERT_KEY].astype(str) == pert
    n = int(m.sum())
    np_frac[pert] = round(float((glob[m] == "NP").mean()), 3) if n else 0.0

keep = (glob != "NP").to_numpy()   # drop escaped cells; keep KO/KD and control
adata[keep].copy().write(work / "adata_mixscape.h5ad")

print(json.dumps({
    "mixscape": "OK",
    "class_counts": glob.value_counts().to_dict(),
    "np_fraction_by_perturbation": np_frac,
    "n_cells_before": int(adata.n_obs),
    "n_cells_after_removing_NP": int(keep.sum()),
    "wrote": "adata_mixscape.h5ad",
    "next": "point perturbation_de_vs_control.py at adata_mixscape.h5ad (escaped cells removed)",
}, indent=2))
