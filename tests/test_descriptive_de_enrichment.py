"""A p-value declared non-inferential must not come back as a gate one step later.

A live plan said, in its DE step, that adjusted p-values were "strictly descriptive/
non-inferential" because the design has one library per arm — and then, in its enrichment step,
passed padj=0.05 and called the input "significantly shifted genes". In run_enrichment that
parameter is the cutoff a gene must clear to ENTER the test, so the contradiction was not wording:
a pseudoreplicated p-value was choosing the gene list, and its output read as significance.
"""

from __future__ import annotations

from bioagent.tools.scrna_pack import _enrichment_input_rows

ROWS = [
    {"gene": "BIG_EFFECT_WEAK_P", "pval_adj": "0.90", "log2fc": "3.0"},
    {"gene": "SMALL_EFFECT_TINY_P", "pval_adj": "0.001", "log2fc": "0.5"},
    {"gene": "BELOW_THE_EFFECT_FLOOR", "pval_adj": "0.001", "log2fc": "0.10"},
]


def test_a_valid_design_still_uses_both_gates():
    assert _enrichment_input_rows(ROWS, 0.25, 0.05, descriptive=False) == [("SMALL_EFFECT_TINY_P", 0.5)]


def test_a_descriptive_design_ignores_the_p_value_entirely():
    """Ignored, not widened: there is no p-value threshold that would make a pseudoreplicated
    test valid, so the gate is removed rather than loosened."""
    got = _enrichment_input_rows(ROWS, 0.25, 0.05, descriptive=True)
    assert [g for g, _ in got] == ["BIG_EFFECT_WEAK_P", "SMALL_EFFECT_TINY_P"]


def test_the_effect_size_floor_survives_in_both_modes():
    for descriptive in (True, False):
        genes = [g for g, _ in _enrichment_input_rows(ROWS, 0.25, 0.05, descriptive)]
        assert "BELOW_THE_EFFECT_FLOOR" not in genes


def test_descriptive_selection_is_ordered_by_effect_so_a_cap_keeps_the_strongest():
    got = _enrichment_input_rows(ROWS, 0.0, 0.05, descriptive=True)
    assert [abs(v) for _, v in got] == sorted((abs(v) for _, v in got), reverse=True)


def test_unparseable_rows_are_skipped_rather_than_defaulting_into_the_list():
    rows = ROWS + [{"gene": "JUNK", "pval_adj": "n/a", "log2fc": "NaN?"}, {"pval_adj": "0", "log2fc": "9"}]
    for descriptive in (True, False):
        genes = [g for g, _ in _enrichment_input_rows(rows, 0.25, 0.05, descriptive)]
        assert "JUNK" not in genes and len(genes) == len(set(genes))
