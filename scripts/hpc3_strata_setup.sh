#!/usr/bin/env bash
# One-time setup of the shared model's "strata" engine on HPC3 (gateway/shared_llm.py,
# AISCIENTIST_SHARED_LLM_ENGINE=strata): DeepSeek-V4.1-Flash-1017 at 2 bits (ISTA-DASLab's GSQ-RCO IQ2_XXS
# GGUF) served by Strata's strata-dsv41 engine and serve/server.py. Builds for A100 (sm_80) and RTX PRO 6000
# (sm_120). RCIC: downloads and builds run on a COMPUTE node, never a login node.
#
#   ssh hpc3.rcic.uci.edu
#   srun -c 16 --mem=64G -p standard --time=4:00:00 --pty /bin/bash -i
#   bash scripts/hpc3_strata_setup.sh            # about 30 min: 257 GB of files, two builds
#
# Writes <base>/engines/{strata-src,llama.cpp-src,llama.cpp-strata-pin,strata-dsv41-build,strata-venv} and
# <base>/models/gguf/{DeepSeek-V4.1-Flash-1017-GSQ-RCO,DeepSeek-V4.1-Flash-shared}; skips what is there.
set -uo pipefail
for _old in $(compgen -v BIOAGENT_ || true); do _new="AISCIENTIST_${_old#BIOAGENT_}"; [ -n "${!_new+x}" ] || export "$_new=${!_old}"; done

BASE="${AISCIENTIST_LAB_BASE:-/dfs3b/ruic20_lab/software/AiScientist}"
ENG="$BASE/engines"
GGUF="$BASE/models/gguf/DeepSeek-V4.1-Flash-1017-GSQ-RCO"
SHARED="$BASE/models/gguf/DeepSeek-V4.1-Flash-shared"
HF_SIF="${AISCIENTIST_VLLM_IMAGE:-$BASE/containers/vllm-0.32.0.sif}"     # any image with the `hf` CLI
ARCH="${STRATA_CUDA_ARCH:-80-real;120-real}"
STRATA_COMMIT="146df3c468739147f520ac3816de806f71232571"                   # 2026-11-07, engine 0.1.58
ISTA_REV="65d53bc8be65123e255ff02f3ded958b6dbf54fd"                        # the six-shard layout (2026-10-27)
UNSLOTH_REV="8a6ec9cf118e785a362ea94ee123c61c70553be6"                     # Strata's pin
SET=DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS
ENGRAM=DeepSeek-V4.1-Flash-Engram-IQ4_NL.gguf
META=UD-Q8_K_XL/DeepSeek-V4.1-Flash-1017-UD-Q8_K_XL-00001-of-00009.gguf
DSPARK=dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf

log() { echo "[hpc3-strata] $*"; }
if [[ "$(hostname)" == *login* ]]; then
  echo "ERROR: you are on a LOGIN node ($(hostname)); RCIC allows no downloads or builds there."
  echo "    srun -c 16 --mem=64G -p standard --time=4:00:00 --pty /bin/bash -i"
  exit 1
fi
source /etc/profile.d/lmod.sh 2>/dev/null || true
module load gcc/11.2.0 cuda/13.0.1 cmake/4.2.3 python/3.10.2 apptainer/1.4.5 || { echo "module load failed"; exit 1; }
mkdir -p "$ENG" "$GGUF" "$SHARED" || { echo "cannot write under $BASE"; exit 1; }

# 1. Strata at the pinned commit; ggml from the llama.cpp commit Strata pins (not a submodule).
if [ ! -d "$ENG/strata-src/.git" ]; then git clone https://github.com/Niko1221/Strata "$ENG/strata-src" || exit 1; fi
git -C "$ENG/strata-src" fetch -q origin && git -C "$ENG/strata-src" checkout -q "$STRATA_COMMIT" || exit 1
PIN=$(grep -oE '^LLAMA_CPP_COMMIT = "[0-9a-f]{40}"' "$ENG/strata-src/setup.py" | grep -oE '[0-9a-f]{40}')
[ -n "$PIN" ] || { echo "no LLAMA_CPP_COMMIT in Strata's setup.py"; exit 1; }
if [ ! -d "$ENG/llama.cpp-src/.git" ]; then git clone https://github.com/ggml-org/llama.cpp "$ENG/llama.cpp-src" || exit 1; fi
if [ ! -d "$ENG/llama.cpp-strata-pin/ggml" ]; then
  git -C "$ENG/llama.cpp-src" fetch -q origin && git -C "$ENG/llama.cpp-src" worktree add --detach "$ENG/llama.cpp-strata-pin" "$PIN" || exit 1
fi
log "Strata $(git -C "$ENG/strata-src" log -1 --format=%h), ggml from llama.cpp ${PIN:0:9}"

# 2. The engine, for A100 and RTX PRO 6000.
if [ ! -x "$ENG/strata-dsv41-build/strata-dsv41" ]; then
  CUDA=$(dirname "$(dirname "$(command -v nvcc)")")
  SRC="$ENG/strata-src" B="$ENG/strata-dsv41-build" CUDA="$CUDA" GG="$ENG/llama.cpp-strata-pin" ARCH="$ARCH" JOBS=16 \
    bash "$ENG/strata-src/packaging/build_dsv41_cuda.sh" > "$ENG/strata-dsv41-build.log" 2>&1 \
    || { echo "build failed: $ENG/strata-dsv41-build.log"; tail -20 "$ENG/strata-dsv41-build.log"; exit 1; }
fi
log "engine: $ENG/strata-dsv41-build/strata-dsv41"

# 3. The server's Python environment (Strata's pinned requirements; it needs Python 3.10+).
if [ ! -x "$ENG/strata-venv/bin/python" ]; then python3 -m venv "$ENG/strata-venv" || exit 1; fi
"$ENG/strata-venv/bin/pip" install -q -r "$ENG/strata-src/requirements.txt" || exit 1
log "server: $ENG/strata-venv ($("$ENG/strata-venv/bin/python" --version))"

# 4. The weights: five model files + the Engram table, and the pack's metadata shard + drafter.
export HF_HOME="${TMPDIR:-/tmp}/hpc3-strata-hf-$$"
hfdl() { apptainer exec -B "$BASE" -B "${TMPDIR:-/tmp}" "$HF_SIF" hf download "$@"; }
hfdl ISTA-DASLab/DeepSeek-V4.1-Flash-1017-GSQ-RCO-GGUF --revision "$ISTA_REV" --local-dir "$GGUF" \
  "$SET-00001-of-00006.gguf" "$SET-00002-of-00006.gguf" "$SET-00003-of-00006.gguf" \
  "$SET-00004-of-00006.gguf" "$SET-00005-of-00006.gguf" "$ENGRAM" README.md || exit 1
ln -sfn "$ENGRAM" "$GGUF/$SET-00006-of-00006.gguf"
hfdl unsloth/DeepSeek-V4.1-Flash-1017-GGUF --revision "$UNSLOTH_REV" --local-dir "$SHARED" "$META" "$DSPARK" || exit 1
rm -rf "$HF_HOME"
for pair in "$META cc15d470afcb030558bbc4d8e2ccd840182e2e95de1a95198138efe63a5fd99f" \
            "$DSPARK 6ba6ac1b331c540691a209e7fc97dec2a24e295acb1e755c84c7d24a4373927a"; do
  set -- $pair
  [ "$(sha256sum "$SHARED/$1" | cut -d' ' -f1)" = "$2" ] || { echo "SHA-256 mismatch: $SHARED/$1"; exit 1; }
done
log "weights: $GGUF, $SHARED"
log "done. In the deployment's config/.env: AISCIENTIST_SHARED_LLM_ENGINE=strata (section 6b of configs/aiscientist.example.env)"
