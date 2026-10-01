"""Preflight extracts CATEGORICAL obs columns (the dataset's experimental design) so the PI
planner can plan around it. anndata stores a categorical column as a subgroup with a
``categories`` child; we read those values for low-cardinality columns and only the count for
high-cardinality ones."""

from __future__ import annotations

import numpy as np
import pytest

h5py = pytest.importorskip("h5py")

from aiscientist.tools.datasets import _obs_categoricals, inspect_h5ad  # noqa: E402


def _write_categorical(group, name, categories, codes):
    g = group.create_group(name)
    g.create_dataset("categories", data=np.array(categories, dtype="S"))
    g.create_dataset("codes", data=np.array(codes, dtype="i1"))


def _make_h5ad(path):
    with h5py.File(path, "w") as f:
        obs = f.create_group("obs")
        _write_categorical(obs, "sampleid", [b"DDX41", b"WT"], [0, 1, 0, 1])
        _write_categorical(obs, "majorclass", [b"AC", b"BC", b"Rod"], [2, 0, 1, 2])
        _write_categorical(obs, "celltype", [f"ct{i}".encode() for i in range(40)],
                           [0, 1, 2, 3])
        obs.create_dataset("percent.mt", data=np.array([1.0, 2.0, 1.4, 0.9]))  # numeric, not categorical
        obs.create_dataset("_index", data=np.array([b"c0", b"c1", b"c2", b"c3"]))


def test_obs_categoricals_lists_low_cardinality_and_counts_high(tmp_path):
    p = tmp_path / "x.h5ad"
    _make_h5ad(p)
    with h5py.File(p, "r") as f:
        cats = _obs_categoricals(f["obs"], list_max=30)

    assert cats["sampleid"] == {"n": 2, "values": ["DDX41", "WT"]}     # condition/group, listed
    assert cats["majorclass"]["values"] == ["AC", "BC", "Rod"]         # existing labels, listed
    assert cats["celltype"]["n"] == 40 and cats["celltype"]["values"] == []  # high-card -> count only
    assert "percent.mt" not in cats and "_index" not in cats           # numeric / private skipped


def test_inspect_h5ad_attaches_obs_categoricals(tmp_path):
    p = tmp_path / "x.h5ad"
    _make_h5ad(p)
    with h5py.File(p, "r") as f:
        result = inspect_h5ad(f)

    assert result["obs_categoricals"]["sampleid"]["values"] == ["DDX41", "WT"]
    assert "sampleid" in result["obs_keys"]


def test_obs_categoricals_never_raises_on_garbage():
    class _Bad:
        def keys(self):  # noqa: D401 - test stub
            raise RuntimeError("boom")

    assert _obs_categoricals(_Bad()) == {}


# --- plain (non-Categorical) columns ------------------------------------------
#
# Whether a column arrived as a pandas Categorical is a detail of whoever wrote the file, and it
# used to decide whether the planner heard about the column at all. A production dataset stored
# `orig.ident` as a plain string array holding ONE value — a single library, i.e. zero biological
# replication — so it was skipped here, and the planner's profile filed it under "numeric /
# high-cardinality": an advertisement for many donors where there was one. The plan that came back
# proposed aggregating counts per donor, which the data could not support.


def _make_h5ad_with_plain_columns(path):
    with h5py.File(path, "w") as f:
        obs = f.create_group("obs")
        _write_categorical(obs, "sampleid", [b"DDX41", b"WT"], [0, 1, 0, 1])
        obs.create_dataset("orig.ident", data=np.array([b"0", b"0", b"0", b"0"]))   # ONE library
        obs.create_dataset("library", data=np.array([b"L1", b"L2", b"L1", b"L2"]))  # two, plain
        obs.create_dataset("nCount_RNA", data=np.array([10.5, 9.1, 22.0, 7.7]))     # continuous
        obs.create_dataset("barcode", data=np.array([f"bc{i}".encode() for i in range(4)]))


def test_a_single_valued_plain_column_is_counted_not_dropped(tmp_path):
    p = tmp_path / "plain.h5ad"
    _make_h5ad_with_plain_columns(p)
    with h5py.File(p, "r") as f:
        cats = _obs_categoricals(f["obs"], list_max=3)

    assert cats["orig.ident"] == {"n": 1, "values": ["0"]}, (
        "one library must be visible as one library — this is the number that decides which "
        "statistical test is legal for a condition contrast")
    assert cats["library"] == {"n": 2, "values": ["L1", "L2"]}


def test_continuous_and_per_cell_columns_stay_out(tmp_path):
    """The counting must not turn every float measurement and every barcode into a design column;
    a profile that lists all of them is one the planner stops reading."""
    p = tmp_path / "plain.h5ad"
    _make_h5ad_with_plain_columns(p)
    with h5py.File(p, "r") as f:
        cats = _obs_categoricals(f["obs"], list_max=3)

    assert "nCount_RNA" not in cats                 # float measurement
    assert "barcode" not in cats                    # 4 distinct values, over list_max=3
