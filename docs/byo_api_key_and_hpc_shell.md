# Bring-your-own API key + the agent's HPC3 shell

Two changes decided together in one session (Yijun, 2026-08-10), because their answers turned
out to constrain each other:

1. **Users may supply their own LLM API key.** The HPC3 account stays required — it is what runs
   the Slurm analysis jobs — but which model *reasons*, and on whose bill, becomes the user's
   choice.
2. **The agent gets a near-complete shell on HPC3**, strictly inside RCIC policy, raising a
   human-in-the-loop prompt whenever it would cross a line.

A third piece landed with them: the sandbox can now **install missing Python packages into a
lab-shared cache**, so a library one member needs is installed once and available to everyone.

Status: **all three are built and tested.** The package cache and its Singularity assumptions were
subsequently **measured on the real HPC3 cluster** (see "Why not just `pip` inside Singularity?");
the worker/shell path and real provider accounts are still unexercised. See the status table at
the end.

---

## Why (2) changed the shape of (1)

The two looked independent, and the deciding question for (1) was whether a session should still
allocate a GPU when the user brings their own API key. Yijun's answer — *don't give them a GPU at
connect, give them a CPU node; GPU work queues separately* — is also what makes (2) possible.

RCIC's rule is not "no wget". It is **login nodes are for logging in and submitting jobs**. The
repo already codifies exactly this line in
[`docs/hpc3_storage_layout.md`](hpc3_storage_layout.md): the Temp sweeper runs `find`/`rm` as a
batch job because "login nodes are for logging in and *submitting*", and the only things left on
the login node are `mkdir`/`test -d`/`tail`/`sbatch`/`du -sh` — "metadata-only and user-initiated".

So a **held CPU compute node** turns "give the agent a real shell" and "obey RCIC" from a
tension into the same design. On a node you legitimately hold, `wget`, `rsync`, decompression and
full-text `grep` are all ordinary. The boundary becomes mechanical — a question of *where a
command runs* — instead of a guessed-at list of forbidden command names.

| Where | What is allowed | How it executes |
|---|---|---|
| Login node | Read-only metadata + job control: `ls`, `stat`, `du`, `find -maxdepth`, `squeue`, `sbatch`, `scancel`, `tail` | existing `executor.exec` (milliseconds, no queue) |
| **Held CPU worker** | **Near-complete shell**: `wget`/`curl`, decompression, full-text search, `bcftools`, `run_code` | `srun --jobid=<id> --overlap bash -lc` on the standing allocation |
| GPU batch job | scGPT, local vLLM inference, anything needing a card | its own `sbatch`; the user waits on Slurm |
| `access-hpc3` DTN | Bulk file transfer in and out | existing `put_file` / `get_file` |

---

## Part 1 — bring-your-own API key (built)

### What already existed

The transport layer was done: `vllm_client.complete / chat_tools / chat_tools_stream /
count_tokens` all already accepted `base_url` + `api_key`. What did not exist was any notion of
*whose* key. `_lab_llm()` read process-global `BIOAGENT_LLM_BASE_URL` / `_API_KEY` / `_MODEL` —
one value for the entire server. The README describes that path accurately as
"a **test convenience**, not the product path". This work promotes it to a real one.

### Provider scope

**Any OpenAI-compatible endpoint, with presets as a convenience rather than a whitelist.**
Presets: OpenRouter, OpenAI, DeepSeek, Qwen/DashScope, Moonshot, Gemini's OpenAI-compat layer,
and Custom. Every one speaks `POST /v1/chat/completions` with a bearer token — precisely what
`vllm_client` already sends — so there is **no per-provider adapter**.

Anthropic is deliberately *not* a preset: its native API is `/v1/messages` with `x-api-key` and a
different tool-call schema. Claude models are reached through OpenRouter until someone needs
native badly enough to justify the adapter.

**Model ids are not hard-coded.** Providers retire ids faster than we redeploy, and a stale
built-in list is worse than none — it looks authoritative and sends users to a 404. Ids come from
the live `GET /models`; the preset only records whether that endpoint exists.

### Storage: a stable id with a rotatable key

This was the question Yijun flagged as needing thought — *how does a user change a key later?*

```
<BIOAGENT_STATE_DIR>/llm_creds/<owner>/
    index.json      # public metadata, no secrets   (0600)
    <id>.key        # the secret, one file per cred (0600)
```

Mirrors `ssh_creds/` exactly: same owner sanitisation, same 0600 discipline, same
"public metadata only" rule at the API boundary. One storage idiom for both secret types means
one thing to audit, one thing to back up, one set of containment rules to get right.

**The credential id is stable; the key is a rotatable field inside it.** Everything downstream —
the session's endpoint choice, a conversation's saved preference, a run in flight — references the
`id`, never the key. Rotating a leaked or expired key therefore updates nothing else and
invalidates no reference.

**Rotation verifies before it commits.** `rotate_key()` takes a `verify` callable and runs it
against the **new** key before a single byte is written; if it raises, the stored credential is
untouched and the old key is still working. This is expressed as a *parameter*, not a convention,
so a caller cannot skip it by forgetting. It exists to prevent the state users actually fear: old
key already discarded, new key not working, no way back.

**Verification distinguishes four causes**, because "my key doesn't work" has four different
fixes and the user can only apply one of them if we say which:

| cause | meaning | what the UI tells the user |
|---|---|---|
| `auth` | 401/403 | check the key was copied whole, and belongs to this provider |
| `credit` | 402, 429, or "insufficient"/"quota" in the body | the key works; the account is out of credit or rate-limited |
| `model` | 400/404 naming the model | that id isn't served here — press ↻ to list what is |
| `endpoint` | 404 at the base, or an unexpected status | check the base URL; most providers end in `/v1` |
| `network` | unreachable host | the server could not reach that host |

Providers disagree about which status means which (402 vs 429 for credit; 400 vs 404 for an
unknown model), so the body text is consulted alongside the status.

**The chat ping is the real test**, not the model list: `GET /models` succeeds on keys with no
credit and on endpoints that will refuse the model we care about. So verification asks the actual
model for one token. It costs a fraction of a cent and exercises the same path the lab will use.

**At-rest encryption is optional** (`BIOAGENT_LLM_KEY_ENCRYPTION=1`), off by default, matching how
SSH private keys are already stored here. The reasoning: encryption protects against *file-level*
exposure (a backup, a stray `cat`, a misplaced tarball) but not against a compromised gateway
process, which must be able to decrypt in order to use the key at all. Against that partial
benefit it adds a real failure mode — lose the master key and every user re-enters theirs. So it
is a deployment choice. The `encrypted` flag is stored **per row**, so switching it on later
affects only newly written rows and existing plaintext rows keep working.

The master key lives in its own 0600 file and **never in `.env`**: the production `.env` on
eyeserver is world-readable by design, so a secret placed there is readable by every account on
the box.

### The key is snapshotted at run start

`_lab_llm()` resolves the secret **once**, at bind time, and the closures capture it. A user who
rotates a key while a long analysis is running does not break it — the running analysis keeps the
key it started with, and the next run picks up the new one. Nothing stores the key on the
`Connection`, so no status payload, log line, or crash dump can serialize it.

### Role split, per credential

The existing PI/Critic-vs-Scientist split is preserved and re-sourced from the credential:
`model` serves the high-volume Scientist tool-calling, optional `lab_model` serves the low-volume
reasoning roles. So a user pays for a strong model only on the calls that need one. (The env path
additionally allows a wholly separate endpoint; a credential is one endpoint, two models.)

### Egress: consent is the control, the scanner is a backstop

`LabLLM`'s own docstring already noted that the PI/Critic payload — dataset profile, accepted
findings, artifact digests — does **not** pass the `DataBoundaryGuard`. That was tolerable while a
remote endpoint was an operator override. With user-supplied keys it becomes the ordinary path, so
it is closed here.

Be honest about what each layer does:

* **Informed consent is the real control.** No string scanner can decide whether a gene list or a
  phenotype description identifies a rare-disease patient. The person who knows the data decides.
  Consent is recorded **per credential** — trusting your own self-hosted vLLM is a different
  decision from trusting a commercial API, and one must not stand in for the other. Endpoints on
  loopback need no consent because nothing leaves the host.
* **The guard is a backstop for accidents.** `_guard_lab_payload` runs on every off-host reasoning
  call: a secret anywhere in the prompt is a hard block (an API key cannot be un-sent, and a false
  positive costs one retry), and a raw data matrix in the reasoning payload is a bug worth failing
  on rather than shipping off-site.
* **`SECRET_PATTERNS` was widened** past `sk-*` to cover Anthropic, Google, AWS, GitHub, Hugging
  Face, Slack, PEM blocks, and our own `*_LLM_API_KEY=` forms. Each pattern is anchored on a
  vendor prefix rather than "a long random string", because the latter matches gene ids,
  checksums, and base64 artifact digests that legitimately appear in these prompts.
* **The technical report records where prompts went**, deterministically (`_llm_provenance_note`),
  in Methods. Not LLM-written, for the same reason the variant facts block isn't: a provenance
  claim a model could paraphrase is worthless. It survives the report writer failing.
* **The UI keeps a banner up** for as long as a remote endpoint is selected — the egress lasts the
  whole session, so the notice does too.

### Connect no longer needs a GPU

Per Yijun: when a user brings an API key, connect should **not** allocate a GPU. It should hold a
**CPU node** instead, and GPU-needing work (scGPT, local inference) queues separately with the user
waiting on Slurm. The multi-role PI/Critic/Scientist structure is unchanged — it just runs on the
API. `_provision_gpu_blocking` is already phase 2, cleanly separable from the SSH phase 1.

---

## Part 2 — the agent's HPC3 shell (built)

### The gap

The Scientist catalog in `agents/registry.py` had **no `ls`, no read, no find, no download**. The
only escape hatch was `run_code`, a Slurm *batch* job — so listing a directory cost minutes of
queue. That is a plausible source of the model guessing at paths rather than looking.

### The toolset, and where each part runs

`list_dir`, `stat_path`, `find_files`, `read_text`, `disk_usage` on the **login node** — the class
the storage doc already calls acceptable there, answering in milliseconds with **no allocation at
all**, so a session that only looks around never costs a node. `run_shell`, `fetch_url` and
`install_package` on the **held CPU worker**, allocated lazily on first use.

There is deliberately **no command allowlist**. The general shell simply never executes on a login
node, which makes RCIC compliance structural rather than a judgement about command names that
would be wrong at the edges. `read_text` goes over SFTP rather than `cat`, so reading a file is
not a login-node process at all.

Reuse, not new machinery: `acquire_allocation` / `JobStore` / the reattach path in
`gateway/slurm_job.py` already do everything a CPU holder job needs, so `worker.ensure_worker()`
is `gpu.ensure_serve_job()` minus `--gres` and minus the vLLM serve body. Commands run as
`srun --jobid=<id> --overlap` steps — `--overlap` is required since Slurm 20.11 to share the
allocation with the holder step.

### Confinement

Reads cover the user's own areas plus registered datasets and shared read-only assets; writes
cover the user's own areas only. The lab account is *shared*, so "it's my account" is not the same
as "it's my data" — `Temp/bob` is unreachable from Alice's session. Paths are normalised before
they are judged (`$HOME/../../etc/passwd` is inside `$HOME` as text and outside it as a path), and
re-checked after `readlink -f`, because a symlink inside the workspace can point anywhere.

### HITL triggers

The agent gets a near-complete shell but **raises a prompt at the edge** — not a silent refusal,
not a silent proceed. It runs on its own per-`RunState` channel (`confirm_event` /
`pending_confirm`), *not* a reuse of plan review: both can be outstanding at different moments in
a run, and one shared event would let an answer to one satisfy the other.

1. Writes or deletes outside `$HOME` + `Temp/<user>` + registered dataset paths
2. Any `rm`/`mv`/`dd`/`shred`/`chown`/recursive-`chmod`
3. Downloads from a non-allowlisted host (the allowlist is anchored on a dot, so
   `evil-ensembl.org` does not pass as `ensembl.org`), or over the size cap — enforced by
   `wget --quota`, so an over-large file aborts mid-stream rather than after it has filled a
   99%-full quota
4. Publishing a package into the lab-shared cache
5. **No approver wired => no.** A fence that opens when nobody can be asked is decorative in
   exactly the headless deployments where it matters most. A timeout is also a no.

Deliberately *not* a trigger: a redirection to an absolute path inside your own workspace.
`samtools sort in.bam > $TEMP/out.bam` is routine, and a prompt that fires on routine work trains
people to approve without reading.

---

## Part 3 — the lab-shared package cache (built)

`analysis.sif` is read-only and fixed at build time, so the moment a snippet needs a library the
image lacks, the step dies — and the model's usual recovery (reimplement it, or vendor a copy) is
the last thing you want in an analysis pipeline. Requirement (Yijun): let the sandbox install what
it is missing, and make every install **public**, so nobody installs the same thing twice.

Three constraints shaped it, none incidental:

**A shared directory is not a shared file.** On HPC3, files created by user A cannot be modified
by user B — which is exactly why `hpc_gc.SHARED_SUBDIRS` are all per-user ("one shared copy would
mean each overwriting a file the others own"). So the cache is **immutable and content-keyed**:
each package installs into its own directory, published by an atomic rename, and *nothing ever
writes to a published directory again*. Cross-user ownership stops mattering because there is no
second write. Readers need only `r-x`.

**Two people can install the same thing at once.** Each stages into its own directory, then
publishes with `mv -T` — a directory rename that fails with `ENOTEMPTY` when the destination
exists. That failure *is* the arbiter: the loser detects the winner's tree, discards its staging
copy, and uses the winner's. No lock file, because advisory locking on a parallel filesystem is
not something to bet correctness on.

**A shared import path is a code-execution channel between lab members.** If A can put a `numpy`
on B's `sys.path`, A runs code as B. Four fences:

* publishing a top-level module the image already provides is **refused** — the cache is for
  *missing* packages only;
* a dependency that collides with an image module is **pruned** from staging before publish, so
  the image's version stays authoritative transitively;
* `sys.path` is extended by **appending**, via a generated `sitecustomize` — not by `PYTHONPATH`,
  which sorts *before* `site-packages`;
* only plain PyPI requirements are accepted (no VCS URLs, no paths, no index override), and every
  publish records who did it.

That third point is the subtle one, and it is verified by execution rather than assertion:
`test_the_append_indirection_actually_prevents_a_hijack` builds a real cache tree containing a
`json.py` beside the package, runs a real interpreter, and checks `json` still resolves to the
stdlib — then runs the *same file* through a plain `PYTHONPATH` and confirms it **is** hijacked.
If that counterfactual ever stops failing, the indirection has stopped mattering and the test says
so.

The cache is bound **read-only** into `run_code` sandboxes: a snippet may import from it, but
publishing has to go through the staging/prune/atomic-publish path.

## Status

| # | Piece | State |
|---|---|---|
| 1 | `llm_credentials.py` — per-user store, stable id, rotatable key, optional encryption | **built** |
| 2 | `llm_providers.py` — presets, live model list, four-cause verification | **built** |
| 3 | `/api/llm-credentials` CRUD + rotate + verify + models; `/api/llm-endpoint` | **built** |
| 4 | `_lab_llm()` per-user binding, role split, bind-time key snapshot | **built** |
| 5 | Egress: consent gate, reasoning-payload guard, wider secret patterns, report provenance | **built** |
| 6 | UI: credential manager, endpoint picker, consent dialog, egress banner | **built** |
| 7 | `worker.py` — CPU worker allocation; connect skips the GPU when an API endpoint is chosen | **built** |
| 8 | `tools/hpc_shell.py` — list/stat/find/read/du on the login node, shell/fetch/install on the worker | **built** |
| 9 | HITL: `RunState.confirm_event`, `/api/confirm`, refuse-by-default with no approver | **built** |
| 10 | `package_cache.py` — immutable shared install cache, bound read-only into `run_code` | **built** |

**202 offline tests** across `test_llm_*`, `test_worker_node`, `test_hpc_shell`,
`test_hpc_shell_wiring`, `test_package_cache`. The BYO-key path was additionally driven end-to-end
through the real console UI against a stub OpenAI-compatible endpoint, and the package cache's
`sys.path` ordering is proven by executing a real interpreter (plus its counterfactual) rather
than by assertion.

### Why not just `pip` inside Singularity? (measured on HPC3, 2026-08-10)

The obvious question, and the more natural design if it worked: a `.sif` is read-only, but
Singularity supports a **persistent overlay** (`--overlay pkgs.img`), which would make this an
ordinary `pip install` into `site-packages` — no `--target`, no `sitecustomize`, no anti-shadowing
machinery. It was tested on HPC3 rather than argued about. It does not work here, for four
independent reasons:

| Probe | Result |
|---|---|
| `singularity overlay create --size 512` unprivileged | **works** |
| non-root writes image `site-packages` through a RW overlay | **Permission denied** — copy-up keeps `root:root 755` |
| `--fakeroot` (which would fix the above) | **unavailable**: `no mapping entry found in /etc/subuid for <your-ucinetid>` |
| plain `pip` under `--containall --overlay` | lands in the tmpfs `$HOME`, **does not persist** |
| `mkdir /opt/...` inside the overlay | **Permission denied** — same root-ownership problem |
| reader `:ro` while a writer holds RW | **FATAL** — `currently in use for writing by another process` |
| two readers, both `:ro` | fine |
| `--writable-tmpfs` + writable `--overlay` | **mutually exclusive** (and we always pass the former) |
| disk cost, one small package | **251 MB** overlay image vs **246 KB** as a `--target` tree |

The reader-blocking row is the operational killer even if the permission ones were solved: one
member installing a package would make every concurrently-starting job in the lab die with
`FATAL`. And a 251 MB floor per package is not affordable on a filesystem at 99 % of a 600 TiB
quota.

An **earlier version of this probe reached the opposite conclusion** and was wrong: it omitted
`--containall`, so the host `$HOME` was bind-mounted, `pip` silently fell back to `~/.local`, and
the "it persisted!" read-back was really just reading the home directory. Production always passes
`--containall`. The corrected probe is what the table above reports.

### The shipped design, verified on the same cluster

`pip install --target` into a bind-mounted dfs3b directory needs no root, no overlay, no lock, and
no 251 MB floor. Re-run against the real `analysis.sif`:

* `pip --target` into the bound cache directory — **works**
* atomic publish via `mv -T`, leaving `drwxr-sr-x … ruic20_hpc` (group-readable, setgid inherited)
* `mv -T` onto an existing directory — **fails with `File exists`**, exactly the concurrency
  arbiter the design relies on
* fresh container, cache bound `:ro`, preamble + `sitecustomize` — **imports the cached package**
* **shadow test reproduced on HPC3**: a cached `json.py` resolves to the stdlib, while the *same
  file* via plain `PYTHONPATH` is `HIJACKED`
* **four concurrent readers, all fine** — where the overlay would have blocked all of them
* **246 KB** on disk

Also confirmed while probing: `<shared_root>` is already `drwxrwsr-x … ruic20_hpc` (group-writable
+ setgid), so the "can we even make a shared writable directory?" risk flagged earlier **does not
exist**. And `apptainer/1.4.5` can only mount overlays read-only — writes need
`singularity/3.11.3`, the module we already configure.

### End-to-end, on the real cluster (2026-08-10)

The whole chain was then run on HPC3 exactly as production does it — holder job, `srun --jobid
--overlap`, containerised `pip --target`, prune, atomic publish, fresh container with the cache
bound `:ro` — using the **shipped** `cache_preamble()` and `SITECUSTOMIZE`, not hand-written
copies:

```
IMPORT-OK   pyranges from  <cache>/analysis.sif-py3.11/pyranges/pyranges
IMAGE WINS  pandas 2.3.3 from /usr/local/lib/python3.11/site-packages/pandas
IMAGE WINS  numpy  2.4.6 from /usr/local/lib/python3.11/site-packages/numpy
```

`pyranges` (absent from the image) resolves from the shared cache; `pandas` and `numpy` still
resolve to the **image's** copies even though pip pulled its own versions in as dependencies —
pruning and the append semantics both hold under production conditions. `srun --jobid --overlap`
works, runs on the compute node (not the login node), supports concurrent steps, honours the
per-command `timeout`, and the worker has network egress (`pypi HTTP 200`).

**Two real bugs were found by running it, both now fixed and regression-tested:**

1. **`srun` without `--ntasks=1` ran the command three times.** With `--cpus-per-task` alone
   against a 4-CPU allocation, Slurm launched several parallel tasks. Harmless for `echo`;
   duplicated work racing on one destination for `wget` or `pip install`.
2. **`pip` unpacked through the container's 64 MB tmpfs and died with `[Errno 28] No space left
   on device`** while fetching numpy (16.9 MB) and pandas (11.3 MB). Every package with real
   dependencies would have failed. `TMPDIR` now points at the bound staging directory, and pip's
   scratch is removed before publish so it never reaches the import path.

### Missing dependencies are resolved BEFORE the snippet runs

What used to happen, measured on HPC3 by running it:

| the model does | what actually happened |
|---|---|
| `import pyranges` | `ModuleNotFoundError`, with nothing pointing at a tool that could fix it |
| `pip install pyranges` (the tool description invited this) | wheel build failed — the container's `/tmp` is a 64 MB tmpfs |
| `pip install humanize` (small, pure Python) | **`pip returncode: 0`, then `ModuleNotFoundError` in the same snippet** |
| the next step | nothing survives; a fresh container each time |

The third row is the worst: `--containall` gives an empty `$HOME`, so
`~/.local/.../site-packages` did not exist when the interpreter started and is therefore not on
`sys.path`. Installing into it mid-process changes nothing. A model reading "install succeeded"
and "module missing" together has no way to reason its way out, and the run_code tool description
was actively steering it there ("pip install of analysis packages is fine if a step needs one") —
now removed.

So the check moved to parse time. `code_imports.top_level_imports` walks the AST (not a regex —
`import` appears in strings and comments), drops stdlib and relative imports, and
`PreflightingExecutor` probes what remains against the container *and* the shared cache. Anything
missing is resolved in **one** confirmation before a single CPU-second is spent.

**The confirmation states a plan AiScientist will carry out, not a command for the user to run.**
The product promise is that a user never touches HPC3 themselves, so a prompt that reads like
homework breaks the promise even when the mechanism works.

Verified end to end on HPC3 from an **empty** cache, driving the shipped classes:

```
[step] This step needs pyranges — not in the analysis image. Asking before installing.
[install] Install "pyranges" on HPC3 so this analysis can continue
          AiScientist will do this for you — you do not need to log in anywhere: …
[success] Installed pyranges into the lab-shared cache

status: ok | returncode: 0
preflight: required=[pyranges, pandas]  missing=[pyranges]  installed=[pyranges]
    ANALYSIS RAN. intervals: 3 / merged: 3 / pandas in use: 2.3.3

STEP 2 (a later step):  prompts shown: 0   ·   SECOND STEP SEES IT: 0.1.4
```

**Two more bugs surfaced only by running it, both fixed and regression-tested:**

1. **`missing_modules` reported everything as missing on a first run**, and offered to install
   `pandas` — which the image has. Cause: Singularity aborts the whole container with **exit 255**
   when a bind source does not exist, and the (not yet created) cache root was being bound. Now
   the root is bound only if it exists, and **only exit 3 counts as missing** — any other status
   means the probe did not run, so the module is assumed present. A false "missing" costs the user
   a bogus confirmation, and a confirmation the user learns to distrust is worse than none.
2. **`ensure_sitecustomize()` was never called** — dead code. `PYTHONPATH` pointed at a directory
   that did not exist, so nothing was appended to `sys.path` and a *successfully installed*
   package still failed to import. Now created when the cache is constructed.

### What is NOT verified

Everything cluster-shaped is unproven until it runs on HPC3:

* `srun --jobid=<id> --overlap` — the mechanism the whole shell rests on. Slurm version and site
  configuration decide whether a step can share the holder's allocation.
* Whether the free `standard` partition hands out a CPU node quickly enough that lazy allocation
  feels instant rather than like the GPU queue it replaces.
* Real provider accounts — every verification path was exercised against a stub.

(The package cache and its filesystem assumptions are no longer on this list: both were measured
on HPC3, see above.)

Enabling any of this is opt-in: `BIOAGENT_WORKER_NODE=1` for the worker (and therefore for
`run_shell`/`fetch_url`/`install_package`). Without it the metadata tools still work and cost
nothing.
