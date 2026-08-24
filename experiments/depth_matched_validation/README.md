# Does `run_depth_matched_de` actually separate biology from depth?

A tool that answers "is this signal real, or is the deeper arm just deeper?" is only worth having
if it gets a KNOWN answer right. `validate.py` builds a dataset where the answer is known and
checks both directions of the claim: real biology must survive, and a ranking manufactured by
depth alone must not.

## The dataset

800 cells per cell type, 300 genes, arm `MUT` sequenced ~2x deeper per cell than `WT`.

| cell type | depth | biology | correct verdict |
|---|---|---|---|
| `RealBio` | 2x imbalance | 30 `TRUE_*` genes really are 3x higher in expression FRACTION | up-ranking **preserved** |
| `DepthOnly` | 2x imbalance | none — identical fractions in both arms | up-ranking **not preserved** |

`DepthOnly` is the control that matters: every gene ordering it produces is an artefact of
sampling more molecules per cell, so a check that calls it preserved would certify noise.

## Result (2026-08-20, `analysis.sif` on HPC3, full log in `result-2026-08-20.log`)

| cell type | direction | Spearman rho | kept top rank | verdict |
|---|---|---|---|---|
| DepthOnly | up | -0.16 | 31.7 % | **inverted** |
| DepthOnly | down | -0.05 | 0 % | **inverted** |
| RealBio | up | **0.82** | 55 % | **preserved** |
| RealBio | down | 0.03 | 25 % | weak |

Gene level: `RealBio` up kept **30/30** of the genes that were genuinely changed and only 3/30 of
the background; `DepthOnly` kept 15/53 up and 0/8 down. Real signal survives intact, manufactured
signal is mostly destroyed, and `RealBio`'s down direction — where this dataset contains no real
biology — is correctly called weak rather than preserved.

## What this run changed about the tool

The first attempt scored a gene "depth-robust" if it kept its SIGN after matching. That passes a
gene whose effect collapses from 3.0 to 0.02, and it called two thirds of pure background robust.
The criterion is now membership of the matched ranking's own top-N — whether the gene KEPT ITS
PLACE, which is the question the check exists to answer.

## Running it

Needs scanpy, so it runs in the analysis container rather than in the unit-test suite:

```bash
singularity exec --containall --bind "$HOME:$HOME" \
  /dfs3b/ruic20_lab/software/AiScientist/containers/analysis.sif \
  python3 validate.py
```

The deterministic parts (quantile matching, Spearman, the catalog contract) are unit-tested in
`tests/test_depth_matched_de.py` and need no container.
