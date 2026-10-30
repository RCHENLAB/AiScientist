"""The "how we know these results are correct" statement (reporting/verification.py).

Built 2026-10-08 after run f3b8268c4fd4's report said "83.8% were in Muller glia" where 83.8% was the
Best2 table's "other" row over its total, and called a cluster Muller glia on GLUL alone. The statement
must expose exactly that kind of number, and never count the model's own prose as evidence."""
from __future__ import annotations

import json
from pathlib import Path

from aiscientist.reporting.verification import build_verification, manuscript_note


def _art(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    (art / "tables").mkdir(parents=True)
    (art / "tables" / "best2_rpe_check.csv").write_text(
        "metric,value\nn_cells_global,16750.0\nn_Best2_positive_total,37.0\n"
        "n_Best2_positive_unassigned,2.0\nn_Best2_positive_rod,4.0\nn_Best2_positive_other,31.0\n"
        "frac_unassigned_above_global_p75,0.002699\n")
    (art / "tables" / "celltype_composition.csv").write_text(
        "cell_type,n_cells,pct\nRod photoreceptor,13520,80.72\nMuller glia,278,1.66\n")
    (art / "tables" / "composition_by_sample.csv").write_text(
        "sample,group,n_cells,pct\nS1,Rod photoreceptor,13520,80.72\nS1,Muller glia,278,1.66\n")
    (art / "tables" / "cluster_cell_types.json").write_text(json.dumps({
        "0": {"cell_type": "Rod photoreceptor", "n_cells": 13520,
              "canonical_markers_enriched": ["Rho", "Nrl"], "top_markers": ["Rho"]},
        "11": {"cell_type": "Muller glia", "n_cells": 278, "canonical_markers_enriched": ["Glul"],
               "top_markers": ["Glul"]},
        "21": {"cell_type": "Unassigned", "n_cells": 27, "unassigned_reason": "withdrawn",
               "evidence": "Muller glia withdrawn: Rlbp1 is not enriched",
               "top_markers": ["Cldn2", "Slc13a4", "Best2"]}}))
    return art


_ROUNDS = [
    {"step": "**QC** — run run_cellqc", "verdict": {"verdict": "accept", "score": 0.9, "critique": "Fine."},
     "scientist_result": {"steps": [
         {"tool": "run_cellqc", "args": {"min_counts": 500}, "result": {
             "status": "ok", "cells_before": 18681, "cells_after": 16750, "pct_cells_removed": 10.34,
             "counts_layer": "ambient-corrected integer UMI counts from CellQC (soupx)",
             "libraries": [{"sample": "S1", "cells_cellranger": 18681, "cells_after_filter": 18011,
                            "cells_after_doublets": 16750, "doublets_removed": 1261,
                            "removed_only_by": {"min_counts": 4, "min_features": 27,
                                                "max_pct_mt": 541, "several_thresholds": 98}}]}},
         {"tool": "run_code", "args": {"code": "print(7.777)"}, "result": {"stdout": "score 7.777"}},
         {"tool": "finish", "args": {"answer": "We found 4,321 cells."}, "result": {}}]}},
    {"step": "**Literature** — deep_literature", "verdict": {"verdict": "revise", "score": 0.25,
                                                           "critique": "The query missed. Redo it."},
     "scientist_result": {"steps": []}},
]
_AGENDA = ["**QC** — run run_cellqc", "**Literature** — deep_literature", "**Figures** — draw"]


def _statement(tmp_path, body):
    art = _art(tmp_path)
    md, summary = build_verification(art, body, _AGENDA, _ROUNDS, {"citations": [], "reason": "none"})
    return md, summary


def test_a_share_taken_from_the_other_bucket_is_shown_with_its_formula(tmp_path):
    md, s = _statement(tmp_path, "Only 37 cells carried Best2, and 83.8% of those were in Muller glia.\n")
    assert s["numbers_derived"] == 1
    assert "n_Best2_positive_other" in md and "'other' bucket" in md


def test_numbers_are_traced_to_tables_and_tool_results(tmp_path):
    md, s = _statement(tmp_path, "QC kept 16,750 of 18,681 cells (10.34% removed); rods were 80.72%.\n")
    assert s["numbers_checked"] == 4 and s["numbers_exact"] == 4 and s["numbers_untraced"] == 0


def test_the_models_own_answer_and_code_are_not_evidence(tmp_path):
    md, s = _statement(tmp_path, "The run found 4,321 cells and a score of 7.777.\n")
    assert s["numbers_untraced"] == 1 and "4,321" in s["untraced"]       # only in finish's answer
    assert s["numbers_printed"] == 1 and "model-written" in md            # only in run_code stdout


def test_a_percentage_that_does_not_match_its_stated_counts_is_flagged(tmp_path):
    md, s = _statement(tmp_path, "Of the cells, 500 of 1,000 cells (40%) were rods.\n")
    assert s["numbers_inconsistent"] == 1 and "is 50.0%, not 40%" in md


def test_labels_qc_steps_and_scope_are_stated(tmp_path):
    md, s = _statement(tmp_path, "Nothing numeric.\n")
    assert "cluster 11 rests on Glul alone" in md
    assert "| 21 | 27 | Muller glia withdrawn: Rlbp1 is not enriched | Cldn2, Slc13a4, Best2 |" in md
    assert s["qc_consistent"] is True and "670 removed, and the per-reason removals sum to 670" in md
    assert "ambient-corrected" in md
    assert "Step 2, Literature: NOT completed (last review 0.25). The query missed." in md
    assert "Step 3, Figures: never ran." in md
    assert "NOT checked against published literature" in md and "One sample" in md
    assert "Rod photoreceptor make up 80.7%" in md


def test_a_step_kept_without_review_is_named_not_counted_as_reviewed(tmp_path):
    # Two unusable Critic replies keep a successful step with score 0.0 and reviewed: False: not a
    # step "accepted with reservations (0.00)", and not one "accepted by the reviewer".
    unreviewed = {"verdict": "accept", "score": 0.0, "reviewed": False,
                  "critique": "Step not reviewed: the Critic returned no usable review twice; ..."}
    rounds = [{**_ROUNDS[0], "verdict": unreviewed}]
    md, s = build_verification(_art(tmp_path), "Nothing numeric.\n", _AGENDA[:1], rounds,
                               {"citations": [], "reason": "none"})
    assert ("- Step 1, QC: kept without review (the Critic returned no usable review twice)."
            in md)
    assert "accepted with reservations" not in md and "accepted by the reviewer" not in md
    assert s["steps_unreviewed"] == 1 and s["steps_reserved"] == 0 and s["steps_incomplete"] == 0
    assert "1 step(s) were kept without a review" in manuscript_note(s)


def test_identifiers_years_and_references_are_not_checked(tmp_path):
    body = ("Cldn2 and GRCm39 and GO:0007601 in 2023, with 10x v4 chemistry; see `work/adata_1234.h5ad`.\n"
            "## References\n1. Smith 2020, 12,345 cells.\n")
    _, s = _statement(tmp_path, body)
    assert s["numbers_checked"] == 0


def test_the_manuscript_carries_a_short_summary(tmp_path):
    _, s = _statement(tmp_path, "Only 37 cells carried Best2, and 83.8% of those were in Muller glia.\n")
    note = manuscript_note(s)
    assert note.startswith("## Verification") and "report/verification.md" in note
    assert "did not complete" in note and "not checked against published literature" in note


def test_a_count_of_one_is_grammatical_and_a_short_report_has_no_discussion(tmp_path):
    """Run 6275df1213ab's statement said "1 are arithmetic" and pointed at "the Discussion" of a
    short QC report that has none."""
    art = _art(tmp_path)
    md, s = build_verification(art, "Only 37 cells carried Best2, and 83.8% of those were in Muller "
                               "glia.\n", _AGENDA[:1], _ROUNDS[:1], {"citations": [], "reason": "none"},
                               brief=True)
    assert s["numbers_checked"] == 1 and s["numbers_derived"] == 1
    assert "1 number in the report was checked" in md and "**1** is arithmetic" in md
    assert "**0** appear as they are" in md
    assert "- The one planned step completed and was accepted by the reviewer." in md
    assert "Discussion" not in md
    assert "- Nothing in the report was checked against published literature (none)." in md
    note = manuscript_note(s)
    assert ("Of 1 number checked, 0 appear in a result table or tool output, 1 is arithmetic on one "
            "table (listed there with its formula)") in note
    assert "Nothing in the report was checked against published literature." in note
    wrong = manuscript_note({"numbers_checked": 2, "numbers_inconsistent": 1})
    assert "1 percentage does not match the counts stated beside it" in wrong


def test_the_gateway_places_the_note_before_references():
    from aiscientist.gateway.app import _insert_verification_note
    md = "# T\n\nBody.\n\n## References\n\n1. X\n"
    out = _insert_verification_note(md, {"numbers_checked": 3, "numbers_exact": 3})
    assert out.index("## Verification") < out.index("## References")


def test_a_mostly_unassigned_annotation_is_called_unreliable(tmp_path):
    art = _art(tmp_path)
    (art / "tables" / "cluster_cell_types.json").write_text(json.dumps({
        "0": {"cell_type": "Unassigned", "n_cells": 9000, "evidence": "mixed"},
        "1": {"cell_type": "Rod photoreceptor", "n_cells": 6000, "canonical_markers_enriched": ["Rho"]}}))
    md, s = build_verification(art, "No numbers.\n", [], [], {"citations": []})
    assert "WARNING: 9,000 of 15,000 cells (60.0%) are unassigned" in md
    assert "not reliable" in manuscript_note(s)


def test_a_change_stated_in_the_sentence_is_derived_and_random_count_pairs_are_not(tmp_path):
    # Run 7f2955e8e: "-1.04%" matched cluster 23's cells over cluster 4's; it is the median's change.
    art = _art(tmp_path)
    (art / "tables" / "clusters.csv").write_text("cluster,n_cells\n4,2016\n23,21\n9,560\n")
    md, s = build_verification(art, "The median UMI shifted from 2,872 to 2,842 (−1.04%). "
                                    "Cluster 9 is 21.56% of the three; 27.78% is no total.\n", [], [],
                               {"citations": []})
    assert "(2,842 − 2,872) ÷ 2,872 × 100, both stated in the sentence" in md
    assert "23 / n_cells ÷ 4 / n_cells" not in md
    assert "total of n_cells" in md                     # 560 / 2,597: a column total is fine
