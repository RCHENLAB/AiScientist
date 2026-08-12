"""Offline-safe tests for the HPC3 gateway provisioning logic.

These exercise the GPU + Ollama managers against the in-process mock host, so
they need neither paramiko/fastapi nor any network. (The FastAPI app and the
paramiko SSH executor are intentionally not imported here.)
"""

from __future__ import annotations

from bioagent.gateway import gpu
from bioagent.gateway.mock_host import MockExecutor
from bioagent.gateway.settings import HPCSettings


def _settings() -> HPCSettings:
    return HPCSettings()


def test_vllm_ensure_installed_against_mock() -> None:
    """The connect flow's image-presence check: vllm_client.ensure_installed runs
    `test -f <image>.sif` against the host — the mock answers it, so provisioning
    proceeds (no Ollama binary install anymore)."""
    from bioagent.gateway import vllm_client

    ex = MockExecutor(preinstalled=True)
    info = vllm_client.ensure_installed(ex, HPCSettings(), lambda *a: None)
    assert info["installed"] is True and info["engine"] == "vllm"


def test_gpu_serve_job_allocates_and_runs() -> None:
    ex = MockExecutor(preinstalled=True)
    settings = _settings()

    # no job initially
    assert gpu.find_running_job(ex, settings) is None

    alloc = gpu.ensure_serve_job(ex, settings, lambda *a: None, wait_seconds=5)
    assert alloc.job_id == "1284756"
    assert alloc.node == "gpu-3-1"

    # a second call should reuse the now-running job, not submit a new one
    reused = gpu.find_running_job(ex, settings)
    assert reused is not None
    assert reused.job_id == alloc.job_id
    assert reused.reused is True


def test_serve_job_reuses_own_job_only() -> None:
    """Reconnecting reuses the user's *own* running job, not a new submit."""
    ex = MockExecutor(preinstalled=True, username="testuser")
    settings = _settings()
    first = gpu.ensure_serve_job(ex, settings, lambda *a: None, wait_seconds=5)

    events: list[str] = []
    second = gpu.ensure_serve_job(ex, settings, lambda lvl, stage, msg: events.append(msg), wait_seconds=5)
    assert second.job_id == first.job_id
    assert second.reused is True
    assert second.owner == "testuser"
    assert any("your own running" in m for m in events)


def test_job_name_is_per_user() -> None:
    """The serve job is named per-user so we can only ever match our own."""
    assert gpu.job_name("testuser") == "bioagent-vllm-testuser"
    # the squeue search is scoped with --me and the per-user name
    ex = MockExecutor(preinstalled=True, username="testuser")
    gpu.ensure_serve_job(ex, _settings(), lambda *a: None, wait_seconds=5)
    # find_running_job must include --me and the per-user name in its squeue call
    captured = {}
    orig = ex.exec
    def spy(cmd, timeout=60.0):
        if "squeue" in cmd and "--name" in cmd:
            captured["cmd"] = cmd
        return orig(cmd, timeout)
    ex.exec = spy  # type: ignore[assignment]
    gpu.find_running_job(ex, _settings())
    assert "--me" in captured.get("cmd", "")
    assert "bioagent-vllm-testuser" in captured.get("cmd", "")


def test_data_group_only_for_dfs_paths() -> None:
    """The ruic20_hpc group is applied only when the vLLM image / HF cache live on
    lab DFS storage, not under $HOME (the serve job must run under that group to
    read DFS)."""
    dfs = HPCSettings()  # defaults: vllm image + HF cache on /dfs3b/ruic20_lab/...
    home = HPCSettings(vllm_image="$HOME/containers/vllm.sif", hf_home="$HOME/hf")
    assert dfs.data_group() == "ruic20_hpc"
    assert home.data_group() is None


def test_gpu_health_parses_nvidia_smi() -> None:
    ex = MockExecutor(preinstalled=True)
    settings = _settings()
    alloc = gpu.ensure_serve_job(ex, settings, lambda *a: None, wait_seconds=5)

    health = gpu.check_health(ex, settings, alloc)
    assert health.healthy is True
    assert health.mem_total_mb == 49140
    assert "L40S" in health.name
    assert 0 <= health.util_percent <= 100
