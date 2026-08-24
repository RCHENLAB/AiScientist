"""The depth-matched check as a TOOL, because three models could not write it as code.

A plan asked the executor to "correlate per-gene logFC against the between-arm median
nCount_RNA". That is not computable — a per-gene vector has no second variable in a single
scalar — and the step failed on every attempt in two production runs, leaving the report's
central claim (is the pan-cell-type translation signature biology or depth?) unanswered.
"""

from __future__ import annotations

import numpy as np
import pytest

from bioagent.agents.registry import _HPC_ANALYSIS_TOOLS
from bioagent.tools.scrna_pack import (
    PARAMS,
    TOOL_SUMMARY,
    _depth_match_targets,
    _spearman,
    scrna_catalog,
)


# --- the matcher ------------------------------------------------------------------------------

def test_the_deeper_arm_lands_on_the_shallower_arms_distribution():
    deep = np.array([1000.0, 2000.0, 3000.0, 4000.0])
    shallow = np.array([500.0, 1000.0, 1500.0, 2000.0])
    target = _depth_match_targets(deep, shallow)
    assert np.median(target) == pytest.approx(np.median(shallow), rel=0.05)


def test_a_cell_is_never_sampled_UP_to_counts_it_never_had():
    """Down-sampling discards molecules; it cannot invent them. The cap also makes the check
    conservative — any residual imbalance runs against calling a signal depth-driven."""
    deep = np.array([100.0, 200.0, 300.0])
    shallow = np.array([5000.0, 6000.0, 7000.0])       # the "deeper" arm is actually shallower
    assert (_depth_match_targets(deep, shallow) <= deep).all()


def test_each_cell_keeps_its_place_in_its_own_arm():
    """Quantile matching, not "everyone to the median": flattening every cell to one number
    would destroy the within-arm variation the rank test reads."""
    deep = np.array([4000.0, 1000.0, 3000.0, 2000.0])
    target = _depth_match_targets(deep, np.array([500.0, 1000.0, 1500.0, 2000.0]))
    assert list(np.argsort(target)) == list(np.argsort(deep))


def test_an_empty_arm_is_returned_untouched_rather_than_crashing():
    deep = np.array([10.0, 20.0])
    assert list(_depth_match_targets(deep, np.array([]))) == [10.0, 20.0]


# --- the correlation --------------------------------------------------------------------------

def test_spearman_reports_an_inversion_as_a_negative_number():
    """The verdict that matters: a ranking that REVERSES once depth is equalised is a detection
    artefact, and rho must be able to say so — an absolute-value or 0..1 measure could not."""
    assert _spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
    assert _spearman([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(1.0)


def test_spearman_averages_ties_the_way_scipy_does():
    assert _spearman([1, 1, 2, 3], [1, 1, 2, 3]) == pytest.approx(1.0)


def test_too_few_points_or_no_variance_returns_none_not_a_fake_correlation():
    assert _spearman([1, 2], [2, 1]) is None
    assert _spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None


# --- the tool is actually reachable -----------------------------------------------------------

def test_the_tool_is_in_the_catalog_with_its_defaults():
    tool = next((t for t in scrna_catalog() if t.name == "run_depth_matched_de"), None)
    assert tool is not None
    assert PARAMS["run_depth_matched_de"]["min_ratio"][0] == 1.05
    assert "run_depth_matched_de" in TOOL_SUMMARY


def test_it_runs_where_the_checkpoints_live():
    """It reads work/adata_qc.h5ad. Six tools once ran in-process on the gateway, whose work/ is
    empty when QC ran as a Slurm job — that is how run_composition failed three rounds running."""
    assert "run_depth_matched_de" in _HPC_ANALYSIS_TOOLS


def test_the_container_cli_can_dispatch_it():
    from bioagent.tools import scrna_cli
    assert "run_depth_matched_de" in scrna_cli._analysis_tools()


def test_the_description_warns_the_model_off_writing_it_by_hand():
    tool = next(t for t in scrna_catalog() if t.name == "run_depth_matched_de")
    assert "run_code" in tool.description and "not a computable operation" in tool.description
