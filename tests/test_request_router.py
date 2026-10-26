"""Which line a request goes to: literature or analysis (RCHENLAB/AiScientist issue 21) — offline.

The composer no longer has a Research / Chat / Corpus scan menu; the session's model routes each
request and code guards the verdict (``agents/request_router.py``). These tests feed the router
scripted model replies, including the malformed ones a small model sends.
"""

from __future__ import annotations

import pytest

from aiscientist.agents import request_router as rr


def _model(reply):
    calls = []

    def complete(messages):
        calls.append(messages)
        if isinstance(reply, Exception):
            raise reply
        return reply

    complete.calls = calls
    return complete


def test_a_confident_literature_verdict_routes_to_literature():
    r = rr.route_request(_model('{"route": "literature", "confidence": 0.92, "reason": "general gene question"}'),
                         "What is known about DDX41 in the retina?")
    assert (r.route, r.source, r.literature) == ("literature", "model", True)
    assert r.reason == "general gene question"


def test_an_analysis_verdict_is_taken_as_given():
    r = rr.route_request(_model('{"route": "analysis", "confidence": 0.6, "reason": "QC"}'), "Run QC on it")
    assert (r.route, r.source) == ("analysis", "model")


@pytest.mark.parametrize("reply", [
    '```json\n{"route": "literature", "confidence": 0.9, "reason": "x"}\n```',
    'Sure. {"route": "literature", "confidence": "90%", "reason": "x"} Hope this helps.',
    '{"route":"literature","confidence":90,"reason":"x"}',
])
def test_a_fenced_or_wrapped_reply_is_read(reply):
    r = rr.route_request(_model(reply), "Which genes cause Leber congenital amaurosis?")
    assert r.route == "literature" and r.confidence == pytest.approx(0.9)


def test_a_weak_literature_verdict_stays_an_analysis():
    r = rr.route_request(_model('{"route": "literature", "confidence": 0.5}'), "What about RHO?")
    assert (r.route, r.source, r.raw_route) == ("analysis", "guard", "literature")


def test_with_data_attached_literature_needs_more_confidence():
    reply = '{"route": "literature", "confidence": 0.7}'
    assert rr.route_request(_model(reply), "What is RHO?").route == "literature"
    assert rr.route_request(_model(reply), "What is RHO?", data=["retina.h5ad"]).route == "analysis"
    assert rr.route_request(_model(reply), "What is RHO?", prior_request="QC my samples").route == "analysis"
    assert rr.route_request(_model('{"route": "literature", "confidence": 0.9}'), "What is RHO?",
                            data=["retina.h5ad"]).route == "literature"


@pytest.mark.parametrize("question", [
    "Why is cluster 3 high in RHO?",
    "What does the literature say about the genes in my dataset?",
    "Is ABCA4 expressed in these cells?",
    "这个数据里的视杆细胞有什么特点？",
    "Do the samples I uploaded show gliosis?",
])
def test_a_question_about_the_users_data_is_an_analysis_whatever_the_model_says(question):
    r = rr.route_request(_model('{"route": "literature", "confidence": 0.99}'), question)
    assert (r.route, r.source) == ("analysis", "guard")


@pytest.mark.parametrize("reply", ["", "I am not sure.", '{"route": "both"}', '{"route": "literature analysis"}',
                                   RuntimeError("model down")])
def test_an_unusable_reply_falls_back_to_the_rules(reply):
    lit = rr.route_request(_model(reply), "What is known about DDX41 in the retina?")
    assert (lit.route, lit.source) == ("literature", "rules")
    ana = rr.route_request(_model(reply), "Cluster my data and find marker genes")
    assert (ana.route, ana.source) == ("analysis", "rules")


def test_without_a_model_the_rules_decide():
    assert rr.route_request(None, "Summarize the published papers on Müller glia").route == "literature"
    assert rr.route_request(None, "哪些基因会导致视网膜色素变性？相关文献").route == "literature"
    # a literature ask that also computes, or with data or an earlier analysis, stays an analysis
    assert rr.route_request(None, "Run DE and check the literature").route == "analysis"
    assert rr.route_request(None, "What is known about DDX41?", data=["a.h5ad"]).route == "analysis"
    assert rr.route_request(None, "What is known about DDX41?", prior_request="QC").route == "analysis"
    assert rr.route_request(None, "Why is RHO down?").route == "analysis"


def test_the_model_is_told_what_the_chat_holds():
    m = _model('{"route": "analysis", "confidence": 0.9}')
    rr.route_request(m, "Compare the two groups", data=[f"s{i}.h5ad" for i in range(8)],
                     prior_request="QC the two samples")
    system, user = m.calls[0]
    assert system["role"] == "system" and "literature" in system["content"] and "analysis" in system["content"]
    assert "8 item(s): s0.h5ad" in user["content"] and "and 2 more" in user["content"]
    assert "“QC the two samples”" in user["content"] and user["content"].endswith("Message: Compare the two groups")
    m2 = _model('{"route": "analysis", "confidence": 0.9}')
    rr.route_request(m2, "hi")
    assert "Data attached to this chat: none" in m2.calls[0][1]["content"]
    assert "Earlier analysis in this chat: none" in m2.calls[0][1]["content"]


def test_parse_route_rejects_what_names_no_single_route():
    assert rr.parse_route('{"route": "chat"}') is None
    assert rr.parse_route("analysis") == ("analysis", 0.0, "")
    assert rr.parse_route('{"intent": "literature", "confidence": 95}') == ("literature", 0.95, "")
