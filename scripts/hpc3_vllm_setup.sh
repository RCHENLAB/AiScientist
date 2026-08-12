#!/usr/bin/env bash
# One-time vLLM + Qwen3.6-AWQ deploy on HPC3, following RCIC's container rules
# (rcic.uci.edu/software/user-installed.html): Singularity (NOT Apptainer), pulled
# on an INTERACTIVE COMPUTE NODE (never a login node), with the cache OFF $HOME, into
# the SHARED lab DFS area (not $HOME). The serve command printed at the end is
# byte-identical to what gateway/gpu.py generates.
#
#   ssh hpc3.rcic.uci.edu                      # (Duo)
#   newgrp ruic20_hpc                          # so you can write under the lab DFS
#   srun -c 4 -p free --time=2:00:00 --pty /bin/bash -i   # << RCIC: build/pull on a compute node
#   bash scripts/hpc3_vllm_setup.sh
set -uo pipefail

BASE="${BIOAGENT_LAB_BASE:-/dfs3b/ruic20_lab/software/bioagent}"
IMAGE="$BASE/containers/vllm.sif"
HF="$BASE/hf"
MODEL="${BIOAGENT_VLLM_MODEL:-QuantTrio/Qwen3.6-35B-A3B-AWQ}"
VLLM_IMG="${VLLM_DOCKER:-docker://vllm/vllm-openai:latest}"
SING_MODULE="${BIOAGENT_CONTAINER_MODULE:-singularity/3.11.3}"

log() { echo "[hpc3-vllm] $*"; }

# RCIC: "If you do builds on login nodes you will have problems." Refuse politely.
if [[ "$(hostname)" == *login* ]]; then
  echo "ERROR: you are on a LOGIN node ($(hostname))."
  echo "RCIC requires container pulls on a COMPUTE node. Claim one first:"
  echo "    srun -c 4 -p free --time=2:00:00 --pty /bin/bash -i"
  echo "then re-run this script."
  exit 1
fi

log "lab base: $BASE   (singularity module: $SING_MODULE)"
mkdir -p "$BASE/containers" "$HF" || { echo "cannot mkdir under $BASE — run 'newgrp ruic20_hpc' first"; exit 1; }

# Singularity (RCIC module), NOT apptainer.
source /etc/profile.d/lmod.sh 2>/dev/null || true
module load "$SING_MODULE" 2>/dev/null || module load singularity 2>/dev/null || true
command -v singularity >/dev/null 2>&1 || {
  echo "singularity not found — run 'module avail singularity' and load it, then re-run"; exit 1; }
log "singularity: $(command -v singularity)  ($(singularity --version 2>/dev/null))"

# Keep the pull cache + tmp OFF $HOME (the layers are big; $HOME has a ~50GB quota).
export SINGULARITY_CACHEDIR="${TMPDIR:-/tmp}/singularity-cache-$USER"
export SINGULARITY_TMPDIR="${TMPDIR:-/tmp}/singularity-tmp-$USER"
mkdir -p "$SINGULARITY_CACHEDIR" "$SINGULARITY_TMPDIR"
log "cache dir (off \$HOME): $SINGULARITY_CACHEDIR"

# 1) container image
if [ -f "$IMAGE" ]; then
  log "image present: $IMAGE (skip pull)"
else
  log "pulling vLLM image -> $IMAGE"
  singularity pull "$IMAGE" "$VLLM_IMG" || { echo "image pull failed"; exit 1; }
fi

# 2) model weights into the shared HF cache (off $HOME), via the IMAGE's own `hf`
#    CLI — no host pip/python needed, and `huggingface-cli` is deprecated in newer
#    huggingface_hub (use `hf download`).
export HF_HOME="$HF"
log "downloading $MODEL into $HF (~24GB; resumable) ..."
singularity exec --env HF_HOME="$HF" "$IMAGE" hf download "$MODEL" \
  || { echo "weight download failed"; exit 1; }

log "DONE. Smoke-test on a GPU node (the free partition above has no GPU):"
cat <<EOF

  srun --partition=gpu --account=ruic20_lab_gpu --gres=gpu:A100:1 \\
       --cpus-per-task=8 --mem=32G --time=00:30:00 --pty /bin/bash -i
  module load $SING_MODULE
  export HF_HOME=$HF
  singularity exec --nv -B $HF:$HF --env HF_HOME=$HF --env HF_HUB_OFFLINE=1 \\
    $IMAGE \\
    vllm serve $MODEL --host 0.0.0.0 --port 8000 \\
      --quantization awq_marlin --max-model-len 32768 --gpu-memory-utilization 0.92 \\
      --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 &
  # then: curl -s 127.0.0.1:8000/v1/models
EOF
