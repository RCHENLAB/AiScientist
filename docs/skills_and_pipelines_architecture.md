# Tools, skills and pipelines

A run has three kinds of capability to draw on. They differ in who may change them and in how
much of the model's context they cost.

| Layer | What it is | Can the model rewrite it? | Where |
|---|---|---|---|
| **Tool** | Fixed, tested code the Scientist calls by name. Most tools are Slurm jobs on HPC3. | No | [`src/aiscientist/tools/<name>/`](../src/aiscientist/tools/README.md) |
| **Skill** | Guidance plus a code template that the Scientist reads, adapts to the dataset and runs. | Yes, that is its purpose | [`skills/`](../skills/README.md), and the induced-skill directory |
| **Pipeline** | A protocol for a whole study. It steers the PI's plan and names the tools it composes. | No | [`preset_pipelines/<name>/`](../preset_pipelines/README.md) |

Adding any of the three is adding a folder. No platform code changes.

Which layer a new capability belongs in: code that carries infrastructure (a Slurm job, a
checkpoint, a container, an external service) or that must behave the same in every run is a tool.
Analysis code that has to be adapted to each dataset is a skill. The order and the rules of a kind
of study are a pipeline.

## Tools

A tool is a folder with a `TOOL.md` and a `tool.py`. The front matter of `TOOL.md` is the tool's
manifest: its name, category, where it runs (`runs_on`), and optionally a container image.
`tools/catalog.py` discovers the folders, and `agents/registry.py` builds the Scientist's tool list
from them and routes each tool by its manifest: in the gateway process, or as a job on the
analysis, variant, phenotype, literature or scGPT line of HPC3. The index of tools is generated
from the manifests into [`src/aiscientist/tools/README.md`](../src/aiscientist/tools/README.md).

The platform adds its own tools to that list: `finish`, `run_code` (the sandbox every skill runs
in), the HPC3 shell tools when the session has a shell, `describe_environment`, and the tools that
read the other layers (`read_skill_reference`, `search_skills`, `read_tool_source`,
`run_in_environment`).

Every tool's name, description and argument schema is in the model's tool list on every step. That
cost grows with the number of tools, which is why adaptable analysis code is kept in skills.

## Skills

A skill is any folder that holds a `SKILL.md`, at any depth, in the Agent-Skills layout. Only
`SKILL.md` is required: its front matter gives `name` and `description`, and its body is the
guidance. A longer skill bundles files beside it, usually a `reference.py` template. The format
and the rules for writing one are in [`skills/README.md`](../skills/README.md).

`agents/skills.py` loads skills from two roots:

- the curated library: the pinned release of the
  [`aiscientist-skills`](https://github.com/lijinbio/aiscientist-skills) registry (see
  [The pinned registry release](#the-pinned-registry-release)). While that release is not
  installed, the library falls back to `skills/` in the repository (`AISCIENTIST_SKILLS_DIR`
  overrides it) and logs a warning;
- the induced-skill directory (`AISCIENTIST_INDUCED_SKILLS_DIR`), outside the repository, where
  the platform writes the skills it learns from runs.

The loader scans both again before every step, so a folder that is added, edited or removed takes
effect at the next step of a running analysis, without a restart. A curated skill always wins
over an induced skill of the same name.

### How a skill reaches the model

A skill's text enters the model's context only when a step uses it:

1. **The list.** Each step's brief lists every skill as `- <name> — <description>`, grouped by
   category. There is no cap on the list.
2. **The guidance.** `read_skill_reference(name)` returns the body of `SKILL.md` and the names of
   the bundled files.
3. **The code.** `read_skill_reference(name, file="reference.py")` returns one bundled file.

The Scientist adapts the template and runs it with `run_code`, or runs its shell commands with
`run_shell`. `search_skills(query)` ranks the library by keyword overlap with the name, the
description and the body.

A skill whose steps are command-line programs that no tool runs declares an `image:` in its
`SKILL.md`. The platform builds that image on HPC3 at first use, and the Scientist runs the skill's
commands inside it with `run_in_environment(skill, command)`.

### How a plan uses a skill

- **The PI sees the library when it plans.** Every draft carries the skill list after the tool
  list, with each description kept up to 1,024 characters. A step that needs a skill is planned as
  a `run_code` step that names the skill in backticks. The prompt tells the PI to prefer a tool
  whenever one covers the step, and to describe a skill's method only as its description states it.
  So a skill's description must name its method.
- **The Scientist is told which skill a step uses.** The brief of a step names the skills its text
  mentions, so the Scientist reads them instead of searching.

### The pinned registry release

`configs/skills_registry.json` pins a registry release tag, `v0.1.0`. A branch name such as `main`
is refused. The config also lists the tiers to load (`core`, `verified` and `community`; the skills
migrated from `skills/` are `community` in `v0.1.0`, so without that tier an installed release
would load only CellQC), the releases root, and whether the fallback to `skills/` is allowed.
Each key has
an `AISCIENTIST_SKILLS_REGISTRY_<KEY>` override. Updating skills means changing the tag and
deploying the release.

`agents/skills_registry.py` reads `skills/releases/<version>/`, which holds the release's three
assets as published: `aiscientist-skills-<version>.zip` (every skill folder at its catalog `path`),
`catalog.json` and `SHA256SUMS`. Releases with one `<name>-<version>.zip` per skill are still read.
The folder is installed on the server, outside git (`.gitignore`, and `scripts/sync_deploy.sh`
never ships or deletes it):

```bash
gh release download v0.1.0 -R lijinbio/aiscientist-skills -D skills/releases/0.1.0
(cd skills/releases/0.1.0 && sha256sum -c SHA256SUMS)   # leave the zip packed
```

The loader checks `SHA256SUMS` against `catalog.json`, the bundle or every skill zip, and every
unpacked file, and refuses the release if any is missing or differs. A refused release gives no
curated skills at all, logged as an error; it never falls back to `skills/`, and the rest of the
platform keeps working. The skill list comes from the catalog: name, description, tags, version,
tier and requirements. `requirements.compute` is also exposed as `metadata["compute"]` for plan
estimates.

Registry names use hyphens (`compare-two-groups`). A skill also answers to the underscore form of
its name and to any names in `metadata.aliases`, so plans, preset pipelines and older runs that
name a skill migrated from `skills/` (`pairwise_de`) still find it; an exact name always wins.
In the registry layout the code sits in `scripts/`: `read_skill_reference` offers
`reference.py`, else `scripts/reference.py`, else the first file in `scripts/`.

A registry skill gets only what it declares. When it is read with `read_skill_reference`, or named
in `run_code(skill=...)` or `run_in_environment`, the code runs in a container with GPU, secrets and
tools limited to its declaration. Network follows `requires-network`: `install` and `runtime` run
with the network every other snippet has (a skill's own steps install its software, as the
registry template's `mamba create` does), `none` runs offline and may only use an image that is
already built. `AISCIENTIST_SANDBOX_NETWORK=0` turns the network off for all of them. Its `scheduler` says whether the skill needs
Slurm: the platform runs every snippet as a contained Slurm job whatever it says, and code inside
that container cannot submit jobs of its own, so no skill gets scheduler access. The local sandbox
refuses a skill that requires a scheduler or a GPU. Uncontained local execution, the
Slurm-to-host fallback, the dependency preflight and `run_shell` are refused for it. Each secret it
names is read per call from the server's `AISCIENTIST_SKILL_SECRET_<NAME>`, passed to the job in a
private temporary file, and redacted from the output the model sees.

Every run's `manifest.json` lists the skills it used, with name, version and release tag.
Successes and errors are counted per release, skill and version in one file for the deployment,
`<AISCIENTIST_STATE_DIR>/skill_versions.sqlite3` (`AISCIENTIST_SKILLS_REGISTRY_STATS` overrides
it), so a broken version can be found and the pin rolled back. A run the user stops is not
counted.

### Skills learned from runs

With `AISCIENTIST_SKILL_INDUCTION=1` and `AISCIENTIST_INDUCED_SKILLS_DIR` set, a run ends with an
induction step (`agents/skill_induction.py`):

1. The candidates are the successful `run_code` calls of the steps the Critic accepted.
2. A model decides for each whether it is a reusable procedure, and if so generalizes it: the
   dataset's paths, columns and thresholds move into a config block at the top. The prompt's
   default answer is no.
3. Checks that do not depend on the model follow: a plain snake_case name, code that compiles and
   is between 120 and 12,000 characters, no clash with an existing name. At most two skills are
   kept per run.
4. The skill is written to the induced-skill directory as `<name>/SKILL.md` and `reference.py`,
   never into the repository and never over an existing folder. An improved version of an existing
   skill is written beside it as `<name>_v2`, and lists show only the newest version; the older
   one can still be read by name.

**An induced skill is reviewed before any model sees it.** It is one run's code turned into a
template, and it can carry that dataset's file format and column names. Everything in the
induced-skill directory starts as `pending`. An admin approves or retires it in the console's
*Admin → Skill review* tab, which shows its `SKILL.md`, its code and the step it came from. Only
approved skills are listed for planning, for the Scientist and in search. The decisions are kept
in `_review.json` in that directory and take effect at the next step of every run. A pending skill
still reserves its name, so the same procedure is not learned twice.

## Pipelines

A pipeline is a folder with a `SKILL.md` whose front matter gives `name`, `description`, the
`tools` it composes and the `data_type` it is for (`scrna` or `variants`), and whose body is the
guidance the PI plans from. A `PROTOCOL.md` beside it is the same protocol written for a
researcher to read and audit; the platform does not load it. `agents/preset_pipelines.py` loads
the folders when the gateway starts.

A pipeline steers the plan and does not replace it. The PI still drafts the steps for the actual
dataset and question, the user reviews and edits the plan, and the Scientist and the Critic run
and check every step.

How a run gets its pipeline:

- **By the dataset.** At the start of a run the platform reads what the dataset is. If one
  pipeline matches that data type it is chosen directly; if several match, a model picks among
  them from the question and the dataset profile; if the data type is unknown, the model picks
  from the whole library or answers that none fits, and the PI then plans from the tool and skill
  lists alone.
- **No automatic choice** when the request limits the run to named analyses ("only QC"): a
  protocol lays out a whole study, which the user has just declined.

When a pipeline is loaded, the console shows the tools it composes and marks those this deployment
cannot run.
