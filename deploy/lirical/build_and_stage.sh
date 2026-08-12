#!/usr/bin/env bash
# Build the LIRICAL .sif for BioAgent's phenotype->disease line + stage the LIRICAL phenotype data (and,
# for genotype-aware scoring, an Exomiser variant database) on dfs3b. Run ON HPC3, as a COMPUTE JOB —
# see "WHERE TO RUN THIS" below. macOS cannot build .sif.
# See deploy/lirical/lirical.def and deploy/lirical/README.md.
#
#   !! WHERE TO RUN THIS !!  RCIC (2026-08-06) reserves the login nodes for logging in and submitting
#   Slurm jobs: no compute, and NO data transfer — rsync/SFTP/rclone/wget/curl belong on
#   access-hpc3.rcic.uci.edu, and they may kill offending login-node processes. With EXOMISER=1 this
#   script pulls ~20GB, so do NOT run it on a login node. Submit it instead (compute nodes on
#   `standard` have outbound egress, verified 2026-07-08):
#       sbatch -p standard -A ruic20_lab -c 4 --mem=16G -t 08:00:00 \
#              --wrap "BUILT=1 EXOMISER=1 $PWD/build_and_stage.sh"
#   (access-hpc3 allows wget/curl/rsync but NOT bash, so this SCRIPT cannot run there — only plain
#   download commands can.)
#
# The image bakes the LIRICAL v2 CLI + a JRE 17 (deps-only, like vep.sif bakes VEP). The DATA is large
# and versioned, so it is staged SEPARATELY here and bind-mounted read-only:
#   * LIRICAL phenotype data (hp.json / phenotype.hpoa / Jannovar transcript DBs, ~1-2GB) -- REQUIRED.
#   * an Exomiser VARIANT database (~20GB) -- OPTIONAL, and ONLY for the genotype-aware posterior.
#
#   !! EXOMISER VERSION GOTCHA !!  The lab already has Exomiser data at
#      /dfs3b/ruic20_lab/{chen/pipeline_restructure/pipeline_restructure,bin/pipeline/pipeline_restructure}/exomiser
#   BUT it is the 2018-era 1805_hg19 / exomiser-cli-10.1.0 data -- the OLD Exomiser 10.x DB schema, and
#   hg19 ONLY. LIRICAL v2 needs an Exomiser data release >= 2302 (the newer .mv.db format). So that
#   existing data CANNOT be reused for LIRICAL v2's genotype step; a fresh Exomiser DB must be staged
#   (this script fetches it when EXOMISER=1). Phenotype-only LIRICAL needs NO Exomiser DB and works now.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LIRICAL_VERSION="${LIRICAL_VERSION:-2.4.1}"          # MUST match the LIRICAL_VERSION baked in lirical.def
EXOMISER_DATA_VERSION="${EXOMISER_DATA_VERSION:-2406}"  # a LIRICAL-v2-compatible Exomiser data release
DFS_ROOT="${BIOAGENT_DFS_ROOT:-/dfs3b/ruic20_lab/software/AiScientist}"
DFS_DIR="${BIOAGENT_CONTAINERS_DIR:-${DFS_ROOT}/containers}"
# Data goes in the lab's SHARED reference dir (download-once, reuse), NOT under the bioagent-private
# DFS_ROOT -- same convention as the VEP cache (reference/vep_annotation).
REF_ROOT="${BIOAGENT_LIRICAL_REF_ROOT:-/dfs3b/ruic20_lab/software/reference/lirical}"
DATA_DIR="${REF_ROOT}/data"                          # LIRICAL `download` target (hp.json, hpoa, Jannovar)
EXOMISER_DIR="${REF_ROOT}/exomiser"                  # fresh Exomiser variant DB(s) for LIRICAL v2
SIF="${DFS_DIR}/lirical.sif"
# Exomiser data mirror (verified 2026-07-14: the Monarch data server lists 2406_hg19/2406_hg38 here).
# Assembly tag is hg19 / hg38.
EXOMISER_BASE="${EXOMISER_BASE:-https://data.monarchinitiative.org/exomiser/latest}"

echo "== 1. Build lirical.sif -- pick ONE route (then re-run with BUILT=1) =="
cat <<ROUTES
  module load singularity        # or apptainer (HPC3 has apptainer/1.4.5, singularity/3.11.3)
  (a) fakeroot build from the def (if RCIC enables --fakeroot):
        cd ${HERE} && singularity build --fakeroot lirical.sif lirical.def
  (b) remote builder (no local root; the def downloads LIRICAL in %post, no local %files, so this works):
        cd ${HERE} && singularity build --remote lirical.sif lirical.def
ROUTES
if [ "${BUILT:-0}" != "1" ]; then
    echo "(set BUILT=1 once ${HERE}/lirical.sif exists to stage it + download the data)"; exit 0
fi

echo "== 2. Stage the .sif where the gateway expects it =="
mkdir -p "${DFS_DIR}"
cp -v "${HERE}/lirical.sif" "${SIF}"

echo "== 3. Download the LIRICAL phenotype data to ${DATA_DIR} (idempotent, REQUIRED) =="
mkdir -p "${DATA_DIR}"
# `lirical download` fetches hp.json, phenotype.hpoa, Homo_sapiens_gene_info.gz, mim2gene_medgen, and
# the Jannovar transcript databases (hg19 + hg38) into -d <dir>. Run it THROUGH the image so the exact
# LIRICAL build that will run at analysis time writes the layout it expects.
if [ -s "${DATA_DIR}/hp.json" ] || [ -s "${DATA_DIR}/hpo.json" ]; then
    echo "  skip (LIRICAL data present): ${DATA_DIR}"
else
    singularity exec -B "${DATA_DIR}:${DATA_DIR}" "${SIF}" lirical download -d "${DATA_DIR}"
fi
ls -la "${DATA_DIR}" | head

echo "== 4. (OPTIONAL) Exomiser variant DB for the GENOTYPE-aware posterior (EXOMISER=1) =="
if [ "${EXOMISER:-0}" = "1" ]; then
    mkdir -p "${EXOMISER_DIR}"
    ASM="${EXOMISER_ASSEMBLY:-hg19}"                 # match the lab's eye VCFs (mostly GRCh37/hg19)
    name="${EXOMISER_DATA_VERSION}_${ASM}"
    dest="${EXOMISER_DIR}/${name}"
    if [ -d "${dest}" ] && [ -s "${dest}/${name}_variants.mv.db" ]; then
        echo "  skip (Exomiser ${name} present): ${dest}"
    else
        echo "  get ${EXOMISER_BASE}/${name}.zip  (~20GB -- verify the URL resolves first!)"
        curl -fSL --retry 3 -o "${EXOMISER_DIR}/${name}.zip" "${EXOMISER_BASE}/${name}.zip"
        unzip -q "${EXOMISER_DIR}/${name}.zip" -d "${EXOMISER_DIR}"
        rm -f "${EXOMISER_DIR}/${name}.zip"
    fi
    ls -la "${dest}" 2>/dev/null | head
else
    echo "  skipped (set EXOMISER=1 [EXOMISER_ASSEMBLY=hg19|hg38] to stage it). LIRICAL still runs"
    echo "  phenotype-only without it -- gene-restricted to the variant line's shortlist."
fi

echo "== 5. Smoke-test LIRICAL (VERIFIED end-to-end on HPC3 2026-07-14, LIRICAL v2.4.1) =="
cat <<EOF
  # LIRICAL v2.4 'prioritize' is CLI-ARGS mode: -p is the comma-separated OBSERVED HPO term IDs (NOT a
  # phenopacket file); -o outdir, -x prefix, -f format. Phenotype-only = omit --assembly/--vcf/-ed*.
  # (a) phenotype-only  (~1 min; expect retinal diseases near the top):
  singularity exec -B /dfs3b:/dfs3b -B /tmp:/tmp ${SIF} \\
    lirical prioritize -p HP:0000512,HP:0000662,HP:0000510 -d ${DATA_DIR} -o /tmp -x smoke_pheno -f tsv
  grep -v '^!' /tmp/smoke_pheno.tsv | head -12

  # (b) genotype-aware  (needs EXOMISER=1 above; -ed19 = the Exomiser DATA DIRECTORY, not the .mv.db file):
  printf '%s\\n' '##fileformat=VCFv4.2' '##contig=<ID=1,length=249250621>' \\
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">' \\
    '#CHROM\\tPOS\\tID\\tREF\\tALT\\tQUAL\\tFILTER\\tINFO\\tFORMAT\\tsmoke-1' \\
    '1\\t94473807\\t.\\tC\\tT\\t100\\tPASS\\t.\\tGT\\t0/1' > /tmp/smoke.vcf
  singularity exec -B /dfs3b:/dfs3b -B /tmp:/tmp ${SIF} \\
    lirical prioritize -p HP:0000512,HP:0000662,HP:0000510 -d ${DATA_DIR} -o /tmp -x smoke_geno -f tsv \\
      --vcf /tmp/smoke.vcf --assembly hg19 -ed19 ${EXOMISER_DIR}/${EXOMISER_DATA_VERSION}_hg19 --sample-id smoke-1
  grep -v '^!' /tmp/smoke_geno.tsv | head -12   # expect ABCA4 diseases sharpened to the top

  # The exact flags are pinned to LIRICAL v${LIRICAL_VERSION}. If one is rejected, run
  # \`singularity exec ${SIF} lirical prioritize --help\` and update tools/phenotype_dx.build_lirical_cmd
  # (a runtime arg -- no image rebuild needed).
EOF

echo "== 6. Enable the phenotype line (in .env / HPCSettings) =="
cat <<EOF
  BIOAGENT_PHENOTYPE_ON_HPC=1          # route the phenotype step to the offline HPC3 LIRICAL line
  BIOAGENT_LIRICAL_IMAGE=${SIF}
  BIOAGENT_LIRICAL_DATA_DIR=${DATA_DIR}
  # genotype-aware scoring (optional -- omit for phenotype-only):
  BIOAGENT_LIRICAL_EXOMISER_HG19=${EXOMISER_DIR}/${EXOMISER_DATA_VERSION}_hg19
  BIOAGENT_LIRICAL_EXOMISER_HG38=${EXOMISER_DIR}/${EXOMISER_DATA_VERSION}_hg38
  BIOAGENT_UPLOADS_ON_HPC=1            # so the VCF lands on dfs3b and is scored in place

The bioagent TOOLS are synced to <lab_storage>/<user>/pysrc by the gateway and bind-mounted in -- a
tool edit needs only a code deploy, NOT an image rebuild. If HPC is unreachable (or LIRICAL is not
staged), the phenotype step reports not_installed and the run continues without the differential.
EOF
