# AiScientist Agent Dashboard

Updated: 2026-06-08

This dashboard is the human-readable companion to `docs/agent_registry.yaml`.
Use Multica for collaboration tasks and this dashboard for the actual AiScientist
agent/component state.

## Status Legend

| Color | Status | Meaning |
| --- | --- | --- |
| Green | `implemented` | Implemented with local tests or sample artifacts. |
| Yellow | `partial` | Adapter, policy, or interface exists, but the real runtime is not connected. |
| Blue | `guardrail` | Safety boundary or permission-control component. |
| Gray | `planned` | Planned but not implemented yet. |

## Current Summary

| Status | Count | Components |
| --- | ---: | --- |
| `implemented` | 12 | Coordinator, Data, Local Analysis, QC, DE/marker screen, Literature Grounding, Generated Code, HPC dry-run, Research Evaluation, Reporter, Autonomous Loop, Parity Evaluation |
| `partial` | 3 | Kosmos adapter/kernel, Kosmos bridge, Biomni execution adapter |
| `guardrail` | 1 | Validation agent |
| `planned` | 3 | Local Qwen runtime, real Slurm submit/queue monitor, frontend dashboard |

## Agent Inventory

| Component | Status | Role | Evidence | Next Step |
| --- | --- | --- | --- | --- |
| `CoordinatorAgent` | `implemented` | Creates staged local-first workflow plans. | `tests/test_workflow.py` | Surface plan steps in frontend timeline. |
| `DataAgent` | `implemented` | Selects reusable tools and builds Kosmos-style kernel/data-boundary plan. | `runs/kosmos-kernel-smoke/kosmos_kernel_smoke.md` | Split tool selection from runtime execution. |
| `LocalAnalysisAgent` | `implemented` | Runs local dataset smoke/preflight analysis. | `runs/literature-tool-check-2/artifacts/qc_preflight_summary.md` | Add production Scanpy loaders and QC plots. |
| `SingleCellQCExecutionAgent` | `implemented` | Builds deterministic single-cell QC artifacts. | `runs/literature-tool-check-2/artifacts/single_cell_qc_execution_summary.md` | Add production thresholds and Scanpy-backed QC. |
| `DifferentialExpressionExecutionAgent` | `implemented` | Runs marker/effect screening or blocks unsupported DE claims. | `runs/literature-tool-check-2/artifacts/de_marker_execution_summary.md` | Add full DE statistics and pathway hooks. |
| `LiteratureGroundingAgent` | `implemented` | Performs sanitized PubMed literature grounding without raw data leakage. | `runs/literature-tool-check-2/artifacts/literature_grounding.md` | Add multi-database retrieval and relevance scoring. |
| `GeneratedCodeExecutionAgent` | `implemented` | Runs generated summary code against derived artifacts only. | `runs/literature-tool-check-2/artifacts/generated_code_execution.md` | Move to container sandbox before arbitrary snippets. |
| `HPCAgent` | `implemented` | Writes Slurm dry-run script and queue/fallback policy. | `runs/hpc-fallback-check/artifacts/hpc_queue_fallback_plan.md` | Add reviewed `sbatch` / `squeue` integration. |
| `ValidationAgent` | `guardrail` | Validates data, network, Slurm, and generated-code boundaries. | `tests/test_workflow.py` | Emit structured frontend risk flags. |
| `ResearchEvaluationAgent` | `implemented` | Judges evidence strength, claim safety, and next research step. | `runs/autonomous-research-pbmc3k-tools/evaluator_report.md` | Version evaluator rubrics and reviewer overrides. |
| `ReporterAgent` | `implemented` | Renders human-readable final reports and artifact links. | `runs/literature-tool-check-2/artifacts/final_report.md` | Emit frontend-friendly section metadata. |
| `CheckpointedAutonomousLoop` | `implemented` | Runs staged autonomous research with checkpoints and token tracking. | `runs/autonomous-research-pbmc3k-tools/autonomous_research_report.md` | Add resume controls and delivery status cards. |
| `KosmosAdapter / KosmosKernelAdapter` | `partial` | Models Kosmos-style tools, permission policy, and data boundaries. | `docs/archive/kosmos_kernel_guardrails.md` | Add optional `KosmosRuntimeClient` / `KosmosCliRuntime`. |
| `KosmosBridgeAgent` | `partial` | Builds Kosmos-compatible research briefs for parity comparison. | `runs/parity-integrated-bridge/parity_report.md` | Connect to reviewed runtime backend. |
| `BiomniExecutionAgent / BiomniAdapter` | `partial` | Models Biomni-style backend capabilities and local-first privacy decisions before live runtime calls. | `tests/test_workflow.py` | Add reviewed local Biomni runtime client. |
| `ParityEvaluationAgent` | `implemented` | Scores AiScientist and Kosmos reference outputs by parity dimensions. | `runs/parity-integrated-bridge/parity_report.md` | Track parity trends across PRs and benchmark runs. |
| `Local Qwen / vLLM Runtime` | `planned` | Route private LLM calls to lab-server local endpoint. | `README.md` | Validate with real lab-server vLLM/SGLang. |
| `Real Slurm Submit / Queue Monitor` | `planned` | Submit reviewed jobs to UCI HPC and monitor queue status. | `runs/hpc-fallback-check/artifacts/hpc_queue_fallback_plan.md` | Add human-review-gated `sbatch` and `squeue`. |
| `Frontend Dashboard` | `planned` | Visualize agent state, artifacts, queue/fallback, checkpoints, and risk gates. | `docs/agent_architecture.mmd` | Start static dashboard from registry and sample run artifacts. |

## Frontend-Ready Views

The first frontend should not try to run every workflow. It should visualize
the current development and runtime state:

- Agent registry table with status color and owner.
- Run artifact list from `artifact_index.json` and final reports.
- Checkpoint timeline for autonomous loop runs.
- HPC execution state: `hpc_slurm`, `queued`, `local_fallback`, or `dry_run`.
- Risk gate cards: raw data to LLM, literature network, generated-code network, Slurm submit.
- Parity/gap summary against Kosmos-style workflow.

Implemented local prototype: `frontend/index.html`.

The researcher-facing frontend should be chat-first: multi-turn LLM conversation
is the primary surface, while sessions, run state, approval prompts, artifacts,
and report downloads stay adjacent to the chat. It should not expose the
internal agent registry, Slurm command JSON, or tool orchestration controls
unless debug/admin mode is explicitly added later.

## Multica Mapping

Use Multica for development management, not as the only source of architecture truth.
Recommended task labels:

- `agent:qc`
- `agent:de`
- `agent:literature`
- `agent:evaluation`
- `agent:kosmos`
- `agent:biomni`
- `agent:slurm`
- `agent:frontend`
- `safety:data-boundary`
- `runtime:local-qwen`
- `runtime:hpc`

Each Multica task should point back to one `id` in `docs/agent_registry.yaml`.

## Next Recommended Build Step

Start the frontend as a read-only local dashboard:

1. Read `docs/agent_registry.yaml`.
2. Read one selected run folder, such as `runs/autonomous-research-pbmc3k-tools/`.
3. Show agent status, artifact status, checkpoints, and risk gates.
4. Do not submit Slurm, call OpenRouter, or launch Kosmos from the frontend in v1.
