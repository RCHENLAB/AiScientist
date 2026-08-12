# scGPT container — build & stage (route B: vendor the refactor)

Deployment artifacts for the scGPT step-1+2 GPU batch job (Route C in
`docs/scgpt_workflow_integration.md`). The orchestration engine that *runs* the image is
already built + tested (`src/bioagent/gateway/scgpt_job.py`, offline-tested via a
`RemoteExecutor` mock — no GPU in CI). This folder is the **image build kit**.

> ⚠️ There is **no maintained scGPT container to `singularity pull`** — this is a **build**,
> and it must run **on HPC3** (or a Linux + Singularity host). **macOS cannot build a `.sif`.**

## Approach: vendor, don't reimplement

The image bundles your **validated** `scGPT_refactor` harness and runs `step1_preprocess.py`
+ `step2_inference.py` **unchanged**. `run_infer.py` is only a thin wrapper that arranges the
directory layout those scripts hardcode (`../download/{query.h5ad,reference_model}`) and
copies their `predictions.csv` out. Single source of truth = your refactor code; no risk of
a subtly-wrong reimplementation. `scgpt`/`torch` live ONLY in the image, never in the
gateway's Python env.

## Files
- `run_infer.py` — in-container entrypoint (`--input/--model/--out` → `predictions.csv`).
- `scgpt.def` — Singularity definition: torch-2.3.0 CUDA base, pip `scgpt` + the reference
  deps, `%files`-copies your `scGPT_refactor` + `run_infer.py` into `/opt/scgpt`.
- `build_and_stage.sh` — copy your harness into the build context, build the `.sif` (3
  routes), verify weights, place the image at the dfs3b path the gateway reads.

## Prerequisites (do these first)
1. **CUDA / driver** — `nvidia-smi` on an HPC3 gpu node; confirm the driver supports the
   base image's CUDA (12.1). Bump the `From:` tag if not.
2. **Build route** — confirm how you can build on/near HPC3: `--fakeroot` (if RCIC enables
   it), `--remote`, or Docker-elsewhere → `singularity build docker://…` (no fakeroot).
3. **Reference model** — obtain `best_model.pt`, `vocab.json`, `id2type.json`,
   `dev_train_args.yml` and stage them under `BIOAGENT_SCGPT_MODEL_DIR` on dfs3b (big; not
   in git). Bound **read-only** per run as `--model`.
4. **Your harness** — point `BIOAGENT_SCGPT_REFACTOR_SRC` at your `scGPT_mwe/scGPT_refactor`
   (the build script copies it into the context). Confirm `scgpt`/`torchtext` resolve
   cleanly against torch 2.3.0; pin in `scgpt.def %post` if your refactor needs a specific
   combo.

## Build + stage
```bash
# copies your scGPT_refactor into the context and prints the build routes:
./deploy/scgpt/build_and_stage.sh
# build with the route you picked (e.g. on HPC3):
cd deploy/scgpt && singularity build --fakeroot scgpt.sif scgpt.def && cd -
# stage the image (weights must already be under BIOAGENT_SCGPT_MODEL_DIR):
BUILT=1 ./deploy/scgpt/build_and_stage.sh
```
Then set `BIOAGENT_SCGPT_IMAGE` / `BIOAGENT_SCGPT_ENTRYPOINT` (printed by the script). The
gateway's `run_scgpt_inference` submits a `gpu:1` job that `singularity exec --nv`s this
image, waits to COMPLETED, and reads `predictions.csv` back.

## Not committed to git
- `scGPT_refactor/` inside this folder (copied at build time) and `scgpt.sif` are build
  artifacts — keep them out of the repo (see `.gitignore`).
- The model weights are never in git; they live on dfs3b.
