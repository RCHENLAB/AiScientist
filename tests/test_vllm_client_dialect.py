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
