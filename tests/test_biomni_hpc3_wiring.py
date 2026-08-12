"""Biomni ↔ HPC3 Qwen3.6 wiring — offline mock tests (deploy + run before going real).

Biomni is the kept "real biomedical tools" backend. Before flipping
``BIOAGENT_BIOMNI_RUNTIME=real`` on the eye server, these tests prove — with NO biomni
install and NO GPU — that:

  1. Biomni is pinned to THIS session's HPC3 Qwen3.6: the live SSH-tunnel port becomes
     the OpenAI-compatible ``base_url`` and the UI model pick becomes the model, with
     ``source="Ollama"`` (a local endpoint, never a cloud LLM).
  2. Without a session, the runtime is deferred so the adapter builds it lazily from env.
  3. The execute path runs end-to-end through a ``MockBiomniRuntime`` behind the
     DataBoundaryGuard (the task reaches A1; raw tables are refused before any call).

Run on the server with: ``/data/BioAgent/env/bin/python -m pytest tests/test_biomni_hpc3_wiring.py``
"""

from __future__ import annotations

import pytest

from bioagent.integrations.biomni_adapter import EXECUTE_MODE, BiomniAdapter, BiomniSafetyPolicy
from bioagent.integrations.biomni_runtime import MockBiomniRuntime, RealBiomniRuntime
from bioagent.integrations.execution import BiomniExecution

_BIOMNI_ENV = ("BIOAGENT_BIOMNI_EXECUTE", "BIOAGENT_BIOMNI_RUNTIME", "BIOAGENT_BIOMNI_BASE_URL", "BIOAGENT_BIOMNI_MODEL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _BIOMNI_ENV:
        monkeypatch.delenv(var, raising=False)


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIOAGENT_BIOMNI_EXECUTE", "1")
    monkeypatch.setenv("BIOAGENT_BIOMNI_RUNTIME", "real")


def test_biomni_runtime_pinned_to_session_tunnel_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    ex = BiomniExecution.from_env(ollama_port=37219, model="qwen3.6:35b-a3b")

    assert ex.enabled
    assert isinstance(ex.runtime, RealBiomniRuntime)
    cfg = ex.runtime.config
    # The HPC3 tunnel's LOCAL end is what Biomni (running on the eye server) connects to.
    assert cfg.base_url == "http://127.0.0.1:37219/v1"
    assert cfg.model == "qwen3.6:35b-a3b"
    # "Custom" = OpenAI-compatible, honors base_url (the "Ollama" source ignores it and
    # can't reach the tunnel's dynamic port). Ollama's /v1 endpoint is OpenAI-compatible.
    assert cfg.source == "Custom"
    assert cfg.api_key == "ollama"
    # data lake skipped by default — opt in with BIOAGENT_BIOMNI_LOAD_DATA_LAKE=1.
    assert cfg.load_data_lake is False


def test_biomni_without_session_defers_runtime_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    ex = BiomniExecution.from_env()  # no per-session port/model (e.g. a non-console caller)

    assert ex.enabled
    assert ex.adapter.policy.mode == "execute"
    # No session pin -> the adapter builds RealBiomniRuntime lazily from env at .run().
    assert ex.runtime is None


def test_biomni_disabled_stays_plan_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # EXECUTE unset -> plan-only, no runtime constructed (the safe default).
    ex = BiomniExecution.from_env(ollama_port=37219, model="qwen3.6:35b-a3b")
    assert ex.enabled is False
    assert ex.runtime is None
    assert ex.adapter.policy.mode == "offline_plan"


def test_biomni_execute_path_reaches_runtime_via_mock() -> None:
    # Full execute path with a mock A1: the sanitized task reaches the runtime and the
    # answer comes back as an executed result — no biomni install, no GPU.
    adapter = BiomniAdapter(policy=BiomniSafetyPolicy(mode=EXECUTE_MODE))
    mock = MockBiomniRuntime(answer="RHO")

    result = adapter.run("What is the official HGNC gene symbol for rhodopsin?", runtime=mock)

    assert result.executed
    assert result.run_result is not None and result.run_result.answer == "RHO"
    assert mock.calls == ["What is the official HGNC gene symbol for rhodopsin?"]


def test_dataset_path_flows_into_biomni_task() -> None:
    # #1 data flow: when a dataset is attached, its PATH (not its rows) reaches A1 so
    # Biomni's generated code reads the local file. The data never enters the prompt.
    from pathlib import Path

    adapter = BiomniAdapter(policy=BiomniSafetyPolicy(mode=EXECUTE_MODE))
    mock = MockBiomniRuntime(answer="done")

    result = adapter.run("Run QC on the PBMC data.", dataset_path=Path("/data/pbmc3k.h5ad"), runtime=mock)

    assert result.executed
    sent = mock.calls[0]
    assert "Run QC on the PBMC data." in sent
    assert "/data/pbmc3k.h5ad" in sent  # the path reached the agent
    assert "read it from the path" in sent


def test_biomni_guard_blocks_raw_table_before_any_runtime_call() -> None:
    adapter = BiomniAdapter(policy=BiomniSafetyPolicy(mode=EXECUTE_MODE))
    mock = MockBiomniRuntime()
    rows = "\n".join("gene,1.1,2.2,3.3,4.4,5.5" for _ in range(4))

    result = adapter.run(f"Analyze:\n{rows}", runtime=mock)

    assert result.status == "blocked_by_guard"
    assert mock.calls == []  # the raw table never reached Biomni / the LLM
