# Plan-revision fidelity — can Qwen3.6-35B-A3B make a *small* edit?

## The question

In plan mode the user does not text-edit the agenda. They type natural-language feedback, and
`ResearchLab._pi_plan(feedback=…, prior_agenda=…)` **re-drafts the whole plan**. The only thing
protecting the steps they did not mention is one sentence of prompt — *"keep what they did not
object to"*. Qwen3.6-35B-A3B is a MoE with ~3B active parameters, and "change one line, copy the
rest verbatim" is the ability that degrades first at low active-parameter counts.

The failure this would produce is quiet: the researcher asks for `resolution 1.0`, gets a plan where
step 2 changed *and* step 5 was silently reworded or dropped, and the UI shows no diff.

## Why the arms are what they are

Not "small model vs big model". Swapping the model is expensive — a 194 GB model needs a whole GPU
node — and it is the wrong purchase if the fault is the scaffolding rather than the weights.

| arm | what it is |
|---|---|
| **A · redraft** | the current production path, calling the real `_pi_plan`; whole agenda regenerated |
| **B · patch** | same model, same plan; the model returns only `{"step": n, "new_text": …}` and **code** applies it — untouched steps cannot change because nothing regenerates them |

If **B ≫ A**, the weakness is the scaffolding and a bigger model buys nothing here.
If **A is already clean**, this worry is closed and no change is needed.

## Metric

Per trial, over `--reps` repetitions (the model is stochastic, so a single run is an anecdote):

- `target_hit` — the step the user asked about actually changed
- `intent_ok` — the changed step contains what was asked for
- `collateral` — **how many OTHER steps changed.** This is the number that matters.
- `count_delta` — steps added or removed
- `clean` — all of the above correct: hit, intent satisfied, zero collateral, same length

The scorer is self-checked against synthetic perfect / collateral / dropped-step / no-op plans
before any model result is trusted, so a red number is the model's, not the metric's.

## Running it

Needs a local port forwarded to a vLLM `/v1` endpoint serving the model. On HPC3:

```bash
sbatch abprobe.sbatch                      # a serve job on free-gpu32
ssh -N -L 42777:<compute-node>:42777 hpc3  # tunnel
python experiments/plan_revision_ab/run_ab.py --port 42777 --reps 5
```

It uses `gateway/vllm_client.complete` — the same client production uses — so the calls match.

Raw per-trial records land in `results.json` next to the script.

## Result, and the fix it produced

Measured on the real served Qwen3.6, 4 requests × 5 reps.

| | clean | any collateral | mean collateral | plan length changed | intent honoured |
|---|---|---|---|---|---|
| **redraft — before the fix** (n=19) | 0% | 100% | 3.89 | 89% | 53% |
| **patch** (n=19) | 100% | 0% | 0.00 | 0% | 100% |
| **redraft — after the fix** (n=20) | 100% | 0% | 0.00 | 0% | 100% |

The worst observed redraft: asking only for *"use resolution 1.0 instead of 0.5"* returned a plan
where the differential-expression step had become a "descriptive summary", enrichment had become a
literature search, and the last step was gone — 6 steps to 5, with no diff shown to the researcher.

`_pi_plan` now patches a single step and falls back to the redraft only for requests a one-step
edit cannot express, so the third row is the production path measured through the same harness.
`results.json` is the before run, `results_after_fix.json` the after.

**The finding that decided a spending question:** the same weights are perfect under the patch
framing, so a larger model buys nothing for plan editing. Note also that `intent_ok` rose 53% → 100%
— the patch framing did not merely stop the collateral damage, it made the model more likely to do
what was actually asked, presumably because "rewrite this one step" is a far easier instruction than
"redraft everything and remember to preserve the rest".
