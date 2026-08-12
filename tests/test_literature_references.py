"""Final report References formatting/insertion.

This module no longer retrieves literature. Retrieval is tested in ``test_literature_search``.
"""

from __future__ import annotations

from bioagent.tools import literature_references as lr


def test_citation_title_html_markup_is_stripped_at_source():
    # Europe PMC titles carry inline HTML (e.g. "<i>BRCA2</i>"); it must never reach the rendered
    # reference as literal '<i>' or the HTML-escaped '&lt;i&gt;'.
    out = lr.references_from_citations([
        {"title": "Risk in carriers of <i>BRCA2</i> variants", "authors": "Conde D",
         "year": "2026", "doi": "10.1/x"},
        {"title": "Role of &lt;i&gt;TP53&lt;/i&gt; in tumours", "authors": "Lo R",
         "year": "2026", "pmid": "123456"},
    ])
    section = lr.format_references_section(out)
    assert "<i>" not in section and "&lt;" not in section
    assert "BRCA2 variants" in section and "TP53 in tumours" in section


def test_references_from_citations_normalises_dedupes_and_requires_identifier():
    out = lr.references_from_citations([
        {
            "title": "DDX41 in retina",
            "authors": "Mars Z",
            "year": "2026",
            "journal": "bioRxiv",
            "doi": "10.64898/2026.01.28.26344834",
            "url": "",
        },
        {
            "title": "DDX41 in retina",
            "authors": "Mars Z",
            "year": "2026",
            "journal": "bioRxiv",
            "doi": "10.64898/2026.01.28.26344834",
        },
        {"title": "No identifier"},
    ], query="DDX41 retina")

    assert out["status"] == "ok"
    assert out["tier"] == "lab_literature_search"
    assert out["query"] == "DDX41 retina"
    assert out["unfiltered_count"] == 3
    assert out["filtered_count"] == 1
    assert out["citations"][0]["url"] == "https://doi.org/10.64898/2026.01.28.26344834"
    assert "doi:10.64898/2026.01.28.26344834" in out["citations"][0]["citation"]


def test_references_from_citations_empty_when_no_doi_or_pmid():
    out = lr.references_from_citations([{"title": "No identifier"}])
    assert out["status"] == "empty"
    assert out["tier"] == "none"
    assert out["citations"] == []


def test_insert_replaces_reserved_placeholder():
    manuscript = (
        "# Title\n\n## Discussion\n\nText.\n\n"
        "## References\n\n"
        "*Citations to be inserted by the literature module (PaperQA).*\n\n"
        "# Output Files Index\n\n- `figures/a.png`\n"
    )
    result = {"status": "ok", "tier": "lab_literature_search", "citations": [
        {"citation": "Li J et al. (2025) DDX41 in retina. Cell. doi:10.1/x",
         "url": "https://doi.org/10.1/x"}]}
    out = lr.insert_references(manuscript, result)

    assert "Citations to be inserted" not in out
    assert "1. Li J et al. (2025)" in out
    assert "# Output Files Index" in out
    assert out.count("## References") == 1


def test_insert_replaces_existing_filled_section_idempotently():
    manuscript = (
        "# Title\n\n## References\n\n1. Old ref.\n\n# Output Files Index\n\n- `x`\n"
    )
    result = {"citations": [{"citation": "New ref (2026).", "url": ""}]}
    out = lr.insert_references(manuscript, result)
    assert "Old ref" not in out and "1. New ref (2026)." in out
    assert "# Output Files Index" in out and out.count("## References") == 1


def test_insert_removes_stray_non_h2_references_block():
    manuscript = (
        "# Title\n\n## Results\n\nText.\n\n### References\n\n"
        "1. Model-made weak ref.\n\n## References\n\n"
        "1. Old ref.\n\n## Output Files Index\n\n- `x`\n"
    )
    result = {"citations": [{"citation": "Author A (2024) Real ref. doi:10.1/x",
                             "url": "https://doi.org/10.1/x"}]}

    out = lr.insert_references(manuscript, result)

    assert "### References" not in out
    assert "Model-made weak ref" not in out
    assert out.count("## References") == 1
    assert "Real ref" in out and "## Output Files Index" in out


def test_insert_empty_citations_leaves_manuscript_clean():
    # No accepted citations -> the MANUSCRIPT must stay clean: the "no citations" note belongs in the
    # technical report's Diagnostics, not the paper. Drop the placeholder AND the now-empty References
    # heading; never print the degradation note into the manuscript.
    manuscript = (
        "# Title\n\n## Discussion\n\nText.\n\n"
        "## References\n\n"
        "*Citations to be inserted by the literature module (PaperQA).*\n\n"
        "# Output Files Index\n\n- `figures/a.png`\n"
    )
    out = lr.insert_references(manuscript, lr.empty_references("no accepted citations"))
    assert "Citations to be inserted" not in out
    assert "No accepted literature-search citations" not in out   # degradation note stays OUT of the paper
    assert "## References" not in out                             # empty section removed
    assert "# Output Files Index" in out and "## Discussion" in out


def test_insert_empty_citations_drops_model_written_references():
    # Literature found nothing but the model hand-wrote a References section -> it is unbacked (this
    # module never fabricates citations), so drop it rather than ship fabricated references.
    manuscript = "# Title\n\n## References\n\n1. Model made this up (2020).\n\n## Output Files Index\n\n- `x`\n"
    out = lr.insert_references(manuscript, {"citations": []})
    assert "Model made this up" not in out and "## References" not in out
    assert "## Output Files Index" in out


def test_format_empty_is_honest_not_fabricated():
    section = lr.format_references_section({"citations": []})
    assert "## References" in section
    assert "No accepted literature-search citations" in section
    assert "Publication Only" not in section


def test_degradation_note_none_on_accepted_lab_literature_search():
    assert lr.degradation_note({
        "status": "ok",
        "tier": "lab_literature_search",
        "citations": [{"citation": "Real ref. doi:10.1234/example"}],
    }) is None


def test_degradation_note_describes_empty_without_hidden_fallback():
    note = lr.degradation_note(lr.empty_references("no accepted literature_search citations"))
    assert note
    assert "hidden fallback search" in note
    assert "no accepted literature_search citations" in note
    assert "fabricated" in note
