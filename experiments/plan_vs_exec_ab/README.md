# Plan vs execution vs writing — is it the model?

**Question (Yijun, 2026-08-19):** on the DDX41 retina runs, is the model writing a *bad plan*, or is it
*executing* a fine plan badly (or writing the report badly)? Test all three, with OpenRouter allowed.

## Design — same scaffolding, swap only the model

Every arm goes through the **real production code paths** (the `ResearchLab` methods are called
directly, not re-implemented), on the **real dataset** (`Ddx41_DEG.h5ad`, 15,307 cells), with the
**real tools running locally**. So: a difference between arms is the model; a defect shared by every
arm is the scaffolding.

| stage | what runs | held constant | measured |
|---|---|---|---|
| **A · plan** | `ResearchLab._pi_plan` | `_PI_SYSTEM`, protocol guidance (`differential_expression` v2), dataset profile incl. `design_by_arm` (1.6× depth imbalance flagged), tool roster | deterministic rubric (14 checks: tool validity, per-cell-type DE, depth-aware, replication-aware, enrichment after DE, no busywork, …) + 2 blind LLM judges |
| **B · execute** | `ResearchLab._scientist` → `ResearchHarness` (8-turn loop) → `_critic`, per step | ONE fixed plan = the 7-step plan the production PI wrote for run `8847d521ba32`; the workspace is **seeded with the outputs of the earlier steps** (production's own recorded tool calls replayed locally); the same specialist persona | did it call the tool the step names, with the args the step specifies; run_code count; run_code that re-implements a catalog tool; reconnaissance calls; hit `max_steps`; finished with a non-empty answer; Critic accept; wall-clock; tokens; + blind judge |
| **C · write** | `lab._synthesize` → `gateway._build_report` → `gateway._review_report` | the SAME accepted findings + figures/tables of run 8847; two prompt variants: **prefix** = the gateway prompts deployed at `9d72d43` and the pre-fix dataset profile; **current** = the rules added in `06e4937` (NUMBER FORMAT / REPLICATION WORDING / DESCRIPTIVE DE / CAPTION TRUTH) + `design_by_arm` in the dataset section | sci-notation %, "significant" claims, multi-donor wording, depth/library-size caveat, embedded figures, unrendered markup + blind judge (raises technical-artefact possibility? consistent about replication? overclaims? invents?) |

Arms (OpenRouter ids; providers pinned because Venice returned empty content for Qwen tool calls):

| arm | model | thinking | why |
|---|---|---|---|
| `qwen36-35b` | qwen/qwen3.6-35b-a3b | off | = prod's **Scientist** loop (`chat_tools`, think=False) |
| `qwen36-35b-think` | same | medium | = prod's **PI / Critic / writer** (`vllm_client.complete` default think=True + `--reasoning-parser qwen3`) |
| `qwen35-122b` | qwen/qwen3.5-122b-a10b | off | bigger open-weight, fits 4×96 GB |
| `deepseek-v4-pro` | deepseek/deepseek-v4-pro | off | strong open-weight |
| `sonnet-5` | anthropic/claude-sonnet-5 | off | frontier ceiling |
| `gpt-5.4` | openai/gpt-5.4 | default | frontier ceiling |
| `minimax-m2.7` | minimax/minimax-m2.7 (229B-A10B) | provider default | round 2: candidate for our 4×96 GB node (FP8 fits) |
| `minimax-m3` | minimax/minimax-m3 (428B-A23B, multimodal) | provider default | round 2: candidate (does NOT fit at FP8) |
| `deepseek-v4-flash` | deepseek/deepseek-v4-flash-0731 (304B, MIT) | provider default / `-low` = effort low + 6× budget | round 2: candidate (FP8 fits, tight) |

Judges: `anthropic/claude-opus-5`, `google/gemini-3.1-pro-preview` (blind to the author arm).

Caveats: OpenRouter serves Qwen3.6 as fp8 (prod = AWQ 4-bit); the local tools run the same code as
HPC3 but on a laptop (no Slurm queue, so wall-clock is model + compute only); the fixed plan for B was
written by the production model, so B measures execution *of that plan* for every arm equally.

## Run

```bash
SP=<scratch>   # holds Ddx41_DEG.h5ad and e2e_results/8847d521ba32/artifacts/process/run_state.json
python experiments/plan_vs_exec_ab/run_ab.py --stage prep  --out $SP/pve_out --run-state … --dataset …
python experiments/plan_vs_exec_ab/run_ab.py --stage A --reps 3 --workers 6 …
python experiments/plan_vs_exec_ab/run_ab.py --stage B --reps 2 --workers 4 …
python experiments/plan_vs_exec_ab/run_ab.py --stage C --reps 2 --workers 4 …
python experiments/plan_vs_exec_ab/run_ab.py --stage judge …
python experiments/plan_vs_exec_ab/run_ab.py --stage report …     # → SUMMARY.md
```

Results of the 2026-08-19 run: `results/SUMMARY.md` (verdict + tables), raw `results/*.jsonl`, five
sample reports under `results/sample_reports/`.

Lessons that cost hours: (1) run `caffeinate -dims` first — a sleeping laptop looks exactly like
stalled providers (synchronized connection resets every ~17 min, 1000-s "API calls"); (2) pin
OpenRouter providers per model (Venice returned empty content for Qwen tool calls; `require_parameters`
keeps tool-less endpoints out); (3) the client streams and abandons a call that delivers nothing for
120 s; (4) `--stage B` must not be killed with a bare `pkill` — the spawn workers survive and keep
writing into the trial workspaces (kill `multiprocessing.spawn` children too).

## Round 6 (2026-09-25): Qwen3.6-35B-A3B-AWQ vs Qwen3.8-27B-INT4, both on our own RTX PRO 6000s

Results are in `results_qwen38/SUMMARY.md`. Arms `qwen36-awq-local` / `qwen38-int4-local[-low|-medium]`
are reached over SSH tunnels (`QWEN36_LOCAL_URL` / `QWEN38_LOCAL_URL`).

- **Planning (A, 8 plans per arm, judges: sound / specific / dataset fidelity):** 3.6 scored
  8.81 / 9.00 / 9.31 at 72 s. 3.8-low scored 9.12 / 9.38 / 9.50 at 147 s, and 3.8-medium
  9.06 / 9.38 / 9.56 at 179 s.
- **3.8 at default effort** could not finish a plan: about 22k thinking tokens, truncated at the
  24k cap. So prod runs with `BIOAGENT_VLLM_REASONING_EFFORT=low`.
- **Writing (C, n=2, current prompts):** judged quality was a tie (7.25 each). 3.8 writes longer
  (28k vs 19k chars) and takes twice as long (~380 s vs ~195 s). It over-claims more often
  (75% vs 25%, 3 vs 0.5 "significant"). This is a small sample, so treat it as a caution, not a
  verdict.


### Round 6b (2026-09-28): does the model KNOW what its report got wrong?

`results_qwen38/reasoning_probe.py` asks each report error as a short standalone question, 2 samples
per model (3.8 at prod's low effort, 3.6, Sonnet 5 as reference; results in `reasoning_probe.json`):

| question | 3.8 | 3.6 | Sonnet 5 |
|---|---|---|---|
| same-direction ribosomal up-shift in all 5 cell types with a 1.6x depth gap: headline finding? (NO) | 2/2 | 2/2 | 2/2 |
| rod genes (Rho/Gnat1/Pde6g) "down" in amacrine cells: explanation? (contamination) | 2/2 | 2/2 | 2/2 |
| does total-count normalisation + log1p remove a 1.6x depth effect on a Wilcoxon test? (NO) | 0/2 | 1/2 | 2/2 |
| two contradictory count tables: does it flag the contradiction? | 1/2 | 0/2 | 2/2 |

Both Qwens answer the first two correctly in isolation, yet their full reports headlined the
up-shift and (3.8) explained the rod genes as a "paracrine cascade". The knowledge is there, but
it is not applied during long synthesis. The last two rows are genuine reasoning gaps.

### Round 6c/6d (2026-09-28): the claim audit (`agents/claim_audit.py`), writing stage C

Same inputs and model (3.8 INT4 at low effort), 3 reports per arm, 2 blind judges:

| arm | overclaims | invents | raises depth artefact | quality |
|---|---|---|---|---|
| no audit | 83% | 50% | 67% | 6.0 |
| audit v1 | 17% | 83%* | 100% | 7.0 |
| audit v2 (authoritative design numbers in the block, no "ROBUST" label) | 33% | **0%** | 100% | **8.67** |

\* v1's "invents" flags were mostly the upstream misreported per-arm cell counts of run 8847's DE step,
copied from the findings. v2 states the dataset's own counts and depth ratio as authoritative, and
they disappeared. What remains: FDR-filtered GSEA term counts reported as findings. A rule against
it was added after this measurement and has not been re-measured.
