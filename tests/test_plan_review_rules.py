"""The plan-review Critic's checklist, extended by what it actually let through.

The pre-flight review is the only layer that can catch an incoherent plan before compute is
spent, and it IS on by default. A live all-Qwen plan still shipped with: a step specifying an
uncomputable operation, a promised figure no step computed, a description contradicting its own
named tool, and a false factual premise inside a literature query. Three of those four were not on
the checklist at all.
"""

from __future__ import annotations

from aiscientist.agents.research_lab import _PLAN_REVIEW_CRITIC_SYSTEM as CHECKLIST


def test_it_asks_whether_each_operation_can_be_computed_at_all():
    assert "COMPUTABLE" in CHECKLIST
    assert "run_depth_matched_de" in CHECKLIST, "name the computable alternative, not just the fault"


def test_it_asks_whether_every_promised_output_is_produced_by_some_step():
    assert "THE OUTPUTS ARE PRODUCED" in CHECKLIST
    assert "run_composition" in CHECKLIST


def test_it_asks_whether_a_step_describes_what_its_named_tool_does():
    assert "THE DESCRIPTION MATCHES THE TOOL" in CHECKLIST
    assert "run_gsea_prerank" in CHECKLIST


def test_a_literature_step_must_ask_rather_than_assert():
    """The model cannot be given knowledge it lacks, but it can be stopped from asserting a
    specific falsehood as the premise of its own search."""
    assert "LITERATURE STEPS ASK" in CHECKLIST


def test_the_profile_counts_as_already_produced():
    """A run_code step re-deriving obs columns the dataset profile already states is not analysis."""
    assert "DATASET PROFILE already states" in CHECKLIST


def test_the_review_is_still_on_by_default():
    from aiscientist.agents.research_lab import LabConfig
    assert LabConfig().plan_review is True
