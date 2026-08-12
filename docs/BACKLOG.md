# Backlog

Larger initiatives that are **decided but deferred** — each big enough to warrant its own
branch when picked up. Newest / highest-level first. Day-to-day TODOs live in the per-line
handoffs (`handoff/<line>/HANDOFF.md`), not here.

---

## Fixed / deterministic pipeline option — deferred, own branch

**Goal.** An **Advanced dropdown (default `None`)** that lets the user pick a **fixed,
deterministic pipeline** which runs a predefined step list **straight to the report** — no PI
free-planning, no plan-review gate, no re-planning / skill-swap mid-run. `None` = today's
PI-planned behaviour. The live feed still streams each step and **Stop still works**; it's the
*planning* that's removed, not the visibility. This is the "run a pipeline to the end" lock the
skills' *steering* deliberately does NOT give (a skill guides the PI; a fixed pipeline bypasses it).

**Hard constraint (Yijun, 2026-07-07).** The pipeline's steps must be **written in ONE
machine-readable `steps` list and edited only there** — a single source of truth. Selecting the
pipeline must NOT require going back to edit "the corresponding steps" anywhere else (e.g. the
skill's prose "Ordered plan"). So the `steps` list **IS** the pipeline definition; do not duplicate
step content between a prose plan and the steps list (no double-maintenance). If a fixed pipeline is
derived from a skill, the steps list replaces/authoritative-over the prose plan for execution — the
executor reads the list, never re-parses the markdown.

**Shape (chosen: true deterministic lock).**
- Add a machine-readable `steps` spec to a pipeline (e.g. a `pipeline:`/`steps:` block in the skill
  folder, or a dedicated pipeline artifact). Each entry = a fixed step brief (+ optional
  suggested tool) the executor runs in order.
- New `LabConfig.fixed_pipeline` (key or None). When set, `run()` takes a branch that **skips PI
  planning + plan-mode review + auto-select/team routing** and feeds the executor the fixed steps
  directly, then renders the report. Each step still runs through the Scientist (+ optional light
  Critic) so it isn't blind — but there is no agenda drafting and no revision loop.
- Gateway: a `fixed_pipeline` field on the lab request. Frontend: a `None`-default dropdown in the
  Advanced panel listing available fixed pipelines.

**Seed.** First fixed pipeline = **`variant_annotation` (VCF)** — matches the demo VCF
(`skills/variant_annotation/examples/demo_variants.vcf`); "upload VCF → run to a report, no gates".

**When picked up:** own branch; build on the now-decoupled `agents/skills.py`. Touches
`agents/skills.py` (steps spec + a fixed-pipeline loader), `agents/research_lab.py` (the bypass
branch in `run()`), `gateway/app.py` (request field), `frontend/console/` (the dropdown).

**Status:** not started (as of 2026-07-07) — **not urgent** ("不急着开发"). Owner: TBD.

## Further decouple the skill system — deferred, own branch

**Goal.** Make the skill library a **standalone subsystem**, decoupled from the research-lab
orchestration and from the "preset" concept it currently rides on. Today a skill is a
`skills/<name>/SKILL.md` (frontmatter + body) plus `scripts/*.py`, but:
- loading/parsing lives in `agents/presets.py` — skills and "presets" are the SAME object
  (`ResearchPreset`, `load_skills`), so the two ideas are conflated;
- selection + reference are wired INTO `agents/research_lab.py` (`_select_skill`,
  `_make_skill_reference_tool`, `LabConfig.skill_library`, `_SKILL_SELECT_SYSTEM`), so the
  lab owns skill logic instead of consuming a clean skill API.

**See [`docs/skills_and_pipelines_architecture.md`](skills_and_pipelines_architecture.md)** — the
"final form" spec (three layers: registry / skills / preset-pipelines) this and the fixed-pipeline
item both fold into.

**Vocabulary + Yijun's intent (2026-07-07) — "skill" is currently mis-named.** Three layers:
- **Tool** = an atomic, decoupled capability (the registry: `run_scanpy_qc`, `run_clustering`,
  `run_de`, `run_enrichment`, `annotate_variants`, `scgpt_annotate`, `literature_search`,
  `run_code`). This is the real composable layer and it already exists.
- **Preset pipeline** = a full end-to-end workflow that *composes* several tools — i.e. exactly
  what each `skills/<name>/SKILL.md` is today (they share a QC→clustering backbone and run it
  start-to-finish). The console's Advanced multi-select holds THESE. As of 2026-07-07 the frontend
  labels them **"preset pipeline"**, not "skill" (`index.html` Advanced panel + the
  `📚 Loaded preset pipeline` feed line in `gateway/app.py`), because that's what they are.
- **Skill (the target)** = a *decoupled, composable* mid-level unit — the thing Yijun actually
  wants the multi-select to check several of and have the PI compose. This layer does NOT exist
  yet; the current "skills" are preset-pipelines wearing the name.

So the decouple work should (a) introduce the composable-skill layer between tools and full
pipelines, (b) make the multi-select select those composable skills, and (c) free the name "skill"
for it — renaming today's `skills/` folders + `agents/skills.py` toward "preset pipeline" for the
end-to-end ones. NB the "Fixed / deterministic pipeline option" item above is the *preset-pipeline*
line; this item is the *composable-skill* line — they are complementary, not the same.

**What "decouple" means here:**
- Extract a dedicated skill module/package (loading, frontmatter schema, script bundling,
  selection/routing) that `research_lab` merely *consumes* through a small interface —
  separate skill definitions from the orchestrator that runs them, and skills from presets.
- This is the seam that later enables **skill induction** (an agent that distils a
  successful run into a new `SKILL.md` + script) as its own agent, per the Kosmos-parity
  roadmap — hard to add while skill logic is spread across `presets.py` + `research_lab.py`.

**When picked up:** own branch (e.g. `refactor/skill-subsystem`); interface + move first
(behaviour-preserving), then induction on top. Touches `agents/presets.py`,
`agents/research_lab.py`, `skills/`. Note the DAG line already has `_apply_skills_planning`
— fold that into the same subsystem.

**Status:** **interface + move DONE** (2026-07-07, commit `163447c`): the canonical
`agents/skills.py` now owns the data model, loading, dataset-aware `select_skill`,
`compose_skill_prompts`, and the `read_skill_reference` tool; `agents/presets.py` is a thin
re-export shim; `research_lab` consumes the module. **Remaining:** (1) **skill induction** (distil a
successful run into a new `SKILL.md`) — the whole point of the seam; (2) fold the DAG line's
`_apply_skills_planning` into `skills.py` too (not touched by the move). Owner: TBD.

## Bring-your-own external API (replace the HPC3 backend) — deferred, own branch

**Goal.** Let a user plug in their **own external LLM/tooling API** (OpenAI-compatible or a
hosted service) and run the whole research lab against it, instead of the UCI HPC3 +
Slurm-vLLM backend. This makes the product usable outside UCI, without a cluster account.

**Why it's a rewrite, not a flag.** The current system assumes HPC3 for far more than the
LLM. Deeply wired to HPC3 today:
- **File storage** — dataset uploads stream to `/dfs3b/ruic20_lab/<user>/uploads/`
  (`BIOAGENT_UPLOADS_ON_HPC`); the gateway holds no data copy.
- **Process / intermediate-artifact storage** — per-step checkpoints (`*.h5ad`), run work
  dirs, and artifacts live on HPC3 DFS between steps.
- **Code execution** — `run_code` (CodeAct), scanpy analysis steps, report render
  (pandoc/XeLaTeX), scGPT, and VL review all run as **Singularity-contained Slurm jobs**
  on HPC3 (`BIOAGENT_ANALYSIS_ON_HPC` / `BIOAGENT_REPORT_ON_HPC` / `run_code` on HPC3).
- **The LLM itself** — vLLM serve job + SSH tunnel (per-session), the part `_lab_llm`
  already abstracts (`BIOAGENT_LLM_BASE_URL` exists for OpenRouter *testing*, but only the
  LLM call — not storage/compute — is redirected).

So a real "external backend" needs a **storage abstraction** and a **compute/execution
abstraction** behind the tools, not just an LLM endpoint swap. The `providers/`
OpenAI-compatible client and `_lab_llm`'s `base_url` path are the seed of the LLM side;
storage + code-execution have no equivalent seam yet.

**When picked up:** open a dedicated branch (e.g. `feat/pluggable-backend`); scope a
storage interface (local FS vs HPC3 DFS vs object store) and an executor interface
(local sandbox vs Slurm) first, then port the tools onto them. Note: `BIOAGENT_LLM_BASE_URL`
+ OpenRouter is a **local test convenience** today, not the product direction — the external
API path is the productized version of that idea.

**Status:** not started (as of 2026-07-05). Owner: TBD.
