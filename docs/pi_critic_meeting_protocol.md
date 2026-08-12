# PI↔Critic Step-Meeting Protocol — Design

Branch: `elastic-chatelet-6c5d2b`. Status: **v1 shipped (linear planner) + partial (DAG)**. Off by
default (`LabConfig.step_meetings`), opt-in until proven — same posture as `planner="dag"` etc.

## Why

The per-step Critic (`_critic`) judges each step *in isolation*: "was THIS step's goal met, grounded
in evidence?" It structurally never sees the whole plan, so it cannot catch the failure class Yijun
flagged on bundle `7e551b8db499` (retina demo):

- a **meaningless step** — de-novo clustering that produces 29 orphan clusters nothing downstream
  consumes, and pathway enrichment run on a known cell type's own identity markers (circular, no
  experimental contrast);
- **redundant re-execution** — QC/clustering/DE re-run every round with inconsistent parameters.

These are *plan-level* judgments. Making one omniscient "global Critic" would just create another
rubber-stamp seat. The right shape (per Yijun) is a **two-way PI↔Critic dialogue** that reviews each
step **in the context of the whole plan**, both *before* and *after* it runs. The PI owns the plan and
has the final call; the Critic is the skeptic. Deterministic guards remain the non-negotiable floor,
because Qwen3.6 provably ignores prompt-only steering (`scripts/no_contrast_enrichment_openrouter.py`).

## The protocol

```
  PI drafts agenda
     │
     ▼
  ⓪ PLAN-TIME REVIEW  (once, before ANY step runs)
     Critic reviews the WHOLE agenda → PI finalizes
     → drop orphan/circular steps, add a reconciliation step, or keep as-is
     │
     ▼   per step:
     ┌─────────────────────────────────────────────────────────────────────┐
     │ ① PRE-FLIGHT GATE  (before the Scientist runs)                        │
     │    deterministic floor → Critic challenge → PI adjudicates (if any)   │
     │    → proceed | amend (fold into brief) | skip (drop from agenda)      │
     ├─────────────────────────────────────────────────────────────────────┤
     │    Scientist runs tools → _critic accepts/revises  (unchanged)        │
     ├─────────────────────────────────────────────────────────────────────┤
     │ ② POST-STEP REVIEW  (after the Critic accepts)                        │
     │    PI: did this change the picture? is any REMAINING step now moot?   │
     │    → prune the moot remaining steps                                   │
     └─────────────────────────────────────────────────────────────────────┘
```

### ⓪ Plan-time review — `_plan_review()`

The single planning pass (`_pi_plan`) is **one-shot with no second look** — the drafted agenda goes
straight to execution (or a human plan-approval card). That is why the retina plan shipped incoherent:
QC → *de-novo cluster to "map known classes"* → *DE by provided labels* → *enrichment* — the clustering
branch produces 29 anonymous clusters no later step reconciles with the labels, and the enrichment is
circular. Every one of those facts was visible at plan time; nothing looked again.

`_plan_review` is that second look, at the plan's **source**. The Critic (`_PLAN_REVIEW_CRITIC_SYSTEM`)
judges the whole agenda — necessity (orphan branch?), coherence (re-cluster de-novo *and* use labels but
never reconcile?), preconditions for THIS dataset (enrichment/discovery-DE without a contrast; a
per-cluster claim without label reconciliation) — and proposes a `revised_agenda`. The PI
(`_PLAN_REVIEW_PI_SYSTEM`) owns the plan and returns the `final_agenda`. **Never worse than the draft:**
an empty / oversized / unparseable revision falls back to the original (mirrors the DAG structurer's
guarantee). Runs in `run()` after the agenda is finalized and **only when no human is curating the plan**
(`plan_review is None` — a human reviewer's decision wins); the deterministic no-contrast strip then
runs *after* it, as the final floor. Emits `plan_review{before, after, issues}`.

This is the cheapest and most on-point catch for a plan that was wrong from step 0: **one** review pass
restructures the whole plan before any compute, where the per-step gate would only prune the bad steps
one at a time after paying to reach them.

### ① Pre-flight gate — `_preflight_gate()`

Runs before `_scientist`. Three layers, cheapest first:

1. **Deterministic floor (no model).** Enrichment on an annotated, no-contrast dataset →
   `skip`, `by="guard"`. Same rule as the plan-time prune (`_annotated_without_contrast` +
   `_is_enrichment_step`), applied here as belt-and-suspenders. Never asks an LLM.
2. **Critic challenge** (`_PREFLIGHT_GATE_SYSTEM`). Given the whole plan (steps tagged
   done/current/remaining/pruned), the accepted findings so far (with artifacts), and the dataset
   profile, the Critic judges the current step on four axes — **necessity** (does anything consume its
   output?), **redundancy** (already covered by an accepted step / checkpoint?), **precondition**
   (enrichment/discovery-DE needs a real 2+-condition contrast; per-cluster claims need clusters
   reconciled with provided labels), **altitude**. Returns `proceed | amend | skip`.
3. **PI adjudication** (`_PREFLIGHT_PI_SYSTEM`) — **only if the Critic objected** (cost: 0 extra calls
   when uncontested). The PI owns the plan and can uphold, soften to `amend`, or overrule to `proceed`.

Enactment: `skip` → the step leaves the **effective agenda** (`pruned` set; convergence is measured on
`len(agenda) - len(pruned)`), emits `steps_pruned{reason:"preflight"}`. `amend` → the amendment is
folded into the Scientist's brief as `[Plan review — adjust how you run THIS step]: …`.

### ② Post-step review — `_poststep_review()`

Runs after `_critic` **accepts** a step (a revised/force-advanced step is inconclusive, so it's not
reviewed). Given what the step produced + the remaining steps, the PI (`_POSTSTEP_PI_SYSTEM`) reports a
`contribution` (`new | confirmed | nothing`) and a **conservative** `prune` list of remaining steps the
step made moot (already answered, or only justified by a branch that didn't pan out). The prune list is
intersected with the actual remaining step texts before enactment; each pruned step emits
`steps_pruned{reason:"poststep_review"}`. This is where "meaningless in hindsight" gets caught even if
the pre-flight gate passed.

## Code seams

⓪ Plan-time review lives in `run()` **before the linear/DAG dispatch**, so it is path-agnostic — it
revises the flat agenda both paths then execute. ① / ② wrap the `_scientist` → `_critic` seam both
paths share:

| Meeting | linear `_run_loop` (1.0 prod path, retina bundle's path) | DAG (0.2.0, opt-in) |
|---|---|---|
| ⓪ plan-time review | ✅ revises agenda pre-dispatch | ✅ (same, pre-dispatch) |
| ① pre-flight `amend` | ✅ | ✅ |
| ① pre-flight `skip` | ✅ (agenda prune + convergence) | recommendation only¹ |
| ② post-step prune | ✅ | emit-only¹ |

¹ Dropping a DAG node with dependents, or pruning downstream nodes, needs the scheduler's
dependency-aware replan (cycle check + `max_replans`). Enacting that from an LLM verdict without it
risks orphaning a checkpoint a later node reads. So on the DAG path v1 enacts ⓪ + ① `amend` + the
deterministic floor; per-step model `skip`/downstream-prune stay linear-only until we can test the graph
mutation on eyeserver. (The highest-value case — enrichment-without-contrast — is already pruned at
plan time for *both* paths by the deterministic strip.)

New symbols in `agents/research_lab.py`: prompts `_PLAN_REVIEW_CRITIC_SYSTEM` / `_PLAN_REVIEW_PI_SYSTEM`
/ `_PREFLIGHT_GATE_SYSTEM` / `_PREFLIGHT_PI_SYSTEM` / `_POSTSTEP_PI_SYSTEM`; `PreflightDecision`; methods
`_plan_state` / `_plan_review` / `_preflight_gate` / `_poststep_review`; config `LabConfig.step_meetings`
(one switch for all three meetings). Events: `plan_review`, `preflight`, `poststep_review`, and the
existing `steps_pruned` (now also `reason ∈ {preflight, poststep_review}`); `lab_done` gains `pruned`.

## Cost

Off → zero extra calls (every helper returns early on `not step_meetings`). On → **once at plan time**:
1 call (plan-review Critic) + 1 more only if it flags issues (PI finalize). **Per step**: 1 (pre-flight
Critic gate) + 1 more only when it objects (PI adjudication) + 1 per **accepted** step (post-step
review). No new calls on revisions. Reuses the single-shot `_complete` (same vLLM/A100 batching). The
plan-time review is the cheapest lever — one pass fixes a wrong-from-step-0 plan before any compute.

## Tests

`tests/test_step_meetings.py` (offline, no scanpy): off-by-default issues no meetings; a plan-time
review revises an incoherent draft (orphan de-novo clustering + circular enrichment) into a leaner plan
before any step runs; the deterministic floor skips enrichment without asking a model; a pre-flight
`skip` prunes a step and the run still converges on the rest; a post-step review prunes a now-moot
downstream step; an `amend` reaches the Scientist's brief. All 6 pass in-process; 96 existing no-fixture
lab/DAG tests stay green (102 total).

## Follow-ups

- Wire an env override (`BIOAGENT_STEP_MEETINGS`) alongside the other `BIOAGENT_*` config knobs.
- DAG: enact `skip`/downstream-prune through the scheduler's replan (cycle-safe) — needs eyeserver test.
- Consider surfacing pre-flight `skip`/`amend` as an optional HITL card (reuse the decision-card path)
  for high-consequence steps, instead of the PI auto-adjudicating.
- Measure: with the protocol on, A/B Qwen3.6 vs a stronger orchestrator in the PI/Critic seats — now
  that a model is actually *given the whole-plan view and asked the necessity question*, the model's
  reasoning strength finally has somewhere to pay off.
