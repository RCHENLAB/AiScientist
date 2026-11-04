"""Review examples from issue #62 on merged main, including the final display boundary."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from aiscientist.gateway.app import _QC_RECORD_SECTIONS, _default_qc_display
from aiscientist.reporting.qc_display import default_qc_text


METHOD = "Doublets were identified with DoubletFinder (McGinnis et al., 2019) and removed."
LIMITATION = ("Doublet calling relies on a single decider; cells the second caller flagged were "
              "retained and may confound rare-population analyses.")
REFERENCE = ("McGinnis CS, Murrow LM, Gartner ZJ. DoubletFinder: doublet detection in single-cell "
             "RNA sequencing data using artificial nearest neighbors. Cell Syst. 2019.")


def _result(tool, record, *, previous=()):
    return SimpleNamespace(rounds=[{
        "verdict": {"verdict": "accept"}, "scientist_result": {"steps": [
            *previous, {"tool": tool, "ok": True, "result": record}]},
    }])


def _cellqc():
    return _result("run_cellqc", {"status": "ok", "cells_before": 2000, "cells_after": 1800,
                                  "libraries": [
        {"sample": "S1", "doublets_removed": 121}, {"sample": "S2", "doublets_removed": 96},
    ]})


@pytest.mark.parametrize("heading,content", [
    ("Methods", METHOD), ("Materials and Methods", METHOD), ("Limitations", LIMITATION),
    ("References", REFERENCE), ("The dataset", METHOD), ("What was run", METHOD),
    ("Background consulted", REFERENCE), ("Output Files Index", "- `tables/doubletfinder_scores.csv`"),
])
def test_protected_scientific_sections_survive_helper_and_gateway(tmp_path, heading, content):
    section = f"## {heading}\n\n### Details\n\n{content}\n"
    report = section + "\n## Results\n\nDoubletFinder and scDblFinder agree at kappa 0.10.\n"
    display = default_qc_text(report, keep=_QC_RECORD_SECTIONS)
    assert section in display.text
    assert "kappa 0.10" not in display.text
    # Back matter is deliberately preserved as a whole by the gateway; put it last there.
    report = "## Results\n\nDoubletFinder and scDblFinder agree at kappa 0.10.\n\n" + section
    text = _default_qc_display(report, tmp_path)
    assert section.rstrip() in text
    assert "kappa 0.10" not in text
    if heading in {"References", "Background consulted", "Output Files Index"}:
        assert text.index("report/doublet_details.md") < text.index(f"## {heading}")


@pytest.mark.parametrize("draft", [
    "DoubletFinder removed 120 doublets in S1 and 95 in S2.",
    "S1: Doublets removed: 121. DoubletFinder and scDblFinder agree at kappa 0.10.",
])
def test_all_authoritative_library_counts_are_present_even_when_one_is_already_shown(tmp_path, draft):
    result = _cellqc()
    original = deepcopy(result.rounds)
    text = _default_qc_display(draft, tmp_path, result)
    assert "S1: Doublets removed: 121." in text
    assert "S2: Doublets removed: 96." in text
    assert "120" not in text and "95" not in text
    assert result.rounds == original


def test_partial_existing_sample_count_does_not_hide_another_samples_removals(tmp_path):
    result = _result("run_cellqc", {"status": "ok", "cells_before": 2000, "cells_after": 1500,
                                  "libraries": [
        {"sample": "S1", "doublets_removed": 120}, {"sample": "S2", "doublets_removed": 95},
    ]})
    text = _default_qc_display(
        "S1: Doublets removed: 120.\nS2: DoubletFinder removed 95 doublets with kappa 0.10.",
        tmp_path, result)
    assert "S1: Doublets removed: 120." in text
    assert "S2: Doublets removed: 95." in text
    assert "kappa 0.10" not in text


def test_removed_results_paragraph_is_replaced_before_protected_methods_and_references(tmp_path):
    methods = f"## Methods\n\n{METHOD}"
    references = f"## References\n\n{REFERENCE}"
    report = ("## Results\n\nDoubletFinder and scDblFinder agree at kappa 0.10.\n\n"
              f"{methods}\n\n{references}\n")
    text = _default_qc_display(report, tmp_path, _cellqc())
    assert methods in text and references in text
    for addition in ("S1: Doublets removed: 121.", "S2: Doublets removed: 96.",
                     "Doublet filtering needs review", "report/doublet_details.md"):
        assert text.index("## Results") < text.index(addition) < text.index("## Methods")


@pytest.mark.parametrize("filtered,skipped,after,removed", [
    (True, False, 940, 60), (False, False, 1000, 0), (None, True, 1000, 0),
])
def test_standalone_doublet_record_uses_actual_removals_not_predictions(tmp_path, filtered, skipped, after, removed):
    record = {"status": "ok", "step": "doublets", "cells_before": 1000, "cells_after": after,
              "skipped": skipped}
    if not skipped:
        record.update(predicted_doublets=123, filtered=filtered)
    result = _result("run_doublet_detection", record, previous=[{
        "tool": "run_scanpy_qc", "ok": True,
        "result": {"status": "ok", "cells_before": 2000, "cells_after": 1000},
    }])
    for index, draft in enumerate(("Doublet scoring completed.", "Doublet score was 0.42; 123 doublets predicted.")):
        text = _default_qc_display(draft, tmp_path / str(index), result)
        assert f"Doublets removed: {removed}." in text
        assert "Doublets removed: 123" not in text
    if not skipped:
        assert record["predicted_doublets"] == 123


def test_standalone_removal_counts_do_not_replace_overall_cellqc_status_or_warnings(tmp_path):
    from aiscientist.gateway.app import _qc_chat_reply, _qc_step_outcome

    result = _cellqc()
    result.rounds[0]["scientist_result"]["steps"][0]["result"]["warnings"] = [
        "S2: NO mitochondrial gene matched; the mitochondrial filter was not applied."]
    result.rounds[0]["verdict"]["verdict"] = "reject"
    result.rounds.append({"verdict": {"verdict": "accept"}, "scientist_result": {"steps": [{
        "tool": "run_doublet_detection", "ok": True,
        "result": {"status": "ok", "cells_before": 1800, "cells_after": 1750,
                   "filtered": True, "predicted_doublets": 50},
    }]}})
    before = deepcopy(result.rounds)
    outcome = _qc_step_outcome(result)
    assert outcome["tool"] == "run_cellqc" and outcome["status"] == "not_accepted"
    reply = _qc_chat_reply(tmp_path, result)
    assert "Quality control not accepted" in reply
    assert "NO mitochondrial gene matched" in reply
    text = _default_qc_display(reply + "\nDoublet score was 0.42.", tmp_path, result)
    assert "run_doublet_detection: Doublets removed: 50." in text
    assert "NO mitochondrial gene matched" in text
    assert "S1: Doublets removed: 121." in text and "S2: Doublets removed: 96." in text
    assert result.rounds == before


def test_failed_standalone_tool_never_claims_removed_cells(tmp_path):
    result = _result("run_doublet_detection", {"status": "error", "cells_before": 1000,
                                              "cells_after": 900, "filtered": True})
    text = _default_qc_display("Doublet score was 0.42.", tmp_path, result)
    assert "Doublets removed" not in text


def test_clean_scanpy_chat_also_reports_the_later_standalone_removal_count(tmp_path):
    from aiscientist.gateway.app import _qc_chat_reply, _qc_only_run

    result = _result("run_scanpy_qc", {"status": "ok", "cells_before": 2000, "cells_after": 1000})
    result.rounds.append({"verdict": {"verdict": "accept"}, "scientist_result": {"steps": [{
        "tool": "run_doublet_detection", "ok": True,
        "result": {"status": "ok", "cells_before": 1000, "cells_after": 940,
                   "filtered": True, "predicted_doublets": 123},
    }]}})
    assert _qc_only_run(result)
    reply = _qc_chat_reply(tmp_path, result)
    assert not default_qc_text(reply).changed
    text = _default_qc_display(reply, tmp_path, result)
    assert "run_scanpy_qc" in text
    assert "run_doublet_detection: Doublets removed: 60." in text
    assert "123" not in text
    assert not (tmp_path / "report/doublet_details.md").exists()


def test_planner_hint_describes_removal_without_promoting_a_second_opinion():
    from aiscientist.tools._lib.cellranger import cellranger_layout_hint

    hint = cellranger_layout_hint({"n_libraries": 1, "all_have_bam": False,
                                  "libraries": [{"sample": "S1", "bam": False}]})
    assert "doublet removal" in hint
    assert "second opinion" not in hint


def _analysis_with_standalone_doublets():
    """Doublet detection inside a full analysis (clustering after it): not a QC-only run."""
    return _result("run_clustering", {"status": "ok"}, previous=[{
        "tool": "run_doublet_detection", "ok": True,
        "result": {"status": "ok", "cells_before": 1000, "cells_after": 940, "filtered": True},
    }])


@pytest.mark.parametrize("results", [
    "Scrublet removed 60 doublets (6%) before clustering. Leiden found 14 clusters.",
    "Leiden clustering at resolution 0.8 found 14 clusters.",
])
def test_a_write_up_the_filter_leaves_alone_gets_no_standalone_count_line(tmp_path, results):
    from aiscientist.gateway.app import _qc_only_run

    result = _analysis_with_standalone_doublets()
    assert not _qc_only_run(result)
    report = (f"# Rod loss\n\n## Results\n\n{results}\n\n## Discussion\n\nRods are reduced.\n\n"
              f"## References\n\n1. {REFERENCE}\n")
    assert _default_qc_display(report, tmp_path, result) == report


def test_a_qc_reply_that_already_states_the_standalone_count_is_not_told_again(tmp_path):
    result = _result("run_doublet_detection", {
        "status": "ok", "cells_before": 1000, "cells_after": 940, "filtered": True,
    }, previous=[{"tool": "run_scanpy_qc", "ok": True,
                  "result": {"status": "ok", "cells_before": 2000, "cells_after": 1000}}])
    reply = "Quality control kept 1,000 of 2,000 cells; doublet detection removed 60 doublets."
    assert _default_qc_display(reply, tmp_path, result) == reply
