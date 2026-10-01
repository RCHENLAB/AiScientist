"""A per-cell test across an experimental CONDITION must not run silently.

Built from a production run (Ziyaoma/f5111e1a2382). A retina .h5ad carrying an 11-level
``majorclass`` annotation and a 2-level ``sampleid`` condition was analysed by pooling all 15,307
cells and running ``rank_genes_groups`` across DDX41 vs WT. It returned 100 "highly significant"
markers with adjusted p-values reported as indistinguishable from zero, and the manuscript
presented them as differential expression.

Two things were wrong, and only one of them is about p-values:

* PSEUDOREPLICATION — cells from one animal are not independent observations of that animal's
  condition, so the p-values are anti-conservative by orders of magnitude.
* COMPOSITION — the arms did not have the same cell-type mix (Cone 1.8% vs 5.5%, MG 12.6% vs
  5.9%). Pooling cell types makes that shift indistinguishable from a change in expression, so
  even the RANKING is not interpretable, p-values aside.

A guard for exactly this existed in an earlier build and was lost in a rewrite. These tests are
what keeps it from being lost again.
"""

from __future__ import annotations

import pytest

pytest.importorskip("scanpy")

import anndata as ad  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from bioagent.tools._lib import scrna as scrna_lib  # noqa: E402
from bioagent.tools.run_de import tool as run_de_tool
from bioagent.tools.run_scanpy_qc import tool as run_scanpy_qc_tool


def _ctx(tmp_path, dataset):
    from types import SimpleNamespace
    return SimpleNamespace(workspace=tmp_path, decisions={"dataset_path": str(dataset)})


def _retina_like(tmp_path, *, n_cells=300, sample_levels=("s1",)):
    """The shape of the production dataset: a 2-level condition, an existing cell-type column, and
    a library column whose level count is the whole question."""
    rng = np.random.default_rng(0)
    genes = [f"Gene{i}" for i in range(60)]
    x = rng.poisson(4.0, (n_cells, len(genes))).astype(np.float32) + 1.0
    adata = ad.AnnData(x)
    adata.var_names = genes
    adata.obs_names = [f"c{i}" for i in range(n_cells)]
    adata.obs["sampleid"] = pd.Categorical(
        ["DDX41" if i % 2 else "WT" for i in range(n_cells)])
    adata.obs["majorclass"] = pd.Categorical(
        [["Rod", "Cone", "MG"][i % 3] for i in range(n_cells)])
    adata.obs["orig.ident"] = pd.Categorical(
        [sample_levels[i % len(sample_levels)] for i in range(n_cells)])
    p = tmp_path / "retina.h5ad"
    adata.write(p)
    return p


def _qc(tmp_path, dataset):
    out = run_scanpy_qc_tool.run_scanpy_qc({"min_genes": 1, "min_cells": 1, "max_pct_mt": 100.0},
                                   _ctx(tmp_path, dataset))
    assert out["status"] == "ok"
    return _ctx(tmp_path, dataset)


# --- the refusal -------------------------------------------------------------


def test_pooling_every_cell_type_across_a_condition_is_refused(tmp_path):
    ctx = _qc(tmp_path, _retina_like(tmp_path))
    out = run_de_tool.run_de({"groupby": "sampleid"}, ctx)

    assert out["status"] == "error"
    err = out["error"]
    assert "CONDITION column" in err
    assert "pseudoreplication" in err.lower()
    # The composition confound is the part a reader will not think of on their own.
    assert "composition" in err.lower()
    # All three exits are named, so the model is not left to guess one.
    assert "run_pseudobulk_de" in err and "stratify_by" in err and "force=true" in err
    # It reports the labels the dataset ALREADY has rather than telling the model to find some.
    assert "majorclass" in str(out["celltype_columns"])


def test_the_refusal_states_how_much_replication_there_actually_is(tmp_path):
    """The number that decides which test is legal. A refusal that omits it just moves the
    guesswork one step later."""
    one = run_de_tool.run_de({"groupby": "sampleid"}, _qc(tmp_path, _retina_like(tmp_path)))
    assert "orig.ident" in one["error"]
    assert "1 distinct value" in one["error"]

    many = tmp_path / "many"
    many.mkdir()
    out = run_de_tool.run_de(
        {"groupby": "sampleid"},
        _qc(many, _retina_like(many, sample_levels=("s1", "s2", "s3", "s4"))))
    assert "4 distinct value" in out["error"]


def test_a_dataset_with_no_sample_column_at_all_is_told_so(tmp_path):
    """`orig.ident` holding one value and no sample column existing are the same finding —
    nothing separates the condition from the individual — and both must be said out loud."""
    rng = np.random.default_rng(1)
    adata = ad.AnnData(rng.poisson(4.0, (100, 30)).astype(np.float32) + 1.0)
    adata.var_names = [f"Gene{i}" for i in range(30)]
    adata.obs["condition"] = pd.Categorical(["KO" if i % 2 else "WT" for i in range(100)])
    p = tmp_path / "bare.h5ad"
    adata.write(p)

    out = run_de_tool.run_de({"groupby": "condition"}, _qc(tmp_path, p))
    assert out["status"] == "error"
    assert "no replication information at all" in out["error"]


# --- what the guard must NOT block -------------------------------------------


def test_marker_analysis_on_the_cell_type_column_is_untouched(tmp_path):
    """The valid use of this tool. A guard that also blocked markers would just be turned off."""
    out = run_de_tool.run_de({"groupby": "majorclass"}, _qc(tmp_path, _retina_like(tmp_path)))
    assert out["status"] == "ok"
    assert not any("pseudorepl" in w.lower() for w in out.get("warnings", []))


def test_a_stratified_contrast_runs_but_is_labelled_exploratory(tmp_path):
    """The protocol's documented no-replicates path. Stratifying removes the composition confound
    but not the pseudoreplication, so it runs — carrying the label that keeps its output from being
    written up as inferential DE."""
    out = run_de_tool.run_de(
        {"groupby": "sampleid", "reference": "WT", "stratify_by": "majorclass"},
        _qc(tmp_path, _retina_like(tmp_path)))

    assert out["status"] == "ok"
    assert out["inference"] == "exploratory_ranking"
    joined = " ".join(out["warnings"])
    assert "exploratory ranking" in joined and "not as differential expression" in joined


def test_force_runs_the_pooled_test_and_says_what_it_just_did(tmp_path):
    """`force` is a deliberate override, not a way to make the problem go away: the result carries
    the label so a downstream writer cannot present it as ordinary DE."""
    out = run_de_tool.run_de({"groupby": "sampleid", "force": True},
                            _qc(tmp_path, _retina_like(tmp_path)))

    assert out["status"] == "ok"
    assert out["inference"] == "pseudoreplicated"
    joined = " ".join(out["warnings"])
    assert "force=true" in joined
    assert "composition" in joined and "non-inferential" in joined


# --- the name heuristics -----------------------------------------------------


def test_orig_ident_reads_as_a_library_id_not_a_cell_type():
    """`ident` is a cell-type hint (Seurat's `Idents`), so `orig.ident` matched BOTH heuristics.
    It is a library id essentially always, and letting it read as a cell-type column would have it
    offered as a stratification target."""
    assert scrna_lib._looks_like_condition_column("orig.ident")
    assert not scrna_lib._looks_like_celltype_column("orig.ident")
    assert scrna_lib._looks_like_celltype_column("majorclass")
    assert scrna_lib._looks_like_celltype_column("celltype")
    assert not scrna_lib._looks_like_condition_column("majorclass")


# --- the analyses a labelled two-arm dataset actually needs -------------------
#
# Found by running the protocol's own pipeline end to end on the real DDX41 object rather than
# reading the code: of the three tools the CORRECT plan reaches for, not one had ever been
# exercised on this data, and two were broken for exactly this case.


def test_composition_runs_off_qc_when_the_labels_already_exist(tmp_path):
    """`run_de` was fixed to accept the QC checkpoint; `run_composition` was missed. It demanded
    `run_clustering` — the step the protocol explicitly forbids on a labelled dataset — so the
    analysis a two-arm annotated study needs FIRST refused to run on exactly those studies."""
    from bioagent.tools.run_composition import tool as run_composition_tool
    from bioagent.tools.run_pseudobulk_de import tool as run_pseudobulk_de_tool

    ctx = _qc(tmp_path, _retina_like(tmp_path))
    out = run_composition_tool.run_composition(
        {"group_key": "majorclass", "sample_key": "orig.ident", "condition_key": "sampleid"}, ctx)
    assert out["status"] == "ok", out.get("error")


def test_composition_reports_per_arm_proportions_even_with_one_library(tmp_path):
    """The shift IS the finding, and it has to survive the study being untestable.

    Derived from the sample->condition map, per-arm proportions collapse when a study has one
    library: every cell maps to one arm and the tool returns the pooled composition of the whole
    object. On the real dataset that hid a 3x depletion of Cone cells between the arms — the shift
    that makes a pooled DE result unreadable as expression change.
    """
    from bioagent.tools.run_composition import tool as run_composition_tool
    from bioagent.tools.run_pseudobulk_de import tool as run_pseudobulk_de_tool

    ctx = _qc(tmp_path, _retina_like(tmp_path))          # `orig.ident` holds ONE value
    out = run_composition_tool.run_composition(
        {"group_key": "majorclass", "sample_key": "orig.ident", "condition_key": "sampleid"}, ctx)

    assert out["tested"] is False                         # one library — no test is valid
    pct = out["pct_by_condition"]
    assert set(pct) == {"DDX41", "WT"}, "both arms must appear, not one pooled row"
    assert all(set(v) == {"Rod", "Cone", "MG"} for v in pct.values())
    assert "no biological replication" in out["note"]
    assert "ARE the descriptive finding" in out["note"], \
        "an untested proportion is still the result — the note must not read as a failure"


def test_pseudobulk_names_the_finding_instead_of_blaming_the_metadata(tmp_path):
    """Refusing is correct; the diagnosis was not. A one-library study got 'check the metadata',
    sending the reader after a bug that does not exist — the metadata is fine, the STUDY has no
    replicates, and that is the thing to report."""
    from bioagent.tools.run_composition import tool as run_composition_tool
    from bioagent.tools.run_pseudobulk_de import tool as run_pseudobulk_de_tool

    ctx = _qc(tmp_path, _retina_like(tmp_path))
    out = run_pseudobulk_de_tool.run_pseudobulk_de(
        {"sample_key": "orig.ident", "condition_key": "sampleid", "group_key": "majorclass"}, ctx)

    assert out["status"] == "error"
    assert "no biological replication at all" in out["error"]
    assert "Nothing here needs fixing in the metadata" in out["error"]
    assert "run_de" in out["error"], "a refusal must point at the analysis that IS valid"


def test_the_significant_count_is_a_count_not_the_cap(tmp_path):
    """`n_genes` caps the reported TABLE. It was also capping the reported COUNT.

    With the default 50, every cell type with more than 50 significant genes came back as
    "up: 50" — so a write-up could not tell 50 from 4,000, and the run's own total was a sum of
    caps. On the real DDX41 object the true total is 9,483 against a reported 447. Truncating the
    table is right; truncating the count and still calling it a count is not.
    """
    ctx = _qc(tmp_path, _retina_like(tmp_path, n_cells=900))
    out = run_de_tool.run_de(
        {"groupby": "sampleid", "reference": "WT", "stratify_by": "majorclass",
         "n_genes": 2, "padj": 1.1, "lfc": 0},   # padj>1 + no lfc floor = every tested gene
        ctx)

    assert out["status"] == "ok"
    for group, counts in out["significant_by_group"].items():
        assert counts["shown_up"] <= 2 and counts["shown_down"] <= 2, "the table IS capped"
        assert counts["up"] + counts["down"] > 4, \
            f"{group}: the count must survive the cap, got {counts}"
        assert counts["truncated"] is True
    assert out["n_genes_cap_per_direction"] == 2
    assert "TRUE counts" in out["table_truncation_note"]
