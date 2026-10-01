"""Groups the analysis REFUSED must reach the report — coverage is a result, not a footnote.

On the lab's real Ddx41 data, `run_de` tested 7 of 12 cell types and refused 5 (12-38 cells in one
arm). That refusal is part of what the study found. But `_collect_facts` only recurses into DICTS,
while both producers emit their skips as a LIST (`run_de`) or a flat dict (`run_pseudobulk_de`), so
the grounding block never carried it — the writer saw results for 7 cell types, had no way to know
5 more existed and were refused, and would write a manuscript that reads as though the analysis
covered the dataset.
"""

from __future__ import annotations

from aiscientist.agents.research_lab import (
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


def test_skips_name_the_arms_when_the_contrast_says_which_is_which():
    """"condition=7, reference=27" made the writer map roles to arms itself; run 8847d521ba32's
    report has three of its five unequal skipped pairs backwards."""
    rounds = [_round({
        "status": "ok", "reference": "WT",
        "comparison": "sampleid: DDX41 vs WT (reference=WT), stratified by majorclass",
        "skipped_groups": [{"group": "Endothelial", "n_condition": 7, "n_reference": 27,
                            "reason": "fewer than min_cells=30 in one or both arms"}],
    })]

    out = _uncovered_groups(rounds)

    assert "DDX41=7, WT=27" in out["Endothelial"]
    assert "condition=" not in out["Endothelial"]


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


# --- counts: the tested (post-QC) numbers, not the uploaded file's -------------------------------
#
# Run f3731e0b7136's manuscript: "Endothelial (7 DDX41 / 27 WT), HC (5 / 11), … and the Microglia WT
# arm (24 cells) was likewise below the floor". Those were the uploaded file's counts. QC removed 30%
# of nuclei, and run_de had tested and refused 5 / 22, 4 / 9, …, 57 / 21. The claim-audit block had
# handed the writers the file's counts as the only authority.

from aiscientist.agents.research_lab import _tested_counts, correct_coverage_counts, pre_qc_counts  # noqa: E402

_PRE = {"Endothelial": {"DDX41": 7, "WT": 27}, "HC": {"DDX41": 5, "WT": 11},
        "Microglia": {"DDX41": 60, "WT": 24}, "Pericyte": {"DDX41": 4, "WT": 2},
        "RGC": {"DDX41": 6, "WT": 8}, "RPE": {"DDX41": 2, "WT": 2}, "MG": {"DDX41": 787, "WT": 535}}
_TESTED = {"Endothelial": {"DDX41": 5, "WT": 22}, "HC": {"DDX41": 4, "WT": 9},
           "Microglia": {"DDX41": 57, "WT": 21}, "Pericyte": {"DDX41": 3, "WT": 2},
           "RGC": {"DDX41": 5, "WT": 7}, "RPE": {"DDX41": 2, "WT": 2}, "MG": {"DDX41": 396, "WT": 277}}
_REFUSED = {g: "fewer than min_cells=30 in one or both arms"
            for g in ("Endothelial", "HC", "Microglia", "Pericyte", "RGC", "RPE")}
_F373 = ("Endothelial (7 DDX41 / 27 WT), HC (5 / 11), Pericyte (4 / 2), RGC (6 / 8), and RPE (2 / 2) had "
         "both arms below the 30-cell floor, and the Microglia WT arm (24 cells) was likewise below the "
         "floor; no primary compositional or molecular claim is made for these groups.\n")


def test_tested_counts_come_from_the_tools():
    rounds = [_round({"status": "ok", "reference": "WT", "condition": "DDX41",
                      "cells_by_group_and_arm": {"MG": {"DDX41": 396, "WT": 277},
                                                 "Endothelial": {"DDX41": 5, "WT": 22}},
                      "skipped_groups": [{"group": "HC", "n_condition": 4, "n_reference": 9,
                                          "reason": "fewer than min_cells=30"}]}),
              _round({"cells_by_group_and_arm": {"RPE": {"DDX41": 1}}}, accepted=False)]
    assert _tested_counts(rounds) == {"MG": {"DDX41": 396, "WT": 277},
                                      "Endothelial": {"DDX41": 5, "WT": 22},
                                      "HC": {"DDX41": 4, "WT": 9}}


def test_the_grounding_says_its_counts_are_after_qc():
    rounds = [_round({"status": "ok", "reference": "WT", "condition": "DDX41",
                      "skipped_groups": [{"group": "HC", "n_condition": 4, "n_reference": 9,
                                          "reason": "fewer than min_cells=30"}]})]
    block = _grounding_facts(rounds)
    assert "cells after QC: DDX41=4, WT=9" in block
    assert "AFTER QC" in block and "before QC" in block


def test_upload_counts_in_a_coverage_statement_become_the_tested_counts():
    out, issues = verify_report_facts(_F373, {}, _REFUSED, tested_counts=_TESTED, pre_counts=_PRE)
    assert out.startswith("Endothelial (5 DDX41 / 22 WT), HC (4 / 9), Pericyte (3 / 2), RGC (5 / 7), "
                          "and RPE (2 / 2) had both arms below")
    assert "Microglia WT arm (21 cells)" in out
    assert len(issues) == 5 and all(i.startswith("coverage counts:") for i in issues)
    assert not any("RPE" in i for i in issues)          # 2 / 2 before and after QC: nothing to fix


def test_a_sentence_about_the_uploaded_file_keeps_its_counts():
    md = ("The uploaded file held 7 DDX41 / 27 WT Endothelial nuclei before QC, below the floor.\n"
          "Endothelial (7 / 27) was refused.\n")
    out, issues = verify_report_facts(md, {}, _REFUSED, tested_counts=_TESTED, pre_counts=_PRE)
    first, second = out.splitlines()
    assert first == "The uploaded file held 7 DDX41 / 27 WT Endothelial nuclei before QC, below the floor."
    assert second == "Endothelial (7 / 27) was refused."   # no coverage word: untouched
    assert not [i for i in issues if i.startswith("coverage counts:")]


def test_a_report_with_the_tested_counts_is_unchanged():
    md = "Endothelial (5 DDX41 / 22 WT) and HC (4 / 9) fell below the 30-cell floor.\n"
    out, issues = verify_report_facts(md, {}, _REFUSED, tested_counts=_TESTED, pre_counts=_PRE)
    assert out == md and not [i for i in issues if i.startswith("coverage counts:")]


def test_only_a_count_equal_to_the_upload_is_replaced():
    md = "HC (6 / 12) and Endothelial (7 DDX41 / 40 WT) fell below the floor.\n"
    out, _ = verify_report_facts(md, {}, _REFUSED, tested_counts=_TESTED, pre_counts=_PRE)
    assert "HC (6 / 12)" in out                           # neither table: left for the Critic, not guessed
    assert "Endothelial (5 DDX41 / 40 WT)" in out         # the one number that IS the upload count


def test_the_profile_counts_are_read_from_design_by_arm():
    assert pre_qc_counts({"design_by_arm": {"cells_by_label_and_arm": {"HC": {"DDX41": 5, "WT": 11}}}}) \
        == {"HC": {"DDX41": 5, "WT": 11}}
    assert pre_qc_counts(None) == {} and pre_qc_counts({"design_by_arm": None}) == {}


def test_the_manuscript_writer_path_is_corrected_too(tmp_path):
    """The manuscript is written in the gateway from the synthesis plus the audit block; it has its
    own coverage paragraph, so it gets the same correction, recorded on the result."""
    import json as _json
    from types import SimpleNamespace

    from aiscientist.gateway import app as gw
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "dataset_results.json").write_text(_json.dumps(
        {"design_by_arm": {"cells_by_label_and_arm": _PRE}}))
    rounds = [_round({"status": "ok", "reference": "WT", "condition": "DDX41",
                      "cells_by_group_and_arm": _TESTED,
                      "skipped_groups": [{"group": g, "n_condition": _TESTED[g]["DDX41"],
                                          "n_reference": _TESTED[g]["WT"], "reason": r}
                                         for g, r in _REFUSED.items()]})]
    result = SimpleNamespace(rounds=rounds, claim_audit={})
    out = gw._correct_manuscript_coverage(_F373, tmp_path, result)
    assert out.startswith("Endothelial (5 DDX41 / 22 WT)")
    assert len(result.claim_audit["report_corrections"]) == 5
