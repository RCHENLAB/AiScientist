# Testing plan edits — a protocol for a reviewer who is not a biologist

The scenario this covers: you open with a deliberately thin prompt (*"help me analyse this
dataset"*), let the PI draft a plan, and then push back on **one number in one step**. What you want
to know is whether the system changes what you asked for **and nothing else** — and whether the
number you approved is the number that actually runs.

Two things make this hard to test by hand, and both are handled by
`scripts/plan_edit_probe.py` so you never have to supply the biology:

1. **Which number is safe to ask about.** `plan_edit_probe.py targets <plan>` reads your plan, lists
   every setting it states, and for each prints a *defensible alternative value*, the two ways to
   ask for it, and the grading command to run afterwards.
2. **Whether anything else moved.** `plan_edit_probe.py grade <before> <after>` prints a verdict.
   For a plan document it uses `plan_doc.apply_edit` — the product's own change list — so the
   comparison is structural, not a guess at prose.

```bash
PYTHONPATH=$PWD/src .venv/bin/python scripts/plan_edit_probe.py targets before.yaml
```

The repo venv is needed for PyYAML, and `PYTHONPATH=$PWD/src` is needed in a worktree or python
imports the main checkout's code instead of this one's.

## The plan is a document, and its numbers are pinned

The plan is a YAML document: every step keeps its prose
(`does`) and gains `tool` + `settings` beside it, where `settings` are the tool arguments the plan
fixes ([`plan_doc.py`](../src/aiscientist/agents/plan_doc.py)). Two consequences for testing:

- **An approved setting is enforced on the tool call.** `pin_args` applies it over whatever the
  model sent, and records any disagreement
  ([`plan_doc.py:786`](../src/aiscientist/agents/plan_doc.py#L786),
  applied at [`research_harness.py:483`](../src/aiscientist/agents/research_harness.py#L483)).
- **The finished run is read back against the approved plan.** `check_adherence`
  ([`plan_doc.py:803`](../src/aiscientist/agents/plan_doc.py#L803)) gives every approved step one
  status — `as_planned`, `not_run`, `tool_not_called`, `tool_failed`, `settings_differ`,
  `not_accepted` — emitted as a `plan_adherence` event and written to the run's
  `process/plan_adherence.md` ([`app.py:10574`](../src/aiscientist/gateway/app.py#L10574)).

This is **on by default**: `AISCIENTIST_PLAN_YAML` has to be set to `0` to turn it off
([`app.py:6926`](../src/aiscientist/gateway/app.py#L6926)). It also applies to a plan nobody
reviewed — in bypass mode the plan is turned into a document and approved unreviewed, so its
numbers still bind ([`research_lab.py:4313`](../src/aiscientist/agents/research_lab.py#L4313)).

If your gateway has the flag off, the plan card is prose steps and none of the above applies. The
probe detects which format a file is and says so on its first line; grade both captures the same
way.

## First: "mid-plan" is four different things

They have different mechanisms and very different guarantees. Decide which one you mean before
reading a result as a pass or a fail.

| # | When | What it goes through | What is guaranteed |
|---|---|---|---|
| **S0 · hand edit** | plan card, before Approve | `POST /api/lab/plan {action: "edit", plan_yaml}` — you edit the YAML, **code** applies it, checked against the tool schemas ([`app.py:7727`](../src/aiscientist/gateway/app.py#L7727)) | **Strongest: no model is involved.** A failure here is a code bug, not sampling |
| **S1 · in words** | plan card, before Approve | `{action: "revise", feedback}` → the reference guard, then `_revise_plan_document`: the model returns *operations* on the document and code applies them ([`research_lab.py:6286`](../src/aiscientist/agents/research_lab.py#L6286)). Only if that cannot be done cleanly does it fall back to a single-step prose patch, then to a whole-plan redraft | **Strong.** A step no operation names is the same object afterwards — including when a step is added, removed or moved |
| **S2 · mid-run** | **after** Approve, while steps execute | `POST /api/chat/inject` → folded in at the next **step boundary** as standing prose guidance | **Weak, by design.** The plan does **not** change. Your note is prose the model may or may not act on, and finished steps are not revisited |
| **S3 · post-run** | after the report | follow-up → intent classifier → `rerun_step`, or `POST /api/lab/continue` | **Medium.** Re-runs the one step and everything downstream, reusing earlier checkpoints |

If you want *"change this number and re-do the affected work"*, use **S0 or S1 before approval**, or
**S3 after the run** — not S2.

## The rule that keeps the test honest

**Do not add a protective clause.** Type *"In step 3, use resolution 1.0 instead of 0.5."* — not
*"…and leave the other steps alone."* The second sentence tests whether coaching rescues the model;
real users don't write it, and the historical failure was measured without it. If a naked request
fails, *then* re-run it with the clause: the difference tells you whether the fix belongs in the
scaffolding or in the prompt.

## The test matrix

`probe` = `PYTHONPATH=$PWD/src .venv/bin/python scripts/plan_edit_probe.py`. Every row's verdict is
mechanical — the script prints `=== PASS ===` / `=== FAIL ===` and exits non-zero on a fail.

### S0 — the hand edit (start here)

| ID | What it stresses | What to do | Pass |
|---|---|---|---|
| **E1** | the deterministic path | in the YAML editor on the card, change one `settings` value (the `targets` output names one), Save | `probe grade before.yaml after.yaml --step 3 --param resolution --to 1.0` → PASS. **No model runs here, so this must pass every time**; a single failure is a code bug worth filing |
| **E2** | the schema check | set a setting to something its tool does not accept (e.g. a word where a number belongs) | the edit is refused with a reason, and the plan is unchanged — not silently accepted |

### S1 — asking in words (the core set)

| ID | What it stresses | What to send | Pass |
|---|---|---|---|
| **A1** | the ops path, step named | `In step 3, use resolution 1.0 instead of 0.5.` | `probe grade … --step 3 --param resolution --to 1.0` → PASS |
| **A2** | the ops path when you name the *topic*, not the step | `Use resolution 1.0 for the clustering.` | same command; the step it picked must be the clustering step |
| **A3** | downstream contradiction | edit a `padj` / `log2fc` that a **later** step restates (`targets` flags these with ⚠) | the plan changes cleanly **and** the feed warns that the later steps still hold the old value. The probe prints `EXPECT-WARNING … [4]` so you know one was owed |
| **A4** | the noise guard | `嗯` (or `hmm`) | `probe grade … --mode unchanged` → PASS. A bare interjection once moved a significance threshold with nothing on screen |
| **A5** | a question is not a change | `Why does step 3 use resolution 0.5?` | it answers, and `--mode unchanged` → PASS |
| **A6** | a step that isn't there | name a topic your plan has no step for: `Change the batch-correction step to use 30 neighbours.` | it says the plan has no such step. **Fail** = it invents one or edits something else |
| **A7** | ambiguous reference | a setting `targets` found in **two** steps, named without a step number: `Change padj to 0.01.` | it asks which step. **Fail** = it silently picks one |
| **A8** | add a step | `Add a literature-review step at the end.` | `probe grade … --mode structural` → PASS. The ops path handles this, so the other steps may be reworded but must not lose a setting, a tool, or themselves |
| **A9** | remove a step | `Drop the enrichment step.` | `--mode structural` → PASS, and the step is gone |
| **A10** | edits accumulate | run A1, then edit a *different* step | after the second edit the first is still there. Grade the second against the plan *after* A1 |
| **A11** | Chinese input | `第 3 步的 resolution 用 1.0，不要 0.5。` | same as A1 — and the plan comes back in **English** |
| **A12** | the fallback is visible | watch the feed on every A-row | when the ops path bails it emits `plan_revise_skipped` with a reason and drops to patch-then-redraft. Note which rows take the fallback: a row that only ever passes *via* the redraft is a weaker result than the same row passing on ops |

### S2 — mid-run

| ID | What to do | Pass |
|---|---|---|
| **B1** | approve, then while a *later* step is pending, inject `Use padj 0.01 for the remaining steps.` | the feed shows *"Incorporating your mid-run note"*. The plan does **not** change — correct here, not a bug. Then check `process/plan_adherence.md`: the later steps should read `settings_differ` against the approved plan, because the approved number is still what is pinned |
| **B2** | inject a change aimed at a step that **already finished**: `Redo the clustering with resolution 1.0.` | the honest outcome is that it says so, or re-runs it. **Finding** if it acknowledges and silently does nothing |

### S3 — post-run

| ID | What to do | Pass |
|---|---|---|
| **C1** | after the report, send `Re-run the clustering step with resolution 1.0.` | routes to `rerun_step`, re-runs that step and its downstream (not the whole study), and the report updates |

### D1 — did the number reach the tool?

Run this after any passing E1/A1. Approve the edited plan, let it run, then open the run's
**`process/plan_adherence.md`**.

> **Pass** — the edited step reads `as_planned`.
> **Finding** — it reads `settings_differ`: the model called the tool with a different value. The
> pin should have overridden that, so a `settings_differ` on a *pinned* setting means the pin did
> not hold, and that is worth filing with the run id.
> **Also check** — `tool_not_called` / `not_run` on the step you edited: the edit landed on a step
> that then never ran, which looks like success on the card and isn't.

Before the plan document existed, nothing bound a plan's number to a tool argument and this row had
no mechanism behind it at all — `plan_doc.py`'s own docstring names the failure ("resolution 1.0
approved, 0.5 run"). It now has one, which turns D1 from an unclosed gap into a check that the
mechanism holds.

## Reading the grader's output

For a plan document the change list is the product's own:

```
PASS  target_hit — step 3 actually changed
       step 3 (s3) setting padj: 0.05 → 0.01
PASS  intent_ok — step 3 now has padj=0.01, wanted 0.01
FAIL  no collateral — 2 change(s) to steps you did not name, +0 step(s) in length
  NOT ASKED FOR: step 2 (s2) setting resolution: 0.5 → 1.2

EXPECT-WARNING  downstream steps still at padj=0.05: [4]
```

- **`EXPECT-WARNING`** is computed independently of the product, so *"it didn't warn"* is
  distinguishable from *"there was nothing to warn about"*.
- **`⚠ FINDING (text_states_old_value)`** means a step's prose still states a number its own
  settings no longer hold. The product normally repairs that itself (it rewrites the number in the
  prose), so this only appears when the repair was not possible — a step that contradicts itself in
  front of the reviewer.
- On a **prose** plan the probe falls back to exact string equality on the steps you did not name,
  and also prints what `diff_plans` — the function that feeds the reviewer's own diff — makes of the
  same pair. `diff_plans` matches steps by token overlap, so a step reworded but still recognisable
  counts as unchanged there; when the strict view sees drift that it does not, the probe says so,
  because the collateral happened *and* the reviewer would never have been told.

## Repeat before believing anything

The planner is sampled, not deterministic. **One trial is an anecdote** — the original measurement
used 5 repetitions per request for exactly this reason. Run each row you care about 3–5 times from a
fresh plan; report `k/n`, not "it worked". A row that passes 4/5 is a real defect with a low rate.

E1 and E2 are the exception: no model runs in them, so one failure is one bug.

## What has already been measured (don't re-derive it)

- **Whole-plan redraft damaged unmentioned steps in 100% of trials** (mean 3.89 steps), while the
  same model asked for a single-step *patch* was 19/19 perfect — which is why the revision path
  never redrafts first (the A/B experiment `plan_revision_ab`, kept in the development repository).
- The guards A4/A6/A7 and the downstream warning A3 have unit coverage in
  `tests/test_plan_review_report_v25.py`; A5 in `tests/test_plan_mode_question.py`; the document
  paths in the `plan_doc` test files. Those tests build the shapes they expect — this protocol
  drives the real service, which is where the interesting failures have always been
  (`scripts/e2e_prod_drive.py --exercise-plan-mode` automates the A5 + A1 pair against a deployed
  gateway).
- The ops path and the pinning have had only a **rough timing check** on the served planner
  (PI draft 32–64 s, the binder +32–36 s per plan card, the reviser 9–56 s, 3/3
  operations parsed, no wrong bindings seen). That is not a measurement of the matrix above — it is
  the reason to run it.

So a **new** failure in A1/A4/A5 is a regression against something measured green. D1, E1/E2 and the
S2/S3 rows have never been run against a deployed gateway at all.
