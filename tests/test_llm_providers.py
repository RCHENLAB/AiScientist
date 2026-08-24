"""Offline tests for the OpenAI-compatible provider layer: URL normalisation, live model
listing, and — the part that matters to users — telling the four failure causes apart.

"My key doesn't work" has four fixes and only one of them is "get a new key", so a verifier
that returns a generic error is barely better than no verifier. Every classification branch
is pinned here. No network: ``urllib.request.urlopen`` is replaced.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from bioagent.gateway import llm_providers as lp


class _Resp(io.BytesIO):
    """Minimal stand-in for the object urlopen returns as a context manager."""

    def __init__(self, body: str, status: int = 200) -> None:
        super().__init__(body.encode("utf-8"))
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install(monkeypatch, handler):
    """``handler(url, payload) -> _Resp | HTTPError | Exception``; anything returned that is an
    exception is raised, so a test can script a 401 as easily as a 200."""
    calls: list[tuple[str, dict | None]] = []

    def fake_urlopen(req, timeout=None):
        payload = json.loads(req.data.decode("utf-8")) if req.data else None
        calls.append((req.full_url, payload))
        out = handler(req.full_url, payload)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _http_error(code: int, body: str = "{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body.encode("utf-8")))


# --- presets -----------------------------------------------------------------


def test_presets_are_public_data_only():
    presets = lp.public_presets()
    keys = {p["key"] for p in presets}
    assert {"openrouter", "custom"} <= keys
    assert all(set(p) == {"key", "label", "base_url", "lists_models", "key_url", "note"} for p in presets)


def test_custom_preset_has_no_base_url():
    assert lp.get_provider("custom").base_url == ""
    assert lp.get_provider("nope") is None


@pytest.mark.parametrize("raw,expected", [
    ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1"),
    ("https://api.deepseek.com/v1/chat/completions", "https://api.deepseek.com/v1"),
    ("https://api.deepseek.com/v1/completions", "https://api.deepseek.com/v1"),
    ("  https://a.example/v1  ", "https://a.example/v1"),
    ("", ""),
])
def test_base_url_normalisation(raw, expected):
    """Users paste the full endpoint out of a provider's curl example; without this we would
    POST to …/chat/completions/chat/completions and report a confusing 404."""
    assert lp.normalize_base_url(raw) == expected


# --- model listing -----------------------------------------------------------


def test_list_models_reads_live_ids(monkeypatch):
    _install(monkeypatch, lambda url, _p: _Resp(json.dumps(
        {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]})))
    assert lp.list_models("https://api.deepseek.com/v1", "sk-x") == ["deepseek-chat", "deepseek-reasoner"]


@pytest.mark.parametrize("handler", [
    lambda url, _p: _http_error(404),
    lambda url, _p: _Resp("not json"),
    lambda url, _p: _Resp(json.dumps({"data": "wrong shape"})),
    lambda url, _p: urllib.error.URLError("dns"),
])
def test_list_models_degrades_to_empty(monkeypatch, handler):
    """A missing model list is a UI inconvenience, never a hard failure."""
    _install(monkeypatch, handler)
    assert lp.list_models("https://a/v1", "sk-x") == []


# --- verification ------------------------------------------------------------


def _verify_with(monkeypatch, chat_result, models=("m1",)):
    def handler(url, payload):
        if url.endswith("/models"):
            return _Resp(json.dumps({"data": [{"id": m} for m in models]}))
        return chat_result
    return _install(monkeypatch, handler)


def test_ok_when_the_model_actually_answers(monkeypatch):
    calls = _verify_with(monkeypatch, _Resp(json.dumps({"choices": [{"message": {"content": "ok"}}]})))
    res = lp.verify("https://a/v1", "sk-good", "m1")

    assert (res.ok, res.cause, res.verified_model) == (True, "ok", "m1")
    chat_url, payload = calls[-1]
    assert chat_url == "https://a/v1/chat/completions"
    assert payload["max_tokens"] == 1, "the ping must stay effectively free"
    assert payload["model"] == "m1"


@pytest.mark.parametrize("status,body,cause", [
    (401, "{}", "auth"),
    (403, "{}", "auth"),
    (402, "{}", "credit"),
    (400, '{"error":"insufficient balance"}', "credit"),
    (429, "{}", "credit"),
    (404, '{"error":"model xyz not found"}', "model"),
    (400, '{"error":"invalid model id"}', "model"),
    (404, "{}", "endpoint"),
    (500, "{}", "endpoint"),
])
def test_failure_causes_are_distinguished(monkeypatch, status, body, cause):
    _verify_with(monkeypatch, _http_error(status, body))
    res = lp.verify("https://a/v1", "sk-x", "m1")
    assert res.ok is False
    assert res.cause == cause, f"HTTP {status} {body} should read as {cause}"


def test_network_failure_is_its_own_cause(monkeypatch):
    _verify_with(monkeypatch, urllib.error.URLError("connection refused"))
    res = lp.verify("https://a/v1", "sk-x", "m1")
    assert (res.ok, res.cause) == (False, "network")
    assert "connection refused" in res.message


def test_error_text_never_echoes_the_key(monkeypatch):
    """Some gateways reflect the Authorization header in 4xx bodies — a real leak path into
    logs and the UI, not a theoretical one."""
    _verify_with(monkeypatch, _http_error(401, '{"error":"bad token sk-super-secret-value"}'))
    res = lp.verify("https://a/v1", "sk-super-secret-value", "m1")
    assert "sk-super-secret-value" not in res.message
    assert "<key>" in res.message


def test_missing_inputs_short_circuit_without_calling_out(monkeypatch):
    calls = _install(monkeypatch, lambda url, _p: pytest.fail("must not call the network"))
    assert lp.verify("", "sk-x", "m").cause == "endpoint"
    assert lp.verify("https://a/v1", "", "m").cause == "auth"
    assert calls == []


def test_without_a_model_a_populated_list_is_the_most_we_claim(monkeypatch):
    _verify_with(monkeypatch, _Resp("{}"), models=("a", "b"))
    res = lp.verify("https://a/v1", "sk-x", None)
    assert (res.ok, res.cause) == (True, "ok")
    assert res.verified_model is None, "nothing was pinged, so nothing is proven to answer"


def test_without_a_model_or_a_list_we_ask_for_a_model(monkeypatch):
    _verify_with(monkeypatch, _Resp("{}"), models=())
    res = lp.verify("https://a/v1", "sk-x", None)
    assert (res.ok, res.cause) == (False, "model")
