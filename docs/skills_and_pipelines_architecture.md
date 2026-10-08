# Skills / Preset-pipelines / Registry — architecture

Status: **BUILT** (2026-07-07). The three layers exist and are wired end-to-end (full suite green).
What remains is additive, not structural: **skill induction** (grow the library from successful
runs), a **`search_skills(query)`** retrieval step for when the library gets large, and making the
console Advanced multi-select offer composable **atomic skills** (today it still offers
preset-pipelines). Owner: Yijun + Claude.

## The three layers

| Layer | What it is | Rewritable by the model? | Lives in |
|---|---|---|---|
| **Registry (fixed core)** | A SMALL set of verified, infra-backed primitives, always available in the Scientist's tool list. | No — fixed. | `agents/registry.py` + `tools/*.py` |
| **Skills (atomic, adaptable)** | A large, growable library of atomic capabilities the agent picks, composes, and **rewrites via `run_code`**. Surfaced ON DEMAND (manifest → fetch), not dumped into the tool list. | Yes — the point. | `skills/<name>/` |
| **Preset-pipelines** | Fixed, reproducible, prompt-driven full research workflows that compose skills + registry tools. Secondary ("额外的东西"). | No — fixed guidance. | `preset_pipelines/<name>/SKILL.md` |

## Why the registry still earns its place

The registry is NOT "just some tools" — its members carry infrastructure the model must not
casually rewrite:

- `run_scanpy_qc` / `run_clustering` / `run_de` / `run_enrichment` — routed to **HPC3 Slurm jobs**
  with **checkpointing** (`_route_analysis`, `_HPC_ANALYSIS_TOOLS`). Rewriting them loses the
  offload + resume.
- `run_code` — the **CodeAct engine** every skill runs on.
- `finish` — control primitive.
- (Candidate) `annotate_variants` (VEP REST), `scgpt_annotate` (GPU job), `literature_search`
  (Europe PMC) — thin wrappers over external services / GPU infra; stable, keep in the registry
  unless we want them rewritable.

Rule of thumb: **infra-backed / verified / control → registry; adaptable analysis code → skill.**

## Why this saves context — and when it does NOT

Today `build_scientist_catalog` puts EVERY registry tool's name + description + JSON schema into the
LLM tool list on EVERY step. That cost grows with the tool count.

The saving comes from **progressive disclosure**, not the folder split:
- Registry stays ~8–10 tools → always in the tool list, cheap.
- Skills are surfaced as a SHORT manifest (name + one-line summary) and the full body is fetched +
  adapted only when a step uses it (`read_skill_reference` + `run_code`) — exactly today's
  `scripts/*.py` mechanism, generalized.

⚠️ **If skills are instead injected as always-on tools, there is NO saving.** Progressive disclosure
is the load-bearing mechanism; the folder split is just its organization.

⚠️ **As the skill library grows, the manifest itself bloats.** Reserve a place for skill
**discovery/retrieval** (`search_skills(query)` or a retrieval step) so the agent finds the right
skill by description instead of reading the whole catalog. Not needed on day one; the schema must
allow it.

## Skill = atomic, adaptable code capability  (as built)

An atomic skill (Yijun's confirmed definition) = a capability that sits ABOVE the fixed `run_*`
primitives and that the model can rewrite/adapt via `run_code`. **As built (2026-07-08)**, each skill
is a **folder** `skills/<name>/` in the Anthropic Agent-Skills shape — description and demonstration
are separate files:
- `SKILL.md` — frontmatter (`name` + one-line `description`) and a markdown body
  (`## When to use` / `## Details & adaptation` / `## Run`). The `description` is the manifest label;
  the body is the human-readable **when-to-use + how-to-adapt** guidance.
- `reference.py` (and any other bundled file) — the adaptable CodeAct **demonstration** the Scientist
  fetches, adapts to the dataset, and runs via `run_code` — a TEMPLATE to rewrite, not code to run
  blindly.
- Loaded by `agents/skills.py` into `SKILLS` (keyed by folder/frontmatter name, no `.py`); override
  the dir with `$AISCIENTIST_SKILLS_DIR`. Grown by **induction** (deferred).
- **Since 2026-10-01, a skill is dropped in, not wired in:** any folder holding a `SKILL.md`, at any
  depth (`skills/<category>/<name>/` works), is a skill; `SKILL.md` is the only required file (a short
  skill keeps its code or shell commands inline); bundled files may sit in subfolders; Agent-Skills
  frontmatter (`license`, `compatibility`, a nested `metadata:` block) is read as-is; and
  `refresh_skills()` re-scans before every step, so no restart is needed. See `skills/README.md`.

**Three-level progressive disclosure** (see `agents/skills.py`):
1. the brief lists only the MANIFEST (`- name — description`), every skill, grouped by category;
2. `read_skill_reference(name)` returns the SKILL.md **guidance** + the bundled-file list;
3. `read_skill_reference(name, file="reference.py")` returns one file's **code**, on demand.

Name lookups tolerate a legacy `.py` suffix, so older configs / preset prose that say `<name>.py`
still resolve.

## What was built (2026-07-07, one focused effort, full suite green)

1. **Moved** `skills/<name>/` → `preset_pipelines/<name>/` (6 folders); renamed the loader
   `agents/skills.py` → `agents/preset_pipelines.py` (pipeline vocab: `PresetPipeline`/`PIPELINES`/
   `get_pipeline`/`list_pipelines`/`select_pipeline`/`compose_pipeline_prompts`; env
   `AISCIENTIST_PIPELINES_DIR`). `presets.py` shim repointed.
2. **Promoted** the 9 `scripts/*.py` to a flat `skills/` atomic library and added a NEW
   `agents/skills.py` (the `Skill` model, `SKILLS` loader, `skill_manifest`, and the
   `read_skill_reference` tool). `$AISCIENTIST_SKILLS_DIR` now points at the atomic library.
3. **Wired** progressive disclosure: the Scientist's brief lists the GLOBAL atomic-skill manifest
   (name + summary), `read_skill_reference` fetches a body on demand; the fixed registry stays the
   always-on core. `PresetPipeline` no longer bundles scripts.

## Resolved decisions

- External-service wrappers (`annotate_variants`, `scgpt_annotate`, `literature_search`) stay in the
  **registry** (fixed infra wrappers, per Yijun's "固定不太重写" rule) — not moved to skills.
- `preset_pipelines` and `skills` are **fully separate modules** (independent loaders/schemas).

## Built (additive, 2026-07-07)

- **Advanced multi-select over atomic skills** — the console's Advanced panel now has a second
  checklist: checked atomic skills are REQUIRED for the run (the plan MUST apply each). Wiring:
  `/api/skills` + `agents/skills.list_skills()` → the picker; `LabRequest.skills` →
  `LabConfig.required_skills` (validated against the library) → a "REQUIRED skills" directive
  appended to the PI's planning guidance; a `🧩 Required skills` feed line. Distinct from pinning a
  preset-pipeline (which steers the whole plan shape).
- **`search_skills(query)` retrieval** — a `search_skills` Scientist tool (`agents/skills.py`):
  keyword/token-overlap ranking over name > summary > body (offline, deterministic, no embedder).
  The brief used to stop listing above `AISCIENTIST_SKILL_MANIFEST_MAX` (12) skills and say "search"
  instead; with 15 skills in the repo no run ever saw the list. **The cap was removed on 2026-10-01**
  (Yijun's call): every brief lists every skill's name + description, grouped by category, so the
  agent reads the whole library before it chooses; `search_skills` stays as a ranking aid. Bodies are
  still fetched only on demand.

## Migrated flat files → folder skills (2026-07-08)

Per Yijun: the flat `skills/<name>.py` form did not match the Anthropic Skill definition (separate
description + demonstration). Each skill is now a folder `skills/<name>/` with `SKILL.md` (curated
frontmatter `description` + `## When to use` guidance) and `reference.py` (the demonstration
template). Changes:
- `agents/skills.py` — `Skill(name, summary, doc, files)`; loader globs `skills/*/SKILL.md`, parses
  frontmatter (reuses the preset-pipeline convention), reads bundled files. `read_skill_reference`
  now takes an optional `file` (guidance first, code on `file=`). `.py`-tolerant `get_skill`/lookup.
- `agents/research_lab.py` — brief text + REQUIRED-skills directive describe the two-call fetch;
  required-skill matching goes through `get_skill` (tolerant).
- Manifest/`list_skills`/`search_skills` unchanged in shape (name + one-line summary), so the
  `/api/skills` console picker and its round-trip keep working (names now carry no `.py`).
- Progressive disclosure is now genuinely three-level (manifest → guidance → code).

## Induced skills are reviewed before any model sees them (2026-10-07)

An induced skill is one run's `run_code` frozen into a template, with that dataset's file format and
column names inside it. Listed for planning on every later run, it got planned onto data it could not
read: run f3b8268c4fd4 put `verify_matrix_provenance` (DDX41's `read_h5ad` + `nCount_RNA` / `sampleid`
/ `majorclass`) in front of a 10x Cell Ranger `.h5`, and the step failed three times. So:

- `agents/skills.py` keeps two views. `ALL_SKILLS` is everything on disk; `SKILLS`, the library the PI
  plans with and the Scientist lists, searches and reads, holds the curated skills plus only the
  induced skills an admin has **approved**. Everything in the induced root counts as induced and starts
  `pending`, so a folder dropped there cannot skip the review; `retired` stays on disk, never offered.
- Decisions live in `<AISCIENTIST_INDUCED_SKILLS_DIR>/_review.json` (status, who, when, note, and a
  short history), written by `set_review`; they apply at the next step of every run, no restart.
- The console's **Admin → Skill review** tab (`/api/admin/skills`, admin only) lists each induced skill
  with the step it was learned from, shows its SKILL.md and code, and approves, retires or resets it.
- Induction still checks names and duplicates against `ALL_SKILLS`, so a skill awaiting review is not
  learned a second time under another name. A newly induced skill is announced as awaiting review.

## The PI plans with the skill library (2026-10-01)

Until then a skill reached a plan only when the user ticked it (REQUIRED skills above) or when the
Scientist went looking mid-step for something no tool covered: the PI drafted every plan from one
preset pipeline and the tool list. Now every draft (`_pi_plan` in `agents/research_lab.py`) carries
the skill manifest after the tools, read from the library as it is at that moment, so each request
that is planned in full is matched against the skills, including ones induced since the last run.

- **What the PI sees** (`plan_skill_lines` in `agents/skills.py`): the whole library, superseded
  versions hidden. It was capped at 40 skills with descriptions cut at 200 characters; on 2026-10-02
  the count cap was removed (Yijun: no list caps) and the cut raised to the Agent-Skills maximum of
  1,024 characters, because 200 cut off the sentence that routes a skill (the CellQC skill's
  description ENDS with "inside an AiScientist analysis ... is the run_cellqc tool, not this
  skill"). A caller can still pass `limit` to get the best keyword matches first.
- **How a plan uses one:** a step that needs a skill is a `run_code` step that names the skill in
  backticks. Tools still come first: the prompt says not to plan a skill that repeats a tool.
- **Execution:** the Scientist's brief names the skill a step mentions (`skills_named_in`), so it
  reads that skill instead of rediscovering it with `search_skills`.
- The multi-cycle re-plan gets the same list, and `check_plan_tooling` does not report a skill
  name (an induced skill may be called `run_…`) as an unknown tool.
- **A skill's description must name its method.** The PI is required to state each step's method,
  and when a description did not name one it guessed (1 plan in 7 said AUCell for a template that
  runs `sc.tl.score_genes`). The prompt now forbids attributing a method the description does not
  state, and `score_signature` names its method. Write new descriptions the same way.
- Measured on Qwen3.8-27B (`experiments/plan_vs_exec_ab/skills_ab.py`, Rounds 7 and 7b in that
  README), on OpenRouter and then on our own INT4 serve job with the production settings: where the
  loaded protocol does not name the needed skill, plans with the list applied `score_signature`
  (15/15 and 8/8) and plans without it wrote their own scoring code (0/8 and 0/8); where the protocol
  already names it, both arms used it; a plain DE question named no skill either way, and no plan
  replaced a tool with a skill.

## Not yet built (deferred)

- **Skill induction** — distil a successful run into a new `skills/<name>/`. **SHELVED per Yijun
  (2026-07-07) — not the focus right now.** (Upgrade `search_skills` to embeddings if/when keyword
  overlap stops being enough.)
