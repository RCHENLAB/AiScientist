"""'ok with zero contexts' from deep_literature is NOT a corpus answer: it must fall through to
the Europe PMC keyword fallback (run 97dfc89dc5aa shipped an empty References because it did not)."""
from __future__ import annotations

from bioagent.agents.research_harness import HarnessTool
from bioagent.agents.research_lab import LabConfig, ResearchLab, Specialist
from bioagent.agents.research_harness import HarnessContext


def _tool(name, payload):
    return HarnessTool(name, f"{name} tool", {"type": "object", "properties": {}},
                       lambda a, c, p=payload: p)


def _lab(catalog):
    from bioagent.agents.research_harness import ResearchHarness
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
