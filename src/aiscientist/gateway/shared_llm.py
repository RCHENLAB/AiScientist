"""The lab's shared model server.

A session's own GPU job is one card and one model that fits it. A model that needs several cards
(DeepSeek-V4-Flash on four 96 GB cards) cannot be asked for per session: four free cards on one
node can take hours or days to get. So one Slurm job holds them for as long as the partition
allows, and a session can choose it as its LLM endpoint, like a saved API key: it then holds no GPU
of its own and every call of the session goes to it. A session on another endpoint (its own GPU's
Qwen, or an API key) never uses it — each session is on one model.

This module is the keeper of that job:

* it finds the job by name (``aiscientist-llm-shared``, after the deployment's Slurm job-name
  prefix) under ONE HPC3 account, submits it when there is none, and submits the next one ahead of
  the running job's time limit so the queue wait happens in the background;
* it holds one local port whose far end follows the job (node and port change at every renewal),
  so the address the gateway, or anything else on this host, uses never changes;
* it moves to a newer job only once that job answers, lets calls in flight on the old one finish,
  then cancels the old one.

The job is not a session job: its name matches none of the prefixes a session release cancels.
The server is started with an API key that lives in a 0600 file in the account's HPC3 home and is
read over SSH; it is never written to this host's disk, the job script or a command line.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .errors import GatewayError
from .executor import RemoteExecutor
from .settings import HPCSettings, slurm_job_name, slurm_job_prefix
from .slurm_job import slurm_time_to_seconds

JOB_NAME = "aiscientist-llm-shared"     # the name's stem: job_name() adds the deployment's prefix


def job_name() -> str:
    """The shared job's Slurm name on this deployment."""
    return slurm_job_name(JOB_NAME)


def remote_dir_name() -> str:
    """The folder under ``~/.bioagent`` with the shared job's port, readiness marker, key, script and
    logs: ``shared-llm``, after the deployment's job-name prefix, so a second deployment keeps its
    own job and key apart. ``~/.bioagent`` is the HPC3-side directory the session serve job already
    uses; it keeps its old name (see CLAUDE.md)."""
    return f"{slurm_job_prefix()}shared-llm"


def remote_dir() -> str:
    return f"$HOME/.bioagent/{remote_dir_name()}"

# The serve flags of the verified run: DeepSeek-V4-Flash NVFP4 on stock vLLM 0.28.0, four RTX PRO
# 6000 cards (HPC3 job 55644297: KV pool 823,709 tokens, tool calls correct). That run used a 131,072
# window; the model takes 1,048,576 (YaRN x16 over 65,536). 262,144 matches the session GPU's Qwen
# window and leaves room for about three full-length requests at once (Yijun, 2026-10-15).
# ``--tensor-parallel-size`` is added from the card count.
_DSV4_SERVE_ARGS = (
    "--trust-remote-code --tokenizer-mode deepseek_v4 --kv-cache-dtype fp8 --block-size 256 "
    "--max-model-len 262144 --gpu-memory-utilization 0.90 --enable-auto-tool-choice "
    "--tool-call-parser deepseek_v4 --reasoning-parser deepseek_v4 --disable-custom-all-reduce"
)
# Multi-card serving on these nodes: the cards talk over PCIe, not InfiniBand or NVLink.
_MULTI_CARD_ENV = (
    "NCCL_DEBUG=WARN NCCL_IB_DISABLE=1 NCCL_SOCKET_IFNAME=lo VLLM_SKIP_P2P_CHECK=1 "
    "NCCL_CUMEM_ENABLE=0 NCCL_P2P_DISABLE=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"
)

# The "strata" engine: DeepSeek-V4.1-Flash-1017 at 2 bits (ISTA-DASLab's GSQ-RCO IQ2_XXS GGUF, six-shard layout:
# five model files, the 111 GB Engram lookup table as the sixth) served by Strata (github.com/Niko1221/Strata: its
# `strata-dsv41` engine behind `serve/server.py`). All of its experts fit two RTX PRO 6000 (HPC3 job 59392175:
# ~147 GB), so the job asks for two cards instead of four.
STRATA_MODEL = "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS"
STRATA_SERVED_NAME = "deepseek-v4.1-flash"
_STRATA_SHARDS = 5
_STRATA_ENGRAM = "DeepSeek-V4.1-Flash-Engram-IQ4_NL.gguf"
# The two files Strata's pack recipe for ISTA's sets also reads (its setup.py `dsv41_pack_cmd`): Unsloth's metadata
# shard, for the Engram hash parameters, and the DSpark drafter.
_STRATA_META = "UD-Q8_K_XL/DeepSeek-V4.1-Flash-1017-UD-Q8_K_XL-00001-of-00009.gguf"
_STRATA_DSPARK = "dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf"
_STRATA_MODULES = "gcc/11.2.0 cuda/13.0.1 python/3.10.2"
_ENGINES = ("vllm", "strata")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class SharedLLMSettings:
    enabled: bool = False
    # Whose HPC3 account runs the job. Either an app account with a saved (unencrypted) SSH key for
    # the cluster, or an explicit HPC3 user + private key file.
    owner: str = ""
    ssh_credential_id: str = ""
    hpc_user: str = ""
    key_path: str = ""
    # What is served.
    model: str = "MJPansa/DeepSeek-V4-Flash-0731-NVFP4"
    served_name: str = "deepseek-v4-flash"
    image: str = ""
    serve_args: str = _DSV4_SERVE_ARGS
    container_env: str = _MULTI_CARD_ENV
    # Where. 180G is what the verified four-card load used (the weights are mapped from disk);
    # with MaxMemPerCPU=6144 on these nodes that is 30 CPUs.
    partition: str = "free-gpu32"
    account: str = ""
    gres: str = "gpu:RTX6000:4"
    cpus: int = 30
    mem_gb: int = 180
    time_limit: str = "3-00:00:00"
    exclude: str = ""
    # How it is kept.
    # Submit the next job this long before the limit. Four free cards on one node took up to 56 h
    # to get (2026-08 to 10), so a day ahead was too late: the endpoint then had a gap.
    renew_lead_s: int = 48 * 3600
    drain_s: int = 1800                # after moving to a newer job, let the old one finish calls
    poll_s: int = 60
    local_port: int = 0                # 0 = any free port; fixed = a stable URL on this host
    # How it is called.
    reasoning_effort: str = "low"
    think_kwargs: tuple = (("enable_thinking", True),)
    # Which server runs the model: "vllm" (serve_args, image) or "strata" (the fields below).
    engine: str = "vllm"
    # strata: --max-context, 131,072 as measured on 2x RTX PRO 6000 and 2x A100 (vLLM's window is in
    # serve_args). The model reads 1,048,576; 262,144 is untested on our cards.
    context: int = 131072
    parallel: int = 4                  # strata: conversations at once, the gate's per-account limit
    strata_root: str = ""              # holds strata-src/, strata-venv/, strata-dsv41-build/
    gguf_dir: str = ""                 # the model's six GGUF shards (the sixth = the Engram file)
    gguf_shared_dir: str = ""          # the pack's metadata shard and DSpark drafter
    host_modules: str = _STRATA_MODULES

    @classmethod
    def from_env(cls, hpc: HPCSettings) -> "SharedLLMSettings":
        env = os.environ.get
        raw_think = env("AISCIENTIST_SHARED_LLM_THINK_KWARGS", "").strip()
        think = cls.think_kwargs
        if raw_think:
            try:
                val = json.loads(raw_think)
                think = tuple(sorted(val.items())) if isinstance(val, dict) else ()
            except json.JSONDecodeError:
                think = cls.think_kwargs
        engine = env("AISCIENTIST_SHARED_LLM_ENGINE", "").strip().lower() or cls.engine
        if engine not in _ENGINES:
            raise ValueError(f"AISCIENTIST_SHARED_LLM_ENGINE={engine!r}: use one of {', '.join(_ENGINES)}")
        strata = engine == "strata"
        # Two cards hold the 2-bit model; MaxMemPerCPU=6144 on free-gpu32 makes 24G four CPUs.
        model_d, name_d = (STRATA_MODEL, STRATA_SERVED_NAME) if strata else (cls.model, cls.served_name)
        gres_d, cpus_d, mem_d = ("gpu:RTX6000:2", 4, 24) if strata else (cls.gres, cls.cpus, cls.mem_gb)
        root = hpc.shared_root.rstrip("/")
        return cls(
            engine=engine,
            context=_int_env("AISCIENTIST_SHARED_LLM_CONTEXT", cls.context),
            parallel=_int_env("AISCIENTIST_SHARED_LLM_PARALLEL", cls.parallel),
            strata_root=env("AISCIENTIST_SHARED_LLM_STRATA_ROOT", "").strip() or f"{root}/engines",
            gguf_dir=env("AISCIENTIST_SHARED_LLM_GGUF_DIR", "").strip()
            or f"{root}/models/gguf/DeepSeek-V4.1-Flash-1017-GSQ-RCO",
            gguf_shared_dir=env("AISCIENTIST_SHARED_LLM_GGUF_SHARED_DIR", "").strip()
            or f"{root}/models/gguf/DeepSeek-V4.1-Flash-shared",
            host_modules=env("AISCIENTIST_SHARED_LLM_HOST_MODULES", cls.host_modules).strip(),
            enabled=_flag("AISCIENTIST_SHARED_LLM"),
            owner=env("AISCIENTIST_SHARED_LLM_OWNER", "").strip(),
            ssh_credential_id=env("AISCIENTIST_SHARED_LLM_SSH_CREDENTIAL", "").strip(),
            hpc_user=env("AISCIENTIST_SHARED_LLM_HPC_USER", "").strip(),
            key_path=env("AISCIENTIST_SHARED_LLM_KEY_PATH", "").strip(),
            model=env("AISCIENTIST_SHARED_LLM_MODEL", "").strip() or model_d,
            served_name=env("AISCIENTIST_SHARED_LLM_SERVED_NAME", "").strip() or name_d,
            image=env("AISCIENTIST_SHARED_LLM_IMAGE", "").strip() or hpc.vllm_image,
            serve_args=env("AISCIENTIST_SHARED_LLM_SERVE_ARGS", cls.serve_args).strip(),
            container_env=env("AISCIENTIST_SHARED_LLM_CONTAINER_ENV", cls.container_env).strip(),
            partition=env("AISCIENTIST_SHARED_LLM_PARTITION", cls.partition).strip(),
            account=env("AISCIENTIST_SHARED_LLM_ACCOUNT", "").strip(),
            gres=env("AISCIENTIST_SHARED_LLM_GRES", "").strip() or gres_d,
            cpus=_int_env("AISCIENTIST_SHARED_LLM_CPUS", cpus_d),
            mem_gb=_int_env("AISCIENTIST_SHARED_LLM_MEM_GB", mem_d),
            time_limit=env("AISCIENTIST_SHARED_LLM_TIME_LIMIT", cls.time_limit).strip(),
            exclude=env("AISCIENTIST_SHARED_LLM_EXCLUDE", "").strip() or (hpc.exclude or ""),
            renew_lead_s=_int_env("AISCIENTIST_SHARED_LLM_RENEW_LEAD_HOURS", 48) * 3600,
            drain_s=_int_env("AISCIENTIST_SHARED_LLM_DRAIN_SECONDS", cls.drain_s),
            poll_s=max(15, _int_env("AISCIENTIST_SHARED_LLM_POLL_SECONDS", cls.poll_s)),
            local_port=_int_env("AISCIENTIST_SHARED_LLM_LOCAL_PORT", 0),
            reasoning_effort=env("AISCIENTIST_SHARED_LLM_REASONING_EFFORT",
                                 cls.reasoning_effort).strip().lower(),
            think_kwargs=think,
        )


def gpu_count(gres: str) -> int:
    """``gpu:RTX6000:4`` -> 4, ``gpu:2`` -> 2, ``gpu:A100`` -> 1."""
    last = (gres or "").rsplit(":", 1)[-1]
    return int(last) if last.isdigit() else 1


# --- the job --------------------------------------------------------------------------------------

_SCRIPT = r"""#!/bin/bash
#SBATCH --job-name=@@JOB_NAME@@
#SBATCH --partition=@@PARTITION@@
@@ACCOUNT_LINE@@#SBATCH --gres=@@GRES@@
#SBATCH --nodes=1
#SBATCH --ntasks=1
@@EXCLUDE_LINE@@#SBATCH --cpus-per-task=@@CPUS@@
#SBATCH --mem=@@MEM_GB@@G
#SBATCH --time=@@TIME_LIMIT@@
#SBATCH --comment=model=@@SERVED_NAME@@
#SBATCH --output=@@HOME@@/.bioagent/@@SHARED_DIR_NAME@@/serve-%j.log

# The lab's shared reasoning-model server (see aiscientist.gateway.shared_llm). The gateway finds
# this job by its name, reads the port from <jobid>.port and treats <jobid>.ready as "the server
# answers".
set -uo pipefail

export SHARED_DIR="$HOME/.bioagent/@@SHARED_DIR_NAME@@"
export SIF="@@IMAGE@@"
export MODEL="@@MODEL@@"
export SERVED_NAME="@@SERVED_NAME@@"
export HF_HOME="@@HF_HOME@@"
export BIND_ROOT="@@BIND_ROOT@@"

source /etc/profile.d/lmod.sh 2>/dev/null || true
module load @@CONTAINER_MODULE@@ 2>/dev/null || true
@@HOST_MODULE_LINE@@
echo "AiScientist shared LLM: job $SLURM_JOB_ID on $(hostname) at $(date)"
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader

# A dead card takes CUDA down for the whole allocation (on hpc3-gpu-n54-00, for every job on the node), and
# Slurm still hands the node out. NVML (nvidia-smi, torch.cuda.device_count) still counts the healthy cards
# there: only starting CUDA tells.
NGPU=$(python3 -c "import ctypes
c = ctypes.CDLL('libcuda.so.1'); n = ctypes.c_int()
print(n.value if c.cuInit(0) == 0 and c.cuDeviceGetCount(ctypes.byref(n)) == 0 else 0)" 2>/dev/null | tail -1)
echo "preflight: CUDA starts on ${NGPU:-0} devices (need @@NGPU@@)"
if [ "${NGPU:-0}" -ne @@NGPU@@ ]; then
  echo "ABORT: $(hostname) exposes ${NGPU:-0} usable CUDA devices, not @@NGPU@@"
  exit 94
fi

PORT=""
for _try in {1..50}; do
  _cand=$(( (RANDOM % 20000) + 30000 ))
  if ! (exec 3<>/dev/tcp/127.0.0.1/$_cand) 2>/dev/null; then PORT=$_cand; break; fi
done
[ -n "$PORT" ] || { echo "ABORT: no free port"; exit 95; }
export PORT
echo "$PORT" > "$SHARED_DIR/$SLURM_JOB_ID.port"
rm -f "$SHARED_DIR/$SLURM_JOB_ID.ready"
echo "AiScientist shared LLM will bind 0.0.0.0:$PORT on $(hostname)"

# The key stays out of the command line and out of this script: the server reads it from its
# environment.
[ -s "$SHARED_DIR/api.key" ] || { echo "ABORT: $SHARED_DIR/api.key is missing"; exit 96; }
export VLLM_API_KEY="$(cat "$SHARED_DIR/api.key")"
export APPTAINERENV_VLLM_API_KEY=$VLLM_API_KEY

# Mark the job ready once the server answers@@READY_WHAT@@.
(
  for _i in $(seq 1 360); do
    if @@READY_CHECK@@; then
      date +%s > "$SHARED_DIR/$SLURM_JOB_ID.ready"
      echo "AiScientist shared LLM: serving $SERVED_NAME ($(date))"
      break
    fi
    sleep 10
  done
) &

@@SERVE_BLOCK@@
status=$?
rm -f "$SHARED_DIR/$SLURM_JOB_ID.ready"
echo "AiScientist shared LLM: server exited with status $status ($(date))"
exit $status
"""

_VLLM_READY = ('curl -sf --max-time 5 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1', " (/health needs no key)")
_VLLM_SERVE = r"""@@RUNNER@@ <<'AISCIENTIST_SHARED_SERVE'
exec @@CONTAINER_BIN@@ exec --nv --bind "$BIND_ROOT:$BIND_ROOT" \
  --env HF_HOME="$HF_HOME" --env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1@@CONTAINER_ENV@@ \
  "$SIF" \
  vllm serve "$MODEL" --host 0.0.0.0 --port "$PORT" --served-model-name "$SERVED_NAME" \
    @@SERVE_ARGS@@
AISCIENTIST_SHARED_SERVE"""

# Strata's API answers /health before its engine has loaded: ready = /v1/models lists the model as loaded.
_STRATA_READY = ('curl -sf --max-time 5 -H "Authorization: Bearer $VLLM_API_KEY" '
                 '"http://127.0.0.1:$PORT/v1/models" 2>/dev/null | grep -q \'"loaded"\'',
                 " with the model loaded")
_STRATA_SERVE = r"""@@RUNNER@@ <<'AISCIENTIST_SHARED_SERVE'
# The Engram table is read a few rows per word, so it goes to the node's own disk (TMPDIR is the job's and
# goes with it); the model files are read once, into the cards, where they are.
LOCAL="${TMPDIR:-/tmp}/aiscientist-shared-$SLURM_JOB_ID"
mkdir -p "$LOCAL" || exit 97
for f in @@STRATA_SHARDS@@; do ln -sfn "@@GGUF_DIR@@/$f" "$LOCAL/$f"; done
cp "@@GGUF_DIR@@/@@STRATA_ENGRAM@@" "$LOCAL/" || { echo "ABORT: could not copy the Engram table to $LOCAL"; exit 97; }
cd "@@STRATA_ROOT@@/strata-src" || exit 97
# Strata's pack is an index beside the files (no weights copied); its recipe for ISTA's sets is setup.py's.
"@@STRATA_ROOT@@/strata-venv/bin/python" tools/dsv41/pack_gguf.py --model "$LOCAL" --shards @@STRATA_SHARDS@@ \
  --engram "$LOCAL/@@STRATA_ENGRAM@@" --engram-meta "@@GGUF_SHARED_DIR@@/@@STRATA_META@@" \
  --dspark-gguf "@@GGUF_SHARED_DIR@@/@@STRATA_DSPARK@@" --dspark-noise-token 128799 --dspark-markov-rank 256 \
  --out "$LOCAL/pack" < /dev/null || { echo "ABORT: Strata could not pack the model"; exit 97; }
cp data/dsv41/profile-2.00.bin "$LOCAL/pack/expert-profile.bin"
CUDA_LIB="$(dirname "$(dirname "$(command -v nvcc)")")/lib64"
# --dspark auto, never off (off still loads the drafter); the RAM store sized for the job, not the node.
cat > "$LOCAL/strata.json" <<EOF
{"exe": "@@STRATA_ROOT@@/strata-dsv41-build/strata-dsv41",
 "args": ["--pack", "$LOCAL/pack", "--max-context", "@@CONTEXT@@", "--eos", "1", "--vision", "off",
          "--dspark", "auto", "--dspark-conf", "0.65", "--ssd-cache", "4"],
 "cwd": "@@STRATA_ROOT@@/strata-src", "tokenizer": "$LOCAL/pack/tokenizer", "model_name": "$SERVED_NAME",
 "arch": "deepseek41", "log": "$SHARED_DIR/engine-$SLURM_JOB_ID.log", "lib_dirs": ["$CUDA_LIB"],
 "engine_ready_s": 3600, "gpu": [@@GPU_LIST@@], "gpus_asked": true, "parallel": @@PARALLEL@@}
EOF
export STRATA_API_KEY=$VLLM_API_KEY
exec "@@STRATA_ROOT@@/strata-venv/bin/python" serve/server.py --engine strata --config "$LOCAL/strata.json" \
  --host 0.0.0.0 --port "$PORT" --parallel @@PARALLEL@@
AISCIENTIST_SHARED_SERVE"""


def serve_script(s: SharedLLMSettings, hpc: HPCSettings, home: str, exclude: str = "") -> str:
    """The sbatch script of the shared job. ``home`` is the account's absolute HPC3 home: Slurm
    does not expand ``$HOME`` in ``#SBATCH --output``."""
    n = gpu_count(s.gres)
    strata = s.engine == "strata"
    args = s.serve_args
    if n > 1 and "--tensor-parallel-size" not in args:
        args = f"--tensor-parallel-size {n} {args}"
    account = s.account or (hpc.account or "")
    # vLLM writes caches beside its weights on lab storage, so it runs under the lab group. Strata only
    # reads there (the supplementary group is enough) and writes to $HOME and the node; and `sg` is
    # setuid, which strips the LD_LIBRARY_PATH its Python and CUDA libraries come from.
    group = hpc.lab_data_group if not strata and any(
        p.startswith(hpc.lab_storage) for p in (s.image, hpc.hf_home)) else ""
    bind_root = hpc.shared_root if hpc.hf_home.startswith(hpc.shared_root) else hpc.hf_home
    env = "".join(f" --env {kv}" for kv in s.container_env.split())
    ready, ready_what = _STRATA_READY if strata else _VLLM_READY
    shards = " ".join(f"{s.model}-{i:05d}-of-{_STRATA_SHARDS + 1:05d}.gguf" for i in range(1, _STRATA_SHARDS + 1))
    values = {
        # first: these bring placeholders of their own
        "SERVE_BLOCK": _STRATA_SERVE if strata else _VLLM_SERVE,
        "READY_CHECK": ready,
        "READY_WHAT": ready_what,
        "HOST_MODULE_LINE": f"module load {s.host_modules} 2>/dev/null || true\n" if strata and s.host_modules else "",
        "STRATA_ROOT": s.strata_root.rstrip("/"),
        "GGUF_DIR": s.gguf_dir.rstrip("/"),
        "GGUF_SHARED_DIR": s.gguf_shared_dir.rstrip("/"),
        "STRATA_SHARDS": shards,
        "STRATA_ENGRAM": _STRATA_ENGRAM,
        "STRATA_META": _STRATA_META,
        "STRATA_DSPARK": _STRATA_DSPARK,
        "CONTEXT": str(s.context),
        "PARALLEL": str(max(1, min(8, s.parallel))),
        "GPU_LIST": ", ".join(str(i) for i in range(n)),
        "JOB_NAME": job_name(),
        "SHARED_DIR_NAME": remote_dir_name(),
        "PARTITION": s.partition,
        "ACCOUNT_LINE": f"#SBATCH --account={account}\n" if account else "",
        "GRES": s.gres,
        "EXCLUDE_LINE": f"#SBATCH --exclude={exclude or s.exclude}\n" if (exclude or s.exclude) else "",
        "CPUS": str(s.cpus),
        "MEM_GB": str(s.mem_gb),
        "TIME_LIMIT": s.time_limit,
        "SERVED_NAME": s.served_name,
        "HOME": home.rstrip("/"),
        "IMAGE": s.image,
        "MODEL": s.model,
        "HF_HOME": hpc.hf_home,
        "BIND_ROOT": bind_root,
        "CONTAINER_MODULE": hpc.container_module,
        "CONTAINER_BIN": hpc.container_bin,
        "NGPU": str(n),
        "RUNNER": f"sg {group} -c 'bash -s'" if group else "bash -s",
        "CONTAINER_ENV": env,
        "SERVE_ARGS": args,
    }
    out = _SCRIPT
    for key, val in values.items():
        out = out.replace(f"@@{key}@@", val)
    return out


@dataclass(frozen=True)
class SharedJob:
    job_id: str
    state: str                      # squeue %t: R, PD, CF
    node: str = ""
    time_left_s: int | None = None  # None while pending or when Slurm does not say
    reason: str = ""
    port: int = 0
    ready: bool = False
    comment: str = ""               # squeue %k: the script's "model=<served name>"

    @property
    def running(self) -> bool:
        return self.state == "R"


def list_command() -> str:
    """The keeper's one listing command: the account's home, this deployment's shared jobs, and
    their port and readiness files."""
    return (
        f'D="{remote_dir()}"; echo "HOME|$HOME"; '
        f"squeue --me --name={job_name()} --states=R,PD,CF --noheader --format='JOB|%i|%t|%N|%L|%r|%k'; "
        'echo "RC|$?"; '
        'for f in "$D"/*.port; do [ -f "$f" ] || continue; j=$(basename "$f" .port); '
        'echo "PORT|$j|$(cat "$f" 2>/dev/null)|$([ -f "$D/$j.ready" ] && echo ready)"; done'
    )


def parse_listing(text: str) -> tuple[str, bool, list[SharedJob]]:
    """``(home, squeue_ok, jobs)`` from the keeper's one listing command. ``squeue_ok`` False means
    Slurm did not answer: the job list is then unknown, which is not the same as empty."""
    home, ok = "", False
    rows: dict[str, SharedJob] = {}
    ports: dict[str, tuple[int, bool]] = {}
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.strip().split("|")]
        if parts[0] == "HOME" and len(parts) > 1:
            home = parts[1]
        elif parts[0] == "RC" and len(parts) > 1:
            ok = parts[1] == "0"
        elif parts[0] == "JOB" and len(parts) >= 5 and parts[1].isdigit():
            left = slurm_time_to_seconds(parts[4], default=-1) if parts[2] == "R" else -1
            rows[parts[1]] = SharedJob(job_id=parts[1], state=parts[2], node=parts[3],
                                       time_left_s=left if left >= 0 else None,
                                       reason=parts[5] if len(parts) > 5 else "",
                                       comment=parts[6] if len(parts) > 6 else "")
        elif parts[0] == "PORT" and len(parts) >= 3 and parts[2].isdigit():
            ports[parts[1]] = (int(parts[2]), len(parts) > 3 and parts[3] == "ready")
    jobs = [replace(j, port=ports.get(j.job_id, (0, False))[0],
                    ready=ports.get(j.job_id, (0, False))[1]) for j in rows.values()]
    return home, ok, sorted(jobs, key=lambda j: int(j.job_id))


# --- what a caller gets ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Binding:
    """Where a call goes while the shared server answers."""
    port: int
    model: str
    api_key: str = field(repr=False, default="")
    effort: str | None = None
    think_kwargs: dict | None = None
    job_id: str = ""

    @property
    def label(self) -> str:
        return f"{self.model} @ shared GPU"


def _probe(port: int, api_key: str, served_name: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Does the server behind the local port answer, and does it serve the model we expect?"""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models",
                                 headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback tunnel
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} from /v1/models"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"no answer from /v1/models ({type(exc).__name__})"
    names = [str(m.get("id") or "") for m in (body or {}).get("data") or [] if isinstance(m, dict)]
    if served_name not in names:
        return False, f"the server serves {names or 'nothing'}, not {served_name}"
    return True, ""


def resolve_identity(s: SharedLLMSettings, hpc: HPCSettings) -> tuple[str, str]:
    """``(hpc_user, private_key_path)`` the keeper logs in with. An unattended login cannot type
    a passphrase or answer Duo, so only an unencrypted key qualifies."""
    if s.key_path:
        if not s.hpc_user:
            raise GatewayError("AISCIENTIST_SHARED_LLM_KEY_PATH needs AISCIENTIST_SHARED_LLM_HPC_USER.",
                               stage="shared_llm")
        return s.hpc_user, os.path.expanduser(s.key_path)
    if not s.owner:
        raise GatewayError("The shared model server has no HPC3 identity: set "
                           "AISCIENTIST_SHARED_LLM_OWNER to an account with a saved SSH key.",
                           stage="shared_llm")
    from . import ssh_credentials
    rows = [r for r in ssh_credentials.list_credentials(s.owner)
            if r.get("host") == hpc.host and not r.get("encrypted")
            and (not s.ssh_credential_id or r.get("id") == s.ssh_credential_id)
            and (not s.hpc_user or r.get("hpc_user") == s.hpc_user)]
    if not rows:
        raise GatewayError(f"Account “{s.owner}” has no saved, passphrase-free SSH key for "
                           f"{hpc.host}; the shared model server cannot log in.", stage="shared_llm")
    full = ssh_credentials.get_credential(s.owner, rows[0]["id"]) or {}
    return rows[0]["hpc_user"], str(full.get("key_path") or "")


# --- the keeper -----------------------------------------------------------------------------------

LogFn = Callable[[str], None]


class SharedLLM:
    """Keeps the shared job alive and the local port pointed at it. One instance per gateway
    process; :meth:`step` is one reconciliation pass and is what the tests drive."""

    _FIRST_RETRY_S = 600          # after a job that never served: wait this, then double
    _MAX_RETRY_S = 6 * 3600

    def __init__(self, settings: SharedLLMSettings, hpc: HPCSettings,
                 executor_factory: Callable[[], RemoteExecutor], *,
                 clock: Callable[[], float] = time.time,
                 probe: Callable[[int, str, str], tuple[bool, str]] = _probe,
                 log: LogFn = lambda line: print(f"[shared-llm] {line}", flush=True)) -> None:
        self.settings = settings
        self.hpc = hpc
        self._factory = executor_factory
        self._clock = clock
        self._probe = probe
        self._log = log
        self._lock = threading.RLock()
        self._executor: RemoteExecutor | None = None
        self._connect_failures = 0
        self._next_connect_at = 0.0
        self._home = ""
        self._api_key = ""
        self._local_port = 0
        self._target: tuple[str, int] | None = None     # read by the tunnel, per connection
        self._active: SharedJob | None = None
        self._switched_at = 0.0
        self._watching = ""            # the job we submitted last, until it serves or disappears
        self._failed_starts = 0
        self._last_submit_at = 0.0
        self._excluded: list[str] = []
        self._state = "starting_up"
        self._detail = ""
        self._pending: SharedJob | None = None
        self._updated_at = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="shared-llm", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop keeping. The Slurm job is left running: it is the lab's, not this process's."""
        self._stop.set()
        self._wake.set()
        ex, self._executor = self._executor, None
        if ex is not None:
            try:
                ex.close()
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except GatewayError as exc:
                self._set("error", exc.message)
            except Exception as exc:  # noqa: BLE001 - the keeper must outlive any one bad pass
                self._set("error", f"{type(exc).__name__}: {exc}")
            self._wake.wait(self.settings.poll_s)
            self._wake.clear()

    # -- one pass ----------------------------------------------------------------------------------

    def step(self) -> None:
        ex = self._connect()
        if ex is None:
            return
        home, squeue_ok, jobs = parse_listing(ex.exec(list_command(), timeout=90.0).stdout)
        self._home = home or self._home
        now = self._clock()
        if not squeue_ok:
            # Slurm did not answer. Nothing is known about the queue, so nothing is submitted or
            # cancelled; a job we were already serving from may still answer.
            self._health(now, note="Slurm did not answer (squeue failed)")
            return

        # A job submitted for another model (the settings changed: a new model or engine) is not ours to
        # wait for or serve from. A queued one goes now; a running one once the new job answers.
        want = f"model={self.settings.served_name}"
        other = [j for j in jobs if j.comment and j.comment != want]
        if [j for j in other if not j.running]:
            gone = [j.job_id for j in other if not j.running]
            ex.exec("scancel " + " ".join(gone))
            self._log(f"cancelled queued job(s) {', '.join(gone)}: submitted for another model")
            if self._watching in gone:
                self._watching = ""
        stale = [j.job_id for j in other if j.running]
        jobs = [j for j in jobs if j not in other]

        running = [j for j in jobs if j.running]
        pending = [j for j in jobs if not j.running]
        ids = {j.job_id for j in jobs}

        # Duplicates in the queue (a manual submit beside ours): the oldest has the most queue age.
        if len(pending) > 1:
            extra = [j.job_id for j in pending[1:]]
            ex.exec("scancel " + " ".join(extra))
            self._log(f"cancelled duplicate queued job(s) {', '.join(extra)}")
            pending = pending[:1]
            if self._watching in extra:
                self._watching = pending[0].job_id
            ids -= set(extra)

        # The job we submitted and were waiting on.
        if self._watching and self._watching not in ids:
            self._failed_starts += 1
            self._note_failed_start(ex, self._watching)
            self._watching = ""

        # Serve from the running job that answers and has the longest left.
        ready = [j for j in running if j.ready and j.port]
        target = max(ready, key=lambda j: j.time_left_s or 0) if ready else None
        if target and (self._active is None or target.job_id != self._active.job_id):
            if self._active is not None:
                self._log(f"moving from job {self._active.job_id} to {target.job_id} on {target.node}")
            self._switched_at = now
        self._active = target
        self._target = (target.node, target.port) if target else None
        if target and target.job_id == self._watching:
            self._watching, self._failed_starts = "", 0

        up = self._health(now, running=running, pending=pending)

        # Older running jobs, once the one we serve from answers and their calls have had time to end.
        if up and self._active is not None:
            mine = self._active.time_left_s or 0
            old = [j.job_id for j in running if j.job_id != self._active.job_id
                   and (j.time_left_s or 0) < mine]
            if old and now - self._switched_at >= self.settings.drain_s:
                ex.exec("scancel " + " ".join(old))
                self._log(f"cancelled superseded job(s) {', '.join(old)}")
            if stale:
                ex.exec("scancel " + " ".join(stale))
                self._log(f"cancelled job(s) {', '.join(stale)}: they serve another model")

        # Submit: when there is no job at all, or ahead of the running job's limit.
        if not pending:
            if not running:
                self._submit(ex, now, "no job is running or queued")
            else:
                left = max((j.time_left_s for j in running if j.time_left_s is not None), default=None)
                if left is not None and left <= self.settings.renew_lead_s:
                    self._submit(ex, now, f"the running job ends in {left // 3600} h")

    # -- pieces ------------------------------------------------------------------------------------

    def _connect(self) -> RemoteExecutor | None:
        if self._executor is not None:
            return self._executor
        now = self._clock()
        if now < self._next_connect_at:
            return None
        ex: RemoteExecutor | None = None
        try:
            ex = self._factory()
            key = ex.exec(
                f'umask 077; D="{remote_dir()}"; mkdir -p "$D"; [ -s "$D/api.key" ] || '
                "printf 'sk-aisci-%s' \"$(head -c 48 /dev/urandom | base64 | tr -d '/+=\\n' | head -c 40)\" "
                '> "$D/api.key"; cat "$D/api.key"').out
            if not key:
                raise GatewayError("Could not read the shared server's API key on the cluster.",
                                   stage="shared_llm")
            opener = getattr(ex, "open_tunnel_to", None)
            if opener is None:
                raise GatewayError("This connection cannot follow a moving server.", stage="shared_llm")
            port = opener(lambda: self._target, local_port=self.settings.local_port,
                          label="the shared model server")
        except GatewayError as exc:
            if ex is not None:
                try:
                    ex.close()
                except Exception:  # noqa: BLE001
                    pass
            # Back off: a login that fails every minute looks like an attack to the cluster.
            self._connect_failures += 1
            self._next_connect_at = now + min(60 * 2 ** self._connect_failures, 1800)
            self._set("no_identity" if exc.stage == "shared_llm" else "ssh_error", exc.message)
            return None
        with self._lock:
            self._executor, self._api_key, self._local_port = ex, key, port
        self._connect_failures = 0
        self._log(f"keeping job '{job_name()}' as {ex.username}@{ex.host}; local port {port}")
        return ex

    def _health(self, now: float, *, running: list[SharedJob] | None = None,
                pending: list[SharedJob] | None = None, note: str = "") -> bool:
        up, why = False, ""
        if self._active is not None:
            up, why = self._probe(self._local_port, self._api_key, self.settings.served_name)
        if up:
            state = "up"
        elif self._active is not None:
            state = "unreachable"
        elif running:
            state = "loading"
        elif pending:
            state = "queued"
        elif running is None:
            state = self._state if self._state in ("up", "unreachable") else "unknown"
        else:
            state = "waiting"
        with self._lock:
            if pending is not None:
                self._pending = pending[0] if pending else None
            self._set(state, note or why or (self._detail if state == "waiting" else ""), now=now)
        return up

    def _submit(self, ex: RemoteExecutor, now: float, why: str) -> None:
        if self._failed_starts:
            wait = min(self._FIRST_RETRY_S * 2 ** (self._failed_starts - 1), self._MAX_RETRY_S)
            if now - self._last_submit_at < wait:
                return
        if not self._home:
            return
        exclude = ",".join(dict.fromkeys(
            [n for n in (self.settings.exclude or "").split(",") if n] + self._excluded))
        script = serve_script(self.settings, self.hpc, self._home, exclude=exclude)
        d = remote_dir()
        write = ex.exec(f'mkdir -p "{d}" && cat > "{d}/serve.sbatch" '
                        f"<<'AISCIENTIST_SHARED_EOF'\n{script}AISCIENTIST_SHARED_EOF")
        # Port and readiness files of jobs that are gone, and logs older than two weeks.
        ex.exec(f'D="{d}"; for f in "$D"/*.port "$D"/*.ready; do [ -f "$f" ] || continue; '
                'j=$(basename "${f%.*}"); squeue --noheader --jobs="$j" 2>/dev/null | grep -q . '
                '|| rm -f "$f"; done; find "$D" -name "serve-*.log" -mtime +14 -delete 2>/dev/null; true')
        res = ex.exec(f'cd "$HOME" && sbatch --parsable "{d}/serve.sbatch"') if write.ok else write
        job_id = (res.out.split(";")[0] if res.ok else "").strip()
        self._last_submit_at = now
        if not job_id.isdigit():
            self._failed_starts += 1
            self._set(self._state, "sbatch refused the job: "
                      + (res.stderr or res.stdout or "").strip()[:200], now=now)
            self._log(f"sbatch refused the job: {(res.stderr or res.stdout or '').strip()[:200]}")
            return
        self._watching = job_id
        self._log(f"submitted job {job_id} ({why})")

    def _note_failed_start(self, ex: RemoteExecutor, job_id: str) -> None:
        """A job we submitted left the queue without ever serving. Keep why, and stop offering
        Slurm a node whose cards the job could not all see."""
        tail = ex.exec(f'tail -n 8 "{remote_dir()}/serve-{job_id}.log" 2>/dev/null').out
        m = re.search(r"ABORT: (\S+) exposes", tail)
        node = m.group(1).split(".")[0] if m else ""
        if node and node not in self._excluded:
            self._excluded.append(node)
        last = tail.splitlines()[-1] if tail else "no log"
        self._detail = f"job {job_id} ended before it served: {last[:200]}"
        self._log(self._detail)

    def _set(self, state: str, detail: str = "", *, now: float | None = None) -> None:
        with self._lock:
            if state != self._state:
                self._log(f"{self._state} -> {state}" + (f" ({detail})" if detail else ""))
            self._state, self._detail = state, detail
            self._updated_at = now if now is not None else self._clock()

    # -- what callers read -------------------------------------------------------------------------

    def binding(self) -> Binding | None:
        with self._lock:
            if self._state != "up" or self._active is None or not self._local_port:
                return None
            s = self.settings
            return Binding(port=self._local_port, model=s.served_name, api_key=self._api_key,
                           effort=s.reasoning_effort or None, think_kwargs=dict(s.think_kwargs) or None,
                           job_id=self._active.job_id)

    def note_failure(self, reason: str) -> None:
        """A call to the shared server failed. Stop handing it out until the next pass has looked."""
        with self._lock:
            if self._state == "up":
                self._set("unreachable", reason)
        self._wake.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            a, p = self._active, self._pending
            return {
                "enabled": True,
                "state": self._state,
                "detail": self._detail,
                "model": self.settings.served_name,
                "gpus": self.settings.gres,
                "job_id": a.job_id if a else "",
                "node": a.node if a else "",
                "expires_at": (self._updated_at + a.time_left_s) if a and a.time_left_s is not None else None,
                "queued_job_id": p.job_id if p else "",
                "queued_reason": p.reason if p else "",
                "local_port": self._local_port,
                "updated_at": self._updated_at,
            }


# --- the process-wide instance ----------------------------------------------------------------------

_MANAGER: SharedLLM | None = None


def _ssh_factory(s: SharedLLMSettings, hpc: HPCSettings) -> Callable[[], RemoteExecutor]:
    def make() -> RemoteExecutor:
        from .ssh_gateway import SSHExecutor
        user, key_path = resolve_identity(s, hpc)
        return SSHExecutor(host=hpc.host, username=user, port=hpc.ssh_port, key_path=key_path)
    return make


def start(hpc: HPCSettings | None = None) -> SharedLLM | None:
    """Start the keeper when ``AISCIENTIST_SHARED_LLM=1``. Idempotent; None when it is off."""
    global _MANAGER
    if _MANAGER is not None:
        return _MANAGER
    hpc = hpc or HPCSettings.from_env()
    s = SharedLLMSettings.from_env(hpc)
    if not s.enabled:
        return None
    _MANAGER = SharedLLM(s, hpc, _ssh_factory(s, hpc))
    _MANAGER.start()
    return _MANAGER


def stop() -> None:
    global _MANAGER
    manager, _MANAGER = _MANAGER, None
    if manager is not None:
        manager.stop()


# --- the shared server as a session's own endpoint ----------------------------------------------------
#
# A session chooses the shared server the way it chooses one of its saved API keys
# (``conn.llm_choice = {"credential_id": ENDPOINT_ID}``): it then holds no GPU of its own and EVERY
# call — the Scientist's tool loop and the fast chat included — goes to the shared model. The address
# and the key stay in this process; the console only ever sees ``endpoint_public()``.

ENDPOINT_ID = "lab-shared"


def endpoint_binding() -> Binding | None:
    """The shared server for a session that chose it as its endpoint, while it answers; else None."""
    manager = _MANAGER
    return manager.binding() if manager is not None else None


def served_window(bound: Binding, timeout: float = 5.0) -> int | None:
    """The context window the running server reports on /v1/models (vLLM's ``max_model_len``,
    Strata's ``meta.n_ctx``), or None when it does not say."""
    req = urllib.request.Request(f"http://127.0.0.1:{bound.port}/v1/models",
                                 headers={"Authorization": f"Bearer {bound.api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback tunnel
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    for m in (body or {}).get("data") or []:
        if not isinstance(m, dict) or m.get("id") != bound.model:
            continue
        meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
        for n in (m.get("max_model_len"), meta.get("n_ctx")):
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                return n
    return None


def context_window() -> int:
    """The shared model's context window, so a session on it trims the Scientist's context to what
    the server takes: what the RUNNING server reports, because a job submitted with older flags keeps
    its window until it is renewed; else the configured one (vLLM's ``--max-model-len``, Strata's
    ``--max-context``)."""
    bound = endpoint_binding()
    reported = served_window(bound) if bound is not None else None
    if reported:
        return reported
    manager = _MANAGER
    if manager is not None and manager.settings.engine == "strata":
        return manager.settings.context
    args = manager.settings.serve_args if manager is not None else _DSV4_SERVE_ARGS
    m = re.search(r"--max-model-len[ =](\d+)", args or "")
    return int(m.group(1)) if m else 262144


def endpoint_public() -> dict[str, Any] | None:
    """The shared server as the console lists it among the LLM endpoints; None when it is not kept.
    No address, no key, no job: the model, and whether it answers now."""
    st = status()
    if not st.get("enabled"):
        return None
    model = str(st.get("model") or "")
    return {"id": ENDPOINT_ID, "label": "Shared model on HPC3", "model": model, "lab_model": model,
            "state": st.get("state") or "", "up": endpoint_binding() is not None,
            "expires_at": st.get("expires_at")}


def note_failure(reason: str) -> None:
    if _MANAGER is not None:
        _MANAGER.note_failure(reason)


def status() -> dict[str, Any]:
    return _MANAGER.status() if _MANAGER is not None else {"enabled": False, "state": "off"}
