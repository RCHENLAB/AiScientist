#!/usr/bin/env bash
# Build the OFFLINE VEP .sif for AiScientist's variant line + stage the VEP caches and ClinVar VCFs on
# dfs3b. Run ON HPC3, as a COMPUTE JOB — see "WHERE TO RUN THIS" below. macOS cannot build .sif.
# See deploy/vep/vep.def and deploy/vep/README.md.
#
#   !! WHERE TO RUN THIS !!  RCIC (2026-08-06) reserves the login nodes for logging in and submitting
#   Slurm jobs: no compute, and NO data transfer — rsync/SFTP/rclone/wget/curl belong on
#   access-hpc3.rcic.uci.edu, and they may kill offending login-node processes. This script pulls
#   ~20GB, so do NOT run it on a login node. Two compliant routes:
#     (a) submit it (compute nodes on `standard` DO have outbound egress, verified 2026-07-08):
#           sbatch -p standard -A ruic20_lab -c 4 --mem=16G -t 08:00:00 \
#                  --wrap "BUILT=1 $PWD/build_and_stage.sh"
#     (b) fetch the big files on the transfer host first, then re-run this to do the rest — it is
#         idempotent and skips anything already present. NOTE access-hpc3 runs a RESTRICTED shell
#         that allows wget/curl/rsync but NOT bash, so this SCRIPT cannot run there; only plain
#         download commands can:
#           ssh <ucinetid>@access-hpc3.rcic.uci.edu "wget -P <dest> <url>"
#
# The image is DEPS-ONLY (vep + bcftools + python3). The bioagent TOOLS are synced to dfs3b +
# bind-mounted at run time (like the analysis line), so a tool edit needs no rebuild. The CACHE and
# ClinVar VCFs are large and versioned, so they are staged SEPARATELY here and bind-mounted read-only.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VEP_RELEASE="${VEP_RELEASE:-112}"                 # MUST match the release_NNN.0 tag in vep.def
DFS_ROOT="${BIOAGENT_DFS_ROOT:-/dfs3b/ruic20_lab/software/AiScientist}"
DFS_DIR="${BIOAGENT_CONTAINERS_DIR:-${DFS_ROOT}/containers}"
# Annotation DBs go in the lab's SHARED reference dir (download-once, reuse across projects), NOT
# under the bioagent-private DFS_ROOT. The vep.sif container stays under DFS_ROOT/containers.
CACHE_ROOT="${BIOAGENT_VEP_CACHE_ROOT:-/dfs3b/ruic20_lab/software/reference/vep_annotation}"
SIF="${DFS_DIR}/vep.sif"
ENSEMBL="https://ftp.ensembl.org/pub/release-${VEP_RELEASE}/variation/indexed_vep_cache"
CLINVAR="https://ftp.ncbi.nlm.nih.gov/pub/clinvar"

echo "== 1. Build vep.sif — pick ONE route (then re-run with BUILT=1) =="
cat <<ROUTES
  module load singularity        # or apptainer (HPC3 has apptainer/1.4.5, singularity/3.11.3)
  (a) fakeroot build from the def (if RCIC enables --fakeroot):
        cd ${HERE} && singularity build --fakeroot vep.sif vep.def
  (b) remote builder (no local root; deps-only def, so this works):
        cd ${HERE} && singularity build --remote vep.sif vep.def
  (c) pull the base + add bcftools/python3 via the def's %post on any Linux+singularity host.
ROUTES
if [ "${BUILT:-0}" != "1" ]; then
    echo "(set BUILT=1 once ${HERE}/vep.sif exists to stage it + download the caches)"; exit 0
fi

echo "== 2. Stage the .sif where the gateway expects it =="
mkdir -p "${DFS_DIR}"
cp -v "${HERE}/vep.sif" "${SIF}"

echo "== 3. Download the VEP caches + ClinVar VCFs to ${CACHE_ROOT} (idempotent) =="
mkdir -p "${CACHE_ROOT}"

fetch() {   # fetch <url> <dest> — skip if a non-empty dest already exists
    local url="$1" dest="$2"
    if [ -s "${dest}" ]; then echo "  skip (exists): ${dest}"; return; fi
    echo "  get ${url}"
    curl -fSL --retry 3 -o "${dest}.part" "${url}"
    mv "${dest}.part" "${dest}"
}

stage_cache() {   # stage_cache <ASSEMBLY> — download + untar into ${CACHE_ROOT}/<ASSEMBLY>/
    local asm="$1"
    local dir="${CACHE_ROOT}/${asm}"
    local tarball="homo_sapiens_vep_${VEP_RELEASE}_${asm}.tar.gz"
    mkdir -p "${dir}"
    if [ -d "${dir}/homo_sapiens/${VEP_RELEASE}_${asm}" ]; then
        echo "  skip (cache present): ${dir}/homo_sapiens/${VEP_RELEASE}_${asm}"; return
    fi
    fetch "${ENSEMBL}/${tarball}" "${dir}/${tarball}"
    echo "  untar ${tarball} -> ${dir}/"
    tar -xzf "${dir}/${tarball}" -C "${dir}"      # -> ${dir}/homo_sapiens/${VEP_RELEASE}_${asm}/
    rm -f "${dir}/${tarball}"
}

stage_clinvar() {   # stage_clinvar <ASSEMBLY> — ClinVar VCF + tabix index for VEP --custom
    local asm="$1"
    fetch "${CLINVAR}/vcf_${asm}/clinvar.vcf.gz"     "${CACHE_ROOT}/clinvar_${asm}.vcf.gz"
    fetch "${CLINVAR}/vcf_${asm}/clinvar.vcf.gz.tbi" "${CACHE_ROOT}/clinvar_${asm}.vcf.gz.tbi"
}

for asm in GRCh38 GRCh37; do
    echo "-- ${asm} --"
    stage_cache "${asm}"
    stage_clinvar "${asm}"
done

echo "== 4. Smoke-test offline VEP on a tiny VCF (GRCh38) =="
cat <<EOF
  printf '%s\\n' \\
    '##fileformat=VCFv4.2' \\
    '#CHROM\\tPOS\\tID\\tREF\\tALT\\tQUAL\\tFILTER\\tINFO' \\
    '17\\t7676154\\t.\\tG\\tA\\t.\\tPASS\\t.' > /tmp/smoke.vcf

  singularity exec --containall --net --network none \\
    -B ${CACHE_ROOT}/GRCh38:${CACHE_ROOT}/GRCh38:ro \\
    -B ${CACHE_ROOT}/clinvar_GRCh38.vcf.gz:${CACHE_ROOT}/clinvar_GRCh38.vcf.gz:ro \\
    -B ${CACHE_ROOT}/clinvar_GRCh38.vcf.gz.tbi:${CACHE_ROOT}/clinvar_GRCh38.vcf.gz.tbi:ro \\
    -B /tmp:/tmp \\
    ${SIF} vep --offline --cache --dir_cache ${CACHE_ROOT}/GRCh38 \\
      --assembly GRCh38 --species homo_sapiens --fork 4 \\
      --input_file /tmp/smoke.vcf --format vcf --json --output_file /tmp/smoke.jsonl \\
      --no_stats --force_overwrite --symbol --sift b --polyphen b --af --af_gnomade --af_gnomadg \\
      --custom file=${CACHE_ROOT}/clinvar_GRCh38.vcf.gz,short_name=ClinVar,format=vcf,type=exact,coords=0,fields=CLNSIG%CLNDN
  cat /tmp/smoke.jsonl     # expect one JSON line: TP53 missense, ClinVar significance present
EOF

echo "== 5. Enable the offline variant line (in .env / HPCSettings) =="
cat <<EOF
  BIOAGENT_VEP_IMAGE=${SIF}
  BIOAGENT_VEP_CACHE_DIR_GRCH38=${CACHE_ROOT}/GRCh38
  BIOAGENT_VEP_CACHE_DIR_GRCH37=${CACHE_ROOT}/GRCh37
  BIOAGENT_VEP_CLINVAR_GRCH38=${CACHE_ROOT}/clinvar_GRCh38.vcf.gz
  BIOAGENT_VEP_CLINVAR_GRCH37=${CACHE_ROOT}/clinvar_GRCh37.vcf.gz
  BIOAGENT_VEP_ASSEMBLY=GRCh38          # default; the LLM can override per call
  BIOAGENT_VEP_FORK=8                   # == cpus-per-task on the CPU node
  BIOAGENT_VARIANT_ON_HPC=1             # route annotate_variants to the offline HPC3 line
  BIOAGENT_UPLOADS_ON_HPC=1             # so the VCF lands on dfs3b and is annotated in place

The bioagent TOOLS are synced to <lab_storage>/<user>/pysrc by the gateway and bind-mounted in — a
tool edit needs only a code deploy, NOT an image rebuild. If HPC is unreachable (or the tool sync
fails), annotate_variants falls back to the REST path in-process (fine for small VCFs).
EOF
