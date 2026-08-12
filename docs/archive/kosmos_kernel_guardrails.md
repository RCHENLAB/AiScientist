# Kosmos Kernel Guardrails

AiScientist now treats Kosmos as the preferred autonomous research-loop kernel, but it keeps a local safety harness in front of Kosmos.

## Current Boundary

`KosmosKernelAdapter` is the boundary between AiScientist and Kosmos-style capabilities.

It records:

- which Kosmos-style tools are available
- whether a tool needs network access
- whether a tool needs an LLM
- whether generated code must run in a sandbox
- whether the tool can read local data
- whether the tool can submit Slurm jobs

The adapter does not import Kosmos runtime dependencies. Kosmos remains a separate environment until the bridge is ready for production.

## Default Offline Policy

Default policy: `KosmosSafetyPolicy(mode="offline_smoke")`

- external network: blocked
- remote LLM: blocked for sensitive data
- raw data in prompts: blocked
- real Slurm submit: blocked
- sandbox for generated code: required

Allowed in default smoke mode:

- `kosmos_research_loop` as a planned local-kernel capability
- `kosmos_docker_code_execution` only with sandbox constraint
- `kosmos_knowledge_graph` as local provenance storage
- `lab_scanpy_qc`
- `lab_marker_screen`

Blocked in default smoke mode:

- `kosmos_literature_search`
- `slurm_submit`

## Data Boundary

Prompts may include:

- research question
- dataset manifest
- file size and short hash
- QC metrics
- marker/effect summaries
- limitations and claim status

Prompts must not include:

- raw expression matrix rows
- patient/sample identifiers
- API keys
- unreviewed `sbatch` commands

## Literature And Generated Code Tools

`LiteratureGroundingAgent` may use public PubMed / NCBI E-utilities network access when research mode enables it. The query must be sanitized and must not include dataset paths, raw rows, sample identifiers, or private metadata. Literature results are public citation records only.

`GeneratedCodeExecutionAgent` may run generated summary code only against derived JSON artifacts such as dataset manifests, QC summaries, DE/marker summaries, and literature grounding results. Generated code must not read raw `.h5ad`, `.csv`, or `.tsv` datasets and must not use network APIs.

## Smoke Command

```bash
PYTHONPATH=src python3 -m bioagent.kosmos_smoke --workspace runs/kosmos-kernel-smoke
```

The smoke report writes:

- `kosmos_kernel_smoke.json`
- `kosmos_kernel_smoke.md`

## Production Direction

When moving from smoke mode to a real UCI cluster setup:

1. Run LLM through local Qwen/vLLM/LiteLLM, not OpenRouter, for private data.
2. Use Kosmos literature search only with sanitized public queries.
3. Mount datasets read-only into Docker/Singularity/Apptainer execution.
4. Require human review before any Slurm submit.
5. Keep AiScientist's evaluation layer independent from Kosmos output generation.
6. Keep high-compute jobs on UCI HPC3/RCIC. If the GPU queue is long, notify the researcher, checkpoint, and offer safe waiting or reviewed smaller-job options instead of silently running heavy analysis on the lab server.
