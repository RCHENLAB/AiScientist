# Phase 2 — In-place HPC3 Compute (Slurm)

**Goal (Jin's route):** run heavy analysis **on HPC3** via Slurm, *in place* on
`dfs3b` data — no data transfer. The eye server only orchestrates and collects
the small derived results. Qwen3.6 (HPC3 GPU) still writes the report.

This replaces Phase 1, where the analysis tools ran locally on the eye server.

## 1. Architecture / data flow

```
User (web, public) → eye server (gateway + agent coordinator)
   │  SSH (paramiko, already in place)
   ▼
HPC3 login node
   ├─ write analysis script + sbatch script  → /dfs3b/ruic20_lab/<user>/.bioagent/jobs/<run_id>/
   ├─ sbatch  (CPU: ruic20_lab / GPU: ruic20_lab_gpu)
   ├─ poll squeue → sacct until done
   │     compute node runs analysis IN PLACE:
   │        reads  /dfs3b/ruic20_lab/<user>/data/<dataset>
   │        writes /dfs3b/ruic20_lab/<user>/.bioagent/results/<run_id>/
   └─ pull back ONLY small derived results (JSON / MD / PNG)
        → eye server /data/BioAgent/<user>/<run_id>/   (served to UI)
   ▼
Qwen3.6 (HPC3 GPU) summarizes derived results → report
   ▼
UI: job status (queued → running → done) + downloads
```

Key boundary: **raw data + big intermediates stay on HPC3**; only small derived
artifacts (a few MB) come to the eye server for the UI and the LLM summary.

## 2. What changes vs Phase 1

| | Phase 1 (now) | Phase 2 (this design) |
| --- | --- | --- |
| Heavy analysis runs on | eye server CPU | **HPC3 compute node (Slurm)** |
| Data location | eye server / small subset | **HPC3 dfs3b (in place)** |
| Transferred to eye server | the dataset | **only small results** |
| `HPCAgent` | writes dry-run script only | **submits + monitors + collects** (gated) |
| Big datasets / GPU analysis | not feasible | feasible |

## 3. Components to build

1. **`SlurmRunner`** (new, in `gateway/` or `hpc/`): over the existing SSH
   connection — write job files, `sbatch`, poll `squeue`/`sacct`, collect
   results, surface queue waits/failures. Reuses the exact submit→poll pattern
   already working in `gateway/gpu.py` (the Ollama serve job).
2. **Analysis job templates** — standalone, parameterized scripts that run on a
   compute node: e.g. `scanpy_qc.py --input <dfs3b path> --out <results dir>`,
   `de_enrichment.R/​py ...`. These are the "real" versions of today's bounded
   `tools/execution.py` smoke analyses.
3. **Pipeline integration** — `HPCAgent` gains a `submit` mode (enabled by a
   flag) that: renders the analysis + sbatch scripts, submits, waits, pulls
   results into the run workspace; `ResearchEvaluationAgent` / `ReporterAgent`
   then consume the real results.
4. **UI job status** — stream `queued → running (node) → done/failed` to the
   log + a compact status chip; results appear in the existing Downloads panel.

## 4. Slurm job lifecycle (what `SlurmRunner` does)

1. `mkdir` job + results dirs under the user's dfs3b area (with `sg ruic20_hpc`).
2. Write `analysis.py` (the template) and `job.sbatch` (heredoc over SSH).
3. `sbatch job.sbatch` → capture job id.
4. Poll `squeue -j <id>` → states `PD` (pending, show queue notice) → `R`
   (running on node) → gone; then `sacct -j <id>` for the final state
   (`COMPLETED` / `FAILED` / `TIMEOUT` / `OUT_OF_MEMORY`).
5. On success: read the small result files back (exec `cat` / SFTP) into the run
   workspace; on failure: surface the job log (`results/<run_id>/slurm-<id>.out`)
   with full cause.

The existing **queue-wait / fallback policy** (`SlurmAdapter.build_queue_fallback_plan`)
already encodes the "GPU queue is long → checkpoint / notify / safe options"
behaviour — wire it into the polling step.

## 5. Partitions / accounts

| Workload | Partition | Account | Notes |
| --- | --- | --- | --- |
| CPU analysis (scanpy QC, DE) | _CPU partition_ (confirm name via `sinfo`) | `ruic20_lab` | most analyses |
| GPU analysis (scVI, large embeddings) | `gpu` | `ruic20_lab_gpu` | `--gres=gpu:1`, ≥24GB card |

`dfs3b` has ~44 TiB free (600 TiB group quota, 93% used) — enough for results,
but coordinate with the PI before staging very large new datasets.

## 6. Safety / guardrails (keep Phase 1's boundaries)

- Real `sbatch` is **opt-in** and human-reviewable (keep `slurm_readiness.md`).
- All paths scoped to the user's own dfs3b dir; no destructive ops outside it.
- Data never leaves UCI (HPC3 ↔ eye server, both UCI).
- Queue-wait + fallback plan instead of silently moving heavy work to the eye
  server (no GPU there anyway).

## 7. Prerequisites — small, no Jin needed

The current analysis tools (`tools/datasets.py`, `tools/execution.py`) import
**only the Python standard library + h5py** — no scanpy/numpy/scipy. So the HPC3
environment is trivial and built via Lmod `module load`, not a custom conda env:

```bash
module load anaconda      # or whatever provides Python 3 (check: module avail)
python analysis.py --input <dfs3b path> --out <results dir>
```

h5py (the one dependency, already in `requirements.txt`) ships with most
anaconda modules; if not: `pip install --user h5py` or a tiny venv on dfs3b.
**<ucinetid> can do this — Jin is not required.**

To confirm, run on HPC3: `module avail 2>&1 | grep -iE "anaconda|python|mamba"`
and tell me the exact module name to load.

Only **if** we later scale up to real heavy analysis (scanpy clustering, scVI,
R/DESeq2) would a bigger env be worth building — and even then a self-built
conda env on dfs3b works without Jin. That is a future scope choice, not a
blocker for Phase 2 with the current tools.

Still to confirm (quick, self-serve):
1. The Python module name (`module avail`).
2. **CPU partition name** (we know `gpu`; confirm via `sinfo -s`).
3. Result-dir convention (proposed `/dfs3b/ruic20_lab/<user>/.bioagent/results/<run_id>/`).

## 8. Implementation plan

| Step | What | Needs HPC3? |
| --- | --- | --- |
| **2a** | `SlurmRunner` (submit / poll / collect) + mock simulator | No — build + test offline now |
| **2b** | Analysis job templates (scanpy_qc, de) runnable standalone | No (logic), Yes (validate) |
| **2c** | Wire `HPCAgent` submit-mode into the pipeline | partial |
| **2d** | UI job-status panel | No |
| **2e** | Confirm Python module name + validate end-to-end on cluster | **Yes** (quick) |

I can build **2a, 2b, 2d** now against the mock host (the same way the Ollama
serve job was built and verified). The only real-cluster step (2e) is confirming
the `module load` name and a validation run — no environment build, no Jin.
