"""The depth-matched check as a TOOL, because three models could not write it as code.

A plan asked the executor to "correlate per-gene logFC against the between-arm median
nCount_RNA". That is not computable — a per-gene vector has no second variable in a single
scalar — and the step failed on every attempt in two production runs, leaving the report's
central claim (is the pan-cell-type translation signature biology or depth?) unanswered.
"""

from __future__ import annotations

import numpy as np
import pytest

from aiscientist.agents.registry import _HPC_ANALYSIS_TOOLS
from aiscientist.tools._lib.scrna import PARAMS, TOOL_SUMMARY
from aiscientist.tools.run_depth_matched_de.tool import (
    _DEPTH_ROBUST_MIN_FRACTION,
    _depth_match_targets,
    _depth_robust_genes,
    _depth_verdict,
    _spearman,
    _with_depth_direction,
)
from aiscientist.tools.catalog import scrna_catalog


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
    from aiscientist.tools import scrna_cli
    assert "run_depth_matched_de" in scrna_cli._analysis_tools()


def test_the_description_warns_the_model_off_writing_it_by_hand():
    tool = next(t for t in scrna_catalog() if t.name == "run_depth_matched_de")
    assert "run_code" in tool.description and "not a computable operation" in tool.description


# --- the direction asymmetry ------------------------------------------------------------------
# Depth inflates detection in the DEEPER arm, so it can only manufacture apparent up-regulation
# there. Treating both directions with one rule is what let a run report "0 of 8 rankings
# survived" about genes the gradient could never have produced. See scrna_pack for the story.

def test_the_gradient_pushes_up_in_whichever_arm_is_deeper():
    assert _with_depth_direction(deeper_is_test=True) == "up"      # test arm deeper -> fake "up"
    assert _with_depth_direction(deeper_is_test=False) == "down"   # ref arm deeper  -> fake "down"


def test_a_ranking_that_runs_against_the_gradient_is_never_called_an_artefact():
    # Same collapsed rho, opposite meaning. WITH the gradient it is evidence of an artefact;
    # AGAINST it, depth cannot have produced the genes — and down-sampling cannot confirm them
    # either, so the tool reports that it has no verdict rather than a false one.
    assert _depth_verdict(0.03, against_depth=False) == "weak"
    assert _depth_verdict(0.03, against_depth=True) == "against_depth_untestable"
    assert _depth_verdict(-0.4, against_depth=False) == "inverted"
    assert _depth_verdict(-0.4, against_depth=True) == "against_depth_untestable"


def test_a_surviving_ranking_is_preserved_whichever_way_it_runs():
    assert _depth_verdict(0.8, against_depth=False) == "preserved"
    assert _depth_verdict(0.8, against_depth=True) == "preserved"


def test_an_uncomputable_rho_is_not_reported_as_an_inversion():
    assert _depth_verdict(None, against_depth=False) == "weak"


# --- what "depth-robust" means ------------------------------------------------------------------

def test_a_gene_whose_effect_collapses_is_not_robust_even_though_its_sign_holds():
    # The counter-example that ruled out a sign-only rule: 3.0 -> 0.02 keeps the sign and means
    # nothing.
    assert _depth_robust_genes(["A"], {"A": 3.0}, {"A": 0.02}) == []


def test_a_gene_that_flips_sign_is_not_robust_however_large_it_stays():
    assert _depth_robust_genes(["A"], {"A": -2.0}, {"A": 2.0}) == []


def test_a_gene_that_keeps_its_sign_and_most_of_its_effect_is_robust():
    # ...even though a strict top-N rank rule would have dropped it. This is the case that used to
    # return 0 of 400.
    assert _depth_robust_genes(["A"], {"A": -1.5}, {"A": -1.4}) == ["A"]   # 93% of the effect
    assert _depth_robust_genes(["A"], {"A": -1.5}, {"A": -1.3}) == ["A"]   # 87%, still over 0.8
    assert _depth_robust_genes(["A"], {"A": -1.5}, {"A": -1.1}) == []      # 73%, under the floor


def test_a_gene_missing_from_the_matched_ranking_is_not_silently_robust():
    assert _depth_robust_genes(["A", "B"], {"A": 1.0, "B": 1.0}, {"A": 0.9}) == ["A"]


def test_the_robustness_floor_is_a_fraction_of_the_original_effect():
    assert _depth_robust_genes(["A"], {"A": 4.0}, {"A": 1.0}, min_fraction=0.2) == ["A"]
    assert _depth_robust_genes(["A"], {"A": 4.0}, {"A": 1.0}, min_fraction=0.5) == []


def test_the_z_floor_is_what_separates_signal_from_background():
    # The log2fc floor passes a gene whose effect is only retained because down-sampling pushed
    # it down; the Wilcoxon z regresses toward zero for noise and holds for a real effect. Both
    # rules are kept: the z one is used whenever the statistic is available.
    orig, matched = {"A": -1.0}, {"A": -1.0}
    assert _depth_robust_genes(["A"], orig, matched) == ["A"]                       # log2fc rule
    assert _depth_robust_genes(["A"], orig, matched,
                               {"A": -20.0}, {"A": -2.0}) == []                     # z collapsed
    assert _depth_robust_genes(["A"], orig, matched,
                               {"A": -20.0}, {"A": -18.0}) == ["A"]                 # z held


def test_the_threshold_is_the_one_the_control_selected():
    # Swept in experiments/depth_matched_validation; see the constant's comment for the numbers.
    assert _DEPTH_ROBUST_MIN_FRACTION == 0.8
