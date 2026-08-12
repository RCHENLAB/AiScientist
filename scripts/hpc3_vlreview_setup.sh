#!/usr/bin/env bash
# One-time Qwen2.5-VL render-review container setup on HPC3, following RCIC's container rules
# (rcic.uci.edu/software/user-installed.html): Singularity (NOT Apptainer), built on an
# INTERACTIVE COMPUTE NODE (never a login node), cache OFF $HOME, into the SHARED lab DFS.
#
# UNLIKE hpc3_vllm_setup.sh (which `singularity pull`s a PUBLIC vLLM image needing no root),
# this BUILDS a custom .def (%post pip installs), so it needs --fakeroot OR --remote. This is
# the one thing to confirm for your RCIC account.
#
#   # 0. get the build kit onto HPC3 (the server/HPC3 cannot git pull the private repo).
#   #    From your Mac (or from the eyeserver's /data/BioAgent/app). RCIC (2026-08-06) forbids
#   #    rsync/SFTP/wget on the login nodes — transfers go to access-hpc3, same $HOME and /dfs3b:
#   rsync -av deploy/vlreview/ access-hpc3.rcic.uci.edu:~/vlreview-build/
#
#   # 1. on HPC3, claim a compute node and run this script FROM the kit dir:
#   ssh hpc3.rcic.uci.edu                                   # (Duo)
#   newgrp ruic20_hpc                                       # so you can write under the lab DFS
#   srun -c 4 -p free --time=2:00:00 --pty /bin/bash -i     # RCIC: build on a compute node
#   cd ~/vlreview-build && bash /path/to/scripts/hpc3_vlreview_setup.sh
#     # (or copy this script into ~/vlreview-build and: bash hpc3_vlreview_setup.sh)
set -uo pipefail

BASE="${BIOAGENT_LAB_BASE:-/dfs3b/ruic20_lab/software/bioagent}"
IMAGE="$BASE/containers/vlreview.sif"
MODEL_DIR="${BIOAGENT_VLREVIEW_MODEL_DIR:-$BASE/vlreview_model}"
VL_REPO="${BIOAGENT_VLREVIEW_HF_REPO:-Qwen/Qwen2.5-VL-7B-Instruct}"
SING_MODULE="${BIOAGENT_CONTAINER_MODULE:-singularity/3.11.3}"
BUILD_MODE="${BIOAGENT_VLREVIEW_BUILD_MODE:-fakeroot}"   # fakeroot | remote

log() { echo "[hpc3-vlreview] $*"; }

# The build context (this dir) must contain the .def + run_review.py (%files copies it in).
DEF="${VLREVIEW_DEF:-vlreview.def}"
if [ ! -f "$DEF" ] || [ ! -f run_review.py ]; then
  echo "ERROR: run this FROM the kit dir (needs $DEF + run_review.py alongside)."
  echo "       rsync deploy/vlreview/ to HPC3 and cd into it first."
  exit 1
fi

# RCIC: "If you do builds on login nodes you will have problems." Refuse politely.
if [[ "$(hostname)" == *login* ]]; then
  echo "ERROR: you are on a LOGIN node ($(hostname))."
  echo "RCIC requires container builds on a COMPUTE node. Claim one first:"
  echo "    srun -c 4 -p free --time=2:00:00 --pty /bin/bash -i"
  echo "then re-run this script."
  exit 1
fi

log "lab base: $BASE   (singularity module: $SING_MODULE)   build mode: $BUILD_MODE"
mkdir -p "$BASE/containers" "$MODEL_DIR" || {
  echo "cannot mkdir under $BASE — run 'newgrp ruic20_hpc' first"; exit 1; }

# Singularity (RCIC module), NOT apptainer.
source /etc/profile.d/lmod.sh 2>/dev/null || true
module load "$SING_MODULE" 2>/dev/null || module load singularity 2>/dev/null || true
command -v singularity >/dev/null 2>&1 || {
  echo "singularity not found — 'module avail singularity', load it, then re-run"; exit 1; }
log "singularity: $(command -v singularity)  ($(singularity --version 2>/dev/null))"

# Keep the build cache + tmp OFF $HOME (layers are big; $HOME has a ~50GB quota).
export SINGULARITY_CACHEDIR="${TMPDIR:-/tmp}/singularity-cache-$USER"
export SINGULARITY_TMPDIR="${TMPDIR:-/tmp}/singularity-tmp-$USER"
mkdir -p "$SINGULARITY_CACHEDIR" "$SINGULARITY_TMPDIR"
log "cache dir (off \$HOME): $SINGULARITY_CACHEDIR"

# 1) build the image (custom .def -> needs fakeroot or remote)
if [ -f "$IMAGE" ]; then
  log "image present: $IMAGE (skip build; rm it to rebuild)"
else
  case "$BUILD_MODE" in
    fakeroot)
      log "building (fakeroot) -> $IMAGE"
      singularity build --fakeroot "$IMAGE" "$DEF" || {
        echo ""; echo "fakeroot build failed. Either your account lacks --fakeroot, or a dep"
        echo "conflict occurred. Retry with the Sylabs remote builder (no local root):"
        echo "    singularity remote login          # one-time, free Sylabs token"
        echo "    BIOAGENT_VLREVIEW_BUILD_MODE=remote bash $0"
        exit 1; }
      ;;
    remote)
      log "building (--remote via Sylabs cloud builder) -> $IMAGE"
      singularity build --remote "$IMAGE" "$DEF" || {
        echo "remote build failed — run 'singularity remote login' first (free Sylabs token)."; exit 1; }
      ;;
    *) echo "unknown BUILD_MODE=$BUILD_MODE (use fakeroot|remote)"; exit 1;;
  esac
fi

# 2) stage the VL weights into the shared model dir (bound read-only at run time). This
#    interactive compute node has outbound net (same as the vLLM setup's model download);
#    the GPU review JOB runs offline (HF_HUB_OFFLINE=1 in the image) against these files.
if [ -f "$MODEL_DIR/config.json" ]; then
  log "weights present: $MODEL_DIR (skip download)"
else
  log "downloading $VL_REPO -> $MODEL_DIR (~16GB; resumable) via the image's hf CLI ..."
  # The image bakes HF_HUB_OFFLINE=1 for the RUN-time GPU job (compute nodes have no net);
  # the SETUP-time download runs inside that same image, so we must turn offline OFF *inside*
  # the container command (set after %environment is sourced, so it reliably wins).
  singularity exec "$IMAGE" bash -lc \
    "export HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0; hf download '$VL_REPO' --local-dir '$MODEL_DIR'" \
    || singularity exec "$IMAGE" bash -lc \
       "export HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0; python -c \"from huggingface_hub import snapshot_download; snapshot_download('$VL_REPO', local_dir='$MODEL_DIR')\"" \
    || { echo "weight download failed — check net on this node / HF availability"; exit 1; }
fi
test -f "$MODEL_DIR/config.json" || { echo "MISSING weights in $MODEL_DIR"; exit 1; }

# 3) sanity: is a cheap 24GB card (A30/RTX6000) actually offered as a typed gres here?
log "GPU gres offered by your partitions (want an A30 or RTX6000 line):"
sinfo -o "%P %G" 2>/dev/null | grep -iE "gpu|a30|rtx|l40|a100" | sort -u || true

cat <<EOF

[hpc3-vlreview] DONE. Set these on the EYESERVER (.env / HPCSettings) so the gateway uses it:
  BIOAGENT_VLREVIEW_ENABLED=1
  BIOAGENT_VLREVIEW_IMAGE=$IMAGE
  BIOAGENT_VLREVIEW_MODEL_DIR=$MODEL_DIR
  BIOAGENT_VLREVIEW_ENTRYPOINT='python /opt/vlreview/run_review.py'
  BIOAGENT_VLREVIEW_GRES='gpu:A30:1'      # <- match a typed gres printed above; NOT gpu:A100

Smoke test on a gpu job (from a compute node with a GPU):
  singularity exec --nv \\
    -B $MODEL_DIR:$MODEL_DIR:ro -B /tmp/vlr_out:/tmp/vlr_out \\
    $IMAGE python /opt/vlreview/run_review.py \\
    --pdf <some_report.pdf> --model $MODEL_DIR --out /tmp/vlr_out
  # then: cat /tmp/vlr_out/review.json
EOF
