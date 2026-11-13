#!/usr/bin/env bash
# Stage VEP release-matched reference caches and ClinVar VCFs for the native MCP service on dfs3b.
# Run on an HPC3 compute node; see the transfer policy below.
# See deploy/vep/README.md. The immutable MCP image is published by aiscientist-mcp.
#
#   !! WHERE TO RUN THIS !!  RCIC (2026-08-06) reserves the login nodes for logging in and submitting
#   Slurm jobs: no compute, and NO data transfer — rsync/SFTP/rclone/wget/curl belong on
#   access-hpc3.rcic.uci.edu, and they may kill offending login-node processes. This script pulls
#   ~20GB, so do NOT run it on a login node. Two compliant routes:
#     (a) submit it (compute nodes on `standard` DO have outbound egress, verified 2026-07-08):
#           sbatch -p standard -A ruic20_lab -c 4 --mem=16G -t 08:00:00 \
#                  --wrap "$PWD/build_and_stage.sh"
#     (b) fetch the big files on the transfer host first, then re-run this to do the rest — it is
#         idempotent and skips anything already present. NOTE access-hpc3 runs a RESTRICTED shell
#         that allows wget/curl/rsync but NOT bash, so this SCRIPT cannot run there; only plain
#         download commands can:
#           ssh <ucinetid>@access-hpc3.rcic.uci.edu "wget -P <dest> <url>"
#
# Reference data remain separate read-only inputs; no application science is copied into an image.
set -euo pipefail
# Legacy BIOAGENT_* names still work (AISCIENTIST_* wins when both are set) — same rule as the
# Python side (aiscientist.core.config.apply_brand_env_aliases).
for _old in $(compgen -v BIOAGENT_ || true); do _new="AISCIENTIST_${_old#BIOAGENT_}"; [ -n "${!_new+x}" ] || export "$_new=${!_old}"; done

VEP_RELEASE="${VEP_RELEASE:-112}"  # must match the registered variant MCP image
CACHE_ROOT="${AISCIENTIST_VEP_CACHE_ROOT:-/dfs3b/ruic20_lab/software/reference/vep_annotation}"
ENSEMBL="https://ftp.ensembl.org/pub/release-${VEP_RELEASE}/variation/indexed_vep_cache"
CLINVAR="https://ftp.ncbi.nlm.nih.gov/pub/clinvar"

echo "== Download the VEP caches + ClinVar VCFs to ${CACHE_ROOT} (idempotent) =="
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

echo "Reference staging finished. Configure the registered variant MCP service:"
cat <<EOF
  VARIANT_VEP_CACHE_DIR_GRCH38=${CACHE_ROOT}/GRCh38
  VARIANT_VEP_CACHE_DIR_GRCH37=${CACHE_ROOT}/GRCh37
  VARIANT_VEP_CLINVAR_GRCH38=${CACHE_ROOT}/clinvar_GRCh38.vcf.gz
  VARIANT_VEP_CLINVAR_GRCH37=${CACHE_ROOT}/clinvar_GRCh37.vcf.gz
EOF
