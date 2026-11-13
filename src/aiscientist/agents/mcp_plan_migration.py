"""Refuse stale executable plans without rewriting their approved scientific settings."""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .tool_names import STATUS_TOOLS, _ANALYSIS_TOOLS

if TYPE_CHECKING:
    from .plan_doc import PlanDoc

_LEGACY = frozenset().union(*_ANALYSIS_TOOLS.values()) - STATUS_TOOLS
_WORD = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z_][A-Za-z0-9_-]*(?![A-Za-z0-9_-])")


class PlanMigrationRequired(ValueError):
    """The old document remains intact; a replacement must pass normal plan review."""

    requires_replan = True


def assert_plan_available(agenda: list[str], catalog: Iterable[Any], *,
                          plan: PlanDoc | None = None,
                          indices: Iterable[int] | None = None) -> None:
    """Check pending steps only, using exact identities from the reviewed registry.

    A renamed tool is a different implementation, not a spelling correction. We do
    not rebind it or ask the Scientist to recreate it with generic code. Accepted
    historical results may still be reused without their old executable installed.
    """
    from .mcp_bridge import configured_servers

    live = {tool.name for tool in catalog}
    replacements: dict[str, set[str]] = {}
    for server in configured_servers():
        if server.adapter != "analysis":
            continue
        owned = _ANALYSIS_TOOLS.get(server.name, ())
        for tool in server.tools:
            wire = tool["name"]
            public = server.planner_name(wire)
            if wire in owned and public in live:
                replacements.setdefault(wire, set()).add(public)
    problems = []
    for index in (range(len(agenda)) if indices is None else sorted(indices)):
        step = agenda[index]
        declared = plan.step_for(step) if plan is not None else None
        # A typed document is authoritative; incidental mentions of old tools in
        # explanatory prose do not change the tool the user actually approved.
        names = {declared.tool} if declared is not None and declared.tool else {
            name for name in _WORD.findall(step) if name in _LEGACY or name.startswith("mcp_")}
        missing = sorted(name for name in names if name not in live
                         and (name in _LEGACY or name.startswith("mcp_")))
        for name in missing:
            choices = sorted(replacements.get(name, ()))
            replacement = (" Available registered replacement: " + ", ".join(choices) + "."
                           if choices else " No reviewed replacement is available in this session.")
            problems.append(f"Step {index + 1} names unavailable tool {name}.{replacement}")
    if problems:
        raise PlanMigrationRequired(
            "This saved plan requires re-planning and review before execution. "
            + " ".join(problems)
            + " Its approved settings and previous results have not been changed; "
            "create and review a replacement plan using the current registered tools.")


def assert_resume_document(agenda: list[str], plan: PlanDoc | None,
                           indices: Iterable[int], modify_note: str = "") -> None:
    """Do not silently drop typed settings when a resume changes a step's prose."""
    if plan is None:
        return
    pending = [agenda[i] for i in indices]
    if any(plan.step_for(step) is None for step in pending) or (modify_note.strip() and pending):
        raise PlanMigrationRequired(
            "This saved plan requires re-planning and review: a resumed step was edited or has "
            "new instructions while its approved tool settings are pinned. Edit and review the "
            "plan document instead; the original settings and previous results are preserved.")
