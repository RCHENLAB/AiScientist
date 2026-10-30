# HPC3 storage layout and cleanup

Where AiScientist keeps its files on HPC3, and what removes them: each session's own folder, removed
when the session ends once its runs' results are on the server; a sweep of what sessions left after
3 days; and, on the user's word, a Delete in the console's Storage tab and the removal of originals
whose data is now kept on the server. The inventory of the shared assets (containers, environments,
model weights) is in [`hpc3_assets.md`](hpc3_assets.md).

A run's results never depend on any of this: they are on the server before anything on HPC3 is
removed (see *What comes back to the server*). With a data root on the server, users' datasets are
kept there too ([`dataset_storage.md`](dataset_storage.md)): HPC3 holds only the copies a session's
runs read.

## The layout

HPC3 holds environments, images, model weights and one folder per session
(`gateway/hpc_layout.py`):

```
/dfs3b/ruic20_lab/software/AiScientist/   <- AISCIENTIST_HPC_SHARED_ROOT
├── envs/                     environments: openspliceai/, analysis-pydeps/ (Python packages the
│                             analysis image lacks), run-code-pkgs/ (packages run_code installs),
│                             conda-pkgs/ (the lab's conda download cache for image builds)
├── containers/               images (hpc3_assets.md)
├── models/                   model weights: hf/ (the Hugging Face cache), scgpt/, vlreview/
└── sessions/<session-uuid>/  one session; the folder name is the session's id
    ├── manifest.json         session and account ids, HPC3 user, app version and commit, when it
    │                         started, when it is removed
    ├── app/                  the AiScientist package its jobs import, copied at the first job
    ├── scripts/              the housekeeping scripts staged from deploy/hpc3/
    ├── logs/                 their logs: sweep.log, clean.log, clean-earlier.log, deletes/,
    │                         originals/, stageout/ (the manifests a cleanup checks)
    ├── inputs/               copies of the server datasets its runs read (read-only to the jobs)
    ├── shell-home/           $HOME inside the agent's run_shell sandbox
    ├── downloads/            the agent's fetch_url default
    ├── jobs/<line>/          job files that belong to no run: the CPU worker, PaperQA in a chat
    └── runs/<run_id>/
        ├── analysis/         work/ (checkpoints), artifacts/, result/ (the run's results)
        ├── variant/ phenotype/ reports/ scgpt/ vlreview/ corpus/
        └── jobs/<line>/      the run's job scripts, args/result JSON and logs
```

`sessions/` is group-writable with the setgid and sticky bits (3775): every lab member's session can
create its folder there, and no one can remove another's. A session folder is its HPC3 user's only
(0700), and the scripts act only on folders the user running them owns.

Never deleted automatically:

* `envs/`, `containers/`, `models/`: the built assets.
* `/dfs3b/ruic20_lab/<ucinetid>/`: each member's personal folder.
* `$HOME/.bioagent/`: the vLLM serve job's own files, `serve.<partition>.sbatch`,
  `vllm.<jobid>.port` and `vllm.<jobid>.spec` (how the gateway finds and steers a running serve
  job), plus that job's log `$HOME/bioagent-vllm-<jobid>.log`. No other job writes to the home
  directory.
* a run's `result/` that is not yet on the server (*The sweep*).

### Folders of the earlier layout

Before session folders, each user had `Temp/<ucinetid>/` (process files, by kind and run),
`uploads/<ucinetid>/`, `pysrc/<ucinetid>/` (the synced source) and `bin/<ucinetid>/` (the scripts
and their logs), and the root held `pkgs/` and `pydeps/`. Nothing new is written to them, except
the uploads of a deployment without a data root. What is left goes this way:

| Folder | What happens to it |
|---|---|
| `Temp/<ucinetid>/` | A run started before the change keeps its folders there: continued later, it finds its checkpoints (`app._resolve_run_home`). The next connect brings the results of its ended runs home and removes them; the sweep removes the rest by age, keeping results. Once empty, the sweep removes the folder. |
| `pysrc/<ucinetid>/`, `bin/<ucinetid>/` | Read by jobs submitted before the change. The sweep removes them once `Temp/<ucinetid>` is empty, and only when none of the user's AiScientist jobs (the model server and the CPU worker aside) is queued or running. |
| `uploads/<ucinetid>/` | Datasets that lived only on HPC3 are copied to the server at their owner's connect; the originals go only through the Storage tab's two steps (*Originals of datasets kept on the server*). The sweep removes the folder once nothing but empty folders is left in it. |
| `pkgs/`, `pydeps/`, `hf/`, `scgpt_model/`, `vlreview_model/` | Moved under `envs/` and `models/` by hand, once (the cutover); the code reads only the new places. |

## What comes back to the server

A run on HPC3 writes three kinds of files, and only the first two are results:

| Files | What they are | When they come back |
|---|---|---|
| `runs/<run_id>/analysis/artifacts/` | the report, figures, tables and the run's record | after every step, to `<AISCIENTIST_RESULTS_DIR>/<owner>/<run_id>/artifacts/`; the run is published from that copy |
| `runs/<run_id>/analysis/result/` | what the run hands on that is too large for the bundle: the processed dataset (`<input>.processed.<time>.<run>.h5ad` and its `.json` record) and CellQC's result folder | when the session ends, every file under `result/`, to `<AISCIENTIST_RESULTS_DIR>/<owner>/<run_id>/result/` |
| everything else | `work/` (the AnnData checkpoints each step reads from the one before), the copies of the inputs, the synced source, job scripts and their args/result JSON, the shell's home and downloads | never |

The run's job logs (`*.out`, `*.err` and `*.log` of each of its jobs, up to 20 MB each) come back
with the results, to `<run_id>/logs/hpc3/<job line>/`, so a failure can still be read after the
HPC3 folder is gone. They are not results: a log that cannot be copied is reported and does not
keep the folder.

How a result is brought home (`gateway/stage_out.py`):

1. Each file is copied through the transfer host into a `.part` file that resumes after an
   interruption, and must arrive at the size HPC3 lists. A result the user already saved to their
   data ("Save to my data") is not copied again when its saved copy still has that size.
2. The SHA-256 of every server copy is written to `<run_id>/stage_out.json`, with its size and path.
3. The run's records of what it hands on (`artifacts/data/result_dataset*.json`,
   `cellqc_result.json`) are pointed at the server copy (`location: gateway`), and the run's result
   metadata says the outputs are kept with the run on the server. "Save to my data" then works as
   for a run that computed on the server.
4. The removal on HPC3 gets the manifest and hashes each HPC3 file again on a compute node: a run
   folder is removed only when every file under its `result/` is in the manifest with the same size
   and checksum (see *When a session disconnects*).

A result brought home stays with its run on the server until the run is deleted (**Runs →
Delete**). It is not removed after `AISCIENTIST_CHECKPOINT_TTL_DAYS` like the `result/` of a run that
computed on the server.

## Job files

Every job a run starts on HPC3 has a few small files of its own: the sbatch script, the args JSON
it is given, the result JSON it prints, the Slurm log (`#SBATCH --output`) and the tool's stderr
log. They go to the run's `jobs/<line>/`, so two runs in flight at once (two chats of one session)
never share a file name. What belongs to no run goes to the session's own `jobs/<line>/`.

| Job line | Directory |
|---|---|
| scanpy analysis tools (QC, clustering, DE, CellQC ...) and image pulls | `runs/<run_id>/jobs/analysis/` |
| `run_code` snippets | `runs/<run_id>/jobs/runcode/` |
| offline VEP variant annotation | `runs/<run_id>/jobs/variant/` |
| LIRICAL phenotype | `runs/<run_id>/jobs/phenotype/` |
| PaperQA literature | `runs/<run_id>/jobs/paperqa/` (in a chat with no run: `jobs/paperqa/`) |
| report rendering (pandoc / xelatex), including the staged `render_<n>.tgz` | `runs/<run_id>/jobs/report/` |
| the session's CPU worker (holder script, log, heartbeat) | `jobs/worker/` |
| scGPT annotation, render review | beside their output: `runs/<run_id>/scgpt/out/`, `runs/<run_id>/vlreview/pass<n>/out/` |

The gateway takes each of these from one function (`app._scratch_dir`), and the job classes have
no default directory: a job line that names none fails when it is built instead of writing
somewhere else. The paths are absolute on purpose. Slurm does not expand `$HOME` in
`#SBATCH --output`, so a path with a shell variable in it is created as written.

## A run's folder across sessions

A run's folders are in the session that started it (`runs/<run_id>/`), and the run's folder on
the server records where (`hpc3_home.json`). A run continued in a later session keeps using that
folder while it exists: continuing from a middle step reads the checkpoints there. A run started
before session folders keeps its `Temp/<ucinetid>/<kind>/<run_id>/` folders the same way. When the
folder is gone, the run starts again in the current session's folder. The gateway decides this
once per run and session, with one `test -d` (`app._resolve_run_home`).

## When a session disconnects

The sweep removes by age, and only when the same user connects again. A disconnect removes by
owner: what this session alone left on HPC3, once its results are on the server.

1. The session's state becomes `staging-out`, and each run it would remove has its results brought
   home (*What comes back to the server*). Copying a large result takes minutes, so the reply to the
   disconnect does not wait: the console says the results are being copied, and the gateway keeps
   the SSH session until the cleanup is queued. **My account → Storage → Sessions** shows how it
   ends.
2. A run whose copy failed is not removed: the session ends `failed`, and the next connect tries
   again.
3. The cleanup job is queued with each run's manifest (`logs/stageout/<tag>/<run_id>.tsv`). For a
   run whose folders hold a `result/` with any file in it, the script hashes every file there again
   and removes the run only when all of them match. A run without a manifest, or with a file that
   differs or is not listed, is kept whole and named on a `KEPT` line of the log, and the job exits
   3, so Slurm records it `FAILED` and the session's record says why at the next connect.

| Removed at disconnect | Why it is safe |
|---|---|
| `runs/<run_id>/` of every run this session started or resumed, wherever it lives | its results are on the server and checked; what goes is the working copy |
| the session folder's `app/`, `scripts/`, `inputs/`, `shell-home/`, `downloads/`, `jobs/` | `inputs/` is a copy of data that lives on the server (the next session copies it again); the rest is machinery |
| `uploads/<ucinetid>/server-data/` | copies of server data left by earlier versions of AiScientist; nothing reads them |

The session folder keeps `logs/` (with this job's log) and `manifest.json`, a few kilobytes, until
the sweep removes them.

`deploy/hpc3/aiscientist_session_clean.sh`, staged to the session's `scripts/session_clean.sh` at
connect. `/api/disconnect` submits it as one `sbatch --wrap` on the `standard` partition (job
`aiscientist-sessionclean-<ucinetid>`, log `logs/clean.log`); the job copies the script to the
node's scratch first, since it removes the folder it was staged in. The gateway decides the targets
(`app._session_cleanup_targets`) and the script removes exactly those: it never lists a directory to
choose what to delete, and it acts only on folders its user owns. `tests/test_session_cleanup.py`
and `tests/test_hpc_sessions_layout.py` run the script against a real directory tree.

What a disconnect does **not** remove:

* **A run that is still executing**, and the earlier run a continuing run reads its dataset from.
  While one of its runs executes, the session's folder (its `app/` and `inputs/` are in use) is
  left to the sweep too.
* **A run another live session also knows** (the same run resumed from a second device).
* **Anything of another session.** Server data is copied per session for this reason: two sessions
  bound to the same folder each have their own copy, so one ending cannot take the other's files.
* **Uploads and saved results kept here.** A dataset uploaded to `uploads/`, and anything "Save to
  my data" moved into the user's personal folder (without a data root), is never a target. With a
  data root, "Save to my data" copies the result to the server instead.
* **A run whose results are not safely on the server.** When "Save to my data" is still copying a
  run's result home, or a copy failed and was not made again, that run's folder is not named to
  the cleanup. The console says so, the session ends as `failed`, and the next connect copies the
  results and removes the folder.

What it costs: after a disconnect a run can no longer be continued from a **middle** step. Its
processed dataset and CellQC result folder are kept with the run on the server, with the report,
figures and tables.

Why a middle step and not the rest: continuing from step *k* reads the AnnData checkpoint that step
*k-1* wrote (`analysis/work/adata_*.h5ad`, hundreds of MB to several GB). Those exist only on HPC3:
the server receives a run's `artifacts/` and removes its own `work/` when the report is written, so
there is nothing on the server to copy back. What the server does hold is the input: a run resumed
in a later session (from its first step, or an interrupted run) has its server-side datasets copied
into that session again and its stored paths repointed (`app._restage_server_data`). To carry a
finished analysis across sessions, save the processed dataset ("Save to my data", or the run option
that saves it at completion): a later message then starts a new run from it and skips the steps
already done.

### At the next connect

Closing the browser tab, a gateway restart, or a dropped connection run no cleanup: the run may
still be executing, and an interrupted run is resumed from these very files. The account's next
connect looks for what such sessions left (`app._recover_earlier_runs`), in a worker thread beside
the login:

* It lists the run folders of the account's HPC3 user, in the session folders it owns and in
  `Temp/<ucinetid>/analysis/`, and takes each run that is a run of this account (its folder is in
  the account's results), has ended on the server (`done`, `cancelled` or `error`), and is not held
  by a live or ending session.
* An `interrupted` run is left alone: it may still be continued from the middle, which needs its
  checkpoints. So is a folder of another account or another deployment sharing the HPC3 account.
* It also takes the folders of the account's sessions that ended (their records), to clear them as
  a disconnect would.
* Nothing is done while one of the account's AiScientist jobs is queued or running on HPC3 (its
  model server and CPU worker aside), since it could be writing into such a folder; the next
  connect tries again.
* Each run's results come home as at a disconnect (the transfers are labelled "an earlier
  session"), and one cleanup job with their manifests removes the folders (log
  `logs/clean-earlier.log` of the current session).

## The sweep

`deploy/hpc3/aiscientist_temp_gc.sh`, staged to the session's `scripts/temp_gc.sh`.

It runs as a Slurm batch job, never on a login node. The gateway's login-node session runs one
`sbatch --wrap`; the `find` and `rm` happen on a compute node in the `standard` partition
(1 CPU, 2 G, 30 min cap).

**Session folders.** Only the folders the user owns, and never one of the user's live sessions
(the gateway names them with `--keep-session`). A folder is swept only when nothing in it has
changed for `--ttl-days`, so a long-running job, which keeps writing, is never swept out from under
itself. A cold folder loses its `app/`, `scripts/`, `inputs/`, `shell-home/`, `downloads/` and
`jobs/`, and each of its runs: a run without results goes whole, a run with results keeps each
`result/` and loses the rest (`held`). Once nothing but `logs/` and `manifest.json` is left, the
folder goes.

**`Temp/<user>` of the earlier layout.** A **unit** is one `Temp/<user>/<kind>/<entry>` directory:
a run directory such as `analysis/<run_id>`, or one job line's directory such as `scratch/runcode`.
A unit is deleted only when its entire subtree has gone untouched for `--ttl-days`, and a cold unit
with a `result/` that holds a file keeps it and loses the rest. With `--retire-legacy` (the gateway
passes it when none of the user's AiScientist jobs is queued or running), an empty `Temp/<user>`
goes, and with it `pysrc/<user>` and `bin/<user>`, and `uploads/<user>` when it holds no file.

The sweep never removes a run's results: the owner's next connect brings them home and removes the
folder (*At the next connect*). The log says `held until copied to the server`, and `GC_RESULT`
counts `removed`, `kept`, `held` and `retired`.

Guards, enforced by the script and covered by `tests/test_hpc_temp_gc.py` and
`tests/test_hpc_sessions_layout.py`:

* `--root` must be absolute, contain no `..`, and not be `/`; only `<root>/sessions` and
  `<root>/Temp` are walked.
* `--ttl-days` must be a non-negative integer; `0` disables the sweep and exits 0.
* `--user` must be a bare account name. The gateway always passes the logged-in user, so a session
  sweeps only its own `Temp` subtree, and session folders are always only the caller's own.
* `--dry-run` prints and removes nothing.

The sweep is asynchronous, so the console reports what the previous sweep of the session deleted,
read from its job log (`logs/sweep.log`).

### When the sweep runs

1. **At every connect**: `app._prepare_shared_storage` creates the session's folder and submits it.
2. **Every 6 hours for a live session**: `app._hpc_temp_gc_loop`.

`scrontab` is disabled on HPC3, and nothing sweeps for a member who no longer connects: their
session folders stay until they connect again or remove them.

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

### What the agent may write

The agent writes only in the user's own folders here: the session's folder, and the user's
`Temp/<ucinetid>/` and `uploads/<ucinetid>/` of the earlier layout.
Nothing outside them, including `$HOME`, the member's personal folder `/dfs3b/ruic20_lab/<ucinetid>/`,
other members' folders and the shared assets, can be written, moved or deleted by the agent, and
the user is not asked to make an exception: `run_shell` and `fetch_url` refuse such a command before it runs.

`run_shell` runs inside Apptainer on the worker (`hpc/shell.py`, `ShellSandbox`):
`apptainer exec --containall --no-mount bind-paths` in `<root>/containers/rockylinux_9.7.20251123.sif`.
- Mounted read-write: only those folders. `HOME` inside is the session's `shell-home/`.
- Mounted read-only: the session's other readable areas (`$HOME` among them), plus RCIC's software
  (`/opt/apps`, `/opt/rcic`) and the host's `/usr`, so `module load R/4.5.2` or
  `module load samtools/1.15.1` works as on the node. `find_software` finds the module for a
  program (see `docs/byo_api_key_and_hpc_shell.md`).
- Network as for `run_code` (`AISCIENTIST_SANDBOX_NETWORK`).

The image is pulled on the worker on first use. `AISCIENTIST_SHELL_SANDBOX=0` runs `run_shell` bare
with only the textual write check; it is an emergency switch, not a boundary.
Deleting or moving data inside them still asks the user first.

The heartbeat is the session's `jobs/worker/aiscientist-worker-<ucinetid>-<jobid>.alive`; a new
session looks for it in all of the user's session folders before it cancels a holder nothing uses. A command refreshes
it when it starts, every minute while it runs and when it ends, so a long command keeps its node.
The next shell command after a release requests a new worker.

## What the console shows of a session

Each session has a state, kept by the gateway (`gateway/session_state.py`) and pushed to the
console as it changes:

| State | While |
|---|---|
| `open` | connected, nothing being copied or run |
| `staging-in` | a run's inputs are being copied from the server to the session's `inputs/` |
| `running` | a run is executing |
| `staging-out` | something is being copied from HPC3 to the server: a run's results at the session's end or at the next connect, a result being saved, a dataset kept only on HPC3 being moved home |
| `cleaning-up` | the session ended and its cleanup job is queued |
| `closed` | the session ended and nothing of it is left to remove, or its cleanup job finished |
| `failed` | a result's copy home failed and was not made again (its run folder is kept), the cleanup kept a run whose results did not match their copy on the server, or the cleanup could not be queued or did not finish |

While several apply, the first in the order staging-out, staging-in, running, failed is shown.
Every copy is listed with its files and bytes as it goes, and with the reason when it fails; a
failed copy to HPC3 fails its run but leaves the session usable. The top bar shows the state while
data moves or something failed; the connection card and the Storage tab show the details.

The Storage tab (**My account → Storage**) also lists the account's last 20 sessions and how each
ended, kept in
`<AISCIENTIST_RESULTS_DIR>/<owner>/.sessions.json`. A session that ended without a disconnect is
listed as ended, its files left to the sweep. A queued cleanup is settled at the user's next
connect: one `sacct` gives its job's end state, and the last cleanup log (`CLEAN_JOB` and
`CLEAN_RESULT` lines) how many folders it removed.

## The Storage tab and its Delete

**My account → Storage** has two parts. *On the server* lists the account's datasets (with their
recorded size) and each run's folder (measured on disk), largest first, with the same Delete as
**Files** and **Runs**; it needs no HPC3 session (`GET /api/storage/server`). *On HPC3* lists, while
connected, the user's own folders area by area: this session's folder (its parts, and each of its
runs), the folders the user's earlier sessions left, what is left of `Temp/<ucinetid>/` (by run),
`uploads/<ucinetid>/` and the personal lab folder `/dfs3b/ruic20_lab/<ucinetid>/` (folded, since
it holds the member's own files too), all but the first only while they hold something. Each entry
has its size and newest change from `du -sk --time`; an earlier session's folder and a `Temp/`
entry also show the day the sweep may remove them (that change plus `AISCIENTIST_TEMP_TTL_DAYS`). Above them is the lab's group quota from
`dfsquotas <ucinetid> dfs3b`, which RCIC reports for the whole group only.

A Delete removes one HPC3 entry, as a Slurm job of
`deploy/hpc3/aiscientist_storage_delete.sh` on the `standard` partition
(`aiscientist-delete-<ucinetid>`, 1 CPU, 2 G, 2 hours), never a command on a login node:

1. `POST /api/storage/delete` checks the path (a clean absolute path strictly inside one of those
   folders: never an area itself, never another member's, never the folder of the session in use,
   no `..`), stages the script to the session's `scripts/storage_delete.sh` at its first deletion,
   submits the job through the compute backend and answers at once with its id. A path already
   being deleted is refused.
2. The tab shows the entry as queued and asks `GET /api/storage/{connection_id}/deletes` every
   8 seconds while it is open: one `sacct` for the session's queued deletions, and the log of each
   that ended (the session's `logs/deletes/<id>.log`). When one has ended, the tab reads the folders
   again; the console says whether the entry was deleted or why not.

The script checks the path again and refuses to run outside a Slurm job. It skips a path outside
the user's own folders (a session folder counts only when the user owns it) or with a symbolic link
anywhere above it (the physical folder must be the
one named), removes an entry that is itself a link as a link, never what it points to, and
`rm -rf` removes the links inside a folder without following them. `tests/test_storage_delete.py`
runs the script against a real directory tree. A mock session deletes at once.

## Originals of datasets kept on the server

When a dataset that lived only on HPC3 is copied to the server, its HPC3 original stays, and the
server copy's `source.json` names it ([`dataset_storage.md`](dataset_storage.md)). The Storage tab
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
what it said stay in the session's `logs/originals/` (the plan keeps their paths, so a Remove in a
later session settles a Check of an earlier one). `tests/test_hpc_originals.py` runs the script
against a real directory tree.

## What still touches a login node

The sweep, the session cleanup, the Storage tab's Delete and the removal of originals run no
`find`, no `rm` and no bulk transfer on a login node: each is a Slurm job, and their scripts are
staged through `access-hpc3.rcic.uci.edu`. What they still run on a login node: `mkdir`, `chmod`
and `test -d` (a job cannot be submitted before its directories exist), the `sbatch` itself,
`sacct` for a job's end state, and the reading of a job's log. The Storage tab's listing runs
`du -sk` and `dfsquotas` there.

Other removals still run on a login node over the SSH session:

* the dataset list's Delete of a dataset kept on HPC3 (`rm -rf` of its folder), and of the
  session's HPC3 copy of a dataset kept on the server;
* cancelling an upload to HPC3 (`rm -rf` of `uploads/<ucinetid>/temp/<folder>`), and `rm -f` of an
  upload's file on HPC3 when its copy fails its check or is cancelled;
* the unpacking of the session's `app/` at its first job (`tar xzf` of the packed source).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AISCIENTIST_HPC_SHARED_ROOT` | `/dfs3b/ruic20_lab/software/AiScientist` | the shared root |
| `AISCIENTIST_TEMP_TTL_DAYS` | `3` | age after which an untouched session folder (or `Temp/` unit) is swept, results kept; `0` disables the sweep |
| `AISCIENTIST_SESSION_CLEANUP` | `1` | bring a session's results home and remove its folder when it disconnects, and those of earlier sessions at the next connect; `0` leaves them to the sweep, which keeps each run's `result/` |
| `AISCIENTIST_HPC_PYDEPS` | unset | `envs/analysis-pydeps`, added to the analysis jobs' import path |
| `AISCIENTIST_HF_HOME`, `AISCIENTIST_SCGPT_MODEL_DIR`, `AISCIENTIST_VLREVIEW_MODEL_DIR` | `models/hf`, `models/scgpt`, `models/vlreview` under the root | the model weights |
| `AISCIENTIST_REMOVE_PERSONAL_ORIGINALS` | off | let the Storage tab also remove originals in the personal lab folder |

## Permissions of the shared root

The root is `drwxrwsr-x` (2775, setgid). `sessions/` is `drwxrwsr-t` (3775: group-writable,
setgid, sticky), so any lab member's session can create its folder and only its owner can remove
it; each session folder is 0700 (`hpc_gc.ensure_shared_dirs`). `/dfs3b/ruic20_lab` itself is not group-writable, which is why the
root is under `software/`. To move it, someone with `ruic20` rights creates the new directory and
`AISCIENTIST_HPC_SHARED_ROOT` points to it; no code changes.
