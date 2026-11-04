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
    assert display.text == removal + "."
    assert not display.needs_review and not display.changed


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


def test_score_values_are_optional_but_the_concrete_failure_remains_visible():
    draft = "DoubletFinder scores failed validation: empty matrix; scDblFinder score was 0.42."
    display = default_qc_text(draft)
    assert "DoubletFinder scores failed validation: empty matrix;" in display.text
    assert "0.42" not in display.text
    assert not display.needs_review
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


def test_a_sentence_after_a_doublet_finding_keeps_other_results():
    # One paragraph, as an Abstract is: a doublet mention must not carry "compared" or "score"
    # in later sentences into the details file.
    draft = ("After ambient-RNA correction and doublet removal, 14,553 cells remained. "
             "Comparing knockout with wild type, rods showed 412 differentially expressed genes, "
             "and an interferon-response score was elevated in Muller glia. "
             "The other caller agreed on 245 of them.")
    display = default_qc_text(draft)
    assert "Comparing knockout with wild type, rods showed 412 differentially expressed genes" in display.text
    assert "interferon-response score was elevated" in display.text
    assert "245" not in display.text


@pytest.mark.parametrize("question, asks", [
    ("Run CellQC and compare DoubletFinder with scDblFinder.", True),
    ("Show me the doublet scores.", True),
    ("How well do the two doublet callers agree?", True),
    ("比较两种双细胞算法", True),
    ("Run CellQC with scDblFinder as the decider.", False),
    ("Run CellQC on these folders and write a QC report.", False),
    ("Run QC with doublet removal, then compare knockout with wild type.", False),
    ("", False),
])
def test_only_a_request_about_the_doublet_calls_asks_for_their_details(question, asks):
    from aiscientist.reporting.qc_display import asks_for_doublet_details

    assert asks_for_doublet_details(question) is asks


def test_a_doublet_clause_with_a_generic_word_keeps_its_result():
    # "compared" here is the next analysis, not a comparison of the callers.
    kept = "After doublet removal, we compared knockout with wild type and found 412 DE genes."
    display = default_qc_text(kept + " Doublet scores were higher in S2.")
    assert kept in display.text
    assert "Doublet scores" not in display.text


def test_another_qc_problem_under_a_doublet_heading_stays_visible():
    draft = ("## Quality control and doublet removal\n\n929 doublets were removed. CellQC flagged "
             "S2 for low retention (40% kept); compare its min_counts threshold with S1's.\n")
    display = default_qc_text(draft)
    assert display.text == draft and not display.changed
    assert "Doublet filtering needs review" not in display.text


def test_an_agreement_of_another_analysis_after_a_doublet_sentence_stays():
    draft = ("After doublet removal, 14,553 cells remained. "
             "scGPT and the marker labels agreed on 87% of cells.")
    display = default_qc_text(draft)
    assert display.text == draft and not display.changed


def test_review_image_with_semicolon_is_preserved_as_a_complete_qc_figure():
    image = "![Per-cell QC metrics after filtering; doublet scores are shown in panel C.](figures/qc.png)"
    display = default_qc_text(image)
    assert display.text == image and not display.changed


def test_review_unclosed_fence_does_not_swallow_discussion():
    draft = "```text\nDoubletFinder pANN = 0.21\n\n## Discussion\nRods retained their marker expression.\n"
    display = default_qc_text(draft)
    assert "## Discussion\nRods retained their marker expression." in display.text
    assert "```" not in display.text
    assert "0.21" not in display.text


def test_review_residual_doublet_finding_is_not_a_caller_comparison():
    draft = ("Cluster 7 shows elevated doublet scores and co-expresses rod and cone markers, "
             "suggesting residual doublets.")
    display = default_qc_text(draft)
    assert display.text == draft and not display.changed


@pytest.mark.parametrize("draft", [
    "Cluster 7 shows elevated doublet scores and co-expresses 42 rod and cone marker genes, suggesting residual doublets.",
    "DoubletFinder flagged cells with low UMI counts in sample 2.",
    "- S2: scDblFinder second opinion failed: R package version 1.2.3 was not installed.",
])
def test_review_unrelated_sentence_numbers_are_not_doublet_diagnostic_values(draft):
    display = default_qc_text(draft)
    assert display.text == draft and not display.changed
    assert not display.needs_review


@pytest.mark.parametrize("draft", [
    "DoubletFinder removed 120 doublets in S1 and 95 in S2.",
    "DoubletFinder 移除了 929 个双细胞。",
])
def test_review_without_records_caller_removal_sentences_keep_all_sample_counts(draft):
    display = default_qc_text(draft)
    assert display.text == draft and not display.changed


@pytest.mark.parametrize("draft", ["DoubletFinder removed 120 doublets in S1 and 95 in S2.",
                                  "DoubletFinder 移除了 929 个双细胞。"])
def test_named_removal_counts_are_not_salvaged_when_the_caller_has_qc_records(draft):
    display = default_qc_text(draft, salvage_counts=False)
    assert display.changed and draft in display.details
    assert "120" not in display.text and "95" not in display.text and "929" not in display.text


def test_review_caller_table_scopes_bare_kappa_column_and_reads_its_low_value():
    draft = "| Sample | DoubletFinder | scDblFinder | kappa |\n|---|---|---|---|\n| S1 | 120 | 140 | 0.10 |"
    display = default_qc_text(draft)
    assert "kappa" not in display.text.lower() and "0.10" not in display.text
    assert "Doublet filtering needs review" in display.text and display.needs_review
    assert draft in display.details


def test_sample_counts_in_a_valued_comparison_are_salvaged_without_records():
    draft = "DoubletFinder removed 120 doublets in S1 and 95 in S2, with caller agreement kappa 0.10."
    display = default_qc_text(draft)
    assert "S1: Doublets removed: 120." in display.text
    assert "S2: Doublets removed: 95." in display.text
    assert "kappa" not in display.text


def test_numeric_per_cell_pann_field_still_moves_out_of_default_prose():
    display = default_qc_text("doubletfinder_pANN = 0.21.")
    assert display.changed and "0.21" not in display.text


@pytest.mark.parametrize("draft", ["DoubletFinder flagged cells with low UMI counts.",
                                  "DoubletFinder score standard error was 0.01."])
def test_review_generic_low_or_error_does_not_invent_a_doublet_warning(draft):
    display = default_qc_text(draft)
    assert not display.needs_review
    assert "Doublet filtering needs review" not in display.text
    assert "Doublet filtering failed" not in display.text
    if "low UMI" in draft:
        assert display.text == draft


def test_review_second_opinion_failure_keeps_sample_and_execution_reason():
    draft = "- S2: scDblFinder second opinion failed: R package not installed."
    display = default_qc_text(draft)
    assert display.text == draft and not display.changed


def test_review_untested_code_notice_keeps_caller_named_step_title():
    draft = ("> **Untested code.** This step ran code that the AI wrote during this run, not a tested "
             "AiScientist tool: *Compare DoubletFinder with scDblFinder*. Treat its results as "
             "exploratory and check them before relying on them.")
    display = default_qc_text(draft)
    assert display.text == draft and not display.changed


def test_regression_bare_marker_score_table_is_not_scoped_by_doublet_heading():
    draft = "## Doublet review\n\n| Gene | score |\n|---|---|\n| RHO | 7.8 |\n| GNAT1 | 3.2 |"
    display = default_qc_text(draft)
    assert display.text == draft and not display.changed


def test_regression_concrete_caller_failure_row_stays_in_metric_table():
    draft = ("## Doublet review\n\n| Metric | Value |\n|---|---|\n"
             "| DoubletFinder failure | empty matrix |\n| Cells kept | 855 |")
    display = default_qc_text(draft)
    assert display.text == draft and not display.changed


def test_regression_citation_year_is_not_a_caller_removal_count_with_records():
    draft = "Doublets were identified with DoubletFinder (McGinnis et al., 2019) and removed."
    display = default_qc_text(draft, salvage_counts=False)
    assert display.text == draft and not display.changed


def test_regression_remaining_cells_are_not_salvaged_as_removed_doublets():
    draft = ("DoubletFinder removed 120 doublets in S1, leaving 880 in S1, "
             "with caller agreement kappa 0.10.")
    display = default_qc_text(draft)
    assert "S1: Doublets removed: 120." in display.text
    assert "Doublets removed: 880" not in display.text


def test_single_sample_prefix_is_kept_when_salvaging_a_diagnostic_clause():
    display = default_qc_text("- S2: DoubletFinder removed 95 doublets, with caller agreement kappa 0.10.")
    assert "S2: Doublets removed: 95." in display.text
    assert "kappa" not in display.text
