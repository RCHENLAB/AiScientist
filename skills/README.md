# Atomic skills

Each skill is a **folder** `skills/<name>/` — the atomic, model-rewritable layer between the fixed
registry tools (`src/bioagent/agents/registry.py`) and the fixed preset pipelines
(`preset_pipelines/`). See `docs/skills_and_pipelines_architecture.md` for the full three-layer model.

## Layout (Anthropic Agent-Skills shape)

```
skills/<name>/
  SKILL.md      # frontmatter (name + one-line description) + guidance body
  reference.py  # the adaptable CodeAct demonstration the Scientist fetches, edits, and runs
```

`SKILL.md`:

```markdown
---
name: <name>                     # canonical id (no ".py"); defaults to the folder name
description: <one line>          # the MANIFEST label the Scientist sees to decide relevance
---

## When to use
When this capability applies, and what curated tool (if any) it complements rather than duplicates.

## Details & adaptation
What it does; which CONFIG / marker / threshold values to adapt to the dataset.

## Run
How to fetch and run it (see below).
```

`reference.py` is a **template to rewrite**, not code to run blindly. It reads checkpoints from
`$BIOAGENT_WORK` and writes deliverables under `$BIOAGENT_ARTIFACTS`.

## How it reaches the Scientist — three-level progressive disclosure

Loaded by `src/bioagent/agents/skills.py` into `SKILLS` (keyed by name, no `.py`). A skill's guidance
and code enter the model's context ONLY when a step uses it:

1. **Manifest** — the step brief lists `- <name> — <description>` for every skill (bodies withheld).
2. `read_skill_reference(name)` — returns the `SKILL.md` guidance + the bundled-file list, no code.
3. `read_skill_reference(name, file="reference.py")` — returns that file's code, to adapt and run
   via `run_code`.

Above `BIOAGENT_SKILL_MANIFEST_MAX` skills (default 12), the brief stops listing the manifest and
tells the agent to `search_skills(query)` first. Name lookups tolerate a legacy `.py` suffix.

## Adding a skill

Drop a new `skills/<name>/` with a `SKILL.md` + `reference.py`. No code change needed — the loader
picks it up. Override the library location with `$BIOAGENT_SKILLS_DIR`.
