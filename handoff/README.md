# Handoffs

Per-line handoff docs. Each work-line / owner has its own folder so contributors update
**only their own** running handoff and nobody steps on anyone else. (The project front-page
README stays at the repo root.)

## Folders

| Folder | Line / owner | What it covers |
|---|---|---|
| [`yijun/`](yijun/HANDOFF.md) | Orchestrator + output (Yijun) | The `ResearchLab` loop (PI/Scientist/Critic), the gateway console, HPC3/vLLM serving + Singularity-Slurm, the report bundle, **scGPT** annotation, deploy. The authoritative running handoff for the core system. |
| [`ziyao/`](ziyao/HANDOFF.md) | Literature line (Ziyao) | `deep_literature` — PaperQA2 over the local Qwen; cited-answer retrieval + grounding. |

Each folder has `HANDOFF.md` (English) and `HANDOFF.zh-CN.md` (中文) — keep both in sync.

## Convention for whoever picks this up next

- **Update your own line's `HANDOFF.md`** (and its `.zh-CN.md`) as you work — newest section
  at the top, dated. Don't edit another line's handoff; cross-reference it instead.
- **Starting a new line?** Add a new folder here (`handoff/<you>/`) with `HANDOFF.md` +
  `HANDOFF.zh-CN.md`, and add a row to the table above.
- Keep the **root `README.md`** as the project front page (not a personal handoff).
- Also append a one-line entry to `.claude/filemanager.md` whenever you add/move/remove a
  handoff folder (the repo's structure-change log).

> Note: `scripts/pr_review_gate.py` requires `handoff/yijun/HANDOFF.md` +
> `handoff/yijun/HANDOFF.zh-CN.md` to exist (the core running handoff). If you rename or
> move the `yijun/` line, update that check too.
