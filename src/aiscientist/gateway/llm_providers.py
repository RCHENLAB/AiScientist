"""OpenAI-compatible LLM provider presets + key verification.

This module is the NETWORK half of the bring-your-own-key feature; :mod:`llm_credentials`
is the DISK half. Keeping them apart means the store never opens a socket and the verifier
never touches the filesystem, so each is testable on its own.

Scope (locked with Yijun): **any OpenAI-compatible endpoint**, with presets as a
convenience rather than a whitelist. Every preset below speaks ``POST /v1/chat/completions``
with a ``Bearer`` token, which is exactly what :mod:`vllm_client` already sends — so the
whole transport layer is reused unchanged and no per-provider adapter exists. Anthropic is
deliberately NOT a preset: its native API is ``/v1/messages`` with ``x-api-key`` and a
different tool-call schema, so Claude models are reached through OpenRouter for now.

Model ids are deliberately NOT hard-coded. Providers rename and retire ids faster than we
redeploy, and a stale built-in list is worse than none — it looks authoritative and sends
users down a 404. ``list_models()`` reads the live ``GET /models`` instead, and the preset
only records whether that endpoint exists.

Verification exists because "my key doesn't work" has four completely different causes and
the user can only fix one of them if we say which: a bad key (401), an account with no
credit (402), a model id that doesn't exist on this endpoint (404), and the endpoint itself
being unreachable. :func:`verify` maps provider responses onto that distinction.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

_TIMEOUT = 15.0


@dataclass(frozen=True)
class Provider:
    """One preset: a base URL and how its endpoint behaves. ``key`` is the stable id we
    store on the credential row; ``custom`` is the escape hatch for anything not listed."""

    key: str
    label: str
    base_url: str
    # Whether ``GET /models`` is served. When False the UI cannot offer a live model list and
    # the user types the id; verification then relies on the chat ping alone.
    lists_models: bool = True
    # Where a user gets a key — shown as a link in the credential dialog, nothing more.
    key_url: str = ""
    # Free-text note rendered under the preset in the UI.
    note: str = ""


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        key="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        key_url="https://openrouter.ai/keys",
        note="One key reaches most vendors, including Anthropic and Google models.",
    ),
    Provider(
        key="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        key_url="https://platform.openai.com/api-keys",
    ),
    Provider(
        key="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        key_url="https://platform.deepseek.com/api_keys",
    ),
    Provider(
        key="dashscope",
        label="Qwen (DashScope)",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        key_url="https://bailian.console.aliyun.com/",
        note="Alibaba's OpenAI-compatible mode. Use the international host if your account is outside mainland China.",
    ),
    Provider(
        key="moonshot",
        label="Moonshot (Kimi)",
        base_url="https://api.moonshot.cn/v1",
        key_url="https://platform.moonshot.cn/console/api-keys",
    ),
    Provider(
        key="gemini",
        label="Google Gemini (OpenAI-compatible)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_url="https://aistudio.google.com/apikey",
        note="Google's OpenAI-compatibility layer, not the native Gemini API.",
    ),
    Provider(
        key="custom",
        label="Custom OpenAI-compatible endpoint",
        base_url="",
        key_url="",
        note="Anything that serves POST /v1/chat/completions with a Bearer token — another lab's vLLM, a proxy, a self-hosted gateway.",
    ),
)

_BY_KEY = {p.key: p for p in PROVIDERS}


def get_provider(key: str) -> Provider | None:
    return _BY_KEY.get((key or "").strip().lower())


def public_presets() -> list[dict[str, Any]]:
    """Preset list for the UI. Pure data — no secrets, no network."""
    return [
        {"key": p.key, "label": p.label, "base_url": p.base_url,
         "lists_models": p.lists_models, "key_url": p.key_url, "note": p.note}
        for p in PROVIDERS
    ]


def normalize_base_url(base_url: str) -> str:
    """Trim a base URL to the form the client expects: no trailing slash, no trailing
    ``/chat/completions`` (a very common paste — users copy the full endpoint out of the
    provider's curl example and we would otherwise POST to ``…/chat/completions/chat/completions``)."""
    url = (base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


# --- verification ------------------------------------------------------------


@dataclass(frozen=True)
class VerifyResult:
    """Why a key did or didn't work. ``cause`` is what the UI keys its message on."""

    ok: bool
    # 'ok' | 'auth' | 'credit' | 'model' | 'endpoint' | 'network'
    cause: str
    message: str
    models: list[str] = field(default_factory=list)
    # The model id actually proven to answer, when the chat ping ran.
    verified_model: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "cause": self.cause, "message": self.message,
                "models": self.models, "verified_model": self.verified_model}


def _scrub(text: str, api_key: str) -> str:
    """Never let a provider's error text carry the key back into a log, an event, or the UI.
    Some gateways echo the ``Authorization`` header verbatim in 4xx bodies, so this is a real
    leak path rather than a theoretical one."""
    if api_key and len(api_key) >= 8:
        text = text.replace(api_key, "<key>")
    return text


def _request(url: str, api_key: str, payload: dict | None, timeout: float) -> tuple[int, str]:
    """One HTTP call. Returns ``(status, body)``; HTTP errors come back as a status too,
    because a 401/402/404 body is the diagnostic we're after, not an exception."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {api_key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


@dataclass(frozen=True)
class ModelsResult:
    """The outcome of asking an endpoint what it serves — WHY it came back empty, not just that
    it did. ``list_models`` throws that away on purpose (a missing list is a UI inconvenience
    mid-verification); the model picker cannot, because "no models" and "wrong key" and "wrong
    URL" need three different things from the user and look identical as an empty list."""

    ok: bool
    # 'ok' | 'auth' | 'credit' | 'model' | 'endpoint' | 'network' | 'unsupported'
    cause: str
    message: str
    models: list[str] = field(default_factory=list)


def probe_models(base_url: str, api_key: str, timeout: float = _TIMEOUT) -> ModelsResult:
    """``GET /models`` with the reason attached. Used by the credential dialog so a user can see
    the model ids BEFORE saving anything — which is the only order that works, since a credential
    with no model id is refused at bind time and the ids live on the endpoint, not in our heads."""
    base = normalize_base_url(base_url)
    if not base:
        return ModelsResult(False, "endpoint", "Enter the base URL first.")
    if not api_key:
        return ModelsResult(False, "auth", "Listing models needs the API key.")
    try:
        status, body = _request(f"{base}/models", api_key, None, timeout)
    except urllib.error.URLError as exc:
        return ModelsResult(False, "network", f"Could not reach {base}: {exc.reason}")
    except (OSError, ValueError) as exc:
        return ModelsResult(False, "network", f"Could not reach {base}: {exc}")

    if status != 200:
        cause, message = _classify(status, body)
        detail = _scrub(body, api_key)[:200].strip()
        return ModelsResult(False, cause, f"{message} ({detail})" if detail else message)
    try:
        parsed = json.loads(body)
    except ValueError:
        return ModelsResult(False, "endpoint", f"{base}/models did not answer with JSON.")
    rows = parsed.get("data") if isinstance(parsed, dict) else None
    models = ([str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")]
              if isinstance(rows, list) else [])
    if not models:
        # The key authenticated (HTTP 200) — this endpoint simply doesn't publish a catalogue.
        return ModelsResult(False, "unsupported",
                            "This endpoint does not publish a model list — type the model id.")
    return ModelsResult(True, "ok", f"{len(models)} models available.", models)


def list_models(base_url: str, api_key: str, timeout: float = _TIMEOUT) -> list[str]:
    """Live model ids from ``GET /models``, or ``[]`` when the endpoint doesn't serve one.
    Best-effort by design: a missing model list is a UI inconvenience, not a failure."""
    return probe_models(base_url, api_key, timeout).models


def _classify(status: int, body: str) -> tuple[str, str]:
    """Map an HTTP status + body onto (cause, human message).

    Status alone is not enough: providers disagree about which code means "out of credit"
    (402 for OpenRouter, 429 with a specific body for others) and about whether an unknown
    model is a 400 or a 404. So the body text is consulted as well.
    """
    lowered = body.lower()
    if status in (401, 403):
        return "auth", "The endpoint rejected this API key (unauthorized)."
    if status == 402 or "insufficient" in lowered or "quota" in lowered or "credit" in lowered:
        return "credit", "The key is valid but the account has no credit or quota left."
    if status in (400, 404) and ("model" in lowered and ("not found" in lowered or "does not exist"
                                                         in lowered or "invalid" in lowered)):
        return "model", "This endpoint does not serve that model id."
    if status == 404:
        return "endpoint", "No OpenAI-compatible API at that base URL (404)."
    if status == 429:
        return "credit", "The endpoint is rate-limiting this key."
    return "endpoint", f"The endpoint answered HTTP {status}."


def verify(base_url: str, api_key: str, model: str | None = None,
           timeout: float = _TIMEOUT) -> VerifyResult:
    """Prove a (base_url, key, model) triple actually works, end to end.

    The chat ping — not the model list — is the real test: ``GET /models`` succeeds on keys
    that have no credit and on endpoints that will refuse the model we care about. So we ask
    for one token from the actual model and treat THAT as the verdict. It costs a fraction of
    a cent and it is the same call path the lab will use.

    This is what makes rotation safe: :func:`llm_credentials.rotate_key` runs this against the
    NEW key before overwriting anything, so a failed rotation leaves the stored credential
    exactly as it was.
    """
    base = normalize_base_url(base_url)
    if not base:
        return VerifyResult(False, "endpoint", "No base URL was given.")
    if not api_key:
        return VerifyResult(False, "auth", "No API key was given.")

    models = list_models(base, api_key, timeout)

    if not model:
        # Nothing to ping. A populated model list still proves the key authenticates, which is
        # the most we can honestly claim without knowing which model the user intends.
        if models:
            return VerifyResult(True, "ok", f"Key accepted; {len(models)} models available.", models)
        return VerifyResult(False, "model", "Choose a model id so the key can be tested.", models)

    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    try:
        status, body = _request(f"{base}/chat/completions", api_key, payload, timeout)
    except urllib.error.URLError as exc:
        return VerifyResult(False, "network", f"Could not reach {base}: {exc.reason}", models)
    except (OSError, ValueError) as exc:
        return VerifyResult(False, "network", f"Could not reach {base}: {exc}", models)

    if status == 200:
        return VerifyResult(True, "ok", f"{model} answered.", models, verified_model=model)

    cause, message = _classify(status, body)
    detail = _scrub(body, api_key)[:300].strip()
    if detail:
        message = f"{message} ({detail})"
    return VerifyResult(False, cause, message, models)
