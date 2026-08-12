<!-- PROJECT_NAME_START -->
## Project name: AiScientist

This project is called **AiScientist** (public: `<PUBLIC_HOSTNAME>`). "BioAgent" is the
**former brand name** — use "AiScientist" in all user-facing text (UI, emails, reports, docs, README).

The **code namespace deliberately stays `bioagent`** for deploy compatibility — do NOT rename these:
the Python package `src/bioagent/` + all `from bioagent…` imports, the `BIOAGENT_*` env vars, the
`/data/BioAgent` prod data dir, the `bioagent` systemd service, the `bioagent` SSH service account, and
the DB. Renaming any of them breaks the deployed prod, HPC3 paths, and SSH creds. Product name ≠ code
namespace. `BioAgentPrototype` (the git repo/dir name) also stays.

**Env vars accept either prefix.** `core.config.apply_brand_env_aliases()` (run at `.env` load) mirrors
`BIOAGENT_*` ⇄ `AISCIENTIST_*`, so ops can set EITHER in `.env` and both the legacy
`os.environ.get("BIOAGENT_X")` reads and new `AISCIENTIST_X` reads resolve (the new-brand value wins when
both are set). New code can use `core.config.env("X")` (base name; new-first, then legacy). This is the
zero-downtime compat layer (phase 1) for an eventual full package/env/path rename.
<!-- PROJECT_NAME_END -->

<!-- ADAPTIVE_KG_AGENT_INSTRUCTIONS_START -->
## Adaptive KG Repo Memory

When Adaptive KG MCP tools are available, use them as the default repo-memory path. Do not wait for the user to explicitly ask for repo memory.

- At the start of repository-understanding, architecture, navigation, debugging, review, or change-planning tasks, call `adaptive_kg_status` first.
- If `adaptive_kg_status` reports that the graph is missing, stale, or has no agent export, proactively call `adaptive_kg_index` before relying on graph results.
- After status/index, call `adaptive_kg_query` to retrieve compact file/symbol/edge context before reading broad source files into the LLM context.
- Use returned graph paths, symbols, and edges to choose the smallest source snippets that still need direct inspection.
- Only bypass Adaptive KG for tiny single-file questions, explicit user requests to avoid tools, or when MCP tools are unavailable.
- If graph evidence is missing, stale, or contradicted by source inspection, say so and fall back to direct code search.
- For high-impact changes or suspicious graph results, call `adaptive_kg_graph_audit` and review audit candidates against targeted source reads.
- Use `adaptive_kg_propose_update` for useful agent-discovered graph facts rather than editing generated graph files directly.
<!-- ADAPTIVE_KG_AGENT_INSTRUCTIONS_END -->

## File structure log (`.claude/filemanager.md`)

Keep a shared record of file/folder STRUCTURE changes so any session or teammate knows who added/changed what and why.

- At the start of repository-navigation or change-planning, **read `.claude/filemanager.md`** for context on existing paths.
- Whenever you **add, remove, move, or rename** a file or directory, **append an entry** to `.claude/filemanager.md` (newest first): date, author (`claude`/`user`), the path(s), the change, a one-line why, and whether it's intentionally not committed. Pure content edits don't need an entry unless they aid orientation.

## Handoff docs (`handoff/<line>/`)

Running handoffs live under `handoff/`, one folder per work-line/owner (see `handoff/README.md`).

- **Update only your own line's handoff** as you work: `handoff/<line>/HANDOFF.md` (+ keep `HANDOFF.zh-CN.md` in sync), newest dated section at the top. Don't edit another line's handoff — cross-reference it.
- The core/orchestrator line is `handoff/yijun/`; the literature line is `handoff/ziyao/`. Starting a new line → add `handoff/<you>/` and a row in `handoff/README.md`.
- The project front-page `README.md` stays at the repo root (not a personal handoff).
