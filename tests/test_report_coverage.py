"""Groups the analysis REFUSED must reach the report — coverage is a result, not a footnote.

On the lab's real Ddx41 data, `run_de` tested 7 of 12 cell types and refused 5 (12-38 cells in one
arm). That refusal is part of what the study found. But `_collect_facts` only recurses into DICTS,
while both producers emit their skips as a LIST (`run_de`) or a flat dict (`run_pseudobulk_de`), so
the grounding block never carried it — the writer saw results for 7 cell types, had no way to know
5 more existed and were refused, and would write a manuscript that reads as though the analysis
covered the dataset.
"""

from __future__ import annotations

from bioagent.agents.research_lab import (
    CriticVerdict,
    LabRound,
    _grounding_facts,
    _uncovered_groups,
    verify_report_facts,
)


def _round(result: dict, *, accepted: bool = True) -> LabRound:
    return LabRound(
        1, 1, "Run DDX41 vs WT per cell type", "Generalist",
        {"steps": [{"tool": "run_de", "ok": True, "result": result}]},
        CriticVerdict("accept" if accepted else "revise", 0.9, ""),
    )


# --- collecting ---------------------------------------------------------------


def test_run_de_style_list_of_skips_is_collected():
    rounds = [_round({
        "status": "ok",
        "skipped_groups": [
            {"group": "HC", "n_condition": 12, "n_reference": 29,
             "reason": "fewer than min_cells=30 in one or both arms"},
            {"group": "RPE", "n_condition": 38, "n_reference": 6,
             "reason": "fewer than min_cells=30 in one or both arms"},
        ],
    })]

    out = _uncovered_groups(rounds)

    assert set(out) == {"HC", "RPE"}
    assert "min_cells=30" in out["HC"]
    assert "condition=12" in out["HC"] and "reference=29" in out["HC"]


def test_pseudobulk_style_dict_of_skips_is_collected():
    """run_pseudobulk_de reports {label: reason} — a different shape, same meaning."""
    rounds = [_round({"status": "ok",
                      "skipped_groups": {"Microglia": "needs >=2 samples in each arm, has {'WT': 1}"}})]

    assert list(_uncovered_groups(rounds)) == ["Microglia"]


def test_a_rejected_step_does_not_contribute_coverage_claims():
    rounds = [_round({"status": "ok", "skipped_groups": [{"group": "HC", "reason": "x"}]},
                     accepted=False)]
    assert _uncovered_groups(rounds) == {}


def test_no_skips_means_nothing_to_say():
    assert _uncovered_groups([_round({"status": "ok"})]) == {}


# --- grounding ----------------------------------------------------------------


def test_grounding_block_names_the_uncovered_groups_and_forbids_claiming_them():
    rounds = [_round({
        "status": "ok", "n_groups": 7,
        "skipped_groups": [{"group": "HC", "n_condition": 12, "n_reference": 29,
                            "reason": "fewer than min_cells=30 in one or both arms"}],
    })]

    block = _grounding_facts(rounds)

    assert "NOT ANALYSED" in block
    assert "HC" in block
    assert "MUST NOT" in block


def test_grounding_block_exists_even_when_there_are_no_numeric_facts():
    """A run whose only reportable fact is what it could NOT do still has to say so."""
    rounds = [_round({"skipped_groups": [{"group": "RGC", "reason": "too few cells"}]})]
    assert "RGC" in _grounding_facts(rounds)


# --- the deterministic audit --------------------------------------------------


def test_a_report_omitting_an_uncovered_group_is_flagged():
    md = "## Results\nWe compared DDX41 vs WT across the major retinal cell classes.\n"

    _out, issues = verify_report_facts(md, {}, {"HC": "too few cells", "RPE": "too few cells"})

    assert len(issues) == 2
    assert all("coverage" in i for i in issues)
    assert any("HC" in i for i in issues)


def test_a_report_that_states_the_gap_is_not_flagged():
    md = ("## Limitations\nHC and RPE were not analysed: fewer than 30 cells in one arm, so no "
          "differential expression was attempted for them.\n")

    _out, issues = verify_report_facts(md, {}, {"HC": "too few cells", "RPE": "too few cells"})

    assert issues == []


def test_the_audit_never_rewrites_the_manuscript():
    """It reports; it does not invent prose. Injecting a sentence no model wrote would put
    unattributable text in a scientific manuscript."""
    md = "## Results\nSeven cell classes were compared.\n"

    out, issues = verify_report_facts(md, {}, {"HC": "too few cells"})

    assert out == md
    assert issues


def test_uncovered_is_optional_so_existing_callers_are_unaffected():
    md = "The analysis used GRCh37.\n"
    out, issues = verify_report_facts(md, {"assembly": "GRCh38"})
    assert "GRCh38" in out and any("assembly" in i for i in issues)
