#!/usr/bin/env bash
# One-time Qwen2.5-VL render-review container setup on HPC3, following RCIC's container rules
# (rcic.uci.edu/software/user-installed.html): Apptainer (module apptainer/1.4.5), built on an
# INTERACTIVE COMPUTE NODE (never a login node), cache OFF $HOME, into the SHARED lab DFS.
#
# UNLIKE hpc3_vllm_setup.sh (which `apptainer pull`s a PUBLIC vLLM image needing no root),
# this BUILDS a custom .def (%post pip installs), with `apptainer build --fakeroot`. HPC3's
# apptainer runs fakeroot in a root-mapped user namespace, so no /etc/subuid entry is needed
# (verified 2026-10-09). Sylabs `--remote` builds are gone: Apptainer removed them.
#
#   # 0. get the build kit onto HPC3 (the server/HPC3 cannot git pull the private repo).
#   #    From your Mac (or from the eyeserver's /data/aiscientist/app). RCIC (2026-08-06) forbids
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
# Legacy BIOAGENT_* names still work (AISCIENTIST_* wins when both are set) — same rule as the
# Python side (aiscientist.core.config.apply_brand_env_aliases).
for _old in $(compgen -v BIOAGENT_ || true); do _new="AISCIENTIST_${_old#BIOAGENT_}"; [ -n "${!_new+x}" ] || export "$_new=${!_old}"; done

BASE="${AISCIENTIST_LAB_BASE:-/dfs3b/ruic20_lab/software/AiScientist}"
IMAGE="$BASE/containers/vlreview.sif"
MODEL_DIR="${AISCIENTIST_VLREVIEW_MODEL_DIR:-$BASE/models/vlreview}"
VL_REPO="${AISCIENTIST_VLREVIEW_HF_REPO:-Qwen/Qwen2.5-VL-7B-Instruct}"
CT_MODULE="${AISCIENTIST_CONTAINER_MODULE:-apptainer/1.4.5}"
CT_BIN="${AISCIENTIST_CONTAINER_BIN:-apptainer}"

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

log "lab base: $BASE   (container module: $CT_MODULE)"
mkdir -p "$BASE/containers" "$MODEL_DIR" || {
  echo "cannot mkdir under $BASE — run 'newgrp ruic20_hpc' first"; exit 1; }

source /etc/profile.d/lmod.sh 2>/dev/null || true
module load "$CT_MODULE" 2>/dev/null || true
command -v "$CT_BIN" >/dev/null 2>&1 || {
  echo "$CT_BIN not found — 'module avail apptainer', load it, then re-run"; exit 1; }
log "runtime: $(command -v "$CT_BIN")  ($("$CT_BIN" --version 2>/dev/null))"

# Keep the build cache + tmp OFF $HOME (layers are big; $HOME has a ~50GB quota). The
# SINGULARITY_* names cover a run with AISCIENTIST_CONTAINER_BIN=singularity.
export APPTAINER_CACHEDIR="${TMPDIR:-/tmp}/apptainer-cache-$USER"
export APPTAINER_TMPDIR="${TMPDIR:-/tmp}/apptainer-tmp-$USER"
export SINGULARITY_CACHEDIR="$APPTAINER_CACHEDIR" SINGULARITY_TMPDIR="$APPTAINER_TMPDIR"
mkdir -p "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"
log "cache dir (off \$HOME): $APPTAINER_CACHEDIR"

# 1) build the image (custom .def -> fakeroot)
if [ -f "$IMAGE" ]; then
  log "image present: $IMAGE (skip build; rm it to rebuild)"
else
  log "building (fakeroot) -> $IMAGE"
  "$CT_BIN" build --fakeroot "$IMAGE" "$DEF" || {
    echo ""; echo "fakeroot build failed — read the %post error above. (An apt-get that dies on"
    echo "'setgroups' needs APT::Sandbox::User root first: see deploy/README.md.)"
    exit 1; }
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
  "$CT_BIN" exec "$IMAGE" bash -lc \
    "export HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0; hf download '$VL_REPO' --local-dir '$MODEL_DIR'" \
    || "$CT_BIN" exec "$IMAGE" bash -lc \
       "export HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0; python -c \"from huggingface_hub import snapshot_download; snapshot_download('$VL_REPO', local_dir='$MODEL_DIR')\"" \
    || { echo "weight download failed — check net on this node / HF availability"; exit 1; }
fi
test -f "$MODEL_DIR/config.json" || { echo "MISSING weights in $MODEL_DIR"; exit 1; }

# 3) sanity: is a cheap 24GB card (A30/RTX6000) actually offered as a typed gres here?
log "GPU gres offered by your partitions (want an A30 or RTX6000 line):"
sinfo -o "%P %G" 2>/dev/null | grep -iE "gpu|a30|rtx|l40|a100" | sort -u || true

cat <<EOF

[hpc3-vlreview] DONE. Set these on the EYESERVER (.env / HPCSettings) so the gateway uses it:
  AISCIENTIST_VLREVIEW_ENABLED=1
  AISCIENTIST_VLREVIEW_IMAGE=$IMAGE
  AISCIENTIST_VLREVIEW_MODEL_DIR=$MODEL_DIR
  AISCIENTIST_VLREVIEW_ENTRYPOINT='python /opt/vlreview/run_review.py'
  AISCIENTIST_VLREVIEW_GRES='gpu:A30:1'      # <- match a typed gres printed above; NOT gpu:A100

Smoke test on a gpu job (from a compute node with a GPU):
  module load $CT_MODULE
  $CT_BIN exec --nv \\
    -B $MODEL_DIR:$MODEL_DIR:ro -B /tmp/vlr_out:/tmp/vlr_out \\
    $IMAGE python /opt/vlreview/run_review.py \\
    --pdf <some_report.pdf> --model $MODEL_DIR --out /tmp/vlr_out
  # then: cat /tmp/vlr_out/review.json
EOF
