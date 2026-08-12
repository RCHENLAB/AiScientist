#!/usr/bin/env bash
# Build the pandoc/XeLaTeX report-render .sif for AiScientist (Phase 5) and stage it on dfs3b.
# Deps-only (pandoc + texlive via the pandoc/extra base) — NO bioagent code, NO GPU. Build on
# HPC3 with `--remote` (no fakeroot there). This is the image SlurmReportRenderer runs pandoc in.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DFS_DIR="${BIOAGENT_CONTAINERS_DIR:-/dfs3b/ruic20_lab/software/AiScientist/containers}"
SIF="${DFS_DIR}/report.sif"

echo "== 1. Build report.sif — pick ONE route (then re-run with BUILT=1) =="
cat <<ROUTES
  (a) remote builder (no local root needed — deps-only def, so this works):
        cd ${HERE} && singularity build --remote report.sif report.def
  (b) fakeroot build (if RCIC enables --fakeroot):
        cd ${HERE} && singularity build --fakeroot report.sif report.def
  (c) pull the base + build on top elsewhere -> registry -> convert on HPC3.
ROUTES
if [ "${BUILT:-0}" != "1" ]; then
    echo "(set BUILT=1 once ${HERE}/report.sif exists to stage it)"; exit 0
fi

echo "== 2. Place the .sif where the gateway expects it =="
mkdir -p "${DFS_DIR}"
cp -v "${HERE}/report.sif" "${SIF}"

echo "== 3. Smoke-test on a CPU node (no GPU needed) =="
cat <<EOF
  # pandoc runs DIRECTLY — the pandoc/extra image has no bash, so don't wrap in a shell; pass env
  # with --env and bind a work dir (NOT /tmp, which --writable-tmpfs replaces with a fresh tmpfs).
  singularity exec --containall --writable-tmpfs "${SIF}" pandoc --version | head -1
  mkdir -p ~/reptest && echo '# hi' > ~/reptest/t.md
  singularity exec --containall --writable-tmpfs --env HOME=/tmp -B ~/reptest:/work \\
    "${SIF}" pandoc /work/t.md -o /work/t.pdf --pdf-engine=xelatex && echo "PDF OK"

Then enable the HPC report path (in .env / HPCSettings):
  BIOAGENT_REPORT_IMAGE=${SIF}
  BIOAGENT_REPORT_ON_HPC=1     # render report.pdf/.docx as a CPU Slurm job (Phase 5)
  BIOAGENT_CPU_PARTITION=standard
  BIOAGENT_CPU_ACCOUNT=ruic20_lab

The report bundle (markdown + figures/tables) is staged to <lab_storage>/<user>/reports and the
PDF/DOCX pulled back. If HPC is unreachable, rendering falls back to local pandoc on the eyeserver.
EOF
