"""Ground truth for run_depth_matched_de: two cell types, opposite expected verdicts.

  RealBio   — MUT is 2x deeper AND 30 TRUE_* genes really are 3x higher in expression FRACTION.
              A correct check must call the up-ranking PRESERVED and keep the TRUE_* genes.
  DepthOnly — MUT is 2x deeper and NOTHING is biologically different: identical fractions.
              Any ranking here is manufactured by depth alone, so a correct check must NOT
              call it preserved, and few genes may keep their place.

The first validation of this tool used a "same sign after matching" rule and called two thirds
of pure background depth-robust; the criterion is now "stays in the matched ranking's own top-N".
"""
import csv
import os
import pathlib
import sys

import anndata as ad
import numpy as np
import pandas as pd
import scanpy as sc

BASE = pathlib.Path(os.path.expanduser("~/dmcheck"))
sys.path.insert(0, str(BASE / "src"))
from bioagent.tools.scrna_pack import run_depth_matched_de  # noqa: E402

rng = np.random.default_rng(0)
n_per, n_true = 400, 30
genes = [f"TRUE_{i}" for i in range(n_true)] + [f"BG_{i}" for i in range(270)]
G = len(genes)
base = rng.gamma(2.0, 1.0, size=G) + 0.5
frac_flat = base / base.sum()
frac_bio = base.copy()
frac_bio[:n_true] *= 3.0
frac_bio = frac_bio / frac_bio.sum()

blocks, sampleid, majorclass = [], [], []
for stratum, frac_mut in (("RealBio", frac_bio), ("DepthOnly", frac_flat)):
    for arm, depths, fr in (("WT", rng.poisson(1500, n_per), frac_flat),
                            ("MUT", rng.poisson(3000, n_per), frac_mut)):
        for total in depths:
            blocks.append(rng.multinomial(max(int(total), 1), fr))
            sampleid.append(arm)
            majorclass.append(stratum)

X = np.vstack(blocks).astype(np.float32)
obs = pd.DataFrame({"sampleid": sampleid, "majorclass": majorclass},
                   index=[f"c{i}" for i in range(len(sampleid))])
adata = ad.AnnData(X=X, obs=obs, var=pd.DataFrame(index=genes))
adata.layers["counts"] = adata.X.copy()
sc.pp.normalize_total(adata, target_sum=1e4)
sc.pp.log1p(adata)

work = BASE / "work"
for sub in ("work", "artifacts/tables", "artifacts/figures"):
    (work / sub).mkdir(parents=True, exist_ok=True)
adata.write(work / "work" / "adata_qc.h5ad")


class Ctx:
    workspace = work
    decisions: dict = {}


res = run_depth_matched_de(
    {"groupby": "sampleid", "reference": "WT", "stratify_by": "majorclass", "n_genes": 60}, Ctx())
print("STATUS:", res.get("status"), res.get("error", ""))
for r in res.get("per_group", []):
    print(f"  {r['group']:10} {r['direction']:5} rho={str(r['spearman_rho']):>8} "
          f"kept_top={str(r['pct_kept_top_rank']):>5}% verdict={r['verdict']:<9} "
          f"ratio={r['depth_ratio']}")

fam = {}
with (work / "artifacts/tables/depth_matched_genes.csv").open() as fh:
    for row in csv.DictReader(fh):
        k = (row["group"], row["direction"], row["gene"].split("_")[0])
        n, ok = fam.get(k, (0, 0))
        fam[k] = (n + 1, ok + (row["depth_robust"] == "True"))

print("\nGROUND TRUTH — genes entering the check -> how many kept their rank:")
for (grp, direction, family), (n, ok) in sorted(fam.items()):
    print(f"  {grp:10} {direction:5} {family:5} {ok}/{n}")

verdicts = {(r["group"], r["direction"]): r["verdict"] for r in res.get("per_group", [])}
print("\nCHECKS")
print("  RealBio up is preserved      :",
      verdicts.get(("RealBio", "up")) == "preserved")
print("  DepthOnly up is NOT preserved:",
      verdicts.get(("DepthOnly", "up")) != "preserved", f"({verdicts.get(('DepthOnly','up'))})")
true_up = fam.get(("RealBio", "up", "TRUE"), (0, 0))
print(f"  real biology kept            : {true_up[1]}/{true_up[0]}")
d_up = fam.get(("DepthOnly", "up", "BG"), (0, 0))
print(f"  pure-depth ranking kept      : {d_up[1]}/{d_up[0]}  (lower is better)")
