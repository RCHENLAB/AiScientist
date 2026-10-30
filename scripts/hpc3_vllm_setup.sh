#!/usr/bin/env bash
# One-time vLLM + Qwen3.8-27B-INT4 deploy on HPC3, following RCIC's container rules
# (rcic.uci.edu/software/user-installed.html): Apptainer (module apptainer/1.4.5), pulled
# on an INTERACTIVE COMPUTE NODE (never a login node), with the cache OFF $HOME, into
# the SHARED lab DFS area (not $HOME). The serve command printed at the end is
# byte-identical to what gateway/gpu.py generates.
#
#   ssh hpc3.rcic.uci.edu                      # (Duo)
#   newgrp ruic20_hpc                          # so you can write under the lab DFS
#   srun -c 4 -p free --time=2:00:00 --pty /bin/bash -i   # << RCIC: build/pull on a compute node
#   bash scripts/hpc3_vllm_setup.sh
set -uo pipefail
# Legacy BIOAGENT_* names still work (AISCIENTIST_* wins when both are set) — same rule as the
# Python side (aiscientist.core.config.apply_brand_env_aliases).
for _old in $(compgen -v BIOAGENT_ || true); do _new="AISCIENTIST_${_old#BIOAGENT_}"; [ -n "${!_new+x}" ] || export "$_new=${!_old}"; done

BASE="${AISCIENTIST_LAB_BASE:-/dfs3b/ruic20_lab/software/AiScientist}"
IMAGE="${AISCIENTIST_VLLM_IMAGE:-$BASE/containers/vllm-0.28.0.sif}"
HF="$BASE/models/hf"
MODEL="${AISCIENTIST_VLLM_MODEL:-RedHatAI/Qwen3.8-27B-INT4}"
VLLM_IMG="${VLLM_DOCKER:-docker://vllm/vllm-openai:v0.28.0}"
CT_MODULE="${AISCIENTIST_CONTAINER_MODULE:-apptainer/1.4.5}"
CT_BIN="${AISCIENTIST_CONTAINER_BIN:-apptainer}"

log() { echo "[hpc3-vllm] $*"; }

# RCIC: "If you do builds on login nodes you will have problems." Refuse politely.
if [[ "$(hostname)" == *login* ]]; then
  echo "ERROR: you are on a LOGIN node ($(hostname))."
  echo "RCIC requires container pulls on a COMPUTE node. Claim one first:"
  echo "    srun -c 4 -p free --time=2:00:00 --pty /bin/bash -i"
  echo "then re-run this script."
  exit 1
fi

log "lab base: $BASE   (container module: $CT_MODULE)"
mkdir -p "$BASE/containers" "$HF" || { echo "cannot mkdir under $BASE — run 'newgrp ruic20_hpc' first"; exit 1; }

source /etc/profile.d/lmod.sh 2>/dev/null || true
module load "$CT_MODULE" 2>/dev/null || true
command -v "$CT_BIN" >/dev/null 2>&1 || {
  echo "$CT_BIN not found — run 'module avail apptainer' and load it, then re-run"; exit 1; }
log "runtime: $(command -v "$CT_BIN")  ($("$CT_BIN" --version 2>/dev/null))"

# Keep the pull cache + tmp OFF $HOME (the layers are big; $HOME has a ~50GB quota). The
# SINGULARITY_* names cover a run with AISCIENTIST_CONTAINER_BIN=singularity.
export APPTAINER_CACHEDIR="${TMPDIR:-/tmp}/apptainer-cache-$USER"
export APPTAINER_TMPDIR="${TMPDIR:-/tmp}/apptainer-tmp-$USER"
export SINGULARITY_CACHEDIR="$APPTAINER_CACHEDIR" SINGULARITY_TMPDIR="$APPTAINER_TMPDIR"
mkdir -p "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"
log "cache dir (off \$HOME): $APPTAINER_CACHEDIR"

# 1) container image
if [ -f "$IMAGE" ]; then
  log "image present: $IMAGE (skip pull)"
else
  log "pulling vLLM image -> $IMAGE"
  "$CT_BIN" pull "$IMAGE" "$VLLM_IMG" || { echo "image pull failed"; exit 1; }
fi

# 2) model weights into the shared HF cache (off $HOME), via the IMAGE's own `hf`
#    CLI — no host pip/python needed, and `huggingface-cli` is deprecated in newer
#    huggingface_hub (use `hf download`).
export HF_HOME="$HF"
log "downloading $MODEL into $HF (~16GB; resumable) ..."
"$CT_BIN" exec --env HF_HOME="$HF" "$IMAGE" hf download "$MODEL" \
  || { echo "weight download failed"; exit 1; }

log "DONE. Smoke-test on a GPU node (the free partition above has no GPU):"
cat <<EOF

  srun --partition=gpu --account=ruic20_lab_gpu --gres=gpu:A100:1 \\
       --cpus-per-task=8 --mem=32G --time=00:30:00 --pty /bin/bash -i
  module load $CT_MODULE
  export HF_HOME=$HF
  $CT_BIN exec --nv -B $HF:$HF --env HF_HOME=$HF --env HF_HUB_OFFLINE=1 \\
    $IMAGE \\
    vllm serve $MODEL --host 0.0.0.0 --port 8000 \\
      --max-model-len 262144 --gpu-memory-utilization 0.92 \\
      --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 &
  # then: curl -s 127.0.0.1:8000/v1/models
EOF
