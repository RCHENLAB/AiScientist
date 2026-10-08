# Findings — what has been measured and decided

This file is the public summary of what building AiScientist has measured and decided: what each
analysis line does today and whether it is on, the validation numbers with their scope, the design
decisions those numbers drove, and the problems still open. The dated process reports, design notes
and experiment records it draws on are archived outside this repository; numbers are copied from them
unchanged, and a date marks any status that may have moved since.

What the system is and how it is built: [README.md](../README.md) and
[architecture/README.md](architecture/README.md). Deployment: [deploy/README.md](../deploy/README.md).
The protocols and code templates the agents load: [preset_pipelines/](../preset_pipelines/) and
[skills/](../skills/).

Last updated 2026-10-01.

## Capabilities and their status

"Gated" means built and tested, switched on by an environment variable (default off).

| Line | What it does | Status |
|---|---|---|
| scRNA-seq | `inspect_dataset` profiles the data deterministically (cells and depth per arm, replication, cell vs nucleus) → scanpy QC → Scrublet doublets → batch integration → Leiden clustering (optional bootstrap-ARI sweep that keeps the finest resolution that still reproduces) → marker annotation → Wilcoxon DE (contrast, stratified by cell type) → pseudobulk DE (pydeseq2) → depth-matched DE → composition → offline ORA and preranked GSEA | On; runs as Slurm jobs in the analysis container on HPC3 |
| scGPT annotation | Per-cell label + confidence against a human-retina reference (123 cell types), as a short-lived GPU batch job; mouse queries are case-folded onto human gene symbols | Optional (2026-09-05) |
| Variant annotation | Offline Ensembl VEP with a local cache (GRCh37 and GRCh38), ClinVar, gnomAD, SIFT/PolyPhen; assembly read from the VCF header. CADD and REVEL for both builds, AlphaMissense for GRCh38 only (left blank on GRCh37, never filled with GRCh38 scores), OpenSpliceAI | Offline line gated (`AISCIENTIST_VARIANT_ON_HPC`); without it, Ensembl REST capped at 500 variants. Predictor plugins (`AISCIENTIST_VEP_PLUGINS`) and SpliceAI (`AISCIENTIST_SPLICEAI`) each gated |
| Inherited-retinal-disease (IRD) prioritisation | Known-gene panel (258 RetNet genes) with region restriction before VEP, a default rarity floor, disease-model tiering (dominant ≤1e-4; recessive ≥2 variants ≤5e-3; X-linked), retina-specific annotation layers | Panel, regions and floor set by `AISCIENTIST_DEFAULT_*`; annotation layers gated (`AISCIENTIST_IRD_ANNOTATE`) (July 2026 roadmap) |
| Phenotype → disease | `map_phenotype_to_hpo` (free text in any language → validated HPO IDs, negations kept, family history dropped) → `run_lirical` (LIRICAL 2.4.1 + Exomiser 2406_hg19; genotype-aware when a VCF is bound) → `diagnose_disease` (adds a literature evidence tier) | On in production (2026-09-05). A VCF with no case description cannot be scored: the mapper returns no terms and `run_lirical` errors |
| Literature | `literature_search`: Europe PMC, queries built at run time from the accepted findings, not from the user's question. `deep_literature`: PaperQA2 on HPC3 using the session's model and local PubMedBERT embeddings over a curated ophthalmic-genetics corpus (1,741 of 1,749 targeted full-text papers acquired as of 2026-07-15; the papers are not in this repository) | On. If the container or corpus is unreachable, `deep_literature` returns `dependency_missing` and the run continues without literature |
| Fast chat | Answer-first streamed path with only the tools marked `chat: true` (`literature_search`, `map_phenotype_to_hpo`, `deep_literature`); no plan, no Critic, no run directory | On; an explicit Research/Chat toggle, Research is the default |
| Reports | Deterministic pandoc PDF/DOCX, a separate technical report for diagnostics, a claim audit before writing, references from accepted citations, regenerate without re-running, optional vision-model review of the render | On; claim audit on (`AISCIENTIST_CLAIM_AUDIT=0` disables); vision review optional |
| `run_code` | CodeAct sandbox as an HPC3 Slurm job (64 GB / 8 CPU / 1 h). Imports are checked before the snippet runs; a missing package is installed, after one confirmation, into an immutable lab-shared cache | On. The near-complete shell (`run_shell`, `fetch_url`, `install_package` on a held CPU node) is gated (`AISCIENTIST_WORKER_NODE=1`); the read-only file tools need no node |
| Orchestration extras | Whole-agenda PI↔Critic review before any step; per-step meetings; DAG planner, expert claiming, safe concurrency; per-agent memory; hypothesis-driven exploration; skill induction | Plan review on (`AISCIENTIST_PLAN_REVIEW=0` disables). Per-step meetings, DAG, concurrency and exploration off by default. Agent memory on in production and skill induction optional (2026-09-05) |
| Model | Qwen3.8-27B INT4 served by vLLM on an HPC3 GPU at `AISCIENTIST_VLLM_REASONING_EFFORT=low`, or the user's own OpenAI-compatible endpoint and key (an HPC3 account is still required) | Production model as of 2026-09-25 |

## Validation results

Every row keeps its sample size; most are small and give direction, not rates.

### Phenotype → disease

Fixtures are synthetic patients built on real ClinVar-classified variants, so the right answer is known
in advance. HPO terms were extracted by the served model (Qwen3.6-35B-A3B), not curated by hand.

| What was tested | Result | N / scope | Main limitation |
|---|---|---|---|
| Does the phenotype drive the ranking? One VCF with two compelling recessive candidates (ABCA4 compound heterozygous; MKKS homozygous), two clinical notes | The note flipped the top gene. Note A → ABCA4, cone-rod dystrophy 3 (posttest 12.36%); note B → MKKS, Bardet-Biedl syndrome 6 (51.11%). Under note A, MKKS fell to rank 5 with compositeLR −6.577 | 1 VCF, 2 notes | Synthetic. The fixture's expected disease for note A was wrong (the note describes cone dysfunction); LIRICAL was right |
| Full chain on a solved case: free-text referral note → HPO → LIRICAL | Known answer at rank 1, compositeLR 12.647, identical on a 5-variant synthetic VCF and on the case's real WGS (4.9 M variants); on the WGS rank 2 trails by 9.4 orders of magnitude | 1 case; 4 observed + 12 excluded terms | Note constructed from public annotations; the phenotype was partly derived from the diagnosis label |
| What negations are worth (same VCF, same case) | 2 label-derived terms and no exclusions: correct disease rank 2 (compositeLR 7.289) behind an off-target syndrome (7.447). Adding five routine negations: correct disease rank 1 (7.474), off-target syndrome rank 18 (−2.368) | 1 case | Artificially starved input |
| Sensitivity to near-synonymous HPO terms | Same case, gene and VCF: posttest 94.78% vs 12.36%, underlying LR about 156,514 vs 1,216 (129×); gene rank unchanged | 1 case, two term sets | Both term sets came from LLMs; no clinician-curated baseline exists |
| Free text → HPO on the real model | 5/5, including a family-history trap and a note with no phenotype (2026-07-15, after one prompt fix) | 5 labelled notes | Small N |
| Diagnosis strings on a clinician's solved-case sheet | All 8 distinct strings map without an LLM (after adding curated aliases) | 8 strings | A disease label yields one coarse term, not the syndrome's features |
| Thinking on vs off for extraction | Both passed every labelled check; 3/5 term sets identical; thinking on returned empty content on the dense note even at a 16,000-token budget | 5 cases, 1 run each | Evidence, not a benchmark |
| LIRICAL posterior | Bayes from a uniform prior of 1/8621 reproduces LIRICAL's recorded percentages to four decimals | 2 recorded values | — |

### Model choice: same scaffolding, only the model swapped

Each arm ran the real production code paths and tools on one real single-nucleus dataset (15,307
cells), scored by deterministic checks plus two blind frontier-model judges. OpenRouter served Qwen3.6
as fp8 where production used AWQ 4-bit.

| What was tested | Result | N / scope | Main limitation |
|---|---|---|---|
| Planning (2026-08-19) | Every arm met the fundamentals (stratified DE, composition, enrichment after DE, replication caveat, no hallucinated tools). Acting on the depth imbalance flagged in the dataset profile separated them: Qwen3.6 thinking off 0%, Qwen3.5-122B 33%, Qwen3.6 thinking on 67%, the other eight arms 100%. Judge soundness: Sonnet 5 and DeepSeek-V4-Flash 9.50, MiniMax-M3 9.25, Qwen3.6 6.00 | 11 arms × 2–3 plans | One dataset, one question |
| Executing one fixed 7-step plan | Named tool called in 86–93% of steps for seven arms (DeepSeek-V4-Pro 67% over 6 trials; Laguna-S-2.1 57%); argument match 1.0 wherever a tool was called (0.9 for DeepSeek-V4-Flash at low effort). MiniMax-M2.7 made the fewest reconnaissance calls (2.50 per step) and most often finished with an answer (50%) | 9 arms × 6–14 trials | Tools ran locally with no Slurm queue |
| Writing the same accepted findings | Judged quality: Sonnet 5 8.50, DeepSeek-V4-Flash (low effort, 6× output budget) 8.00, MiniMax-M3 7.50, Qwen3.6 thinking on 5.50, Qwen3.6 4.25. Writing rules removed mechanical errors in every arm; what remained was judgement | 2 reports per arm and prompt | Small N |
| Qwen3.8-27B INT4 vs Qwen3.6-35B-A3B AWQ, both self-hosted (2026-09-25) | Planning (sound / specific / dataset fidelity): 3.6 8.81 / 9.00 / 9.31 at 72 s; 3.8 low effort 9.12 / 9.38 / 9.50 at 147 s; 3.8 medium 9.06 / 9.38 / 9.56 at 179 s. At default effort 3.8 could not finish a plan (about 22k thinking tokens, cut at a 24k cap). Writing: no quality gain; 3.8 writes longer, takes about twice as long and over-claimed more (75% vs 25%) | 8 plans per arm; writing n=2 | Writing sample is a caution, not a verdict |
| Does the model know what its report got wrong? (2026-09-28) | Both Qwens answered two questions about technical artefacts in their own report correctly in isolation (2/2) yet got them wrong in the full report. Two real gaps: whether total-count normalisation + log1p removes a 1.6× depth effect on a Wilcoxon test (3.8 0/2, 3.6 1/2, Sonnet 5 2/2); flagging two contradictory count tables (3.8 1/2, 3.6 0/2, Sonnet 5 2/2) | 2 samples per question | Small N |
| Claim audit before writing (Qwen3.8, low effort) | Overclaims / invents / quality: no audit 83% / 50% / 6.0; audit v1 17% / 83% / 7.0; audit v2 (the dataset's own counts and depth ratio stated as authoritative) 33% / 0% / 8.67 | 3 reports per arm, 2 judges | A later rule against reporting FDR-filtered GSEA term counts as findings has not been re-measured |

### Scaffolding mechanisms

| What was tested | Result | N / scope | Main limitation |
|---|---|---|---|
| Plan revision on the served Qwen3.6: whole-plan redraft vs a one-step patch applied by code | Redraft: 0% clean, other steps changed in 100% of trials (mean 3.89), plan length changed 89%, intent honoured 53%. Patch: 100% clean, no collateral change, intent 100%. Production after the fix: 100% clean | 4 requests × 5 reps (n = 19 / 19 / 20) | One model |
| `run_depth_matched_de` on synthetic data with a known answer: two cell types × 800 cells, 300 genes, one arm ~2× deeper (2026-09-02) | Real-biology type, up: ρ 0.84, preserved; 27/30 changed genes and 0/30 background kept. Depth-only type, up: ρ 0.15, weak (correctly not preserved). Both down directions: `against_depth_untestable`. Robustness floor on the Wilcoxon z swept, not chosen: 0.5 / 0.6 / 0.8 / 1.0 keep 30 / 30 / 27 / 7 of the 30 real genes and 0 background, while the pure-depth control passes 50% / 43% / 35% / 28% | 1 synthetic dataset | The down direction cannot be validated by this check at all |
| The skill library in the PI's planning prompt (Qwen3.8-27B at reasoning effort xhigh, 2026-10-01) | A study question that also asked for a gene-signature score, held under the differential-expression protocol (which does not name the skill): plans with the list applied the vetted `score_signature` template 15/15 on OpenRouter and 8/8 on our INT4 build, plans without it 0/8 and 0/8, each writing its own scoring code; English and Chinese alike. Under the signature protocol, which our router picks for these questions and which names the skill, both arms used it (4/4 each). A plain DE question: no plan named a skill, and no plan replaced a tool with a skill. With only a one-line description, 1/7 plans credited the template with a method it does not run (AUCell instead of `sc.tl.score_genes`); after the template's description named its method and the prompt forbade inventing one, every plan was correct | 79 plans, 4 per arm and question (12 per arm for the plain question on our build): 31 on OpenRouter, 48 on a serve job with the production settings and per-role effort | Planning only. Deterministic plan score 0.95 vs 0.98, mostly a formatting check; the enrichment tool was swapped for a code summary in 2 of 43 plans with the list and 0 of 36 without, too few to call |
| Evidence split in a team meeting: every expert sees all six findings vs each holds a slice | Shared: the experts debated methodology in general and cited methods absent from the evidence (scVI, MAST, DESeq2). Split: the debate anchored on the study's own tables, and the synthesis moved from generic rules to dataset-specific judgements | n=1 per arm | An illustration, not a rate; the synthesis prompt may be the larger lever |
| PROTOCOL.md vs SKILL.md as the agent-facing pipeline file (Qwen3.6, one IRD planning prompt) | Deterministic score tied at 11/11 (the rubric saturated). LLM judge, completeness / params / faithfulness: SKILL.md 9.3 / 8.7 / 8.7, PROTOCOL.md 8.0 / 7.7 / 6.0. The protocol arm added a forbidden report step in 3/3 trials (SKILL.md 1/3). Readability tied at 8/10. Cost +36–41% prompt tokens | 2 runs × 3 trials; judge on one run | The 2026-09-05 technical report summarised this as "more auditable at no planning cost"; the measured numbers show no planning gain, and human auditability was never tested |

### Annotation and variant interpretation

| What was tested | Result | N / scope | Main limitation |
|---|---|---|---|
| scGPT labels vs supplied labels on a mouse retina single-nucleus dataset (human-retina reference) | Neurons agree (rods 99.8%), but 73% of Müller glia come back "Astrocyte" and every endothelial cell "Microglia", at confidence ≥ 0.94 | 1 dataset | The reference has no endothelial or pericyte class; confidence is not accuracy |
| VEP variant line vs the lab's ANNOVAR-based IRD reference pipeline, same input VCF | 94% gene-level concordance on annotation, but the prioritised shortlist was clinically off-target (mitochondrial, PRAMEF and lncRNA noise; known IRD-gene candidates missing) | 1 benchmark VCF | The gap is specialisation, not annotation correctness |

### Infrastructure

| What was tested | Result | N / scope | Main limitation |
|---|---|---|---|
| Installing packages for `run_code`: Singularity persistent overlay vs `pip --target` into a bind-mounted cache (HPC3, 2026-08-10) | Overlay: non-root writes denied, fakeroot unavailable, a reader dies (`FATAL`) while a writer holds the overlay, 251 MB per small package. `--target`: no root needed, atomic publish by `mv -T`, four concurrent readers fine, 246 KB; the image's pandas and numpy still win over pip's copies | Probes on the production container | — |
| Inline Mermaid in the chat | `securityLevel: "strict"` alone stripped the script handler but still let a model-written `<img src>` through; with HTML labels off plus an SVG sanitizer, 0 injected elements | 1 injection vector, Chromium | Not fuzzed |
| Serving on 96 GB RTX PRO 6000 cards (2026-08 to 2026-09) | Qwen3.6-35B-A3B on one card with a 58.8 GiB KV pool (about 20 KiB KV per token; 262K context ≈ 5 GiB). DeepSeek-V4-Flash NVFP4 (127 GB): TP=4 works on SGLang 0.5.18, TP=2 runs out of memory. MiniMax-M2.7 FP8 (220 GB): TP=4 on vLLM 0.22.1 with a 196,608-token context; vLLM 0.27.1 and nightly hang in warm-up. MiniMax-M3 NVFP4 (228 GB) loads at TP=4 but returns corrupted output on every stable vLLM. Planning rule: aggregate VRAM ≈ 1.8 × weight size | 4-card node | Answers whether a model fits, not throughput (PCIe, no NVLink) |

## Design decisions and why

| Decision | Evidence |
|---|---|
| An explicit Research/Chat toggle, not a classifier | A research question misrouted to chat returns a fluent answer with no analysis behind it, and nothing flags it; the opposite misroute only costs waiting. The chat catalog is an allow-list (`chat: true`), so a new tool never joins it by accident |
| `SKILL.md` is the only file the agent loads; `PROTOCOL.md` is a rendering for people | The format A/B above. A readable protocol must be generated from the code that runs, with a staleness check, never a hand-forked copy |
| Plan feedback patches one step; a full redraft only when one edit cannot express the request | Plan-revision A/B: collateral changes 100% → 0%, intent 53% → 100% on the same weights, so a bigger model buys nothing for plan editing |
| Deterministic guards over prompt instructions | The served Qwen3.6 ignored prompt-only steering against enrichment without a contrast; a step answer swapped two arms' cell counts and the Critic accepted it at 0.95. Code now refuses to accept an errored or empty step, hard-skips no-contrast enrichment, and refuses answers whose per-arm counts contradict their own tools (`agents/step_numbers.py`) |
| The whole agenda is reviewed by the Critic and finalised by the PI before any step runs, on by default | A production plan re-clustered a dataset that already carried 11 expert labels, planned pseudobulk with no donor column and contradicted itself between steps — all visible at plan time. Costs one or two completions per run |
| Claims are audited before the report is written | Models knew the artefact caveats in isolation but did not apply them while writing, so each check became a short separate question carrying the dataset's design facts (invents 50% → 0%, quality 6.0 → 8.67) |
| A hypothesis needs a rival and a discriminator before it is admitted; adjudication picks hypothesis, rival or neither | A one-entry ledger marked a claim "supported" on a test the design fixed in advance (one library per arm, so pseudobulk finds nothing either way). `admit()` in `agents/hypotheses.py` rejects a missing or restated rival, an empty discriminator, and a between-arm significance test with fewer than two replicates per arm |
| The LLM does language; the ontology owns HPO IDs | A transposed ID is a different real phenotype (HP:0000662 Nyctalopia vs HP:0000622 Blurred vision) and fails silently. The model picks a numbered candidate from a retrieved closed set; `run_lirical` re-validates every incoming ID; each call checks that the bundled HPO release matches LIRICAL's |
| Report LIRICAL's rank and compositeLR, never its posterior as a confidence | The posterior saturates above an LR of about 10⁶ (a 1.44× LR gap displayed as 99.9692% vs 99.9557%) and moved 7.7× on near-synonym term choice |
| Literature evidence never rewrites the calibrated probability | LIRICAL asks how well the patient matches; literature asks whether the association is established. A separate `final_score` (literature weighted 0.65, because curation lags the literature) ranks candidates while `posttest_prob` stays untouched and no probability is invented for a literature-only candidate. Evidence tiers are capped by the retrieved passages, counting independent sources |
| `think=False` for bounded structured-extraction calls to a reasoning model | The served model spent 4,529 reasoning tokens on one note and returned empty content at an 800-token cap, still empty at 4,000, with no error raised |
| Production runs Qwen3.8 at low reasoning effort | At default effort it could not finish a plan; low effort planned as well as medium in less time |
| Reproduce the lab IRD pipeline's filter logic on VEP instead of wrapping its ANNOVAR code | Parity target is clinical-grade (the same candidates surface), not byte-identical; the reference pipeline serves as an oracle to diff against. Region restriction runs before VEP, because a gene filter after VEP scopes the result but does not cut annotation time |
| Depth-matched DE treats only the up direction as testable | Depth can only manufacture apparent up-regulation in the deeper arm; a surviving-effect rule passed 92% of pure background in the down direction. The earlier tool applied one ρ threshold to both directions and counted untestable down rankings as failures in a real run |
| scGPT runs as a separate short-lived GPU batch job | scGPT is a gene-token model, not an LLM, so the served LLM cannot stand in; sharing the LLM's GPU risks OOM and a second persistent GPU sits idle. All jobs reuse the one SSH session, so it adds no user step |
| File triage: a deterministic peek at upload, an LLM description only when a model is already up | An upload must never trigger a GPU; deterministic facts (format, assembly, sample IDs) are stamped back over the model's description so it cannot contradict the bytes |
| The agent's shell runs on a CPU node the user holds, with no command allowlist | HPC3 login nodes are for logging in and submitting jobs, so placement makes compliance structural. Writes outside the user's areas, destructive commands, downloads from non-allowlisted hosts and publishing to the shared cache raise a confirmation; with no approver wired, the answer is no |
| The shared package cache is immutable, content-keyed and appended to `sys.path` | The overlay measurements above; appending rather than `PYTHONPATH` stops a cached module shadowing the image's, verified by a counterfactual test |
| Bring-your-own keys: a stable credential id, rotation verifies the new key first, the key is fixed at run start, consent is recorded per credential | A key can be rotated without breaking references or a running analysis. No string scanner can tell whether a gene list identifies a rare-disease patient, so consent is the control and the payload scanner is a backstop for secrets and raw matrices |
| Agent memory is private per agent, kept on the gateway, and holds method lessons only | Containers are ephemeral and network-off; dataset numbers must never enter a lesson; memory steers frozen weights and cannot add a skill the model lacks |
| HPC3 process files go to a per-user Temp area swept after 3 days by a Slurm job | Nothing cleaned per-run files before, and an automated delete cannot run in folders people curate by hand. A unit is deleted only when its whole subtree is cold; uploads are never deleted automatically |
| Role-split recommendation (2026-09-05): DeepSeek-V4-Flash for PI and writer, MiniMax-M2.7 for the Scientist | From the model A/B. Each fits four 96 GB cards at TP=4; serving both at once needs eight. A recommendation only — the production model as of 2026-09-25 is Qwen3.8 |

## Known limitations and open problems

- **Phenotype line.** LIRICAL scores only against curated HPO/OMIM annotations, so a gene with a new
  disease association cannot rank; a low rank means "not curated", never "not causal". The posterior
  is uncalibrated end to end (calibration on solved cases is planned, not done), and no
  clinician-curated HPO baseline exists. Nothing forces the phenotype step when a study description
  contains symptoms; the model still decides to call it. Per-term likelihood ratios live only in
  LIRICAL's HTML output, which earlier runs did not keep.
- **Literature evidence.** The evidence-tier grading is calibrated against the ClinGen rubric, not yet
  against how the real corpus retrieves (as of 2026-08-05). The corpus sits in one member's personal
  storage; if it moves, `deep_literature` silently degrades to `dependency_missing` (as of 2026-08-08).
- **Variant line.** AlphaMissense is GRCh38-only. On the IRD roadmap (July 2026), gene-level
  constraint (pLI/RVIS/GDI), compound-het phasing and feeding the inclusion reason into the shortlist
  are not done, and the retina-specific layers need an HPC3 run to verify. How a patient's phenotype
  enters a variant run for Exomiser-style scoring is undecided.
- **Model limits.** Qwen3.8 and Qwen3.6 miss that total-count normalisation does not remove a depth
  effect, and do not flag contradictory count tables. Qwen3.8 over-claimed more than Qwen3.6 in
  writing (n=2). The model evaluation covers one dataset and one question.
- **Depth-matched DE.** Down-regulated genes in a depth-imbalanced design are neither validated nor
  refuted; the check cannot test them.
- **scGPT.** Glial and endothelial labels are not trustworthy against the human-retina reference, and
  their confidence is high anyway.
- **Orchestration.** On the DAG path: resume, hard enforcement of each node's declared inputs and
  outputs, reuse of outputs that already exist, and dynamic re-planning are not built; per-step
  meetings only amend (they cannot skip or prune) there. Agent memory v1 is read and written only on
  the DAG path, so the default linear planner does not use it, and retrieval is keyword/recency, not
  semantic. The hypothesis-ledger design left a second Critic axis — is this verdict the right
  inference, not just faithful to its artifact — as separate, unbuilt work.
- **Bring-your-own keys.** Every provider path was exercised against a stub endpoint, not real
  provider accounts (as of 2026-08-10).
- **Format A/B.** Whether `PROTOCOL.md` is easier for a researcher to audit was never tested with a
  human reader; the LLM judge is the wrong instrument for it.
- **Serving.** MiniMax-M3, the strongest all-rounder in the model A/B, needs NVFP4 support that is not
  in a stable vLLM release; the stable path returns corrupted output without raising.
