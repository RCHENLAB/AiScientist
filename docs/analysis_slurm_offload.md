# Research: run the scanpy analysis line as HPC3 Slurm jobs

**Branch:** `feat/scanpy-slurm-offload` · **Status:** research/design (no code yet) · 2026-07-02

Goal: move the single-cell **analysis line** (`run_scanpy_qc` / `run_clustering` / `run_de` /
`run_enrichment`) off the eyeserver's in-process CPU and onto **HPC3 as Slurm batch jobs**, so the
gateway host stops being the concurrency bottleneck.

## Why (the motivation)

The analysis tools currently execute **in-process on the eyeserver** (see below). Under lab-wide
concurrent use, N heavy runs (PCA / Leiden / DE / gseapy) compete for that one host's CPU+RAM —
this is the real concurrency ceiling, not the web layer or SSH. Two principles point the same way:

- **Compute placement is the scaling lever** — offloading analysis to HPC3 frees the gateway to
  stay I/O-bound and serve many concurrent sessions.
- **Data/compute co-location** — heavy matrices + heavy compute both belong on HPC3/dfs3b.

This is the previously-deferred "analysis-as-HPC3-Slurm-job" work. It is the CPU-analysis sibling
of what already exists for `run_code` (opt-in Slurm), scGPT (GPU batch job), and vlreview.

## Current state (grounded)

| Piece | Where | How it runs today |
|---|---|---|
| **Scanpy tools** | `src/bioagent/tools/scrna_pack.py` (`scrna_catalog()`, lines 133–491) | **In-process on eyeserver**, `sys.executable`, **no resource cap**. A **checkpoint chain**: QC → `work/adata_qc.h5ad` → clustering → `work/adata_clustered.h5ad` → DE → `work/adata_de.h5ad`; enrichment reads `tables/de_leiden_all.csv`. Returns **derived metrics only**; writes figures/tables to `artifacts/`. |
| **run_code (CodeAct)** | `agents/sandbox.py` `CodeSandbox` | Local subprocess (throwaway tmpdir, CPU rlimit + 180s timeout). **Already has an HPC path** (below). |
| **run_code on HPC (opt-in)** | `gateway/slurm_sandbox.py` `SlurmCodeExecutor` | Stages the snippet → sbatch inside `analysis.sif` → `#SBATCH --mem` cap → reads stdout/artifacts back. Gated by `BIOAGENT_RUN_CODE_ON_HPC`; **falls back** to `CodeSandbox` on failure. |
| **scGPT / vlreview** | `gateway/scgpt_job.py`, `vlreview_job.py` (+ `*_runner.py`) | Always a dedicated batch job: stage inputs → `sbatch` → poll → read one output file. **The exact tool-shaped pattern we need.** |
| **Batch-job engine** | `gateway/slurm_job.py` | `singularity_exec()`, `build_analysis_script()`, `acquire_allocation()` (submit + startup-retry×3), `run_batch_job()` (submit→RUNNING→terminal state). |
| **Analysis image** | `deploy/analysis/analysis.def` → staged `analysis.sif` | Bundles scanpy≥1.10 / anndata / gseapy / leidenalg / igraph / scikit-misc / matplotlib(Agg). Path in `settings.analysis_image` = `/dfs3b/ruic20_lab/software/bioagent/containers/analysis.sif`. |
| **Wiring** | `gateway/app.py` (~1455–1483), `agents/registry.py` `build_scientist_catalog`, `gateway/settings.py` | Catalog is built once with injected executors; `run_code_on_hpc` chooses local vs Slurm for run_code. `RemoteExecutor` protocol (`gateway/executor.py`) — real `SSHExecutor` / `MockExecutor` for offline tests. |

**The gap:** the scanpy tools have **no HPC path and no switch** — they always run in-process. Every
other reusable building block already exists.

## Design (proposed)

### 1. Keep the four tool functions as the single source of truth

Don't fork the analysis logic. On the HPC path, **the same `run_scanpy_qc`/… functions run inside
`analysis.sif` on HPC3** instead of in the gateway process. That needs a small **container
entrypoint** that can call a named tool against a workspace:

```
# inside analysis.sif, on the compute node:
python -m bioagent.tools.scrna_cli <tool_name> --workspace <dfs_run_dir> --args-json <path>
```

**Key open question — how does `scrna_pack` get imported in the container?** Two options:
- **(A) Bake** a minimal `bioagent` (just `tools/scrna_pack.py` + deps) into `analysis.sif` at build
  time (extend `deploy/analysis/analysis.def` + `build_and_stage.sh`). Clean, versioned with the
  image; rebuild+restage on tool changes.
- **(B) Bind-mount / stage** the current `scrna_pack.py` (+ a tiny CLI) into the job at submit time.
  Always matches the deployed code; no image rebuild, but more moving parts per job.

Recommendation: **(A) bake** for reproducibility, with a thin CLI module `scrna_cli.py` added to the
image. (B) is the fallback if we want the image to stay code-agnostic.

### 2. A `SlurmAnalysisExecutor` (wrap the tools, don't rewrite them)

Mirror `SlurmCodeExecutor` / `scgpt_runner`, but tool-shaped. For each of the four tools, its
registered `executor(args, ctx)`:
1. ensures a **shared per-run DFS workspace** exists (below);
2. writes `args` to a small JSON on DFS;
3. `build_analysis_script()` → `singularity_exec(analysis.sif, cmd=scrna_cli <tool> …)` with binds:
   dataset **ro**, `work/` + `artifacts/` **rw**;
4. `run_batch_job()` (reuse verbatim) — submit, retry-on-queue, wait for terminal state;
5. **pull back only the small outputs** — the result JSON + any new figures/tables — and return the
   tool's result dict unchanged.

Signature stays the standard `(args, ctx) -> dict`, so the registry/harness don't change.

### 3. Shared per-run DFS workspace (handles the checkpoint chain)

The chain (QC→cluster→DE→enrichment) means checkpoints accumulate. Stage **once**, keep checkpoints
on DFS between steps, never round-trip the big `.h5ad`s:

```
/dfs3b/ruic20_lab/<user>/analysis/<run_id>/
  data/<dataset>.h5ad     # staged once (ro bind); see staging strategy
  work/                   # adata_qc / adata_clustered / adata_de  (stay on DFS)
  artifacts/figures,tables  # written by the job; small → pulled back to eyeserver for the bundle
```

`run_id` from `ctx.workspace.name` (same isolation trick scGPT uses).

### 4. Placement switch + fallback

- New opt-in `BIOAGENT_ANALYSIS_ON_HPC` (+ `settings.analysis_on_hpc`), mirroring
  `run_code_on_hpc`. Default **off** → in-process (unchanged behavior).
- In `app.py` where the catalog is built: choose in-process `scrna_catalog()` vs an HPC-wrapped
  variant. **Graceful fallback**: if `remote is None` / mock / job fails → run in-process locally
  (same policy as `SlurmCodeExecutor.local_fallback`).
- Reuse existing `HPCSettings` fields (partition/account/mem/cpus/time); no new infra.

## The one real cost decision — dataset staging

Uploads currently land on the **eyeserver**, so the matrix must reach HPC3 DFS before compute:
- **(a) Per-run SFTP stage** (`put_file`) — simple; a WAN round-trip that's painful for ~15 GB.
- **(b) Content-hash cache on DFS** — stage once per dataset, skip re-upload on later runs.
- **(c) End-state: uploads land on HPC3 directly** (true co-location; removes staging entirely).

Recommend **(a)+(b) now**, **(c)** as the follow-on once this path is proven.

## Open questions

1. **Container importability** — bake vs bind (§1); biggest unknown, decides the image work.
2. **Which tools to route** — all four, or only the heavy ones (clustering, DE)? Light tools may not
   be worth the ~queue+startup latency (`acquire_allocation` waits for RUNNING).
3. **DFS scratch lifecycle** — `work/` checkpoints on quota. Reuse the "no auto-delete of research
   data" policy but add a TTL cleanup for `analysis/<run_id>/work/` scratch specifically.
4. **Concurrency** — N users × up to 4 jobs each vs the lab Slurm account. Each user submits under
   their **own** HPC3 account, so it's their own queue/priority; watch dfs3b quota, not app limits.
5. **Latency UX** — batch queue adds seconds–minutes per step; surface `acquire_allocation` state in
   the lab event stream (like scGPT/vlreview `Visual review …` lines).

## Phased plan

- **Phase 0 (this doc)** — map current state; decide **bake vs bind** (§1) and **which tools** (§Q2).
- **Phase 1 — container CLI** — add `scrna_cli` entrypoint; bake `scrna_pack` (+deps) into
  `analysis.def`; rebuild+restage via `build_and_stage.sh`. Verify `singularity exec analysis.sif
  python -m bioagent.tools.scrna_cli …` runs one tool end-to-end on a CPU node.
- **Phase 2 — `SlurmAnalysisExecutor`** — wrap the four tools, reuse `run_batch_job`; shared-DFS
  workspace + staging(a/b) + selective readback. Offline tests via `MockExecutor`.
- **Phase 3 — wire the switch** — `BIOAGENT_ANALYSIS_ON_HPC` in `app.py`/`registry`; fallback to
  in-process; event-stream progress lines.
- **Phase 4 — real HPC3 validation** — sbatch/squeue/scancel on a real CPU node, dfs3b binds,
  end-to-end QC→cluster→DE→enrichment, concurrency smoke (2–3 simultaneous runs).

## Reuse map — how much already exists

| Needed | Exists? | Source |
|---|---|---|
| Batch submit + supervise (`sbatch`/`squeue`/`sacct`, retry) | ✅ | `slurm_job.py` `run_batch_job` / `acquire_allocation` |
| Singularity containment + binds | ✅ | `slurm_job.py` `singularity_exec` |
| CPU analysis image (scanpy/gseapy/leidenalg) | ✅ | `deploy/analysis/analysis.sif` |
| Stage→submit→poll→readback tool pattern | ✅ | `SlurmCodeExecutor`, `scgpt_runner.py` |
| Opt-in flag + graceful fallback pattern | ✅ | `run_code_on_hpc` + `SlurmCodeExecutor.local_fallback` |
| Settings (partition/account/mem/cpus/time) + env overrides | ✅ | `gateway/settings.py` `HPCSettings` |
| Offline testability | ✅ | `RemoteExecutor` / `MockExecutor` |
| **Container CLI entrypoint for the tools** | ❌ | Phase 1 |
| **`SlurmAnalysisExecutor` (tool-shaped wrapper)** | ❌ | Phase 2 |
| **`analysis_on_hpc` switch + wiring** | ❌ | Phase 3 |
| **Dataset staging strategy (eyeserver → dfs3b)** | ❌ | §"cost decision" |

**Bottom line:** this is mostly **connecting the analysis tools to the Slurm pipeline that already
exists** (proven by run_code/scGPT/vlreview), not building new infrastructure. The genuinely new
work is a container CLI entrypoint, a tool-shaped executor wrapper, and the dataset-staging policy.
