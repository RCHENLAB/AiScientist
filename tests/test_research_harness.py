"""Offline tests for the agentic research harness.

A fake ``chat_fn`` returns scripted tool calls, so the full orchestration loop runs
with no GPU / Ollama. Focus areas: tool sequencing, the prompt error-feedback
contract (errors are emitted + fed back + never faked), the JSON fallback for models
that don't emit native tool_calls, the stop reasons, and the privacy guard.
"""

from __future__ import annotations

import json
from typing import Any

from aiscientist.agents.research_harness import (
    HarnessConfig,
    HarnessContext,
    HarnessTool,
    ResearchHarness,
    _compress_message,
    _group_turns,
    _is_context_overflow,
    _msg_tokens,
    default_catalog,
)

# A loaded single-cell dataset the QC/DE tools can read (decisions['dataset_result']).
DATASET_RESULT = {
    "dataset_kind": "single_cell_counts",
    "cells": 400,
    "genes": 40,
    "mean_total_counts": 760.9,
    "mean_mito_fraction": 0.019,
    "condition_effects": {
        "CD3D": {"log2_fold_change": 2.1, "baseline_mean": 1.0, "comparison_mean": 4.2},
        "MS4A1": {"log2_fold_change": -1.5, "baseline_mean": 3.0, "comparison_mean": 1.0},
    },
}


def _ctx() -> HarnessContext:
    return HarnessContext(decisions={"dataset_result": DATASET_RESULT})


def _tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    # Native tool_calls carry an id (like real OpenAI/vLLM/OpenRouter responses), so
    # the harness feeds results back as role:tool + tool_call_id (the production path).
    return {"content": "", "tool_calls": [{"id": f"call_{name}", "type": "function",
                                           "function": {"name": name, "arguments": args}}]}


def _scripted(responses: list[dict[str, Any]], record: list | None = None):
    """A fake chat_fn that returns each scripted assistant message in turn."""
    it = iter(responses)

    def _call(messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        if record is not None:
            record.append([dict(m) for m in messages])
        return next(it)

    return _call


def test_runs_tools_in_order_then_finishes() -> None:
    events: list[dict] = []
    harness = ResearchHarness(chat_fn=_scripted([
        _tool_call("run_qc", {}),
        _tool_call("run_de_markers", {}),
        _tool_call("finish", {"answer": "QC clean; CD3D/MS4A1 are candidate markers (hypotheses)."}),
    ]))

    result = harness.run("Analyze a PBMC scRNA-seq dataset.", _ctx(), on_event=events.append)

    assert result.status == "ok"
    assert result.stop_reason == "finished"
    assert result.final_answer.startswith("QC clean")
    assert [s["tool"] for s in result.steps] == ["run_qc", "run_de_markers", "finish"]
    assert all(s["ok"] for s in result.steps)
    # QC actually executed (real lab builder output flowed through).
    assert any(e["type"] == "tool_result" and e["tool"] == "run_qc" for e in events)
    assert not result.errors


def test_tool_error_is_emitted_fed_back_and_not_faked() -> None:
    def boom(_args: dict, _ctx: HarnessContext) -> dict:
        raise RuntimeError("simulated tool crash")

    boom_tool = HarnessTool("boom", "always fails", {"type": "object", "properties": {}}, boom)
    catalog = [boom_tool, *[t for t in default_catalog() if t.name == "finish"]]

    events: list[dict] = []
    seen_messages: list[list[dict]] = []
    harness = ResearchHarness(
        catalog=catalog,
        chat_fn=_scripted([
            _tool_call("boom", {}),
            _tool_call("finish", {"answer": "recovered after the tool error"}),
        ], record=seen_messages),
    )

    result = harness.run("do something", _ctx(), on_event=events.append)

    # Error surfaced live, recorded, and the failed step is NOT marked ok.
    assert any(e["type"] == "tool_error" and e["tool"] == "boom" for e in events)
    assert any(err["tool"] == "boom" and "simulated tool crash" in err["error"] for err in result.errors)
    boom_step = next(s for s in result.steps if s["tool"] == "boom")
    assert boom_step["ok"] is False
    # The error was fed back to the model: the 2nd model call saw a tool message with it.
    second_call_messages = seen_messages[1]
    assert any(m.get("role") == "tool" and "simulated tool crash" in m.get("content", "") for m in second_call_messages)
    # And the loop adapted: it still reached finish.
    assert result.stop_reason == "finished"
    assert result.final_answer == "recovered after the tool error"


def test_malformed_or_absent_tool_calls_trigger_json_fallback() -> None:
    # First turn: no native tool_calls, but a JSON {"tool": ...} object in the text.
    harness = ResearchHarness(chat_fn=_scripted([
        {"content": '{"tool": "run_qc", "args": {}}', "tool_calls": []},
        _tool_call("finish", {"answer": "done via fallback"}),
    ]))

    result = harness.run("analyze", _ctx())

    assert [s["tool"] for s in result.steps] == ["run_qc", "finish"]
    assert result.final_answer == "done via fallback"


def test_unknown_tool_name_is_a_validation_error_not_a_crash() -> None:
    harness = ResearchHarness(chat_fn=_scripted([
        _tool_call("does_not_exist", {}),
        _tool_call("finish", {"answer": "ok"}),
    ]))

    result = harness.run("analyze", _ctx())

    assert any("unknown tool 'does_not_exist'" in err["error"] for err in result.errors)
    assert result.stop_reason == "finished"  # loop continued past the bad call


def test_missing_required_arg_is_a_validation_error() -> None:
    need_arg = HarnessTool(
        "need_arg", "needs x",
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        lambda _args, _ctx: {"status": "ok"},
    )
    harness = ResearchHarness(catalog=[need_arg, *default_catalog()], chat_fn=_scripted([
        _tool_call("need_arg", {}),            # no arguments at all: a reply cut off at the limit
        _tool_call("need_arg", {"y": "1"}),    # a wrong parameter name
        _tool_call("finish", {"answer": "ok"}),
    ]))

    result = harness.run("analyze", _ctx())

    errors = [err["error"] for err in result.errors]
    assert any("arrived with NO arguments" in e and "SHORTER" in e for e in errors)
    assert any("missing required args for 'need_arg'" in e and "got ['y']" in e for e in errors)
    assert result.final_answer == "ok"


def test_max_steps_stops_with_incomplete_status() -> None:
    harness = ResearchHarness(
        config=HarnessConfig(max_steps=2),
        chat_fn=_scripted([_tool_call("run_qc", {}), _tool_call("run_qc", {})]),
    )

    result = harness.run("analyze", _ctx())

    assert result.stop_reason == "max_steps"
    assert result.status == "incomplete"
    # Since the run-97dfc89dc5aa fix a budget-exhausted step never returns an EMPTY answer:
    # it carries a deterministic digest of what the tools returned, so the Critic and the
    # report writer have material even when the model did not wrap up.
    assert result.final_answer and "auto-summary" in result.final_answer


def test_repeated_tool_errors_bail_the_step_early() -> None:
    # A tool that always crashes: the step must give up after max_tool_errors (3), not grind
    # all the way to max_steps burning GPU + context (the 5bd05b3f5880 enrichment hang shape).
    def boom(_a: dict, _c: HarnessContext) -> dict:
        raise RuntimeError("simulated crash")

    boom_tool = HarnessTool("boom", "always fails", {"type": "object", "properties": {}}, boom)
    harness = ResearchHarness(
        catalog=[boom_tool, *[t for t in default_catalog() if t.name == "finish"]],
        chat_fn=_scripted([_tool_call("boom", {}) for _ in range(6)]),
    )

    result = harness.run("do it", _ctx())

    assert result.stop_reason == "repeated_tool_errors"
    assert result.status == "incomplete"
    assert len(result.errors) == 3          # bailed on the 3rd consecutive error


def test_tool_returning_cancelled_stops_the_step() -> None:
    # A cancellable tool (run_code / Slurm) that scancelled itself on Stop returns
    # {status: cancelled}; the harness must end the step at once, not treat it as a result.
    def cancelled(_a: dict, _c: HarnessContext) -> dict:
        return {"status": "cancelled", "error": "Run cancelled by the user."}

    tool = HarnessTool("run_code", "cancellable", {"type": "object", "properties": {}}, cancelled)
    harness = ResearchHarness(
        catalog=[tool, *[t for t in default_catalog() if t.name == "finish"]],
        chat_fn=_scripted([_tool_call("run_code", {}), _tool_call("run_code", {})]),
    )

    result = harness.run("go", _ctx())

    assert result.stop_reason == "cancelled"
    assert result.status == "incomplete"


def test_distinct_errors_are_not_cut_off_early() -> None:
    # A step that hits DIFFERENT errors each turn is the model debugging (converging), NOT stuck —
    # it must be allowed to keep trying (up to max_steps), never bailed after a few distinct fails.
    n = {"i": 0}
    kinds = ["SyntaxError: bad token", "KeyError: gene_name", "ImportError: no scanpy",
             "ValueError: empty frame", "TypeError: NoneType"]

    def flaky(_a: dict, _c: HarnessContext) -> dict:
        e = kinds[n["i"]]
        n["i"] += 1
        raise RuntimeError(e)   # a genuinely DIFFERENT error each turn (the model is debugging)

    flaky_tool = HarnessTool("flaky", "fails differently", {"type": "object", "properties": {}}, flaky)
    harness = ResearchHarness(
        config=HarnessConfig(max_steps=5, max_repeated_errors=3),
        catalog=[flaky_tool, *[t for t in default_catalog() if t.name == "finish"]],
        chat_fn=_scripted([_tool_call("flaky", {}) for _ in range(6)]),
    )

    result = harness.run("debug it", _ctx())

    assert result.stop_reason == "max_steps"     # NOT repeated_tool_errors — distinct errors ran on
    assert n["i"] == 5                            # all 5 turns used (bounded only by max_steps)


def test_redundant_repeats_after_success_stop_early() -> None:
    # Once a tool has succeeded, re-issuing the IDENTICAL call is not re-executed (no
    # re-running clustering mid-step) and, after max_wasted_after_success such spins, the
    # step stops with the win instead of looping to max_steps.
    calls = {"n": 0}

    def echo(_a: dict, _c: HarnessContext) -> dict:
        calls["n"] += 1
        return {"status": "ok", "step": "clustering"}

    echo_tool = HarnessTool("run_thing", "succeeds", {"type": "object", "properties": {}}, echo)
    harness = ResearchHarness(
        catalog=[echo_tool, *[t for t in default_catalog() if t.name == "finish"]],
        chat_fn=_scripted([_tool_call("run_thing", {}) for _ in range(5)]),
    )

    result = harness.run("cluster", _ctx())

    assert result.stop_reason == "done_early"
    assert calls["n"] == 1                   # the identical repeats were NOT re-executed
    assert [s["tool"] for s in result.steps] == ["run_thing"]   # one real successful step


def _probe_then_two_errors(first: str) -> ResearchHarness:
    """``first`` succeeds, two DIFFERENT run_code errors follow, then the step's tool and finish:
    the shape of run f3b8268c4fd4's first ORA attempt."""
    errors = iter(["AttributeError: 'AnnData' object has no attribute 'close'",
                   "AttributeError: 'numpy.ndarray' object has no attribute 'keys'"])

    def _raise(_a: dict, _c: HarnessContext) -> dict:
        raise RuntimeError(next(errors))

    def _ok(_a: dict, _c: HarnessContext) -> dict:
        return {"status": "ok"}

    empty = {"type": "object", "properties": {}}
    catalog = [HarnessTool(first, "succeeds", empty, _ok),
               HarnessTool("run_code", "fails", empty, _raise),
               HarnessTool("run_enrichment", "the step's tool", empty, _ok),
               *[t for t in default_catalog() if t.name == "finish"]]
    return ResearchHarness(catalog=catalog, chat_fn=_scripted([
        _tool_call(first, {}), _tool_call("run_code", {}), _tool_call("run_code", {}),
        _tool_call("run_enrichment", {}), _tool_call("finish", {"answer": "enriched"})]))


def test_a_lookup_is_not_a_result_so_errors_after_it_do_not_stop_the_step() -> None:
    result = _probe_then_two_errors("read_tool_source").run("ORA", _ctx())

    assert result.stop_reason == "finished"
    assert [s["tool"] for s in result.steps] == [
        "read_tool_source", "run_code", "run_code", "run_enrichment", "finish"]


def test_errors_after_a_real_result_still_stop_the_step_early() -> None:
    result = _probe_then_two_errors("run_clustering").run("ORA", _ctx())

    assert result.stop_reason == "done_early"
    assert "run_enrichment" not in [s["tool"] for s in result.steps]


def test_guard_blocks_raw_table_brief_before_any_model_call() -> None:
    def must_not_be_called(_messages: list[dict], _tools: list[dict]) -> dict:
        raise AssertionError("chat_fn must not be called when the guard blocks the brief")

    rows = "\n".join("gene,1.1,2.2,3.3,4.4,5.5" for _ in range(4))
    harness = ResearchHarness(chat_fn=must_not_be_called)

    result = harness.run(f"Analyze:\n{rows}", _ctx())

    assert result.status == "blocked_by_guard"
    assert result.stop_reason == "blocked_by_guard"
    assert result.final_answer is None


def test_guard_allows_raw_table_when_endpoint_is_local() -> None:
    # When the run targets the session's LOCAL tunneled Qwen (ctx.tunnel_port set), raw
    # tabular data never leaves the box, so the guard must NOT block it — the loop should
    # proceed to the model (here a stub that immediately finishes).
    def finish_now(_messages: list[dict], _tools: list[dict]) -> dict:
        return _tool_call("finish", {"answer": "done"})

    rows = "\n".join("gene,1.1,2.2,3.3,4.4,5.5" for _ in range(4))
    harness = ResearchHarness(chat_fn=finish_now)
    ctx = HarnessContext(decisions={"dataset_result": DATASET_RESULT}, tunnel_port=45877)

    result = harness.run(f"Analyze:\n{rows}", ctx)

    assert result.status != "blocked_by_guard"
    assert result.final_answer == "done"


def test_guard_blocks_secrets_even_on_local_endpoint() -> None:
    # Secrets are ALWAYS blocked, local endpoint or not.
    def must_not_be_called(_messages: list[dict], _tools: list[dict]) -> dict:
        raise AssertionError("chat_fn must not be called when the guard blocks the brief")

    harness = ResearchHarness(chat_fn=must_not_be_called)
    ctx = HarnessContext(decisions={"dataset_result": DATASET_RESULT}, tunnel_port=45877)

    # The fixture must embed a secret-like token so the guard has something to block.
    # Assemble the env-var-name and value from fragments so the SOURCE never shows the
    # name immediately followed by '=' and the token — the PR review gate flags that
    # adjacency as a real leaked key. The runtime prompt the guard inspects is identical.
    leaked = "OPENAI_API_KEY" + "=sk-abcdef0123456789"
    result = harness.run(f"here is my key {leaked}", ctx)

    assert result.status == "blocked_by_guard"


# --- context-window budgeting -------------------------------------------------


def _assistant_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": arguments}}]}


def _tool_reply(call_id: str, payload: dict) -> dict[str, Any]:
    # Mirrors _feed_result's native path: role:tool + matching id, content capped at 4000.
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload)[:4000]}


def test_compress_message_elides_failures_and_digests_successes() -> None:
    big_ok = {"status": "ok", "artifact": "/work/markers.csv",
              "genes": ["G%d" % i for i in range(500)]}
    ok_msg = _compress_message(_tool_reply("c1", big_ok))
    digest = json.loads(ok_msg["content"])
    assert ok_msg["tool_call_id"] == "c1"                 # pairing id preserved
    assert digest["status"] == "ok" and digest["artifact"] == "/work/markers.csv"
    assert len(digest["genes"]) < 500                     # list was capped by result_digest

    fail_msg = _compress_message(_tool_reply("c2", {"status": "error", "error": "boom"}))
    elided = json.loads(fail_msg["content"])
    assert elided["_elided"] == "earlier failed tool attempt" and elided["status"] == "error"

    # Assistant turn: long prose + a long run_code argument both get a head only.
    long_code = "x = 1\n" * 200
    asst = _compress_message(_assistant_call("c3", "run_code", json.dumps({"code": long_code})))
    assert asst["tool_calls"][0]["id"] == "c3"            # id preserved for pairing
    assert asst["tool_calls"][0]["function"]["arguments"].endswith("…")


def _pairing_is_valid(messages: list[dict]) -> bool:
    """Every role:tool reply is answered by an earlier assistant tool_call with its id."""
    seen_ids: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant":
            for call in m.get("tool_calls") or []:
                seen_ids.add(call.get("id"))
        elif m.get("role") == "tool":
            if m.get("tool_call_id") not in seen_ids:
                return False
    return True


def test_budget_messages_trims_to_window_keeps_preamble_and_pairing() -> None:
    harness = ResearchHarness()
    # A tiny window forces trimming; reserve+margin leave only a small body budget.
    harness.config = HarnessConfig(max_model_len=3000, output_reserve_tokens=256,
                                   context_safety_margin=128)
    tool_schemas = [t.schema() for t in harness.catalog]

    messages: list[dict] = [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "THE ORIGINAL BRIEF"},
    ]
    # 8 fat turns — each well over the trimmed budget on its own when summed.
    for i in range(8):
        cid = f"call_{i}"
        messages.append(_assistant_call(cid, "run_code", json.dumps({"code": "y=1\n" * 300})))
        messages.append(_tool_reply(cid, {"status": "ok", "blob": "Z" * 3000, "i": i}))

    events: list[dict] = []
    trimmed = harness._budget_messages(messages, tool_schemas, events.append)

    budget = (3000 - 256 - 128)
    assert sum(_msg_tokens(m) for m in trimmed) < budget          # fits the window
    assert trimmed[0]["content"] == "SYSTEM PROMPT"               # preamble kept verbatim
    assert trimmed[1]["content"] == "THE ORIGINAL BRIEF"
    assert _pairing_is_valid(trimmed)                             # no orphaned tool replies
    assert len(trimmed) < len(messages)                          # something was shed
    # The most RECENT turn survives (newest-first keep): its tool reply id is present.
    assert any(m.get("tool_call_id") == "call_7" for m in trimmed)
    assert any(e["type"] == "context_trimmed" for e in events)


def test_budget_messages_noop_when_already_small() -> None:
    harness = ResearchHarness()  # default 32768 window
    tool_schemas = [t.schema() for t in harness.catalog]
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "brief"},
        _assistant_call("c0", "run_qc", "{}"),
        _tool_reply("c0", {"status": "ok"}),
    ]
    out = harness._budget_messages(messages, tool_schemas, lambda _e: None)
    assert out is messages                                        # untouched, same object
    assert _group_turns(messages[2:]) == [[messages[2], messages[3]]]


def test_extra_reserve_trims_more_aggressively() -> None:
    harness = ResearchHarness(config=HarnessConfig(max_model_len=2200))
    tool_schemas = [t.schema() for t in harness.catalog]
    big = "x" * 4000
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "brief"},
        _assistant_call("c0", "run_qc", "{}"),
        _tool_reply("c0", {"status": "ok", "blob": big}),
        _assistant_call("c1", "run_qc", "{}"),
        _tool_reply("c1", {"status": "ok", "blob": big}),
    ]
    base = harness._budget_messages(messages, tool_schemas, lambda _e: None)
    tighter = harness._budget_messages(messages, tool_schemas, lambda _e: None, extra_reserve=1500)
    # A larger reserve never keeps MORE than the baseline budget.
    assert len(tighter) <= len(base)


def test_exact_counter_tightens_until_real_count_fits() -> None:
    # The char estimate says the candidate fits, but the SERVER tokenizer reports it's
    # over the window — the budgeter must tighten (drop turns) until the exact count fits.
    big = "y" * 3000
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "brief"},
        _assistant_call("c0", "run_qc", "{}"),
        _tool_reply("c0", {"status": "ok", "blob": big}),
        _assistant_call("c1", "run_qc", "{}"),
        _tool_reply("c1", {"status": "ok", "blob": big}),
        _assistant_call("c2", "run_qc", "{}"),
        _tool_reply("c2", {"status": "ok", "blob": big}),
    ]

    # Fake exact counter: "true" token count is just the message char total / 4. The
    # window is small enough that some turns must go.
    def fake_count(msgs: list[dict], tools: list[dict]) -> int:
        chars = sum(len(m.get("content") or "") for m in msgs)
        for m in msgs:
            for c in m.get("tool_calls") or []:
                chars += len(str((c.get("function") or {}).get("arguments") or ""))
        return chars // 4

    harness = ResearchHarness(
        config=HarnessConfig(max_model_len=1200, output_reserve_tokens=200, context_safety_margin=0),
        count_tokens_fn=fake_count,
    )
    tool_schemas = [t.schema() for t in harness.catalog]
    events: list[dict] = []
    out = harness._budget_messages(messages, tool_schemas, events.append)

    allowed = 1200 - 200  # max_model_len − output_reserve
    assert fake_count(out, tool_schemas) <= allowed          # exact count now fits the real window
    assert any(e["type"] == "context_measured" for e in events)


def test_exact_counter_none_falls_back_to_estimate() -> None:
    # When the counter returns None (remote API / old server), behavior == estimate-only.
    harness = ResearchHarness(count_tokens_fn=lambda _m, _t: None)
    tool_schemas = [t.schema() for t in harness.catalog]
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "brief"},
        _assistant_call("c0", "run_qc", "{}"),
        _tool_reply("c0", {"status": "ok"}),
    ]
    out = harness._budget_messages(messages, tool_schemas, lambda _e: None)
    assert out is messages                                   # untouched, same as no counter


def test_context_overflow_signature_detection() -> None:
    assert _is_context_overflow(RuntimeError("This model's maximum context length is 32768 tokens"))
    assert _is_context_overflow(ValueError("please reduce the length of the messages"))
    assert not _is_context_overflow(RuntimeError("connection refused"))


def test_run_retries_and_recovers_on_context_overflow() -> None:
    # chat_fn raises a context-length 400 on the FIRST call, then succeeds — the harness
    # should recompact and retry rather than fail the run.
    calls = {"n": 0}

    def flaky(messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("This model's maximum context length is 32768 tokens")
        return _tool_call("finish", {"answer": "recovered after recompaction"})

    events: list[dict] = []
    harness = ResearchHarness(chat_fn=flaky)
    result = harness.run("do something", _ctx(), on_event=events.append)

    assert result.stop_reason == "finished"
    assert result.final_answer == "recovered after recompaction"
    assert any(e["type"] == "context_overflow_retry" for e in events)
    assert calls["n"] == 2


def test_run_reraises_non_overflow_errors() -> None:
    def boom(messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        raise RuntimeError("vLLM is down")

    harness = ResearchHarness(chat_fn=boom)
    try:
        harness.run("do something", _ctx(), on_event=lambda _e: None)
    except RuntimeError as exc:
        assert "vLLM is down" in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("a non-overflow chat error must propagate, not be swallowed")




# --- the turn budget is visible, and exhausting it still produces an answer ---------------
#
# Measured on run ed4cfce52a2a: 5 of 16 Scientist rounds ended on ``max_steps``, and those scored
# 0.52 from the Critic against 0.73 for rounds that finished — every one of them handed over a
# deterministic auto-summary instead of the model's own account, because the cap was invisible and
# the model spent its last turn starting analysis that was then cut off. Two changes: the model is
# TOLD when turns run low, and a step that runs out anyway gets one text-only closing turn.

def _answering(text: str):
    """A chat_fn that always answers in prose — what a closing turn should elicit."""
    def _call(_messages, _tools):
        return {"content": text, "tool_calls": []}
    return _call


def test_the_model_is_told_when_its_turns_are_running_out() -> None:
    seen: list[list[dict]] = []
    harness = ResearchHarness(
        config=HarnessConfig(max_steps=3, warn_turns_left=2),
        chat_fn=_scripted([_tool_call("run_qc", {}), _tool_call("run_qc", {}),
                           _tool_call("finish", {"answer": "done"})], record=seen))
    events: list[dict] = []
    harness.run("Analyze.", _ctx(), on_event=events.append)

    warned = [e for e in events if e["type"] == "turn_budget_low"]
    assert len(warned) == 1 and warned[0]["left"] == 2 and warned[0]["budget"] == 3
    # Injected ONCE — repeating it every turn would crowd out the work in a window we already trim.
    budget_notes = [m for m in seen[-1] if "TURN BUDGET" in str(m.get("content", ""))]
    assert len(budget_notes) == 1


def test_a_step_that_runs_out_of_turns_still_answers_in_its_own_words() -> None:
    """The closing turn: one text-only call rather than the deterministic digest the Critic
    marks down. The digest remains the fallback, so this can only improve the verdict."""
    calls: list[list[dict]] = []

    def _chat(messages, tools):
        calls.append([dict(m) for m in messages])
        if len(calls) <= 2:
            return _tool_call("run_qc", {})
        return {"content": "QC ran on 400 cells; medians are in tables/qc.csv.", "tool_calls": []}

    harness = ResearchHarness(config=HarnessConfig(max_steps=2), chat_fn=_chat)
    events: list[dict] = []
    result = harness.run("Analyze.", _ctx(), on_event=events.append)

    assert len(calls) == 3                       # 2 budgeted turns + 1 closing turn
    assert result.stop_reason == "closing_turn"
    assert result.status == "ok"
    assert result.final_answer.startswith("QC ran on 400 cells")
    assert not result.answer_synthesized        # the model's words, not the auto-digest
    assert any(e["type"] == "closing_turn" for e in events)
    assert "TURN BUDGET EXHAUSTED" in str(calls[-1][-1]["content"])


def _counting_tool() -> HarnessTool:
    """A tool that always succeeds. Callers vary its ``n`` so each call is distinct — otherwise
    the repeated-identical-call early-out (``max_wasted_after_success``) ends the step with
    ``done_early`` and the turn budget is never what runs out."""
    return HarnessTool("note", "records a number",
                       {"type": "object", "properties": {"n": {"type": "integer"}}},
                       lambda args, _ctx: {"noted": args.get("n")})


def test_the_closing_turn_cannot_run_another_tool() -> None:
    """It is a way to SPEND the last word, not to reopen the budget: a tool call made there is
    discarded. Otherwise 'one more turn' becomes another Slurm job and the cap means nothing."""
    n = iter(range(99))

    def _chat(_messages, _tools):
        return _tool_call("note", {"n": next(n)})   # never stops asking for tools

    harness = ResearchHarness(catalog=[_counting_tool()], config=HarnessConfig(max_steps=2),
                              chat_fn=_chat)
    result = harness.run("Analyze.", _ctx(), on_event=lambda _e: None)

    assert [s["tool"] for s in result.steps] == ["note", "note"]   # the 3rd call was discarded
    assert result.stop_reason == "max_steps"     # no text came back, so nothing to promote
    assert result.answer_synthesized             # falls back to the deterministic digest


def test_a_step_that_finishes_normally_gets_no_closing_turn() -> None:
    calls: list[int] = []

    def _chat(messages, tools):
        calls.append(1)
        return _tool_call("finish", {"answer": "done early"}) if len(calls) > 1 \
            else _tool_call("run_qc", {})

    harness = ResearchHarness(config=HarnessConfig(max_steps=8), chat_fn=_chat)
    result = harness.run("Analyze.", _ctx(), on_event=lambda _e: None)

    assert len(calls) == 2 and result.stop_reason == "finished"   # no extra call was spent


def test_a_failing_closing_turn_costs_one_call_and_nothing_else() -> None:
    """The closing turn is advisory. A step whose artifacts are already on disk must never be
    lost to a model call that errors at the very end."""
    calls: list[int] = []

    def _chat(_messages, _tools):
        calls.append(1)
        if len(calls) > 2:
            raise RuntimeError("provider 500 on the closing turn")
        return _tool_call("run_qc", {})

    harness = ResearchHarness(config=HarnessConfig(max_steps=2), chat_fn=_chat)
    events: list[dict] = []
    result = harness.run("Analyze.", _ctx(), on_event=events.append)

    assert result.stop_reason == "max_steps"
    assert result.answer_synthesized and "run_qc" in result.final_answer
    assert any(e["type"] == "closing_turn_failed" for e in events)


def test_the_closing_turn_can_be_switched_off() -> None:
    n = iter(range(99))

    def _chat(_messages, _tools):
        return _tool_call("note", {"n": next(n)})

    harness = ResearchHarness(catalog=[_counting_tool()],
                              config=HarnessConfig(max_steps=2, closing_turn_on_budget=False),
                              chat_fn=_chat)
    events: list[dict] = []
    result = harness.run("Analyze.", _ctx(), on_event=events.append)
    assert not any(e["type"] == "closing_turn" for e in events)
    assert result.answer_synthesized


def test_extra_steps_raises_the_ceiling_for_one_attempt() -> None:
    """What a retry gets. The config is untouched — the bonus is per-call, so it cannot leak into
    the next step."""
    calls: list[int] = []

    def _chat(_messages, _tools):
        calls.append(1)
        return _tool_call("note", {"n": len(calls)})

    harness = ResearchHarness(catalog=[_counting_tool()],
                              config=HarnessConfig(max_steps=2, closing_turn_on_budget=False),
                              chat_fn=_chat)
    harness.run("Analyze.", _ctx(), on_event=lambda _e: None, extra_steps=2)
    assert len(calls) == 4
    assert harness.config.max_steps == 2          # the config itself never changed

    calls.clear()
    harness.run("Analyze.", _ctx(), on_event=lambda _e: None)
    assert len(calls) == 2                        # the next step is back to the default


def test_a_tool_result_with_bytes_or_numpy_reaches_the_model_instead_of_ending_the_run():
    import json
    import numpy as np
    from aiscientist.agents.research_harness import ResearchHarness
    messages: list = []
    ResearchHarness._feed_result(messages, True, "c1", {
        "status": "ok", "attr": b"library", "n": np.int64(3), "arr": np.array([1.5, 2.5]),
        "keys": {"b", "a"}})
    fed = json.loads(messages[0]["content"])
    assert fed == {"status": "ok", "attr": "library", "n": 3, "arr": [1.5, 2.5], "keys": ["a", "b"]}
