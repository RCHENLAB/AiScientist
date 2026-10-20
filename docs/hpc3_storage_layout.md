# HPC3 storage layout and cleanup

Where AiScientist keeps its files on HPC3, and what removes them: a sweep of `Temp/` after 3 days,
a cleanup when a session disconnects, and, on the user's word, the storage panel's Delete and the
removal of originals whose data is now kept on the server. The inventory of the shared assets (containers, model weights) is in
[`hpc3_assets.md`](hpc3_assets.md).

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
└── bin/<ucinetid>/                     <- staged from deploy/hpc3/:
    ├── temp_gc.sh                         the sweep (at each connect)
    ├── session_clean.sh                   the session cleanup (at each connect)
    ├── remove_originals.sh                the removal of originals (when one is checked)
    ├── originals/<plan>.{check,remove}.{tsv,log}   what each check and removal was given and said
    ├── storage_delete.sh                  the storage panel's Delete (at the session's first one)
    └── deletes/<id>.log                   what each deletion said
```

`Temp/` sits in the same root as the containers and model weights, so the sweep is confined to
`<root>/Temp` by the script itself.

Never deleted automatically:

* `uploads/`: raw uploaded data of a deployment without a data root (with
  `AISCIENTIST_UPLOADS_ON_HPC`, a file uploaded through the browser is moved here and removed from
  the server, so this is its only copy), and data uploaded before a data root was configured. It is
  deleted by hand only, from the storage panel or the dataset list. With a data root nothing new is
  written here, and each dataset kept only here is copied to the server when its owner connects;
  the original stays until the user removes it (see *Originals of datasets kept on the server*).
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
| the session's CPU worker (holder script, log, heartbeat) | `scratch/worker/` |
| scGPT annotation, render review | beside their output: `scgpt/<run_id>/out/`, `vlreview/<run_id>/pass<n>/out/` |

The gateway takes each of these from one function (`app._scratch_dir`), and the job classes have
no default directory: a job line that names none fails when it is built instead of writing
somewhere else. The paths are absolute on purpose. Slurm does not expand `$HOME` in
`#SBATCH --output`, so a path with a shell variable in it is created as written.

## The sweep

`deploy/hpc3/aiscientist_temp_gc.sh`, staged to `<root>/bin/<ucinetid>/temp_gc.sh`.

It runs as a Slurm batch job, never on a login node. The gateway's login-node session runs one
`sbatch --wrap`; the `find` and `rm` happen on a compute node in the `standard` partition
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
connect. `/api/disconnect` submits it as one `sbatch --wrap` on the `standard` partition (job
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

* **A run whose result is not safely on the server.** When "Save to my data" is still copying a
  run's result home, or its copy failed and was not made again, that run's folder is not named to
  the cleanup: a later session can still save it, until the sweep. The console says so, and the
  session ends as `failed`.

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

## The session's CPU worker

The agent's shell tools (`run_shell`, `fetch_url`, `install_package`) and the import check before a
`run_code` snippet run as `srun` steps on one CPU job per HPC3 user, `aiscientist-worker-<ucinetid>`
(4 CPU, 16 G, `standard`, charged to the lab's account; only with `AISCIENTIST_WORKER_NODE=1`). The
analysis steps do not use it: each of them submits its own job, sized for that step. The worker is
requested on the first shell command and held only while the agent uses it.

| The worker ends when | Done by |
|---|---|
| 15 minutes pass without a command (`AISCIENTIST_WORKER_IDLE_TIMEOUT_S`, at least 60) | the gateway (`scancel`) |
| the session disconnects, the user logs out, the server stops, or "Stop my GPU job" is pressed | the gateway (`scancel`) |
| the user's next session starts and nothing is using it | the gateway (`scancel`) |
| its heartbeat goes 5 minutes past the idle timeout without a touch (the gateway crashed or restarted) | the job itself |
| 8 hours pass (`AISCIENTIST_WORKER_TIME_LIMIT`) | Slurm |

The heartbeat is `scratch/worker/aiscientist-worker-<ucinetid>-<jobid>.alive`. A command refreshes
it when it starts, every minute while it runs and when it ends, so a long command keeps its node.
The next shell command after a release requests a new worker.

## What the console shows of a session

Each session has a state, kept by the gateway (`gateway/session_state.py`) and pushed to the
console as it changes:

| State | While |
|---|---|
| `open` | connected, nothing being copied or run |
| `staging-in` | a run's inputs are being copied from the server to `Temp/<ucinetid>/server-data/<session>/` |
| `running` | a run is executing |
| `staging-out` | something is being copied from HPC3 to the server: a result being saved, a dataset kept only on HPC3 being moved home |
| `cleaning-up` | the session ended and its cleanup job is queued |
| `closed` | the session ended and nothing of it is left to remove, or its cleanup job finished |
| `failed` | a result's copy home failed and was not made again (its run folder is kept), or the cleanup could not be queued or did not finish |

While several apply, the first in the order staging-out, staging-in, running, failed is shown.
Every copy is listed with its files and bytes as it goes, and with the reason when it fails; a
failed copy to HPC3 fails its run but leaves the session usable. The top bar shows the state while
data moves or something failed; the connection card and the storage panel show the details.

The storage panel also lists the account's last 20 sessions and how each ended, kept in
`<AISCIENTIST_RESULTS_DIR>/<owner>/.sessions.json`. A session that ended without a disconnect is
listed as ended, its files left to the sweep. A queued cleanup is settled at the user's next
connect: one `sacct` gives its job's end state, and the last cleanup log (`CLEAN_JOB` and
`CLEAN_RESULT` lines) how many folders it removed.

## The storage panel's Delete

The storage panel lists the user's own folders: `Temp/<ucinetid>/` (one level deeper, by run),
`uploads/<ucinetid>/` and the personal lab folder `/dfs3b/ruic20_lab/<ucinetid>/`, each entry
with its `du -sh` size. Its Delete button removes one entry, as a Slurm job of
`deploy/hpc3/aiscientist_storage_delete.sh` on the `standard` partition
(`aiscientist-delete-<ucinetid>`, 1 CPU, 2 G, 2 hours), never a command on a login node:

1. `POST /api/storage/delete` checks the path (a clean absolute path strictly inside one of those
   three folders: never a folder itself, never another member's, no `..`), stages the script to
   `bin/<ucinetid>/storage_delete.sh` at the session's first deletion, submits the job through the
   compute backend and answers at once with its id. A path already being deleted is refused.
2. The panel shows the entry as queued and asks `GET /api/storage/{connection_id}/deletes` every
   8 seconds while it is open: one `sacct` for the session's queued deletions, and the log of each
   that ended (`bin/<ucinetid>/deletes/<id>.log`). When one has ended, the panel reads the folders
   again; the console says whether the entry was deleted or why not.

The script checks the path again and refuses to run outside a Slurm job. It skips a path outside
the user's own folders or with a symbolic link anywhere above it (the physical folder must be the
one named), removes an entry that is itself a link as a link, never what it points to, and
`rm -rf` removes the links inside a folder without following them. `tests/test_storage_delete.py`
runs the script against a real directory tree. A mock session deletes at once.

## Originals of datasets kept on the server

When a dataset that lived only on HPC3 is copied to the server, its HPC3 original stays, and the
server copy's `source.json` names it ([`dataset_storage.md`](dataset_storage.md)). The storage panel
removes such originals in two explicit steps, each a Slurm job of
`deploy/hpc3/aiscientist_remove_originals.sh` on the `standard` partition
(`aiscientist-originals-<ucinetid>`, 1 CPU, 2 G, 1 hour), never a command on a login node:

1. **Check** (`POST /api/hpc-originals/check`). The gateway reads every `source.json` of the account
   and leaves out, with the reason shown: an original outside `uploads/<ucinetid>/` (another
   member's, the shared assets, the uploads folder itself), one copied by another HPC3 account, one
   whose server copy is missing or smaller than recorded or is no longer what the dataset reads,
   one a dataset still reads on HPC3, and, unless the deployment sets
   `AISCIENTIST_REMOVE_PERSONAL_ORIGINALS=1`, anything in the personal lab folder
   (`/dfs3b/ruic20_lab/<ucinetid>/`, where earlier results and uploads went). The rest goes to a
   dry-run job that checks each original on HPC3 and removes nothing.
2. **Remove** (`POST /api/hpc-originals/remove`), after the user confirms. Allowed only once the
   dry run finished, within a day of it, and while no run of the session is executing. It takes only
   the originals the dry run would remove that are still unchanged candidates, and the job checks
   each one again before removing it.

The script removes an original only when it is strictly inside the user's uploads folder (or, with
`--personal`, inside the personal folder of that same user), its path has no `..` and no symbolic
link anywhere, a folder holds no link and no hidden file (neither was copied to the server), and its
size on HPC3 equals the server copy's. Anything else is skipped with the reason. It refuses to run
outside a Slurm job. When the removal finishes, each removed original is recorded in its dataset's
`source.json` (`hpc3_removed`), and the next check no longer lists it. What each job was given and
what it said stay in `bin/<ucinetid>/originals/`. `tests/test_hpc_originals.py` runs the script
against a real directory tree.

## What still touches a login node

The sweep, the session cleanup, the storage panel's Delete and the removal of originals run no
`find`, no `rm` and no bulk transfer on a login node: each is a Slurm job, and their scripts are
staged through `access-hpc3.rcic.uci.edu`. What they still run on a login node: `mkdir`, `chmod`
and `test -d` (a job cannot be submitted before its directories exist), the `sbatch` itself,
`sacct` for a job's end state, and the reading of a job's log. The storage panel's listing runs
`du -sh` there.

Other removals still run on a login node over the SSH session:

* the dataset list's Delete of a dataset kept on HPC3 (`rm -rf` of its folder), and of the
  session's HPC3 copy of a dataset kept on the server;
* cancelling an upload to HPC3 (`rm -rf` of `uploads/<ucinetid>/temp/<folder>`), and `rm -f` of an
  upload's file on HPC3 when its copy fails its check or is cancelled;
* the rewrite of `pysrc/<ucinetid>/` at connect (`rm -rf` of the old tree, then `tar xzf`).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AISCIENTIST_HPC_SHARED_ROOT` | `/dfs3b/ruic20_lab/software/AiScientist` | the shared root |
| `AISCIENTIST_TEMP_TTL_DAYS` | `3` | age after which an untouched `Temp/` unit is removed; `0` disables the sweep |
| `AISCIENTIST_SESSION_CLEANUP` | `1` | remove a session's own `Temp/` files when it disconnects; `0` leaves them to the sweep |
| `AISCIENTIST_REMOVE_PERSONAL_ORIGINALS` | off | let the storage panel also remove originals in the personal lab folder |

## Permissions of the shared root

The root is `drwxrwsr-x` (2775, setgid), and `Temp/`, `uploads/`, `pysrc/` and `bin/` are
group-writable, so any lab member's session can create its own per-user subdirectory
(`hpc_gc.ensure_shared_dirs`). `/dfs3b/ruic20_lab` itself is not group-writable, which is why the
root is under `software/`. To move it, someone with `ruic20` rights creates the new directory and
`AISCIENTIST_HPC_SHARED_ROOT` points to it; no code changes.
