#!/bin/bash
#SBATCH --job-name=retigene_embed
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=08:00:00
#SBATCH --output=%x-%j.log
# Supply --account=<account> --partition=<cpu-partition> when submitting.
# Operator-prepared native MCP checkout and immutable literature SIF; no app scientific code.
set -euo pipefail
: "${SLURM_JOB_ID:?Submit with sbatch}"
: "${MCP_REPO:?Set a checkout of the independently versioned aiscientist-mcp repository}"
: "${LITERATURE_MCP_SIF:?Set the verified published literature SIF}"
: "${BASE:?Set the corpus root}"
PAPERS="${PAPERS:-$BASE/papers}"
MANIFEST="${MANIFEST:-$BASE/paperqa_manifest.csv}"
INDEX_DIR="${INDEX_DIR:-$BASE/index_pubmedbert}"
EMBEDDING="${EMBEDDING:-st-NeuML/pubmedbert-base-embeddings}"
INDEX_NAME="${INDEX_NAME:-retigene_full_pubmedbert}"
export HF_HOME="${HF_HOME:-$BASE/hf_cache}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
mkdir -p "$INDEX_DIR"
apptainer exec --bind "$MCP_REPO/scripts:/operator:ro" --bind "$BASE:$BASE" \
    "$LITERATURE_MCP_SIF" python /operator/build_paperqa_directory_index.py \
    --papers "$PAPERS" --manifest "$MANIFEST" --index-dir "$INDEX_DIR" \
    --embedding "$EMBEDDING" --name "$INDEX_NAME" --concurrency 4
