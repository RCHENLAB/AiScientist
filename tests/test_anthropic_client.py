"""The native Anthropic transport (a user's own Claude key) — offline, against a fake SDK client.

What is pinned:
* the OpenAI-shaped history the harness keeps is translated into a VALID Messages API request
  (system on top, tool_use / tool_result paired, results first in their turn), including the
  histories the harness produces after compressing or dropping turns;
* Claude's raw turn (thinking blocks) rides on the assistant message and is replayed, with
  ``drop_block`` set so an edited history degrades instead of failing with a 400;
* responses map back to the ``vllm_client`` shapes (``tool_calls`` with JSON-string arguments,
  ``finish_reason`` "length" on a max_tokens stop, OpenAI usage keys);
* refusals, server-side fallback markers and rejected optional parameters are handled;
* credentials with ``provider="anthropic"`` are verified and routed through this module, for the
  lab roles, the Scientist's tool loop and the fast chat.
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

from aiscientist.gateway import anthropic_client as ac
from aiscientist.gateway import llm_providers
from aiscientist.gateway.errors import GatewayError


# --- a fake SDK client ---------------------------------------------------------------------------


class _Stream:
    def __init__(self, final, events=()):
        self.final, self.events = final, list(events)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return self.final


class _BadRequest(Exception):
    status_code = 400

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class _Auth(Exception):
    status_code = 401
    message = "invalid x-api-key"


class _Fake:
    """Records each request; answers with ``finals`` in order (an Exception instance is raised)."""

    def __init__(self, *finals, events=(), models=("claude-opus-5-5", "claude-sonnet-5-5")):
        self.finals, self.events, self.model_ids = list(finals), list(events), list(models)
        self.requests: list[tuple[str, dict]] = []
        fake = self

        class _Msgs:
            def __init__(self, ns):
                self.ns = ns

            def stream(self, **kw):
                fake.requests.append((self.ns, kw))
                nxt = fake.finals.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return _Stream(nxt, fake.events)

            def create(self, **kw):
                fake.requests.append((self.ns + ".create", kw))
                nxt = fake.finals.pop(0) if fake.finals else _final([{"type": "text", "text": "ok"}])
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt

        self.messages = _Msgs("std")
        self.beta = SimpleNamespace(messages=_Msgs("beta"))
        self.models = SimpleNamespace(list=lambda **kw: [SimpleNamespace(id=m) for m in fake.model_ids])


def _final(blocks, stop="end_turn", model="claude-opus-5-5", usage=None, details=None):
    u = usage or {"input_tokens": 100, "output_tokens": 20,
                  "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50}
    return SimpleNamespace(content=blocks, stop_reason=stop, model=model,
                           usage=SimpleNamespace(**u), stop_details=details)


@pytest.fixture()
def fake(monkeypatch):
    holder = {}

    def install(*finals, **kw):
        holder["c"] = _Fake(*finals, **kw)
        monkeypatch.setattr(ac, "_client", lambda api_key, base_url, timeout: holder["c"])
        return holder["c"]

    for v in ("AISCIENTIST_ANTHROPIC_FALLBACKS", "AISCIENTIST_ANTHROPIC_EFFORT",
              "AISCIENTIST_ANTHROPIC_EFFORT_SCIENTIST", "AISCIENTIST_ANTHROPIC_SCIENTIST_MAX_TOKENS"):
        monkeypatch.delenv(v, raising=False)
    return install


THINK = {"type": "thinking", "thinking": "", "signature": "sig-abc"}


def _tool_call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


# --- translation: OpenAI history -> Messages API ---------------------------------------------------


def test_system_goes_on_top_and_tool_results_pair_with_their_calls():
    system, msgs = ac.to_anthropic([
        {"role": "system", "content": "You are the Scientist."},
        {"role": "user", "content": "Brief"},
        {"role": "assistant", "content": "Looking.", "tool_calls": [
            _tool_call("t1", "inspect", {"p": 1}), _tool_call("t2", "qc", {})]},
        {"role": "tool", "tool_call_id": "t1", "content": '{"ok": true}'},
        {"role": "tool", "tool_call_id": "t2", "content": '{"ok": false}'},
        {"role": "user", "content": "3 turns left."},
    ])
    assert system == [{"type": "text", "text": "You are the Scientist."}]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assistant = msgs[1]["content"]
    assert assistant[0] == {"type": "text", "text": "Looking."}
    assert assistant[1] == {"type": "tool_use", "id": "t1", "name": "inspect", "input": {"p": 1}}
    # Both results in ONE user turn, before the nudge text.
    last = msgs[2]["content"]
    assert [b["type"] for b in last] == ["tool_result", "tool_result", "text"]
    assert [b["tool_use_id"] for b in last[:2]] == ["t1", "t2"]


def test_a_later_system_message_becomes_a_note_not_a_new_system_prompt():
    system, msgs = ac.to_anthropic([
        {"role": "system", "content": "S"}, {"role": "user", "content": "U"},
        {"role": "system", "content": "Budget low"},
    ])
    assert system == [{"type": "text", "text": "S"}]
    assert msgs[-1]["content"][-1]["text"] == "[System note] Budget low"


def test_histories_trimmed_by_the_harness_still_pair_up():
    # The harness dropped the assistant turn that issued t0 but kept its result, and stopped on
    # `finish` before t9's result was fed back.
    _, msgs = ac.to_anthropic([
        {"role": "user", "content": "Brief"},
        {"role": "tool", "tool_call_id": "t0", "content": "old result"},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("t9", "finish", {"answer": "x"})]},
    ])
    assert msgs[0]["content"][-1] == {"type": "text", "text": "Tool result: old result"}
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"][0]["type"] == "tool_result"
    assert msgs[-1]["content"][0]["tool_use_id"] == "t9"


def test_compressed_arguments_that_are_no_longer_json_become_an_object():
    _, msgs = ac.to_anthropic([
        {"role": "user", "content": "Brief"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "run_code", "arguments": '{"code": "imp…'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "{}"},
    ])
    assert msgs[1]["content"][0]["input"] == {"_truncated_arguments": '{"code": "imp…'}


def test_an_unchanged_turn_is_replayed_verbatim_with_its_thinking():
    calls = [_tool_call("t1", "inspect", {"p": 1})]
    raw = [THINK, {"type": "tool_use", "id": "t1", "name": "inspect", "input": {"p": 1}}]
    turn = {"role": "assistant", "content": "", "tool_calls": calls,
            "provider_blocks": {"provider": "anthropic", "fp": ac.fingerprint("", calls), "blocks": raw}}
    _, msgs = ac.to_anthropic([{"role": "user", "content": "B"}, turn,
                               {"role": "tool", "tool_call_id": "t1", "content": "r"}])
    assert msgs[1]["content"] == raw


def test_a_compressed_turn_keeps_its_thinking_ahead_of_the_shortened_content():
    calls = [_tool_call("t1", "run_code", {"code": "x" * 50})]
    raw = [THINK, {"type": "text", "text": "long prose " * 20},
           {"type": "tool_use", "id": "t1", "name": "run_code", "input": {"code": "x" * 50}}]
    turn = {"role": "assistant", "content": "long prose…", "tool_calls": calls,
            "provider_blocks": {"provider": "anthropic", "fp": "stale", "blocks": raw}}
    _, msgs = ac.to_anthropic([{"role": "user", "content": "B"}, turn,
                               {"role": "tool", "tool_call_id": "t1", "content": "r"}])
    blocks = msgs[1]["content"]
    assert blocks[0] == THINK
    assert blocks[1] == {"type": "text", "text": "long prose…"}
    assert blocks[2]["type"] == "tool_use"


def test_a_turn_of_bare_thinking_is_left_out():
    turn = {"role": "assistant", "content": "", "provider_blocks": {
        "provider": "anthropic", "fp": ac.fingerprint("", []), "blocks": [THINK]}}
    _, msgs = ac.to_anthropic([{"role": "user", "content": "B"}, turn,
                               {"role": "user", "content": "Use a tool."}])
    assert [m["role"] for m in msgs] == ["user"]


def test_a_history_that_starts_with_the_assistant_gets_a_user_turn_first():
    _, msgs = ac.to_anthropic([{"role": "assistant", "content": "hi"}, {"role": "user", "content": "q"}])
    assert msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant"


def test_openai_function_tools_become_anthropic_tools():
    out = ac.tools_to_anthropic([{"type": "function", "function": {
        "name": "inspect", "description": "Look", "parameters": {"properties": {"p": {"type": "integer"}}}}}])
    assert out == [{"name": "inspect", "description": "Look",
                    "input_schema": {"properties": {"p": {"type": "integer"}}, "type": "object"}}]


# --- requests and responses ---------------------------------------------------------------------


def test_a_scientist_turn_maps_tool_use_back_and_carries_its_raw_blocks(fake):
    client = fake(_final([THINK, {"type": "text", "text": "Checking QC."},
                          {"type": "tool_use", "id": "toolu_1", "name": "qc", "input": {"mito": 20}}],
                         stop="tool_use"))
    out = ac.chat_tools("claude-opus-5-5", [{"role": "system", "content": "S"},
                                            {"role": "user", "content": "B"}],
                        [{"type": "function", "function": {"name": "qc", "parameters": {"type": "object"}}}],
                        api_key="sk-ant-test", effort="medium")
    assert out["content"] == "Checking QC."
    assert out["tool_calls"] == [{"id": "toolu_1", "type": "function",
                                  "function": {"name": "qc", "arguments": '{"mito": 20}'}}]
    assert out["finish_reason"] == "tool_calls"
    assert out["provider_blocks"]["blocks"][0] == THINK
    assert out["provider_blocks"]["fp"] == ac.fingerprint(out["content"], out["tool_calls"])
    assert out["usage"]["prompt_tokens"] == 1050 and out["usage"]["completion_tokens"] == 20

    ns, kw = client.requests[0]
    assert ns == "beta"
    assert kw["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert set(kw["betas"]) == {"thinking-binding-controls-2026-08-01", "server-side-fallback-2026-07-01"}
    assert kw["fallbacks"] == "default"
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["cache_control"] == {"type": "ephemeral"}
    assert kw["system"] == [{"type": "text", "text": "S"}]
    assert kw["tools"][0]["name"] == "qc" and kw["max_tokens"] == 32000


def test_the_echoed_turn_round_trips_unchanged_on_the_next_request(fake):
    client = fake(_final([THINK, {"type": "tool_use", "id": "toolu_1", "name": "qc", "input": {}}],
                         stop="tool_use"),
                  _final([{"type": "text", "text": "Done."}]))
    history = [{"role": "user", "content": "B"}]
    out = ac.chat_tools("claude-opus-5-5", history, [], api_key="k")
    history += [{"role": "assistant", "content": out["content"], "tool_calls": out["tool_calls"],
                 "provider_blocks": out["provider_blocks"]},
                {"role": "tool", "tool_call_id": "toolu_1", "content": "{}"}]
    ac.chat_tools("claude-opus-5-5", history, [], api_key="k")
    sent = client.requests[1][1]["messages"]
    assert sent[1]["content"] == [THINK, {"type": "tool_use", "id": "toolu_1", "name": "qc", "input": {}}]


def test_a_lab_completion_returns_text_and_openai_usage(fake):
    fake(_final([THINK, {"type": "text", "text": "Plan: …"}], stop="max_tokens"))
    text, usage = ac.complete_ex("claude-opus-5-5", [{"role": "user", "content": "plan"}],
                                 api_key="k", max_tokens=16000, effort="high")
    assert text == "Plan: …"
    assert usage["finish_reason"] == "length"           # the gateway's truncation retry keys on it
    assert usage["prompt_tokens"] == 1050


def test_one_off_calls_that_share_their_rules_cache_only_the_rules(fake):
    """The corpus scan: 1,500 different passages under one system prompt. Caching the whole
    prompt wrote each one to cache (1.25x) and read none back; only the rules are shared."""
    client = fake(_final([{"type": "text", "text": "{}"}]), _final([{"type": "text", "text": "{}"}]))
    msgs = [{"role": "system", "content": "RULES"}, {"role": "user", "content": "PASSAGE 1"}]
    ac.complete_ex("claude-haiku-5-5", msgs, api_key="k", max_tokens=8000, cache="system")
    kw = client.requests[0][1]
    assert "cache_control" not in kw
    assert kw["system"] == [{"type": "text", "text": "RULES", "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"][0]["content"][0] == {"type": "text", "text": "PASSAGE 1"}
    # Every other call keeps whole-prompt auto-caching.
    ac.complete_ex("claude-haiku-5-5", msgs, api_key="k", max_tokens=8000)
    kw = client.requests[1][1]
    assert kw["cache_control"] == {"type": "ephemeral"} and kw["system"] == [{"type": "text", "text": "RULES"}]


def test_a_lab_completion_does_not_send_thinking(fake):
    client = fake(_final([{"type": "text", "text": "ok"}]))
    ac.complete_ex("claude-opus-5-5", [{"role": "user", "content": "q"}], api_key="k", max_tokens=1000)
    assert "thinking" not in client.requests[0][1]


def test_an_older_model_gets_no_effort_thinking_or_fallbacks(fake):
    client = fake(_final([{"type": "text", "text": "ok"}], model="claude-haiku-4-5"))
    ac.chat_tools("claude-haiku-4-5", [{"role": "user", "content": "q"}], [], api_key="k", effort="high")
    ns, kw = client.requests[0]
    assert ns == "std"
    assert not {"thinking", "output_config", "fallbacks", "betas"} & set(kw)


def test_fallbacks_can_be_switched_off(fake, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_ANTHROPIC_FALLBACKS", "0")
    client = fake(_final([{"type": "text", "text": "ok"}]))
    ac.complete_ex("claude-opus-5-5", [{"role": "user", "content": "q"}], api_key="k", max_tokens=100)
    assert client.requests[0][0] == "std" and "fallbacks" not in client.requests[0][1]


def test_a_rejected_optional_parameter_is_dropped_and_the_call_retried(fake):
    client = fake(_BadRequest("output_config.effort: Extra inputs are not permitted"),
                  _final([{"type": "text", "text": "ok"}]))
    text, _ = ac.complete_ex("claude-opus-5-5", [{"role": "user", "content": "q"}], api_key="k",
                             max_tokens=100, effort="high")
    assert text == "ok"
    assert "output_config" in client.requests[0][1] and "output_config" not in client.requests[1][1]


def test_a_400_about_something_else_is_not_retried(fake):
    client = fake(_BadRequest("messages.1: tool_use ids must be unique"))
    with pytest.raises(GatewayError) as exc:
        ac.complete_ex("claude-opus-5-5", [{"role": "user", "content": "q"}], api_key="k", max_tokens=100)
    assert len(client.requests) == 1 and exc.value.stage == "anthropic_endpoint"


def test_a_refusal_raises_with_its_category(fake):
    fake(_final([], stop="refusal", details=SimpleNamespace(category="bio", explanation="Declined.")))
    with pytest.raises(GatewayError) as exc:
        ac.complete_ex("claude-opus-5-5", [{"role": "user", "content": "q"}], api_key="k", max_tokens=100)
    assert exc.value.stage == "anthropic_refusal" and "bio" in exc.value.message


def test_after_a_mid_output_fallback_the_declined_partial_is_not_acted_on(fake):
    fake(_final([THINK, {"type": "text", "text": "Partial. "},
                 {"type": "tool_use", "id": "old", "name": "qc", "input": {}},
                 {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"}},
                 {"type": "text", "text": "Continued."},
                 {"type": "tool_use", "id": "new", "name": "qc", "input": {}}], stop="tool_use"))
    out = ac.chat_tools("claude-opus-5-5", [{"role": "user", "content": "q"}], [], api_key="k")
    assert [c["id"] for c in out["tool_calls"]] == ["new"]
    assert out["content"] == "Partial. \nContinued."
    kinds = [b["type"] for b in out["provider_blocks"]["blocks"]]
    assert kinds == ["text", "text", "tool_use"]


def test_a_too_long_prompt_reads_as_a_context_overflow_to_the_harness(fake):
    from aiscientist.agents.research_harness import _is_context_overflow
    fake(_BadRequest("prompt is too long: 1200000 tokens > 1000000 maximum"))
    with pytest.raises(GatewayError) as exc:
        ac.chat_tools("claude-opus-5-5", [{"role": "user", "content": "q"}], [], api_key="k")
    assert _is_context_overflow(exc.value)


def test_the_fast_chat_stream_yields_text_then_calls_then_blocks(fake):
    def ev(kind, **kw):
        return SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type=kind, **kw))
    fake(_final([{"type": "text", "text": "Hello world"}]),
         events=[ev("text_delta", text="Hello "), ev("text_delta", text="world"),
                 SimpleNamespace(type="message_stop")])
    got = list(ac.chat_tools_stream("claude-opus-5-5", [{"role": "user", "content": "hi"}], [], api_key="k"))
    assert got[:2] == [("content", "Hello "), ("content", "world")]
    assert got[2] == ("tool_calls", [])
    assert got[3][0] == "provider_blocks"
    # The streamed text (concatenated deltas) still matches the stored blocks' fingerprint.
    assert got[3][1]["fp"] == ac.fingerprint("Hello world", [])


# --- verification and provider dispatch ----------------------------------------------------------


def test_verify_lists_models_then_pings_the_model(fake):
    client = fake()
    ok, cause, message, models, verified = ac.verify("sk-ant-x", "claude-opus-5-5")
    assert ok and verified == "claude-opus-5-5" and "claude-sonnet-5-5" in models
    ns, kw = client.requests[0]
    assert ns == "std.create" and "thinking" not in kw and kw["output_config"] == {"effort": "low"}


def test_verify_names_a_bad_key(fake, monkeypatch):
    c = fake()
    c.models = SimpleNamespace(list=lambda **kw: (_ for _ in ()).throw(_Auth()))
    ok, cause, *_ = ac.verify("sk-bad", "claude-opus-5-5")
    assert not ok and cause == "auth"


def test_the_anthropic_preset_and_host_select_the_native_transport():
    assert llm_providers.transport_for("anthropic") == "anthropic"
    assert llm_providers.transport_for(None, "https://api.anthropic.com") == "anthropic"
    assert llm_providers.transport_for("custom", "https://api.anthropic.com/v1/") == "anthropic"
    assert llm_providers.transport_for("openrouter", "https://openrouter.ai/api/v1") == "openai"
    preset = next(p for p in llm_providers.public_presets() if p["key"] == "anthropic")
    assert preset["transport"] == "anthropic" and preset["base_url"] == "https://api.anthropic.com"


def test_provider_verify_and_probe_dispatch_to_the_native_api(monkeypatch):
    monkeypatch.setattr(ac, "verify", lambda key, model, base, timeout=0: (True, "ok", "answered", ["m"], model))
    monkeypatch.setattr(ac, "probe_models", lambda key, base, timeout=0: (True, "ok", "1 model", ["m"]))
    r = llm_providers.verify("https://api.anthropic.com", "k", "claude-opus-5-5")
    assert r.ok and r.verified_model == "claude-opus-5-5"
    assert llm_providers.probe_models("", "k", provider="anthropic").models == ["m"]


def test_base_urls_are_reduced_to_the_api_root():
    assert ac.normalize_base_url("") == "https://api.anthropic.com"
    assert ac.normalize_base_url("https://api.anthropic.com/v1/") == "https://api.anthropic.com"
    assert ac.normalize_base_url("https://proxy.example/v1/messages") == "https://proxy.example"


# --- gateway wiring --------------------------------------------------------------------------------


fastapi = pytest.importorskip("fastapi")


class _Conn:
    selected_model = "QuantTrio/Qwen3.6-35B-A3B-AWQ"
    tunnel_port = 37219
    mock = False

    def __init__(self, credential_id):
        self.owner = "alice"
        self.llm_choice = {"credential_id": credential_id}

    def emit(self, *a, **kw):
        pass


@pytest.fixture()
def anthropic_cred(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AISCIENTIST_LLM_KEY_ENCRYPTION", raising=False)
    for v in ("AISCIENTIST_LLM_BASE_URL", "AISCIENTIST_LAB_LLM_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    from aiscientist.gateway import llm_credentials
    importlib.reload(llm_credentials)
    return llm_credentials.create("alice", provider="anthropic", base_url="https://api.anthropic.com",
                                  model="claude-sonnet-5-5", lab_model="claude-opus-5-5",
                                  api_key="sk-ant-own-example", label="Lab Claude")


def test_an_anthropic_credential_runs_the_lab_roles_and_the_scientist_natively(anthropic_cred, monkeypatch):
    from aiscientist.gateway import app as gw_app
    from aiscientist.gateway import vllm_client
    seen = []
    monkeypatch.setattr(ac, "complete_ex", lambda model, messages, **kw: seen.append(
        ("lab", model, kw)) or ("plan", {"finish_reason": "stop"}))
    monkeypatch.setattr(ac, "chat_tools", lambda model, messages, tools, **kw: seen.append(
        ("sci", model, kw)) or {"content": "", "tool_calls": [], "usage": {}})
    monkeypatch.setattr(vllm_client, "complete_ex", lambda *a, **k: pytest.fail("used the OpenAI path"))
    monkeypatch.setattr(vllm_client, "chat_tools", lambda *a, **k: pytest.fail("used the OpenAI path"))

    llm = gw_app._lab_llm(_Conn(anthropic_cred["id"]))
    assert llm.complete_fn([{"role": "user", "content": "plan"}], role="plan") == "plan"
    llm.scientist_chat([{"role": "user", "content": "run"}], [])
    assert llm.count_tokens([], []) is None

    lab, sci = seen
    assert lab[1] == "claude-opus-5-5" and lab[2]["api_key"] == "sk-ant-own-example"
    assert lab[2]["effort"] == "high" and lab[2]["max_tokens"] >= 16000
    assert sci[1] == "claude-sonnet-5-5" and sci[2]["effort"] == "medium"
    assert llm.scientist_remote is True and llm.lab_role_remote is True


def test_the_harness_echoes_provider_blocks_on_the_next_request():
    from aiscientist.agents.research_harness import HarnessContext, ResearchHarness
    seen = []
    blocks = {"provider": "anthropic", "fp": "x", "blocks": [THINK]}

    def chat(messages, tools):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:      # a call to a tool that does not exist: the error is fed back
            return {"content": "", "tool_calls": [_tool_call("t1", "no_such_tool", {})],
                    "provider_blocks": blocks}
        return {"content": "", "tool_calls": [_tool_call("t2", "finish", {"answer": "ok"})]}

    ResearchHarness(catalog=[], chat_fn=chat).run("brief", HarnessContext())
    assert len(seen) == 2
    assistant = [m for m in seen[1] if m.get("role") == "assistant"]
    assert assistant and assistant[0]["provider_blocks"] == blocks


def test_an_openai_compatible_server_never_sees_provider_blocks():
    from aiscientist.gateway import vllm_client
    blocks = {"provider": "anthropic", "fp": "x", "blocks": [THINK]}
    wired = vllm_client._wire_messages([{"role": "user", "content": "q"},
                                        {"role": "assistant", "content": "", "provider_blocks": blocks}])
    assert wired == [{"role": "user", "content": "q"}, {"role": "assistant", "content": ""}]


def test_the_fast_chat_stream_ends_with_its_usage(fake):
    fake(_final([{"type": "text", "text": "Hi"}]),
         events=[SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="Hi"))])
    got = list(ac.chat_tools_stream("claude-haiku-5-5", [{"role": "user", "content": "hi"}], [], api_key="k"))
    kind, usage = got[-1]
    assert kind == "usage"
    assert usage["completion_tokens"] == 20 and usage["prompt_tokens"] == 100 + 900 + 50


def test_a_rate_limit_is_not_read_as_an_empty_account():
    """Both used to come back as "credit"; they have different fixes (wait vs top up)."""
    RateLimitError = type("RateLimitError", (Exception,), {"status_code": 429})
    BadRequestError = type("BadRequestError", (Exception,), {"status_code": 400})
    assert ac.classify_error(RateLimitError("slow down"))[0] == "rate"
    assert ac.classify_error(BadRequestError("Your credit balance is too low"))[0] == "credit"
