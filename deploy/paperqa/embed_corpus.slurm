#!/bin/bash
#SBATCH --job-name=retigene_embed
#SBATCH --partition=standard              # RCIC HPC3 free CPU partition (no --gres)
#SBATCH --account=ruic20_lab
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=08:00:00
#SBATCH --output=%x-%j.log
#
# Build the PaperQA PubMedBERT embedding index for the full RetiGene corpus on HPC3.
#
# This is a CPU job. The embedding model is a LOCAL sentence-transformers model
# (st-NeuML/pubmedbert-base-embeddings — biomedical, 768-dim), so no cloud API is
# called; the indexer points any accidental LLM call at a dead local endpoint (see
# scripts/build_paperqa_directory_index.py).
#
# Edit the four paths in the CONFIG block, or override them with environment
# variables when you submit:
#   sbatch --export=ALL,BASE=/dfs3b/ruic20_lab/$USER/retigene deploy/paperqa/embed_corpus.slurm
#
# Prereqs (do these ONCE, see deploy/paperqa/README.md):
#   1. a conda/venv env with `paper-qa[local]` + sentence-transformers  -> $PQA_ENV
#   2. the corpus (papers/ + paperqa_manifest.csv) rsync'd under $BASE
#   3. the PubMedBERT weights pre-staged into $HF_HOME (login node has internet)

set -euo pipefail

# ---------------------------------------------------------------- CONFIG ----
# Repo checkout on HPC3 (contains scripts/build_paperqa_directory_index.py):
REPO="${REPO:-/dfs3b/ruic20_lab/$USER/BioAgentPrototype}"
# Data root on DFS (holds papers/ and paperqa_manifest.csv):
BASE="${BASE:-/dfs3b/ruic20_lab/$USER/retigene}"
PAPERS="${PAPERS:-$BASE/papers}"
MANIFEST="${MANIFEST:-$BASE/paperqa_manifest.csv}"
INDEX_DIR="${INDEX_DIR:-$BASE/index_pubmedbert}"
# Python environment with paper-qa[local] installed:
PQA_ENV="${PQA_ENV:-$HOME/.conda/envs/paperqa}"
# Embedding model — biomedical PubMedBERT (768-dim). Set per advisor: go straight to
# the high-level domain model, skip the small MiniLM pilot. Override with EMBEDDING= if
# you ever want the fast MiniLM (st-multi-qa-MiniLM-L6-cos-v1) instead.
EMBEDDING="${EMBEDDING:-st-NeuML/pubmedbert-base-embeddings}"
INDEX_NAME="${INDEX_NAME:-retigene_full_pubmedbert}"
# ----------------------------------------------------------------------------

# Hugging Face cache on DFS so model weights persist and compute nodes can read
# them offline after a one-time login-node download.
export HF_HOME="${HF_HOME:-$BASE/hf_cache}"
export SENTENCE_TRANSFORMERS_HOME="$HF_HOME"
export TOKENIZERS_PARALLELISM=false
# Fully offline: compute nodes have no internet. HF_HUB_OFFLINE alone is NOT enough —
# transformers still tries a network HEAD call for the model config unless
# TRANSFORMERS_OFFLINE is also set, which fails on a no-internet node even when the
# model IS pre-cached. Set BOTH. Comment out only for the very first run if the model
# is not staged yet (login node has internet).
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"

echo "[embed] node=$(hostname) start=$(date -Is)"
echo "[embed] REPO=$REPO"
echo "[embed] PAPERS=$PAPERS  ($(find "$PAPERS" -maxdepth 1 -iname '*.pdf' | wc -l) PDFs)"
echo "[embed] MANIFEST=$MANIFEST"
echo "[embed] INDEX_DIR=$INDEX_DIR  EMBEDDING=$EMBEDDING  HF_HOME=$HF_HOME"

module load anaconda/2024.06 2>/dev/null || module load miniconda3 2>/dev/null || true
# shellcheck disable=SC1091
source activate "$PQA_ENV" 2>/dev/null || conda activate "$PQA_ENV"

cd "$REPO"
python scripts/build_paperqa_directory_index.py \
  --papers   "$PAPERS" \
  --manifest "$MANIFEST" \
  --index-dir "$INDEX_DIR" \
  --embedding "$EMBEDDING" \
  --name "$INDEX_NAME" \
  --concurrency 4

echo "[embed] done=$(date -Is)"
echo "[embed] index written to $INDEX_DIR"
