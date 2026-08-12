# Research-path skills

Each folder here is one **research path** the PI can be steered toward — the
operon-style (`swaruplab/operon`) skill pattern, ported to our PI → Scientist → Critic
loop. A skill is *guidance that steers the PI's planning*, not a hard agenda: the PI
still drafts the agenda adaptively, plan mode still lets the user edit it, and the
Scientist + Critic still execute and verify each step.

## Layout

```
skills/
  <name>/
    SKILL.md        # frontmatter (name + description + tools) + markdown body = PI guidance
    scripts/        # runnable reference code the Scientist adapts via run_code (CodeAct)
    references/     # (optional) supporting notes/specs
```

## SKILL.md format

```markdown
---
name: <kebab-or-snake slug, also the preset key>
description: <one-line label shown in the selector / used for PI routing>
tools: <comma-separated registered tools this protocol composes>
---

<the default PI-planning guidance — when to use this path, ordered steps that name the
 tools, grounding/honesty constraints. Reference a bundled script by file name where the
 tools don't cover a step.>
```

`src/bioagent/agents/presets.py` loads every `*/SKILL.md` here into the preset registry
(`name` → key, `description` → label, body → prompt, `tools:` → `ResearchPreset.tools`,
`scripts/*.py` → `ResearchPreset.scripts`). **Adding a research path = dropping a new
folder — no Python change.** Override the location with `BIOAGENT_SKILLS_DIR`.

## scripts/ — reference code (CodeAct templates), surfaced by progressive disclosure

Each `scripts/*.py` is a **vetted template the Scientist adapts via `run_code`**, NOT code
that runs automatically. Scripts stay in **separate files on purpose** — the Scientist's
per-step brief lists only a **manifest** (each script's `name` + a one-line summary), and the
full body is fetched **on demand** via the `read_skill_reference(name)` tool *only when a step
needs analysis the tools don't cover*. So a large template costs context exactly when it is
used, and "the template needs a local tweak first" is the normal path: fetch → adapt → run.

**Summary line (the manifest label).** The manifest shows the **first line of the script's
module docstring**, so start every `scripts/*.py` with a one-line `"""Reference template —
…"""` that says what it does. That single line is all the Scientist sees until it fetches the
body, so make it decision-useful.

Conventions a script may assume (set by the `run_code` sandbox): read the dataset from
`BIOAGENT_DATASET`, checkpoints from `BIOAGENT_WORK` (`adata_qc.h5ad` → `adata_clustered.h5ad`
→ `adata_de.h5ad`), and write figures/tables under `BIOAGENT_ARTIFACTS`. Scripts should
**call the registered tools' outputs**, never reimplement what a tool already does.

Because the body is never dumped into context, **script size is not a context cost** — a
template can be as long as it needs to be. (The old worry "don't let reference code grow" only
applied to the eager/inline scheme; progressive disclosure removes it.)

## Where `run_code` executes — local sandbox vs. HPC3 Slurm

By default a `run_code` snippet runs in the **local `CodeSandbox`** on the eyeserver: an isolated
subprocess with a wall-clock timeout, but **no hard memory cap**. A snippet that loads the full
AnnData and `.copy()`-s large subsets in a loop can exhaust host RAM and be OOM-killed
(`returncode == -9`). So a script must keep peak memory modest: subset with a **view** (not
`.copy()`), delete intermediates, and aggregate (pseudobulk) before heavy ops.

Set `BIOAGENT_RUN_CODE_ON_HPC=1` to instead submit each snippet as a **Singularity-contained CPU
batch job on HPC3** (`SlurmCodeExecutor`), where CPU/RAM are effectively unlimited and
`#SBATCH --mem` is a **real, cgroup-enforced cap** — an over-budget snippet fails cleanly as
`OUT_OF_MEMORY` instead of taking down the shared server. The dataset is bind-mounted **read-only**;
only the run's work/artifacts are writable; the network is off. If HPC is unavailable the executor
falls back to the local sandbox. Example sbatch request it generates (CPU analysis job):

```bash
#!/bin/bash
#SBATCH --job-name=bioagent_runcode_3
#SBATCH --partition=standard            # RCIC HPC3 free CPU partition (no --gres)
#SBATCH --account=ruic20_lab
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G                        # <-- the real, cgroup-enforced memory cap
#SBATCH --time=01:00:00
#SBATCH --output=$HOME/.bioagent/runcode/bioagent_runcode_3-%j.log

set -euo pipefail
module load singularity/3.11.3 2>/dev/null || true
singularity exec --containall --writable-tmpfs --net --network none \
  -B "$BIOAGENT_DATASET":"$BIOAGENT_DATASET":ro \
  -B "$BIOAGENT_WORK":"$BIOAGENT_WORK" -B "$BIOAGENT_ARTIFACTS":"$BIOAGENT_ARTIFACTS" \
  /dfs3b/ruic20_lab/software/AiScientist/containers/analysis.sif \
  bash -lc 'python $HOME/.bioagent/runcode/snippet_3.py > out 2> err; echo $? > rc'
```

Tune with `BIOAGENT_RUN_CODE_MEM_GB` (default 64), `BIOAGENT_CPU_PARTITION`, `BIOAGENT_CPU_ACCOUNT`,
`BIOAGENT_RUN_CODE_TIME_LIMIT`, `BIOAGENT_ANALYSIS_IMAGE`. A snippet reads the SAME
`BIOAGENT_DATASET` / `BIOAGENT_WORK` / `BIOAGENT_ARTIFACTS` env vars either way, so scripts are
identical across the two backends.

## Boundary vs. tools

A skill is the **workflow layer** (how/when to compose tools). It is NOT the place for
structured capabilities — `scgpt_annotate`, the biotools, etc. stay registered Python
tools so the vLLM tool-parser stays reliable. Skill bodies should *call* existing tools,
and only reach for custom `run_code` where no tool covers the step.

> Today the loader reads frontmatter + body + `scripts/*.py` (surfaced to the Scientist by
> progressive disclosure — manifest in the brief, body fetched via `read_skill_reference`).
> Deferred: `references/` supporting notes and description-based skill selection at scale.

## Which layer does my change belong in? (decision rule)

When you add functionality, default to the **highest** layer that can express it. Touch the
engine only when nothing above it can.

| You are adding… | Layer | Where | Engine edit? |
|---|---|---|---|
| A research **workflow / protocol** ("do X kind of study") | **skill** | `skills/<name>/SKILL.md` (+ `scripts/`) | none — drop a folder |
| A one-off computation a single protocol needs | **skill script** | that skill's `scripts/*.py`, run via `run_code` | none |
| A **deterministic capability** many protocols call | **tool** | register in `agents/registry.py` catalog | light (one entry; not the dispatcher) |
| An **expert persona / perspective** | **agent** | registry data | none |
| A mechanic **every run must have** (what context enters planning, the dispatch loop, checkpointing, a new event/UI type) | **engine** | `agents/research_lab.py` / `lab/kernel.py` / preflight | yes — rare |

**The test:** *is this one workflow's logic, or a property the whole kernel must have?*
One workflow → skill/tool (data-driven, no kernel edit). Whole kernel → engine.

**Why a skill cannot substitute for an engine fix.** A SKILL.md is a steering prompt read by
the PI planner. It cannot (a) change *what data reaches* the planner, (b) *add a capability*
to extract/compute something, or (c) enforce a property across *all* skills + free planning.
Those are code. Example (2026-06-30): "the planner must see the dataset's experimental design"
is a kernel property + a preflight capability → it lives in `.py` (`_dataset_context`,
`_obs_categoricals`), and it is what makes the `differential_expression` skill reachable on a
KO-vs-WT dataset in the first place. Engine work exists to make the **skill layer** stronger,
not to absorb features that belong in a skill.
