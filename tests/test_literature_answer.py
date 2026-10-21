"""The literature-search skill: queries, PubMed papers, an answer whose every citation is checked.

``agents/literature_answer.py`` runs once a literature plan is approved (RCHENLAB/AiScientist issue
21). Offline: the model and the search are scripted.
"""

from __future__ import annotations

import csv
import io

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
