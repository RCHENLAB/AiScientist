#!/usr/bin/env bash
# Build the CPU analysis .sif for AiScientist CodeAct (run_code on HPC3) and stage it on dfs3b.
# scanpy/pandas/gseapy — NO GPU. Build on an HPC3 compute node with Apptainer (macOS cannot
# build .sif). This is the image SlurmCodeExecutor runs each run_code snippet inside.
set -euo pipefail
# Legacy BIOAGENT_* names still work (AISCIENTIST_* wins when both are set) — same rule as the
# Python side (aiscientist.core.config.apply_brand_env_aliases).
for _old in $(compgen -v BIOAGENT_ || true); do _new="AISCIENTIST_${_old#BIOAGENT_}"; [ -n "${!_new+x}" ] || export "$_new=${!_old}"; done

HERE="$(cd "$(dirname "$0")" && pwd)"
DFS_DIR="${AISCIENTIST_CONTAINERS_DIR:-/dfs3b/ruic20_lab/software/AiScientist/containers}"
SIF="${DFS_DIR}/analysis.sif"

# The AiScientist TOOLS are NOT in this image — they are synced to dfs3b + bind-mounted at run time
# (see gateway/slurm_analysis.py / app._sync_aiscientist_source_to_hpc). So the image is DEPS-ONLY
# and only needs rebuilding when the Python deps change — a tool edit needs no rebuild.

echo "== 1. Build analysis.sif with Apptainer (then re-run with BUILT=1) =="
cat <<ROUTES
  # On an HPC3 COMPUTE node (RCIC: no builds on login nodes). Apptainer's --fakeroot needs no
  # /etc/subuid entry on HPC3, and Sylabs --remote builds are gone (docs/hpc3_assets.md).
  cd ${HERE} && sbatch -p standard -A ruic20_lab -c 8 --mem=16G -t 04:00:00 -J build-analysis \\
    --wrap 'module load apptainer/1.4.5 && APPTAINER_CACHEDIR=\$TMPDIR/ac apptainer build --fakeroot analysis.sif analysis.def'
  # Alternative: build a Docker image elsewhere -> registry, then on HPC3:
  #   apptainer build analysis.sif docker://<registry>/bioagent-analysis:cpu
ROUTES
if [ "${BUILT:-0}" != "1" ]; then
    echo "(set BUILT=1 once ${HERE}/analysis.sif exists to stage it)"; exit 0
fi

echo "== 2. Place the .sif where the gateway expects it =="
mkdir -p "${DFS_DIR}"
cp -v "${HERE}/analysis.sif" "${SIF}"

echo "== 3. Smoke-test the image on a CPU node (no GPU needed) =="
cat <<EOF
  # analysis stack (deps only — the AiScientist tools are bound at run time, not in the image):
  apptainer exec --containall --writable-tmpfs --net --network none \\
    ${SIF} python -c "import scanpy, gseapy, pandas, h5py; print('ok', scanpy.__version__)"

Then enable the HPC paths (in .env / HPCSettings):
  AISCIENTIST_ANALYSIS_IMAGE=${SIF}
  AISCIENTIST_CPU_PARTITION=standard          # RCIC HPC3 free CPU partition
  AISCIENTIST_CPU_ACCOUNT=ruic20_lab
  AISCIENTIST_RUN_CODE_MEM_GB=64              # the real per-job memory cap
  AISCIENTIST_RUN_CODE_ON_HPC=1               # CodeAct run_code as CPU Slurm jobs
  AISCIENTIST_ANALYSIS_ON_HPC=1               # scanpy QC/cluster/DE/enrichment as CPU Slurm jobs (Phase 4)
  AISCIENTIST_UPLOADS_ON_HPC=1                # uploads land on dfs3b so analysis reads them in place (Phase 2)

The AiScientist TOOLS are synced to <lab_storage>/<user>/pysrc by the gateway (over its SSH session)
and bind-mounted in — so a tool edit needs only a normal code deploy, NOT an image rebuild. The
dataset lives on dfs3b; the analysis run dir is created under <lab_storage>/<user>/analysis/<run_id>.
If HPC is unreachable (or the tool sync fails), analysis falls back in-process.
EOF
