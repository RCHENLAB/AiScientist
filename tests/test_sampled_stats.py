"""A statistic printed from the first rows of a matrix is flagged on the run_code result.

Prod run 74873590ca7b (2026-10-23), step 2, printed "raw max 7.123" from the first 300 nuclei and
200,000 values; the answer reported ".raw max 7.12" beside step 1's whole-matrix "X max 8.48" as an
unresolved provenance discrepancy (``run_scanpy_qc`` sets ``adata.raw = adata`` after log1p, so they
are one matrix). Run 7f2955e8e834, step 12, did the same ("`.raw` max 6.845"). The snippets below are
the runs' own code.
"""

from __future__ import annotations

import pytest

from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.research_lab import make_run_code_tool
from aiscientist.agents.sampled_stats import annotate, sampled_statistics

RUN_74873590 = """
import os, json, numpy as np, pandas as pd, scanpy as sc
from pathlib import Path
work=Path(os.environ['AISCIENTIST_WORK'])
adata=sc.read_h5ad(work/'adata_qc.h5ad')
use_raw=adata.raw is not None
pool=adata.raw.var_names if use_raw else adata.var_names
M=adata.raw.X if use_raw else adata.X
print('matrix', '.raw' if use_raw else 'X', 'raw genes', len(pool))
# sanity: .raw is log-normalised (not integer counts)
s=M[:300]; v=np.asarray(s.data if hasattr(s,'data') else s).ravel()[:200000]
print('raw max', float(v.max()), 'integer?', bool(np.allclose(v,np.round(v))))
"""
RUN_7F2955 = ("x = ad.raw.X\n"
              "v = np.asarray(x[:300].data if hasattr(x,'data') else x.ravel())[:200000].astype(float)\n"
              "print('raw matrix max:', v.max(), 'all-integer?', bool(np.allclose(v, np.round(v))))\n")
# Step 1 of the same run: the whole matrix. This one is right and must stay quiet.
STEP_1 = """
import os, scanpy as sc, numpy as np
a = sc.read_h5ad(os.path.join(os.environ['AISCIENTIST_WORK'],'adata_qc.h5ad'))
print('X max', float(a.X.max()) if not hasattr(a.X,'toarray') else float(a.X.max()))
c = a.layers['counts']
print('counts max', float(c.max()), 'integer?', np.allclose(c.data, np.round(c.data)))
X=a.X
print(X.max(), a.var_names[:5].tolist())
"""


@pytest.mark.parametrize("code, sample", [(RUN_74873590, "`M[:300]`"), (RUN_7F2955, "`x[:300]`")])
def test_the_prod_sampled_max_is_flagged(code, sample):
    found = sampled_statistics(code)
    assert len(found) == 1
    assert found[0].startswith("SAMPLED STATISTIC:") and "prints the max of a SAMPLE" in found[0]
    assert sample in found[0] and "[:200000]" in found[0]
    assert "Do not report it as the file's or the matrix's max" in found[0]


@pytest.mark.parametrize("code", [
    STEP_1,
    "print(len(rnk), rnk[:3])",                                     # a peek, no statistic
    "print(df.head(3).to_string())",
    "top = df.sort_values('log2fc', ascending=False)[:10]\nprint(top['log2fc'].max())",   # a ranking
    "sub = X[:300]\nsub = X\nprint(sub.max())",                     # reassigned from the whole
    "print('raw sample max in first 100 rows:', X[:100].max())",    # the print says it is a sample
    "draw = np.random.choice(n, size=n)\nprint(vals[draw].mean())", # a bootstrap is meant to sample
    "for i in range(0, n, 1000):\n    print(X[i:i+1000].max())",    # a chunk, not a leading slice
    "print(adata[:, genes].X.max())",                               # a gene subset by name
    "def broken(:\n",                                               # not Python: nothing to say
])
def test_whole_objects_rankings_and_labelled_samples_are_left_alone(code):
    assert sampled_statistics(code) == []


@pytest.mark.parametrize("code, stat", [
    ("print('mean', np.mean(adata.X[:500]))", "mean"),
    ("sub = adata.X[:5]\nprint(f'counts per cell {sub.sum(axis=1)}')", "sum"),
    ("s = X.iloc[:200]\nm = s.median()\nprint('median', s.median())", "median"),
    ("v = adata.raw.X[: min(500, adata.n_obs)]\nprint('max', float(v.max()))", "max"),
])
def test_other_statistics_of_a_sample_are_flagged(code, stat):
    found = sampled_statistics(code)
    assert len(found) == 1 and f"prints the {stat} of a SAMPLE" in found[0]


def test_annotate_adds_to_the_results_warnings():
    assert annotate({"status": "ok", "stdout": "raw max 7.12"}, RUN_74873590)["warnings"][0].startswith(
        "SAMPLED STATISTIC")
    kept = annotate({"status": "ok", "stdout": "x", "warnings": ["earlier"]}, RUN_74873590)
    assert kept["warnings"][0] == "earlier" and len(kept["warnings"]) == 2
    one = annotate({"status": "ok", "stdout": "x", "warnings": "single"}, RUN_74873590)
    assert one["warnings"][0] == "single" and len(one["warnings"]) == 2
    assert "warnings" not in annotate({"status": "ok", "stdout": "x"}, STEP_1)
    assert "warnings" not in annotate({"status": "error", "stdout": ""}, RUN_74873590)   # nothing printed
    assert annotate("plain text", RUN_74873590) == "plain text"


def test_the_run_code_tool_returns_the_warning_with_the_result():
    """The Scientist reads it on its next turn, before it writes the step's answer."""
    tool = make_run_code_tool(lambda code: {"status": "ok", "stdout": "raw max 7.123154163360596 integer? False"})
    result = tool.executor({"code": RUN_74873590}, HarnessContext(decisions={}, tunnel_port=1, model="m"))
    assert result["stdout"].startswith("raw max 7.12")
    assert len(result["warnings"]) == 1 and "`M[:300]`" in result["warnings"][0]
    quiet = tool.executor({"code": STEP_1}, HarnessContext(decisions={}, tunnel_port=1, model="m"))
    assert "warnings" not in quiet
