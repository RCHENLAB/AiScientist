"""The plan review must not let a MISSING primary analysis through silently.

Regression cases are real drafts: Laguna-S-2.1 produced a differential-expression plan with QC, a
depth-matched check, composition and two enrichment steps — and no DE step. The enrichment steps
read a DE table no step wrote. Measured before this guard existed, the Critic named the omission
in 10 of 10 reviews and the step still reached the final agenda about half the time: the loss was
in the handoff (Critic prose -> Critic revised_agenda -> PI), not in the detection.
"""
from bioagent.agents.research_lab import _missing_analysis_family, _names_tool

DE_MISSING = ("MISSING PRIMARY ANALYSIS: The plan lacks a step to perform the actual differential "
              "expression (DE) analysis. Steps 3, 5 and 6 reference 'depth-matched DE tables' but "
              "no step computes the Wilcoxon rank-sum test.")

# The draft the guard exists for. It names four analysis tools — which is why an earlier version of
# this check, asking only whether ANY analysis tool was named, never fired once.
DRAFT_WITHOUT_DE = [
    "**QC & normalization** — run `run_scanpy_qc` to filter cells.",
    "**Depth-matched check** — run `run_depth_matched_de` because the arms differ 1.6x.",
    "**Composition** — run `run_composition` for per-class proportions.",
    "**Pathway interpretation** — run `run_enrichment` and `run_gsea_prerank` on the DE tables.",
]


def test_a_missing_de_step_resolves_to_the_de_tool_family():
    assert _missing_analysis_family([DE_MISSING]) == ("run_de", "run_pseudobulk_de")


def test_the_draft_that_motivated_this_does_not_satisfy_the_family():
    family = _missing_analysis_family([DE_MISSING])
    assert family is not None
    # It names run_scanpy_qc / run_depth_matched_de / run_composition / run_enrichment ...
    assert _names_tool(DRAFT_WITHOUT_DE, ("run_scanpy_qc", "run_composition"))
    # ... and still fails the family the Critic actually asked for. This is the whole point.
    assert not _names_tool(DRAFT_WITHOUT_DE, family)


def test_adding_the_de_step_satisfies_it():
    family = _missing_analysis_family([DE_MISSING])
    fixed = DRAFT_WITHOUT_DE + [
        "**Per-cell-type contrast** — run `run_de` with groupby='sampleid', reference='WT'.",
    ]
    assert _names_tool(fixed, family)


def test_a_complaint_about_an_existing_step_is_not_a_missing_analysis():
    """Narrowness matters more than coverage here: a false positive burns an extra PI call on
    every review, and 'should also report X' is the commonest shape of Critic prose."""
    for benign in ("The DE step should also report effect sizes.",
                   "Step 3 names run_de but does not state the reference level.",
                   "The enrichment step should name its gene-set library."):
        assert _missing_analysis_family([benign]) is None


def test_other_families_resolve_too():
    cases = {
        "No step computes cell-type composition, yet step 6 promises a proportion chart.":
            ("run_composition",),
        "The plan lacks any pathway enrichment, so the GSEA figure has no input.":
            ("run_enrichment", "run_gsea_prerank"),
        "There is no quality-control step; filtering is never computed.":
            ("run_scanpy_qc",),
    }
    for issue, family in cases.items():
        assert _missing_analysis_family([issue]) == family, issue


def test_the_first_matching_family_wins_when_an_issue_names_several():
    """A Critic issue often mentions the downstream step that broke ('enrichment reads a DE table
    nothing wrote'). The DE family is checked first because it is the one reported as absent."""
    issue = ("MISSING: no differential expression step exists, so the enrichment and pathway "
             "steps have no input table.")
    assert _missing_analysis_family([issue]) == ("run_de", "run_pseudobulk_de")
