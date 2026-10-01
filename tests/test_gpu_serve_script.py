"""The Slurm serve-script always serves vLLM. These offline tests pin the
dynamic-interface contract (per-user job name, free-port pick, port file, sg group,
sbatch) so it can't silently break.
"""

from __future__ import annotations

import base64

from aiscientist.gateway import gpu
from aiscientist.gateway.settings import HPCSettings
from aiscientist.gateway.settings import LAB_STORAGE, REFERENCE_ROOT, SHARED_ROOT  # noqa: F401


def _script(**over) -> str:
    return gpu._serve_script(HPCSettings(**over), "testuser")


# --- dynamic-interface plumbing (per-user job name + free-port pick) ---------

def test_serve_script_keeps_dynamic_port_and_jobname():
    s = _script(llm_backend="vllm")
    assert "#SBATCH --job-name=bioagent-vllm-testuser" in s
    assert "/dev/tcp/127.0.0.1/$_cand" in s          # free-port probe
    assert f'echo "$AISCIENTIST_PORT" > {gpu.PORT_FILE_JOB}' in s  # writes the PER-JOB port file
    assert "vllm.${SLURM_JOB_ID}.port" in s                  # per-job (not a single clobbered file)
    assert "ollama" not in s.lower()                 # no Ollama anywhere in the serve script


# --- vLLM path ---------------------------------------------------------------

def test_vllm_backend_runs_singularity_vllm_with_tool_calling():
    s = _script(
        llm_backend="vllm",
        vllm_image=f"{LAB_STORAGE}/software/AiScientist/containers/vllm.sif",
        vllm_model="QuantTrio/Qwen3.6-35B-A3B-AWQ",
        vllm_quantization="awq_marlin",
        hf_home=f"{LAB_STORAGE}/software/AiScientist/hf",
    )
    # group-wrap: vLLM image/HF on DFS -> body is base64'd into `sg ruic20_hpc`
    assert "sg ruic20_hpc -c 'bash -s'" in s
    # the real serve command lives inside the base64 blob — decode and assert on it
    body = gpu.serve_body_from_script(s)
    assert "singularity exec --nv" in body  # RCIC HPC3 uses Singularity, not Apptainer
    assert "vllm serve QuantTrio/Qwen3.6-35B-A3B-AWQ" in body
    assert "--enable-auto-tool-choice --tool-call-parser qwen3_coder" in body
    assert "--quantization awq_marlin" in body
    assert "--reasoning-parser qwen3" in body
    assert f'--port "$(cat {gpu.PORT_FILE_JOB})"' in body      # binds the dynamic per-job port
    assert "export HF_HOME=\"/dfs3b/ruic20_lab/software/AiScientist/hf\"" in body
    assert "HF_HUB_OFFLINE=1" in body                          # weights pre-staged, no hub call


def test_vllm_optional_flags_omitted_when_unset():
    s = _script(llm_backend="vllm", vllm_quantization="", vllm_reasoning_parser="", vllm_extra_args="")
    body = gpu.serve_body_from_script(s)
    assert "--quantization" not in body
    assert "--reasoning-parser" not in body
    # tool calling is always on
    assert "--enable-auto-tool-choice" in body


def test_default_model_is_qwen38_int4_autodetected_quantization():
    s = HPCSettings()
    body = gpu._vllm_serve_body(s)
    assert "vllm serve RedHatAI/Qwen3.8-27B-INT4 " in body
    assert "/containers/vllm-0.28.0.sif" in body
    assert "--quantization" not in body      # compressed-tensors is auto-detected
