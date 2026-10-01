"""The tool contract: the one module the tools and the platform share.

Every model-callable tool in ``aiscientist.tools`` is a :class:`HarnessTool`, an OpenAI/vLLM function
schema plus a Python executor ``(args, ctx) -> result dict``. The platform (``aiscientist.agents`` and
``aiscientist.gateway``) assembles the catalog from these records, routes some of them to HPC3 job
lines, and hands each executor a context object.

This module exists so that the tools never import the platform. Before it, twelve tool modules
imported ``HarnessTool`` from ``agents.research_harness``, and two reached into
``gateway.vllm_client`` for an LLM. That was a cycle (the agents import the tools, the tools import
the agents), and it is what kept the tools from shipping as their own package (AiScientist-tools).
The dependency now points one way: the platform imports the tools; the tools import this module and
nothing above it. ``tests/test_repo_boundaries.py`` holds that line.

Three pieces:

* :class:`HarnessTool` is the tool record. It moved here unchanged; ``agents.research_harness``
  re-exports it, so ``from aiscientist.agents.research_harness import HarnessTool`` keeps working.
* :class:`ToolContext` lists the fields a tool may read from its context. The platform's
  ``HarnessContext`` satisfies it. A tool that needs something else should take it as an argument
  or a factory parameter, not reach for a platform attribute.
* :func:`session_chat_fn` lets a tool ask the session's served model a bounded question without
  importing the gateway. The gateway registers the backend when ``gateway.vllm_client`` is imported
  (:func:`register_llm_backend`); a process without that backend (an HPC3 job, a script) gets
  ``None`` and the tool takes its no-LLM path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class ToolContext(Protocol):
    """What a tool may read from the context it is called with.

    ``decisions`` carries the run's bound data (``dataset_path``, ``dataset_result``, ...);
    ``workspace`` is the run directory a tool writes ``work/`` and ``artifacts/`` under;
    ``tunnel_port`` / ``model`` identify the session's served model (``None`` when the session has
    no local model); ``llm_is_remote`` says whether prompts leave this host. Optional extras that a
    few tools probe with ``getattr`` (``llm_base_url``) are not part of the contract.
    """

    decisions: dict[str, Any]
    workspace: Path | None
    tunnel_port: int | None
    model: str
    llm_is_remote: bool


@dataclass(frozen=True)
class HarnessTool:
    """One callable tool: an OpenAI/vLLM function schema + a Python executor."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON-schema for the args object
    executor: Callable[[dict[str, Any], "ToolContext"], dict[str, Any]]
    reads_private_data: bool = False
    category: str = "general"            # registry metadata: qc | analysis | figure | codeact | backend | control
    requires: tuple[str, ...] = ()       # capability deps (e.g. "scanpy", "gseapy", "graphviz", "biomni")
    # False when the tool is PRESENT in the catalog but cannot actually run in this deployment —
    # e.g. scgpt_annotate without a live GPU session. Such a tool stays listed (the System page
    # shows what exists, and the model gets an honest not-enabled result rather than a missing
    # name), but a preset pipeline built around it should not be chosen as if it were available.
    enabled: bool = True

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


# (port, model, messages, *, max_tokens, timeout, think) -> reply text
LlmBackend = Callable[..., str]
ChatFn = Callable[[list[dict[str, Any]]], str]

_llm_backend: LlmBackend | None = None


def register_llm_backend(backend: LlmBackend | None) -> None:
    """Install the function that sends one bounded completion to the session's served model.

    Called by ``gateway.vllm_client`` at import time. ``None`` removes it (tests)."""
    global _llm_backend
    _llm_backend = backend


def session_chat_fn(ctx: Any, *, max_tokens: int, timeout: float, think: bool = False) -> ChatFn | None:
    """A ``messages -> text`` callable bound to the session's served model, or ``None``.

    ``None`` when the context has no ``tunnel_port`` (no local model in this session) or no backend
    is registered in this process. Callers treat ``None`` as "no LLM available" and fall back to
    their deterministic path; they must not guess an endpoint themselves.
    """
    port = getattr(ctx, "tunnel_port", None)
    backend = _llm_backend
    if port is None or backend is None:
        return None
    model = getattr(ctx, "model", "") or ""

    def chat(messages: list[dict[str, Any]]) -> str:
        return backend(port, model, messages, max_tokens=max_tokens, timeout=timeout, think=think)

    return chat
