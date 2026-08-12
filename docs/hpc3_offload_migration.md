# Migration plan: offload data + compute to HPC3 (eyeserver → pure gateway)

**Branch:** `feat/hpc3-offload` · **Status:** plan (implementation staged in phases) · 2026-07-02

Goal: **uploads land on HPC3 dfs3b (not eyeserver)** and **every srun-able CPU/GPU task runs on
HPC3**, so the eyeserver only does gateway work (auth / WebSocket / HTTP / SSH tunnels). Companion
to [`analysis_slurm_offload.md`](analysis_slurm_offload.md) (the scanpy-specific piece).

## The one fact that shapes everything

**`/dfs3b` is NOT mounted on the eyeserver** (verified: `ls /dfs3b` → No such file or directory; no
NFS/sshfs). The gateway reaches HPC3 storage **only via SSH** (`put_file` / `get_file` / `exec`).

**Consequence:** the moment an uploaded dataset lives on dfs3b, **no eyeserver-side code can read it
directly.** So "upload to HPC3" **cannot be done alone** — it forces the data *consumers* (preflight,
scanpy analysis, anything that opens the matrix) to run **on HPC3** too. This is a **co-location
shift**, not an upload tweak. That's actually the goal — it just means the phases are coupled.

## Grounded current state (what breaks)

- **Uploads** → `<workspace>/uploads/` on eyeserver; `Dataset.path` stores that **local** path
  (`app.py` `/api/upload*`, `models.py:Dataset`).
- **Connection** = SSH **+** GPU allocation **+** vLLM, all inside `/api/connect`. `conn.executor`
  (the SSH `RemoteExecutor`) is only assigned mid-provisioning (app.py ~L335). Upload requires a
  `connection_id`.
- **Every dataset reader assumes a local path** and will break if the path is `/dfs3b/...`:
  preflight/h5py (`tools/datasets.py`), scanpy tools (`tools/scrna_pack.py`), sandbox env vars
  (`agents/sandbox.py`), report render (`tools/report.py`), schematic (`tools/schematic.py`).
- **Reusable plumbing (already there):** `conn.executor.put_file/get_file/exec`; `_storage_base(conn)`
  = `/dfs3b/ruic20_lab/<user>`; HPC3 storage list/delete APIs; `SlurmCodeExecutor` binds paths into
  Singularity; `slurm_job.py` engine; `analysis.sif`.

## Three decisions to lock before coding

1. **Decouple SSH from GPU (the linchpin).** ⚠️ **DECIDED, SHIPPED, THEN REVERSED (2026-07-27).**
   To upload to dfs3b, `conn.executor` (SSH) must exist at upload time — SSH was entangled with GPU
   alloc + vLLM in one slow `/api/connect`. Phase 1 split it behind `BIOAGENT_LAZY_GPU`: an SSH
   connect first, GPU/vLLM allocated lazily on the first run.
   **Outcome:** lazy provisioning behaved badly on this cluster and its half-connected state
   ("SSH up, no model") made the connection lifecycle hard to reason about in both the gateway and
   the console, so the flag ran `=0` in prod and the whole path was **removed**. `/api/connect` is
   the *alternative* listed below: SSH + GPU in one shot, session usable only at `status==ready`.
   Uploads therefore require a fully-connected session.
2. **Connect-first UX.** Uploading before connecting becomes impossible (no SSH = nowhere on dfs3b to
   put the file). The UI must require SSH-connect before the upload control is enabled. Product call:
   acceptable? (I think yes — it's a lab tool; you log in to your HPC account first.)
3. **Enrichment needs the internet.** `gseapy` Enrichr hits a **public API**; HPC3 compute nodes
   typically have **no outbound internet**. So `run_enrichment` either **stays on the eyeserver**
   (it's light CPU + network — fine to keep local) or runs on a login node / via proxy. Default:
   **keep enrichment on eyeserver**, offload the heavy in-memory steps (QC/cluster/DE).

## Progress (branch `feat/hpc3-offload`)

- ↩️ **Phase 1** — SSH/GPU decouple (`BIOAGENT_LAZY_GPU`), `fbc1243`. **REVERTED 2026-07-27**: the
  lazy path worked badly on this cluster, prod ran it off (`=0`), and the half-connected state made
  the lifecycle confusing — setting, endpoint (`POST /api/connect/gpu`), backend branches and the
  console's lazy status states are all gone. `/api/connect` is once again SSH + GPU in one shot.
  Tests: `test_connect_provisioning.py` (asserts they come up together; `test_lazy_gpu.py` was
  rewritten into it). Phase 2 is unaffected — it needs `conn.executor`, which a ready session has.
- ✅ **Phase 2** — uploads → dfs3b (`BIOAGENT_UPLOADS_ON_HPC`), `16570ac` + **2b** folders `71362c3`.
  Single-file + chunked + folder trees; stage-back for local tools; remote primary-matrix detect;
  remote-aware delete. Tests: `test_uploads_hpc.py`.
- ✅ **Phase 4** — scanpy analysis → CPU Slurm (`BIOAGENT_ANALYSIS_ON_HPC`), `de96133`. `scrna_cli`
  in-container entrypoint + `SlurmAnalysisExecutor` + registry routing + in-process fallback.
  Tools **bind-mounted from dfs3b** (`2433775`), NOT baked → a tool edit needs no image rebuild;
  `analysis.sif` is deps-only (`--remote`-buildable). Tests: `test_scrna_cli.py`, `test_slurm_analysis.py`.
- ⏸ **Phase 3** preflight → HPC3: **intentionally skipped** — it's a cheap header read; a Slurm
  container spin-up would cost more than the work. Stays local (on the staged-back file).
- ⏳ Remaining: **Phase 5** report render (pandoc/xelatex) → Slurm (needs a texlive image),
  **Phase 6** flip run_code default, frontends (`connected` status + upload UX). **Ops:** build +
  stage `analysis.sif` (deps-only) on HPC3 — until then Phase 4 falls back in-process.

All shipped phases are flag-gated (default off) with fallbacks; full suite **337 passed**.

## Phased plan (each phase = flag + fallback + tests)

- **Phase 1 — SSH/GPU decoupling** *(foundational; enables upload-to-HPC3)*. Split `/api/connect`:
  `conn.executor` available after SSH+Duo; GPU/vLLM allocated lazily on first run. Keep the current
  combined path behind a flag during rollout. Update the console connect flow.
- **Phase 2 — Uploads → dfs3b.** Upload handlers stream to a temp file, then `put_file` →
  `/dfs3b/ruic20_lab/<user>/uploads/<name>` (chunked + folder uploads too). `Dataset.path` = the
  remote path. `/api/datasets/delete` + storage delete → `rm -rf` on dfs3b via `exec`. Fallback:
  if no SSH executor, keep the local path (dev mode).
- **Phase 3 — Preflight → HPC3.** `run_dataset_smoke_analysis` / h5ad inspect run as a quick
  `exec` (h5py inside `analysis.sif`, or a tiny srun) reading dfs3b, returning small JSON — instead
  of opening the file on eyeserver.
- **Phase 4 — Analysis → HPC3 Slurm** (the [`analysis_slurm_offload.md`](analysis_slurm_offload.md)
  design). Now uploads are *already* on dfs3b, so there's **no per-run staging** — the co-location
  payoff. `SlurmAnalysisExecutor` + container CLI + shared `<run_id>` dir on dfs3b.
- **Phase 5 — Report render → HPC3 Slurm.** pandoc/xelatex/graphviz as a CPU job (needs texlive in
  the image — bake into `analysis.sif` or a `report.sif`); pull back only the PDF/DOCX/MD.
- **Phase 6 — flip run_code default to HPC** (`BIOAGENT_RUN_CODE_ON_HPC` on); schematic stays local
  (trivial) or joins the report job.

## End-state placement

| Stays on eyeserver | Moves to HPC3 |
|---|---|
| gateway (auth / WS / HTTP / SSH tunnels) | uploads / datasets (dfs3b) |
| literature search (external API) | preflight, scanpy QC/cluster/DE |
| enrichment (light CPU + Enrichr network) | run_code (default), report render |
| final bundle assembly (pulls small outputs back) | scGPT / vlreview / vLLM (already there) |

## Risks / open items

- **Big uploads over SFTP** (~15 GB matrices) — `put_file` is a WAN transfer; show progress, consider
  chunked resumable put. (Content-hash cache to skip re-uploads of the same dataset.)
- **dfs3b quota + scratch cleanup** — per-run `work/` checkpoints accumulate on dfs3b; add TTL cleanup
  (keep the no-auto-delete policy for *research data*, but reap `analysis/<run_id>/work/` scratch).
- **Cold-start UX** — connecting pays the whole ~10-min A100 spin-up before the session is usable
  (the lazy path that deferred it is gone); the cold-start card in the console is the current answer.
- **Rollback** — every phase is flag-gated with a local fallback, so we can ship incrementally and
  revert a phase without breaking the others.

## Recommended first step

**Phase 1 (SSH/GPU decoupling)** — was the recommended starting point and shipped first, but see the
reversal note above: it has since been removed, and Phase 2 stands on its own (a *ready* session has
the SSH executor uploads need).
