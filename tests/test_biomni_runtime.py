"""Offline tests for the I3 wiring: BiomniAdapter.run behind the safety guard.

These never import the real ``biomni`` package — they drive the adapter with a
``MockBiomniRuntime`` so the guard/policy/runtime path is verified before Biomni
is installed on the eye server.
"""

import pytest

from bioagent.integrations.biomni_adapter import (
    EXECUTE_MODE,
    BiomniAdapter,
    BiomniSafetyPolicy,
)
from bioagent.integrations.biomni_runtime import (
    BiomniNotInstalledError,
    BiomniRuntimeConfig,
    MockBiomniRuntime,
    RealBiomniRuntime,
)


def _execute_policy() -> BiomniSafetyPolicy:
    return BiomniSafetyPolicy(mode=EXECUTE_MODE)


def test_run_executes_with_mock_runtime_when_policy_allows() -> None:
    adapter = BiomniAdapter(policy=_execute_policy())
    runtime = MockBiomniRuntime()

    result = adapter.run("Summarize retina marker genes for RPE cells.", runtime=runtime)

    assert result.executed
    assert result.status == "executed"
    assert result.run_result is not None
    assert result.run_result.runtime == "mock"
    assert runtime.calls == ["Summarize retina marker genes for RPE cells."]
    # The plan is still produced alongside the run.
    assert result.plan.status == "offline_plan_prepared"


def test_default_policy_is_plan_only_and_never_calls_runtime() -> None:
    adapter = BiomniAdapter()  # default mode == "offline_plan"
    runtime = MockBiomniRuntime()

    result = adapter.run("Any question.", runtime=runtime)

    assert result.status == "blocked_by_policy"
    assert not result.executed
    assert result.run_result is None
    assert runtime.calls == []  # runtime must not be touched on the blocked path
    assert any("plan-only" in note for note in result.notes)


def test_guard_blocks_secret_before_runtime() -> None:
    adapter = BiomniAdapter(policy=_execute_policy())
    runtime = MockBiomniRuntime()
    task = "Use this key OPENAI_API_KEY=sk-abcdef0123456789 to call the model."

    result = adapter.run(task, runtime=runtime)

    assert result.status == "blocked_by_guard"
    assert result.run_result is None
    assert runtime.calls == []
    assert result.data_boundary.prompt_contains_secret_risk is True


def test_guard_blocks_raw_table_when_private_data_not_allowed() -> None:
    adapter = BiomniAdapter(policy=BiomniSafetyPolicy(mode=EXECUTE_MODE, allow_private_data=False))
    runtime = MockBiomniRuntime()
    rows = "\n".join("gene,1.1,2.2,3.3,4.4,5.5" for _ in range(4))
    task = f"Analyze these expression rows:\n{rows}"

    result = adapter.run(task, runtime=runtime)

    assert result.status == "blocked_by_guard"
    assert runtime.calls == []
    assert result.data_boundary.prompt_contains_raw_data_risk is True


def test_to_dict_is_json_friendly() -> None:
    adapter = BiomniAdapter(policy=_execute_policy())
    result = adapter.run("Plan a QC pass.", runtime=MockBiomniRuntime(answer="ok"))
    payload = result.to_dict()

    assert payload["status"] == "executed"
    assert payload["run_result"]["answer"] == "ok"
    assert isinstance(payload["plan"], dict)
    assert isinstance(payload["data_boundary"], dict)


def test_runtime_config_from_env_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "BIOAGENT_BIOMNI_MODEL",
        "BIOAGENT_OLLAMA_MODEL",
        "BIOAGENT_BIOMNI_DATA_PATH",
        "BIOAGENT_BIOMNI_SOURCE",
        "BIOAGENT_BIOMNI_BASE_URL",
        "BIOAGENT_BIOMNI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    default = BiomniRuntimeConfig.from_env()
    assert default.model == "qwen3.6:35b-a3b"
    assert default.base_url == "http://127.0.0.1:11434/v1"

    # Shared Ollama var is the fallback; the Biomni-specific var wins.
    monkeypatch.setenv("BIOAGENT_OLLAMA_MODEL", "qwen3")
    assert BiomniRuntimeConfig.from_env().model == "qwen3"
    monkeypatch.setenv("BIOAGENT_BIOMNI_MODEL", "qwen3.6:35b-a3b")
    assert BiomniRuntimeConfig.from_env().model == "qwen3.6:35b-a3b"


def test_real_runtime_raises_clear_error_without_biomni() -> None:
    # biomni is not installed in CI, so constructing the agent must raise the
    # actionable error (not a bare ImportError) when run() forces the import.
    try:
        import biomni  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - only when biomni is actually installed
        pytest.skip("biomni is installed; the not-installed error path does not apply")

    runtime = RealBiomniRuntime(BiomniRuntimeConfig())
    with pytest.raises(BiomniNotInstalledError):
        runtime.run("anything")
