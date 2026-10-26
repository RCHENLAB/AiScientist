"""Offline checks for page provenance, visible citation gaps and search-derived gene rows."""

import json

import pytest

from aiscientist.agents import corpus_answer as ca
from aiscientist.agents import literature_answer as lit


CONTEXTS = [{"pmid": "12345", "page": 6, "text": "PRPF31 haploinsufficiency causes retinal disease.",
             "citation": "A study. PMID:12345"}]


def test_gene_rows_require_verbatim_evidence_from_the_selected_passage():
    good = {"gene": "PRPF31", "evidence": CONTEXTS[0]["text"], "passage": 1}
    bad = [dict(good, evidence="PRPF31 causes retinal disease."),
           dict(good, gene="RHO"), dict(good, passage=2), dict(good, passage=True),
           dict(good, evidence=CONTEXTS[0]["text"].lower())]
    rows = ca.verify_gene_table(json.dumps([good, *bad, good]), CONTEXTS)
    assert len(rows) == 1
    assert rows[0]["citation"] == "PMID 12345, p. 6; A study. PMID:12345"
    assert rows[0]["evidence"] == CONTEXTS[0]["text"]
    assert ca.verify_gene_table(json.dumps([good]), [dict(CONTEXTS[0], page=None)]) == []
    assert ca.verify_gene_table("not JSON", CONTEXTS) == []
    assert ca.verify_gene_table("{}", CONTEXTS) == []
    assert "verbatim" in ca.gene_table_messages("Which genes?", CONTEXTS)[0]["content"]


def test_corpus_pages_survive_pubmed_merge_and_are_deduplicated():
    pubmed = lit.Paper(1, "Merged paper", pmid="12345", citation="PubMed citation")
    contexts = [*CONTEXTS, dict(CONTEXTS[0], page=8), *CONTEXTS]
    paper, = lit.add_corpus(lit.Collected([pubmed], []), {"status": "ok", "contexts": contexts}).papers
    assert paper.pages == ["6", "8"]
    assert "PubMed citation (PMID 12345, p. 6, 8)" in lit.references_md([paper], [paper])


def test_uncited_paragraphs_warn_after_invalid_citations_are_removed():
    papers = [lit.Paper(1, "Paper")]
    body, cited = lit.finish_answer("Supported [1].\n\nPRPF31 loss causes retinal cell death [99].", papers)
    assert "[99]" not in body
    assert "**Citation warning:**" in body
    assert cited == papers
    body, _ = lit.finish_answer("Supported [1].\n\nAlso supported [1].", papers)
    assert "Citation warning" not in body


def test_reference_only_citations_do_not_count_as_answer_support():
    body, cited = lit.finish_answer("The papers do not answer this.\n\n## References\n[1] Paper",
                                    [lit.Paper(1, "Paper")])
    assert "The papers do not answer this." in body
    assert "Citation warning" not in body
    assert cited == []


@pytest.mark.parametrize("paragraph", [
    "The retrieved papers do not address the effect on human cones.",
    "The papers did not report any measurements of survival in cones.",
    "No paper found in the search answers the question about treatment.",
    "The effect on human cones was not addressed in these experiments.",
    "It is unclear whether this mechanism applies to retinal tissue.",
    "The effect in cones was not reported in the retrieved passages.",
    "## Remaining questions about the retinal mechanism",
    "**Remaining questions about the retinal mechanism**",
    "Further research is needed.",
    "The abstracts do not address the effects on cones.",
    "The papers don\u2019t address the effects on cones.",
    "Remaining questions about the retinal mechanism:",
])
def test_uncited_abstentions_headings_and_short_paragraphs_do_not_warn(paragraph):
    body, _ = lit.finish_answer("Supported finding [1].\n\n" + paragraph, [lit.Paper(1, "Paper")])
    assert "Citation warning" not in body


def test_uncited_findings_mixed_with_a_limitation_still_warn():
    body, _ = lit.finish_answer(
        "PRPF31 loss causes retinal cell death. The clinical effect is unclear.", [lit.Paper(1, "Paper")])
    assert "Citation warning" in body
