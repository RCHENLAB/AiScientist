"""Offline tests for the vLLM /v1 client — no live server.

Monkeypatch ``urllib.request.urlopen`` with canned OpenAI-compatible responses to
prove the SSE stream parsing, the tool-call shape (must match ``ollama.chat_tools``
so the agentic harness is backend-agnostic), and model verification.
"""

from __future__ import annotations

import io
import json
from contextlib import contextmanager

import pytest

from bioagent.gateway import vllm_client
from bioagent.gateway.errors import GatewayError
from bioagent.gateway.settings import HPCSettings


class _FakeResp(io.BytesIO):
    """A urlopen() stand-in: context-managed, iterable by line, .read()able."""

    def __enter__(self):  # noqa: D105
        return self

    def __exit__(self, *a):  # noqa: D105
        return False


@contextmanager
def _patch_urlopen(monkeypatch, payload: bytes, capture: dict | None = None):
    def fake(req, timeout=None):  # noqa: ANN001
        if capture is not None and hasattr(req, "data") and req.data:
            capture["body"] = json.loads(req.data.decode("utf-8"))
            capture["url"] = req.full_url
            capture["auth"] = req.get_header("Authorization")
        return _FakeResp(payload)
    monkeypatch.setattr(vllm_client.urllib.request, "urlopen", fake)
    yield


def test_get_models_parses_openai_data(monkeypatch):
    body = json.dumps({"data": [{"id": "Qwen/Qwen3.6-35B-A3B-AWQ"}, {"id": "other"}]}).encode()
    with _patch_urlopen(monkeypatch, body):
        assert vllm_client.get_models(37219) == ["Qwen/Qwen3.6-35B-A3B-AWQ", "other"]


def test_ensure_model_matches_on_last_path_segment(monkeypatch):
    # served full repo id should satisfy a config that names just the leaf, and vice versa.
    body = json.dumps({"data": [{"id": "Qwen/Qwen3.6-35B-A3B-AWQ"}]}).encode()
    settings = HPCSettings(llm_backend="vllm", vllm_model="Qwen3.6-35B-A3B-AWQ")
    events: list[tuple] = []
    with _patch_urlopen(monkeypatch, body):
        vllm_client.ensure_model(37219, settings, lambda *a: events.append(a))
    assert events and events[-1][0] == "success"


def test_ensure_model_raises_when_not_served(monkeypatch):
    body = json.dumps({"data": [{"id": "some-other-model"}]}).encode()
    settings = HPCSettings(llm_backend="vllm", vllm_model="Qwen/Qwen3.6-35B-A3B-AWQ")
    with _patch_urlopen(monkeypatch, body), pytest.raises(GatewayError) as exc:
        vllm_client.ensure_model(37219, settings, lambda *a: None)
    assert "not serving" in str(exc.value)


def test_chat_stream_splits_thinking_and_content(monkeypatch):
    # vLLM SSE: reasoning_content (Qwen3 thinking) then content, ending with [DONE].
    sse = (
        'data: {"choices":[{"delta":{"reasoning_content":"let me think"}}]}\n'
        'data: {"choices":[{"delta":{"content":"the "}}]}\n'
        'data: {"choices":[{"delta":{"content":"answer"}}]}\n'
        "data: [DONE]\n"
    ).encode()
    with _patch_urlopen(monkeypatch, sse):
        out = list(vllm_client.chat_stream(37219, "m", [{"role": "user", "content": "q"}]))
    assert out == [("thinking", "let me think"), ("content", "the "), ("content", "answer")]


def test_chat_stream_sets_enable_thinking_flag(monkeypatch):
    cap: dict = {}
    with _patch_urlopen(monkeypatch, b"data: [DONE]\n", capture=cap):
        list(vllm_client.chat_stream(37219, "m", [], think=False))
    assert cap["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert cap["body"]["stream"] is True


def test_chat_tools_returns_ollama_compatible_shape(monkeypatch):
    # OpenAI tool_calls carry function.name + function.arguments (JSON string).
    # ResearchHarness._parse_call accepts that shape unchanged.
    body = json.dumps({
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "run_qc", "arguments": "{\"dataset\": \"a.h5ad\"}"},
                }],
            }
        }]
    }).encode()
    cap: dict = {}
    with _patch_urlopen(monkeypatch, body, capture=cap):
        msg = vllm_client.chat_tools(37219, "m", [{"role": "user", "content": "qc"}], tools=[{"x": 1}])
    assert msg["content"] == ""
    call = msg["tool_calls"][0]
    assert call["function"]["name"] == "run_qc"
    assert json.loads(call["function"]["arguments"]) == {"dataset": "a.h5ad"}
    # request carried the tools + auto tool choice
    assert cap["body"]["tool_choice"] == "auto" and cap["body"]["tools"] == [{"x": 1}]
    assert cap["url"].endswith("/v1/chat/completions")


def test_count_tokens_hits_tokenize_endpoint_at_root(monkeypatch):
    # vLLM /tokenize lives at the server ROOT (not /v1) and returns an exact count.
    cap: dict = {}
    with _patch_urlopen(monkeypatch, json.dumps({"count": 12345, "max_model_len": 32768}).encode(), capture=cap):
        n = vllm_client.count_tokens(37219, "m", [{"role": "user", "content": "hi"}], tools=[{"x": 1}])
    assert n == 12345
    assert cap["url"].endswith("/tokenize") and "/v1/" not in cap["url"]
    assert cap["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert cap["body"]["add_generation_prompt"] is True and cap["body"]["tools"] == [{"x": 1}]


def test_count_tokens_returns_none_for_remote_base_url(monkeypatch):
    # Remote OpenAI-compatible APIs (e.g. OpenRouter) have no /tokenize → None (caller
    # falls back to the char estimate). Must not even attempt a request.
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("count_tokens must not hit the network for a remote base_url")
    monkeypatch.setattr(vllm_client.urllib.request, "urlopen", boom)
    assert vllm_client.count_tokens(0, "m", [{"role": "user", "content": "hi"}],
                                    base_url="https://openrouter.ai/api/v1") is None


def test_count_tokens_swallows_transport_errors(monkeypatch):
    def boom(req, timeout=None):  # noqa: ANN001
        raise OSError("tunnel down")
    monkeypatch.setattr(vllm_client.urllib.request, "urlopen", boom)
    # Never raises — token counting must never break a run.
    assert vllm_client.count_tokens(37219, "m", [{"role": "user", "content": "hi"}]) is None


class _FakeExec:
    """Minimal RemoteExecutor stand-in: returns a canned result per command."""

    def __init__(self, stdout="", stderr="", code=0):
        self._out, self._err, self._code = stdout, stderr, code
        self.commands: list[str] = []

    def exec(self, command, timeout=60.0):  # noqa: ANN001
        self.commands.append(command)
        import types
        return types.SimpleNamespace(stdout=self._out, stderr=self._err, exit_status=self._code)


def test_ensure_installed_verifies_sif_present():
    ex = _FakeExec(stdout="__SIF_OK__\n")
    settings = HPCSettings(llm_backend="vllm", vllm_image="/dfs/vllm.sif")
    events: list[tuple] = []
    info = vllm_client.ensure_installed(ex, settings, lambda *a: events.append(a))
    assert info["installed"] is True and info["engine"] == "vllm"
    assert "test -f /dfs/vllm.sif" in ex.commands[0]
    assert events[-1][0] == "success"


def test_ensure_installed_raises_when_sif_missing():
    ex = _FakeExec(stdout="", stderr="no such file")
    settings = HPCSettings(llm_backend="vllm", vllm_image="/dfs/missing.sif")
    with pytest.raises(GatewayError) as exc:
        vllm_client.ensure_installed(ex, settings, lambda *a: None)
    assert "container image not found" in str(exc.value)


def test_get_tags_is_get_models_alias():
    assert vllm_client.get_tags is vllm_client.get_models


def test_base_url_and_api_key_override_route_to_openrouter(monkeypatch):
    # complete() and chat_tools() can target an arbitrary OpenAI /v1 (OpenRouter for
    # off-cluster testing) instead of the session tunnel.
    body = json.dumps({"choices": [{"message": {"content": "ok", "tool_calls": []}}]}).encode()
    cap: dict = {}
    with _patch_urlopen(monkeypatch, body, capture=cap):
        vllm_client.complete(0, "qwen/qwen3.6-35b-a3b", [{"role": "user", "content": "q"}],
                             base_url="https://openrouter.ai/api/v1", api_key="sk-or-test")
    assert cap["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert cap["auth"] == "Bearer sk-or-test"

    cap.clear()
    with _patch_urlopen(monkeypatch, body, capture=cap):
        vllm_client.chat_tools(0, "m", [], tools=[], base_url="https://openrouter.ai/api/v1", api_key="sk-or-test")
    assert cap["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert cap["auth"] == "Bearer sk-or-test"


def test_default_no_base_url_uses_session_tunnel(monkeypatch):
    body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
    cap: dict = {}
    with _patch_urlopen(monkeypatch, body, capture=cap):
        vllm_client.complete(37219, "m", [{"role": "user", "content": "q"}])
    assert cap["url"] == "http://127.0.0.1:37219/v1/chat/completions"
    assert cap["auth"] is None   # no Authorization header for the local tunnel


def test_complete_returns_assistant_content(monkeypatch):
    body = json.dumps({"choices": [{"message": {"content": "the answer"}}]}).encode()
    cap: dict = {}
    with _patch_urlopen(monkeypatch, body, capture=cap):
        out = vllm_client.complete(37219, "m", [{"role": "user", "content": "q"}], max_tokens=16)
    assert out == "the answer"
    assert cap["body"]["stream"] is False and cap["body"]["max_tokens"] == 16


def test_complete_default_leaves_thinking_untouched(monkeypatch):
    """The orchestrator (PI/Critic) calls complete() with think=True (default) and no max_tokens cap —
    its payload must stay byte-for-byte as before (no chat-template kwarg), so this fix cannot alter it."""
    body = json.dumps({"choices": [{"message": {"content": "x"}}]}).encode()
    cap: dict = {}
    with _patch_urlopen(monkeypatch, body, capture=cap):
        vllm_client.complete(37219, "m", [{"role": "user", "content": "q"}])
    assert "chat_template_kwargs" not in cap["body"]


def test_complete_think_false_disables_reasoning(monkeypatch):
    """think=False disables the Qwen3 thinking trace (the vLLM-native switch). This is LOAD-BEARING for
    map_phenotype_to_hpo: with thinking ON the reasoning trace exhausts max_tokens and returns EMPTY
    content (finish_reason=length) -> zero HPO terms. See mapper.make_hpo_mapping_tool."""
    body = json.dumps({"choices": [{"message": {"content": "x"}}]}).encode()
    cap: dict = {}
    with _patch_urlopen(monkeypatch, body, capture=cap):
        vllm_client.complete(37219, "m", [{"role": "user", "content": "q"}], max_tokens=1500, think=False)
    assert cap["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_tools_maps_fmt_to_response_format(monkeypatch):
    body = json.dumps({"choices": [{"message": {"content": "{}", "tool_calls": []}}]}).encode()
    cap: dict = {}
    schema = {"type": "object", "properties": {"tool": {"type": "string"}}}
    with _patch_urlopen(monkeypatch, body, capture=cap):
        vllm_client.chat_tools(37219, "m", [], tools=[], fmt=schema)
    assert cap["body"]["response_format"]["type"] == "json_schema"
    assert cap["body"]["response_format"]["json_schema"]["schema"] == schema
