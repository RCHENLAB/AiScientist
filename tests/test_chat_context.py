"""Context awareness + compaction for the fast chat path (``agents/chat_context.py``,
driven through ``agents/quick_chat.run_quick_chat``).

Everything here is OFFLINE: the token counter, the summarizer and the model stream are all
injected, and — as with ``tests/test_quick_chat.py`` — nothing imports
``bioagent.gateway.app`` (which needs ``paramiko``), so this file runs on a bare checkout.

The behaviour under test is the one the old 12-message cutoff got wrong: a long conversation
must keep its memory (in condensed form) instead of silently forgetting its own opening, and
it must say how full it is — while never, under any failure of the machinery that does that,
breaking the user's chat turn.
"""

from __future__ import annotations

import pathlib

import pytest

from bioagent.agents.chat_context import (
    ChatContextLimits,
    build_chat_messages,
)
from bioagent.agents.quick_chat import QuickChatConfig, run_quick_chat


# --- helpers ------------------------------------------------------------------


def scripted(*turns):
    """A stream_fn replaying ``turns``, recording the messages it was handed."""
    seen = []

    def _stream(messages, schemas):
        seen.append([dict(m) for m in messages])
        yield from turns[min(len(seen) - 1, len(turns) - 1)]

    _stream.seen = seen
    return _stream


def chat(n_exchanges: int, chars: int = 40) -> list[dict[str, str]]:
    """``n`` user/assistant exchanges, each message padded to ``chars`` so a test can dial
    the conversation's token weight without depending on the estimator's constants."""
    out: list[dict[str, str]] = []
    for i in range(n_exchanges):
        out.append({"role": "user", "content": f"Q{i} " + "x" * chars})
        out.append({"role": "assistant", "content": f"A{i} " + "y" * chars})
    return out


def counter(value):
    """An exact token counter: a fixed int, a callable, or None (= 'cannot count')."""
    calls = []

    def _count(messages, tools):
        calls.append((len(messages), len(tools or [])))
        return value(messages) if callable(value) else value

    _count.calls = calls
    return _count


def summarizer(text="Earlier: the user asked about RPE65 and LCA2; we agreed to check ClinVar."):
    """A summarizer that records the prompt it was given and returns ``text``."""
    seen = []

    def _summarize(messages, max_tokens):
        seen.append({"messages": messages, "max_tokens": max_tokens})
        return text(messages) if callable(text) else text

    _summarize.seen = seen
    return _summarize


def types_of(events):
    return [e["type"] for e in events]


def user_visible(messages):
    """The message contents, minus the (single) system slot, as a flat list."""
    return [m["content"] for m in messages if m["role"] != "system"]


# --- a short chat is left completely alone ------------------------------------


def test_short_chat_is_untouched_no_summary_no_trim():
    """The common case must cost nothing: no summarizer call, no dropped turns, no
    ``context_trimmed`` — just the conversation, verbatim, plus one occupancy report."""
    summarize = summarizer()
    events = []
    report = build_chat_messages(system="SYS", question="and RPE65?", history=chat(3),
                                 summarize_fn=summarize, on_event=events.append)

    assert report.compacted is False
    assert report.summary == "" and report.summarized_messages == 0 and report.dropped_messages == 0
    assert summarize.seen == [], "a chat that fits must never pay for a summarizer call"
    assert types_of(events) == ["context_measured"]
    # system + 6 history messages + the question, in order and unmodified.
    assert [m["role"] for m in report.messages] == \
        ["system", "user", "assistant", "user", "assistant", "user", "assistant", "user"]
    assert user_visible(report.messages)[-1] == "and RPE65?"


def test_short_chat_through_the_loop_emits_measured_but_not_trimmed():
    stream = scripted([("content", "sure")])
    events = []
    res = run_quick_chat(stream_fn=stream, tools=[], question="q", history=chat(2),
                         on_event=events.append, summarize_fn=summarizer())

    assert res.context_compacted is False
    assert res.context_used > 0 and res.context_allowed > 0
    assert "context_trimmed" not in types_of(events)
    assert [e for e in events if e["type"] == "context_measured"]


# --- compaction: recent verbatim, older summarized ----------------------------


def _tight(**kw) -> ChatContextLimits:
    """A deliberately small budget so compaction fires on a manageable conversation — but
    still roomy enough for the summary PLUS the 3 verbatim exchanges, so these tests exercise
    the summarize-the-older-half path rather than the shed-the-tail last resort."""
    base = {"max_prompt_tokens": 900, "max_model_len": 131072, "output_reserve_tokens": 2048,
            "keep_last_exchanges": 3}
    base.update(kw)
    return ChatContextLimits(**base)


def test_compaction_keeps_the_last_n_exchanges_verbatim():
    """The load-bearing half of the design: recent wording is never paraphrased, because the
    user says "that gene" / "the second one" and a summary would destroy the referent."""
    summarize = summarizer()
    history = chat(12, chars=200)
    report = build_chat_messages(system="SYS", question="now what?", history=history,
                                 config=_tight(), summarize_fn=summarize)

    assert report.compacted is True
    tail = [m["content"] for m in history[-6:]]          # the last 3 exchanges = 6 messages
    body = user_visible(report.messages)
    assert body[-1] == "now what?"
    assert body[:-1] == tail, "the last N exchanges must survive byte-for-byte"


def test_the_injected_summary_actually_carries_older_content_forward():
    """Memory, not just truncation: the evicted turns have to reach the model somehow."""
    summarize = summarizer("The user is investigating RPE65 in a Leber congenital amaurosis case.")
    report = build_chat_messages(system="SYS", question="next?", history=chat(12, chars=200),
                                 config=_tight(), summarize_fn=summarize)

    # The memory is SPLICED INTO the single system message — never a second one. Qwen3.6's own
    # chat template raises 'System message must be at the beginning' for a non-first system
    # message, so a second one would 400 the turn (verified against the real staged template).
    systems = [m for m in report.messages if m["role"] == "system"]
    assert len(systems) == 1, "exactly ONE system message — a second one breaks the chat template"
    assert systems[0] is report.messages[0], "and it is the first message"
    assert "RPE65 in a Leber congenital amaurosis case" in systems[0]["content"]
    assert systems[0]["content"].startswith("SYS"), "the chat contract stays at the top"
    assert report.summary.startswith("The user is investigating")
    assert report.summarized_messages > 0
    # The memory sits inside that system message, ahead of every conversation turn, so the model
    # reads it as established background rather than as the newest thing the user said.
    assert report.messages[1]["role"] != "system"

    # And the summarizer was handed the OLD turns — the ones being evicted, not the recent ones.
    prompt = summarize.seen[0]["messages"][-1]["content"]
    assert "Q0" in prompt and "A0" in prompt


def test_summary_is_rebuilt_incrementally_from_the_previous_one():
    """Cost must stay flat as the chat grows: turn N folds the PREVIOUS summary plus only the
    newly-evicted turns, never the whole history again."""
    summarize = summarizer("rolling memory v2")
    report = build_chat_messages(
        system="SYS", question="next?", history=chat(12, chars=200), config=_tight(),
        prior_summary="rolling memory v1", prior_summary_through=10, summarize_fn=summarize)

    prompt = summarize.seen[0]["messages"][-1]["content"]
    assert "PREVIOUS SUMMARY:\nrolling memory v1" in prompt
    assert "Q0" not in prompt, "already-summarized turns must not be re-read"
    assert "Q5" in prompt, "the newly-evicted turns must be"
    assert report.summary == "rolling memory v2"
    assert report.summary_through > 10


def test_memory_survives_across_consecutive_turns():
    """The whole feature, end to end: a fact stated at the very start of a long conversation
    is still in the prompt many turns later, even though those turns are long gone verbatim.
    This is exactly what the old 12-message cutoff could not do.

    Drives the loop the way the gateway does — hand each turn's ``summary`` /
    ``summary_through`` back into the next one.
    """
    fact = "the patient is a 6-year-old with RPE65-related LCA"
    history = [{"role": "user", "content": f"Case: {fact}. " + "background " * 40},
               {"role": "assistant", "content": "Noted. " + "context " * 40}]
    cfg = QuickChatConfig(max_prompt_tokens=900, keep_last_exchanges=2)

    # A summarizer that behaves like the real one: it folds the previous summary and the newly
    # evicted turns into a new briefing, so the original fact keeps propagating.
    def summarize(messages, max_tokens):
        blob = messages[-1]["content"]
        return f"MEMO({fact})" if fact in blob else blob.split("PREVIOUS SUMMARY:\n")[-1][:60]

    summary, through, prompts = "", 0, []
    for i in range(6):
        stream = scripted([("content", f"answer {i}")])
        res = run_quick_chat(stream_fn=stream, tools=[], question=f"follow-up {i}",
                             history=list(history), config=cfg, summarize_fn=summarize,
                             prior_summary=summary, prior_summary_through=through)
        summary, through = res.summary, res.summary_through
        prompts.append(stream.seen[0])
        history.append({"role": "user", "content": f"follow-up {i}"})
        history.append({"role": "assistant", "content": res.text + " " + "filler " * 40})

    last = prompts[-1]
    # The opening turn is long gone from the verbatim window…
    assert not any(fact in m["content"] for m in last if m["role"] != "system")
    # …but it is still in the prompt, carried by the rolling memory.
    assert any(f"MEMO({fact})" in m["content"] for m in last if m["role"] == "system")
    assert through > 0


def test_summary_through_is_returned_so_the_next_turn_can_resume():
    report = build_chat_messages(system="SYS", question="q", history=chat(12, chars=200),
                                 config=_tight(), summarize_fn=summarizer())
    assert 0 < report.summary_through < 24
    # Fed back with a history that no longer matches (the client edited/reordered it), the
    # memory resets instead of slicing at a bogus index.
    reset = build_chat_messages(system="SYS", question="q", history=chat(1),
                                config=_tight(), prior_summary="stale",
                                prior_summary_through=999, summarize_fn=summarizer())
    assert reset.summary == "" and reset.summary_through == 0


# --- token counting: exact when available, estimate when not ------------------


def test_exact_counter_is_used_when_it_answers():
    count = counter(1234)
    events = []
    report = build_chat_messages(system="SYS", question="q", history=chat(2),
                                 count_tokens_fn=count, on_event=events.append)
    assert report.used_tokens == 1234 and report.exact is True
    assert count.calls, "the injected counter must actually be consulted"
    measured = [e for e in events if e["type"] == "context_measured"][0]
    assert measured["exact_tokens"] == 1234 and measured["exact"] is True


def test_estimate_is_used_when_the_counter_returns_none():
    """``vllm_client.count_tokens`` returns None for a remote base_url or an older server —
    that is a supported state, not an error, and occupancy must still be reported."""
    events = []
    report = build_chat_messages(system="SYS", question="q", history=chat(4),
                                 count_tokens_fn=counter(None), on_event=events.append)
    assert report.exact is False
    assert report.used_tokens > 0, "a char estimate is still a number the UI can show"
    assert [e for e in events if e["type"] == "context_measured"][0]["exact"] is False


def test_a_raising_counter_degrades_to_the_estimate():
    def boom(messages, tools):
        raise RuntimeError("tunnel died")

    report = build_chat_messages(system="SYS", question="q", history=chat(4), count_tokens_fn=boom)
    assert report.exact is False and report.used_tokens > 0


def test_exact_count_over_budget_forces_compaction_the_estimate_would_have_missed():
    """The estimate can undershoot dense text. When the SERVER says we are over, we compact
    anyway — that is the whole point of paying for /tokenize."""
    summarize = summarizer()
    # Tiny history (the estimate says it fits) but the server reports a huge prompt.
    report = build_chat_messages(system="SYS", question="q", history=chat(8),
                                 config=ChatContextLimits(max_prompt_tokens=1000,
                                                          keep_last_exchanges=2),
                                 count_tokens_fn=counter(50_000), summarize_fn=summarize)
    assert report.compacted is True
    assert summarize.seen, "the older turns should have been folded into the memory"


# --- degradation: nothing here may break a chat turn --------------------------


@pytest.mark.parametrize("bad,label", [
    (lambda m, t: (_ for _ in ()).throw(RuntimeError("vLLM 500")), "raises"),
    (lambda m, t: "", "empty"),
    (lambda m, t: "  \n ", "whitespace"),
    (lambda m, t: "ok", "too short to be a summary"),
    (lambda m, t: None, "returns None"),
])
def test_a_broken_summarizer_degrades_to_dropping_the_oldest_turns(bad, label):
    """Compaction must NEVER be the thing that breaks a chat turn. Every summarizer failure
    mode falls back to exactly the old behaviour: drop the oldest, keep the recent."""
    history = chat(12, chars=200)
    report = build_chat_messages(system="SYS", question="now what?", history=history,
                                 config=_tight(), summarize_fn=bad)

    assert report.summary == "", f"{label}: junk must not be injected as memory"
    assert [m["role"] for m in report.messages].count("system") == 1
    assert report.summarized_messages == 0
    assert report.dropped_messages > 0, f"{label}: it degrades by dropping, not by keeping all"
    body = user_visible(report.messages)
    assert body[-1] == "now what?"
    assert body[:-1] == [m["content"] for m in history[-6:]]   # last 3 exchanges intact


def test_no_summarizer_at_all_still_compacts():
    """The bare-checkout / offline case: nothing injected, so it behaves exactly as the
    fast path did before this feature existed."""
    report = build_chat_messages(system="SYS", question="q", history=chat(12, chars=200),
                                 config=_tight())
    assert report.compacted is True and report.summary == "" and report.dropped_messages > 0


def test_a_broken_summarizer_does_not_break_the_chat_turn_end_to_end():
    def boom(messages, max_tokens):
        raise RuntimeError("summarizer exploded")

    stream = scripted([("content", "answered anyway")])
    res = run_quick_chat(stream_fn=stream, tools=[], question="q", history=chat(12, chars=200),
                         config=QuickChatConfig(max_prompt_tokens=900, keep_last_exchanges=3),
                         summarize_fn=boom)
    assert res.text == "answered anyway"
    assert res.context_compacted is True and res.summary == ""


# --- the events the UI reads --------------------------------------------------


def test_context_events_carry_sane_numbers():
    events = []
    report = build_chat_messages(system="SYS", question="q", history=chat(12, chars=200),
                                 config=_tight(), summarize_fn=summarizer(),
                                 count_tokens_fn=None, on_event=events.append)

    assert types_of(events) == ["context_trimmed", "context_measured"]
    trimmed, measured = events
    assert trimmed["compressed_turns"] >= 1
    assert trimmed["dropped_turns"] >= 0
    assert trimmed["approx_tokens"] == measured["exact_tokens"] == report.used_tokens
    assert 0 < measured["exact_tokens"] <= measured["allowed"]
    assert measured["allowed"] == report.allowed_tokens == 900
    # The vocabulary is deliberately the research path's, so gateway renderers work unchanged.
    assert set(measured) >= {"type", "exact_tokens", "allowed"}
    assert set(trimmed) >= {"type", "compressed_turns", "dropped_turns", "approx_tokens"}


def test_occupancy_is_reported_on_every_turn_even_when_nothing_happens():
    """"Awareness" means the number is always available — the indicator must not go blank
    just because this particular turn needed no compaction."""
    events = []
    build_chat_messages(system="SYS", question="q", history=[], on_event=events.append)
    measured = [e for e in events if e["type"] == "context_measured"]
    assert len(measured) == 1 and measured[0]["exact_tokens"] > 0


def test_result_exposes_the_numbers_the_console_shows():
    res = run_quick_chat(stream_fn=scripted([("content", "hi")]), tools=[], question="q",
                         history=chat(2), count_tokens_fn=counter(999))
    assert (res.context_used, res.context_exact) == (999, True)
    assert res.context_allowed == QuickChatConfig().allowed_prompt_tokens


# --- the budget itself --------------------------------------------------------


def test_budget_is_bounded_well_below_the_served_window():
    """Chat self-limits to ~24K even though prod serves 131072: prefill time is what the fast
    path is buying down, and a 100K prompt would spend it all before the first token."""
    cfg = ChatContextLimits(max_model_len=131072)
    assert cfg.max_prompt_tokens == 24_000
    assert cfg.allowed_prompt_tokens == 24_000


def test_budget_is_clamped_to_the_real_window_minus_the_output_reserve():
    """A small served window WINS over chat's own ceiling — otherwise we would build a prompt
    the server rejects outright."""
    cfg = ChatContextLimits(max_prompt_tokens=24_000, max_model_len=8192,
                            output_reserve_tokens=2048)
    assert cfg.allowed_prompt_tokens == 8192 - 2048

    # …and the compactor really enforces the clamped number, not the raw ceiling.
    report = build_chat_messages(system="SYS", question="q", history=chat(60, chars=300),
                                 config=ChatContextLimits(max_prompt_tokens=24_000,
                                                          max_model_len=4096,
                                                          output_reserve_tokens=2048,
                                                          keep_last_exchanges=2))
    assert report.allowed_tokens == 2048
    assert report.used_tokens <= 2048


def test_a_degenerate_window_still_leaves_working_room():
    """Never return a zero/negative budget: a mis-set window must not make chat unusable."""
    cfg = ChatContextLimits(max_prompt_tokens=24_000, max_model_len=512, output_reserve_tokens=2048)
    assert cfg.allowed_prompt_tokens >= 512


def test_env_overrides_the_budget_and_the_verbatim_window(monkeypatch):
    monkeypatch.setenv("BIOAGENT_CHAT_MAX_PROMPT_TOKENS", "9000")
    monkeypatch.setenv("BIOAGENT_CHAT_KEEP_EXCHANGES", "2")
    cfg = ChatContextLimits()
    assert cfg.max_prompt_tokens == 9000 and cfg.keep_last_exchanges == 2
    # Garbage in the env must not take the console down with it.
    monkeypatch.setenv("BIOAGENT_CHAT_MAX_PROMPT_TOKENS", "not-a-number")
    assert ChatContextLimits().max_prompt_tokens == 24_000


def test_quickchat_config_exposes_the_context_knobs():
    """The fast path's one config object stays the single place a caller tunes chat."""
    cfg = QuickChatConfig(max_prompt_tokens=1234, keep_last_exchanges=2)
    assert (cfg.max_prompt_tokens, cfg.keep_last_exchanges) == (1234, 2)
    assert cfg.max_turns == 4 and cfg.max_tool_calls == 6      # the loop bounds are untouched


# --- edges --------------------------------------------------------------------


def test_the_verbatim_tail_is_shed_when_even_it_will_not_fit():
    """One pasted 30K-char message can blow the budget on its own. Shedding it is correct;
    raising, or emitting a prompt the server will reject, is not."""
    history = [{"role": "user", "content": "x" * 20000},
               {"role": "assistant", "content": "y" * 20000},
               {"role": "user", "content": "short follow-up"},
               {"role": "assistant", "content": "short answer"}]
    report = build_chat_messages(system="SYS", question="q", history=history,
                                 config=ChatContextLimits(max_prompt_tokens=800,
                                                          keep_last_exchanges=4))
    assert report.compacted is True
    assert user_visible(report.messages) == ["short follow-up", "short answer", "q"]
    assert report.dropped_messages >= 2


def test_tool_schemas_count_toward_occupancy():
    """The catalog is resent every turn and counts toward the prompt; leaving it out of the
    estimate would understate how full the window is."""
    schemas = [{"type": "function", "function": {"name": "literature_search", "description": "x" * 3000,
                                                 "parameters": {"type": "object", "properties": {}}}}]
    without = build_chat_messages(system="SYS", question="q", history=chat(1))
    with_tools = build_chat_messages(system="SYS", question="q", history=chat(1),
                                     tool_schemas=schemas)
    assert with_tools.used_tokens > without.used_tokens

    # …and the exact counter is handed those same schemas, so its number includes them too.
    count = counter(10)
    build_chat_messages(system="SYS", question="q", history=chat(1), tool_schemas=schemas,
                        count_tokens_fn=count)
    assert count.calls[0][1] == 1


def test_non_conversation_history_entries_are_ignored():
    history = [{"role": "system", "content": "leak"}, {"role": "user", "content": ""},
               {"role": "tool", "content": "junk"}, {"role": "user", "content": "real"}]
    report = build_chat_messages(system="SYS", question="q", history=history)
    assert user_visible(report.messages) == ["real", "q"]


def test_the_system_prompt_is_never_compacted_away():
    report = build_chat_messages(system="LOAD-BEARING CONTRACT", question="q",
                                 history=chat(60, chars=400), config=_tight(),
                                 summarize_fn=summarizer())
    # Compaction may APPEND memory to it, but the contract itself must survive verbatim and stay
    # first — and it must remain the only system message (see the chat-template note above).
    assert report.messages[0]["role"] == "system"
    assert report.messages[0]["content"].startswith("LOAD-BEARING CONTRACT")
    assert [m["role"] for m in report.messages].count("system") == 1


def test_empty_history_is_a_normal_first_turn():
    report = build_chat_messages(system="SYS", question="hello")
    assert report.compacted is False
    assert report.messages == [{"role": "system", "content": "SYS"},
                               {"role": "user", "content": "hello"}]


# --- the served model's own chat template ------------------------------------
#
# The compacted prompt has to survive the REAL template, not just our assertions about shape.
# Qwen3.6's chat_template.jinja rejects any system message that is not the first:
#     {%- if message.role == "system" %}{%- if not loop.first %}
#         {{- raise_exception('System message must be at the beginning.') }}
# so injecting the conversation memory as a SECOND system message renders an exception and vLLM
# 400s the turn — every chat that compacted would fail, precisely when the memory matters. That
# is why _assemble splices the memory into the single system message. This test renders the real
# template (vendored from the staged QuantTrio/Qwen3.6-35B-A3B-AWQ snapshot on dfs3b) so the
# constraint is enforced by the model's own rules rather than by a comment.


def _qwen_template():
    jinja2 = pytest.importorskip("jinja2")
    path = pathlib.Path(__file__).parent / "fixtures" / "qwen3_chat_template.jinja"
    env = jinja2.Environment(loader=jinja2.BaseLoader())

    def raise_exception(msg):
        raise jinja2.exceptions.TemplateError(msg)

    env.globals["raise_exception"] = raise_exception
    return env.from_string(path.read_text(encoding="utf-8"))


def test_a_compacted_prompt_renders_through_the_real_qwen_chat_template():
    tpl = _qwen_template()
    report = build_chat_messages(system="SYS", question="next?", history=chat(12, chars=200),
                                 config=_tight(), summarize_fn=summarizer("earlier: RPE65 case"))
    assert report.compacted and report.summary, "this test is only meaningful once compaction fired"

    rendered = tpl.render(messages=report.messages, tools=None, add_generation_prompt=True)
    assert "earlier: RPE65 case" in rendered, "the memory must reach the model"
    assert rendered.count("<|im_start|>system") == 1


def test_a_second_system_message_would_break_that_template():
    """Pins WHY the memory is spliced in: the shape we deliberately avoid really does raise."""
    jinja2 = pytest.importorskip("jinja2")
    tpl = _qwen_template()
    with pytest.raises(jinja2.exceptions.TemplateError, match="System message must be at the beginning"):
        tpl.render(messages=[{"role": "system", "content": "SYS"},
                             {"role": "system", "content": "MEMORY"},
                             {"role": "user", "content": "q"}],
                   tools=None, add_generation_prompt=True)
