# HPC3 SSH + Ollama Console

A real, deployable web console that connects to UCI **HPC3**, ensures a **GPU**
allocation, auto-detects / installs **Ollama**, and drives **Qwen3** for
research chat — replacing the earlier placeholder "HPC connection" modal.

> Code: `src/bioagent/gateway/`. Frontend: `frontend/console/`.

## What it does (end to end)

On **Connect**, the gateway runs this pipeline and streams every step to the UI:

1. **SSH login** — real `paramiko` session to `hpc3.rcic.uci.edu`, either
   password + Duo push (keyboard-interactive) or an SSH key with passphrase.
2. **Ollama detect / install** — checks for the `ollama` binary on the login
   node; if missing, installs it **unprivileged** into shared `$HOME`
   (`$HOME/.bioagent/ollama`, no root). If present, it is reused.
3. **GPU allocation (Slurm)** — submits a GPU job that runs `ollama serve` on a
   compute node, and waits until it is `RUNNING`. **Every connect guarantees a
   real GPU was allocated** — if the queue stalls or the job fails, you are told.
4. **Serve + tunnel** — the serve job binds a *dynamic free port* on its compute
   node (so users sharing a GPU node never collide) and records it in
   `$HOME/.bioagent/ollama.port`; the gateway reads it and opens an SSH
   local-port-forward on a *dynamic local port* too. Both ends are per-session, and
   the console feeds each session's live tunnel port into that run's Biomni/Kosmos
   endpoint — so concurrent users (or research teams) never share a port anywhere.
5. **Model ready** — over the tunnel, checks `/api/tags`; pulls the model
   (streaming progress) if it isn't present yet.
6. **Live** — Qwen3 is reachable; the chat box turns on.

A background **GPU health watchdog** then polls `nvidia-smi` on the allocated
node every ~20s. If the GPU link drops, the job ends, or utilization looks
abnormal, an **error event** is pushed to the log immediately.

Every failure is surfaced with its **full cause** — the user-facing message, the
failing command, its exit status, stdout/stderr, and a Python traceback — behind
a "Show full cause" toggle in the log panel. Nothing is swallowed.

## Chat drives the AiScientist multi-agent pipeline

With the **"Run AiScientist pipeline"** toggle on (default), a chat message is not a
plain LLM reply — it runs the full `VisionResearchAgent` multi-agent pipeline
(`src/bioagent/workflows/vision.py`):

- The 13 agents run in order (Coordinator → Kosmos tool selection → Biomni plan →
  single-cell QC → differential expression → literature grounding → generated-code
  → **Slurm dry-run** → validation → research evaluation → **Reporter**).
- The deterministic tools execute and write **real artifacts**; each agent's
  progress streams to the log panel live (via the pipeline's `on_step` hook).
- The **Reporter's LLM is pointed at the tunneled HPC3 Qwen3**
  (`http://127.0.0.1:<tunnel_port>/v1`, model `qwen3`) — so Qwen3 actually writes
  the planning note inside the report. No process-global env mutation: the
  endpoint is injected via `VisionResearchAgent(..., llm_client=...)`.
- The final `final_report.md` streams into the chat, and every artifact
  (QC/DE/literature/Slurm script/report) appears as a **download link**, served
  by `GET /api/artifacts/{connection_id}/{name}`.

Untick the toggle for a plain Qwen3 conversation (no tools, no artifacts).

Safety is unchanged: the Slurm step is still **dry-run only** (writes a script,
never `sbatch`), no raw data is uploaded, and generated code stays sandboxed to
derived JSON artifacts.

## Run it

Install gateway deps (kept separate from the dependency-light core):

```bash
pip install -r requirements-gateway.txt      # or: pip install -e ".[gateway]"
```

Start the console:

```bash
PYTHONPATH=src python3 -m bioagent.gateway --port 8800
# then open http://127.0.0.1:8800/
```

### Mock mode (no cluster needed)

Tick **"Mock mode"** in the UI (or send `"mock": true` to `/api/connect`). An
in-process simulator (`mock_host.py`) plays the whole HPC3 + Ollama flow —
install, GPU job, serve, model pull, health, and a simulated Qwen3 reply — so you
can demo and develop the UI with no credentials and no network.

### Real HPC3 run

You must be **on the UCI campus network or VPN**. Fill in your UCInetID and
password, tick the campus-network box, and Connect; approve the **Duo push** on
your phone when prompted. Or choose **SSH key** and give the key path +
passphrase.

## Strict per-user isolation (one project never affects another)

The lab runs many projects on the same charge account, so the console is
**strictly scoped to your own jobs** and can never touch anyone else's:

- The serve job is named **per user** — `bioagent-ollama-<ucinetid>`.
- Finding a job to reuse uses `squeue --me` + that per-user name, so it only ever
  matches **your own** running job. Reconnecting reuses *your* GPU server instead
  of starting a second one (no redundant allocation for you).
- **Stop my GPU job** runs `scancel` **only on your own job** — guarded in the
  backend (refuses if the owner isn't you) and by Slurm's own permissions. There
  is no button anywhere that can cancel a shared or another member's job.

On GPU-SU economics: because charging is by allocated-resource × wallclock (an
idle GPU still costs ~34 SU/hr — see below), the right pattern is a **short
walltime**, reusing your own job while you work, and clicking **Stop my GPU job**
when you're done so you stop paying. A truly lab-wide *shared* endpoint (one
server everyone reads) is a deliberate **admin** decision to deploy once — not
something this per-user tool creates or lets anyone tear down.

## How HPC3 charges (RCIC Service Units)

Charging is **usage-based but by allocated resources × wallclock time**, not a
flat subscription and not by actual utilization (an idle GPU still charges):

- CPU: 1 core-hour = 1 SU.
- GPU: **≥ 34 SU/hour** for a single GPU (32 for the GPU + 2 required CPU cores).
- RUIC20 GPU pool: ~23,863 SU available ≈ **~700 GPU-hours for the whole lab**
  until the next reallocation. One GPU left on 24/7 ≈ 816 SU/day — it would drain
  the pool in about a month. Hence: on-demand + short walltime + Stop-when-done.

Source: <https://rcic.uci.edu/slurm/jobs.html>.

## First-time HPC3 setup (UCI RUIC20)

From the account-creation email, the one-time steps are:

1. **Follow your welcome email** to confirm SSH access + **Duo** to
   `hpc3.rcic.uci.edu` (you must be on campus network or UCI VPN).
2. Your accounts: group `ruic20_hpc`, Slurm charge accounts `ruic20_lab` (CPU)
   and **`ruic20_lab_gpu` (GPU)** — the console defaults GPU jobs to the latter.
3. Lab storage is `/dfs3b/ruic20_lab/<ucinetid>` (yours: `/dfs3b/ruic20_lab/<ucinetid>`).
   **Run `newgrp ruic20_hpc` before `cd`/reading/writing under `/dfs3b/ruic20_lab/`.**
   (The console keeps the Ollama install in your `$HOME`, so it doesn't need this;
   `newgrp` matters when your analysis jobs touch lab **data**.)
4. Copy `configs/aiscientist.example.env` into your `.env` (or `export` it) before
   launching the console.

## Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `BIOAGENT_HPC_HOST` | `hpc3.rcic.uci.edu` | SSH host |
| `BIOAGENT_SLURM_PARTITION` | `gpu` | GPU partition |
| `BIOAGENT_SLURM_ACCOUNT` | `ruic20_lab_gpu` | Slurm GPU charge account |
| `BIOAGENT_SLURM_GRES` | `gpu:1` | GPU request |
| `BIOAGENT_SLURM_CPUS_PER_TASK` | `8` | CPUs for the serve job |
| `BIOAGENT_SLURM_MEMORY_GB` | `32` | Memory for the serve job |
| `BIOAGENT_SLURM_TIME_LIMIT` | `02:00:00` | Walltime for the serve job |
| `BIOAGENT_OLLAMA_MODEL` | `qwen3` | Model tag to serve (the proposal's "Qwen 3.6" → a Qwen3 tag) |
| `BIOAGENT_OLLAMA_PORT` | `11434` | Fallback compute-node port (the serve job picks a dynamic free port; this is only used if the pick fails) |
| `BIOAGENT_OLLAMA_LOCAL_PORT` | `0` | Local tunnel port. 0 = auto per session (recommended; the console feeds the live port to Biomni/Kosmos). Pin only for an external tool that needs a fixed local URL |
| `BIOAGENT_OLLAMA_HOME` | `$HOME/.bioagent/ollama` | Unprivileged install + model dir (shared FS) |
| `BIOAGENT_OLLAMA_INSTALL_URL` | ollama linux tgz | Source for the unprivileged install |
| `BIOAGENT_GPU_POLL_SECONDS` | `20` | GPU health poll interval |

## Architecture

```
frontend/console/  (clean SPA: pipeline, GPU health meter, chat, error log)
        │  REST: /api/connect /api/chat /api/disconnect
        │  WS:   /ws/{id}   (live events, chat tokens, gpu health)
        ▼
gateway/app.py        FastAPI app + per-connection pub/sub event stream
   ├── ssh_gateway.py  real paramiko SSH (password+Duo / key), exec, port-forward
   ├── ollama.py       detect / unprivileged install / HTTP pull + chat stream
   ├── gpu.py          Slurm serve-job allocation + nvidia-smi health
   ├── mock_host.py    in-process HPC3 + Ollama simulator (mock mode)
   ├── settings.py     env-driven HPC/Ollama config
   └── errors.py       structured, fully-printed errors + event model
```

The gateway logic depends only on a small `RemoteExecutor` interface, so the
exact same provisioning + chat flow runs against real SSH and the mock host.

## Security model

- The **backend** holds the SSH session; the **browser never holds SSH keys**.
- The password is used **only** for the SSH handshake + Duo and is not persisted
  to disk or session storage.
- Run the gateway on a host you control (your laptop on VPN, or the lab server),
  not as a shared public service, unless you put it behind real auth.
- Ollama is installed unprivileged in your `$HOME`; no root is used or needed.
- The GPU job keeps running until its Slurm time limit or `scancel`; disconnecting
  the console does not kill it (so you can reconnect to the same GPU).

## Limitations / next steps

- The live HPC3 path is built to RCIC conventions but **must be validated on the
  real cluster** (account/partition/gres specifics, Duo prompt wording, and
  whether your allocation allows interactive GPU serve jobs). Mock mode is the
  only path verifiable off-network.
- Chat now drives the full `VisionResearchAgent` pipeline with Qwen3 as the
  Reporter LLM (see "Chat drives the AiScientist multi-agent pipeline"). The pipeline
  is still a **fixed-order** workflow whose tools are deterministic Python — the
  LLM does not yet *choose* tools via function-calling (that is the "Option B"
  agentic upgrade: an Ollama tool-calling loop). Passing a real dataset into the
  pipeline from the console (currently planning/QC runs without an uploaded
  dataset) is the other near-term step.
- The Duo handler auto-selects the push option ("1"); passcode-only accounts may
  need a small tweak.
