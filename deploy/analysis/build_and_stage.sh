#!/usr/bin/env bash
# Build the CPU analysis .sif for AiScientist CodeAct (run_code on HPC3) and stage it on dfs3b.
# scanpy/pandas/gseapy — NO GPU. Build on HPC3 or any Linux+singularity host (macOS cannot
# build .sif). This is the image SlurmCodeExecutor runs each run_code snippet inside.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DFS_DIR="${BIOAGENT_CONTAINERS_DIR:-/dfs3b/ruic20_lab/software/AiScientist/containers}"
SIF="${DFS_DIR}/analysis.sif"

# The bioagent TOOLS are NOT in this image — they are synced to dfs3b + bind-mounted at run time
# (see gateway/slurm_analysis.py / app._sync_bioagent_source_to_hpc). So the image is DEPS-ONLY
# and only needs rebuilding when the Python deps change — a tool edit needs no rebuild. It also
# means `--remote` works (no local-file %files to ship to the cloud builder).

echo "== 1. Build analysis.sif — pick ONE route (then re-run with BUILT=1) =="
cat <<ROUTES
  (a) fakeroot build from the def (if RCIC enables --fakeroot):
        cd ${HERE} && singularity build --fakeroot analysis.sif analysis.def
  (b) remote builder (no local root needed — DEPS-only def, so this works):
        cd ${HERE} && singularity build --remote analysis.sif analysis.def
  (c) Docker elsewhere -> registry -> convert on HPC3 (NO fakeroot):
        docker build -t <registry>/bioagent-analysis:cpu ${HERE}   # add a Dockerfile FROM the same base
        docker push <registry>/bioagent-analysis:cpu
        singularity build analysis.sif docker://<registry>/bioagent-analysis:cpu
ROUTES
if [ "${BUILT:-0}" != "1" ]; then
    echo "(set BUILT=1 once ${HERE}/analysis.sif exists to stage it)"; exit 0
fi

echo "== 2. Place the .sif where the gateway expects it =="
mkdir -p "${DFS_DIR}"
cp -v "${HERE}/analysis.sif" "${SIF}"

echo "== 3. Smoke-test the image on a CPU node (no GPU needed) =="
cat <<EOF
  # analysis stack (deps only — the bioagent tools are bound at run time, not in the image):
  singularity exec --containall --writable-tmpfs --net --network none \\
    ${SIF} python -c "import scanpy, gseapy, pandas, h5py; print('ok', scanpy.__version__)"

Then enable the HPC paths (in .env / HPCSettings):
  BIOAGENT_ANALYSIS_IMAGE=${SIF}
  BIOAGENT_CPU_PARTITION=standard          # RCIC HPC3 free CPU partition
  BIOAGENT_CPU_ACCOUNT=ruic20_lab
  BIOAGENT_RUN_CODE_MEM_GB=64              # the real per-job memory cap
  BIOAGENT_RUN_CODE_ON_HPC=1               # CodeAct run_code as CPU Slurm jobs
  BIOAGENT_ANALYSIS_ON_HPC=1               # scanpy QC/cluster/DE/enrichment as CPU Slurm jobs (Phase 4)
  BIOAGENT_UPLOADS_ON_HPC=1                # uploads land on dfs3b so analysis reads them in place (Phase 2)

The bioagent TOOLS are synced to <lab_storage>/<user>/pysrc by the gateway (over its SSH session)
and bind-mounted in — so a tool edit needs only a normal code deploy, NOT an image rebuild. The
dataset lives on dfs3b; the analysis run dir is created under <lab_storage>/<user>/analysis/<run_id>.
If HPC is unreachable (or the tool sync fails), analysis falls back in-process.
EOF
