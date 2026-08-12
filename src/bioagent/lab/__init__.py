"""DRAFT (week-of-2026-06-22 discussion) — proposed multi-agent "Virtual Lab" kernel.

NOT wired into the gateway. Pure Python, no LangGraph/LangChain. Two pieces:

- ``archive`` — the **Lab Archive**: durable, append-only, resumable state that lives OFF
  the ephemeral compute node, so each expert's memory + meeting transcripts survive an
  ``srun`` reclaim / gateway restart / a week between sessions.
- ``kernel`` — a small FIXED dispatcher (``call_tool`` / ``run_agent_turn`` / ``run_meeting``
  + a PI plan/route loop) over DATA-driven registries (Tool / Agent / Playbook). New
  workflows are DATA (register a tool/expert/playbook), never new dispatcher code.

See handoff/yijun/HANDOFF.md "2026-06-18 (later)".
"""
