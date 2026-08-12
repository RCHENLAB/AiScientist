# Biomni + Kosmos — Real Integration Design

**Decision:** integrate the *real* Biomni (biomedical tool agent) and Kosmos
(autonomous research loop) behind AiScientist's existing safety adapters, driven by
the **local Qwen3.6** model — not cloud LLMs. The current `BiomniAdapter` /
`KosmosKernelAdapter` are scaffolds (status `partial` in `agent_registry.yaml`);
this turns them into real runtime clients.

## 1. Both run on the eye server, LLM on HPC3

| Runs on | What | Why |
| --- | --- | --- |
| **eye server** | Biomni (+ 11GB data lake), Kosmos (+ Docker sandbox), AiScientist agents, frontend | CPU + 256GB RAM + /data 6.9TB + public-facing **internet egress** (needed for the data lake + literature). HPC3 compute nodes usually have no egress. |
| **HPC3** | Qwen3.6 on GPU (the LLM endpoint both frameworks call), + Phase-2 Slurm compute | the only GPU |

Both frameworks support a **local OpenAI-compatible / Ollama endpoint**, so they
point at the tunneled Qwen3.6 — nothing goes to a cloud LLM.

## 2. Directory layout on the eye server (`/data`, not `/home`)

Per the eye-server admin note, project data goes under **`/data`** (the 6.9TB
disk); `/home` is tiny. Recommended layout:

```
/data/BioAgent/
├── miniconda/         # conda on /data
├── env/               # Python 3.11 env (biomni + kosmos deps)   ┐ shared (software +
├── biomni_data/       # Biomni data lake ~11GB (public ref data) ┘ public ref → one copy)
├── kosmos/            # Kosmos clone
├── app/               # this repo (the gateway)
└── users/<ucinetid>/  # per-user results / personal data  (BIOAGENT_RESULTS_DIR)
```

The data lake and env are **shared** (one copy for the lab — the 11GB lake is
public reference data, no reason to duplicate per user). Only `users/<ucinetid>/`
is per-user. Set `BIOAGENT_RESULTS_DIR=/data/BioAgent/users`.

## 3. Privacy posture (no raw-data leakage)

- **LLM = local Qwen3.6** → reasoning + your data never reach a cloud model. This
  is the main leakage vector, and it's closed.
- **Biomni data lake download** = pulling *public* reference DBs **in** (download),
  not uploading your data. Safe. One-time, on the eye server.
- **Literature / public-DB search** (PubMed/ArXiv/Semantic Scholar) sends *queries
  out*. Two postures:
  - **air-gapped:** disable it (the existing `allow_literature_network=False`
    default) → nothing leaves except the one-time data-lake download.
  - **on + sanitized:** only public topic terms (organism/method/public gene
    names) go out, enforced by `DataBoundaryGuard` — never raw data, IDs, secrets.
- **Code sandbox:** Kosmos uses Docker for code execution (falls back to unsafe
  `exec()` without it) — install Docker on the eye server so generated code is
  sandboxed.

## 4. LLM config → Qwen3.6

Assumes Qwen3.6 reachable from the eye server at `http://127.0.0.1:<port>` (an
SSH tunnel to the HPC3 Ollama serve job; see §6).

**Biomni:**
```python
from biomni.agent import A1
agent = A1(
    path="/data/BioAgent/biomni_data",
    llm="qwen3.6:35b-a3b",
    source="Custom",                          # or source="Ollama"
    base_url="http://127.0.0.1:11434/v1",     # Ollama OpenAI-compatible
    api_key="ollama",
)
```

**Kosmos** (`.env`, via LiteLLM):
```
LLM_PROVIDER=litellm
LITELLM_MODEL=ollama_chat/qwen3.6:35b-a3b
LITELLM_API_BASE=http://127.0.0.1:11434
# air-gapped first run:
ENABLE_LITERATURE=false
```

## 5. Wiring the adapters (keep the safety layer in front)

- `BiomniAdapter.build_execution_plan(...)` stays (the policy + capability gate);
  add `BiomniAdapter.run(task)` that calls `A1.go()` **only after**
  `DataBoundaryGuard` sanitizes inputs and the policy allows it.
- `KosmosKernelAdapter` stays (tool registry + `DataBoundaryGuard`); add a
  `KosmosRuntimeClient` that invokes `kosmos run` (the subprocess path already in
  `eval/comparison.py`) with the LiteLLM→Qwen3.6 env.
- The autonomous loop (`eval/autonomous_loop.py`) can delegate its research-loop
  reasoning to Kosmos and its specialist tool calls to Biomni; AiScientist keeps
  intake, privacy, validation, claim control, and reporting.

## 6. Phased plan

| Step | What | Needs eye server / HPC3 |
| --- | --- | --- |
| **I1** | Install Biomni on `/data`, point at Qwen3.6, run a tiny task (network off) | eye server + Qwen3.6 endpoint |
| **I2** | Install Kosmos on `/data`, LiteLLM→Qwen3.6, `kosmos run` a tiny question | eye server + Qwen3.6 endpoint |
| **I3** | `BiomniAdapter` → real `A1.go()` behind the safety policy | code (me) + validation (you) |
| **I4** | `KosmosRuntimeClient` → real `kosmos run` in the autonomous loop | code (me) + validation (you) |
| **I5** | CI: make biomni/kosmos **optional extras** so the lightweight core/tests still pass offline | me |

## 7. Constraints to watch (real-cluster)

1. **Heavy infra**: Biomni's scientific stack + 11GB lake; Kosmos wants Docker
   (+ optional Neo4j/Redis). Real setup on the eye server.
2. **Tool-calling reliability**: both rely on function/tool calling; the open
   model must reliably emit tool calls. Qwen3 supports it — validate first.
3. **LLM throughput + SU**: autonomous loops make many LLM calls → the single
   HPC3 GPU serve may bottleneck and SU rises. Consider a **persistent** Qwen3.6
   serve with a stable port instead of an ephemeral per-connection tunnel.
4. **License check**: confirm Biomni + Kosmos licenses are OK for lab use.
5. **Dataset-filename leak surface (open — discuss with Jin).** Red line:
   raw dataset *contents* never reach an external API; sanitized public-DB /
   literature queries are OK. The wiring meets this — Biomni's `A1.go` gets only
   the sanitized question, Kosmos's brief gets only the question + the bare
   dataset filename (never rows), `DataBoundaryGuard` blocks raw tables/secrets,
   and the LLM is local Qwen3.6. **Residual:** with
   `BIOAGENT_KOSMOS_ENABLE_LITERATURE=true`, a dataset *filename* that embeds an
   identifier (e.g. `patient_12345_biopsy.csv`) could ride into an outbound
   literature query. Left as-is for now (low impact). Fix when wanted: have
   `build_research_loop_prompt` emit `manifest: <size>B sha256:<12>` instead of
   `dataset_path.name`. Note also that `KosmosSafetyPolicy.allow_external_network`
   gates only the *plan*; the real literature on/off is the runtime env
   `BIOAGENT_KOSMOS_ENABLE_LITERATURE`, so enforce egress posture there (or at the
   OS/firewall level) — not via the policy flag alone.
