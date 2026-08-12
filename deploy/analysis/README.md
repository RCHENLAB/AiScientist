# Analysis container — build & stage (CPU CodeAct image)

Build kit for `analysis.sif` — the CPU Singularity image that AiScientist runs each `run_code`
(CodeAct) snippet inside **when `BIOAGENT_RUN_CODE_ON_HPC=1`**. The orchestration engine that
*runs* the image is already built + offline-tested (`src/bioagent/gateway/slurm_sandbox.py`,
driven by a `RemoteExecutor` mock — no cluster in CI). This folder is the **image build kit**.

> ⚠️ macOS cannot build `.sif`. Build on **HPC3** or any Linux host with `singularity`.

## Why this exists

The local `CodeSandbox` (eyeserver) runs snippets as an **uncapped** subprocess → a memory-heavy
snippet gets OOM-killed (`returncode -9`). On HPC3, CPU/RAM are ~unlimited and `#SBATCH --mem` is a
**real cgroup cap**. `SlurmCodeExecutor` submits each snippet as a contained CPU batch job in this
image. It is CPU-only (scanpy/pandas/gseapy) — **no GPU/torch** (that's `scgpt.sif`).

## Build & stage (3 steps)

```bash
# on HPC3 (or a Linux+singularity host), from a checkout of this repo:
cd deploy/analysis

# 1. build the image — pick a route the script prints (fakeroot / --remote / docker→convert)
singularity build --fakeroot analysis.sif analysis.def      # if RCIC enables --fakeroot
#   ...or `singularity build --remote analysis.sif analysis.def`

# 2. stage it onto shared DFS where the gateway looks for it
BUILT=1 ./build_and_stage.sh
#   -> copies analysis.sif to /dfs3b/ruic20_lab/software/AiScientist/containers/analysis.sif

# 3. smoke-test on a CPU node
singularity exec --containall --writable-tmpfs --net --network none \
  /dfs3b/ruic20_lab/software/AiScientist/containers/analysis.sif \
  python -c "import scanpy, gseapy, pandas; print('ok', scanpy.__version__)"
```

## Enable it

Set these in the gateway env (`.env` / `HPCSettings`) — until `BIOAGENT_RUN_CODE_ON_HPC=1`,
`run_code` stays on the local sandbox and this image is unused:

```
BIOAGENT_RUN_CODE_ON_HPC=1
BIOAGENT_ANALYSIS_IMAGE=/dfs3b/ruic20_lab/software/AiScientist/containers/analysis.sif
BIOAGENT_CPU_PARTITION=standard        # RCIC HPC3 free CPU partition (no GPU)
BIOAGENT_CPU_ACCOUNT=ruic20_lab
BIOAGENT_RUN_CODE_MEM_GB=64            # real per-snippet memory cap
```

The dataset + the run's work/artifacts dirs must be reachable on the compute node (shared DFS). If
they are not the same paths as on the eyeserver, point the executor at the HPC3 paths with
`BIOAGENT_HPC_DATASET` / `BIOAGENT_HPC_WORK` / `BIOAGENT_HPC_ARTIFACTS`. If HPC is unreachable at
run time, `run_code` falls back to the local sandbox automatically.

## Keep in sync

The pinned packages in `analysis.def` mirror the `analysis` extra in `pyproject.toml` (plus
`scikit-misc`, `igraph`, `psutil`). When you bump one, bump the other so a snippet behaves the same
locally and on HPC3.
