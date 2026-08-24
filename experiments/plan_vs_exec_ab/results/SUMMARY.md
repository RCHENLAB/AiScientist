# Plan vs execution vs writing — is it the model? (2026-08-19, two rounds)

**Question.** On the DDX41 runs, is Qwen3.6-35B writing a *bad plan*, or *executing* a fine plan badly, or writing the
report badly? Same scaffolding (the real `ResearchLab` code paths, the real dataset, the real tools running locally),
swap only the model. Design and arms: [README.md](../README.md). Round 2 added the open-weight candidates Yijun asked
about for our own 4×96 GB node: MiniMax-M2.7, MiniMax-M3, DeepSeek-V4-Flash-0731.

**Which arm is "prod"?** In production the PI / Critic / writer calls go through `vllm_client.complete()` whose
default is `think=True` with `--reasoning-parser qwen3`, i.e. **thinking ON**; only the Scientist tool loop
(`chat_tools`, `think=False`) and a few bounded helpers run without it. So read **`qwen36-35b-think` as prod for
stages A and C** and **`qwen36-35b` as prod for stage B**. (The first write-up of this experiment said prod had
thinking off everywhere — that was wrong; corrected here.)

## Verdict

| stage | what the evidence says | model or scaffolding? |
|---|---|---|
| **A · plan** | Identical prompt / profile / guidance; the profile says "⚠ DEPTH IMBALANCE 1.6×". Plans that check or correct it: **prod (Qwen3.6 + thinking) 2/3** (judges 50 %), Qwen3.6 without thinking 0/3, Qwen3.5-122B 1/3; **every other model 100 %** — DeepSeek-V4-pro/-flash, MiniMax-M2.7/-M3, Sonnet 5, GPT-5.4. Judge soundness: 6.0 (no-think) · **7.2 (prod)** · 8.5 (DeepSeek-pro, MiniMax-M2.7) · 9.25–9.5 (MiniMax-M3, DeepSeek-flash, Sonnet, GPT). Basics (stratified DE, composition, enrichment after DE, replication caveat, no hallucinated tools) are right for every arm. | **Model.** A clean gradient on the one thing that needed judgement; thinking OFF would make it worse, so the prod setting is right — the weights are the limit. |
| **B · execute the SAME 7-step plan** | Every model **calls the right tool with the right arguments** (86–93 % of steps; argument match 1.0 whenever called). The failure is *after* the tool returns: 2.7–4.3 further calls (run_code re-reading the tables it just wrote, `list_dir` of the same folders) until the 8-call budget is spent — **50–83 % of steps hit `max_steps`, and a step that hits max_steps ends with an EMPTY final answer** (answer present: prod Qwen 4/14, Qwen-think 5/14, Qwen3.5 2/12, DeepSeek-pro 3/6, **MiniMax-M2.7 7/14, MiniMax-M3 6/14**, DeepSeek-flash 3/14). The Critic accepts 67–93 % anyway on the artefacts. Step 7 (a pure run_code synthesis step) was finished by **nobody** (0/13). Code that actually ran clean: MiniMax-M3 95 %, DeepSeek-flash 90 %, MiniMax-M2.7 86 %, Qwen 79–82 %. | **Scaffolding (with a model component).** Seven models of very different size, same pathology ⇒ the loop's contract (8 tool calls; a brief that says "re-ground by reading the cited artifact"; no "the tool returned — finish now" rule; an empty answer at max_steps that the writer then has to do without). MiniMax-M2.7 copes best, DeepSeek-flash worst; none escapes it. Frontier arms (Sonnet/GPT) not measured for B (round-1 key cap; not repeated on purpose). |
| **C · write the SAME findings** | Same facts, figures, tables. **Prod (Qwen-think) with the pre-fix prompt — the configuration that wrote run 8847's report — raises the "technical artefact" possibility 0/4** (that is the report Yijun reviewed); with the `06e4937` rules 3/4, overclaims 1/4, quality 5.5. Others with the current rules, judged quality: **Sonnet 5 8.5, DeepSeek-V4-flash (low effort) 8.0, MiniMax-M3 7.5**, DeepSeek-V4-pro 4.75, MiniMax-M2.7 4.5 (verbose, 42 k chars, "significant" 6.5×), Qwen3.5-122B 3.25. Qwen3.6 without thinking: 4.25 and still "significant" 4/4 under the rules. Depth facts were in every writer's previews (`qc_descriptive_summary.csv`); the difference is connecting them. **Budget trap:** DeepSeek-flash at provider-default reasoning spent the entire 24 k output budget thinking on the synthesize call and returned EMPTY content 4/4 (quality 2); at `effort=low` + 6× budget it is the second-best writer. | **Model, partly recoverable.** Rules stop the mechanical errors (sci-notation, invented captions) but not judgement; a stronger writer is the real fix — and any reasoning model needs an output budget sized for its thinking or the writer silently produces nothing. |

## Which of the three candidates for the 4× RTX PRO 6000 (384 GB) node?

| model | size | fits 384 GB? | plan | execute | write (current rules) | code ran clean | cost on OpenRouter |
|---|---|---|---|---|---|---|---|
| **MiniMax-M2.7** | 229B-A10B (M2 family: M2/2.1/2.5/2.7, newest 2.7) | **yes, FP8 ≈ 230 GB, comfortable** (4-bit ≈ 120 GB) | 8.5, depth 2/2 | **best**: 50 % finished w/ answer, fewest recon calls, grounded 4.5 | 4.5 — verbose, over-claims | 86 % | /bin/zsh.30/.20 |
| **MiniMax-M3** | 428B-A23B, native multimodal (text/image/video), 1M ctx | **no at FP8 (428 GB)**; only at FP4/INT4 (≈215 GB, CoreWeave/Morph serve fp4) — quality cost + VL memory | 9.25, depth 2/2 | 2nd: 43 % w/ answer, grounded 4.0 | **7.5**, artefact 2/2 | **95 %** | /bin/zsh.30/.20 |
| **DeepSeek-V4-Flash-0731** | 304B total (284B-A13B + DSpark spec-decode module), MIT, native FP8 | **yes, FP8 ≈ 300 GB — tight** (≈ 20 GB/GPU left for KV; 4-bit comfortable); needs a vLLM recent enough for V4 + DSpark (our vllm.sif is not) | **9.5**, depth 2/2 | weakest: 21 % w/ answer, most run_code (4.4/step) | **8.0 at effort=low**; empty at default effort | 90 % | **/bin/zsh.14//bin/zsh.28** (cheapest) |

Recommendation: **DeepSeek-V4-Flash-0731 for the PI + writer roles** (best plan and second-best write, MIT, cheapest; run it
with a capped reasoning effort and a ≥32 k output budget, never the provider default), and **MiniMax-M2.7 for the
Scientist loop** (best execution behaviour, interleaved thinking is built for tool use, fits FP8 with room). If one model
has to do everything on one node, MiniMax-M3 is the best all-rounder on the numbers but does not fit at FP8 — so the
practical single-model pick is DeepSeek-V4-Flash with the execution-loop fix below (its execution weakness is mostly the
same scaffolding problem every model has). The role split already exists (`BIOAGENT_LAB_LLM_*`).

## What to change in the product

1. **Fix the execution loop, not the model:** after the step's named tool succeeds, allow ≤ 2 further calls and then force
   `finish`; at `max_steps` never return an empty answer — synthesise it from the tool digest. Every model tested needs
   this; it is also most of the 79 minutes (each run_code on HPC3 is a Slurm job).
2. **Keep thinking ON for PI/Critic/writer** (it already is); size the output budget to the model (a reasoning model with a
   2–8 k cap returns nothing). The Scientist loop can stay without thinking.
3. **Buy/serve a stronger model for PI + writer only** (see the table) via `BIOAGENT_LAB_LLM_*`.

## Caveats

* Round 1's OpenRouter key ( cap) ran out mid-Stage-B: all Sonnet 5 / GPT-5.4 execution trials, 8 DeepSeek-pro trials,
  2 Qwen3.5 trials and the GPT-5.4 reports did not run. Round 2 (after the refill) ran the three candidates fully (A 2 reps,
  B 7×2, C 2×2) plus the flash low-effort re-run for A/C; total spend both rounds ≈ .
* **Wall-clock is not reported.** The laptop idle-slept in 17-minute cycles for the first ~5 h of round 1 (DarkWake log),
  which looked exactly like stalled providers; `caffeinate` fixed it. Token and call counts are unaffected.
* Qwen3.6 on OpenRouter is fp8 (prod: AWQ 4-bit). Four Qwen3.6 step-1/3 trials are reconstructed from their logged metrics.
* The fixed plan for stage B was written by the production model, so B measures execution *of that plan* for every arm.
* Round-2 arms ran at provider-default reasoning (MiniMax interleaved thinking; DeepSeek default effort) in all three stages,
  i.e. thinking ON in the Scientist loop too — slightly more favourable than prod's no-think Scientist.


# Raw tables

Question: What changes between DDX41 mutant and WT retina?
Fixed plan for stage B: production run 8847d521ba32 (7 steps).


## A · Planning (real `_pi_plan`, current prompt + dataset profile incl. design_by_arm)

| arm | n | steps | rubric | halluc. tools | depth-aware | replication-aware | judge: sound | specific | dataset-fid | depth✓ (judge) | seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen36-35b | 3 | 8.33 | 0.93 | 0 | 0% | 100% | 6 | 6.83 | 7.5 | 0% | 24.87 |
| qwen36-35b-think | 3 | 7.67 | 0.98 | 0 | 67% | 100% | 7.17 | 7.67 | 8.17 | 50% | 284.97 |
| qwen35-122b | 3 | 9 | 0.93 | 0 | 33% | 100% | 6.33 | 7.33 | 8 | 17% | 13.2 |
| deepseek-v4-pro | 3 | 11 | 0.93 | 0 | 100% | 100% | 8.5 | 9.17 | 9.5 | 100% | 44.77 |
| sonnet-5 | 3 | 11.33 | 0.98 | 0 | 100% | 100% | 9.5 | 9.5 | 10 | 100% | 79.83 |
| gpt-5.4 | 3 | 15.67 | 0.91 | 0 | 100% | 100% | 9.33 | 9.33 | 9.67 | 100% | 29.63 |
| minimax-m2.7 | 2 | 9 | 1.0 | 0 | 100% | 100% | 8.5 | 8.5 | 8.75 | 100% | 53.85 |
| minimax-m3 | 2 | 11 | 1.0 | 0 | 100% | 100% | 9.25 | 9.5 | 9.75 | 100% | 252.35 |
| deepseek-v4-flash | 2 | 10 | 1.0 | 0 | 100% | 100% | 9.5 | 9.5 | 9.75 | 100% | 162.55 |
| deepseek-v4-flash-low | 2 | 8 | 1.0 | 0 | 100% | 100% | 9.5 | 9.5 | 9.75 | 100% | 41.8 |

## B · Execution of the SAME 7-step plan (real `_scientist` + real local tools + real `_critic`)

| arm | trials | named tool called | named tool ok | arg match | run_code/step | re-implemented tool | recon calls/step | hit max_steps | finished w/ answer | critic accept | judge: did step | grounded | not wasted | prompt tok/step |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen36-35b | 14 | 86% | 86% | 1.0 | 3.36 | 0 | 3.79 | 64% | 29% | 71% | 6.25 | 2.54 | 3.93 | 170170.2 |
| qwen36-35b-think | 14 | 93% | 93% | 1.0 | 3 | 0 | 3.79 | 57% | 36% | 93% | 7.25 | 3.07 | 5.04 | 145064.07 |
| qwen35-122b | 12 | 92% | 92% | 1.0 | 1.42 | 0 | 4.92 | 83% | 17% | 92% | 6.12 | 1.54 | 4.67 | 133200.58 |
| deepseek-v4-pro | 6 | 67% | 67% | 1.0 | 2.33 | 0 | 5 | 50% | 50% | 67% | 6.17 | 4.75 | 3.67 | 117899 |
| minimax-m2.7 | 14 | 93% | 93% | 1.0 | 4 | 2 | 2.5 | 50% | 50% | 71% | 6.68 | 4.46 | 5.5 | 124676.14 |
| minimax-m3 | 14 | 93% | 93% | 1.0 | 2.93 | 2 | 5.71 | 57% | 43% | 79% | 6.82 | 4 | 4.54 | 137808.79 |
| deepseek-v4-flash | 14 | 86% | 86% | 1.0 | 4.43 | 2 | 4.86 | 79% | 21% | 79% | 5.54 | 2.04 | 3.75 | 156830.64 |

Per step (named tool ok % / mean run_code):

| arm | s1 run_scanpy_qc | s2 run_de | s3 run_composition | s4 run_clustering | s5 run_enrichment | s6 run_gsea_prerank | s7 run_code |
|---|---|---|---|---|---|---|---|
| qwen36-35b | 100% / 2.5 | 100% / 4 | 100% / 4.5 | 100% / 5 | 50% / 3.5 | 100% / 3.5 | 50% / 0.5 |
| qwen36-35b-think | 100% / 2.5 | 100% / 3.5 | 100% / 1 | 100% / 5 | 100% / 2 | 50% / 3 | 100% / 4 |
| qwen35-122b | 100% / 0.5 | 100% / 1.5 | 100% / 0 | 100% / 3 | 100% / 1.5 | 50% / 0 | 100% / 7 |
| deepseek-v4-pro | 100% / 2 | 50% / 3.5 | 50% / 1.5 | — | — | — | — |
| sonnet-5 | — | — | — | — | — | — | — |
| gpt-5.4 | — | — | — | — | — | — | — |
| minimax-m2.7 | 100% / 1 | 100% / 3.5 | 100% / 2 | 100% / 5.5 | 50% / 6 | 100% / 4 | 100% / 6 |
| minimax-m3 | 100% / 2 | 100% / 2 | 100% / 1.5 | 100% / 4.5 | 50% / 0 | 100% / 3.5 | 100% / 7 |
| deepseek-v4-flash | 100% / 5.5 | 50% / 3.5 | 100% / 2 | 100% / 6 | 50% / 3 | 100% / 4 | 100% / 7 |
| deepseek-v4-flash-low | — | — | — | — | — | — | — |

Execution errors: 38 trials (all sonnet-5 and gpt-5.4, 8 deepseek-v4-pro, 2 qwen35-122b) failed with `HTTP 403 Key limit exceeded` in round 1 — excluded from every statistic above. deepseek-v4-flash-low was run for A and C only.

## C · Writing the SAME accepted findings (real `_synthesize`)

| arm | prompt | n | chars | sci-notation % | 'significant' claims | multi-donor claim | depth mentioned | technical caveat | judge: raises artefact | consistent replication | overclaims | invents | quality |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen36-35b | prefix | 2 | 20652.5 | 0 | 4.5 | 0 | 0 | 0% | 0% | 75% | 100% | 100% | 3.5 |
| qwen36-35b | current | 2 | 21501 | 0 | 4 | 0 | 1 | 0% | 25% | 100% | 100% | 25% | 4.25 |
| qwen36-35b-think | prefix | 2 | 17436.5 | 0 | 1 | 0 | 1 | 0% | 0% | 100% | 75% | 50% | 4 |
| qwen36-35b-think | current | 2 | 22308.5 | 0 | 0 | 0 | 3 | 50% | 75% | 75% | 25% | 75% | 5.5 |
| qwen35-122b | prefix | 2 | 22782.5 | 0 | 5.5 | 0 | 1 | 0% | 0% | 100% | 100% | 75% | 3.5 |
| qwen35-122b | current | 2 | 24581 | 0 | 4 | 0 | 1 | 0% | 0% | 100% | 100% | 50% | 3.25 |
| deepseek-v4-pro | prefix | 2 | 28725.5 | 0 | 1 | 0 | 0.5 | 0% | 0% | 100% | 50% | 25% | 5.25 |
| deepseek-v4-pro | current | 2 | 28468.5 | 0 | 4 | 0 | 2.5 | 50% | 75% | 100% | 100% | 100% | 4.75 |
| sonnet-5 | prefix | 2 | 21523.5 | 0 | 0 | 0 | 4 | 0% | 50% | 100% | 50% | 25% | 6.75 |
| sonnet-5 | current | 2 | 23817.5 | 0 | 2 | 0 | 9 | 100% | 100% | 100% | 50% | 0% | 8.5 |
| minimax-m2.7 | prefix | 2 | 35303 | 0 | 13 | 0 | 3.5 | 0% | 0% | 75% | 100% | 100% | 2.75 |
| minimax-m2.7 | current | 2 | 41738.5 | 0 | 6.5 | 0 | 9.5 | 0% | 75% | 100% | 50% | 100% | 4.5 |
| minimax-m3 | prefix | 2 | 32199 | 0 | 3 | 1 | 2.5 | 0% | 0% | 100% | 100% | 25% | 4.75 |
| minimax-m3 | current | 2 | 27966.5 | 0 | 3 | 1 | 4.5 | 50% | 100% | 100% | 75% | 50% | 7.5 |
| deepseek-v4-flash | prefix | 2 | 18279.5 | 0 | 2.5 | 0 | 2 | 0% | 0% | 75% | 50% | 25% | 3.25 |
| deepseek-v4-flash | current | 2 | 8279.5 | 0 | 0.5 | 0 | 0 | 0% | 0% | 25% | 50% | 50% | 2 |
| deepseek-v4-flash-low | prefix | 2 | 27854.5 | 0 | 2.5 | 0 | 3 | 50% | 0% | 100% | 75% | 0% | 5.5 |
| deepseek-v4-flash-low | current | 2 | 26003 | 0 | 2 | 0 | 9.5 | 100% | 100% | 100% | 75% | 0% | 8 |

## Tokens / cost / latency (this experiment, per arm)

| arm | calls | prompt tok | completion tok | USD | seconds |
|---|---|---|---|---|---|
| qwen36-35b | 133 | 2400073 | 102191 | 0.36 | 867 |
| qwen36-35b-think | 122 | 2215233 | 192678 | 0.47 | 1934 |
| qwen35-122b | 121 | 1972666 | 85764 | 0.72 | 1501 |
| deepseek-v4-pro | 72 | 1106999 | 85715 | 0.79 | 1076 |
| minimax-m2.7 | 120 | 1941966 | 160449 | 0.46 | 2600 |
| minimax-m3 | 125 | 2131678 | 246656 | 0.55 | 1891 |
| deepseek-v4-flash | 133 | 2350080 | 417221 | 0.27 | 3399 |
| sonnet-5 | 11 | 271514 | 89532 | 1.44 | 787 |
| gpt-5.4 | 3 | 41082 | 7687 | 0.16 | 88 |
| judge-opus5 | 6 | 51331 | 1724 | 0.30 | 44 |
| judge-gemini31 | 6 | 35940 | 6177 | 0.15 | 59 |
| deepseek-v4-flash-low | 12 | 194505 | 187713 | 0.08 | 1164 |
