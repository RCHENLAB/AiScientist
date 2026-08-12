#!/usr/bin/env bash
# Build the PaperQA .sif and stage it on dfs3b, where the gateway's offload executor finds it.
# Mirrors deploy/scgpt/build_and_stage.sh. PaperQA has NO ready-made image to `singularity pull`
# (paper-qa[local] is a pip install), so this is a BUILD, on HPC3 (macOS cannot build .sif).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DFS_DIR="${BIOAGENT_PAPERQA_CONTAINER_DIR:-/dfs3b/ruic20_lab/software/AiScientist/containers}"
SIF="${DFS_DIR}/paperqa.sif"

echo "== 1. Build paperqa.sif — pick ONE route (then re-run with BUILT=1) =="
# The bioagent source + the corpus/model are bind-mounted at RUN time (not baked in), so unlike the
# scgpt image there is NO vendored code to stage into the build context first.
cat <<ROUTES
  (a) fakeroot build from the def (if RCIC enables --fakeroot):
        cd ${HERE} && singularity build --fakeroot paperqa.sif paperqa.def
  (b) remote builder (no local root needed):
        cd ${HERE} && singularity build --remote paperqa.sif paperqa.def
  (c) build a Docker image elsewhere -> registry -> convert on HPC3 (NO fakeroot):
        # on a machine with Docker (translate paperqa.def to a Dockerfile: same base + the one
        # pip install), then:
        docker build -t <registry>/paperqa:cpu .
        docker push <registry>/paperqa:cpu
        # on HPC3:
        singularity build paperqa.sif docker://<registry>/paperqa:cpu
ROUTES
if [ "${BUILT:-0}" != "1" ]; then
    echo "(set BUILT=1 once ${HERE}/paperqa.sif exists to stage it)"; exit 0
fi

echo "== 2. Place the .sif where the gateway expects it =="
mkdir -p "${DFS_DIR}"
cp -v "${HERE}/paperqa.sif" "${SIF}"

cat <<EOF

Done. Set this (in .env / HPCSettings) so the gateway finds the image:
  BIOAGENT_PAPERQA_IMAGE=${SIF}

The corpus INDEX + PubMedBERT model are bind-mounted read-only per run (NOT baked into the image):
  BIOAGENT_PAPERQA_INDEX_DIR=/dfs3b/ruic20_lab/<user>/retigene/index_pubmedbert
  BIOAGENT_PAPERQA_PAPERS=/dfs3b/ruic20_lab/<user>/retigene/papers
  BIOAGENT_PAPERQA_MANIFEST=/dfs3b/ruic20_lab/<user>/retigene/paperqa_manifest.csv
  HF_HOME=/dfs3b/ruic20_lab/<user>/retigene/hf_cache            # holds the embedding model
  BIOAGENT_PAPERQA_EMBEDDING=st-<HF_HOME>/hub/models--NeuML--pubmedbert-base-embeddings/snapshots/<hash>

Smoke test (bioagent src + retigene data bind-mounted; Qwen reachable at --args.llm_base_url).
Write /tmp/pq_args.json with question/model/llm_base_url/embedding/index_dir/papers/manifest, then:
  singularity exec \\
    -B <repo>/src:/opt/bioagent-src:ro \\
    -B /dfs3b/ruic20_lab/<user>/retigene:/dfs3b/ruic20_lab/<user>/retigene:ro \\
    --env PYTHONPATH=/opt/bioagent-src \\
    --env HF_HOME=/dfs3b/ruic20_lab/<user>/retigene/hf_cache \\
    ${SIF} python3 -m bioagent.tools.paperqa_cli \\
    --tool deep_literature --workspace /tmp --args /tmp/pq_args.json
EOF
