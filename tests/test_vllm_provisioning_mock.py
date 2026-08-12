"""Mock-mode proof that the vLLM serving path provisions end-to-end — no cluster.

The dynamic-interface plumbing (sbatch -> dynamic /dev/tcp port -> PORT_FILE ->
squeue poll -> read port) is backend-agnostic: gpu.py only swapped the serve-script
*body* (Ollama -> Apptainer+vLLM). So the SAME MockExecutor that drives the Ollama
flow drives the vLLM flow unchanged — it treats the sbatch as an opaque submit.

Together with test_vllm_client.py (/v1 client) and test_gpu_serve_script.py (serve
command), this confirms the vLLM provisioning + client path "runs through" offline,
BEFORE the full Ollama removal + app.py rewire.
"""

from __future__ import annotations

import base64

from bioagent.gateway import gpu
from bioagent.gateway.mock_host import MockExecutor
from bioagent.gateway.settings import HPCSettings


def _vllm_settings() -> HPCSettings:
    return HPCSettings(
        llm_backend="vllm",
        vllm_image="/dfs3b/ruic20_lab/software/bioagent/containers/vllm.sif",
        vllm_model="QuantTrio/Qwen3.6-35B-A3B-AWQ",
        hf_home="/dfs3b/ruic20_lab/software/bioagent/hf",
    )


def test_vllm_serve_job_allocates_and_reads_dynamic_port():
    ex = MockExecutor(preinstalled=True, username="testuser")
    settings = _vllm_settings()

    # No job yet -> fresh submit, then it must reach RUNNING with a node + port.
    assert gpu.find_running_job(ex, settings) is None
    alloc = gpu.ensure_serve_job(ex, settings, lambda *a: None, wait_seconds=5)
    assert alloc.job_id == "1284756"
    assert alloc.node == "gpu-3-1"
    assert alloc.port == ex.state.serve_port      # dynamic port read back from PORT_FILE

    # Reconnect reuses the user's own running job (no second allocation).
    reused = gpu.find_running_job(ex, settings)
    assert reused is not None and reused.reused is True and reused.job_id == alloc.job_id


def test_vllm_submitted_script_is_singularity_vllm():
    # What ensure_serve_job writes+submits for vLLM is the Apptainer/vLLM serve
    # script (group-wrapped because the image/HF live on lab DFS).
    settings = _vllm_settings()
    script = gpu._serve_script(settings, "testuser")
    assert "sg ruic20_hpc -c 'bash -s'" in script           # DFS group wrapper
    body = base64.b64decode(script.split("printf %s ", 1)[1].split(" |", 1)[0]).decode("utf-8")
    assert "singularity exec --nv" in body  # RCIC HPC3 uses Singularity
    assert "vllm serve QuantTrio/Qwen3.6-35B-A3B-AWQ" in body
    assert "--enable-auto-tool-choice --tool-call-parser qwen3_coder" in body
    assert "ollama serve" not in body                        # no Ollama on this path


def test_vllm_health_check_still_works():
    # GPU health (srun nvidia-smi) is backend-agnostic — must still parse.
    ex = MockExecutor(preinstalled=True)
    settings = _vllm_settings()
    alloc = gpu.ensure_serve_job(ex, settings, lambda *a: None, wait_seconds=5)
    health = gpu.check_health(ex, settings, alloc)
    assert health.healthy is True and 0 <= health.util_percent <= 100
