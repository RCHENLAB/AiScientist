# AiScientist

AiScientist is a web research agent for biomedical data: single-cell RNA-seq, VCFs and clinical
phenotypes. A researcher adds a dataset and asks a question in plain language. LLM roles then work
as a small lab: the PI drafts a plan, the researcher reviews it, the Scientist runs each step with
curated tools or its own code, the Critic checks every step, and the run ends in a cited,
manuscript-style report (PDF and DOCX) with its figures and tables.

It is built for the UCI vision research lab and runs at <https://AiScientist.eye.som.uci.edu>. The
site is open on the public internet. An account is registered with a `@uci.edu` email or created
by an admin, and a run needs the user's own UCI HPC3 account.

## How it works

```
Browser ──HTTPS──▶ Envoy ingress
                        │
                        ▼
        Gateway on the lab server (FastAPI + WebSocket, Postgres):
        accounts, chats, run orchestration, report assembly
                        │  one SSH session per user
                        ▼
        UCI HPC3 (Slurm + Apptainer): the LLM on a GPU, and every tool job
        (single-cell analysis, variant annotation, phenotype ranking,
        literature, report rendering)
```

1. **Sign in and connect.** The gateway opens an SSH session to HPC3 as the user, with their
   password and Duo or with a saved SSH key.
2. **Choose the model.** The model a session chooses answers all of its calls, the tool-calling
   Scientist included: Qwen3.8-27B in a Slurm GPU job of the user's own (it waits for a free card),
   the lab's shared model (DeepSeek-V4-Flash, one long-lived job on several HPC3 GPUs; no wait), or
   the user's own LLM API key (no wait; [guide](docs/claude_api_guide.md)). The gateway reaches a
   cluster model through an SSH tunnel. What a key spends is tracked under Account › Usage.
3. **Add data.** Upload it in the browser (resumable), or point to data already on the lab server.
4. **Ask.** A quick question is answered in the chat. A research question becomes a plan, a
   dependency graph of steps, which the user approves, edits or cancels before anything runs.
5. **Run.** For each step the Scientist calls tools, most of them Slurm jobs on HPC3, and the Critic
   accepts the result or sends the step back.
6. **Report.** The claims are audited against the tool results, the report is written and rendered,
   and the figures, tables and a bundle of the run can be downloaded.

The diagrams, the code layers and the tool routing are in
[`docs/architecture/README.md`](docs/architecture/README.md).

## Design rules

- **The data stays on UCI systems.** Datasets live on the lab server and on HPC3, and with the
  default model the prompts stay there too. `DataBoundaryGuard` refuses a prompt that carries a raw
  data table or a secret. Outside calls go to public reference services such as Europe PMC and
  Ensembl, and to an LLM provider only when the user brings their own key.
- **Heavy compute runs on HPC3 as Slurm jobs.** The gateway only orchestrates. HPC3 login nodes are
  used to log in and submit jobs, and files are transferred through `access-hpc3.rcic.uci.edu`.
- **Results are checked.** A tool reports a failure instead of faking success, the Critic
  cannot accept a step whose tool call failed or returned nothing, and the report's claims and
  numbers are checked against the tool results before and after it is written.
- **Every dataset and result is described.** Each carries a Croissant 1.1 `croissant.json` (files,
  checksums, table columns, ontology terms, license, and for a result its inputs and run), which its
  owner reviews on the Datasets page and MCP clients read at `/mcp`. See
  [`docs/development/metadata.md`](docs/development/metadata.md).

## Tools, skills and pipelines

| | What it is | Where |
|---|---|---|
| Tool | Fixed code the Scientist calls by name | [`src/aiscientist/tools/`](src/aiscientist/tools/README.md), one folder per tool |
| Skill | A code template the Scientist adapts and runs | [`skills/`](skills/README.md) |
| Pipeline | An end-to-end protocol the PI picks when planning | [`preset_pipelines/`](preset_pipelines/README.md) |

Adding one of these is adding a folder. No platform code changes.

## Run it

```bash
./deploy.sh              # create the virtual environment and install (safe to re-run)
./start.sh               # serve the console on 127.0.0.1:8800
python3 -m pytest        # the test suite: offline, no cluster, .env or network needed
```

- **Without a cluster:** tick "Mock mode" in the console, or set `AISCIENTIST_LLM_BASE_URL` to
  any OpenAI-compatible endpoint. Storage and compute still expect HPC3.
- **Off campus:** the public site needs no VPN. A gateway run on your own computer does, because
  HPC3's SSH port answers only on the UCI campus network or the VPN.
- **Configuration:** every setting is described in
  [`configs/aiscientist.example.env`](configs/aiscientist.example.env).
- **Accounts:** `aiscientist-admin` manages users from the command line.
- **Production:** a systemd unit on the lab server behind the Envoy ingress. The runbook is
  [`deploy/README.md`](deploy/README.md).

## Repository layout

```text
src/aiscientist/
  gateway/       the web app: accounts, the HPC3 session, LLM clients, Slurm job executors
  agents/        the research lab (PI, Scientist, Critic), the planner, the fast chat path
  tools/         every model-callable tool, one folder each
  reporting/     report rendering, the result bundle, the reference list
  metadata/      Croissant metadata for datasets and results
  hpc/           the HPC3 shell session and its tools
frontend/console/  the browser UI
skills/            code templates for the Scientist
preset_pipelines/  protocols for the PI
deploy/            the systemd unit, Kubernetes and Envoy config, HPC3 container definitions
configs/           the configuration reference
docs/              architecture, findings, HPC3 storage and assets
tests/             the offline test suite
```

## Documentation

- [`docs/architecture/README.md`](docs/architecture/README.md): how the system is built.
- [`docs/FINDINGS.md`](docs/FINDINGS.md): what has been measured and decided, and why.
- [`src/aiscientist/tools/README.md`](src/aiscientist/tools/README.md): the tool index.
- [`docs/skills_and_pipelines_architecture.md`](docs/skills_and_pipelines_architecture.md): tools,
  skills and pipelines in detail.
- [`docs/hpc3_storage_layout.md`](docs/hpc3_storage_layout.md) and
  [`docs/hpc3_assets.md`](docs/hpc3_assets.md): what lives where on HPC3, and what is cleaned up.
- [`deploy/README.md`](deploy/README.md): deploying and updating the server.
- [`docs/claude_api_guide.md`](docs/claude_api_guide.md): using your Claude subscription's API
  credits with AiScientist, keeping the key safe, and watching what it spends.
- [`docs/development/metadata.md`](docs/development/metadata.md): the metadata guideline, the
  metadata API and the MCP server.
- [`CONTRIBUTING.md`](CONTRIBUTING.md): how to contribute.

Released under the [MIT License](LICENSE).
