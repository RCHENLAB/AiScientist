# Plan vs execution vs writing — same scaffolding, different models

Question: What changes between DDX41 mutant and WT retina?
Fixed plan for stage B: production run 8847d521ba32 (7 steps).


## A · Planning (real `_pi_plan`, current prompt + dataset profile incl. design_by_arm)

| arm | n | steps | rubric | halluc. tools | depth-aware | replication-aware | judge: sound | specific | dataset-fid | depth✓ (judge) | seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen36-35b | 3 | 8.33 | 0.91 | 0 | 0% | 100% | 6 | 6.83 | 7.5 | 0% | 24.87 |
| qwen36-35b-think | 3 | 7.67 | 0.98 | 0 | 67% | 100% | 7.17 | 7.67 | 8.17 | 50% | 284.97 |
| qwen35-122b | 3 | 9 | 0.94 | 0 | 33% | 100% | 6.33 | 7.33 | 8 | 17% | 13.2 |
| deepseek-v4-pro | 3 | 11 | 0.93 | 0 | 100% | 100% | 8.5 | 9.17 | 9.5 | 100% | 44.77 |
| sonnet-5 | 3 | 11.33 | 0.98 | 0 | 100% | 100% | 9.5 | 9.5 | 10 | 100% | 79.83 |
| gpt-5.4 | 3 | 15.67 | 0.83 | 0 | 100% | 100% | 9.33 | 9.33 | 9.67 | 100% | 29.63 |
| minimax-m2.7 | 2 | 9 | 1.0 | 0 | 100% | 100% | 8.5 | 8.5 | 8.75 | 100% | 53.85 |
| minimax-m3 | 2 | 11 | 1.0 | 0 | 100% | 100% | 9.25 | 9.5 | 9.75 | 100% | 252.35 |
| deepseek-v4-flash | 2 | 10 | 1.0 | 0 | 100% | 100% | 9.5 | 9.5 | 9.75 | 100% | 162.55 |
| deepseek-v4-flash-low | 2 | 8 | 0.97 | 0 | 100% | 100% | 9.5 | 9.5 | 9.75 | 100% | 41.8 |
| laguna-s-2.1 | 3 | 6.33 | 0.89 | 0 | 100% | 100% | 9.33 | 9.33 | 9.83 | 100% | 156.3 |

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
| deepseek-v4-flash-low | 14 | 86% | 86% | 0.9 | 4.36 | 1 | 3.79 | 64% | 21% | 71% | 6.18 | 2.04 | 4 | 145191.93 |
| laguna-s-2.1 | 14 | 57% | 57% | 1.0 | 2 | 0 | 8.79 | 71% | 14% | 29% | 4.64 | 2.86 | 4.32 | 171073 |

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
| deepseek-v4-flash-low | 100% / 5 | 50% / 2.5 | 100% / 2 | 100% / 5 | 50% / 5 | 100% / 3 | 100% / 8 |
| laguna-s-2.1 | 50% / 2 | 0% / 1.5 | 50% / 1 | 100% / 4.5 | 50% / 0 | 50% / 0 | 100% / 5 |

Execution errors (38): deepseek-v4-pro#s5#0: RuntimeError: deepseek-v4-pro: HTTP 403: {"error":{"message":"Key limit exceeded; deepseek-v4-pro#s5#1: RuntimeError: deepseek-v4-pro: HTTP 403: {"error":{"message":"Key limit exceeded; deepseek-v4-pro#s6#0: RuntimeError: deepseek-v4-pro: HTTP 403: {"error":{"message":"Key limit exceeded; deepseek-v4-pro#s6#1: RuntimeError: deepseek-v4-pro: HTTP 403: {"error":{"message":"Key limit exceeded; deepseek-v4-pro#s7#0: RuntimeError: deepseek-v4-pro: HTTP 403: {"error":{"message":"Key limit exceeded; deepseek-v4-pro#s7#1: RuntimeError: deepseek-v4-pro: HTTP 403: {"error":{"message":"Key limit exceeded; sonnet-5#s1#0: RuntimeError: sonnet-5: HTTP 403: {"error":{"message":"Key limit exceeded (total; sonnet-5#s1#1: RuntimeError: sonnet-5: HTTP 403: {"error":{"message":"Key limit exceeded (total; sonnet-5#s2#0: RuntimeError: sonnet-5: HTTP 403: {"error":{"message":"Key limit exceeded (total; sonnet-5#s2#1: RuntimeError: sonnet-5: HTTP 403: {"error":{"message":"Key limit exceeded (total; sonnet-5#s3#0: RuntimeError: sonnet-5: HTTP 403: {"error":{"message":"Key limit exceeded (total; sonnet-5#s3#1: RuntimeError: sonnet-5: HTTP 403: {"error":{"message":"Key limit exceeded (total

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
| deepseek-v4-flash-low | 131 | 2227192 | 258020 | 0.22 | 1839 |
| laguna-s-2.1 | 129 | 2467260 | 189649 | 0.11 | 1760 |
| laguna-s-2.1_ms24 | 167 | 4485866 | 223375 | 0.13 | 2223 |
| sonnet-5 | 11 | 271514 | 89532 | 1.44 | 787 |
| gpt-5.4 | 3 | 41082 | 7687 | 0.16 | 88 |
| judge-opus5 | 28 | 144380 | 2680 | 0.79 | 103 |
| judge-gemini31 | 28 | 114779 | 7988 | 0.33 | 119 |
