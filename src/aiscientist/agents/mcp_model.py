"""Trusted per-call model bindings for standalone MCP workers.

Only the gateway attaches ``executor.mcp_model_binding``. Planner arguments and
run decisions never choose endpoints or credentials. User-owned API sessions must
remain on their guarded gateway transport until a metered MCP relay exists.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

_MODEL_TOOLS = {
    "literature": ("LITERATURE", {"deep_literature", "extract_haploinsufficient_genes"}),
    "phenotype-diagnosis": ("PHENOTYPE", {"map_phenotype_to_hpo"}),
    "data-utilities": ("DATA_UTILITIES", {"inspect_dataset"}),
}


@dataclass(frozen=True)
class ModelBinding:
    endpoint: str
    model: str
    api_key: str = field(default="", repr=False)
    protocol: str = "openai"

    def __post_init__(self):
        if self.protocol != "openai":
            raise ValueError("This MCP worker needs an OpenAI-compatible model; use the existing gateway tool for native Anthropic sessions")
        if any(not isinstance(value, str) or any(c in value for c in ("\0", "\n", "\r"))
               for value in (self.endpoint, self.model, self.api_key)):
            raise ValueError("invalid trusted MCP model configuration")
        parsed = urlsplit(self.endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("MCP model endpoint must be an HTTP(S) base URL without credentials or query parameters")
        if parsed.hostname.lower() == "api.anthropic.com":
            raise ValueError("Native Anthropic needs the existing guarded gateway transport; it is not an OpenAI-compatible MCP endpoint")
        if not self.model.strip():
            raise ValueError("trusted MCP model name is empty")


@dataclass(frozen=True)
class CallModel:
    prefix: str
    binding: ModelBinding

    @property
    def secret_name(self) -> str:
        return self.prefix + "_MODEL_API_KEY"


def _operator_binding(prefix: str, values: dict[str, str]) -> ModelBinding | None:
    endpoint, model = values.get(prefix + "_MODEL_ENDPOINT", ""), values.get(prefix + "_MODEL", "")
    if not endpoint and not model:
        return None
    if not endpoint or not model:
        raise ValueError("configure both the approved MCP model endpoint and model name")
    key_file = os.environ.get(prefix + "_MODEL_API_KEY_FILE", "")
    if key_file:
        try:
            key = Path(key_file).read_text().strip()
        except OSError:
            raise ValueError("configured MCP model credential file cannot be read") from None
    else:
        key = os.environ.get(prefix + "_MODEL_API_KEY", os.environ.get("AISCIENTIST_SKILL_SECRET_" + prefix + "_MODEL_API_KEY", ""))
    return ModelBinding(endpoint.rstrip("/"), model, key, os.environ.get(prefix + "_MODEL_PROTOCOL", "openai"))


def prepare_model(server: str, tool: str, executor: Any, values: dict[str, str]) -> tuple[CallModel | None, str]:
    """Resolve once per tool call; return only public shell setup and private metadata."""
    selected = _MODEL_TOOLS.get(server)
    if server == "phenotype-diagnosis" and tool == "diagnose_disease":
        prefix = "LITERATURE"
    elif selected is None or tool not in selected[1]:
        return None, ""
    else:
        prefix = selected[0]
    resolver = getattr(executor, "mcp_model_binding", None)
    if resolver is not None:
        if not callable(resolver):
            raise ValueError("MCP model binding must be a trusted callback")
        binding = resolver()  # None is explicitly no session model; never switch to a global one.
        if binding is not None and not isinstance(binding, ModelBinding):
            raise ValueError("trusted MCP model callback returned an invalid binding")
    else:
        binding = _operator_binding(prefix, values)
    values.pop(prefix + "_MODEL_ENDPOINT", None)
    values.pop(prefix + "_MODEL", None)
    # The gateway credential-file path is not meaningful inside an HPC container.
    # Credentials instead arrive through POLICY and its 0600 per-job secret file.
    setup = f"unset {prefix}_MODEL_API_KEY_FILE\n"
    if binding is None:
        return None, setup + f"unset {prefix}_MODEL_ENDPOINT {prefix}_MODEL {prefix}_MODEL_API_KEY\n"
    values.update({prefix + "_MODEL_ENDPOINT": binding.endpoint, prefix + "_MODEL": binding.model})
    if not binding.api_key:
        setup += f"unset {prefix}_MODEL_API_KEY\n"
    return CallModel(prefix, binding), setup


def run_with_model(model: CallModel | None, executor: Any, call: Callable[[], Any]) -> Any:
    """Extend the existing immutable skill policy for exactly one pinned MCP job.

    SlurmSandbox owns the protected credential file and its finally cleanup. The
    extra redaction covers the dynamically bound key beyond declared skill keys.
    """
    if model is None:
        return call()
    if not isinstance(model, CallModel):
        raise ValueError("invalid private MCP model binding")
    from .skills_registry import POLICY
    policy = POLICY.get()
    key = model.binding.api_key
    if key and (policy is None or not getattr(executor, "supports_skill_policy", False)):
        raise ValueError("this executor cannot protect a per-job MCP model credential")
    if policy is not None and policy.get("network", "none") == "none":
        raise ValueError("the pinned skill does not declare network access for a model")
    token = None
    try:
        if policy is not None:
            secrets = dict(policy.get("secret_values", {}))
            secrets[model.secret_name] = key
            token = POLICY.set({**policy, "secret_values": secrets})
        result = call()
    except Exception as exc:
        message = str(exc).replace(key, "[REDACTED]") if key else str(exc)
        raise ValueError(message) from None
    finally:
        if token is not None:
            POLICY.reset(token)

    def redact(value):
        if isinstance(value, str):
            return value.replace(key, "[REDACTED]") if key else value
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {redact(k): redact(v) for k, v in value.items()}
        return value
    return redact(result)
