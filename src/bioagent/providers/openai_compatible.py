from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMResponse:
    model: str
    content: str
    provider: str = "openrouter"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    estimated_usage: bool = False
    endpoint_name: str | None = None
    fallback_attempts: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class LLMEndpointConfig:
    name: str
    base_url: str
    model: str
    api_key: str | None = None
    timeout_seconds: int = 45
    reasoning_effort: str | None = "none"


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 45,
        reasoning_effort: str | None = "none",
        endpoint_name: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("BIOAGENT_LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        self.model = model or os.environ.get("BIOAGENT_LLM_MODEL") or os.environ.get("OPENROUTER_MODEL", "qwen/qwen3.6-35b-a3b")
        self.base_url = (
            base_url
            or os.environ.get("BIOAGENT_LLM_BASE_URL")
            or os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.reasoning_effort = reasoning_effort
        self.endpoint_name = endpoint_name

    @property
    def available(self) -> bool:
        return bool(self.api_key) or self.provider_name != "openrouter"

    @property
    def provider_name(self) -> str:
        if "openrouter.ai" in self.base_url:
            return "openrouter"
        return self.endpoint_name or "local_openai_compatible"

    def chat(self, messages: list[dict[str, str]], max_tokens: int = 1200, temperature: float = 0.2) -> LLMResponse:
        if not self.available:
            raise LLMError("No remote API key or local OpenAI-compatible base URL is configured.")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if self.reasoning_effort == "none":
            payload["reasoning"] = {"effort": "none", "exclude": True}
        elif self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", "http://localhost/bioagent-prototype"),
            "X-Title": os.environ.get("OPENROUTER_APP_TITLE", "AiScientist"),
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"OpenRouter network error: {exc.reason}") from exc

        parsed = json.loads(body)
        choices = parsed.get("choices") or []
        if not choices:
            raise LLMError("OpenRouter response did not include choices.")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        finish_reason = choices[0].get("finish_reason")
        if not content:
            raise LLMError(f"OpenRouter response message was empty; finish_reason={finish_reason or 'unknown'}.")
        usage = parsed.get("usage")
        estimated_usage = False
        if isinstance(usage, dict):
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)
        else:
            estimated_usage = True
            prompt_chars = sum(len(message.get("content", "")) for message in messages)
            prompt_tokens = max(1, prompt_chars // 4)
            completion_tokens = max(1, len(content) // 4)
            total_tokens = prompt_tokens + completion_tokens
            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "estimated": True,
            }
        return LLMResponse(
            model=self.model,
            content=content,
            provider=self.provider_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            usage=usage,
            finish_reason=finish_reason,
            estimated_usage=estimated_usage,
            endpoint_name=self.endpoint_name,
        )


class FallbackLLMClient:
    """Try lab-server LLM first, then optional queued/remote fallbacks."""

    def __init__(self, clients: list[OpenRouterClient]) -> None:
        self.clients = clients

    @property
    def available(self) -> bool:
        return any(client.available for client in self.clients)

    @property
    def provider_name(self) -> str:
        return "fallback_llm"

    def chat(self, messages: list[dict[str, str]], max_tokens: int = 1200, temperature: float = 0.2) -> LLMResponse:
        attempts: list[dict[str, str]] = []
        last_error: LLMError | None = None
        for client in self.clients:
            if not client.available:
                attempts.append({"endpoint": client.provider_name, "status": "unavailable"})
                continue
            try:
                response = client.chat(messages, max_tokens=max_tokens, temperature=temperature)
            except LLMError as exc:
                attempts.append({"endpoint": client.provider_name, "status": "failed", "error": str(exc)[:300]})
                last_error = exc
                continue
            attempts.append({"endpoint": client.provider_name, "status": "used"})
            return LLMResponse(
                model=response.model,
                content=response.content,
                provider=response.provider,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                total_tokens=response.total_tokens,
                usage=response.usage,
                finish_reason=response.finish_reason,
                estimated_usage=response.estimated_usage,
                endpoint_name=response.endpoint_name,
                fallback_attempts=attempts,
            )
        if last_error:
            raise LLMError(f"All LLM fallback endpoints failed. Last error: {last_error}")
        raise LLMError("No LLM fallback endpoints are available.")


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def build_llm_fallback_client(
    reasoning_effort: str | None = "none",
    include_openrouter: bool | None = None,
    model: str | None = None,
) -> FallbackLLMClient:
    """Build production-oriented LLM fallback order.

    Default order:
    1. lab server OpenAI-compatible endpoint
    2. UCI/HPC OpenAI-compatible endpoint, if available
    3. OpenRouter only when explicitly allowed for prototype runs
    """

    clients: list[OpenRouterClient] = []
    lab_base_url = os.environ.get("BIOAGENT_LAB_LLM_BASE_URL") or os.environ.get("BIOAGENT_LLM_BASE_URL")
    lab_model = model or os.environ.get("BIOAGENT_LAB_LLM_MODEL") or os.environ.get("BIOAGENT_LLM_MODEL", "Qwen/Qwen3.6-35B-A3B")
    if lab_base_url:
        clients.append(
            OpenRouterClient(
                api_key=os.environ.get("BIOAGENT_LAB_LLM_API_KEY") or os.environ.get("BIOAGENT_LLM_API_KEY") or "local-dev-key",
                model=lab_model,
                base_url=lab_base_url,
                timeout_seconds=int(os.environ.get("BIOAGENT_LAB_LLM_TIMEOUT_SECONDS", "20")),
                reasoning_effort=reasoning_effort,
                endpoint_name="lab_server_llm",
            )
        )

    uci_base_url = os.environ.get("BIOAGENT_UCI_LLM_BASE_URL")
    if uci_base_url:
        clients.append(
            OpenRouterClient(
                api_key=os.environ.get("BIOAGENT_UCI_LLM_API_KEY") or "local-dev-key",
                model=os.environ.get("BIOAGENT_UCI_LLM_MODEL", lab_model),
                base_url=uci_base_url,
                timeout_seconds=int(os.environ.get("BIOAGENT_UCI_LLM_TIMEOUT_SECONDS", "10")),
                reasoning_effort=reasoning_effort,
                endpoint_name="uci_gpu_llm_fallback",
            )
        )

    allow_openrouter = include_openrouter if include_openrouter is not None else _truthy_env("BIOAGENT_ALLOW_REMOTE_LLM_FALLBACK")
    if allow_openrouter and (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("BIOAGENT_LLM_API_KEY")):
        clients.append(
            OpenRouterClient(
                api_key=os.environ.get("OPENROUTER_API_KEY") or os.environ.get("BIOAGENT_LLM_API_KEY"),
                model=os.environ.get("OPENROUTER_MODEL", "qwen/qwen3.6-35b-a3b"),
                base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
                timeout_seconds=int(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "45")),
                reasoning_effort=reasoning_effort,
                endpoint_name="openrouter_prototype_fallback",
            )
        )

    if not clients:
        clients.append(OpenRouterClient(reasoning_effort=reasoning_effort, endpoint_name="default_openrouter"))
    return FallbackLLMClient(clients)
