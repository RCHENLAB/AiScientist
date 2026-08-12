# docs/archive/ — superseded docs (early-decision record)

These documents are **no longer current**. They are kept as a record of the project's early decisions and
direction — useful for "why did we do X back then?", not for how the system works now. Nothing here is
maintained; do not treat any of it as authoritative.

**For current docs, see [`../README.md`](../README.md).**

| Doc | What it was | Superseded by / why |
|---|---|---|
| `biomni_kosmos_integration.md` | Biomni real-integration plan | Biomni **retired** — paper-qa/PaperQA2 took the literature role |
| `reference_architecture.md` | Kosmos + Biomni + BioAgent framing | Biomni gone; Kosmos-parity very-low-priority |
| `kosmos_kernel_guardrails.md` | Kosmos-style guardrails | Kosmos-parity deprioritized |
| `hpc3_console.md` | HPC3 SSH + **Ollama** console | LLM stack moved to **vLLM** (`gateway/gpu.py` serve job) |
| `literature_embedding_plan.md` | "no local embedding / two-tier references" decision | contradicted by the current **PaperQA2** (local-embedding) direction; see `docs/paperqa2_evidence_layer_contract.md` |
| `phase2_hpc_compute.md` | Early in-place Slurm compute plan | superseded by `docs/analysis_slurm_offload.md` + the shipped `SlurmAnalysisExecutor` |
| `project_plan.md` | Early project plan | superseded by `docs/BACKLOG.md` + the roadmaps |
| `agent_dashboard.md` | Early collaborator status dashboard | early concept, not maintained |
| `harness_rules.md` | Early harness rules | superseded by the actual harness/registry |
| `answer_framework.md` | Early framing note | early scaffolding thinking |
| `minimal_framework.md` | Early framing note | early scaffolding thinking |
| `frontend_ux_fixes.md` | One-off frontend fix batch (a branch) | merged; tracker no longer needed |

_Moved here 2026-07-14 from `docs/`; referrers were updated to `docs/archive/…` in the same change._
