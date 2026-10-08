# Atomic skills

A skill is **any folder holding a `SKILL.md`**, at any depth under `skills/` — the atomic,
model-rewritable layer between the fixed registry tools (`src/aiscientist/agents/registry.py`) and
the fixed preset pipelines (`preset_pipelines/`). See `docs/skills_and_pipelines_architecture.md` for
the full three-layer model.

## Adding a skill

Drop the folder in. That is all:

- **No code change and no restart.** The loader re-scans `skills/` before every step, so a folder
  added, edited or removed is seen by the next step of a running analysis.
- **Any depth.** `skills/<name>/` and a category layout such as
  `skills/single-cell/qc-snrna-with-cellqc-standalone/` both work; the category groups the skill in
  the list the agent reads.
- **Only `SKILL.md` is required.** A short skill can keep its code or shell commands inline in the
  body. A longer one bundles files next to it (`reference.py`, `scripts/stage.sh`, ...), which the
  agent fetches on demand.
- **Agent-Skills frontmatter works unchanged**, so a skill written for another catalog (for example
  `lijinbio/aiscientist-skills`) can be copied in as it is.

Folders whose name starts with `_` or `.` are skipped (drafts, a cloned repository's `.git/`). If two
folders use the same `name`, the first in path order is kept and the clash is logged; the repository
tests reject it. Override the library location with `$AISCIENTIST_SKILLS_DIR`.

A skill is instructions the agent follows, so it is reviewed like code before it is merged here.

## Layout (Anthropic Agent-Skills shape)

```
skills/[<category>/]<name>/
  SKILL.md        # required: frontmatter + body (guidance, and for a short skill the code itself)
  reference.py    # optional: an adaptable template the agent fetches, edits and runs
  scripts/...     # optional: any other bundled files, fetched by their relative path
```

`SKILL.md`:

```markdown
---
name: <name>                  # canonical id; defaults to the folder name
description: <what it does and when to use it>   # what the agent reads in the list to decide
# optional Agent-Skills fields, all read and kept:
license: MIT
compatibility: Requires bash and conda
metadata:
  category: single-cell       # else the folder the skill sits in
  version: "1.0.0"
  compute: "16 CPUs, 80 GB"
  requires-gpu: "false"
  # optional: the software environment the skill's commands run in (see below)
  image: "bioconda:cellqc=0.3.6 r-doubletfinder python=3.12"
  cpus: "16"
  mem_gb: "80"
  time_limit: "08:00:00"
---

## When to use
When this capability applies, and what curated tool (if any) it complements rather than duplicates.

## Run
The code or commands inline, or how to adapt the bundled file.
```

### A skill that needs its own software

A skill whose steps are command-line programs that no tool runs (an R or Snakemake pipeline, say)
declares the environment they need, with the same `image` value a `TOOL.md` uses:
`bioconda:<pkg>=<version> ...` (built from conda-forge + bioconda), or a pinned `docker://` image.
`cpus`, `mem_gb` and `time_limit` are optional. The keys go in `metadata:` (the Agent-Skills place
for custom fields) or at the top level. The skill list marks such a skill, and the agent runs its
commands with `run_in_environment(skill, command)`: a CPU Slurm job on HPC3 inside that image, with
the bound dataset read-only at `$AISCIENTIST_DATASET_ROOT` and a working directory that persists
across calls (`$AISCIENTIST_ENV_DIR`). The image is built once, on first use, for the whole lab:

- **Isolation:** every recipe is its own image, so two skills never share installed packages. A
  changed recipe (another version, an added pin) is a new image.
- **Reproducibility:** an image is never re-solved behind your back. Its lock file (exact packages
  and URLs) sits next to it as `<image>.sif.lock.txt`.
- **Shared downloads:** builds read the lab's shared conda download cache, so packages an earlier
  build downloaded are not downloaded again.

Pin what matters in the recipe. An unpinned solve takes the newest version: the published CellQC
BioContainer got Python 3.14 that way, on which its nuclear-fraction step fails. A declaration
that fails validation (a floating `:latest`, an unpinned first package) is ignored and the reason
is logged; the skill still loads as guidance.

A bundled `reference.py` is a **template to rewrite**, not code to run blindly. It reads checkpoints
from `$AISCIENTIST_WORK` and writes deliverables under `$AISCIENTIST_ARTIFACTS`. A skill that bundles
`reference.py` must have `## When to use` and `## Run` sections (`tests/test_skills_library.py`).

## How it reaches the Scientist — progressive disclosure

Loaded by `src/aiscientist/agents/skills.py` into `SKILLS` (keyed by name, no `.py`). A skill's body
and code enter the model's context ONLY when a step uses it:

1. **List** — every step brief lists EVERY skill as `- <name> — <description>`, grouped under
   `[<category>]`. There is no cap: the agent reads the whole list before it chooses.
2. `read_skill_reference(name)` — returns the `SKILL.md` body, its `metadata`, and the list of
   bundled files.
3. `read_skill_reference(name, file="reference.py")` — returns that file, to adapt and run.

Python runs through `run_code`; shell commands through `run_shell` when the session has an HPC3
shell, otherwise through `subprocess` inside `run_code`. `search_skills(query)` ranks the library by
keyword. Name lookups tolerate a legacy `.py` suffix.
