"""The literature-search skill: queries, PubMed papers, an answer whose every citation is checked.

``agents/literature_answer.py`` runs once a literature plan is approved (RCHENLAB/AiScientist issue
21). Offline: the model and the search are scripted.
"""

from __future__ import annotations

import csv
import io

import pytest

from aiscientist.agents import literature_answer as lit


def _hit(n, *, doi=True, abstract="Abstract text."):
    return {"title": f"Paper {n}", "authors": f"Author{n} A, Other B", "year": "2024", "journal": "J Eye",
            "doi": f"10.1/p{n}" if doi else "", "pmid": str(1000 + n), "url": f"https://doi.org/10.1/p{n}",
            "citation": f"Author{n} et al. (2024) Paper {n}. J Eye. doi:10.1/p{n}", "abstract": abstract}


def test_the_plan_step_names_the_question_the_tool_and_where_it_runs():
    step = lit.plan_step("What is known about   DDX41 in the retina?")
    assert step.startswith("**Literature search** — ")
    assert "“What is known about DDX41 in the retina?”" in step and "`literature_search`" in step
    assert "no HPC3 job" in step
    long = lit.plan_step("x " * 400)
    assert "…" in long and len(long) < 700


def test_queries_come_from_the_model_cleaned_and_capped():
    raw = '```json\n["DDX41 retina", "DDX41 AND photoreceptor", "\\"DEAD-box helicase\\" retinal disease", ' \
          '"ddx41 retina", "a b c d e f g h i j", "fifth"]\n```'
    qs = lit.plan_queries(lambda m: raw, "What is known about DDX41 in the retina?")
    assert qs == ["DDX41 retina", "DDX41 photoreceptor", "DEAD-box helicase retinal disease", "a b c d e f g h"]


def test_without_usable_queries_the_tools_own_focusing_gives_one():
    for reply in ("", "no idea", '{"queries": []}', RuntimeError("down")):
        def complete(m, r=reply):
            if isinstance(r, Exception):
                raise r
            return r
        assert lit.plan_queries(complete, "DDX41 retina?", fallback=lambda q: "DDX41 retina") == ["DDX41 retina"]
    assert lit.plan_queries(None, "RHO rods", fallback=lambda q: q) == ["RHO rods"]
    assert lit.plan_queries(lambda m: '{"queries": ["RHO rod"]}', "q") == ["RHO rod"]


def test_papers_are_merged_in_turn_from_each_query_without_duplicates():
    rankings = {"a": [_hit(1), _hit(2), _hit(3)], "b": [_hit(2), _hit(4)], "c": {"status": "error", "error": "timeout"}}

    def search(q, n):
        r = rankings[q]
        return r if isinstance(r, dict) else {"status": "ok", "results": r}

    got = lit.collect_papers(search, ["a", "b", "c"], max_papers=3)
    assert [p.title for p in got.papers] == ["Paper 1", "Paper 2", "Paper 4"]
    assert [p.number for p in got.papers] == [1, 2, 3]
    assert got.papers[1].queries == ["b", "a"]           # found by both, first at the top of "b"
    assert got.searched == [{"query": "a", "count": 3}, {"query": "b", "count": 2},
                            {"query": "c", "count": 0, "error": "timeout"}]
    assert got.errors == ["timeout"]


def test_a_hit_without_doi_or_pmid_is_not_a_paper():
    hit = _hit(1, doi=False)
    hit["pmid"] = ""
    got = lit.collect_papers(lambda q, n: {"status": "ok", "results": [hit, _hit(2)]}, ["a"])
    assert [p.title for p in got.papers] == ["Paper 2"] and got.papers[0].number == 1


def test_the_answer_prompt_numbers_every_paper_with_its_abstract():
    papers = lit.collect_papers(lambda q, n: {"status": "ok", "results": [_hit(1), _hit(2, abstract="")]}, ["a"]).papers
    system, user = lit.answer_messages("Q?", papers)
    assert "cite it by its number" in system["content"] and "English" in system["content"]
    assert "[1] Paper 1\nAuthor1 A et al., J Eye, 2024\nAbstract: Abstract text." in user["content"]
    assert "Abstract: (no abstract available)" in user["content"] and user["content"].startswith("Question: Q?")


def test_citations_that_are_no_paper_are_removed_and_references_are_written_by_code():
    papers = lit.collect_papers(lambda q, n: {"status": "ok", "results": [_hit(i) for i in range(1, 5)]}, ["a"]).papers
    text = ("DDX41 is a helicase [1]. It is expressed in rods [2, 9] and cones [3–4]. Made up [7]. "
            "Twice [1].\n\n## References\n1. Invented et al. Nature.")
    body, cited = lit.finish_answer(text, papers)
    assert body == "DDX41 is a helicase [1]. It is expressed in rods [2] and cones [3, 4]. Made up. Twice [1]."
    assert [p.number for p in cited] == [1, 2, 3, 4]
    refs = lit.references_md(cited, papers)
    assert refs.startswith("**References**\n\n- [1] Author1 et al. (2024) Paper 1.")
    assert "\n- [4] Author4 et al." in refs                    # its own number, not a renumbered list
    assert "https://doi.org/10.1/p1" in refs and "Invented" not in refs


def test_an_answer_that_cites_nothing_lists_the_papers_read():
    papers = lit.collect_papers(lambda q, n: {"status": "ok", "results": [_hit(1)]}, ["a"]).papers
    body, cited = lit.finish_answer("The abstracts do not address this.", papers)
    assert cited == [] and lit.references_md(cited, papers).startswith("**Papers read** (the answer cites none")
    assert lit.references_md([], []) == ""


def test_no_papers_says_what_was_searched_and_that_nothing_was_answered_from_memory():
    got = lit.collect_papers(lambda q, n: {"status": "error", "error": "ConnectError"}, ["DDX41 retina"])
    text = lit.no_papers_answer("q", got)
    assert "“DDX41 retina”" in text and "ConnectError" in text and "Nothing was answered from memory" in text


def test_what_is_saved_says_how_it_was_made_and_lists_every_paper():
    got = lit.collect_papers(lambda q, n: {"status": "ok", "results": [_hit(1), _hit(2)]}, ["a", "b"])
    md = lit.method_md(got, "2026-10-20")
    assert "on 2026-10-20 with 2 queries" in md and "- “a”: 2 paper(s)" in md and "2 distinct paper(s)" in md
    rows = list(csv.DictReader(io.StringIO(lit.papers_csv(got.papers, {2}))))
    assert [(r["number"], r["cited"], r["pmid"]) for r in rows] == [("1", "no", "1001"), ("2", "yes", "1002")]
    assert rows[0]["queries"] == "a | b"


# --- the lab's corpus next to PubMed ------------------------------------------------------------------

def _ctx(pmid, chunk, text, page=1):
    return {"pmid": str(pmid), "chunk": chunk, "page": page, "text": text,
            "citation": f"Nakamura M et al. RDH5 paper {pmid}. Am J Ophthalmol (2002). PMID:{pmid} DOI:10.1/x"}


def test_one_model_call_gives_the_pubmed_queries_and_the_corpus_words():
    raw = ('Sure: {"queries": ["RDH5 fundus albipunctatus", "RDH5 dark adaptation"], '
           '"terms": ["RDH5", "fundus albipunctatus", "\\"dark adaptation\\"", "rdh5", "a b c d e", 7]}')
    queries, terms = lit.plan_search(lambda m: raw, "q")
    assert queries == ["RDH5 fundus albipunctatus", "RDH5 dark adaptation"]
    assert terms == ["RDH5", "fundus albipunctatus", "dark adaptation"]      # deduplicated, at most 4 words
    assert lit.plan_search(lambda m: '["DDX41 retina"]', "q") == (["DDX41 retina"], [])


def test_corpus_passages_come_first_one_source_per_paper_and_a_pubmed_duplicate_joins_it():
    pubmed = lit.collect_papers(lambda q, n: {"status": "ok", "results": [_hit(1), _hit(2)]}, ["a"])
    result = {"status": "ok", "terms": [{"term": "RDH5", "n_passages": 116, "use": "filter"}],
              "contexts": [_ctx(9001, 3, "The rod b-wave recovered after 3 h of dark adaptation."),
                           _ctx(1002, 0, "Same paper as PubMed hit 2.", page=4),
                           _ctx(9001, 5, "A second passage of the same paper.")]}
    got = lit.add_corpus(pubmed, result)
    assert [(p.number, p.pmid, p.sources) for p in got.papers] == [
        (1, "9001", ["corpus"]), (2, "1002", ["corpus", "pubmed"]), (3, "1001", ["pubmed"])]
    first, both = got.papers[0], got.papers[1]
    assert first.passages == ["(p. 1) The rod b-wave recovered after 3 h of dark adaptation.",
                              "(p. 1) A second passage of the same paper."]
    assert (first.title, first.authors, first.year, first.journal, first.doi) == (
        "RDH5 paper 9001", "Nakamura M et al.", "2002", "Am J Ophthalmol", "10.1/x")
    assert lit.references_md([first], got.papers) == (
        "**References**\n\n- [1] Nakamura M et al. RDH5 paper 9001. Am J Ophthalmol (2002). "
        "PMID:9001 DOI:10.1/x (PMID 9001, p. 1) https://pubmed.ncbi.nlm.nih.gov/9001/")
    assert first.url == "https://pubmed.ncbi.nlm.nih.gov/9001/"
    assert both.title == "Paper 2" and both.abstract == "Abstract text." and both.passages == ["(p. 4) Same paper as PubMed hit 2."]
    assert got.corpus == {"status": "ok", "terms": result["terms"], "passages": 3, "papers": 2}
    # the answer reads the passages and the abstracts; the saved method and tables say where each came from
    user = lit.answer_messages("Q?", got.papers)[1]["content"]
    assert "Papers found in the lab's RetiGene corpus and PubMed" in user
    assert "[1] RDH5 paper 9001" in user and "- (p. 1) The rod b-wave recovered" in user
    assert "[2] Paper 2" in user and "Abstract: Abstract text." in user
    md = lit.method_md(got, "2026-10-21")
    assert "3 passage(s) from 2 paper(s)" in md and "Words used: RDH5 (116)" in md
    rows = list(csv.DictReader(io.StringIO(lit.papers_csv(got.papers, {1}))))
    assert [(r["source"], r["corpus_passages"], r["cited"]) for r in rows] == [
        ("corpus", "2", "yes"), ("corpus+pubmed", "1", "no"), ("pubmed", "0", "no")]
    assert {k: rows[0][k] for k in ("title", "authors", "year", "journal", "doi")} == {
        "title": "RDH5 paper 9001", "authors": "Nakamura M et al.", "year": "2002",
        "journal": "Am J Ophthalmol", "doi": "10.1/x"}
    assert {k: rows[1][k] for k in ("title", "authors", "year", "journal", "doi")} == {
        "title": "Paper 2", "authors": "Author2 A, Other B", "year": "2024",
        "journal": "J Eye", "doi": "10.1/p2"}  # PubMed metadata takes precedence.
    assert lit.passages_md(got.papers).startswith("# RetiGene corpus passages read\n\n## [1] Nakamura M")


def test_a_failed_corpus_search_leaves_pubmed_alone_and_says_so():
    pubmed = lit.collect_papers(lambda q, n: {"status": "ok", "results": [_hit(1)]}, ["a"])
    got = lit.add_corpus(pubmed, {"status": "error", "error": "job timed out"})
    assert [p.number for p in got.papers] == [1] and got.corpus["error"] == "job timed out"
    assert "the search failed (job timed out); the answer rests on PubMed alone" in lit.method_md(got, "d")
    assert lit.add_corpus(pubmed, None) is pubmed                       # no corpus: unchanged
    none = lit.add_corpus(lit.collect_papers(lambda q, n: {"status": "ok", "results": []}, ["a"]),
                          {"status": "failed", "error": "0 passages"})
    assert lit.no_papers_answer("q", none).startswith("Neither the lab's RetiGene corpus nor PubMed returned a paper")


def test_the_plan_step_says_when_the_corpus_is_searched_too():
    step = lit.plan_step("How does the rod ERG change?", corpus=True)
    assert "RetiGene corpus" in step and "`search_corpus`" in step and "PubMed" in step
    assert "no HPC3 job" not in step


@pytest.mark.parametrize("citation,expected", [
    ("Smith J et al. Vitamin A. A follow-up study. Eye (2003). PMID:9001 DOI:10.1/study",
     ("Vitamin A. A follow-up study", "Smith J et al.", "2003", "Eye", "10.1/study")),
    ("Smith J. Vitamin A. A follow-up study. Eye (2003). PMID:9001",
     ("Vitamin A. A follow-up study", "Smith J", "2003", "Eye", "")),
    ("Smith J et al. Vitamin A. A follow-up study. Eye. PMID:9001 DOI:10.1/study",
     ("Vitamin A. A follow-up study", "Smith J et al.", "", "Eye", "10.1/study")),
    ("Smith J. Vitamin A. A follow-up study. Eye. PMID:9001",
     ("Vitamin A. A follow-up study", "Smith J", "", "Eye", "")),
    ("Smith J et al. One title. PMID:9001", ("One title", "Smith J et al.", "", "", "")),
])
def test_legacy_corpus_citations_fill_csv_without_rewriting_references(citation, expected):
    got = lit.add_corpus(lit.Collected([], []), {"status": "ok", "contexts": [
        {"pmid": "9001", "text": "A passage.", "citation": citation}]})
    row, = csv.DictReader(io.StringIO(lit.papers_csv(got.papers)))
    assert tuple(row[k] for k in ("title", "authors", "year", "journal", "doi")) == expected
    assert got.papers[0].line() == citation + " (PMID 9001) https://pubmed.ncbi.nlm.nih.gov/9001/"


def test_structured_corpus_metadata_takes_precedence_over_the_citation():
    context = _ctx(9001, 0, "A passage.")
    fields = {"title": "A full title. With periods.", "authors": "First A, Second B", "year": "2003",
              "journal": "J. Eye Res.", "doi": "10.1/manifest"}
    context.update(fields)
    got = lit.add_corpus(lit.Collected([], []), {"status": "ok", "contexts": [context]})
    row, = csv.DictReader(io.StringIO(lit.papers_csv(got.papers)))
    assert {k: row[k] for k in fields} == fields
    assert got.papers[0].citation == context["citation"]
    # Empty columns still fall back to the citation, rather than erasing known metadata.
    context.update(authors="", year="", doi="")
    got = lit.add_corpus(lit.Collected([], []), {"status": "ok", "contexts": [context]})
    assert (got.papers[0].authors, got.papers[0].year, got.papers[0].doi) == ("Nakamura M et al.", "2002", "10.1/x")


def test_manifest_title_disambiguates_periods_in_a_legacy_journal_name():
    context = {"pmid": "9001", "text": "Passage.", "title": "Vitamin A. A follow-up study",
               "citation": "Smith J et al. Vitamin A. A follow-up study. J. Med. Genet. (2003). PMID:9001"}
    paper, = lit.add_corpus(lit.Collected([], []), {"status": "ok", "contexts": [context]}).papers
    assert (paper.title, paper.authors, paper.journal, paper.year) == (
        "Vitamin A. A follow-up study", "Smith J et al.", "J. Med. Genet", "2003")


def test_deeper_queries_still_read_only_twelve_papers():
    calls = []

    def search(query, limit):
        calls.append((query, limit))
        return {"status": "ok", "results": [_hit(i) for i in range(100)]}

    got = lit.collect_papers(search, ["RHO retina", "RHO degeneration"])
    assert calls == [("RHO retina", 12), ("RHO degeneration", 12)]
    assert len(got.papers) == lit.MAX_PAPERS == 12
    assert [p.number for p in got.papers] == list(range(1, 13))
    assert all(p.queries == ["RHO retina", "RHO degeneration"] for p in got.papers)
