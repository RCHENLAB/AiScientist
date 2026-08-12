# AiScientist

**A privacy-first, multi-agent bioinformatics research console for the UCI vision /
ocular-biology lab.** A researcher logs in through the browser, points it at a dataset,
and asks a scientific question in plain language. Behind the login, the console connects
to **UCI HPC3** (SSH + Duo), serves an open-weights LLM (**Qwen3.6-35B-A3B**) on a GPU
via Slurm + vLLM, and runs a role-based *research lab* — Principal Investigator →
Scientist → Critic — that plans the work, executes real single-cell analysis and
literature retrieval, and streams back a citable, publication-shaped report with
downloadable figures, tables, and a PDF/DOCX manuscript.

Public deployment: **<PUBLIC_HOSTNAME>** (UCI-network / VPN).

---

## Why it exists

Single-cell and spatial analyses are a long chain of judgement calls — QC thresholds,
clustering resolution, differential expression, enrichment, marker interpretation,
literature grounding — that a domain scientist normally does by hand across many tools.
AiScientist runs that chain as a supervised agent team so a researcher gets a
first-draft, reproducible analysis and manuscript from a question and a dataset, while
**raw data and the model stay on UCI infrastructure**.

Design posture:

- **Privacy-first.** The LLM runs on UCI HPC3 and never leaves campus. Raw datasets stay
  server-side; `DataBoundaryGuard` blocks raw rows and secrets from ever reaching a
  prompt; external literature queries are sanitized (a bare filename and the question,
  never data).
- **Heavy compute stays on HPC3.** GPU inference, `run_code`, scanpy analysis, report
  rendering, scGPT, and VL review all run as Singularity-contained Slurm jobs on the
  cluster — never silently on the web server.
- **Honest over impressive.** Tools report failures instead of faking success; a
  deterministic guard refuses to let the Critic "accept" a step whose tool run errored
  or returned nothing. Step degradations are recorded in a diagnostics report, and the
  final manuscript renders clean.

---

## What a run looks like

1. **Log in & connect.** A AiScientist account (admin-created; no self-signup, `@uci.edu`
   for registration) gates the app. The console then SSHes to `hpc3.rcic.uci.edu` with
   your UCInetID + interactive **Duo**. HPC3 credentials are never stored — only a bcrypt
   hash of the *app* password lives in the DB.
2. **Serve the model.** A per-user Slurm GPU job serves `QuantTrio/Qwen3.6-35B-A3B-AWQ`
   via **vLLM** (Singularity, OpenAI-compatible `/v1`) on a dynamic port; the console
   SSH-tunnels to it. Jobs are per-user-isolated (`squeue --me`) and the model is warmed
   on connect so the first query isn't stuck on the cold load from shared DFS.
3. **Upload a dataset.** Resumable chunked upload; the file streams straight to your
   HPC3 area (`/dfs3b/ruic20_lab/<user>/uploads/`) — the gateway keeps no copy.
4. **Run the research lab.** The PI plans; the Scientist executes real tools; the Critic
   judges each step; the loop converges. Progress streams per-role, token-by-token
   (including the model's visible thinking), with a plan-mode pause for human
   review/edit before anything runs.
5. **Results.** Per-run artifacts in `runs/console/<ucinetid>/<run_id>/` — figures,
   tables, a citable PDF/DOCX manuscript, and a downloadable bundle. Reports can be
   regenerated without re-running the analysis.

---

## Architecture — two tiers

```
Browser ──HTTPS──▶ Envoy Gateway (<PUBLIC_HOSTNAME>)
                        │
                        ▼
        eyeserver: FastAPI + WebSocket console  ← accounts, chat history, uploads,
        (systemd `bioagent.service`, Postgres)    orchestration, report assembly
                        │  SSH + Duo, per-session tunnel
                        ▼
        UCI HPC3 (Slurm): vLLM GPU serve (Qwen3.6-35B) · run_code · scanpy analysis ·
        pandoc/XeLaTeX report render · scGPT · VL review — all Singularity-contained
```

The web console (`src/bioagent/gateway/`) is a deployable FastAPI + WebSocket app; the
compute lives on HPC3 behind SSH. The two are decoupled by per-session SSH tunnels and
Slurm job submission, so the eyeserver holds no GPU and no persistent dataset copy.

---

## The research lab (PI → Scientist → Critic)

The live agent system is a role-based loop in `src/bioagent/agents/`:

- **PI** turns the question into an ordered agenda of concrete steps (plan-mode pauses
  here for human review/edit — the same human-in-the-loop gate as the Duo prompt).
- **Scientist** (`ResearchHarness`) executes each step by **calling tools** on the loaded
  dataset — one or more per turn, reading each result before continuing.
- **Critic** judges each step `accept | revise` with a score and a specific critique; a
  deterministic guard refuses to accept a step whose tool run errored or returned nothing
  (the model-critic can't rubber-stamp a failure).
- On convergence, a deterministic, **no-AI** report is assembled (pandoc → PDF/DOCX),
  embedding the exact figures/tables the tools produced, with a formatted references
  section built from accepted literature citations.

### DAG planning & real multi-agent (0.2.0)

Beyond the linear pipeline, the lab can plan the work as a **dependency DAG** and run it
with a team of specialists — all **feature-flagged and additive** (default off = the
0.1.0 linear pipeline runs unchanged):

- **DAG planner** (`agents/dag.py`) — a structure pass turns the agenda into a
  `LabPlan` of `TaskNode`s with explicit dependencies; a ready-set scheduler runs nodes
  as their prerequisites complete. Scoped per-node briefs ("do ONLY this task; earlier
  outputs already exist") structurally cure the double-QC / step-1-runs-everything
  failure modes.
- **Coordinator + expert claiming** — a Coordinator picks the next ready task; specialists
  **claim** tasks by expertise fit (real multi-agent, not a fixed router).
- **Safe concurrency** — independent branches (e.g. literature vs analysis) run in
  parallel when their mutable footprints are disjoint; opt-in via
  `BIOAGENT_MAX_CONCURRENCY`.
- **Per-agent evolving memory** (`agents/agent_memory.py`) — each specialist keeps a
  private, disk-backed memory (`episodes.jsonl` + distilled `lessons.md`) that it recalls
  into its brief and reflects on across runs (in-context learning on frozen weights).

Flags (gateway env, all default off): `BIOAGENT_PLANNER=dag`,
`BIOAGENT_MAX_CONCURRENCY=<n>`, `BIOAGENT_AGENT_MEMORY=1`. See
[`docs/dag_planner_design.md`](docs/dag_planner_design.md) and
[`docs/agent_memory_design.md`](docs/agent_memory_design.md).

### The Scientist's tool catalog (`agents/registry.py`)

- **Single-cell analysis** (`tools/scrna_pack.py`) — real scanpy QC → clustering → DE →
  gseapy Enrichr enrichment, emitting deterministic scanpy/matplotlib figures and tables;
  runs as a Singularity Slurm job on HPC3.
- **`run_code`** — a CodeAct sandbox that reads the run's dataset + checkpoints and writes
  new artifacts, with a real per-snippet memory cap (runs on HPC3).
- **Literature** — `literature_search` (Europe PMC keyword retrieval) and
  `deep_literature` (`tools/paperqa_search.py`, PaperQA2 RAG grounded on the on-host Qwen
  + local embeddings — nothing leaves campus); `tools/literature_references.py` formats
  the manuscript's references from accepted citations.
- **`scgpt_annotate`** — per-cell type annotation via scGPT (separate short-lived GPU
  batch job).
- **`schematic`** — a deterministic graphviz workflow diagram. **`finish`** — return the
  final answer.

`DataBoundaryGuard` gates the brief before any model call. The whole loop runs offline in
tests via an injected `chat_fn` with scripted tool calls.

---

## HPC3 / RCIC login boundary

- HPC3 host `hpc3.rcic.uci.edu`; interactive access over SSH. Password login requires
  **Duo**, and users must be on the UCI campus network or VPN.
- **Data transfer goes to `access-hpc3.rcic.uci.edu`, never a login node.** RCIC's 2026-08-06
  notice reserves `login-i15/16/17` for logging in and submitting Slurm jobs — not compute, and
  not `rsync`/`SFTP`/`rclone`/`wget` — and says offending processes may be killed. The gateway
  honours this automatically: `put_file`/`get_file` open their own connection to
  `BIOAGENT_HPC_TRANSFER_HOST` while every `exec`, Slurm call and tunnel stays on the login node.
  That host mounts the same `$HOME` and `/dfs3b`, and its shell is restricted to transfer
  commands (`rsync`/`wget`/`curl`, not `bash`), so scripts still run on `hpc3.rcic.uci.edu`.
- User SSH keys must have a non-empty passphrase and must not be shared.
- High-compute GPU/CPU work runs on HPC3 through Slurm — never silently on the web
  server. Heavy single-cell, spatial, and multi-omics work belongs on the cluster.

References: `https://rcic.uci.edu/account/login.html`,
`https://rcic.uci.edu/slurm/jobs.html`.

---

## Deploy & run

**Production (eyeserver).** The console runs as a host systemd unit (`bioagent.service`,
user `aiscientist`, `/data/BioAgent/app`), bound to the node IP on `:8800`, fronted by
the `aiscientist` Kubernetes Service → Envoy Gateway at `<PUBLIC_HOSTNAME>`.
Config/secrets are read from `/data/BioAgent/app/.env` (see
`configs/aiscientist.example.env` and [`deploy/`](deploy/)). Redeploy backend changes with
`./deploy/redeploy.sh` (rsync + `systemctl restart` — drops live sessions; there is no
zero-downtime backend path). Full deploy kit, TLS, and systemd notes:
[`deploy/README.md`](deploy/README.md), [`docs/archive/hpc3_console.md`](docs/archive/hpc3_console.md).

**Local / dev.**

```bash
./deploy.sh                       # create the venv + install (idempotent)
./start.sh                        # bind 127.0.0.1:8800 — SSH-tunnel to view
# or: bioagent-console --port 8800     (tick "Mock mode" to demo without a cluster)
```

Off-cluster testing can point the LLM at any OpenAI-compatible endpoint via
`BIOAGENT_LLM_BASE_URL` (e.g. OpenRouter) — a **test convenience**, not the product path
(storage + compute still assume HPC3; see [`docs/BACKLOG.md`](docs/BACKLOG.md)). Admin
user management is `bioagent-admin`.

---

## Versions & rollback

- **`v0.1.0` — "pipeline"** (git tag on `main`): the linear PI→Scientist→Critic pipeline.
  This is the **frozen rollback snapshot** — a known-good, deployable state. Roll back
  with `git checkout v0.1.0 && ./deploy/redeploy.sh`.
- **`0.2.0` — "DAG"** (this line): adds the DAG planner, real multi-agent, per-agent
  memory, and safe concurrency — all feature-flagged over 0.1.0. Merges to and is
  maintained as the mainline.

See [`handoff/yijun/HANDOFF.md`](handoff/yijun/HANDOFF.md) for the release model, the
rollback procedure, and the DAG ↔ literature merge-coordination notes.

---

## Test

```bash
python3 -m pytest        # offline; no cluster, .env, or network needed
```

---

## Package layout

```text
src/bioagent/
  agents/        the research lab (PI/Scientist/Critic), DAG planner, agent memory,
                 tool registry, CodeAct sandbox, provenance
  gateway/       web console: SSH+Duo, Slurm vLLM serve + tunnel, accounts, chat history,
                 uploads, report assembly, HPC3 offload (analysis/report/scGPT/VL review)
  tools/         real analysis (scanpy/gseapy), literature (Europe PMC + PaperQA2),
                 report (pandoc), scGPT annotation, visual review, schematic, datasets
  lab/           research-lab support
  providers/     OpenAI-compatible LLM client (+ fallback)
  integrations/  DataBoundaryGuard safety (+ dormant adapters)
  hpc/           Slurm boundaries
  core/          shared config
frontend/console/  the browser UI (served from disk per request)
deploy/            systemd unit, k8s/Envoy, nginx, HPC3 container defs, redeploy kit
```

---

## Status

**Implemented + tested** (485 offline tests): the web console (accounts, SSH/Duo, Slurm
vLLM serve + tunnel, GPU isolation, mid-run tunnel/serve auto-recovery), the linear
PI→Scientist→Critic lab, the DAG planner + real multi-agent + per-agent evolving memory
(feature-flagged), the real scanpy/gseapy analysis line, the `run_code` sandbox, the
literature line (Europe PMC + PaperQA2) with manuscript references, deterministic pandoc
PDF/DOCX reports with regenerate-without-rerun, HPC3 offload of uploads/analysis/report,
and server-side chat history + resumable upload.

**Direction** (see [`handoff/`](handoff/) and [`docs/BACKLOG.md`](docs/BACKLOG.md)):
LangGraph + Postgres-checkpointer port of the loop, provenance stamping toward
Kosmos-parity, multi-cycle research loops, and a pluggable backend so a user can bring
their own external API in place of HPC3.
