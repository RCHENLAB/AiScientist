"""The answer-first fast path (``agents/quick_chat.py``) + its streaming transport
(``gateway.vllm_client.chat_tools_stream``).

These run OFFLINE: the loop takes an injected ``stream_fn``, and the transport tests feed
canned SSE bytes to a stubbed ``urlopen``. No GPU, no SSH, and — importantly — no import of
``bioagent.gateway.app`` (which needs ``paramiko``), so this file runs on a bare checkout.
"""

from __future__ import annotations

import io
import json

import pytest

from bioagent.agents.quick_chat import (
    QuickChatConfig,
    _parse_args,
    run_quick_chat,
)
from bioagent.gateway import vllm_client


# --- test doubles -------------------------------------------------------------


class FakeTool:
    """Duck-typed HarnessTool: the loop only needs name/schema()/executor."""

    def __init__(self, name, fn, params=None):
        self.name = name
        self._fn = fn
        self._params = params or {"type": "object", "properties": {}}
        self.calls = []

    def schema(self):
        return {"type": "function",
                "function": {"name": self.name, "description": "", "parameters": self._params}}

    def executor(self, args, ctx):
        self.calls.append(args)
        return self._fn(args, ctx)


def scripted(*turns):
    """A stream_fn that replays ``turns`` — each a list of (kind, payload) pairs — one per call,
    recording the messages it was handed so tests can assert on the conversation built up."""
    seen = []

    def _stream(messages, schemas):
        seen.append([dict(m) for m in messages])
        turn = turns[min(len(seen) - 1, len(turns) - 1)]
        yield from turn

    _stream.seen = seen
    return _stream


def tool_call(name, args, call_id="call_1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


# --- the loop -----------------------------------------------------------------


def test_answers_in_one_turn_without_tools():
    """The base case and the whole point of the path: text streams out and the loop stops.
    No tool call means exactly ONE model turn — no speculative second round."""
    stream = scripted([("content", "CADD 25 is "), ("content", "roughly the top 0.3%.")])
    events = []
    res = run_quick_chat(stream_fn=stream, tools=[], question="what is CADD 25?",
                         on_event=events.append)

    assert res.text == "CADD 25 is roughly the top 0.3%."
    assert res.turns == 1
    assert res.tool_calls == []
    assert not res.hit_turn_limit
    # Tokens are emitted as they arrive — that is what the client streams into the bubble.
    assert [e["token"] for e in events if e["type"] == "content"] == \
        ["CADD 25 is ", "roughly the top 0.3%."]


def test_tokens_are_emitted_before_the_turn_ends():
    """Answer-first is a LATENCY claim, so assert the ordering that makes it true: the first
    content event must be emitted before the loop has finished consuming the turn."""
    order = []

    def _stream(messages, schemas):
        yield ("content", "Rod-cone dystrophy.")
        order.append("model-still-generating")

    def _record(ev):
        # Context accounting is emitted once up front, BEFORE any model call — it is not part
        # of the streaming contract under test here, so keep this assertion on the tokens.
        if not str(ev["type"]).startswith("context_"):
            order.append("emitted:" + ev["type"])

    run_quick_chat(stream_fn=_stream, tools=[], question="q", on_event=_record)
    assert order == ["emitted:content", "model-still-generating"]


def test_tool_call_result_is_fed_back_and_a_second_turn_answers():
    hits = FakeTool("literature_search", lambda a, c: {"summary": "3 papers", "papers": ["a"]})
    stream = scripted(
        [("content", "Let me confirm. "), ("tool_calls", [tool_call("literature_search", {"query": "RPE65"})])],
        [("content", "RPE65 causes LCA2.")],
    )
    events = []
    res = run_quick_chat(stream_fn=stream, tools=[hits], question="RPE65?", on_event=events.append)

    assert hits.calls == [{"query": "RPE65"}]
    assert res.turns == 2
    assert res.text == "Let me confirm. RPE65 causes LCA2."
    assert res.tool_calls == [{"tool": "literature_search", "args": {"query": "RPE65"}}]
    kinds = [e["type"] for e in events]
    assert "tool_start" in kinds and "tool_result" in kinds

    # The second turn must see a role:tool message carrying the matching tool_call_id — vLLM and
    # OpenRouter both 400 without it, which is the bug this asserts against.
    second = stream.seen[1]
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert "3 papers" in tool_msgs[0]["content"]


def test_tool_exception_becomes_data_for_the_model_not_a_dead_turn():
    """A tool that raises must not kill the user's chat turn: the error goes back to the model,
    which answers anyway."""
    def boom(_a, _c):
        raise RuntimeError("Europe PMC unreachable")

    broken = FakeTool("literature_search", boom)
    stream = scripted(
        [("tool_calls", [tool_call("literature_search", {"query": "x"})])],
        [("content", "I could not reach the literature service, but from memory: …")],
    )
    events = []
    res = run_quick_chat(stream_fn=stream, tools=[broken], question="q", on_event=events.append)

    assert res.turns == 2
    assert res.text.startswith("I could not reach")
    assert [e for e in events if e["type"] == "tool_error"]
    fed = json.loads([m for m in stream.seen[1] if m["role"] == "tool"][0]["content"])
    assert fed["status"] == "error" and "Europe PMC unreachable" in fed["error"]


def test_unknown_tool_is_reported_to_the_model():
    stream = scripted(
        [("tool_calls", [tool_call("run_code", {"code": "1"})])],
        [("content", "I can't run analyses in chat mode.")],
    )
    res = run_quick_chat(stream_fn=stream, tools=[], question="q")
    fed = json.loads([m for m in stream.seen[1] if m["role"] == "tool"][0]["content"])
    assert fed["status"] == "error" and "unknown tool" in fed["error"]
    assert res.text == "I can't run analyses in chat mode."


def test_turn_limit_is_enforced_and_flagged():
    """A model that calls a tool forever must be stopped, and the caller must be able to TELL —
    otherwise the UI presents a truncated answer as if it were complete."""
    t = FakeTool("literature_search", lambda a, c: {"summary": "ok"})
    forever = scripted([("content", "…"), ("tool_calls", [tool_call("literature_search", {"query": "q"})])])
    res = run_quick_chat(stream_fn=forever, tools=[t], question="q",
                         config=QuickChatConfig(max_turns=3, max_tool_calls=99))
    assert res.turns == 3
    assert res.hit_turn_limit is True


def test_tool_budget_exhaustion_asks_the_model_to_wrap_up():
    t = FakeTool("literature_search", lambda a, c: {"summary": "ok"})
    forever = scripted([("tool_calls", [tool_call("literature_search", {"query": "q"})])])
    res = run_quick_chat(stream_fn=forever, tools=[t], question="q",
                         config=QuickChatConfig(max_turns=4, max_tool_calls=1))
    assert len(res.tool_calls) == 1                 # budget respected
    assert len(t.calls) == 1                       # and the tool really only ran once
    wrapup = [m for m in forever.seen[-1]
              if m["role"] == "user" and "Tool budget exhausted" in str(m.get("content"))]
    assert wrapup, "the model must be told in-band, not silently ignored"


def test_cancel_stops_mid_stream():
    """Stop has to land DURING generation — a long answer is exactly when the user reaches for it."""
    fired = {"n": 0}

    def _stream(messages, schemas):
        for i in range(10):
            fired["n"] += 1
            yield ("content", f"token{i} ")

    res = run_quick_chat(stream_fn=_stream, tools=[], question="q",
                         should_cancel=lambda: fired["n"] >= 3)
    assert res.stopped is True
    assert fired["n"] == 3           # stopped promptly, not after draining the generator


def test_history_is_carried_and_trimmed():
    stream = scripted([("content", "yes")])
    history = [{"role": "user", "content": f"m{i}"} for i in range(30)]
    run_quick_chat(stream_fn=stream, tools=[], question="and that one?", history=history,
                   config=QuickChatConfig(max_history_messages=4))
    msgs = stream.seen[0]
    assert msgs[0]["role"] == "system"
    # 4 carried history turns (the LAST four) + the new question
    assert [m["content"] for m in msgs[1:]] == ["m26", "m27", "m28", "m29", "and that one?"]


def test_thinking_is_separated_from_content():
    stream = scripted([("thinking", "hmm "), ("content", "answer")])
    events = []
    res = run_quick_chat(stream_fn=stream, tools=[], question="q", on_event=events.append)
    assert res.text == "answer" and res.thinking == "hmm "
    assert [e["type"] for e in events if not e["type"].startswith("context_")] == \
        ["thinking", "content"]


@pytest.mark.parametrize("raw,expected", [
    ('{"query": "x"}', {"query": "x"}),
    ({"query": "x"}, {"query": "x"}),
    ("", {}),
    ("not json", {}),
    ("[1,2]", {}),          # valid JSON, wrong shape
])
def test_parse_args_is_total(raw, expected):
    """Malformed tool arguments are a normal LLM outcome; they must never raise."""
    assert _parse_args(raw) == expected


# --- the streaming transport --------------------------------------------------


def sse(*events):
    body = "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
    return io.BytesIO(body.encode("utf-8"))


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def stub_urlopen(monkeypatch, payload_sink, body):
    def _open(req, timeout=None):
        payload_sink.append(json.loads(req.data.decode("utf-8")))
        return FakeResp(body.getvalue())
    monkeypatch.setattr(vllm_client.urllib.request, "urlopen", _open)


def test_chat_tools_stream_yields_content_then_assembled_tool_calls(monkeypatch):
    """The transport contract: tokens stream as they arrive, and fragmented tool-call deltas are
    reassembled into ONE call with concatenated arguments."""
    sent = []
    stub_urlopen(monkeypatch, sent, sse(
        {"choices": [{"delta": {"content": "Looking. "}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "literature_search", "arguments": '{"que'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'ry": "RPE65"}'}}]}}]},
    ))
    out = list(vllm_client.chat_tools_stream(1234, "m", [{"role": "user", "content": "q"}],
                                            [{"type": "function"}]))
    assert out[0] == ("content", "Looking. ")
    kind, calls = out[-1]
    assert kind == "tool_calls"
    assert calls == [{"id": "c1", "type": "function",
                      "function": {"name": "literature_search", "arguments": '{"query": "RPE65"}'}}]
    assert json.loads(calls[0]["function"]["arguments"]) == {"query": "RPE65"}


def test_chat_tools_stream_defaults_thinking_off(monkeypatch):
    """think=False by default is the latency decision, not an accident — pin it."""
    sent = []
    stub_urlopen(monkeypatch, sent, sse({"choices": [{"delta": {"content": "hi"}}]}))
    list(vllm_client.chat_tools_stream(1234, "m", [], None))
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent[0]["stream"] is True
    assert "tools" not in sent[0]          # no tools passed → no tool_choice forced


def test_chat_tools_stream_emits_nothing_extra_without_tool_calls(monkeypatch):
    sent = []
    stub_urlopen(monkeypatch, sent, sse(
        {"choices": [{"delta": {"reasoning_content": "th"}}]},
        {"choices": [{"delta": {"content": "ans"}}]},
    ))
    out = list(vllm_client.chat_tools_stream(1234, "m", [], [], think=True))
    assert out == [("thinking", "th"), ("content", "ans")]
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": True}


def test_registry_quickchat_catalog_is_the_cheap_subset():
    """The fast path must NOT be able to reach the heavy analysis line. If a future tool lands in
    this catalog by accident, a chat turn could launch a multi-hour Slurm job with no run bundle to
    write into — so pin both the allow-list AND the exclusions."""
    from bioagent.agents.registry import build_quickchat_catalog

    names = {t.name for t in build_quickchat_catalog()}
    assert names == {"literature_search", "map_phenotype_to_hpo"}
    for heavy in ("run_code", "annotate_variants", "run_lirical", "run_scanpy_qc",
                  "run_clustering", "run_de", "run_enrichment", "scgpt_annotate",
                  "make_schematic"):
        assert heavy not in names, f"{heavy} must not be reachable from the fast chat path"
    # Every entry still has to be a usable tool schema.
    for t in build_quickchat_catalog():
        fn = t.schema()["function"]
        assert fn["name"] == t.name and fn["description"] and "properties" in fn["parameters"]


def test_schematic_tool_is_untouched_on_the_research_path():
    """Feature B adds INLINE chat diagrams; it must not have removed the backend figure tool."""
    from bioagent.agents.registry import build_scientist_catalog

    assert "make_schematic" in {t.name for t in build_scientist_catalog()}


def test_system_prompt_states_the_load_bearing_contracts():
    """Three behaviours the fast path depends on live only in the prompt, so regressions there are
    invisible: answer-first ordering, the refusal to fake an analysis, and the mermaid affordance
    that Feature B renders."""
    from bioagent.agents.quick_chat import QUICK_CHAT_SYSTEM

    lowered = QUICK_CHAT_SYSTEM.lower()
    assert "answer first" in lowered
    assert "mermaid" in lowered                     # ties feature A to feature B
    assert "research mode" in lowered               # tells the user where analysis actually happens
    assert "never invent" in lowered
    # Label-syntax guidance: verified against live Qwen3.6, whose mermaid labels default to `\n` /
    # <br> / <b> and punctuation-heavy text, which the htmlLabels:false renderer draws as literal
    # characters or fails to parse. Without this steer, real diagrams degrade to source or render
    # ugly. (frontend/console/app.js also normalises the source as a deterministic backstop.)
    assert "single-line" in lowered
    assert "\\n" in QUICK_CHAT_SYSTEM               # tells the model NOT to use a literal \n
    # Anti-fabrication of tool use: live Qwen3.6-35B-A3B-AWQ occasionally narrated "I have
    # retrieved literature from PubMed" WITHOUT calling literature_search (~1 in 4 runs). The
    # model must not claim a lookup it did not actually run.
    assert "actually called a tool" in lowered


def test_multiple_parallel_tool_calls_keep_their_indexes(monkeypatch):
    sent = []
    stub_urlopen(monkeypatch, sent, sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "b", "function": {"name": "two", "arguments": "{}"}},
            {"index": 0, "id": "a", "function": {"name": "one", "arguments": "{}"}}]}}]},
    ))
    (kind, calls), = list(vllm_client.chat_tools_stream(1234, "m", [], []))
    assert kind == "tool_calls"
    assert [c["function"]["name"] for c in calls] == ["one", "two"]   # sorted by index
