#!/usr/bin/env bash
# Build the scGPT .sif (route B: vendor scGPT_refactor) and stage it on dfs3b.
# scGPT has NO ready-made image to `singularity pull` — this is a BUILD, on HPC3 (or a
# Linux+singularity host). macOS cannot build .sif.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DFS_DIR="${BIOAGENT_SCGPT_DIR:-/dfs3b/ruic20_lab/software/AiScientist/containers}"
MODEL_DIR="${BIOAGENT_SCGPT_MODEL_DIR:-/dfs3b/ruic20_lab/software/AiScientist/scgpt_model}"
SIF="${DFS_DIR}/scgpt.sif"
# Where YOUR scGPT_refactor lives (it gets copied into the build context next to the .def).
REFACTOR_SRC="${BIOAGENT_SCGPT_REFACTOR_SRC:-$HOME/scGPT_mwe/scGPT_refactor}"

echo "== 0. Stage the vendored harness into the build context =="
# The .def's %files copies ./scGPT_refactor from the build context (this dir). Bring yours in.
if [ ! -d "${HERE}/scGPT_refactor" ]; then
    [ -d "${REFACTOR_SRC}" ] || { echo "MISSING ${REFACTOR_SRC} — set BIOAGENT_SCGPT_REFACTOR_SRC"; exit 1; }
    cp -a "${REFACTOR_SRC}" "${HERE}/scGPT_refactor"
    echo "copied ${REFACTOR_SRC} -> ${HERE}/scGPT_refactor"
fi

echo "== 1. Build scgpt.sif — pick ONE route (then re-run with BUILT=1) =="
cat <<ROUTES
  (a) fakeroot build from the def (if RCIC enables --fakeroot):
        cd ${HERE} && singularity build --fakeroot scgpt.sif scgpt.def
  (b) remote builder (no local root needed):
        cd ${HERE} && singularity build --remote scgpt.sif scgpt.def
  (c) build a Docker image elsewhere -> registry -> convert on HPC3 (NO fakeroot):
        # on a machine with Docker (build context = ${HERE}):
        docker build -t <registry>/scgpt:cu121 ${HERE}
        docker push <registry>/scgpt:cu121
        # on HPC3:
        singularity build scgpt.sif docker://<registry>/scgpt:cu121
ROUTES
if [ "${BUILT:-0}" != "1" ]; then
    echo "(set BUILT=1 once ${HERE}/scgpt.sif exists to stage it)"; exit 0
fi

echo "== 2. Check the reference model weights are staged (NOT in git) =="
# Obtain best_model.pt, vocab.json, id2type.json, dev_train_args.yml and place them under
# MODEL_DIR; they are bind-mounted READ-ONLY into the job at run time (--model).
mkdir -p "${DFS_DIR}" "${MODEL_DIR}"
for f in best_model.pt vocab.json id2type.json dev_train_args.yml; do
    [ -f "${MODEL_DIR}/${f}" ] || { echo "MISSING ${MODEL_DIR}/${f} — stage the reference model first"; exit 1; }
done

echo "== 3. Place the .sif where the gateway expects it =="
cp -v "${HERE}/scgpt.sif" "${SIF}"

cat <<EOF

Done. Set these (in .env / HPCSettings) so the gateway finds the image:
  BIOAGENT_SCGPT_IMAGE=${SIF}
  BIOAGENT_SCGPT_ENTRYPOINT='python /opt/scgpt/run_infer.py'
  # model dir is passed per-run and bound read-only: ${MODEL_DIR}

Smoke test on a gpu:1 job (after weights are staged):
  singularity exec --nv \\
    -B ${MODEL_DIR}:${MODEL_DIR}:ro -B /tmp/scgpt_out:/tmp/scgpt_out \\
    ${SIF} python /opt/scgpt/run_infer.py \\
    --input <some_query.h5ad> --model ${MODEL_DIR} --out /tmp/scgpt_out
EOF
