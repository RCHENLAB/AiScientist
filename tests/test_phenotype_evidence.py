"""Tests for the literature EVIDENCE track (tools/phenotype_evidence.py).

The grading is the trust boundary of the whole line — it is what stops a fluent model assertion from
becoming a clinical-looking ClinGen tier — so most of this file is the REFUSALS: no passage, a
claimed tier the passages cannot support, a refutation, a dead literature backend. All offline: no
corpus, no GPU, no network.
"""
from __future__ import annotations

from bioagent.tools.phenotype_evidence import (
    build_evidence_question,
    classify_study_type,
    evidence_ceiling,
    extract_doi,
    extract_pmid,
    grade_evidence,
    make_deep_literature_runner,
    read_stated_tier,
    source_key,
)


def _ctx(citation: str, summary: str) -> dict:
    return {"citation": citation, "summary": summary, "score": 5}


# --- source identity ------------------------------------------------------------------------------


def test_extract_pmid_from_citation_and_url():
    assert extract_pmid("Smith et al. 2019, PMID: 30982610") == "30982610"
    assert extract_pmid("https://pubmed.ncbi.nlm.nih.gov/12915482/") == "12915482"
    assert extract_pmid("no identifier here") == ""


def test_extract_doi_and_source_key_prefers_pmid():
    assert extract_doi("Chen 2021 https://doi.org/10.1016/j.ajhg.2021.03.005") == \
        "10.1016/j.ajhg.2021.03.005"
    # two chunks of the SAME paper collapse to one source, which is what the ceiling counts
    a = source_key("Den Hollander 1999 Nat Genet PMID: 10508521")
    b = source_key("Den Hollander 1999 Nat Genet  PMID:10508521 (chunk 4)")
    assert a == b == "pmid:10508521"
    # no pmid -> doi; no doi -> the citation text itself, so a source is never silently merged
    assert source_key("Li 2020 doi:10.1000/abc").startswith("doi:")
    assert source_key("An Unindexed Preprint").startswith("cite:")


# --- study type + ceiling -------------------------------------------------------------------------


def test_classify_study_type_prefers_experimental_then_falls_back_weakest():
    assert classify_study_type("CRB1 knockout mouse model shows degeneration") == "functional"
    assert classify_study_type("a cohort of 45 probands was screened") == "cohort"
    assert classify_study_type("a systematic review of IRD genes") == "review"
    assert classify_study_type("we report a patient with night blindness") == "case_report"
    # unclassifiable text must land on the WEAKEST class so it can never inflate the ceiling
    assert classify_study_type("lorem ipsum") == "case_report"


def test_evidence_ceiling_counts_independent_sources_not_passages():
    one_paper = [{"source_id": "pmid:1", "study_type": "case_report"},
                 {"source_id": "pmid:1", "study_type": "case_report"},
                 {"source_id": "pmid:1", "study_type": "case_report"}]
    assert evidence_ceiling(one_paper) == "LIMITED"        # 3 chunks, still ONE paper
    assert evidence_ceiling([]) == "NONE"
    assert evidence_ceiling([{"source_id": "pmid:1", "study_type": "cohort"}]) == "MODERATE"
    assert evidence_ceiling([{"source_id": "pmid:1", "study_type": "case_report"},
                             {"source_id": "pmid:2", "study_type": "case_report"}]) == "MODERATE"
    assert evidence_ceiling([{"source_id": "pmid:1", "study_type": "functional"},
                             {"source_id": "pmid:2", "study_type": "case_report"}]) == "STRONG"
    three = [{"source_id": f"pmid:{i}", "study_type": "case_report"} for i in range(3)]
    assert evidence_ceiling(three) == "STRONG"
    four_plus_functional = three + [{"source_id": "pmid:9", "study_type": "functional"}]
    assert evidence_ceiling(four_plus_functional) == "DEFINITIVE"


def test_read_stated_tier_takes_the_concluding_mention():
    # an answer that walks the rubric before concluding must be read on its VERDICT, not its first word
    assert read_stated_tier("Not DEFINITIVE, and more than LIMITED. Classification: MODERATE") == "MODERATE"
    assert read_stated_tier("no grade named here") == ""


# --- grading: the three trust rules ---------------------------------------------------------------


def test_no_passage_means_ungraded_however_confident_the_prose():
    g = grade_evidence("CRB1 causes RP12; this is textbook. Classification: DEFINITIVE.", [],
                       gene="CRB1", disease="RP12")
    assert g["association"] is False and g["clingen_tier"] == "NONE"
    assert g["evidence_status"] == "ungraded"          # NOT 'unsupported' — the corpus said nothing
    assert g["stated_tier"] == "DEFINITIVE"            # the claim is recorded, just not believed


def test_stated_tier_needs_the_whole_token():
    # "definitively"/"strongly" are prose, not a ClinGen grade — reading them as one would let ordinary
    # hedging language set a clinical tier
    assert read_stated_tier("CRB1 definitively causes RP12 and is strongly supported") == ""


def test_passages_cap_a_claimed_tier():
    # one case report, but the answer asserts DEFINITIVE -> capped to what one source can support
    g = grade_evidence("This association is DEFINITIVE.",
                       [_ctx("Solo 2015 PMID: 26000000", "we report a patient with RP")],
                       gene="X", disease="RP")
    assert g["association"] is True and g["clingen_tier"] == "LIMITED"
    assert any("capped" in n for n in g["notes"])


def test_model_may_grade_below_the_ceiling():
    # 3 independent sources would allow STRONG, but the model read them and said LIMITED — respect it
    ctxs = [_ctx(f"Paper{i} PMID: 1000000{i}", "we report a patient") for i in range(3)]
    g = grade_evidence("The evidence is sparse. LIMITED.", ctxs, gene="X", disease="RP")
    assert g["clingen_tier"] == "LIMITED"


def test_refutation_and_dispute_are_graded_on_their_own_axis():
    ctxs = [_ctx("Retraction 2020 PMID: 32000000", "the reported association was refuted")]
    g = grade_evidence("This association has been refuted by later work.", ctxs,
                       gene="X", disease="RP")
    assert g["clingen_tier"] == "REFUTED" and g["association"] is False
    assert g["evidence_status"] == "contradicted"

    d = grade_evidence("Later cohorts failed to replicate this finding.", ctxs,
                       gene="X", disease="RP")
    assert d["clingen_tier"] == "DISPUTED" and d["evidence_status"] == "contradicted"


def test_retrieved_but_unsupportive_is_distinct_from_nothing_retrieved():
    g = grade_evidence("The provided context does not support an association.",
                       [_ctx("Unrelated 2020 PMID: 33333333", "a review of IRD genes")],
                       gene="TTN", disease="RP")
    assert g["association"] is False and g["clingen_tier"] == "NONE"
    assert g["evidence_status"] == "unsupported"       # papers came back; none supported the link


def test_graded_evidence_carries_its_passages():
    g = grade_evidence("Supported across families with mouse data. STRONG.",
                       [_ctx("A 1999 PMID: 10508521", "12 unrelated families with RP"),
                        _ctx("B 2003 PMID: 12915482", "knockout mouse model shows degeneration")],
                       gene="CRB1", disease="RP12")
    assert g["clingen_tier"] == "STRONG" and g["evidence_status"] == "graded"
    assert {e["pmid"] for e in g["evidence"]} == {"10508521", "12915482"}
    assert all(e["quote"] for e in g["evidence"])      # every graded claim keeps its passage


# --- the question ----------------------------------------------------------------------------------


def test_question_is_built_from_the_specific_inputs():
    q = build_evidence_question("CRB1", "RP12", ["HP:0000510"], {"HP:0000510": "Rod-cone dystrophy"})
    assert "CRB1" in q and "RP12" in q
    assert "Rod-cone dystrophy" in q                   # the patient's phenotype, not a generic query
    assert "DEFINITIVE" in q and "REFUTED" in q        # the rubric, so the answer is gradeable


# --- the runner ------------------------------------------------------------------------------------


def test_runner_grades_a_live_answer():
    def fake(args, ctx):
        assert "CRB1" in args["question"]
        return {"status": "ok",
                "answer": "Reported in several unrelated families with knockout mouse support. STRONG.",
                "contexts": [_ctx("A 1999 PMID: 10508521", "12 unrelated families"),
                             _ctx("B 2003 PMID: 12915482", "knockout mouse model")]}

    out = make_deep_literature_runner(fake, None)(gene="CRB1", disease="RP12",
                                                  hpo_terms=["HP:0000510"])
    assert out["association"] is True and out["clingen_tier"] == "STRONG"
    assert out["question"].startswith("Is there published evidence")


def test_runner_degrades_instead_of_raising():
    def missing(args, ctx):
        return {"status": "dependency_missing", "note": "paper-qa not installed"}

    out = make_deep_literature_runner(missing, None)(gene="CRB1", disease="RP12")
    assert out["association"] is False and out["clingen_tier"] == "NONE"
    assert out["evidence_status"] == "ungraded"        # a dead backend must not look like a refutation
    assert "dependency_missing" in out["notes"][0]

    def boom(args, ctx):
        raise RuntimeError("tunnel closed")

    crashed = make_deep_literature_runner(boom, None)(gene="CRB1", disease="RP12")
    assert crashed["clingen_tier"] == "NONE" and "RuntimeError" in crashed["notes"][0]


def test_runner_binds_hpo_labels_for_retrieval():
    seen = {}

    def fake(args, ctx):
        seen["q"] = args["question"]
        return {"status": "ok", "answer": "no evidence", "contexts": []}

    runner = make_deep_literature_runner(fake, None, hpo_labels={"HP:0000510": "Rod-cone dystrophy"})
    runner(gene="CRB1", disease="RP12", hpo_terms=["HP:0000510"])
    assert "Rod-cone dystrophy" in seen["q"]
