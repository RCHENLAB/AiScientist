"""Plan steps that need model-written code, held while the deployment turns it off (RCHENLAB/AiScientist#150).

A step needs model-written code when the tool it runs is one of the code tools, or when it names no
tool at all (the Scientist would have to write the analysis). A literature step and the report step
run their own tools. With ``AISCIENTIST_MODEL_WRITTEN_CODE`` off such a step is never accepted,
whatever the plan came from:

* a plan being drafted or reviewed (the first draft, the plan review, a revision in chat or a hand
  edit, a saved plan shown again, the autonomous path, a campaign's next cycle): the step is taken
  out and reported as not available (:func:`hold_code_steps`). So is a covered step that needs its
  output. The rest of the plan runs; when the hold left nothing to run, nothing runs
  (:func:`nothing_runs`). A plan that holds no step is not this case: a plan of the report step
  alone runs as it always has;
* a plan that would run without review (an approved saved plan, a resumed run): it is refused, and
  must be planned again (:func:`refuse_code_steps`);
* a step proposed while the run executes (exploration, an alternative after a failed step): it is
  not proposed (:func:`code_step`).

Each function is a no-op with the switch on. The lab calls them at each place a plan can change;
they are kept here, apart from the plan loop, so that loop can change without touching them.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Callable, Iterable

from ..code_policy import (WITHHELD_TOOLS, NotAvailable, model_written_code, not_available_markdown,
                           skill_does)
from ..loop_utils import safe_json_loads
from ..plan_doc import PlanStep
from .models import LabResult
from .step_kinds import _is_literature_step, _is_report_step
from .tooling import _declared_tools, _primary_tool

if TYPE_CHECKING:
    from ..plan_doc import PlanDoc

EventFn = Callable[[dict[str, Any]], None]


def needs_code(text: str, declared: "PlanStep | None" = None) -> bool:
    """Does this step need code the model writes? ``declared`` is the plan document's step, whose
    ``tool`` is authoritative when it has one."""
    if _is_report_step(text) or _is_literature_step(text):
        return False
    tool = declared.tool if declared is not None and declared.tool else _primary_tool(text)
    return tool is None or tool in WITHHELD_TOOLS


def code_step(text: str) -> bool:
    """A step proposed during the run that must not be offered: it needs model-written code and the
    deployment does not run it. Always False with the switch on."""
    if model_written_code():
        return False
    return needs_code(text) or bool(_declared_tools(text) & WITHHELD_TOOLS)


def _plan_of(lab: Any) -> "PlanDoc | None":
    return getattr(lab, "_approved_plan", None) or getattr(lab, "_plan_doc", None)


def _uncovered(agenda: "list[str]", plan: "PlanDoc | None",
               indices: "Iterable[int] | None" = None) -> "list[int]":
    picks = range(len(agenda)) if indices is None else sorted(indices)
    return [i for i in picks
            if needs_code(agenda[i], plan.step_for(agenda[i]) if plan is not None else None)]


# --- what the plan card says about a held step ----------------------------------------------------

_MATCH_SYSTEM = (
    "You match plan steps that no tested tool performs on this deployment with the closest skill of "
    "its library, and find the other steps that need their output. Reply with ONE JSON object and "
    "nothing else:\n"
    '{"unavailable": [{"step": <number>, "closest_skill": "<a skill name from the list, or empty>", '
    '"lacks": "<what that skill does not do that the step needs, as words completing \'It does not '
    '...\'; empty when the skill does what the step asks>"}], '
    '"needs": [{"step": <number of a step NOT marked unavailable>, "needs": <number of the step '
    'whose output it reads>}]}\n'
    "Name a skill only when it does what the step asks or most of it; otherwise leave it empty. List "
    "under needs every step that cannot start without the output (a table, labels, scores or a "
    "figure) of a step marked unavailable, or of a step that itself needs one; not a step that "
    "merely comes after it.")


def _library() -> "dict[str, Any]":
    from ..skills import SKILLS, refresh_skills, superseded_names
    refresh_skills()
    hidden = superseded_names(SKILLS)
    return {n: s for n, s in list(SKILLS.items()) if n not in hidden}


def _skill_item(step: str, name: str, lacks: str, lib: "dict[str, Any]") -> NotAvailable:
    from ..skills import get_skill
    skill = get_skill(name, lib) if name else None
    if skill is None:
        return NotAvailable(step=step)
    lacks = " ".join(str(lacks or "").split()).rstrip(". ")
    if lacks.lower().startswith("it does not "):
        lacks = lacks[len("it does not "):]
    return NotAvailable(step=step, skill=skill.name, does=skill_does(skill.summary), lacks=lacks)


def describe(lab: Any, agenda: "list[str]",
             uncovered: "list[int]") -> "tuple[list[NotAvailable], list[int]]":
    """``(items, dependents)``: what the card says about each step at ``uncovered``, and the
    positions of the other steps that need one's output (directly or through another).

    One ``classify`` call names the closest skill and what it lacks, and reads the dependencies. A
    skill the step's own text names is used when there is no model or its reply cannot be read; the
    plan is never held up by this call."""
    from ..skills import skills_named_in
    lib = _library()
    named = {i: (skills_named_in(agenda[i], lib) or [""])[0] for i in uncovered}
    picks: "dict[int, tuple[str, str]]" = {i: (named[i], "") for i in uncovered}
    needs: "dict[int, int]" = {}
    if getattr(lab, "_complete_fn", None) is not None:
        listing = "\n".join(
            f"{i + 1}. {'[UNAVAILABLE] ' if i in uncovered else ''}{s}" for i, s in enumerate(agenda))
        skills = "\n".join(f"- {s.name}: " + " ".join((s.summary or "").split())[:400]
                           for s in lib.values())
        try:
            raw = lab._complete([
                {"role": "system", "content": _MATCH_SYSTEM},
                {"role": "user", "content": (
                    f"Plan steps:\n{listing}\n\n"
                    f"Skills:\n{skills or '(none)'}\n\nReturn the JSON object now.")},
            ], role="classify")
            obj = safe_json_loads(raw)
            if not isinstance(obj, dict):
                m = re.search(r"\{.*\}", raw or "", re.DOTALL)
                obj = safe_json_loads(m.group(0)) if m else {}
            for row in (obj or {}).get("unavailable") or []:
                idx = _index(row.get("step") if isinstance(row, dict) else None, len(agenda))
                if idx in picks:
                    picks[idx] = (named[idx] or str(row.get("closest_skill") or "").strip(),
                                  str(row.get("lacks") or ""))
            for row in (obj or {}).get("needs") or []:
                if not isinstance(row, dict):
                    continue
                dep, src = _index(row.get("step"), len(agenda)), _index(row.get("needs"), len(agenda))
                if dep is not None and src is not None and dep not in uncovered and dep != src:
                    needs.setdefault(dep, src)
        except Exception:  # noqa: BLE001 - the plan stands without the match
            pass
    items = [_skill_item(agenda[i], *picks[i], lib) for i in uncovered]
    # A step whose input comes from a held step is held too, and so on down the chain.
    held, blocked = set(uncovered), {}
    changed = True
    while changed:
        changed = False
        for dep, src in needs.items():
            if src in held and dep not in held:
                held.add(dep)
                blocked[dep] = src
                changed = True
    items += [NotAvailable(step=agenda[dep], needs=agenda[src]) for dep, src in sorted(blocked.items())]
    return items, sorted(blocked)


def _index(value: Any, n: int) -> "int | None":
    try:
        i = int(value) - 1
    except (TypeError, ValueError):
        return None
    return i if 0 <= i < n else None


# --- the gate --------------------------------------------------------------------------------------

def hold_code_steps(lab: Any, agenda: "list[str]", emit: EventFn, *, source: str,
                    redrafted: bool = False) -> "list[str]":
    """``agenda`` without the steps that need model-written code, and without the steps that need
    their output, when the deployment does not run it. ``source`` says where the plan came from
    (``draft``, ``plan_review``, ``revision``, ``saved_plan``, ``final``, ``next_cycle``).

    ``lab._unavailable`` is kept as the held steps of the current plan, for the plan card, the reply
    and the skill request. ``redrafted`` says the plan was drafted anew (the first draft, a whole
    redraft in chat): its own held steps replace the earlier ones, so a step the new plan no longer
    has is dropped and a reworded one is listed once. Any other change (the plan review, an edit, a
    patch of one step, the last check, a campaign's next cycle) keeps the steps held so far and adds
    its own. ``steps_unavailable`` is emitted whenever the list changes: ``steps`` are the new
    lines, ``all`` the whole list (empty when a redraft holds nothing)."""
    agenda = list(agenda)
    if model_written_code():
        return agenda
    earlier: "list[NotAvailable]" = list(getattr(lab, "_unavailable", None) or [])
    uncovered = _uncovered(agenda, _plan_of(lab))
    items, held = [], set()
    if uncovered:
        items, dependents = describe(lab, agenda, uncovered)
        held = set(uncovered) | set(dependents)
    current = [] if redrafted else list(earlier)
    for it in items:
        if all(it.step != e.step for e in current):
            current.append(it)
    lab._unavailable = current
    if current != earlier:
        before = {e.step for e in earlier}
        emit({"type": "steps_unavailable", "source": source,
              "steps": [it.to_dict() for it in current if it.step not in before],
              "all": [it.to_dict() for it in current]})
    return [s for i, s in enumerate(agenda) if i not in held]


def nothing_runs(lab: Any, agenda: "list[str]") -> bool:
    """True when the hold left this plan nothing to run: the switch is off, the current plan holds a
    step (``lab._unavailable``), and ``agenda`` has no step but the report step, which would write
    up nothing. A plan that holds no step, the report step alone or none, is not this case."""
    if model_written_code() or not getattr(lab, "_unavailable", None):
        return False
    return not any(not _is_report_step(s) for s in agenda)


def not_available_result(lab: Any, question: str, emit: EventFn) -> LabResult:
    """The run's end when no step of the plan is covered: nothing runs and no job is submitted. The
    answer is the plan card's lines; the gateway says whether the request was recorded."""
    items = list(getattr(lab, "_unavailable", None) or [])
    emit({"type": "plan_not_available", "steps": [it.to_dict() for it in items]})
    return LabResult(question, [], [], False, 0, not_available_markdown(items, recorded=False))


def refuse_code_steps(agenda: "list[str]", plan: "PlanDoc | None",
                      indices: "Iterable[int] | None" = None) -> None:
    """Refuse a plan that would run without review while one of its pending steps needs
    model-written code. Its document and earlier results stay as they are; it is planned again."""
    if model_written_code():
        return
    bad = _uncovered(agenda, plan, indices)
    if not bad:
        return
    from ..mcp_plan_migration import PlanMigrationRequired
    from ..code_policy import step_name
    named = "; ".join(f"step {i + 1} ({step_name(agenda[i])})" for i in bad[:6])
    raise PlanMigrationRequired(
        f"This saved plan cannot run: {named} needs code the AI writes, which this deployment does "
        "not run. Nothing ran, and the plan and its earlier results are unchanged. Ask again to plan "
        "the request anew: a step no tested tool or skill does is then reported as not available.")


def drop_code_alternatives(options: "list[str]") -> "list[str]":
    """Alternatives for a failed step that do not need model-written code."""
    return [o for o in options if not (_declared_tools(o) & WITHHELD_TOOLS)] if not model_written_code() \
        else list(options)


def record(items: "Iterable[NotAvailable]") -> "list[dict[str, Any]]":
    """The held steps as the run's result and a saved plan keep them."""
    return [it.to_dict() for it in items]


def restore(rows: Any) -> "list[NotAvailable]":
    """The held steps a saved plan kept (:func:`record`)."""
    return [NotAvailable.from_dict(r) for r in rows or [] if isinstance(r, dict) and r.get("step")]


__all__ = ["code_step", "describe", "drop_code_alternatives", "hold_code_steps", "needs_code",
           "not_available_result", "nothing_runs", "record", "refuse_code_steps", "restore"]
