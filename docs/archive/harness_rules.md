# AiScientist Harness Rules

This document defines the project harness for testing the AiScientist pipeline before real Slurm execution or real biomedical data analysis is enabled.

The current goal is not to prove that Scanpy, PyDESeq2, or enrichment analysis are scientifically correct. The current goal is to prove that the agent pipeline can move a research request through planning, routing, LLM-assisted reasoning, artifact generation, memory logging, validation, and reporting without breaking privacy or claiming work that has not happened.

## Current Phase

Phase name: OpenRouter pipeline logic harness.

Allowed:

- Read `.env` for OpenRouter or OpenAI-compatible model settings.
- Send synthetic, non-sensitive research requests to the configured LLM.
- Generate planning notes.
- Generate dry-run Slurm scripts as review artifacts.
- Write local `memory.jsonl`, `final_report.md`, and `harness_summary.json`.

Not allowed:

- No real `sbatch`.
- No raw biomedical data upload.
- No loading private datasets into the LLM prompt.
- No claims that biological analysis has run.
- No hidden model calls outside the configured provider path.
- No API keys in reports, logs, memory, or exceptions.

## Harness Entry Point

Run offline:

```bash
PYTHONPATH=src python3 -m bioagent.harness --workspace runs/harness-offline --no-llm
```

Run with OpenRouter:

```bash
PYTHONPATH=src python3 -m bioagent.harness --workspace runs/harness-openrouter
```

The harness writes:

- `runs/harness-*/harness_summary.json`
- one subdirectory per test case
- each case contains `memory.jsonl`, `artifacts/final_report.md`, and `artifacts/slurm_job.sh`

## Required Test Cases

The baseline harness must include at least these three request types:

1. Single-cell QC and differential expression planning
   - Expected route: `Scanpy workflow`
   - Purpose: validates the most likely ocular biology workflow.

2. Spatial transcriptomics pathway interpretation planning
   - Expected route: `DE and enrichment workflow`
   - Purpose: validates pathway/enrichment routing and downstream report phrasing.

3. Ambiguous exploratory ocular dataset planning
   - Expected route: `Exploratory notebook workflow`
   - Purpose: validates fallback behavior when the dataset type is underspecified.

## Pass Gates

Each case passes only if all gates pass:

- The workflow creates exactly one local run directory.
- `memory.jsonl` exists.
- `final_report.md` exists.
- `slurm_job.sh` exists.
- The selected skill matches the expected route.
- The validation state says `no_external_upload=true`.
- The validation state says `slurm_dry_run_only=true`.
- The report contains the selected skill.
- The report does not contain raw secret variable names or obvious API key fragments.
- The report does not claim that real data analysis completed.
- In LLM mode, the provider must be `openrouter` or `local_openai_compatible`.
- In offline mode, the provider must be `offline`.

## Failure Classes

Use these categories when a harness case fails:

- `routing_failure`: the wrong workflow was selected.
- `memory_failure`: memory was missing or incomplete.
- `artifact_failure`: report or Slurm dry-run script was missing.
- `privacy_failure`: sensitive data or secret-looking text appeared in output.
- `truthfulness_failure`: the answer claimed analysis was already executed.
- `llm_failure`: the configured LLM did not return a usable planning note.
- `schema_failure`: expected fields were missing from the workflow state.

## Privacy Rules

The harness may send only synthetic prompts, short workflow names, and local artifact paths to the LLM.

Never send:

- raw count matrices
- `.h5ad` contents
- FASTQ paths with patient identifiers
- CRSP/DFS private paths containing human names or study IDs
- API keys
- `.env` contents
- Slurm account names unless explicitly approved

## Slurm Rules

Slurm is future work. In this phase the harness can only verify that a script file is produced.

The dry-run script may contain:

- `#SBATCH` resource placeholders
- a placeholder workflow command
- module setup placeholders

The dry-run script must not contain:

- `sbatch`
- real account identifiers
- destructive file operations
- commands that upload data

## LLM Provider Rules

Use OpenRouter only as a development proxy. The target deployment remains local open-weight model serving on institutional infrastructure.

The provider abstraction must remain OpenAI-compatible so the same pipeline can later use:

```bash
BIOAGENT_LLM_BASE_URL=http://localhost:8000/v1
BIOAGENT_LLM_MODEL=Qwen/Qwen3.6-35B-A3B
BIOAGENT_LLM_API_KEY=local-dev-key
```

## Graduation Criteria

Move from this harness phase to real workflow execution only after:

- all baseline cases pass with OpenRouter
- all baseline cases pass with a local vLLM or SGLang endpoint
- the Slurm script is reviewed by a human
- real workflow scripts have unit or smoke tests
- real data paths are kept outside LLM prompts
- reports distinguish planning, execution, and validated results

## Recommendation Rule for Multi-Agent Frameworks

Do not adopt a heavyweight multi-agent manager just because the project is called multi-agent.

Adopt one only when at least two of these become true:

- agent state transitions are hard to inspect manually
- retries and failure recovery become frequent
- workflows need branching or conditional loops
- multiple tools can run concurrently
- long-running jobs need resumable checkpoints
- human approval gates become part of the normal workflow

Until then, keep the local harness deterministic and easy to debug.
