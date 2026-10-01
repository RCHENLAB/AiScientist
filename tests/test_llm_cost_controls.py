"""Cost controls on the LLM paths: a remote output ceiling, local reasoning effort, usage on disk.

The defect these cover: ``complete()`` sent no ``max_tokens`` on the metered path (the lab computes
one, but the injected ``(messages) -> str`` contract had nowhere to put it) and discarded the
provider's ``usage`` block, so a per-token endpoint was both uncapped and unmeasurable. Everything
here is env-gated and defaults to the previous behaviour on the cluster's own GPU, where an output
budget is a context-fitting number rather than a spending limit.
"""
from __future__ import annotations

import json

import pytest

from bioagent.agents.research_lab import _call_with_role
from bioagent.gateway import vllm_client


def _capture(monkeypatch, usage: dict | None = None):
    """Stub urlopen; return the dict that will hold the payload actually sent."""
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

    body: dict = {"choices": [{"message": {"content": "ok"}}], "model": "served-model"}
    if usage is not None:
        body["usage"] = usage

    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        sent["payload"] = json.loads(req.data.decode())
        return _Resp(body)

    monkeypatch.setattr(vllm_client.urllib.request, "urlopen", fake_urlopen)
    return sent


# --- the remote output ceiling ------------------------------------------------

def test_lab_max_tokens_splits_writer_from_the_short_roles(monkeypatch):
    monkeypatch.delenv("BIOAGENT_LAB_MAX_TOKENS", raising=False)
    monkeypatch.delenv("BIOAGENT_LAB_MAX_TOKENS_WRITER", raising=False)
    # Planning/critique/scheduling emit a verdict or a JSON object; synthesis writes a manuscript.
    assert vllm_client.lab_max_tokens("reason") == 4096
    assert vllm_client.lab_max_tokens(None) == 4096
    assert vllm_client.lab_max_tokens("critic") == 4096      # anything unknown is a short role
    assert vllm_client.lab_max_tokens("writer") == 16384


def test_lab_max_tokens_is_env_tunable_and_floored(monkeypatch):
    monkeypatch.setenv("BIOAGENT_LAB_MAX_TOKENS", "1024")
    monkeypatch.setenv("BIOAGENT_LAB_MAX_TOKENS_WRITER", "32768")
    assert vllm_client.lab_max_tokens("reason") == 1024
    assert vllm_client.lab_max_tokens("writer") == 32768
    monkeypatch.setenv("BIOAGENT_LAB_MAX_TOKENS", "3")       # a floor, so a typo cannot mute a role
    assert vllm_client.lab_max_tokens("reason") == 256
    monkeypatch.setenv("BIOAGENT_LAB_MAX_TOKENS", "not-a-number")
    assert vllm_client.lab_max_tokens("reason") == 4096      # falls back, never raises


def test_remote_completion_carries_the_cap_it_was_given(monkeypatch):
    sent = _capture(monkeypatch)
    vllm_client.complete(0, "m", [{"role": "user", "content": "hi"}],
                         base_url="https://api.example/v1", max_tokens=4096)
    assert sent["payload"]["max_tokens"] == 4096


def test_no_cap_means_no_key_so_the_cluster_server_keeps_its_default(monkeypatch):
    sent = _capture(monkeypatch)
    vllm_client.complete(1234, "m", [{"role": "user", "content": "hi"}])
    assert "max_tokens" not in sent["payload"]


# --- local reasoning effort ---------------------------------------------------

def test_local_effort_sends_nothing_unless_configured(monkeypatch):
    monkeypatch.delenv("BIOAGENT_VLLM_REASONING_EFFORT", raising=False)
    sent = _capture(monkeypatch)
    vllm_client.complete(1234, "m", [{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in sent["payload"]


def test_local_effort_is_sent_when_configured(monkeypatch):
    monkeypatch.setenv("BIOAGENT_VLLM_REASONING_EFFORT", "low")
    sent = _capture(monkeypatch)
    vllm_client.complete(1234, "m", [{"role": "user", "content": "hi"}])
    assert sent["payload"]["reasoning_effort"] == "low"


def test_local_effort_is_absent_on_a_no_think_call(monkeypatch):
    # think=False is the bounded structured-output path; asking for effort there is incoherent.
    monkeypatch.setenv("BIOAGENT_VLLM_REASONING_EFFORT", "high")
    sent = _capture(monkeypatch)
    vllm_client.complete(1234, "m", [{"role": "user", "content": "hi"}], think=False)
    assert "reasoning_effort" not in sent["payload"]


def test_local_effort_never_leaks_to_a_remote_endpoint(monkeypatch):
    # A remote endpoint gets its strength from BIOAGENT_LAB_LLM_REASONING, not this vLLM-side knob.
    monkeypatch.setenv("BIOAGENT_VLLM_REASONING_EFFORT", "high")
    sent = _capture(monkeypatch)
    vllm_client.complete(0, "m", [{"role": "user", "content": "hi"}],
                         base_url="https://api.example/v1")
    assert "reasoning_effort" not in sent["payload"]


def test_a_garbage_effort_value_is_ignored(monkeypatch):
    monkeypatch.setenv("BIOAGENT_VLLM_REASONING_EFFORT", "very-hard-please")
    sent = _capture(monkeypatch)
    vllm_client.complete(1234, "m", [{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in sent["payload"]


# --- usage comes back ---------------------------------------------------------

def test_complete_ex_returns_the_providers_usage(monkeypatch):
    _capture(monkeypatch, usage={"prompt_tokens": 120, "completion_tokens": 30,
                                 "total_tokens": 150, "cost": 0.0042})
    text, usage = vllm_client.complete_ex(0, "m", [{"role": "user", "content": "hi"}],
                                          base_url="https://api.example/v1")
    assert text == "ok"
    assert usage["prompt_tokens"] == 120
    assert usage["completion_tokens"] == 30
    assert usage["cost"] == 0.0042


def test_usage_names_the_model_the_endpoint_actually_billed(monkeypatch):
    # The served name can differ from the one requested (an alias, or a provider's routing).
    _capture(monkeypatch, usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    _, usage = vllm_client.complete_ex(0, "requested-name", [{"role": "user", "content": "hi"}],
                                       base_url="https://api.example/v1")
    assert usage["model"] == "served-model"


def test_missing_usage_is_an_empty_dict_not_a_crash(monkeypatch):
    _capture(monkeypatch, usage=None)
    text, usage = vllm_client.complete_ex(1234, "m", [{"role": "user", "content": "hi"}])
    assert text == "ok"
    assert usage["model"] == "served-model"          # the only key a provider cannot omit for us
    assert "prompt_tokens" not in usage


def test_complete_keeps_its_text_only_contract(monkeypatch):
    _capture(monkeypatch, usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10})
    assert vllm_client.complete(1234, "m", [{"role": "user", "content": "hi"}]) == "ok"


# --- role plumbing ------------------------------------------------------------

def test_role_is_passed_only_to_a_fn_that_accepts_it():
    seen: dict = {}

    def production_fn(messages, *, role=None):
        seen["role"] = role
        return "x"

    assert _call_with_role(production_fn, [{"role": "user", "content": "q"}], "writer") == "x"
    assert seen["role"] == "writer"


def test_a_plain_lambda_double_is_still_valid():
    # Every test double, the lab kernel, skill induction and agent memory use (messages) -> str.
    assert _call_with_role(lambda msgs: f"got {len(msgs)}", [{"role": "user", "content": "q"}],
                           "writer") == "got 1"


def test_a_kwargs_fn_is_treated_as_accepting_role():
    def wrapper(messages, **kw):
        return kw.get("role", "none")

    assert _call_with_role(wrapper, [], "reason") == "reason"


def test_a_typeerror_from_inside_the_fn_is_not_swallowed():
    # Probing the signature (rather than catching TypeError around the call) is what keeps a real
    # bug inside the completion visible instead of silently retrying without the role.
    def broken(messages, *, role=None):
        raise TypeError("a real bug in here")

    with pytest.raises(TypeError, match="a real bug in here"):
        _call_with_role(broken, [], "reason")


# --- a reply cut off at the ceiling is broken, not short ------------------------------------
#
# The `reason` ceiling was measured against one model. claude-sonnet writes longer agendas and hit
# it six times in a day; a truncated agenda is unterminated JSON, _parse_plan returns None, and the
# PI falls back to an agenda whose only step is the user's own question. One of those reached a
# reviewer as a two-step "plan" — their question, then an appended literature step — with nothing
# anywhere saying a parse had failed.

def test_complete_ex_reports_why_generation_stopped():
    """Without finish_reason the caller cannot tell a short answer from a severed one."""
    import json as _json
    from bioagent.gateway import vllm_client

    body = {"choices": [{"message": {"content": "{\"agenda\": [\"a\", \"b"},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4096}}

    class _Resp:
        def read(self): return _json.dumps(body).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import urllib.request
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: _Resp()
    try:
        _text, usage = vllm_client.complete_ex(0, "m", [{"role": "user", "content": "x"}],
                                               base_url="https://example.invalid/v1", max_tokens=4096)
    finally:
        urllib.request.urlopen = orig
    assert usage["finish_reason"] == "length"
    assert usage["completion_tokens"] == usage.get("completion_tokens")


def test_a_complete_reply_reports_its_own_stop_reason_too():
    import json as _json
    from bioagent.gateway import vllm_client

    body = {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}}

    class _Resp:
        def read(self): return _json.dumps(body).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import urllib.request
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: _Resp()
    try:
        _text, usage = vllm_client.complete_ex(0, "m", [{"role": "user", "content": "x"}],
                                               base_url="https://example.invalid/v1", max_tokens=4096)
    finally:
        urllib.request.urlopen = orig
    assert usage["finish_reason"] == "stop"
