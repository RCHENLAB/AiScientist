"""Asking about a plan must not cost you the plan.

Plan mode had three actions — approve / revise / cancel — so every reply a reviewer typed became
"revise". Asking "why is step 3 a Wilcoxon test?" did not get an answer; it triggered a re-plan.
And a whole-plan redraft damages the steps nobody mentioned (measured: it silently dropped an
unmentioned step in 100% of trials), so the price of asking a question was losing part of the plan
you were asking about.

The classifier is deliberately biased toward QUESTION: reading a change request as a question
wastes one exchange, while reading a question as a change request rewrites work nobody asked to
touch.
"""

from __future__ import annotations

import json

from bioagent.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
from bioagent.agents.research_lab import LabConfig, ResearchLab, classify_plan_reply


def test_english_questions_and_change_requests_are_told_apart():
    for text in ("why is step 3 a Wilcoxon test?", "what does run_de actually do",
                 "explain step 3", "how many donors does this have?"):
        assert classify_plan_reply(text) == "question", text
    for text in ("stratify by majorclass", "remove the literature step",
                 "could you use 0.5 instead?", "change step 2 to pseudobulk"):
        assert classify_plan_reply(text) == "change", text


def test_chinese_questions_are_caught_mid_sentence():
    """Chinese does not front its question word — "第3步为什么用 Wilcoxon" has it in the middle,
    and a startswith rule sent exactly that reply down the re-plan path."""
    for text in ("第3步为什么用 Wilcoxon", "run_de 到底做了什么", "这个 0.25 是什么意思",
                 "解释一下第三步"):
        assert classify_plan_reply(text) == "question", text
    for text in ("把第4步改成 pseudobulk", "第2步加上 composition", "把文献那步删掉"):
        assert classify_plan_reply(text) == "change", text


def test_asking_ABOUT_a_verb_is_not_asking_FOR_it():
    """The change-verb scan runs first, which is right for "could you use 0.5 instead?" and was
    wrong for "why do you use wilcoxon here?" — the same verb, one a request and one a question
    about the request's subject. The second cost the reviewer a re-plan for asking."""
    for text in ("why do you use wilcoxon here?",
                 "why does step 3 remove cells with high mito?",
                 "第3步为什么要去掉低质量细胞?",
                 "为什么不加 doublet 检测?",
                 "how do I change the resolution?",
                 "what if there are no replicates?"):
        assert classify_plan_reply(text) == "question", text
    # The modal openers still lose to the verb, because those DO front polite requests.
    assert classify_plan_reply("could you use 0.5 instead?") == "change"
    # ...and an interrogative that fronts a PROPOSAL is a change, verb or no verb.
    for text in ("how about using 0.5", "what about dropping the enrichment step"):
        assert classify_plan_reply(text) == "change", text


def test_a_change_verb_beats_a_question_mark():
    """"Could you use 0.5 instead?" is a request wearing a question mark."""
    assert classify_plan_reply("could you use 0.5 instead?") == "change"
    assert classify_plan_reply("能不能把第3步改成 pseudobulk?") == "change"


def _lab(agenda, answer="Because the comparison is within one sample, the cell is a valid unit."):
    seen: list[str] = []

    def complete(messages):
        sys_prompt = messages[0]["content"]
        seen.append(sys_prompt)
        if "answering a researcher's question about the analysis plan" in sys_prompt:
            return answer
        if "reviewing a DRAFT analysis plan" in sys_prompt:
            return json.dumps({"issues": [], "revised_agenda": []})
        if "finalizing the analysis plan" in sys_prompt:
            return json.dumps({"final_agenda": list(agenda)})
        if "Principal Investigator of a bioinformatics lab" in sys_prompt:
            return json.dumps({"agenda": list(agenda)})
        return "FINAL REPORT: done."

    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"), LabConfig(),
                      complete_fn=complete,
                      scientist=ResearchHarness(catalog=default_catalog(),
                                                chat_fn=lambda *_a: {"content": "", "tool_calls": [
                                                    {"id": "f", "type": "function",
                                                     "function": {"name": "finish",
                                                                  "arguments": '{"answer": "ok"}'}}]}))
    return lab, seen


def test_a_question_is_answered_and_the_plan_survives_untouched():
    agenda = ["Assess quality with `run_scanpy_qc`", "Contrast the arms with `run_de`"]
    lab, seen = _lab(agenda)
    events: list[dict] = []
    replies = iter([{"action": "revise", "feedback": "why is step 2 a Wilcoxon test?"},
                    {"action": "approve"}])
    result = lab.run("Compare the arms", on_event=events.append,
                     plan_review=lambda _k, _p: next(replies))

    answered = [e for e in events if e["type"] == "plan_answer"]
    assert answered and "valid unit" in answered[0]["answer"]
    assert result.agenda == agenda, "the plan must come back byte-identical after a question"
    # and no redraft was attempted for it
    assert not any("revise ONE step" in s for s in seen)


def test_a_change_request_still_re_plans():
    """The floor under the whole thing: making questions cheap must not make changes impossible."""
    agenda = ["Assess quality with `run_scanpy_qc`", "Contrast the arms with `run_de`"]
    lab, seen = _lab(agenda)
    events: list[dict] = []
    replies = iter([{"action": "revise", "feedback": "stratify by majorclass"},
                    {"action": "approve"}])
    lab.run("Compare the arms", on_event=events.append, plan_review=lambda _k, _p: next(replies))

    assert not any(e["type"] == "plan_answer" for e in events)
    assert any("plan" in s.lower() for s in seen)
