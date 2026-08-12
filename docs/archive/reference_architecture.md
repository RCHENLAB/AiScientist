# Reference Architecture: Kosmos, Biomni, and AiScientist

This note records the external systems currently used as architecture references
for AiScientist and clarifies where a future Biomni integration should sit.

## Reference Systems

### Kosmos

Reference:

- Repository: https://github.com/jimmc414/Kosmos

Kosmos is the reference point for a broader autonomous scientist loop. Its
published repository describes capabilities for hypothesis generation,
experiment design, literature search, sandboxed code execution, knowledge graph
tracking, validation, budget control, and multi-cycle research workflows.

AiScientist should not import Kosmos as a default dependency. The current project
keeps Kosmos behind `KosmosAdapter` and `KosmosKernelAdapter`, where AiScientist
can model tool permissions, data boundaries, and future runtime handoff without
collapsing local lab safety checks into the external research-loop runtime.

Role in AiScientist:

- Reference for autonomous research-loop planning.
- Future optional runtime behind a guarded adapter.
- Baseline for parity and gap evaluation.
- Not the owner of local dataset privacy, Slurm safety, or ocular workflow
  validation.

### Biomni

Reference:

- Repository: https://github.com/snap-stanford/Biomni
- Documentation: https://docs.biomni.phylo.bio/

Biomni is the reference point for a biomedical tool-execution layer. Its
documentation describes an agent that combines LLM reasoning, biomedical
database access, tool retrieval, code execution, and domain-specific workflows
across genomics, variants, proteins, pharmacology, pathways, single-cell
analysis, and other biomedical tasks.

Biomni can overlap with several AiScientist roles, especially computational biology
and machine-learning specialist behavior. In this project it should be treated
as a backend capability provider, not as the top-level orchestrator. AiScientist
should call it only through a privacy-aware adapter that controls model provider,
database access, local file exposure, and output validation.

Role in AiScientist:

- Future biomedical execution backend for selected domain tasks.
- Tool provider for computational biology, ML/statistical analysis, literature,
  database lookup, pathway analysis, variant/gene interpretation, and selected
  omics workflows.
- Optional local-first runtime when configured with a local OpenAI-compatible
  model endpoint and restricted external API access.
- Not the owner of PI-style planning, team-meeting orchestration, final claim
  validation, or human approval.

## Combined Product Boundary

AiScientist remains the lab-facing system. It should own intake, privacy policy,
dataset preflight, workflow routing, validation, reports, checkpoints, and
human-review gates. Kosmos and Biomni are reusable capability references behind
that boundary.

```text
Researcher / Lab UI
  -> AiScientist Intake and Orchestrator
      -> CoordinatorAgent
          -> DataAgent
              -> KosmosAdapter for research-loop planning references
              -> BiomniAdapter for biomedical execution capabilities
          -> LocalAnalysisAgent for deterministic local preflight
          -> LiteratureGroundingAgent for sanitized public citation lookup
          -> HPCAgent for Slurm dry-run and future reviewed submission
          -> ValidationAgent for privacy, reproducibility, and claim checks
          -> ResearchEvaluationAgent
          -> ReporterAgent
```

## Slurm UI and Gateway Boundary

The lab UI can remove most day-to-day SSH friction for researchers, but it
should not become a browser-side SSH terminal. UCI HPC3/RCIC interactive access
still depends on UCInetID, Duo, and SSH or key-based login requirements. The
AiScientist product boundary should therefore be:

- frontend: provide the researcher-facing multi-turn LLM chat workspace, session
  history, visible run state, report/artifact downloads, and approval prompts
  only when a tool needs UCI HPC3 authorization.
- AiScientist backend: validate data boundaries, render the Slurm script, record
  approval state, enforce `docs/slurm_command_contract.schema.json`, and expose
  a narrow Slurm action API. LLM command objects and Slurm details remain
  backend/internal unless a concrete approval prompt or final report needs them.
- Slurm gateway: execute `sbatch`, `squeue`, and `scancel` on HPC3 with audited
  per-user credentials; never expose private keys to the browser.
- ValidationAgent: block unreviewed submit/cancel actions, raw-data upload, and
  heavy lab-server fallback unless explicitly approved for diagnostic scope.

This keeps the researcher experience graphical while preserving the compliance
and accountability normally enforced by SSH, Duo, and Slurm account charging.

## Proposed Biomni Adapter Boundary

The first integration should add a `BiomniAdapter` rather than installing Biomni
directly into AiScientist's base runtime.

Responsibilities:

- Select Biomni-compatible capabilities from a small allowlist.
- Enforce local-first model configuration for sensitive tasks.
- Block raw dataset rows, sample identifiers, API keys, and unreviewed private
  metadata from prompts.
- Distinguish public database queries from private lab-data analysis.
- Capture tool provenance, input summaries, output files, limitations, and
  validation status.
- Return structured artifacts that AiScientist can evaluate independently.

Current statuses:

- `biomni_adapter`: partial
- `biomni_local_model_policy`: partial guardrail
- `biomni_public_database_query`: planned, sanitized only
- `biomni_private_data_execution`: planned, local-only and reviewed

## Figma Mapping

In the merged Figma architecture, Biomni should not appear as another complete
agent team parallel to AiScientist. It should appear as an execution backend behind
the specialist agents.

Component mapping:

| Figma component | Merged role |
| --- | --- |
| Principal Investigator / Coordinator | AiScientist `CoordinatorAgent`; owns agenda, routing, and final handoff. |
| Computational Biologist | AiScientist specialist that can call `BiomniAdapter`. |
| Machine Learning Specialist | AiScientist specialist that can call local tools or selected Biomni ML/statistical tools. |
| Immunologist / Domain Specialist | AiScientist specialist that can request Biomni database/pathway support. |
| Scientific Critic | AiScientist `ValidationAgent` and `ResearchEvaluationAgent`; reviews Biomni and Kosmos outputs. |
| Team Meeting | AiScientist/Kosmos-style research-loop orchestration. |
| Individual Meeting | Focused specialist execution and critique loop. |
| Biomni | Biomedical execution backend, not the owner of orchestration. |

## Privacy Position

Default Biomni usage with cloud LLM providers or external biomedical APIs is not
appropriate for private lab data. A AiScientist integration should support two
clear modes:

- `public_query_mode`: allowed to call public databases with sanitized,
  non-sensitive queries.
- `private_data_mode`: local model endpoint, local files, no external API calls,
  no public sharing links, and validation before final reporting.

The final answer shown to a researcher should always identify whether a result
came from local deterministic tools, Biomni-backed execution, Kosmos-style
planning, HPC/Slurm, or literature/database lookup.
