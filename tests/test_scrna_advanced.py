"""The steps the analysis line was missing (tools/scrna_advanced).

scanpy is in the `analysis` extra and absent on most hosts, so these tests drive the tools
with a stand-in `sc` over small real numpy/pandas objects. That is enough to pin the things
that actually decide whether an answer is right:

  * pseudobulk REFUSES when there are too few samples, instead of degrading to a cell-level
    test — the refusal is the feature;
  * a sample that carries two condition values is an error, not something to average over;
  * summing a checkpoint with no raw counts is refused rather than done on log values;
  * composition is tested on CLR, not raw proportions;
  * annotation lets raw expression overrule the z-argmax, and leaves incoherent clusters alone.
"""

from __future__ import annotations

import builtins
import json
import types

import numpy as np
import pytest

# pandas ships in the `analysis` extra, like scanpy — same convention the h5py-backed tests use.
pd = pytest.importorskip("pandas")

from aiscientist.tools._lib import scrna as scrna_lib  # noqa: E402
from aiscientist.tools.run_clustering import tool as run_clustering_tool
from aiscientist.tools.run_composition import tool as run_composition_tool
from aiscientist.tools.run_de import tool as run_de_tool
from aiscientist.tools.run_depth_matched_de import tool as run_depth_matched_de_tool
from aiscientist.tools.run_doublet_detection import tool as run_doublet_detection_tool
from aiscientist.tools.run_integration import tool as run_integration_tool
from aiscientist.tools.run_marker_annotation import tool as run_marker_annotation_tool
from aiscientist.tools.run_pseudobulk_de import tool as run_pseudobulk_de_tool
from aiscientist.tools.run_scanpy_qc import tool as run_scanpy_qc_tool


def _ctx(tmp_path):
    return types.SimpleNamespace(workspace=tmp_path, decisions={})


class _AnnData:
    """Enough AnnData for these tools: obs/var_names/X/layers/raw + boolean-mask slicing."""

    def __init__(self, X, obs, var_names, layers=None, raw=None):
        self.X = X
        self.obs = obs
        self.var_names = pd.Index(var_names)
        self.var = pd.DataFrame(index=self.var_names)
        self.layers = layers or {}
        self.raw = raw
        self.obsm = {}
        self.uns = {}
        self.written_to = None

    @property
    def n_obs(self): return self.X.shape[0]

    @property
    def n_vars(self): return self.X.shape[1]

    @property
    def obs_names(self): return self.obs.index

    def __getitem__(self, key):
        rows = key[0] if isinstance(key, tuple) else key
        rows = np.asarray(rows)
        if rows.dtype != bool:
            rows = np.isin(np.arange(self.n_obs), rows)
        return _AnnData(self.X[rows], self.obs.loc[rows], self.var_names,
                        {k: v[rows] for k, v in self.layers.items()}, self.raw)

    def copy(self): return self

    def write(self, path): self.written_to = path


def _make(n_cells=40, n_genes=6, seed=0, genes=None, **obs_cols):
    rng = np.random.default_rng(seed)
    genes = list(genes) if genes else [f"G{i}" for i in range(n_genes)]
    X = rng.poisson(5, size=(n_cells, len(genes))).astype(float)
    obs = pd.DataFrame(obs_cols, index=[f"cell{i}" for i in range(n_cells)])
    return _AnnData(X, obs, genes, layers={"counts": X.copy()})


def _install_sc(monkeypatch, adata):
    """A scanpy stand-in that hands back `adata` from read_h5ad."""
    sc = types.SimpleNamespace(
        read_h5ad=lambda p: adata,
        get=types.SimpleNamespace(),
        tl=types.SimpleNamespace(),
        pp=types.SimpleNamespace(),
    )
    for _mod in (scrna_lib, run_clustering_tool, run_composition_tool, run_de_tool, run_depth_matched_de_tool, run_doublet_detection_tool, run_integration_tool, run_marker_annotation_tool, run_pseudobulk_de_tool, run_scanpy_qc_tool):  # every module that looks `_import_scanpy` up
        monkeypatch.setattr(_mod, "_import_scanpy", lambda: sc)
    return sc


def _touch(tmp_path, name):
    d = tmp_path / "work"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("", encoding="utf-8")


# --- pseudobulk: the refusals are the feature ---------------------------------


def test_pseudobulk_refuses_when_there_is_no_replication(tmp_path, monkeypatch):
    # One donor per arm. There is no valid test at this replication, and the ONLY safe answer
    # is to say so — falling back to a cell-level Wilcoxon would report thousands of
    # "significant" genes that are really one donor differing from another.
    ad = _make(sample=["S1"] * 20 + ["S2"] * 20, cond=["ctrl"] * 20 + ["dis"] * 20)
    _install_sc(monkeypatch, ad)
    _touch(tmp_path, "adata_qc.h5ad")

    out = run_pseudobulk_de_tool.run_pseudobulk_de(
        {"sample_key": "sample", "condition_key": "cond"}, _ctx(tmp_path))

    assert out["status"] == "error"
    assert "replicates" in out["note"] or "replication" in out["note"]
    assert "all_cells" in out["skipped"]
    assert "NOT falling back" in out["skipped"]["all_cells"]


def test_pseudobulk_runs_with_replication_and_names_the_unit(tmp_path, monkeypatch):
    ad = _make(n_cells=80,
               sample=sum(([f"S{i}"] * 20 for i in range(4)), []),
               cond=["ctrl"] * 40 + ["dis"] * 40)
    _install_sc(monkeypatch, ad)
    _touch(tmp_path, "adata_qc.h5ad")

    out = run_pseudobulk_de_tool.run_pseudobulk_de(
        {"sample_key": "sample", "condition_key": "cond"}, _ctx(tmp_path))

    assert out["status"] == "ok"
    # The result states what a reader needs to judge it: what a replicate WAS, and how many.
    assert out["unit_of_replication"] == "sample"
    res = out["results_by_group"]["all_cells"]
    assert res["n_samples"] == {"ctrl": 2, "dis": 2}
    assert res["contrast"] == "dis vs ctrl"
    assert (tmp_path / "artifacts" / "tables" / "pseudobulk_all.csv").exists()


def test_pseudobulk_rejects_a_sample_spanning_two_conditions(tmp_path, monkeypatch):
    # A replicate belongs to exactly one arm. If one doesn't, the design is mislabeled and
    # aggregating it would silently blend the arms into a single pseudobulk profile.
    ad = _make(sample=["S1"] * 40, cond=["ctrl"] * 20 + ["dis"] * 20)
    _install_sc(monkeypatch, ad)
    _touch(tmp_path, "adata_qc.h5ad")

    out = run_pseudobulk_de_tool.run_pseudobulk_de(
        {"sample_key": "sample", "condition_key": "cond"}, _ctx(tmp_path))

    assert out["status"] == "error"
    assert "more than one value" in out["error"]


def test_pseudobulk_refuses_a_checkpoint_without_raw_counts(tmp_path, monkeypatch):
    # Summing log-normalized values is not aggregation. Better to refuse than to return a
    # confident, meaningless profile.
    ad = _make(n_cells=80,
               sample=sum(([f"S{i}"] * 20 for i in range(4)), []),
               cond=["ctrl"] * 40 + ["dis"] * 40)
    ad.layers = {}
    _install_sc(monkeypatch, ad)
    _touch(tmp_path, "adata_qc.h5ad")

    out = run_pseudobulk_de_tool.run_pseudobulk_de(
        {"sample_key": "sample", "condition_key": "cond"}, _ctx(tmp_path))

    assert out["status"] == "error"
    assert "no raw-count layer" in out["error"]
    assert "run_scanpy_qc" in out["error"]


def test_pseudobulk_keeps_underpowered_groups_visible(tmp_path, monkeypatch):
    # Cell type B has one sample per arm. Its skip must be REPORTED, not silently omitted —
    # "we could not test B" is a finding about the study, not an implementation detail.
    ad = _make(n_cells=120,
               sample=sum(([f"S{i}"] * 20 for i in range(6)), []),
               cond=["ctrl"] * 60 + ["dis"] * 60,
               ct=(["A"] * 20 + ["A"] * 20 + ["B"] * 20) * 2)
    _install_sc(monkeypatch, ad)
    _touch(tmp_path, "adata_qc.h5ad")

    out = run_pseudobulk_de_tool.run_pseudobulk_de(
        {"sample_key": "sample", "condition_key": "cond", "group_key": "ct"}, _ctx(tmp_path))

    assert out["status"] == "ok"
    assert "A" in out["results_by_group"]
    assert "B" in out["skipped_groups"]


def test_bh_fdr_is_monotone_and_bounded():
    p = [0.001, 0.01, 0.03, 0.2, 0.9]
    q = scrna_lib._bh_fdr(p)
    assert all(0 <= v <= 1 for v in q)
    assert q == sorted(q)                    # monotone in p for an already-sorted input
    assert all(a <= b + 1e-12 for a, b in zip(p, q))   # adjustment never shrinks a p-value
    assert scrna_lib._bh_fdr([]) == []


# --- composition ---------------------------------------------------------------


def test_composition_will_not_test_without_replication(tmp_path, monkeypatch):
    ad = _make(sample=["S1"] * 20 + ["S2"] * 20, cond=["ctrl"] * 20 + ["dis"] * 20,
               leiden=["0", "1"] * 20)
    _install_sc(monkeypatch, ad)
    _touch(tmp_path, "adata_clustered.h5ad")

    out = run_composition_tool.run_composition(
        {"sample_key": "sample", "condition_key": "cond"}, _ctx(tmp_path))

    assert out["status"] == "ok" and out["tested"] is False
    assert "not evidence of an effect" in out["note"]


def test_composition_reports_the_counts_behind_its_percentages(tmp_path, monkeypatch):
    ad = _make(sample=["S1"] * 20 + ["S2"] * 20, cond=["ctrl"] * 20 + ["dis"] * 20,
               leiden=["0", "1"] * 20)
    _install_sc(monkeypatch, ad)
    _touch(tmp_path, "adata_clustered.h5ad")

    out = run_composition_tool.run_composition(
        {"sample_key": "sample", "condition_key": "cond"}, _ctx(tmp_path))

    assert out["cells_by_group_and_arm"] == {"0": {"ctrl": 10, "dis": 10},
                                             "1": {"ctrl": 10, "dis": 10}}
    assert out["pct_by_condition"]["ctrl"]["0"] == 50.0


def test_composition_tests_on_clr_and_returns_the_compositional_caveat(tmp_path, monkeypatch):
    ad = _make(n_cells=80,
               sample=sum(([f"S{i}"] * 20 for i in range(4)), []),
               cond=["ctrl"] * 40 + ["dis"] * 40,
               leiden=["0", "1"] * 40)
    _install_sc(monkeypatch, ad)
    _touch(tmp_path, "adata_clustered.h5ad")

    out = run_composition_tool.run_composition(
        {"sample_key": "sample", "condition_key": "cond"}, _ctx(tmp_path))

    assert out["tested"] is True
    assert "centered log-ratio" in out["method"]
    # The constraint that makes a "decrease" ambiguous travels WITH the result.
    assert "sum to 1" in out["caveat"]
    assert all("pval_adj" in r for r in out["results"])
    assert (tmp_path / "artifacts" / "tables" / "composition_test.csv").exists()


# --- doublets / integration guards ---------------------------------------------


def test_doublets_refuse_without_counts(tmp_path, monkeypatch):
    ad = _make()
    ad.layers = {}
    _install_sc(monkeypatch, ad)
    _touch(tmp_path, "adata_qc.h5ad")
    out = run_doublet_detection_tool.run_doublet_detection({}, _ctx(tmp_path))
    assert out["status"] == "error" and "no raw-count layer" in out["error"]


@pytest.mark.parametrize("message", [
    # scanpy 1.11.5, the version inside analysis.sif (measured on HPC3, 2026-09-30)
    "threshold is None and thus scrublet requires skimage, but skimage is not installed.",
    # scanpy 1.12 names the distribution instead
    "threshold is None and thus scrublet requires scikit-image, but it is not installed.",
])
def test_doublets_without_scikit_image_report_the_missing_dependency(tmp_path, monkeypatch,
                                                                       message):
    """Either scanpy wording must come back as dependency_missing naming scikit-image — an
    opaque error gives the model (and any installer) nothing to act on."""
    sc = _install_sc(monkeypatch, _make())

    def _scrublet(_adata, **_kwargs):
        raise ValueError(message)
    sc.pp.scrublet = _scrublet
    _touch(tmp_path, "adata_qc.h5ad")

    out = run_doublet_detection_tool.run_doublet_detection({}, _ctx(tmp_path))

    assert out["status"] == "dependency_missing"
    assert out["dependency"] == "scikit-image"


def test_integration_refuses_a_single_batch(tmp_path, monkeypatch):
    # One level means there is nothing to correct. Running anyway would burn time and, worse,
    # imply to the reader that a batch effect was handled.
    ad = _make(donor=["D1"] * 40)
    _install_sc(monkeypatch, ad)
    _touch(tmp_path, "adata_qc.h5ad")
    out = run_integration_tool.run_integration({"batch_key": "donor"}, _ctx(tmp_path))
    assert out["status"] == "error"
    assert "nothing to integrate" in out["error"]
    assert out["batch_sizes"] == {"D1": 40}


def test_integration_requires_a_batch_key(tmp_path, monkeypatch):
    _install_sc(monkeypatch, _make(donor=["D1"] * 20 + ["D2"] * 20))
    _touch(tmp_path, "adata_qc.h5ad")
    out = run_integration_tool.run_integration({}, _ctx(tmp_path))
    assert out["status"] == "error" and "batch_key is required" in out["error"]


# --- marker annotation ---------------------------------------------------------


def _annotation_sc(monkeypatch, adata, score_by_cluster, raw_by_cluster):
    """Stand-in scanpy for run_marker_annotation: `score_genes` writes a per-cell column from
    `score_by_cluster`, and `obs_df` returns the raw means from `raw_by_cluster`."""
    clusters = adata.obs["leiden"].astype(str).values

    def score_genes(ad, genes, score_name, use_raw=True):
        ct = score_name[len("score_"):]
        ad.obs[score_name] = [score_by_cluster[c][ct] for c in clusters]

    def obs_df(ad, keys, use_raw=True):
        gene_keys = [k for k in keys if k != "leiden"]
        data = {g: [raw_by_cluster[c].get(g, 0.0) for c in clusters] for g in gene_keys}
        data["leiden"] = clusters
        return pd.DataFrame(data, index=ad.obs.index)

    sc = _install_sc(monkeypatch, adata)
    sc.tl.score_genes = score_genes
    sc.get.obs_df = obs_df
    return sc


def test_raw_expression_overrules_the_z_argmax_and_the_correction_is_reported(tmp_path, monkeypatch):
    # Cluster "0" scores highest for DC (shared LAMP3 inflates it), but the RAW discriminators
    # say AT2. The raw check must win, and the disagreement must survive into the output —
    # that disagreement is evidence about the data, not noise to tidy away.
    # Three clusters, because the z-score is per CELL TYPE across clusters: with only two
    # clusters every column peaks by the same number of s.d. and the argmax is a coin flip.
    # Here DC's score is sharply peaked on cluster 0 while AT2's is flat, so the first pass
    # calls cluster 0 "DC" — which is exactly the shared-marker trap (a DC panel riding on
    # LAMP3 that AT2 also expresses).
    ad = _make(n_cells=30, genes=["SFTPC", "CLEC9A"],
               leiden=["0"] * 10 + ["1"] * 10 + ["2"] * 10)
    scores = {"0": {"AT2": 0.4, "DC": 0.9},
              "1": {"AT2": 0.3, "DC": 0.1},
              "2": {"AT2": 0.2, "DC": 0.1}}
    raw = {"0": {"SFTPC": 3.0, "CLEC9A": 0.05},     # raw says AT2, unambiguously
           "1": {"SFTPC": 0.02, "CLEC9A": 2.0},
           "2": {"SFTPC": 2.5, "CLEC9A": 0.03}}
    _annotation_sc(monkeypatch, ad, scores, raw)
    _touch(tmp_path, "adata_de.h5ad")

    out = run_marker_annotation_tool.run_marker_annotation({
        "panel": {"AT2": ["SFTPC"], "DC": ["CLEC9A"]},
        "discriminators": {"AT2": ["SFTPC"], "DC": ["CLEC9A"]},
    }, _ctx(tmp_path))

    assert out["status"] == "ok"
    assert out["labels"]["0"] == "AT2"          # raw wins over the score argmax
    assert "0" in out["corrected_by_raw_check"]
    saved = json.loads((tmp_path / "artifacts" / "tables" / "cluster_cell_types.json")
                       .read_text(encoding="utf-8"))
    assert saved["0"]["first_pass_label"] == "DC"    # both calls kept
    assert saved["0"]["cell_type"] == "AT2"


def test_a_cluster_with_no_dominant_signal_stays_unassigned(tmp_path, monkeypatch):
    # Two lineages tie. Forcing a label here is how a doublet or low-quality cluster becomes a
    # named cell type in a figure.
    ad = _make(n_cells=10, genes=["SFTPC", "CLEC9A"], leiden=["0"] * 10)
    _annotation_sc(monkeypatch, ad, {"0": {"AT2": 0.5, "DC": 0.5}},
                   {"0": {"SFTPC": 1.0, "CLEC9A": 1.0}})
    _touch(tmp_path, "adata_de.h5ad")

    out = run_marker_annotation_tool.run_marker_annotation({
        "panel": {"AT2": ["SFTPC"], "DC": ["CLEC9A"]},
        "discriminators": {"AT2": ["SFTPC"], "DC": ["CLEC9A"]},
    }, _ctx(tmp_path))

    assert out["labels"]["0"] == "Unassigned"
    assert out["unassigned_clusters"] == ["0"]


def test_annotation_requires_a_panel_and_says_why(tmp_path, monkeypatch):
    _install_sc(monkeypatch, _make(leiden=["0"] * 40))
    _touch(tmp_path, "adata_de.h5ad")
    out = run_marker_annotation_tool.run_marker_annotation({}, _ctx(tmp_path))
    assert out["status"] == "error"
    assert "panel" in out["error"] and "another tissue" in out["error"]


def test_missing_scanpy_is_graceful_for_every_new_tool(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.split(".")[0] in {"scanpy", "anndata", "matplotlib"}:
            raise ImportError(name=name.split(".")[0])
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    for fn in (run_doublet_detection_tool.run_doublet_detection, run_integration_tool.run_integration,
               run_pseudobulk_de_tool.run_pseudobulk_de, run_composition_tool.run_composition,
               run_marker_annotation_tool.run_marker_annotation):
        out = fn({}, _ctx(tmp_path))
        assert out["status"] == "dependency_missing", fn.__name__


# --- `input`: a file a run_code step wrote, instead of the usual checkpoint ------------------------


def _scrublet_stub(sc):
    def _scrublet(adata, **_kwargs):
        adata.obs["doublet_score"] = np.linspace(0.0, 1.0, adata.n_obs)
        adata.obs["predicted_doublet"] = adata.obs["doublet_score"] > 0.95
    sc.pp.scrublet = _scrublet


def test_doublets_on_an_input_file_are_written_back_to_it_not_to_the_qc_checkpoint(
        tmp_path, monkeypatch):
    """In-place tools annotate the file they READ. Writing the caller's own object over
    adata_qc.h5ad would be exactly the checkpoint overwrite `input` exists to make unnecessary."""
    ad = _make()
    _scrublet_stub(_install_sc(monkeypatch, ad))
    _touch(tmp_path, "adata_qc.h5ad")
    _touch(tmp_path, "mine.h5ad")

    out = run_doublet_detection_tool.run_doublet_detection({"input": "mine.h5ad", "filter": False},
                                               _ctx(tmp_path))

    assert out["status"] == "ok"
    assert ad.written_to.name == "mine.h5ad"
    assert out["read_from"] == "work/mine.h5ad" and out["checkpoint"] == "work/mine.h5ad"


def test_doublets_without_input_still_update_the_qc_checkpoint(tmp_path, monkeypatch):
    ad = _make()
    _scrublet_stub(_install_sc(monkeypatch, ad))
    _touch(tmp_path, "adata_qc.h5ad")

    out = run_doublet_detection_tool.run_doublet_detection({"filter": False}, _ctx(tmp_path))

    assert ad.written_to.name == "adata_qc.h5ad"
    assert out["checkpoint"] == "adata_qc.h5ad" and out["read_from"] == "work/adata_qc.h5ad"


def test_composition_reads_the_input_file(tmp_path, monkeypatch):
    ad = _make(ct=["A"] * 20 + ["B"] * 20)
    read: list[str] = []
    sc = _install_sc(monkeypatch, ad)
    sc.read_h5ad = lambda p: (read.append(p.name), ad)[1]
    _touch(tmp_path, "adata_qc.h5ad")
    _touch(tmp_path, "relabelled.h5ad")

    out = run_composition_tool.run_composition({"group_key": "ct", "input": "relabelled.h5ad"},
                                         _ctx(tmp_path))

    assert out["status"] == "ok"
    assert read == ["relabelled.h5ad"] and out["read_from"] == "work/relabelled.h5ad"


def test_an_input_outside_the_run_is_refused_by_every_advanced_tool(tmp_path, monkeypatch):
    _install_sc(monkeypatch, _make())
    for fn in (run_doublet_detection_tool.run_doublet_detection, run_integration_tool.run_integration,
               run_pseudobulk_de_tool.run_pseudobulk_de, run_composition_tool.run_composition,
               run_marker_annotation_tool.run_marker_annotation):
        out = fn({"input": "/tmp/somewhere_else.h5ad", "batch_key": "b", "sample_key": "s",
                  "condition_key": "c", "panel": {"X": ["G0"]}}, _ctx(tmp_path))
        assert out["status"] == "error" and "outside" in out["error"], fn.__name__
