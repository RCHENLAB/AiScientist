#!/usr/bin/env bash
# Idempotent staging of the VEP predictor databases into the lab's shared annotation-DB directory,
# to be bind-mounted read-only into vep.sif (Jin Li's pattern: download each DB ONCE, check whether it
# already exists, only fetch the missing ones). Re-running is safe: anything already present is skipped.
#
#   !! WHERE TO RUN THIS !!  RCIC (2026-08-06) reserves the login nodes for logging in and submitting
#   Slurm jobs: no compute, and NO data transfer — rsync/SFTP/rclone/wget/curl belong on
#   access-hpc3.rcic.uci.edu, and they may kill offending login-node processes. This script pulls up
#   to ~90GB (CADD alone is 87GB), so do NOT run it on a login node. Submit it (compute nodes on
#   `standard` have outbound egress, verified 2026-07-08):
#     sbatch -p standard -A ruic20_lab -c 4 --mem=16G -t 24:00:00 --wrap \
#       "DB_ROOT=/dfs3b/ruic20_lab/software/reference/vep_annotation/plugins $PWD/stage_annotation_dbs.sh"
#   (access-hpc3 allows wget/curl/rsync but NOT bash, so this SCRIPT cannot run there — only plain
#   download commands can. It also clones a git repo, which the restricted shell would refuse.)
#
# Toggle individual DBs with STAGE_<NAME>=0 (default 1). CADD is 87 GB — set STAGE_CADD=0 to skip it
# (pending the keep/drop decision). Point BIOAGENT_VEP_* at these paths in prod .env once staged.
set -uo pipefail

DB_ROOT="${DB_ROOT:-/dfs3b/ruic20_lab/software/reference/vep_annotation/plugins}"
REF_ROOT="${REF_ROOT:-/dfs3b/ruic20_lab/software/reference/vep_annotation/ref}"
VEP_RELEASE="${VEP_RELEASE:-112}"
mkdir -p "$DB_ROOT"/{alphamissense,cadd,revel} "$REF_ROOT"

have() { [[ -s "$1" ]]; }                       # non-empty file exists
say()  { printf '== %s\n' "$*"; }
fetch() {  # fetch URL DEST — skip if DEST already exists, else download atomically
  local url="$1" dest="$2"
  if have "$dest"; then say "skip (present): $(basename "$dest")"; return 0; fi
  say "download: $(basename "$dest")"
  curl -fSL --retry 3 -o "$dest.part" "$url" && mv "$dest.part" "$dest"
}

# --- VEP plugin scripts (.pm) — bind-mounted via --dir_plugins, so NO vep.sif rebuild -------------
# vep.sif ships zero plugin scripts; VEP loads them from --dir_plugins, so we just clone the repo once.
if [[ "${STAGE_PLUGINS_DIR:-1}" == 1 ]]; then
  PDIR="$DB_ROOT/vep_plugins"
  if [[ -f "$PDIR/CADD.pm" ]]; then say "skip (present): VEP_plugins"; else
    say "clone Ensembl VEP_plugins (release-$VEP_RELEASE)"
    rm -rf "$PDIR.tmp"
    git clone --depth 1 -b "release/$VEP_RELEASE" https://github.com/Ensembl/VEP_plugins.git "$PDIR.tmp" \
      && mv "$PDIR.tmp" "$PDIR" || say "WARN: VEP_plugins clone failed"
  fi
fi

# --- AlphaMissense (hg38): ~0.6 GB, then tabix ------------------------------------------------------
if [[ "${STAGE_ALPHAMISSENSE:-1}" == 1 ]]; then
  AM="$DB_ROOT/alphamissense/AlphaMissense_hg38.tsv.gz"
  fetch "https://zenodo.org/records/8208688/files/AlphaMissense_hg38.tsv.gz" "$AM"
  if have "$AM" && ! have "$AM.tbi"; then
    say "tabix AlphaMissense"; tabix -s 1 -b 2 -e 2 -f -S 1 "$AM" || say "WARN: tabix failed (need htslib)"
  fi
fi

# --- CADD (GRCh38 whole-genome SNVs): 87 GB, ships bgzipped + tabixed ------------------------------
if [[ "${STAGE_CADD:-1}" == 1 ]]; then
  base="https://kircherlab.bihealth.org/download/CADD/v1.7/GRCh38"
  fetch "$base/whole_genome_SNVs.tsv.gz"     "$DB_ROOT/cadd/whole_genome_SNVs.tsv.gz"
  fetch "$base/whole_genome_SNVs.tsv.gz.tbi" "$DB_ROOT/cadd/whole_genome_SNVs.tsv.gz.tbi"
fi

# --- REVEL (GRCh38): small, needs a reformat into the VEP-plugin tabixed layout --------------------
if [[ "${STAGE_REVEL:-1}" == 1 ]]; then
  OUT="$DB_ROOT/revel/new_tabbed_revel_grch38.tsv.gz"
  if have "$OUT"; then say "skip (present): $(basename "$OUT")"; else
    say "build REVEL (download + reformat to GRCh38-tabbed)"
    tmp="$DB_ROOT/revel/_revel.zip"
    fetch "https://rothsj06.dmz.hpc.mssm.edu/revel-v1.3_all_chromosomes.zip" "$tmp"
    ( cd "$DB_ROOT/revel" && unzip -o _revel.zip >/dev/null &&
      # VEP REVEL-plugin recipe (GRCh38): comma->tab; COMMENT the header with '#' so tabix exposes it
      # (else the plugin errors "Could not read headers"); DROP rows with no GRCh38 position
      # (grch38_pos='.', col 3) so the position sort/index is valid; index on chr(1) + grch38_pos(3).
      { head -n1 revel_with_transcript_ids | tr ',' '\t' | sed 's/^/#/' ; \
        tail -n +2 revel_with_transcript_ids | tr ',' '\t' | awk -F'\t' '$3!="."' | sort -k1,1 -k3,3n ; } \
        | bgzip -c > new_tabbed_revel_grch38.tsv.gz &&
      tabix -f -s 1 -b 3 -e 3 new_tabbed_revel_grch38.tsv.gz ) \
      || say "WARN: REVEL build failed — check the download URL / column layout for this REVEL version"
  fi
fi

# --- Reference genome FASTA (for bcftools norm + VEP --hgvs) ---------------------------------------
if [[ "${STAGE_REF:-1}" == 1 ]]; then
  FA="$REF_ROOT/Homo_sapiens.GRCh38.dna.primary_assembly.fa"
  if have "$FA"; then say "skip (present): $(basename "$FA")"; else
    say "download + unpack GRCh38 reference FASTA (~3 GB)"
    fetch "https://ftp.ensembl.org/pub/release-${VEP_RELEASE}/fasta/homo_sapiens/dna/Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz" "$FA.gz" &&
      gunzip -f "$FA.gz"
  fi
  have "$FA" && ! have "$FA.fai" && { say "samtools faidx"; samtools faidx "$FA" || say "WARN: faidx failed (need samtools)"; }
fi

# --- SpliceAI (OpenSpliceAI) — conda env + PyTorch model ensemble ---------------------------------
# OpenSpliceAI runs from its OWN conda env (torch; not in vep.sif) but is exec'd INSIDE vep.sif at run
# time (its conda-forge python runs under the container's glibc — verified). ~50 s/variant on CPU, so
# it is a panel-stage tool. Set STAGE_SPLICEAI=0 to skip.
if [[ "${STAGE_SPLICEAI:-1}" == 1 ]]; then
  SA_ENV="${SA_ENV:-/dfs3b/ruic20_lab/software/AiScientist/envs/openspliceai}"
  SA_MODELS="${SA_MODELS:-/dfs3b/ruic20_lab/software/reference/spliceai/OSAI-MANE-10000nt}"
  if [[ -x "$SA_ENV/bin/openspliceai" ]]; then say "skip (present): openspliceai env"; else
    say "create conda env + pip install openspliceai (conda-forge; pulls torch)"
    export CONDA_PKGS_DIRS="${CONDA_PKGS_DIRS:-/dfs3b/ruic20_lab/software/AiScientist/.conda-pkgs}"
    module load miniconda3/25.11.1 2>/dev/null || true
    conda create -y -p "$SA_ENV" -c conda-forge --override-channels python=3.10 pip \
      && "$SA_ENV/bin/pip" install --no-input openspliceai || say "WARN: openspliceai env build failed"
  fi
  # OSAI-MANE-10000nt is a 5-model ensemble on the JHU CCB FTP (https to that host is blocked; ftp works)
  mkdir -p "$SA_MODELS"
  SA_FTP="ftp://ftp.ccb.jhu.edu/pub/data/OpenSpliceAI/OSAI-MANE/10000nt"
  for rs in 10 11 12 13 14; do
    fetch "$SA_FTP/model_10000nt_rs${rs}.pt" "$SA_MODELS/model_10000nt_rs${rs}.pt"
  done
fi

say "done. Present under $DB_ROOT and $REF_ROOT:"
du -sh "$DB_ROOT"/* "$REF_ROOT"/* 2>/dev/null || true
cat <<EOF

Set in prod .env (point BIOAGENT_VEP_* at what got staged):
  BIOAGENT_VEP_PLUGINS=1
  BIOAGENT_VEP_PLUGINS_DIR=$DB_ROOT/vep_plugins
  BIOAGENT_VEP_ALPHAMISSENSE=$DB_ROOT/alphamissense/AlphaMissense_hg38.tsv.gz
  BIOAGENT_VEP_CADD_SNV=$DB_ROOT/cadd/whole_genome_SNVs.tsv.gz
  BIOAGENT_VEP_REVEL=$DB_ROOT/revel/new_tabbed_revel_grch38.tsv.gz
  BIOAGENT_REF_FASTA=$REF_ROOT/Homo_sapiens.GRCh38.dna.primary_assembly.fa
  BIOAGENT_SPLICEAI=1
  BIOAGENT_SPLICEAI_BIN=${SA_ENV:-/dfs3b/ruic20_lab/software/AiScientist/envs/openspliceai}/bin/openspliceai
  BIOAGENT_SPLICEAI_MODELS=${SA_MODELS:-/dfs3b/ruic20_lab/software/reference/spliceai/OSAI-MANE-10000nt}
EOF
