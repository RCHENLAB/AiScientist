"""The Leiden resolution is CHOSEN, not defaulted.

Every cell-type label inherits the partition it was assigned on, so `resolution=1.0` is an
unexamined assumption in every downstream call. `_select_resolution` sweeps candidates and
keeps the FINEST one whose clusters still reproduce under resampling (adjusted Rand index).

scanpy is in the `analysis` extra and absent on most hosts, so these tests drive the selection
rule with a stand-in `sc` whose Leiden is a deterministic function of resolution: coarse
resolutions reproduce exactly (ARI 1.0), fine ones scatter. numpy + scikit-learn are real.
"""

from __future__ import annotations

import types

import numpy as np

from bioagent.tools import scrna_pack


class _Obs(dict):
    """The sliver of the pandas API `_select_resolution` touches on `adata.obs`."""

    def __setitem__(self, key, value):
        super().__setitem__(key, np.asarray(value))

    def __getitem__(self, key):
        col = super().__getitem__(key)
        return types.SimpleNamespace(
            astype=lambda _t: types.SimpleNamespace(values=np.asarray(col).astype(str)),
            nunique=lambda: len(set(np.asarray(col).tolist())),
        )


class _AData:
    """Cells keep their identity through slicing, the way a real AnnData view does — a cell's
    label must depend on the cell, not on its position in whatever subset it landed in."""

    def __init__(self, n_obs, idx=None):
        self.n_obs = n_obs
        self.obs = _Obs()
        self._idx = np.arange(n_obs) if idx is None else np.asarray(idx)

    def __getitem__(self, idx):
        return _AData(len(idx), idx=self._idx[np.asarray(idx)])

    def copy(self):
        return self


def _fake_sc(unstable_at):
    """Leiden stand-in. At resolutions below `unstable_at`, a cell's label is a stable function
    of its identity, so any subsample reproduces the full-data partition (ARI 1.0). At or above
    it, labels are drawn at random per call — the signature of over-clustering."""
    rng = np.random.default_rng(1234)

    def leiden(ad, resolution, random_state=0, key_added="leiden"):
        idx = ad._idx
        if resolution >= unstable_at:
            ad.obs[key_added] = rng.integers(0, 12, size=ad.n_obs)
        else:
            ad.obs[key_added] = idx % max(2, int(round(resolution * 10)))

    return types.SimpleNamespace(
        tl=types.SimpleNamespace(leiden=leiden),
        pp=types.SimpleNamespace(neighbors=lambda *a, **k: None),
    )


def _select(adata, sc, **kw):
    params = dict(candidates=[0.2, 0.4, 0.6, 0.8, 1.0], n_boot=3, subsample=0.8,
                  stability_min=0.90, n_neighbors=15, n_pcs=30, max_cells=20000)
    params.update(kw)
    return scrna_pack._select_resolution(sc, adata, **params)


def test_picks_the_finest_resolution_that_still_reproduces():
    # 0.8 and 1.0 scatter; 0.6 is the finest stable one. Taking the MOST stable instead would
    # return 0.2 and under-cluster the data, which is why the rule is "finest above the floor".
    res, sweep, note = _select(_AData(400), _fake_sc(unstable_at=0.8))
    assert res == 0.6
    assert note == ""
    by_res = {r["resolution"]: r["stability"] for r in sweep}
    assert by_res[0.6] >= 0.9 and by_res[1.0] < 0.9
    assert [r["resolution"] for r in sweep] == [0.2, 0.4, 0.6, 0.8, 1.0]
    assert all("stability_sd" in r and "n_clusters" in r for r in sweep)


def test_no_candidate_clears_the_floor_says_so_instead_of_pretending():
    # Everything scatters. Returning the best of a bad set is acceptable; doing it SILENTLY is
    # not — the partition is less reproducible than the floor requires and the note must say so.
    res, sweep, note = _select(_AData(400), _fake_sc(unstable_at=0.0))
    assert res in {r["resolution"] for r in sweep}
    assert "stability floor" in note and "LESS reproducible" in note
    assert all(r["stability"] < 0.9 for r in sweep)


def test_sweep_is_capped_and_the_subsetting_is_disclosed():
    # The sweep costs n_boot x n_candidates re-clusterings, so it runs on a bounded subset of a
    # large object. That changes what was measured, so it is reported rather than assumed.
    res, _sweep, note = _select(_AData(50_000), _fake_sc(unstable_at=0.8), max_cells=5_000)
    assert res == 0.6
    assert "5000-cell subset of 50000" in note
    assert "applied to all 50000 cells" in note


def test_a_single_cluster_never_wins_however_stable_it_looks():
    # A one-cluster partition reproduces exactly under any resample, so its ARI is 1.0 and it
    # clears every floor. It must still lose: "1 cluster, perfectly stable" is the absence of a
    # clustering, and on weak data it would otherwise beat every real candidate.
    def one_cluster_below(threshold):
        rng = np.random.default_rng(7)

        def leiden(ad, resolution, random_state=0, key_added="leiden"):
            if resolution < threshold:
                ad.obs[key_added] = np.zeros(ad.n_obs, dtype=int)      # everything in one group
            else:
                ad.obs[key_added] = rng.integers(0, 12, size=ad.n_obs)  # unstable
        return types.SimpleNamespace(tl=types.SimpleNamespace(leiden=leiden),
                                     pp=types.SimpleNamespace(neighbors=lambda *a, **k: None))

    res, sweep, note = _select(_AData(400), one_cluster_below(0.6))
    by_res = {r["resolution"]: r for r in sweep}
    assert by_res[0.2]["n_clusters"] == 1 and by_res[0.2]["stability"] == 1.0
    assert res != 0.2                       # the trivially-stable degenerate one does not win
    assert "LESS reproducible" in note      # and the weakness of what DID win is stated
    # the degenerate rows stay in the table — seeing them is how the run gets diagnosed
    assert any(r["n_clusters"] == 1 for r in sweep)


def test_no_structure_at_any_resolution_is_reported_as_such():
    def always_one(ad, resolution, random_state=0, key_added="leiden"):
        ad.obs[key_added] = np.zeros(ad.n_obs, dtype=int)

    sc = types.SimpleNamespace(tl=types.SimpleNamespace(leiden=always_one),
                               pp=types.SimpleNamespace(neighbors=lambda *a, **k: None))
    _res, _sweep, note = _select(_AData(200), sc)
    assert "single cluster" in note and "no population structure" in note


def test_stability_floor_is_honoured_as_given():
    # A laxer floor admits a finer resolution; the threshold is a real knob, not decoration.
    strict, _, _ = _select(_AData(400), _fake_sc(unstable_at=0.8), stability_min=0.99)
    assert strict == 0.6
    lax, sweep, _ = _select(_AData(400), _fake_sc(unstable_at=1.0), stability_min=0.90)
    assert lax == 0.8 and {r["resolution"] for r in sweep} == {0.2, 0.4, 0.6, 0.8, 1.0}
