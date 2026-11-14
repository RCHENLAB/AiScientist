#!/usr/bin/env bash
# Stage references using a pre-pulled, verified native phenotype MCP image.
# Submit this script as a Slurm compute job. It does not build application images.
set -euo pipefail
: "${SLURM_JOB_ID:?Run reference download inside an authorized Slurm allocation}"
: "${PHENOTYPE_MCP_SIF:?Set the absolute verified phenotype-diagnosis MCP SIF path}"
: "${AISCIENTIST_LIRICAL_REF_ROOT:?Set the writable reference destination}"
[[ "$PHENOTYPE_MCP_SIF" = /* && -f "$PHENOTYPE_MCP_SIF" ]] || { echo "Missing absolute MCP SIF" >&2; exit 1; }
REF_ROOT="$AISCIENTIST_LIRICAL_REF_ROOT"
DATA_DIR="$REF_ROOT/data"
EXOMISER_DIR="$REF_ROOT/exomiser"
EXOMISER_DATA_VERSION="${EXOMISER_DATA_VERSION:-2406}"
EXOMISER_BASE="${EXOMISER_BASE:-https://data.monarchinitiative.org/exomiser/latest}"
mkdir -p "$DATA_DIR"
if [[ -s "$DATA_DIR/hp.json" || -s "$DATA_DIR/hpo.json" ]]; then
    echo "LIRICAL phenotype references already present: $DATA_DIR"
else
    apptainer exec --bind "$DATA_DIR:$DATA_DIR" "$PHENOTYPE_MCP_SIF" lirical download -d "$DATA_DIR"
fi
if [[ "${EXOMISER:-0}" = 1 ]]; then
    ASM="${EXOMISER_ASSEMBLY:-hg19}"
    [[ "$ASM" = hg19 || "$ASM" = hg38 ]] || { echo "Assembly must be hg19 or hg38" >&2; exit 1; }
    [[ "$EXOMISER_DATA_VERSION" =~ ^[0-9]{4}$ ]] || { echo "Invalid Exomiser version" >&2; exit 1; }
    mkdir -p "$EXOMISER_DIR"
    name="${EXOMISER_DATA_VERSION}_${ASM}"
    dest="$EXOMISER_DIR/$name"
    if [[ ! -s "$dest/${name}_variants.mv.db" ]]; then
        curl -fSL --retry 3 -o "$EXOMISER_DIR/$name.zip" "$EXOMISER_BASE/$name.zip"
        unzip -q "$EXOMISER_DIR/$name.zip" -d "$EXOMISER_DIR"
        rm -f "$EXOMISER_DIR/$name.zip"
    fi
    echo "Optional genotype references: $dest"
fi
printf 'Set LIRICAL_DATA_DIR=%s\n' "$DATA_DIR"
printf 'Set PHENOTYPE_ALLOWED_ROOTS=%s\n' "$REF_ROOT"
echo "The application uses the registered phenotype MCP image; no app scientific source is mounted."
