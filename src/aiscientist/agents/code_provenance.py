"""Which steps of a run ran tested code, and which ran code the model wrote during the run.

"Tested" is a property of the code that ran, not of the request (RCHENLAB/AiScientist issue 13): a
curated tool (``run_cellqc``, ``run_de``, ``literature_search`` ...) is code the team wrote and
tested; ``run_code``, ``run_shell`` and ``run_in_environment`` run code or commands the model wrote
or typed during this run, even when it adapted them from a vetted skill of the pinned registry
release that the plan assigned to the step. The platform records every call a step made, so the
label needs no matching and no model: it is read off the rounds.

A step's final attempt decides its label. It is ``untested`` when its code wrote a table, figure or
file, or when code ran and no curated tool did: then the result came from that code. It is
``tested`` when a curated tool produced the result, even if code then only read it to check it —
the CellQC runs on production read ``qc_status.csv`` or the QC checkpoint that way after
``run_cellqc``, and that is a check, not a step the tool could not do. It is ``none`` when the step
only looked things up or nothing succeeded. Every code-tool call of every attempt is kept, failed
ones included: they are the evidence a skill request carries (``gateway/skill_requests.py``).
"""
from __future__ import annotations

import re
from typing import Any

from .plan_doc import CODE_TOOLS
from .research_harness import LOOKUP_TOOLS
# Whether a code call wrote a table, figure or file is the same question the QC-only reply asks
# (agents/scope.py): one rule, so a run never gets the QC chat reply AND an untested-code notice
# that disagree about the same script.
from .scope import call_wrote_outputs

#: Calls that neither produce a result nor run code: the Scientist's answer and the lookups.
NEUTRAL_TOOLS = frozenset(LOOKUP_TOOLS | {"finish"})

#: How much of one call's code a record keeps. Longer code is cut, and the record says so.
MAX_CODE_CHARS = 20_000

UNTESTED = "untested"
TESTED = "tested"
NONE = "none"


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """A field of a round, which is a ``LabRound`` in production and a dict in run_state.json."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _verdict(rnd: Any) -> "tuple[str, str]":
    v = _get(rnd, "verdict") or {}
    if isinstance(v, dict):
        return str(v.get("verdict") or ""), str(v.get("critique") or "")
    return str(getattr(v, "verdict", "") or ""), str(getattr(v, "critique", "") or "")


def _calls(rnd: Any) -> "list[dict[str, Any]]":
    res = _get(rnd, "scientist_result") or {}
    steps = res.get("steps") if isinstance(res, dict) else None
    return [s for s in (steps or []) if isinstance(s, dict)]


def _code_call(call: dict[str, Any]) -> dict[str, Any]:
    """One code-tool call as a record keeps it: what ran, whether it worked, what it said."""
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    code = str(args.get("code") or args.get("command") or "")
    out: dict[str, Any] = {"tool": str(call.get("tool") or ""), "ok": bool(call.get("ok"))}
    # The skill the code ran under: named in the call, or by the tool's result (run_in_environment
    # reports the registry skill whose environment it used).
    result = call.get("result") if isinstance(call.get("result"), dict) else {}
    skill = args.get("skill") or result.get("skill")
    if skill:
        out["skill"] = str(skill)
    out["code"] = code[:MAX_CODE_CHARS]
    if len(code) > MAX_CODE_CHARS:
        out["code_truncated_from"] = len(code)
    if call.get("ok"):
        out["wrote"] = call_wrote_outputs(call)
        out["summary"] = str(call.get("summary") or "")[:600]
        files = [str(f) for k in ("artifacts", "tables", "figures", "files")
                 for f in (result.get(k) or []) if isinstance(f, str)]
        if files:
            out["files"] = list(dict.fromkeys(files))[:40]
    else:
        # A sandbox that ran and failed returns its output as ``result``, not ``error``.
        out["error"] = str(call.get("error") or result.get("error") or result.get("stderr")
                           or call.get("summary") or "")[:1200]
    return out


def step_status(calls: "list[dict[str, Any]]") -> str:
    """``untested`` / ``tested`` / ``none`` for one attempt's calls (see the module docstring)."""
    ok = [c for c in calls if c.get("ok")]
    code = [c for c in ok if str(c.get("tool") or "") in CODE_TOOLS]
    tool = any(str(c.get("tool") or "") not in CODE_TOOLS | NEUTRAL_TOOLS for c in ok)
    if any(call_wrote_outputs(c) for c in code) or (code and not tool):
        return UNTESTED
    return TESTED if tool else NONE


def run_provenance(rounds: "list[Any]") -> dict[str, Any]:
    """Every step of a run, in the order it first ran, with its label and its code calls.

    A step that ran more than once (a revise, then its accept) is labelled by its LAST attempt,
    because that is the result the reply and the report use; the code of every attempt is kept.
    """
    order: list[str] = []
    by_step: dict[str, dict[str, Any]] = {}
    for rnd in rounds or []:
        text = str(_get(rnd, "step") or "").strip()
        if not text:
            continue
        calls = _calls(rnd)
        verdict, critique = _verdict(rnd)
        entry = by_step.get(text)
        if entry is None:
            order.append(text)
            entry = by_step[text] = {"step": text, "attempts": 0, "code_calls": []}
        entry["attempts"] += 1
        entry["status"] = step_status(calls)
        entry["accepted"] = verdict == "accept"
        entry["verdict"] = verdict
        entry["critique"] = critique[:1500]
        entry["tools"] = list(dict.fromkeys(
            str(c.get("tool")) for c in calls
            if c.get("ok") and str(c.get("tool") or "") not in NEUTRAL_TOOLS))
        entry["code_calls"].extend(_code_call(c) for c in calls
                                   if str(c.get("tool") or "") in CODE_TOOLS)
    steps = [by_step[t] for t in order]
    accepted = [s for s in steps if s["accepted"]]
    return {
        "steps": steps,
        "accepted_steps": len(accepted),
        "untested_accepted": [s["step"] for s in accepted if s["status"] == UNTESTED],
        "tested_accepted": [s["step"] for s in accepted if s["status"] == TESTED],
        "code_attempts": sum(len(s["code_calls"]) for s in steps),
    }


def has_untested_code(prov: dict[str, Any]) -> bool:
    """True when a step needed model-written code: its result came from code, or code was tried
    and no tested tool produced a result. Such a run is a skill request, whether or not the step
    was accepted. Code that only checked a tested tool's result does not count."""
    for s in prov.get("steps") or []:
        if s.get("status") == UNTESTED or (s.get("code_calls") and s.get("status") == NONE):
            return True
    return False


def step_title(step: str, limit: int = 80) -> str:
    """A step's title for a sentence: ``**QC** — Run run_cellqc …`` → ``QC``; a step with no bold
    title is its text, shortened."""
    text = " ".join(re.sub(r"[*`]", "", str(step)).split())
    text = re.sub(r"^\d+[.)]\s*", "", re.split(r"\s+[—–]\s+|\s+-\s+", text, maxsplit=1)[0])
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def untested_notice(prov: dict[str, Any], *, recorded: bool) -> str:
    """The notice the chat reply and the report open with when an accepted step's result came
    from model-written code. Empty when none did: then the reply rests on tested tools only."""
    untested = list(prov.get("untested_accepted") or [])
    if not untested:
        return ""
    total = int(prov.get("accepted_steps") or len(untested))
    names = "; ".join(f"*{step_title(s)}*" for s in untested[:6])
    if len(untested) > 6:
        names += f"; and {len(untested) - 6} more"
    lead = ("This step" if total == 1 else
            f"{len(untested)} of the {total} completed steps" if len(untested) < total else
            f"All {total} completed steps")
    text = (f"> **Untested code.** {lead} ran code that the AI wrote during this run, not a tested "
            f"AiScientist tool: {names}. Treat {'its' if total == 1 else 'their'} results as "
            "exploratory and check them before relying on them. Every attempt's code is in "
            "`process/untested_code.md`.")
    if recorded:
        text += (" This request was recorded, with that code and its results, so the team can "
                 "build and test a skill for it.")
    return text + "\n"
