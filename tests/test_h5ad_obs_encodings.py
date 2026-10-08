"""The raw-h5py obs readers behind the dataset profile accept every on-disk encoding anndata uses
for an obs column, not just the one the file's writer happened to pick.

anndata 0.13 under pandas 3 (whose default ``str`` dtype is nullable) stores the obs INDEX, and
every string column it does not turn into a categorical, as a ``nullable-string-array`` GROUP
(``values`` + boolean ``mask``) instead of a ``string-array`` dataset. The readers called
``.shape`` / ``[:n]`` on it, the never-raise guard around the per-arm table swallowed the
AttributeError, and every .h5ad written by a current anndata silently lost ``design_by_arm`` —
the depth/QC imbalance check the claim audit and the report rely on. Nullable-integer columns
(an ``Int64`` nCount_RNA) are the same group shape and were dropped from the QC medians in every
anndata version.

The hand-built files run wherever h5py does (CI included); the last test writes through whatever
anndata is installed, so the next encoding change fails here instead of in a researcher's run."""

from __future__ import annotations

import numpy as np
import pytest

h5py = pytest.importorskip("h5py")

from aiscientist.tools.datasets import _obs_categoricals, _obs_column_values, inspect_h5ad  # noqa: E402


def _categorical(group, name, categories, codes):
    g = group.create_group(name)
    g.attrs["encoding-type"] = "categorical"
    g.create_dataset("categories", data=np.array(categories, dtype="S"))
    g.create_dataset("codes", data=np.array(codes, dtype="i1"))


def _nullable(group, name, values, mask, encoding):
    """anndata's ``values`` + ``mask`` group (mask True = missing)."""
    g = group.create_group(name)
    g.attrs["encoding-type"] = encoding
    g.create_dataset("values", data=values)
    g.create_dataset("mask", data=np.array(mask, dtype=bool))


def _strings(values):
    return np.array([v.encode() for v in values], dtype=h5py.string_dtype())


@pytest.mark.parametrize("index_encoding", ["string-array", "nullable-string-array"])
def test_design_by_arm_survives_either_index_encoding(tmp_path, index_encoding):
    n = 6
    names = [f"c{i}" for i in range(n)]
    p = tmp_path / "x.h5ad"
    with h5py.File(p, "w") as f:
        obs = f.create_group("obs")
        obs.attrs["_index"] = "_index"
        if index_encoding == "string-array":            # anndata <= 0.12 / pandas 2
            obs.create_dataset("_index", data=_strings(names))
        else:                                           # anndata 0.13 / pandas 3
            _nullable(obs, "_index", _strings(names), [False] * n, index_encoding)
        _categorical(obs, "sampleid", [b"KO", b"WT"], [0, 1] * 3)
        _categorical(obs, "majorclass", [b"MG", b"Rod"], [0, 0, 0, 1, 1, 1])
        obs.create_dataset("nCount_RNA", data=np.array([300.0, 100.0, 300.0, 100.0, 300.0, 100.0]))
    with h5py.File(p, "r") as f:
        d = inspect_h5ad(f)["design_by_arm"]
    assert d["cells_by_arm"] == {"KO": 3, "WT": 3}
    assert d["cells_by_label_and_arm"]["MG"] == {"KO": 2, "WT": 1}
    assert d["qc_median_by_arm"]["nCount_RNA"] == {"KO": 300.0, "WT": 100.0}
    assert "3.0x" in d["depth_imbalance"]


def test_nullable_string_and_integer_columns_are_read_and_their_mask_honoured(tmp_path):
    """A plain condition column stored as nullable strings still defines the arms; a masked entry
    is neither a level nor a measurement."""
    p = tmp_path / "x.h5ad"
    with h5py.File(p, "w") as f:
        obs = f.create_group("obs")
        obs.attrs["_index"] = "_index"
        _nullable(obs, "_index", _strings([f"c{i}" for i in range(6)]), [False] * 6,
                  "nullable-string-array")
        _nullable(obs, "sampleid", _strings(["KO", "WT"] * 3), [False] * 6, "nullable-string-array")
        # one library, with a missing entry that must not count as a second level ("")
        _nullable(obs, "orig.ident", _strings(["0", "", "0", "0", "0", "0"]),
                  [False, True, False, False, False, False], "nullable-string-array")
        # KO's 999 is masked: its median must be over [100, 300], not [100, 300, 999]
        _nullable(obs, "nCount_RNA", np.array([100, 50, 300, 60, 999, 70]),
                  [False, False, False, False, True, False], "nullable-integer")
        obs.create_group("odd")                         # an encoding nobody knows: skipped
    with h5py.File(p, "r") as f:
        cats = _obs_categoricals(f["obs"])
        assert cats["orig.ident"] == {"n": 1, "values": ["0"]}
        assert cats["sampleid"] == {"n": 2, "values": ["KO", "WT"]}
        assert "odd" not in cats
        assert _obs_column_values(f["obs"], "orig.ident", 3) == ["0", "NA", "0"]
        assert _obs_column_values(f["obs"], "odd", 3) is None
        d = inspect_h5ad(f)["design_by_arm"]
    assert d["condition_column"] == "sampleid"
    assert d["cells_by_arm"] == {"KO": 3, "WT": 3}
    assert d["qc_median_by_arm"]["nCount_RNA"] == {"KO": 200.0, "WT": 60.0}


def test_profile_of_a_file_written_by_the_installed_anndata(tmp_path):
    """Whatever anndata/pandas this environment has, the profile of a file it writes carries the
    per-arm table. ``convert_strings_to_categoricals=False`` keeps plain string columns plain, so
    the newest string encoding is exercised wherever the installed stack produces it."""
    ad = pytest.importorskip("anndata")
    pd = pytest.importorskip("pandas")
    n = 60
    a = ad.AnnData(np.ones((n, 4), dtype=np.float32))
    a.obs_names = [f"c{i}" for i in range(n)]
    a.obs["sampleid"] = ["KO" if i % 2 else "WT" for i in range(n)]        # plain strings
    a.obs["majorclass"] = pd.Categorical([["Rod", "Cone", "MG"][i % 3] for i in range(n)])
    a.obs["orig.ident"] = ["0"] * n                                        # one library
    a.obs["nCount_RNA"] = pd.array([3000 if i % 2 else 1000 for i in range(n)], dtype="Int64")
    a.obs["percent.mt"] = np.full(n, 1.5)
    p = tmp_path / "written.h5ad"
    a.write_h5ad(p, convert_strings_to_categoricals=False)
    with h5py.File(p, "r") as f:
        r = inspect_h5ad(f)
    assert r["obs_categoricals"]["orig.ident"] == {"n": 1, "values": ["0"]}
    d = r["design_by_arm"]
    assert d["condition_column"] == "sampleid"
    assert d["cells_by_arm"] == {"KO": 30, "WT": 30}
    assert d["cells_by_label_and_arm"]["Rod"] == {"KO": 10, "WT": 10}
    assert d["qc_median_by_arm"]["nCount_RNA"] == {"KO": 3000.0, "WT": 1000.0}
    assert d["qc_median_by_arm"]["percent.mt"] == {"KO": 1.5, "WT": 1.5}
    assert "3.0x" in d["depth_imbalance"]
