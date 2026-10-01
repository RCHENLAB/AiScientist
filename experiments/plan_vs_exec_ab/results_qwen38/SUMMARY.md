# Plan vs execution vs writing — same scaffolding, different models

Question: What changes between DDX41 mutant and WT retina?
Fixed plan for stage B: production run 8847d521ba32 (7 steps).


## A · Planning (real `_pi_plan`, current prompt + dataset profile incl. design_by_arm)

| arm | n | steps | rubric | halluc. tools | depth-aware | replication-aware | judge: sound | specific | dataset-fid | depth✓ (judge) | seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen36-awq-local | 8 | 9.5 | 0.98 | 0 | 100% | 100% | 8.81 | 9 | 9.31 | 100% | 71.85 |
| qwen38-int4-local-low | 8 | 10.25 | 1.0 | 0 | 100% | 100% | 9.12 | 9.38 | 9.5 | 100% | 146.53 |
| qwen38-int4-local-medium | 8 | 11.75 | 0.97 | 0 | 100% | 100% | 9.06 | 9.38 | 9.56 | 100% | 178.8 |

## B · Execution of the SAME 7-step plan (real `_scientist` + real local tools + real `_critic`)

| arm | trials | named tool called | named tool ok | arg match | run_code/step | re-implemented tool | recon calls/step | hit max_steps | finished w/ answer | critic accept | judge: did step | grounded | not wasted | prompt tok/step |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

Per step (named tool ok % / mean run_code):

| arm | s1 run_scanpy_qc | s2 run_de | s3 run_composition | s4 run_clustering | s5 run_enrichment | s6 run_gsea_prerank | s7 run_code |
|---|---|---|---|---|---|---|---|
| qwen36-awq-local | — | — | — | — | — | — | — |
| qwen38-int4-local-low | — | — | — | — | — | — | — |
| qwen38-int4-local-low-audit | — | — | — | — | — | — | — |
| qwen38-int4-local-low-audit2 | — | — | — | — | — | — | — |
| qwen38-int4-local-medium | — | — | — | — | — | — | — |

## C · Writing the SAME accepted findings (real `_synthesize`)

| arm | prompt | n | chars | sci-notation % | 'significant' claims | multi-donor claim | depth mentioned | technical caveat | judge: raises artefact | consistent replication | overclaims | invents | quality |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen36-awq-local | prefix | 2 | 17140 | 0 | 0.5 | 0 | 0 | 0% | 0% | 100% | 25% | 25% | 5.5 |
| qwen36-awq-local | current | 2 | 18928.5 | 0 | 0.5 | 0 | 1.5 | 100% | 100% | 100% | 25% | 25% | 7.25 |
| qwen38-int4-local-low | prefix | 2 | 22567 | 0 | 0.5 | 0 | 0 | 50% | 0% | 100% | 25% | 0% | 6.25 |
| qwen38-int4-local-low | current | 3 | 26975.67 | 0 | 2 | 0 | 2.33 | 67% | 67% | 100% | 83% | 50% | 6 |
| qwen38-int4-local-low-audit | current | 3 | 25873.67 | 0 | 0.33 | 0 | 6 | 100% | 100% | 100% | 17% | 83% | 7 |
| qwen38-int4-local-low-audit2 | current | 3 | 25788 | 0 | 0.67 | 0 | 7.67 | 100% | 100% | 100% | 33% | 0% | 8.67 |

## Tokens / cost / latency (this experiment, per arm)

| arm | calls | prompt tok | completion tok | USD | seconds |
|---|---|---|---|---|---|
| qwen36-awq-local | 12 | 0 | 0 | 0.00 | 738 |
| qwen38-int4-local-low | 3 | 0 | 0 | 0.00 | 376 |
| qwen38-int4-local-medium | 8 | 0 | 0 | 0.00 | 1430 |
| judge-opus5 | 3 | 31203 | 1155 | 0.18 | 25 |
| judge-gemini31 | 3 | 21212 | 2914 | 0.08 | 27 |
| qwen38-int4-local-low-audit | 108 | 0 | 0 | 0.00 | 3012 |
| qwen38-int4-local-low-audit2 | 108 | 0 | 0 | 0.00 | 2705 |
