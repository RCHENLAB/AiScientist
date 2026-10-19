# HPC3 storage layout and cleanup

Where AiScientist keeps its files on HPC3, and what removes them: a sweep of `Temp/` after 3 days,
and a cleanup when a session disconnects. The inventory of the shared assets (containers, model
weights) is in [`hpc3_assets.md`](hpc3_assets.md).

A run's deliverables do not depend on any of this. Every HPC3 job copies its `artifacts/` back to
the server when a step finishes, to `<AISCIENTIST_RESULTS_DIR>/<ucinetid>/<run_id>/artifacts/`.
With a data root on the server, users' datasets are kept there too
([`dataset_storage.md`](dataset_storage.md)): HPC3 holds only the copies a session's runs read.

## The layout

```
/dfs3b/ruic20_lab/software/AiScientist/  <- AISCIENTIST_HPC_SHARED_ROOT
├── containers/ hf/ envs/ ollama/       <- shared assets, never swept (hpc3_assets.md)
│   scgpt_model/ vlreview_model/
├── Temp/<ucinetid>/                    <- process files: removed when their session disconnects,
│   │                                      and swept after 3 days whatever happened to the session
│   ├── analysis/<run_id>/{work,artifacts,result}/
│   ├── variant/<run_id>/
│   ├── phenotype/<run_id>/
│   ├── scgpt/<run_id>/
│   ├── vlreview/<run_id>/
│   ├── reports/<run_id>/               <- the render bundle
│   ├── server-data/<session>/          <- this session's copies of the datasets its runs read from
│   │                                      the server (the user's own, and bound server folders)
│   ├── paperqa/
│   └── scratch/{analysis,runcode,variant,phenotype,paperqa,report,worker}/<run_id>/
│                                       <- each job's sbatch script, args/result JSON, Slurm log
│                                          and tool log (see "Job files")
├── uploads/<ucinetid>/                 <- uploads of a deployment without a data root, and those
│                                          from before one; never swept
├── pysrc/<ucinetid>/                   <- synced AiScientist source, rewritten at each connect
└── bin/<ucinetid>/                     <- staged from deploy/hpc3/ at each connect:
    ├── temp_gc.sh                         the sweep
    └── session_clean.sh                   the session cleanup
```

`Temp/` sits in the same root as the containers and model weights, so the sweep is confined to
`<root>/Temp` by the script itself.

Never deleted automatically:

* `uploads/`: raw uploaded data of a deployment without a data root (with
  `AISCIENTIST_UPLOADS_ON_HPC`, a file uploaded through the browser is moved here and removed from
  the server, so this is its only copy), and data uploaded before a data root was configured. It is
  deleted by hand only, from the storage panel or the dataset list. With a data root nothing new is
  written here, and each dataset kept only here is copied to the server when its owner connects;
  the original stays until it is removed by hand (the server copy's `source.json` names it).
* `pysrc/`: bind-mounted read-only by running jobs for their whole lifetime.
* `<root>/{containers,hf,envs,ollama,scgpt_model,vlreview_model}/`: the built assets.
* `/dfs3b/ruic20_lab/<ucinetid>/`: each member's personal folder. Neither the sweep nor the cleanup
  touches it.
* `$HOME/.bioagent/`: the vLLM serve job's own files, `serve.<partition>.sbatch`,
  `vllm.<jobid>.port` and `vllm.<jobid>.spec` (how the gateway finds and steers a running serve
  job), plus that job's log `$HOME/bioagent-vllm-<jobid>.log`. No other job writes to the home
  directory.

## Job files

Every job a run starts on HPC3 has a few small files of its own: the sbatch script, the args JSON
it is given, the result JSON it prints, the Slurm log (`#SBATCH --output`) and the tool's stderr
log. They go to `Temp/<ucinetid>/scratch/<line>/<run_id>/`: one directory per job line, and in
it one per run, so two runs in flight at once (two chats of one session) never share a file name.
The session's CPU worker belongs to no run and writes to `scratch/worker/` itself.

| Job line | Directory under `Temp/<ucinetid>/` |
|---|---|
| scanpy analysis tools (QC, clustering, DE, CellQC ...) and image pulls | `scratch/analysis/` |
| `run_code` snippets | `scratch/runcode/` |
| offline VEP variant annotation | `scratch/variant/` |
| LIRICAL phenotype | `scratch/phenotype/` |
| PaperQA literature | `scratch/paperqa/` |
| report rendering (pandoc / xelatex), including the staged `render_<n>.tgz` | `scratch/report/` |
| the session's CPU worker (holder script + log) | `scratch/worker/` |
| scGPT annotation, render review | beside their output: `scgpt/<run_id>/out/`, `vlreview/<run_id>/pass<n>/out/` |

The gateway takes each of these from one function (`app._scratch_dir`), and the job classes have
no default directory: a job line that names none fails when it is built instead of writing
somewhere else. The paths are absolute on purpose. Slurm does not expand `$HOME` in
`#SBATCH --output`, so a path with a shell variable in it is created as written.

## The sweep

`deploy/hpc3/aiscientist_temp_gc.sh`, staged to `<root>/bin/<ucinetid>/temp_gc.sh`.

It runs as a Slurm batch job, never on a login node. The gateway's login-node session runs one
`sbatch --wrap`; the `find` and `rm` happen on a compute node in the free `standard` partition
(1 CPU, 2 G, 30 min cap).

A **unit** is one `Temp/<user>/<kind>/<entry>` directory: a run directory such as
`analysis/<run_id>`, or one job line's directory such as `scratch/runcode`, with the run folders in
it. A unit is deleted only when its entire subtree has gone
untouched for `--ttl-days`. A long-running job keeps writing, so it is never half-deleted, and a run
with files of mixed age is either removed whole or kept whole.

Guards, enforced by the script and covered by `tests/test_hpc_temp_gc.py`:

* `--root` must be absolute, contain no `..`, and not be `/`; only `<root>/Temp` is walked.
* `--ttl-days` must be a non-negative integer; `0` disables the sweep and exits 0.
* `--user` must be a bare account name. The gateway always passes the logged-in user, so a session
  sweeps only its own subtree.
* `--dry-run` prints and removes nothing.

The sweep is asynchronous, so the console reports what the previous sweep deleted, read from its
job log (`<root>/bin/<ucinetid>/temp_gc.log`).

### When the sweep runs

1. **At every connect**: `app._prepare_shared_storage` creates the user's directories and submits it.
2. **Every 6 hours for a live session**: `app._hpc_temp_gc_loop`.
3. **From cron** (optional, per member). The cron line submits the job; it does not sweep in place:

   ```
   0 3 * * * sbatch --job-name=aiscientist-tempgc-$USER --partition=standard --account=ruic20_lab --cpus-per-task=1 --mem=2G --time=00:30:00 --wrap "bash /dfs3b/ruic20_lab/software/AiScientist/bin/$USER/temp_gc.sh --root /dfs3b/ruic20_lab/software/AiScientist --ttl-days 3 --user $USER --quiet"
   ```

   `scrontab` is disabled on HPC3, so this is an ordinary `crontab` entry.

## When a session disconnects

The sweep removes by age, and only when the same user connects again. A disconnect removes by
owner: what this session alone left on HPC3.

| Removed at disconnect | Why it is safe |
|---|---|
| `Temp/<ucinetid>/<kind>/<run_id>/` for every run this session started or resumed (analysis, variant, phenotype, reports, scgpt, vlreview, corpus) | the deliverables are already on the server (`artifacts/`); what goes is the working copy |
| `Temp/<ucinetid>/server-data/<session>/` | a copy of data that lives on the server (the user's datasets, bound server folders); the next session copies it again |
| `uploads/<ucinetid>/server-data/` | copies of server data left by earlier versions of AiScientist; nothing reads them |

`deploy/hpc3/aiscientist_session_clean.sh`, staged to `<root>/bin/<ucinetid>/session_clean.sh` at
connect. `/api/disconnect` submits it as one `sbatch --wrap` on the free CPU partition (job
`aiscientist-sessionclean-<ucinetid>`, log `bin/<ucinetid>/session_clean.log`) and then closes the
SSH session; the job needs neither. The gateway decides the targets (`app._session_cleanup_targets`)
and the script removes exactly those: it never lists a directory to choose what to delete.
`tests/test_session_cleanup.py` runs the script against a real directory tree.

What a disconnect does **not** remove:

* **A run that is still executing**, and the earlier run a continuing run reads its dataset from.
  Their folders, and with them the session's `server-data/` copies, are left to the sweep.
* **A run another live session also knows** (the same run resumed from a second device).
* **Anything of another session.** Server data is copied per session for this reason: two sessions
  bound to the same folder each have their own copy, so one ending cannot take the other's files.
* **Uploads and saved results kept here.** A dataset uploaded to `uploads/`, and anything "Save to
  my data" moved into the user's personal folder (without a data root), is never a target. With a
  data root, "Save to my data" copies the result to the server instead.
* **`scratch/` and `paperqa/`**: per-job scripts and logs, shared by the user's sessions. The sweep
  removes them once they are old enough.

What it costs: after a disconnect a run can no longer be continued from a **middle** step, and its
processed dataset or CellQC result directory is gone unless it was saved to the user's data first
(the console says "kept on HPC3 until you disconnect"). The report, figures and tables are on the
server and stay.

Why a middle step and not the rest: continuing from step *k* reads the AnnData checkpoint that step
*k-1* wrote (`analysis/<run_id>/work/adata_*.h5ad`, hundreds of MB to several GB). Those exist only
on HPC3: the server receives a run's `artifacts/` and removes its own `work/` when the report is
written, so there is nothing on the server to copy back. What the server does hold is the input: a
run resumed in a later session (from its first step, or an interrupted run) has its server-side
datasets copied into that session again and its stored paths repointed
(`app._restage_server_data`). To carry a finished analysis across sessions, save the processed
dataset ("Save to my data", or the run option that saves it at completion): a later message then
starts a new run from it and skips the steps already done.

**Not covered: a session that ends without a disconnect.** Closing the browser tab, a gateway
restart, or a dropped connection run no cleanup: the run may still be executing, and an
interrupted run is resumed from these very files. Those sessions are left to the sweep, which runs
only when that user connects again.

### What still touches a login node

`mkdir`, `chmod` and `test -d` at connect (a job cannot be submitted before its directories exist),
the `tail` of the last job log, the `sbatch` itself, and the storage panel's `du -sh`. No `find`,
no `rm -rf` and no bulk transfer: files are staged through `access-hpc3.rcic.uci.edu`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AISCIENTIST_HPC_SHARED_ROOT` | `/dfs3b/ruic20_lab/software/AiScientist` | the shared root |
| `AISCIENTIST_TEMP_TTL_DAYS` | `3` | age after which an untouched `Temp/` unit is removed; `0` disables the sweep |
| `AISCIENTIST_SESSION_CLEANUP` | `1` | remove a session's own `Temp/` files when it disconnects; `0` leaves them to the sweep |

## Permissions of the shared root

The root is `drwxrwsr-x` (2775, setgid), and `Temp/`, `uploads/`, `pysrc/` and `bin/` are
group-writable, so any lab member's session can create its own per-user subdirectory
(`hpc_gc.ensure_shared_dirs`). `/dfs3b/ruic20_lab` itself is not group-writable, which is why the
root is under `software/`. To move it, someone with `ruic20` rights creates the new directory and
`AISCIENTIST_HPC_SHARED_ROOT` points to it; no code changes.
