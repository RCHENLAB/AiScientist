"""Served-model dialect knobs in vllm_client are env-gated and default to today's behaviour."""
from __future__ import annotations

import json

import pytest

from bioagent.gateway import vllm_client


def _capture(monkeypatch):
    sent: dict = {}

    class _Resp:
        def __init__(self, body: dict):
            self._b = json.dumps(body).encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        sent["payload"] = json.loads(req.data.decode())
        return _Resp({"choices": [{"message": {"content": "ok", "tool_calls": []}}]})

    monkeypatch.setattr(vllm_client.urllib.request, "urlopen", fake_urlopen)
    return sent


def test_complete_think_true_sends_nothing_by_default(monkeypatch):
    monkeypatch.delenv("BIOAGENT_VLLM_THINK_ON_KWARGS", raising=False)
    sent = _capture(monkeypatch)
    vllm_client.complete(1234, "m", [{"role": "user", "content": "hi"}])
    assert "chat_template_kwargs" not in sent["payload"]


def test_complete_think_false_still_opts_out(monkeypatch):
    monkeypatch.setenv("BIOAGENT_VLLM_THINK_ON_KWARGS", '{"enable_thinking": true}')
    sent = _capture(monkeypatch)
    vllm_client.complete(1234, "m", [{"role": "user", "content": "hi"}], think=False)
    assert sent["payload"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_complete_think_true_sends_opt_in_when_configured(monkeypatch):
    monkeypatch.setenv("BIOAGENT_VLLM_THINK_ON_KWARGS", '{"enable_thinking": true}')
    sent = _capture(monkeypatch)
    vllm_client.complete(1234, "m", [{"role": "user", "content": "hi"}])
    assert sent["payload"]["chat_template_kwargs"] == {"enable_thinking": True}


def test_complete_opt_in_never_reaches_a_remote_base_url(monkeypatch):
    monkeypatch.setenv("BIOAGENT_VLLM_THINK_ON_KWARGS", '{"enable_thinking": true}')
    sent = _capture(monkeypatch)
    vllm_client.complete(0, "m", [{"role": "user", "content": "hi"}], base_url="https://openrouter.ai/api/v1")
    assert "chat_template_kwargs" not in sent["payload"]


def test_chat_tools_defaults_unchanged(monkeypatch):
    for k in ("BIOAGENT_SCIENTIST_MAX_TOKENS", "BIOAGENT_SCIENTIST_CHAT_TEMPLATE_KWARGS"):
        monkeypatch.delenv(k, raising=False)
    sent = _capture(monkeypatch)
    vllm_client.chat_tools(1234, "m", [{"role": "user", "content": "hi"}], [])
    assert sent["payload"]["max_tokens"] == 2048
    assert "chat_template_kwargs" not in sent["payload"]


def test_chat_tools_env_budget_and_kwargs(monkeypatch):
    monkeypatch.setenv("BIOAGENT_SCIENTIST_MAX_TOKENS", "8192")
    monkeypatch.setenv("BIOAGENT_SCIENTIST_CHAT_TEMPLATE_KWARGS", '{"enable_thinking": false}')
    sent = _capture(monkeypatch)
    vllm_client.chat_tools(1234, "m", [{"role": "user", "content": "hi"}], [])
    assert sent["payload"]["max_tokens"] == 8192
    assert sent["payload"]["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("raw", ["", "not json", "[]", "{}"])
def test_bad_env_values_are_ignored(monkeypatch, raw):
    monkeypatch.setenv("BIOAGENT_VLLM_THINK_ON_KWARGS", raw)
    monkeypatch.setenv("BIOAGENT_SCIENTIST_CHAT_TEMPLATE_KWARGS", raw)
    assert vllm_client._think_on_kwargs() is None
    assert vllm_client._scientist_kwargs() is None


def test_remote_reasoning_effort_only_on_base_url(monkeypatch):
    monkeypatch.setenv("BIOAGENT_LAB_LLM_REASONING", '{"effort": "low"}')
    sent = _capture(monkeypatch)
    vllm_client.complete(0, "m", [{"role": "user", "content": "hi"}], base_url="https://openrouter.ai/api/v1")
    assert sent["payload"]["reasoning"] == {"effort": "low"}
    sent2 = _capture(monkeypatch)
    vllm_client.complete(1234, "m", [{"role": "user", "content": "hi"}])
    assert "reasoning" not in sent2["payload"]


# --- the Scientist's tool turns: reasoning effort + why generation stopped ------------------------


def test_chat_tools_sends_the_configured_effort(monkeypatch):
    monkeypatch.setenv("BIOAGENT_VLLM_REASONING_EFFORT", "medium")
    sent = _capture(monkeypatch)
    out = vllm_client.chat_tools(1234, "m", [{"role": "user", "content": "hi"}], [])
    assert sent["payload"]["reasoning_effort"] == "medium"
    assert out["finish_reason"] == ""                     # the fake reply names no reason


def test_chat_tools_sends_no_effort_unless_configured(monkeypatch):
    monkeypatch.delenv("BIOAGENT_VLLM_REASONING_EFFORT", raising=False)
    sent = _capture(monkeypatch)
    vllm_client.chat_tools(1234, "m", [{"role": "user", "content": "hi"}], [])
    assert "reasoning_effort" not in sent["payload"]


def test_chat_tools_explicit_effort_wins_and_never_goes_remote(monkeypatch):
    monkeypatch.setenv("BIOAGENT_VLLM_REASONING_EFFORT", "medium")
    sent = _capture(monkeypatch)
    vllm_client.chat_tools(1234, "m", [], [], reasoning_effort="low")
    assert sent["payload"]["reasoning_effort"] == "low"
    vllm_client.chat_tools(0, "m", [], [], base_url="https://openrouter.ai/api/v1", reasoning_effort="low")
    assert "reasoning_effort" not in sent["payload"]


def test_chat_tools_reports_a_truncated_turn(monkeypatch):
    class _Resp:
        def read(self):
            return json.dumps({"choices": [{"finish_reason": "length",
                                            "message": {"content": "", "tool_calls": []}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(vllm_client.urllib.request, "urlopen", lambda req, timeout=0: _Resp())
    out = vllm_client.chat_tools(1234, "m", [], [])
    assert out["finish_reason"] == "length" and out["tool_calls"] == []


def test_chat_tools_retries_without_an_effort_the_model_rejects(monkeypatch):
    import io
    import urllib.error
    sent: list[dict] = []

    class _Resp:
        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok", "tool_calls": []}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        body = json.loads(req.data.decode())
        sent.append(body)
        if "reasoning_effort" in body:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(
                b'{"error": {"message": "Unexpected reasoning effort high. Supported types are '
                b'xhigh (default), medium, and low."}}'))
        return _Resp()

    monkeypatch.setattr(vllm_client.urllib.request, "urlopen", fake_urlopen)
    out = vllm_client.chat_tools(1234, "m", [], [], reasoning_effort="high")
    assert out["content"] == "ok"
    assert sent[0]["reasoning_effort"] == "high" and "reasoning_effort" not in sent[1]
