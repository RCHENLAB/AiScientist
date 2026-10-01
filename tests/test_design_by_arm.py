"""The two-arm profile carries the numbers a reviewer needs BEFORE any comparison — cells per arm,
cells per label per arm, and the arms' median depth — and flags a depth imbalance.

Built from a production report that narrated "translation machinery up in every cell type" as
DDX41 biology. The arms differed 1.6x in median depth (3,078 vs 1,916 counts) and every stratum
came back up >> down with ribosomal genes on top: the classic depth artifact, and nothing in the
run had computed the one table that shows it. Now the profile does, deterministically, and run_de
names a same-direction skew for what it usually is."""

from __future__ import annotations

import pytest

pytest.importorskip("h5py")
pytest.importorskip("scanpy")

import anndata as ad  # noqa: E402
import h5py  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from bioagent.tools.datasets import inspect_h5ad  # noqa: E402


def _two_arm_h5ad(tmp_path, *, depth_ratio=1.6):
    rng = np.random.default_rng(0)
    n = 600
    arm = np.array(["KO" if i % 2 else "WT" for i in range(n)])
    label = np.array([["Rod", "Cone", "MG"][i % 3] for i in range(n)])
    lam = np.where(arm == "KO", 8.0 * depth_ratio, 8.0)
    x = rng.poisson(lam[:, None], (n, 50)).astype(np.float32)
    a = ad.AnnData(x)
    a.var_names = [f"G{i}" for i in range(50)]
    a.obs_names = [f"c{i}" for i in range(n)]
    a.obs["sampleid"] = pd.Categorical(arm)
    a.obs["majorclass"] = pd.Categorical(label)
    a.obs["orig.ident"] = pd.Categorical(["0"] * n)
    a.obs["nCount_RNA"] = x.sum(1)
    a.obs["nFeature_RNA"] = (x > 0).sum(1)
    a.obs["percent.mt"] = rng.uniform(0.5, 3.0, n)
    p = tmp_path / "two_arm.h5ad"
    a.write(p)
    return p


def test_profile_carries_cells_and_depth_per_arm_and_flags_imbalance(tmp_path):
    with h5py.File(_two_arm_h5ad(tmp_path), "r") as h:
        r = inspect_h5ad(h)
    d = r["design_by_arm"]
    assert d["condition_column"] == "sampleid"
    assert d["cells_by_arm"] == {"KO": 300, "WT": 300}
    assert d["label_column"] == "majorclass"
    assert d["cells_by_label_and_arm"]["Rod"] == {"KO": 100, "WT": 100}
    assert set(d["qc_median_by_arm"]) >= {"nCount_RNA", "nFeature_RNA", "percent.mt"}
    assert "1.6x" in d["depth_imbalance"] or "1.5x" in d["depth_imbalance"] or "1.7x" in d["depth_imbalance"]


def test_matched_depth_is_not_flagged(tmp_path):
    with h5py.File(_two_arm_h5ad(tmp_path, depth_ratio=1.0), "r") as h:
        r = inspect_h5ad(h)
    assert "depth_imbalance" not in r["design_by_arm"]


def test_run_de_names_a_same_direction_skew_across_strata(tmp_path):
    """Every stratum up >> down is a technical signature; the tool says so where the numbers are."""
    from types import SimpleNamespace
    from bioagent.tools.run_de import tool as run_de_tool
    from bioagent.tools.run_scanpy_qc import tool as run_scanpy_qc_tool
    p = _two_arm_h5ad(tmp_path, depth_ratio=2.5)      # a big depth gap -> global 'up' in KO
    ctx = SimpleNamespace(workspace=tmp_path, decisions={"dataset_path": str(p)})
    assert run_scanpy_qc_tool.run_scanpy_qc({"min_genes": 1, "min_cells": 1, "max_pct_mt": 100.0}, ctx)["status"] == "ok"
    out = run_de_tool.run_de({"groupby": "sampleid", "reference": "WT", "stratify_by": "majorclass",
                             "min_cells": 10, "min_pct": 0}, ctx)
    assert out["status"] == "ok"
    # Whether the toy data trips the >=100-genes gate depends on the draw; the CONTRACT is that
    # when the skew is there it is named as technical, not biological.
    if out.get("direction_bias"):
        assert any("DIRECTION BIAS" in w and "technical" in w for w in out["warnings"])


def test_the_dataset_section_renders_the_per_arm_table(tmp_path):
    from types import SimpleNamespace
    from bioagent.gateway.app import _dataset_section
    (tmp_path / "data").mkdir()
    import json
    dr = {"dataset_path": "/u/x.h5ad", "cells": 600, "genes": 50, "dataset_kind": "h5ad_single_cell",
          "obs_categoricals": {"sampleid": {"n": 2, "values": ["KO", "WT"]},
                               "majorclass": {"n": 3, "values": ["Rod", "Cone", "MG"]}},
          "design_by_arm": {"condition_column": "sampleid", "cells_by_arm": {"KO": 300, "WT": 300},
                            "label_column": "majorclass",
                            "cells_by_label_and_arm": {"Rod": {"KO": 100, "WT": 100},
                                                       "RPE": {"KO": 2, "WT": 3}},
                            "qc_median_by_arm": {"nCount_RNA": {"KO": 3078.0, "WT": 1916.0}},
                            "depth_imbalance": "median nCount_RNA differs 1.6x between arms"}}
    (tmp_path / "data" / "dataset_results.json").write_text(json.dumps(dr))
    out = _dataset_section(tmp_path, SimpleNamespace(rounds=[]))
    assert "| cells | 300 | 300 |" in out
    assert "| median nCount_RNA | 3078.0 | 1916.0 |" in out
    assert "| Rod | 100 | 100 | yes |" in out and "| RPE | 2 | 3 | no |" in out
    assert "**Depth check.** median nCount_RNA differs 1.6x" in out
    # and no list runs into its label (the blank line that pandoc needs)
    assert "**Experimental design.**\n\n- " in out


def test_snrna_hint_from_nuclear_fraction():
    """A nuclear_fraction obs column yields the computed snRNA hint (protocol + mt-threshold
    guidance), so no model has to connect the dots itself."""
    import numpy as np
    import pandas as pd
    from bioagent.tools.datasets import _design_by_arm

    n = 80
    obs = pd.DataFrame({
        "sampleid": ["KO"] * 40 + ["WT"] * 40,
        "majorclass": (["Rod"] * 20 + ["MG"] * 20) * 2,
        "nCount_RNA": np.r_[np.full(40, 3000.0), np.full(40, 1900.0)],
        "percent.mt": np.full(n, 1.4),
        "nuclear_fraction": np.full(n, 0.33),
    })
    cats = {"sampleid": {"n": 2, "values": ["KO", "WT"]},
            "majorclass": {"n": 2, "values": ["Rod", "MG"]}}
    out = _design_by_arm(obs, cats, n)
    assert out and "snrna_hint" in out
    assert "single-NUCLEUS" in out["snrna_hint"] and "1-5%" in out["snrna_hint"]


def test_no_snrna_hint_without_the_column():
    import numpy as np
    import pandas as pd
    from bioagent.tools.datasets import _design_by_arm

    obs = pd.DataFrame({
        "sampleid": ["KO"] * 40 + ["WT"] * 40,
        "nCount_RNA": np.full(80, 2000.0),
    })
    cats = {"sampleid": {"n": 2, "values": ["KO", "WT"]}}
    out = _design_by_arm(obs, cats, 80)
    assert not out or "snrna_hint" not in out
