"""Reference template — rank perturbations by E-distance to control (which guides actually did something).

In a pooled screen most guides are SILENT (no editing, or the gene is dispensable here). Before
running DE on everything, rank each perturbation by how far it moved cells away from the non-targeting
control in PCA space, and permutation-test whether that shift is real. Output is the shortlist of
perturbations with a genuine transcriptional phenotype — feed it to ONLY_PERTURBATIONS in
`perturbation_de_vs_control.py`.

The statistic is the scPerturb **E-distance** (energy distance with squared-Euclidean cost, Peidli et
al. 2024): E(P,C) = 2·mean‖p−c‖² − mean‖p−p'‖² − mean‖c−c'‖², computed here in closed form
(O(n·d) memory, no pairwise matrix) so it is exact and scales to large screens. Permutation shuffles
the P-vs-C labels to get a null for "this perturbation is no different from control".

ADAPT the CONFIG. Writes a ranked table + (optional) a pairwise perturbation×perturbation E-distance
matrix (for grouping perturbations that act alike) and prints a JSON summary for the report.
"""
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import scanpy as sc

# ----- CONFIG: adapt to THIS dataset -----------------------------------------------------------
PERT_KEY = "perturbation"      # obs column naming the guide / perturbation per cell
CONTROL = "NT"                # the shared non-targeting control level in PERT_KEY
N_PCS = 30                     # PCA dims the distance is measured in
MIN_CELLS = 30                 # skip a perturbation with fewer than this many cells
N_PERM = 1000                  # label permutations for the significance test (0 = skip, distances only)
FDR = 0.05                     # BH-adjusted permutation-p threshold for calling a perturbation "real"
PAIRWISE = False               # also emit a perturbation x perturbation E-distance matrix (heavier)
SEED = 0
# ----------------------------------------------------------------------------------------------

rng = np.random.default_rng(SEED)
work = Path(os.environ["BIOAGENT_WORK"])
art = Path(os.environ["BIOAGENT_ARTIFACTS"])
tdir = art / "tables"; tdir.mkdir(parents=True, exist_ok=True)

# Prefer the clustered checkpoint (has X_pca); else QC'd; else raw + compute PCA here.
for cand in ("adata_clustered.h5ad", "adata_qc.h5ad"):
    if (work / cand).exists():
        adata = sc.read_h5ad(work / cand); break
else:
    adata = sc.read_h5ad(os.environ["BIOAGENT_DATASET"])

if PERT_KEY not in adata.obs:
    raise SystemExit(f"obs has no column {PERT_KEY!r}; available: {list(adata.obs.columns)}")
if "X_pca" not in adata.obsm:
    sc.pp.pca(adata, n_comps=N_PCS)
X = np.asarray(adata.obsm["X_pca"][:, :N_PCS], dtype=np.float64)

labels = adata.obs[PERT_KEY].astype(str).to_numpy()
if CONTROL not in set(labels):
    raise SystemExit(f"control {CONTROL!r} not in {PERT_KEY} values {sorted(set(labels))[:20]}")
ctrl_mask = labels == CONTROL
Xc = X[ctrl_mask]


def _grp_stats(A):
    """mean vector, total variance (mean ||a-mean||^2), n — the closed-form E-distance ingredients."""
    mu = A.mean(0)
    var = float(((A - mu) ** 2).sum(1).mean())
    return mu, var, A.shape[0]


def _edist_from_stats(mu_a, var_a, mu_b, var_b):
    delta_ab = float(((mu_a - mu_b) ** 2).sum()) + var_a + var_b
    return 2.0 * delta_ab - 2.0 * var_a - 2.0 * var_b   # sq-euclidean energy distance


mu_c, var_c, n_c = _grp_stats(Xc)
perts = sorted(set(labels) - {CONTROL})
rows = []
for pert in perts:
    Xp = X[labels == pert]
    if Xp.shape[0] < MIN_CELLS:
        rows.append({"perturbation": pert, "n_cells": int(Xp.shape[0]), "edistance": np.nan,
                     "perm_pvalue": np.nan, "skipped": "too_few_cells"})
        continue
    mu_p, var_p, n_p = _grp_stats(Xp)
    e_obs = _edist_from_stats(mu_p, var_p, mu_c, var_c)

    pval = np.nan
    if N_PERM > 0:
        # Null: pool this perturbation + control, reshuffle the two labels, recompute E-distance.
        pool = np.concatenate([Xp, Xc], axis=0)
        n = n_p
        ge = 1  # +1 for the observed (Davison–Hinkley correction)
        for _ in range(N_PERM):
            idx = rng.permutation(pool.shape[0])
            A = pool[idx[:n]]; B = pool[idx[n:]]
            mu_a, va, _ = _grp_stats(A); mu_b, vb, _ = _grp_stats(B)
            if _edist_from_stats(mu_a, va, mu_b, vb) >= e_obs:
                ge += 1
        pval = ge / (N_PERM + 1)
    rows.append({"perturbation": pert, "n_cells": int(n_p), "edistance": e_obs,
                 "perm_pvalue": pval, "skipped": ""})

df = pd.DataFrame(rows)
tested = df[df["skipped"] == ""].copy()
# Benjamini–Hochberg on the permutation p-values.
if N_PERM > 0 and len(tested):
    m = len(tested)
    order = tested["perm_pvalue"].to_numpy().argsort()
    ranked = tested.iloc[order].reset_index(drop=True)
    padj = (ranked["perm_pvalue"].to_numpy() * m / (np.arange(m) + 1))
    padj = np.minimum.accumulate(padj[::-1])[::-1].clip(max=1.0)
    ranked["perm_padj"] = padj
    ranked["significant"] = ranked["perm_padj"] < FDR
    tested = ranked
tested = tested.sort_values("edistance", ascending=False)
tested.to_csv(tdir / "perturbation_edistance.csv", index=False)

real_hits = tested[tested.get("significant", False) == True]["perturbation"].tolist() \
    if "significant" in tested else tested.sort_values("edistance", ascending=False)["perturbation"].tolist()

pairwise_path = None
if PAIRWISE and len(perts) >= 2:
    keep = [p for p in perts if (labels == p).sum() >= MIN_CELLS]
    stats = {p: _grp_stats(X[labels == p]) for p in keep}
    M = pd.DataFrame(np.zeros((len(keep), len(keep))), index=keep, columns=keep)
    for i, a in enumerate(keep):
        for b in keep[i + 1:]:
            e = _edist_from_stats(stats[a][0], stats[a][1], stats[b][0], stats[b][1])
            M.loc[a, b] = M.loc[b, a] = e
    pairwise_path = tdir / "perturbation_edistance_pairwise.csv"
    M.to_csv(pairwise_path)

print(json.dumps({
    "control": CONTROL,
    "perturbation_key": PERT_KEY,
    "n_pcs": N_PCS,
    "n_perturbations": len(perts),
    "n_tested": int((df["skipped"] == "").sum()),
    "n_skipped_low_cells": int((df["skipped"] == "too_few_cells").sum()),
    "n_significant": (int((tested["significant"]).sum()) if "significant" in tested else None),
    "ranked_top": tested.head(15)[[c for c in ("perturbation", "n_cells", "edistance",
                                   "perm_pvalue", "perm_padj", "significant") if c in tested]]
                     .to_dict("records"),
    "real_hits_for_DE": real_hits,   # -> set ONLY_PERTURBATIONS in perturbation_de_vs_control.py
    "pairwise_matrix": (str(pairwise_path) if pairwise_path else None),
    "note": "silent perturbations (not significant) are a result — do not force DE on them",
}, indent=2))
