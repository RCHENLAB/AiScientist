# Repository Boundaries

This document is the practical engineering rulebook for this repository.

## Golden Rule

Keep one repository, but treat each top-level package under `src/bioagent/` as if it may become a separate repository later.

That means every package should expose clear interfaces and avoid reaching into unrelated implementation details.

## Allowed Dependency Direction

Preferred import direction:

```text
workflows -> agents -> integrations/tools/hpc/providers/memory/core
eval -> workflows/core
__main__ -> workflows/core
```

Shared foundational modules:

```text
core
```

Leaf modules:

```text
tools
hpc
providers
memory
integrations
```

Avoid reverse imports:

- `core` must not import `agents`
- `tools` must not import `agents`
- `providers` must not import `agents`
- `hpc` must not import `agents`
- `memory` must not import `agents`

## Stable Interfaces

These interfaces should change slowly:

- `WorkflowState`
- `AgentMessage`
- `Artifact`
- `VisionResearchAgent.run(...)`
- `WorkflowMemory.record_event(...)`
- `OpenRouterClient.chat(...)`
- `SlurmAdapter.render_script(...)`
- `run_dataset_smoke_analysis(...)`
- `KosmosKernelAdapter.build_kernel_plan(...)`
- `KosmosSafetyPolicy`

Changing any of these should include a test update and a note in `docs/archive/project_plan.md`.

## Ownership Boundaries

### Agent Layer

Agent classes decide what should happen next. They should not implement heavy tool logic.

Good:

```text
LocalAnalysisAgent calls run_dataset_smoke_analysis(...)
```

Bad:

```text
LocalAnalysisAgent manually parses every dataset format itself
```

### Tool Layer

Tool modules do local deterministic work. They should not call LLMs.

Good:

```text
tools/datasets.py reads CSV and writes JSON/Markdown summaries
```

Bad:

```text
tools/datasets.py calls OpenRouter to explain results
```

Execution tools should stay in tool-pack modules rather than inside one large agent class.

Good:

```text
SingleCellQCExecutionAgent calls tools/execution.py for QC artifacts
DifferentialExpressionExecutionAgent calls tools/execution.py for marker-screen artifacts
```

Bad:

```text
One execution agent directly implements Scanpy, GSEA, plotting, Slurm, and result critique
```

### Provider Layer

Provider modules normalize LLM calls. They should not know about ocular biology workflows.

Good:

```text
providers/openai_compatible.py sends messages and returns content
```

Bad:

```text
providers/openai_compatible.py decides whether to run Scanpy
```

### Integration Layer

Integration modules define boundaries to external frameworks such as Kosmos and Edison. They should not make unsafe external calls by default.

Good:

```text
KosmosKernelAdapter records tool permissions and data-boundary decisions
```

Bad:

```text
KosmosKernelAdapter silently uploads local data or submits Slurm jobs
```

### HPC Layer

HPC modules render and later submit cluster jobs. In the current phase they must remain dry-run.

Good:

```text
SlurmAdapter.write_script(...)
```

Not allowed yet:

```text
subprocess.run(["sbatch", ...])
```

### Memory Layer

Memory modules persist workflow provenance. They should never store secrets or raw private data.

Good:

```text
record selected workflow, artifact paths, validation status
```

Bad:

```text
record .env contents or raw patient-level matrices
```

## Generated Files

Generated outputs belong under `runs/`.

`runs/` is ignored by git. If an output should become a permanent example, copy a cleaned and small version into `examples/`.

## Config Files

Use:

- `configs/*.example.*` for committed templates
- `.env` for local secrets

Never commit:

- real OpenRouter keys
- Slurm account secrets
- private dataset paths
- generated cluster logs with private identifiers

## When to Add a New Directory

Add a new package only when it has a distinct reason to exist.

Good future additions:

- `schemas/` if Pydantic or JSON schemas grow large
- `ui/` if a backend/frontend prototype lands in this repo
- `reports/` if report rendering becomes complex
- `workflows/scrna.py` when real Scanpy execution starts

Avoid:

- `utils/` as a dumping ground
- one directory per tiny function
- one repository per early agent

## When to Split Repositories

Split only when one of these is true:

- different deployment target
- different ownership group
- stable public API
- separate release cycle
- separate security boundary

Until then, keep the monorepo.
