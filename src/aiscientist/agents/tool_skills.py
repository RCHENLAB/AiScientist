"""Tools that run the method of a registry skill (``skill:`` in their TOOL.md).

Such a tool keeps no copy of what the skill defines. The skill, from the pinned aiscientist-skills
release (or the transitional local copy), declares the container image and Slurm resources of the
tool's job, and ships the reference settings the tool reads under ``assets/``. This module is the
platform's side of that arrangement:

- :func:`job_environment` gives the image and resources a tool's job runs with;
- :func:`with_skill` attaches the skill to a call (:data:`catalog.SKILL_ARG`), so the tool reads its
  settings from its arguments wherever it runs, and records the skill version in the run's
  manifest.json;
- :func:`attach_skill` wraps a built tool so that every call carries its skill.

The tools never load skills themselves: they ship as their own package and import nothing from
the platform.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from ..tools import catalog


class ToolSkillError(RuntimeError):
    """A tool names a skill that cannot be used: not installed, not an allowed tier, or without a
    valid environment."""


def tool_skill(m: Any) -> Any:
    """The skill that manifest ``m`` names. Re-scans the skill roots first, so an installed or
    updated release is used without a restart."""
    from .skills import get_skill, refresh_skills
    refresh_skills()
    skill = get_skill(m.skill)
    if skill is None:
        raise ToolSkillError(f"{m.name} runs skill {m.skill!r}, which is not installed or not an "
                             "allowed tier in this deployment")
    if not skill.image:
        raise ToolSkillError(f"{m.name}: skill {m.skill!r} declares no usable environment"
                             + (f" ({skill.env_problem})" if skill.env_problem else ""))
    return skill


def job_environment(m: Any) -> dict[str, Any]:
    """The image and Slurm resources a tool's job runs with: those of the skill its manifest names,
    else its own TOOL.md fields ("" / 0 = the line's defaults). Raises :class:`ToolSkillError` for a
    named skill that cannot be used, rather than running the job in the wrong image."""
    src = tool_skill(m) if m.skill else m
    return {"image": src.image, "cpus": src.cpus, "mem_gb": src.mem_gb, "time_limit": src.time_limit}


def with_skill(m: Any, args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """``args`` with the skill that manifest ``m`` names under :data:`catalog.SKILL_ARG`: its name,
    version and release, and its ``assets/`` files. The skill is recorded in the run's
    manifest.json, without becoming the run's active skill. A call that already carries its skill,
    or a tool that names none, is returned unchanged."""
    if not m.skill or isinstance(args.get(catalog.SKILL_ARG), dict):
        return args
    skill = tool_skill(m)
    from .skills_registry import record_in_manifest
    record_in_manifest(ctx, skill)
    return {**args, catalog.SKILL_ARG: {
        "name": skill.name, "version": skill.version or "local",
        "registry_release": skill.registry_release or None,
        "files": {k: v for k, v in sorted(skill.files.items()) if k.startswith("assets/")}}}


def attach_skill(m: Any, tool: Any) -> Any:
    """``tool`` with an executor that attaches its skill to every call, wherever the call then runs
    (an HPC3 executor, or in-process). A skill that cannot be used is reported as the call's error."""
    if not m.skill:
        return tool
    inner = tool.executor

    def _exec(args: dict, ctx: Any, _m=m, _inner=inner) -> dict:
        try:
            args = with_skill(_m, args or {}, ctx)
        except ToolSkillError as exc:
            return {"status": "error", "error": f"{exc}. Install or allow that skill, then run "
                                                f"{_m.name} again."}
        return _inner(args, ctx)

    _exec.__wrapped__ = getattr(inner, "__wrapped__", inner)   # source introspection finds the tool
    try:
        return dataclasses.replace(tool, executor=_exec)
    except TypeError:
        tool.executor = _exec
        return tool
