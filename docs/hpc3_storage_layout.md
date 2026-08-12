# HPC3 storage layout & the 3-day Temp sweep

**Status:** implemented; the HPC3 side is **created and live-verified**. Needs an eyeserver
restart (and a `.env` review — see [Deploy](#deploy)) to take effect in prod.

The asset inventory of everything we own on HPC3 is a separate doc: `docs/hpc3_assets.md`.

## The problem this fixes

Results land on the **eyeserver** — `<BIOAGENT_RESULTS_DIR>/<ucinetid>/<run_id>/artifacts/`
(prod: `/data/BioAgent`). Every HPC3 executor mirrors its `artifacts/` back there when a step
finishes, so the deliverables are never stranded on the cluster.

The **process files** are a different story. Until this change, every HPC3-offloaded step wrote
into the member's **personal** lab dir:

| what | where it used to go |
|---|---|
| scanpy work/ checkpoints + artifacts | `/dfs3b/ruic20_lab/<user>/analysis/<run_id>/` |
| offline-VEP variant workspace | `/dfs3b/ruic20_lab/<user>/variant/<run_id>/` |
| LIRICAL phenotype workspace | `/dfs3b/ruic20_lab/<user>/phenotype/<run_id>/` |
| scGPT staged query + predictions | `/dfs3b/ruic20_lab/<user>/scgpt/<run_id>/` |
| pandoc/xelatex render bundle | `/dfs3b/ruic20_lab/<user>/reports/` |
| `run_code` snippets, sbatch scripts, args/result JSON, Slurm logs | `/dfs3b/ruic20_lab/<user>/.bioagent/{runcode,analysis,variant,phenotype,paperqa}/` |

and **nothing ever cleaned any of it.** The product's only GC (`app._expire_old_checkpoints`,
`BIOAGENT_CHECKPOINT_TTL_DAYS`, default 7d) sweeps the eyeserver's local run bundles and never
touches HPC3. The only HPC3-side deletion was a human clicking an item in the storage panel.

That was not fixable in place: you cannot safely automate `rm -rf` against a directory people
also keep hand-curated data in. So the machine-generated files moved out.

Measured 2026-08-07: `ruic20_hpc` on dfs3b was at **595.26 TiB of 600 TiB (99.2 %)** — about
4.7 TiB free for the whole lab. Our own leftovers were 11 G (`analysis`) + 17 G (`variant`) for
one member alone.

## The layout

```
/dfs3b/ruic20_lab/software/AiScientist/  <- BIOAGENT_HPC_SHARED_ROOT (was software/bioagent)
├── containers/ hf/ envs/ ollama/       <- 350+ GB of assets, NEVER swept (docs/hpc3_assets.md)
│   scgpt_model/ vlreview_model/
├── Temp/<ucinetid>/                    <- process files, SWEPT after 3 days
│   ├── analysis/<run_id>/{work,artifacts}/
│   ├── variant/<run_id>/
│   ├── phenotype/<run_id>/
│   ├── scgpt/<run_id>/
│   ├── reports/
│   ├── paperqa/
│   └── scratch/{runcode,analysis,variant,phenotype,paperqa}/   sbatch + args/result + job logs
├── uploads/<ucinetid>/                 <- raw research data, NEVER swept
├── pysrc/<ucinetid>/                   <- synced bioagent source, rewritten each connect
└── bin/<ucinetid>/temp_gc.sh           <- the sweeper, staged from deploy/hpc3/
```

Temp sits in the *same* root as the containers and model weights, so the sweeper's confinement to
`<root>/Temp` is load-bearing rather than cosmetic. It is enforced in the script and proven live:
a decoy asset dir with a 10-day-old file, placed next to the real `containers/`, survived a real
sweep untouched.

Deliberately **not** in scope of any automatic deletion:

* `uploads/` — raw uploaded data. Manual deletion only, from the storage panel or the dataset
  list. (Long-standing rule: 误删研究数据的风险 > 存储成本.)
* `pysrc/` — bind-mounted read-only by running jobs for their whole lifetime.
* `<shared_root>/{containers,hf,envs,ollama,scgpt_model,vlreview_model}/` — the built assets.
* `/dfs3b/ruic20_lab/<ucinetid>/` — **every member's personal folder stays untouched.** Old runs
  that predate this change are still there, still browsable, and are only ever removed by hand.
* `$HOME/.bioagent/` — vLLM port files + `serve.sbatch` (2.4 MB, rewritten in place).

## The sweep

`deploy/hpc3/aiscientist_temp_gc.sh`, staged to `<shared_root>/bin/<ucinetid>/temp_gc.sh`.

**It runs as a Slurm batch job, never on a login node.** The sweep walks directory trees and calls
`rm -rf` — real filesystem work, and RCIC's rule is that login nodes are for logging in and
*submitting* jobs. So the gateway's login-node session only ever runs one `sbatch --wrap`; the
`find`/`rm` happen on a compute node in the free `standard` partition (1 CPU, 2 G, 30 min cap).

A **unit** is one `Temp/<user>/<kind>/<entry>` directory (a run dir, or a flat scratch dir with
no per-run subdirs). A unit is deleted only when **its entire subtree** has gone untouched for
`--ttl-days`. That all-or-nothing rule is the safety property: a long-running job keeps writing,
so it can never be half-deleted out from under itself, and a run with mixed-age files is either
fully cold or fully kept.

Guards, all enforced by the script itself and covered by `tests/test_hpc_temp_gc.py`:

* `--root` must be absolute, contain no `..`, and not be `/`; only `<root>/Temp` is walked.
* `--ttl-days` must be a non-negative integer; `0` disables the sweep and exits 0.
* `--user` must be a bare account name — the gateway always passes the logged-in user, so a
  session only ever sweeps its own subtree.
* `--dry-run` prints and removes nothing.

Verified live on HPC3 in the real location (2026-08-08), end to end through Slurm: jobs 55150830 /
55150832 ran on compute node `hpc3-l18-05`, state COMPLETED, and left `GC_RESULT removed=1 kept=1`
in the log, which the next submit read back. Cold units removed; a warm run kept *including* its
10-day-old files; a decoy dir beside the 103 GB of real assets untouched; another user's Temp,
`uploads/` and `pysrc/` untouched; all guards reject (relative root, `..`, `/`, non-integer TTL,
an injected `--user`).

Because the sweep is asynchronous, what the console can report *now* is what the PREVIOUS sweep
deleted — read from its job log (`<shared_root>/bin/<ucinetid>/temp_gc.log`, one small `tail`).
Housekeeping does not need a synchronous answer, and this keeps the login node idle.

### When it runs

1. **On every connect** — `app._prepare_shared_storage` creates this user's dirs and submits.
2. **Every 6 h per live session** — `app._hpc_temp_gc_loop`.
3. **Cron backstop** (optional, per member). Note the cron line *submits* rather than sweeping in
   place, for the same reason:

   ```
   0 3 * * * sbatch --job-name=aiscientist-tempgc-$USER --partition=standard --account=ruic20_lab --cpus-per-task=1 --mem=2G --time=00:30:00 --wrap "bash /dfs3b/ruic20_lab/software/AiScientist/bin/$USER/temp_gc.sh --root /dfs3b/ruic20_lab/software/AiScientist --ttl-days 3 --user $USER --quiet"
   ```

   `scrontab` is **disabled** on HPC3 (`scrontab: fatal: scrontab is disabled on this cluster`),
   so a Slurm-native cron is not an option; `/etc/cron.deny` is empty, so ordinary `crontab` is.

### What still touches a login node

`mkdir`/`chmod`/`test -d` at connect (you cannot submit a job before your dirs exist), the `tail`
of the last job log, the `sbatch` itself, and the storage panel's `du -sh`. All are metadata-only
and user-initiated — the class of thing a login node is for. No `find`, no `rm -rf`, no bulk
transfer: staging goes over `access-hpc3` (the DTN) via `put_file`.

## Config

| env var | default | meaning |
|---|---|---|
| `BIOAGENT_HPC_SHARED_ROOT` | `/dfs3b/ruic20_lab/software/AiScientist` | shared project root |
| `BIOAGENT_TEMP_TTL_DAYS` | `3` | age after which a cold Temp unit is removed; `0` disables |

Both also accept the `AISCIENTIST_*` prefix (`core.config.apply_brand_env_aliases`).

## Deploy

The HPC3 side is **done** (2026-08-08): the root exists, is `drwxrwsr-x` (2775) with setgid, and
`Temp/ uploads/ pysrc/ bin/` are created group-writable so any member's session can make its own
per-user subdir. `ensure_shared_dirs` was run against the real cluster and both a dry-run and a
real sweep came back clean.

What is left is the eyeserver:

1. Restart the gateway so the new defaults load.
2. **Check `/data/BioAgent/app/.env` for pinned `software/bioagent` paths.** They still resolve
   through the compat symlink, so nothing breaks either way — but update them so the symlink can
   eventually go. `BIOAGENT_HPC_SHARED_ROOT` itself needs no entry unless you want a non-default.
3. Optional per-member cron backstop (above).

`/dfs3b/ruic20_lab` (top level) is `drwxr-s--- ruic20 ruic20_hpc` — group members cannot mkdir
there, and `newgrp`/`sg` do not change that (supplementary groups already count for the access
check; the group simply has no `w`). If you ever want the root at `/dfs3b/ruic20_lab/AiScientist`
instead, someone with `ruic20` rights has to create it, and `BIOAGENT_HPC_SHARED_ROOT` moves there
with no code change.

## Migrating the old files

Nothing is moved or deleted automatically. Existing
`/dfs3b/ruic20_lab/<ucinetid>/{analysis,variant,phenotype,reports,scgpt,.bioagent}/` stay exactly
where they are; a member can review and delete them from the storage panel, which now lists all
three areas (Temp, Uploads, personal lab folder) with their sizes.
