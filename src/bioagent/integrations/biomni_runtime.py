"""Runtime clients that turn the BiomniAdapter plan into a real (or mocked) call.

The real client lazily imports ``biomni`` (an optional extra — ``pip install
biomni``) so the lightweight core and offline CI never need it installed. The
mock client implements the same tiny interface, which lets ``BiomniAdapter.run``
be exercised end-to-end before Biomni is available on the eye server.

Privacy: the real client points Biomni at the *local* Qwen3.6 endpoint (an SSH
tunnel to the HPC3 Ollama serve job), so reasoning never reaches a cloud LLM.
See ``docs/archive/biomni_kosmos_integration.md`` §4.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable


class BiomniNotInstalledError(RuntimeError):
    """Raised when the real Biomni runtime is requested but ``biomni`` is absent."""


@dataclass(frozen=True)
class BiomniRuntimeConfig:
    """How to construct a real ``biomni.agent.A1``, all env-overridable.

    Grounded in the upstream Biomni source (verified against snap-stanford/Biomni):

    - ``source="Custom"`` (NOT ``"Ollama"``): Biomni's ``Ollama`` source builds a
      ``ChatOllama`` that **ignores ``base_url``** and only hits localhost:11434, so it
      cannot reach our per-session HPC3 tunnel (a dynamic port). ``Custom`` builds a
      ``ChatOpenAI`` against ``base_url`` — and Ollama exposes an OpenAI-compatible API
      at ``/v1``, so this reaches the tunnel (and OpenRouter for local tests).
    - ``load_data_lake=False``: A1 downloads an ~11GB S3 data lake when
      ``expected_data_lake_files is None``. We pass ``[]`` to skip it; flip to True only
      when a task needs lake-backed tools.
    """

    data_path: str = "/data/BioAgent/biomni_data"
    model: str = "qwen3.6:35b-a3b"
    source: str = "Custom"
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key: str = "ollama"
    use_tool_retriever: bool = False
    load_data_lake: bool = False

    @classmethod
    def from_env(cls) -> "BiomniRuntimeConfig":
        def _flag(name: str, default: bool) -> bool:
            raw = os.environ.get(name)
            return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}

        # BIOAGENT_BIOMNI_MODEL wins; else the vLLM served model (the name vLLM
        # exposes via --served-model-name, which a chat request must match); else
        # the legacy shared var; else the default. In the console path the model is
        # passed in per-session, so this chain only drives the standalone CLI/debug.
        model = (
            os.environ.get("BIOAGENT_BIOMNI_MODEL")
            or os.environ.get("BIOAGENT_VLLM_MODEL")
            or os.environ.get("BIOAGENT_OLLAMA_MODEL")
            or cls.model
        )
        return cls(
            data_path=os.environ.get("BIOAGENT_BIOMNI_DATA_PATH", cls.data_path),
            model=model,
            source=os.environ.get("BIOAGENT_BIOMNI_SOURCE", cls.source),
            base_url=os.environ.get("BIOAGENT_BIOMNI_BASE_URL", cls.base_url),
            api_key=os.environ.get("BIOAGENT_BIOMNI_API_KEY", cls.api_key),
            use_tool_retriever=_flag("BIOAGENT_BIOMNI_USE_TOOL_RETRIEVER", cls.use_tool_retriever),
            load_data_lake=_flag("BIOAGENT_BIOMNI_LOAD_DATA_LAKE", cls.load_data_lake),
        )


@dataclass(frozen=True)
class BiomniRunResult:
    """Normalized result of a single Biomni ``A1.go`` call."""

    status: str          # "ok" | "error"
    task: str
    answer: str
    log: str
    runtime: str         # "real" | "mock"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class BiomniRuntime(Protocol):
    """Minimal interface the adapter needs — a single ``run`` call."""

    def run(self, task: str) -> BiomniRunResult: ...


def _normalize_go_return(task: str, raw: Any, runtime: str) -> BiomniRunResult:
    """Coerce Biomni ``A1.go``'s return into a stable ``BiomniRunResult``.

    Biomni's ``go`` historically returns ``(log, final_message)``; we stay
    defensive in case that shape shifts across versions.
    """

    if isinstance(raw, tuple) and len(raw) == 2:
        log, answer = raw
        log_text = "\n".join(str(item) for item in log) if isinstance(log, (list, tuple)) else str(log)
        answer_text = str(answer)
    else:
        log_text = ""
        answer_text = str(raw)
    return BiomniRunResult(status="ok", task=task, answer=answer_text, log=log_text, runtime=runtime)


class RealBiomniRuntime:
    """Lazily builds a ``biomni.agent.A1`` and runs the task through it."""

    def __init__(self, config: BiomniRuntimeConfig | None = None) -> None:
        self.config = config or BiomniRuntimeConfig.from_env()
        self._agent: Any | None = None

    def _ensure_agent(self) -> Any:
        if self._agent is None:
            try:
                from biomni.agent import A1  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - exercised only without biomni
                raise BiomniNotInstalledError(
                    "Biomni is not installed. Install the optional extra with "
                    "`pip install biomni` (or `pip install .[biomni]`) on the eye "
                    "server, then retry. Use MockBiomniRuntime for offline tests."
                ) from exc
            self._agent = A1(
                path=self.config.data_path,
                llm=self.config.model,
                source=self.config.source,
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                use_tool_retriever=self.config.use_tool_retriever,
                # None -> A1 downloads the ~11GB S3 data lake; [] -> skip it.
                expected_data_lake_files=None if self.config.load_data_lake else [],
            )
        return self._agent

    def run(self, task: str) -> BiomniRunResult:
        agent = self._ensure_agent()
        raw = agent.go(task)
        return _normalize_go_return(task, raw, runtime="real")


class MockBiomniRuntime:
    """Offline stand-in for Biomni — deterministic, records the tasks it sees.

    Used by tests and by the smoke path so ``BiomniAdapter.run`` can be exercised
    end-to-end before the real Biomni stack is installed.
    """

    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def run(self, task: str) -> BiomniRunResult:
        self.calls.append(task)
        answer = self.answer or f"[mock biomni] would call A1.go on a sanitized brief: {task[:120]}"
        return BiomniRunResult(
            status="ok",
            task=task,
            answer=answer,
            log="mock biomni runtime — no real A1 call was made",
            runtime="mock",
        )
