"""Organize a research-lab run into a CATEGORIZED bundle (better-than-operon: the
intermediate "how" and the final deliverables are classified into subdirs, not dumped
into one flat directory):

    <run>/artifacts/
      report/    report.pdf · report.docx · report.md      (final deliverables)
      figures/   *.png                                      (data figures + schematics)
      tables/    *.csv                                       (raw output data)
      process/   agenda.json · lab_result.json · round_NN.json · transcript.md
      data/      dataset_results.json · qc_metrics.json …    (input/preflight records)

This module writes the ``process/`` artifacts — the Virtual-Lab-style meeting record
(the PI agenda, each Scientist↔Critic round, and the final synthesis) as both machine
JSON and a human-readable markdown transcript. Pure + deterministic; takes the plain
``LabResult.to_dict()`` so it's decoupled from the orchestrator and easy to test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_process_artifacts(result: dict[str, Any], process_dir: Path) -> list[Path]:
    """Write agenda.json, lab_result.json, per-round JSON, and transcript.md.
    Returns the list of files written."""
    process_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def _dump(name: str, obj: Any) -> None:
        p = process_dir / name
        p.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(p)

    _dump("agenda.json", {"question": result.get("question"), "agenda": result.get("agenda", [])})
    _dump("lab_result.json", result)

    rounds = result.get("rounds", []) or []
    for r in rounds:
        n = r.get("round_no", len(written))
        _dump(f"round_{int(n):02d}.json", r)

    transcript = _render_transcript(result)
    tp = process_dir / "transcript.md"
    tp.write_text(transcript, encoding="utf-8")
    written.append(tp)
    return written


def _render_transcript(result: dict[str, Any]) -> str:
    """A human-readable meeting record: agenda → each round (Scientist + Critic) →
    final synthesis. Mirrors the Virtual-Lab 'team meeting' transcript."""
    lines: list[str] = ["# Research transcript", ""]
    if result.get("question"):
        lines += [f"**Research question:** {result['question']}", ""]

    agenda = result.get("agenda", []) or []
    if agenda:
        lines.append("## Agenda (PI)")
        lines += [f"{i}. {step}" for i, step in enumerate(agenda, 1)]
        lines.append("")

    for r in result.get("rounds", []) or []:
        sci = r.get("scientist_result", {}) or {}
        verdict = r.get("verdict", {}) or {}
        steps = sci.get("steps", []) or []
        # Mark each tool call pass/fail (a step with ok=False is a tool that errored).
        tools = ", ".join(
            s.get("tool", "?") + ("" if s.get("ok", True) else " ✗") for s in steps
        ) or "(none)"
        lines.append(f"## Round {r.get('round_no', '?')} — {r.get('step', '')}")
        scientist_label = r.get("specialist") or "Scientist"
        status = sci.get("status", "?")
        lines.append(
            f"- **{scientist_label}** [status: {status}] (tools: {tools}): "
            f"{sci.get('final_answer') or '(no answer)'}"
        )
        # Surface each tool error inline so a failed run is debuggable from the transcript.
        for s in steps:
            if not s.get("ok", True) and s.get("error"):
                lines.append(f"  - ⚠️ `{s.get('tool', '?')}` failed: {str(s.get('error'))[:300]}")
        lines.append(
            f"- **Critic**: {str(verdict.get('verdict', '?')).upper()} "
            f"(score {verdict.get('score', 0)}) — {verdict.get('critique', '')}"
        )
        lines.append("")

    lines.append("## Final report (PI synthesis)")
    lines.append("")
    lines.append(result.get("final_answer") or "(no synthesis produced)")
    lines.append("")
    summary = (
        f"converged={result.get('converged')} · "
        f"accepted_steps={result.get('accepted_steps')}/{len(agenda)}"
    )
    lines.append(f"_Run summary: {summary}_")
    lines.append("")
    lines += _render_debug_summary(result)
    return "\n".join(lines) + "\n"


def _render_debug_summary(result: dict[str, Any]) -> list[str]:
    """A flat, at-a-glance debug view: every tool call across the run with ok/ERROR, then
    every collected error. Lets a real-machine test be diagnosed without opening the JSON."""
    lines = ["## Tool & error summary (debug)", ""]
    any_call = False
    for r in result.get("rounds", []) or []:
        rn = r.get("round_no", "?")
        sci = r.get("scientist_result", {}) or {}
        for s in sci.get("steps", []) or []:
            any_call = True
            mark = "ok" if s.get("ok", True) else "ERROR"
            line = f"- round {rn}: `{s.get('tool', '?')}` [{mark}]"
            if mark == "ERROR" and s.get("error"):
                line += f" — {str(s.get('error'))[:200]}"
            lines.append(line)
    if not any_call:
        lines.append("- (no tool calls recorded)")

    errs = [
        (r.get("round_no", "?"), e)
        for r in (result.get("rounds", []) or [])
        for e in ((r.get("scientist_result", {}) or {}).get("errors", []) or [])
    ]
    if errs:
        lines += ["", "### Errors"]
        for rn, e in errs:
            lines.append(f"- round {rn}: `{e.get('tool', '?')}` — {str(e.get('error'))[:300]}")
    lines.append("")
    return lines
