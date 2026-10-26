"""'ok with zero contexts' from deep_literature is NOT a corpus answer: it must fall through to
the Europe PMC keyword fallback (run 97dfc89dc5aa shipped an empty References because it did not)."""
from __future__ import annotations

from aiscientist.agents.research_harness import HarnessTool
from aiscientist.agents.research_lab import LabConfig, ResearchLab, Specialist
from aiscientist.agents.research_harness import HarnessContext


def _tool(name, payload):
    return HarnessTool(name, f"{name} tool", {"type": "object", "properties": {}},
                       lambda a, c, p=payload: p)


def _lab(catalog):
    from aiscientist.agents.research_harness import ResearchHarness
    sci = ResearchHarness(catalog=catalog, chat_fn=lambda m, t: {"content": "x", "tool_calls": []})
    return ResearchLab(HarnessContext(), LabConfig(max_rounds=1), complete_fn=lambda m: "{}",
                       scientist=sci)


def test_zero_context_corpus_answer_falls_back_to_europe_pmc():
    deep = _tool("deep_literature", {"status": "ok", "contexts": [],
                                     "answer": "I cannot answer this question due to having no papers."})
    calls = []

    def lit_exec(args, ctx):
        calls.append(args)
        return {"status": "ok", "n_found": 2,
                "papers": [{"title": "t", "doi": "10.1/x", "year": 2024, "source": "europe_pmc"}]}

    lit = HarnessTool("literature_search", "keyword search",
                      {"type": "object", "properties": {}}, lit_exec)
    lab = _lab([deep, lit])
    events = []
    res = lab._scientist("What changes in DDX41 retina?",
                         "**Literature grounding** — search the literature with `deep_literature`.",
                         Specialist("s", "p"), "", [], events.append)
    assert calls, "Europe PMC fallback did not run"
    assert any(e.get("type") == "literature_fallback" for e in events)
    assert res.status in ("ok", "incomplete")
    # The Critic is told the switch was the platform's (run 7f2955e8e revised it as off-brief).
    assert (res.final_answer or "").startswith("(Platform note: the RetiGene corpus")


def test_cited_corpus_answer_does_not_fall_back():
    deep = _tool("deep_literature", {"status": "ok",
                                     "contexts": [{"citation": "Farrar GJ et al. 2017"}],
                                     "answer": "Grounded answer."})
    calls = []
    lit = HarnessTool("literature_search", "keyword search",
                      {"type": "object", "properties": {}},
                      lambda a, c: calls.append(a) or {"status": "ok", "papers": []})
    lab = _lab([deep, lit])
    res = lab._scientist("q", "**Literature grounding** — use `deep_literature`.",
                         Specialist("s", "p"), "", [], lambda e: None)
    assert not calls
    assert res.status == "ok" and "Farrar" in (res.final_answer or "")


def test_a_cannot_answer_with_one_tangential_context_still_falls_back():
    # Run f3b8268c4fd4: one off-topic passage came back with "I cannot answer." and status ok; the
    # step was treated as a corpus answer, so Europe PMC never ran and References shipped empty.
    deep = _tool("deep_literature", {"status": "ok", "answer": "\n\nI cannot answer.",
                                     "formatted_answer": "Question: q\n\n\n\nI cannot answer.",
                                     "contexts": [{"citation": "Davidson AE et al. 2009"}]})
    calls = []
    lit = HarnessTool("literature_search", "keyword search", {"type": "object", "properties": {}},
                      lambda a, c: calls.append(a) or {"status": "ok", "papers": []})
    _lab([deep, lit])._scientist("q", "**Literature grounding** — use `deep_literature`.",
                                 Specialist("s", "p"), "", [], lambda e: None)
    assert calls, "Europe PMC fallback did not run"


def test_the_corpus_is_asked_a_question_not_a_keyword_string():
    asked = []
    deep = HarnessTool("deep_literature", "corpus", {"type": "object", "properties": {}},
                       lambda a, c: asked.append(a["question"]) or {
                           "status": "ok", "answer": "Grounded.", "contexts": [{"citation": "X 2020"}]})
    step = ("**Deep literature & external benchmark** — Ground the interpretation with `deep_literature`, "
            "asking one focused scientific question: the established single-cell marker profile and "
            "expected cell-type composition of the inferred tissue. The cited answer grounds the "
            "discussion.")
    # The model is unavailable here ("{}"), so the brief itself becomes the question.
    _lab([deep])._scientist("analyse this dataset", step, Specialist("s", "p"), "", [], lambda e: None)
    assert asked and "marker profile and expected cell-type composition" in asked[0]
    assert "`" not in asked[0] and "**" not in asked[0] and "GO:" not in asked[0]

    model_q = "What is the expected cell-type composition of adult mouse retina in single-cell studies?"
    from aiscientist.agents.research_harness import ResearchHarness
    lab = ResearchLab(HarnessContext(), LabConfig(max_rounds=1), complete_fn=lambda m: model_q,
                      scientist=ResearchHarness(catalog=[deep], chat_fn=lambda m, t: {}))
    lab._scientist("analyse this dataset", step, Specialist("s", "p"), "", [], lambda e: None)
    assert asked[-1] == model_q


def _europe_pmc(calls):
    def lit_exec(args, ctx):
        calls.append(args)
        return {"status": "ok", "n_found": 1,
                "results": [{"title": "t", "doi": "10.1/x", "year": 2024, "source": "europe_pmc"}]}
    return HarnessTool("literature_search", "keyword search",
                       {"type": "object", "properties": {}}, lit_exec)


def test_a_failed_corpus_call_says_why_in_the_event_log():
    """Prod run BioAdmin/1e887ed2bf8e logged `deep_literature: dependency_missing: 0 context(s)` and
    nothing else: the tool result named the dependency and carried a note, the fallback then replaced
    the step's tool calls, and no file of the run said what had been missing."""
    deep = _tool("deep_literature", {"status": "dependency_missing", "dependency": "paper-qa",
                                     "note": "`paper-qa` is not installed."})
    events = []
    _lab([deep, _europe_pmc([])])._scientist(
        "q", "**Literature grounding** — use `deep_literature`.",
        Specialist("s", "p"), "", [], events.append)
    summary = next(e["summary"] for e in events
                   if e.get("type") == "tool_result" and e.get("tool") == "deep_literature")
    assert summary.startswith("dependency_missing: 0 context(s)")
    assert "paper-qa" in summary and "not installed" in summary


def test_a_step_naming_deep_literature_in_a_session_without_it_goes_to_europe_pmc_and_says_so():
    """A session with no HPC3 literature line has no `deep_literature` in its roster, but a preset or
    a plan drafted earlier can still name it. The step runs on Europe PMC without a doomed call
    first, and the Critic is told the platform chose the tool (run 7f2955e8e revised a step for
    exactly that switch)."""
    calls, events = [], []
    res = _lab([_europe_pmc(calls)])._scientist(
        "Which cell types are in this mouse retina?",
        "**Literature grounding** — Run `deep_literature` for the rod genes found above.",
        Specialist("s", "p"), "", [], events.append)
    assert calls and res.status == "ok", "Europe PMC did not run"
    assert not any(e.get("tool") == "deep_literature" for e in events)
    assert (res.final_answer or "").startswith(
        "(Platform note: `deep_literature`, the RetiGene corpus, is not available in this session")


def test_a_literature_search_step_gets_no_platform_note():
    res = _lab([_europe_pmc([])])._scientist(
        "q", "**Literature grounding** — Run `literature_search` for the rod genes found above.",
        Specialist("s", "p"), "", [], lambda e: None)
    assert "Platform note" not in (res.final_answer or "")
