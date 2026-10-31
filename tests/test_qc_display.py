"""Default output boundary for dirty model drafts; source audit data is not modified."""
import pytest

from aiscientist.reporting.qc_display import default_qc_text


def test_mixed_prose_keeps_removals_other_scores_and_material_warning():
    draft = ("45 doublets removed. DoubletFinder and scDblFinder agree poorly (kappa 0.10). "
             "ISG score increased to 1.5; annotation kappa was 0.8.")
    display = default_qc_text(draft)
    assert "45 doublets removed." in display.text
    assert "ISG score increased to 1.5; annotation kappa was 0.8." in display.text
    assert "Doublet filtering needs review" in display.text
    assert "DoubletFinder" not in display.text and "0.10" not in display.text
    assert display.needs_review and display.changed
    assert draft in display.details


@pytest.mark.parametrize("removal", ["DoubletFinder removed 1,261 doublets",
                                     "1,261 doublets were removed by DoubletFinder",
                                     "Doublets removed: 1,261, called by DoubletFinder"])
def test_counts_survive_when_the_same_sentence_names_a_caller(removal):
    display = default_qc_text(removal + ".")
    assert display.text == "Doublets removed: 1,261."
    assert not display.needs_review


def test_doublet_table_columns_leave_unrelated_scientific_columns_intact():
    draft = ("| Sample | DoubletFinder score | scDblFinder score | ISG score | annotation kappa |\n"
             "|---|---:|---:|---:|---:|\n| S1 | 0.21 | 0.42 | 1.5 | 0.8 |\n")
    display = default_qc_text(draft)
    assert "| Sample | ISG score | annotation kappa |" in display.text
    assert "| S1 | 1.5 | 0.8 |" in display.text
    assert "DoubletFinder" not in display.text and "0.42" not in display.text
    assert display.details == (draft.rstrip(),)


def test_metric_value_table_and_heading_scope_do_not_hide_other_kappa():
    draft = ("## Doublet diagnostics\n\n| Metric | Value |\n|---|---|\n"
             "| Doublets removed | 45 |\n| Kappa | 0.10 |\n| ISG score | 1.5 |\n\n"
             "## Annotation\n\nKappa was 0.8; this is inter-rater agreement.\n")
    display = default_qc_text(draft)
    assert "| Doublets removed | 45 |" in display.text
    assert "| Kappa |" not in display.text
    assert "| ISG score | 1.5 |" in display.text
    assert "Kappa was 0.8; this is inter-rater agreement." in display.text
    assert display.needs_review


def test_generated_doublet_figure_is_optional_but_other_figure_and_downloads_stay():
    draft = ("![DoubletFinder versus scDblFinder scores](figures/doublet_scores.png)\n"
             "![ISG score by condition](figures/isg.png)\n"
             "- `tables/doubletfinder_scores.csv`\n- [CellQC metrics](tables/cellqc_metrics.csv)\n")
    display = default_qc_text(draft)
    assert "figures/doublet_scores.png" not in display.text
    assert "figures/isg.png" in display.text
    assert "tables/doubletfinder_scores.csv" in display.text
    assert "tables/cellqc_metrics.csv" in display.text
    assert "figures/doublet_scores.png" in "\n".join(display.details)


def test_generic_figure_caption_cannot_hide_a_doublet_diagnostic_filename():
    display = default_qc_text("![QC](figures/doublet_scores.png)\n"
                              "[Detailed scores](tables/doublet_scores.csv)")
    assert "figures/doublet_scores.png" not in display.text
    assert "tables/doublet_scores.csv" in display.text


def test_doublet_failure_still_has_a_visible_failure_warning():
    draft = ("DoubletFinder failed because the input matrix was empty.\n"
             "Mitochondrial filtering failed: no matching genes.")
    display = default_qc_text(draft)
    assert display.text == draft
    assert "Mitochondrial filtering failed: no matching genes." in display.text
    assert not display.changed


def test_comparison_with_failure_retains_a_generic_failure_and_the_original_reason():
    draft = "DoubletFinder scores failed validation: empty matrix; scDblFinder score was 0.42."
    display = default_qc_text(draft)
    assert "Doublet filtering failed" in display.text
    assert display.needs_review
    assert draft in display.details


def test_independent_clause_in_diagnostic_sentence_survives():
    display = default_qc_text("The doublet callers disagree (kappa 0.1), whereas ISG score rose to 1.5.")
    assert "ISG score rose to 1.5." in display.text
    assert "kappa" not in display.text


@pytest.mark.parametrize("joiner", [" ", "\n"])
def test_doublet_context_carries_to_next_sentence_but_stops_at_another_topic(joiner):
    draft = ("DoubletFinder and scDblFinder agree poorly." + joiner + "Kappa was 0.10. "
             "ISG score was 1.5. Annotation kappa was 0.8.")
    display = default_qc_text(draft)
    assert "0.10" not in display.text
    assert "ISG score was 1.5. Annotation kappa was 0.8." in display.text
    assert display.needs_review


def test_doublet_count_establishes_context_for_an_unnamed_caller():
    display = default_qc_text("45 doublets removed. The other caller flags 100 more cells.")
    assert "45 doublets removed." in display.text
    assert "100" not in display.text


def test_comma_and_does_not_move_independent_isg_result_into_details():
    display = default_qc_text("DoubletFinder and scDblFinder agree at kappa 0.1, and ISG score is 1.5.")
    assert "ISG score is 1.5." in display.text
    assert "kappa" not in display.text


def test_chinese_sentence_boundaries_preserve_counts_and_unrelated_scores():
    display = default_qc_text("移除了45个双细胞。DoubletFinder和scDblFinder的kappa为0.1。ISG评分为1.5。")
    assert "移除了45个双细胞。" in display.text
    assert "ISG评分为1.5。" in display.text
    assert "kappa" not in display.text


def test_context_does_not_cross_blank_paragraphs_without_a_doublet_heading():
    display = default_qc_text("DoubletFinder and scDblFinder agree poorly.\n\nKappa was 0.8.")
    assert "Kappa was 0.8." in display.text


@pytest.mark.parametrize("text", ["Doublets removed: 0.", "Annotation kappa: 0.8.",
                                  "ISG score is elevated; cell-type agreement is 0.9.",
                                  "Doublet filtering needs review; see optional technical details.",
                                  "Ambient RNA correction failed."])
def test_unrelated_results_and_concise_qc_are_unchanged(text):
    display = default_qc_text(text)
    assert display.text == text
    assert not display.changed and not display.details


def test_guard_is_idempotent_and_preserves_original_diagnostic_block():
    draft = "## Doublet scores\n\n```csv\nbarcode,scdblfinder_score\na,0.42\n```\n"
    first = default_qc_text(draft)
    second = default_qc_text(first.text)
    assert not second.changed
    assert second.text == first.text
    assert "scdblfinder_score" in "\n".join(first.details)
