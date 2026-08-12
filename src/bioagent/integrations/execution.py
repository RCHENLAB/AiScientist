"""Env-driven selection of Biomni / Kosmos execution mode + runtime.

This is the single place that decides *whether* the pipeline and the autonomous
loop actually call the real Biomni / Kosmos runtimes, and *which* runtime they
call. It exists so the wiring in ``agents/pipeline.py`` and
``eval/autonomous_loop.py`` stays a one-liner and the policy lives in one place.

Default posture (no env set):
    - execution is **OFF** -> the adapters stay plan-only (the historical
      behavior). Nothing imports or calls the heavy runtimes, so the lightweight
      core and offline CI stay green.

When execution is enabled (``BIOAGENT_<FW>_EXECUTE=1``):
    - the adapter is built with ``mode="execute"`` so ``.run()`` proceeds past
      the policy gate (the ``DataBoundaryGuard`` still runs first and can still
      block on secret / raw-table content);
    - the runtime is chosen by ``BIOAGENT_<FW>_RUNTIME``:
        * ``real`` (default) -> the real runtime, which imports ``biomni`` /
          shells out to the ``kosmos`` CLI lazily and raises a clear
          *NotInstalled* error if the framework is absent (strict production
          posture: never silently fall back to a fake answer);
        * ``mock``           -> the deterministic in-process mock, so the full
          execute path can be exercised on a laptop without installing the heavy
          stacks (local-dev / CI posture).

All LLM endpoints (Ollama base URL, model) come from the runtime configs'
``from_env`` (see ``biomni_runtime`` / ``kosmos_runtime``), which already default
to the local Qwen3.6 over the HPC3 SSH tunnel and are fully env-overridable — so
nothing here hardcodes a host or model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from .biomni_adapter import BiomniAdapter, BiomniSafetyPolicy
from .biomni_runtime import BiomniRuntime, BiomniRuntimeConfig, MockBiomniRuntime, RealBiomniRuntime

_REAL = "real"
_MOCK = "mock"


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _runtime_kind(name: str, default: str = _REAL, allowed: set[str] = frozenset({_REAL, _MOCK})) -> str:
    kind = (os.environ.get(name) or default).strip().lower()
    return kind if kind in allowed else default


@dataclass(frozen=True)
class BiomniExecution:
    """A ready-to-call Biomni adapter + the runtime ``.run()`` should use.

    ``runtime is None`` means "let the adapter construct the real runtime lazily"
    (the strict path). A non-None ``runtime`` is an explicit mock for local dev.
    """

    enabled: bool
    runtime_kind: str
    adapter: BiomniAdapter
    runtime: BiomniRuntime | None

    @classmethod
    def from_env(cls, *, ollama_port: int | None = None, model: str | None = None) -> "BiomniExecution":
        """Build the execution config from env, with optional per-session overrides:
        ``ollama_port`` pins the real runtime at this session's live tunnel port
        (``127.0.0.1:<port>/v1``) and ``model`` is the model the user picked in the
        UI — so concurrent users each reach their OWN Qwen3.6 with the model they
        chose, no process-wide fixed port or model. When both are omitted, the real
        runtime falls back to the env/default config (``BIOAGENT_BIOMNI_MODEL`` /
        ``BIOAGENT_OLLAMA_MODEL`` / base_url)."""
        enabled = _flag("BIOAGENT_BIOMNI_EXECUTE")
        kind = _runtime_kind("BIOAGENT_BIOMNI_RUNTIME")
        policy = BiomniSafetyPolicy(mode="execute" if enabled else "offline_plan")
        runtime: BiomniRuntime | None = None
        if enabled and kind == _MOCK:
            runtime = MockBiomniRuntime()
        elif enabled and (ollama_port or model):
            overrides: dict[str, str] = {}
            if ollama_port:
                overrides["base_url"] = f"http://127.0.0.1:{ollama_port}/v1"
            if model:
                overrides["model"] = model  # raw Ollama tag, e.g. "qwen3.6:35b-a3b"
            runtime = RealBiomniRuntime(replace(BiomniRuntimeConfig.from_env(), **overrides))
        return cls(enabled=enabled, runtime_kind=kind, adapter=BiomniAdapter(policy=policy), runtime=runtime)
