# Experiment: operon-style, human-auditable protocol format

**Question (from Yijun's advisor).** Our `preset_pipelines/*/SKILL.md` carry *no* code, so a
bioinformatics researcher (non-CS) can't review what each step actually does. The advisor wants an
operon-style protocol: summary → collapsible detail per step, with the key command/params inlined so a
researcher can audit "did you do each step right?". **Can we make the pipeline machine-reproducible AND
researcher-readable at the same time?**

**This is a throwaway prototype — NOT wired in.** It lives outside `preset_pipelines/`, so the preset
loader (`preset_pipelines.py` globs `preset_pipelines/*/SKILL.md`) never picks it up. The stable
`variant_annotation` pipeline is untouched.

## What's here

| File | What |
|---|---|
| `variant_annotation/PROTOCOL.md` | the NEW operon-style body — summary table → per-step `<details>` (what / why / 🔬agent-chosen params / the real command / ✅verify checklist). Commands pulled faithfully from the real source (`build_norm_cmd`, `build_filter_cmd`, `build_vep_cmd`, `_VEP_ANNOT_FLAGS`, `_is_damaging`). Minimal jumping — code inlined, one source footnote per block. |
| `ab_test.py` | OpenRouter A/B harness: feeds the OLD `SKILL.md` body vs the NEW `PROTOCOL.md` body to the SAME planner prompt on a rare-disease/GRCh37 study, scores each plan **deterministically** (7-phase coverage, param correctness, anti-patterns) and optionally via an **LLM judge** (+ a readability rating of each body). |
| `results/` | raw plans + scores written by the harness. |

## Design principle (how "reproducible" and "readable" coexist)

The readable doc must be a **projection of the code that actually runs**, never a hand-forked copy —
otherwise a researcher audits a stale snippet, which is worse than none. So the intended production
mechanism is: mark the key builder functions in source, generate `PROTOCOL.md` from them, and add a CI
staleness check. This prototype hand-writes the projection to preview the format; the generator is the
follow-up if the format proves out.

Most steps ultimately call an existing **registered tool** (`annotate_variants`), not bespoke code — so
the protocol documents *the tool call the agent emits* (the scientific knobs the researcher audits) plus
*the fixed command the tool runs internally* (shown once), which is also what minimises jumping.

## Run the A/B test

```bash
export OPENROUTER_API_KEY=sk-or-...          # your key; never committed
python experiments/protocol_format/ab_test.py --trials 3            # deterministic only (cheap)
python experiments/protocol_format/ab_test.py --trials 5 --judge \
    --judge-model anthropic/claude-3.5-sonnet                       # + LLM judge & readability
```

Deterministic score is out of 11 (coverage 7 + params 3 + order 1, minus anti-patterns). The point is
**not** "NEW must beat OLD on planning" — it's "NEW is far more human-auditable AT NO planning cost"
(same-or-better plan, for ~36% more prompt tokens). The harness quantifies both sides of that trade.
