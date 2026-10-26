"""The plan's own-judgement disclosure reaches the answer and the report.

Prod run 74873590ca7b (2026-10-23): the PI's SELF-SOURCED disclosure said the 14-gene interferon set
was "a team choice, not a named published signature", and the QC thresholds were its own. That record
reached plan.md and run_state.json only; the answer called the set "a frozen 14-gene
interferon-stimulated gene (ISG) set" and the report never said who chose it. Scripted models.
"""

from __future__ import annotations

from aiscientist.agents import own_choices
from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.research_lab import (
    CriticVerdict,
    LabConfig,
    LabRound,
    ResearchLab,
    ResumeState,
)

# The run's disclosure, verbatim (run_state.json "self_sourced").
DISCLOSURE = (
    "The 14-gene ISG set and the Mx1 rule are a team choice, not a named published signature, frozen "
    "before scoring. The QC thresholds (200 genes, 5% mitochondrial, 3-nucleus gene minimum) are my "
    "choices for single-nucleus data. The depth-binned comparison is an addition beyond the bare score "
    "comparison, made because the 1.6-fold depth imbalance directly confounds an arm difference in "
    "score. I did not verify whether the existing `majorclass` labels were assigned using ISG or stress "
    "genes, which could bias within-class comparisons. The Cliff's delta bands are a convention, not a "
    "biological threshold. Differential expression, depth-matched DE, ambient-RNA checks and literature "
    "are left out because the request asks only for the score comparison.")
NONE = "none — every choice in this plan comes from the guidance and the dataset profile."


def test_the_section_quotes_the_record_once():
    block = own_choices.section(DISCLOSURE)
    assert block.startswith(own_choices.HEADING)
    assert "> The 14-gene ISG set and the Mx1 rule are a team choice, not a named published signature" in block
    md = own_choices.add_section("# Report\n\nBody.", DISCLOSURE)
    assert md.count(own_choices.HEADING) == 1 and md.endswith(block)
    assert own_choices.add_section(md, DISCLOSURE) == md          # a text that has it keeps one copy


def test_nothing_to_disclose_adds_nothing():
    for text in ("", "   ", NONE, "None."):
        assert own_choices.disclosed(text) == ""
        assert own_choices.writer_note(text) == ""
        assert own_choices.add_section("# Report", text) == "# Report"


def test_the_writer_is_told_which_choices_were_the_systems():
    note = own_choices.writer_note(DISCLOSURE)
    assert "not a named published signature" in note
    assert "chosen for this run" in note and "Limitations" in note
    assert "do not copy it as a section of your own" in note


def test_the_report_gets_it_before_the_back_matter():
    from aiscientist.gateway import app as gw

    md = "# Report\n\n## Results\n\nText.\n\n## References\n\n1. A paper.\n"
    out = own_choices.add_section(md, DISCLOSURE, insert=gw._insert_before_backmatter)
    assert out.index(own_choices.HEADING) < out.index("## References")
    assert out.index("## Results") < out.index(own_choices.HEADING)


def test_the_gateway_adds_it_to_every_kind_of_report():
    """One call after the manuscript checks covers the full, the brief and the QC report (the brief
    one is built by code and never sees the synthesis that carries the section)."""
    import inspect

    from aiscientist.gateway import app as gw

    src = inspect.getsource(gw._run_lab)
    checked = src.index("report_md = _correct_manuscript_markers(report_md, result")
    added = src.index("own_choices.add_section(report_md, lab_obj.self_sourced")
    verified = src.index("_verification_statement, report_md")
    assert checked < added < verified


def _round():
    return LabRound(1, 2, "ISG score per nucleus", "S",
                    {"steps": [{"tool": "run_code", "ok": True, "result": {"stdout": "x"}},
                               {"tool": "run_enrichment", "ok": True, "result": {}}],
                     "final_answer": "Scored 15,307 nuclei with the 14-gene set."},
                    CriticVerdict("accept", 0.8, ""))


def _lab(seen):
    def complete(messages):
        seen["writer"] = messages[1]["content"]
        return "# Report\n\nA frozen 14-gene ISG set was scored.\n"

    return ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"), LabConfig(),
                       complete_fn=complete)


def test_the_answer_carries_the_disclosure(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_CLAIM_AUDIT", "0")
    seen: dict[str, str] = {}
    lab = _lab(seen)
    lab._self_sourced = DISCLOSURE
    out = lab._synthesize("Score the ISG signature", [_round()], lambda _e: None)
    assert "CHOICES MADE FOR THIS RUN" in seen["writer"] and "not a named published signature" in seen["writer"]
    assert out.startswith("# Report\n\nA frozen 14-gene ISG set was scored.")
    assert out.rstrip().endswith(own_choices.section(DISCLOSURE).rstrip())


def test_an_answer_with_nothing_disclosed_is_unchanged(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_CLAIM_AUDIT", "0")
    seen: dict[str, str] = {}
    lab = _lab(seen)
    lab._self_sourced = NONE
    out = lab._synthesize("Score the ISG signature", [_round()], lambda _e: None)
    assert "CHOICES MADE FOR THIS RUN" not in seen["writer"]
    assert own_choices.HEADING not in out


def test_a_continued_run_keeps_the_plans_disclosure():
    state = {"agenda": ["**QC** — run `run_scanpy_qc`."], "rounds": [], "self_sourced": DISCLOSURE}
    assert ResumeState.from_run_state(state, 0).self_sourced == DISCLOSURE
    assert ResumeState.from_interrupted(state).self_sourced == DISCLOSURE
    assert ResumeState.from_run_state({"agenda": ["x"], "rounds": []}, 0).self_sourced == ""
