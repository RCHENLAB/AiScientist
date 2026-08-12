# docs/ index — what each document is, and whether it's current

A map so you don't have to guess which doc is live. Three buckets: **Current** (authoritative, being
worked on), **Reference** (a shipped/decided feature — consult when touching it), **Superseded** (decision
changed or early framing — kept for history, safe to archive). When you add a doc, add a row here.

## Current — authoritative, active work
| Doc | What it is |
|---|---|
| [ird_pipeline_parity_roadmap.md](ird_pipeline_parity_roadmap.md) | The IRD parity plan (11 layers → phases) + gap status. The live IRD tracker. |
| [ird_filter_spec.md](ird_filter_spec.md) | The lab's annotate/filter/prioritize logic, extracted as our port spec. |
| [vcf_pipeline_tools.md](vcf_pipeline_tools.md) | VCF pipeline tool + data inventory (VEP/ClinVar/predictors/SpliceAI, versions, env vars). |
| [phenotype_gene_confidence_rag_spec.md](phenotype_gene_confidence_rag_spec.md) | Phenotype→disease differential-diagnosis design (LIRICAL primary + PaperQA2 evidence track). |
| [case_a_why_a_wrong_answer_ranked_first.md](case_a_why_a_wrong_answer_ranked_first.md) | Why a WRONG disease outranked the true IMPG2 answer on a real solved case — and the experiment that fixed it (5 negations: LR +7.447 → −2.368). The strongest evidence for the free-text→HPO line. |
| [postmortem_demo_case_a_stargardt_vs_cord.md](postmortem_demo_case_a_stargardt_vs_cord.md) | Postmortem: I predicted Stargardt, LIRICAL said cone-rod dystrophy, LIRICAL was right. Also: the posterior is not a confidence. |
| [free_text_to_hpo_mapping.md](free_text_to_hpo_mapping.md) | Clinician free text → validated HPO IDs (`map_phenotype_to_hpo`): the closed-set design, the bundled HPO lexicon, and what's unverified. |
| [paperqa2_evidence_layer_contract.md](paperqa2_evidence_layer_contract.md) | Interface handoff for the classmate's PaperQA2 evidence track (send-ready). |
| [skills_and_pipelines_architecture.md](skills_and_pipelines_architecture.md) | The current registry / atomic-skills / preset-pipelines architecture. |
| [BACKLOG.md](BACKLOG.md) | The running backlog. |

## Reference — shipped / decided; consult when you touch that area
| Doc | What it is |
|---|---|
| [self_registration.md](self_registration.md) | SMTP self-registration + email verification (SHIPPED). |
| [ssh_key_login.md](ssh_key_login.md) | SSH-key login setup (skip password + Duo). |
| [analysis_slurm_offload.md](analysis_slurm_offload.md) | Scanpy analysis line as HPC3 Slurm jobs (SHIPPED — `SlurmAnalysisExecutor`). |
| [hpc3_offload_migration.md](hpc3_offload_migration.md) | Plan: move data+compute to HPC3 (largely shipped). |
| [scgpt_workflow_integration.md](scgpt_workflow_integration.md) | scGPT GPU-job design + gap analysis (SHIPPED). |
| [pi_critic_meeting_protocol.md](pi_critic_meeting_protocol.md) | PI↔Critic step-meeting protocol design (flag-gated). |
| [dag_planner_design.md](dag_planner_design.md) | DAG planner + multi-agent design (0.2.0 line, flag-gated). |
| [agent_memory_design.md](agent_memory_design.md) | Per-agent isolated+evolving memory design (future). |
| [repository_boundaries.md](repository_boundaries.md) | What lives in this repo vs. elsewhere. |
| [dependency_management.md](dependency_management.md) | Dependency / venv conventions. |
| [adr-0001-conda-vs-venv.md](adr-0001-conda-vs-venv.md) | ADR: stay on venv+pip, not conda. (ADRs are kept permanently as decision records.) |

## Superseded / historical — moved to [archive/](archive/) (early-decision record)
| Doc | Why superseded |
|---|---|
| [biomni_kosmos_integration.md](archive/biomni_kosmos_integration.md) | **Biomni retired** — paper-qa/PaperQA2 replaced it for the literature role. |
| [hpc3_console.md](archive/hpc3_console.md) | Built around **Ollama**; the LLM stack moved to **vLLM** (`gpu.py` serve job). |
| [literature_embedding_plan.md](archive/literature_embedding_plan.md) | Decided "no local embedding / two-tier references" — **now contradicted** by the PaperQA2 (local-embedding) direction. Re-check before trusting. |
| [reference_architecture.md](archive/reference_architecture.md) | Early "Kosmos + Biomni + BioAgent" framing; Biomni gone, Kosmos-parity very-low-priority. |
| [kosmos_kernel_guardrails.md](archive/kosmos_kernel_guardrails.md) | Kosmos-parity deprioritized (see the kosmos-parity roadmap memory). |
| [phase2_hpc_compute.md](archive/phase2_hpc_compute.md) | Early Slurm plan; superseded by `analysis_slurm_offload.md` + the shipped executors. |
| [project_plan.md](archive/project_plan.md) | Early project plan; superseded by BACKLOG + the roadmaps. |
| [agent_dashboard.md](archive/agent_dashboard.md) | Early dashboard concept. |
| [harness_rules.md](archive/harness_rules.md) | Early harness rules; superseded by the actual harness/registry. |
| [answer_framework.md](archive/answer_framework.md) | Tiny early framing note. |
| [minimal_framework.md](archive/minimal_framework.md) | Tiny early framing note. |
| [frontend_ux_fixes.md](archive/frontend_ux_fixes.md) | A one-off frontend fix batch (branch), merged. |
| [diary/2026-06-09.md](diary/2026-06-09.md) | Dated work diary — historical by nature. |

> **Archived 2026-07-14.** These docs were moved to [`archive/`](archive/) and every referrer (READMEs,
> handoffs, `agent_registry.yaml`, `scripts/pr_review_gate.py`, code comments) was updated to the new path
> in the same change — nothing dangles. They are kept as a record of early decisions, not maintained. The
> work diary stays under [`diary/`](diary/) (it is a dated log, not a superseded decision).
