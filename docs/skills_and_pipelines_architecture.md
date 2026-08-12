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
  the dir with `$BIOAGENT_SKILLS_DIR`. Grown by **induction** (deferred).

**Three-level progressive disclosure** (see `agents/skills.py`):
1. the brief lists only the MANIFEST (`- name — description`);
2. `read_skill_reference(name)` returns the SKILL.md **guidance** + the bundled-file list (no code);
3. `read_skill_reference(name, file="reference.py")` returns one file's **code**, on demand.

Name lookups tolerate a legacy `.py` suffix, so older configs / preset prose that say `<name>.py`
still resolve.

## What was built (2026-07-07, one focused effort, full suite green)

1. **Moved** `skills/<name>/` → `preset_pipelines/<name>/` (6 folders); renamed the loader
   `agents/skills.py` → `agents/preset_pipelines.py` (pipeline vocab: `PresetPipeline`/`PIPELINES`/
   `get_pipeline`/`list_pipelines`/`select_pipeline`/`compose_pipeline_prompts`; env
   `BIOAGENT_PIPELINES_DIR`). `presets.py` shim repointed.
2. **Promoted** the 9 `scripts/*.py` to a flat `skills/` atomic library and added a NEW
   `agents/skills.py` (the `Skill` model, `SKILLS` loader, `skill_manifest`, and the
   `read_skill_reference` tool). `$BIOAGENT_SKILLS_DIR` now points at the atomic library.
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
  The brief now switches on library size: ≤ `MANIFEST_MAX` (env `BIOAGENT_SKILL_MANIFEST_MAX`,
  default 12) → inline the manifest as before; beyond that → don't list any, tell the agent to call
  `search_skills(query)` first, then `read_skill_reference`. So even the name+summary list can't
  bloat every step as the library grows. Two small always-on tools (search + read); bodies still
  fetched only on demand.

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

## Not yet built (deferred)

- **Skill induction** — distil a successful run into a new `skills/<name>/`. **SHELVED per Yijun
  (2026-07-07) — not the focus right now.** (Upgrade `search_skills` to embeddings if/when keyword
  overlap stops being enough.)
