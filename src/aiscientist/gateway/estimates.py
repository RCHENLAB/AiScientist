"""Plan estimates and durable compute usage, independent of the planning model."""
from __future__ import annotations

import contextvars
import functools
import json
import logging
import math
import re
import statistics
import time
from dataclasses import replace

from sqlalchemy import func, select

from ..tools import catalog
from .db import session_scope
from .models import ComputeEstimate, ComputeUsage

log = logging.getLogger(__name__)
# A tool can submit several batch jobs; accounting belongs to that call, even with DAG threads.
job_usage: contextvars.ContextVar[list | None] = contextvars.ContextVar("job_usage", default=None)
FIELDS = ("seconds", "cpu_hours", "gpu_hours", "cost")
HISTORY_RUNS = 30
# A run faster than this share of the median did not do the same work, so it never sets the low end.
LOW_OUTLIER = 0.2


def profile(skill: str) -> dict:
    declared = catalog.resource_profile(skill)
    manifest = catalog.manifest(skill)
    from ..agents.tool_skills import ToolSkillError, job_environment
    try:
        env = job_environment(manifest) if manifest else {}
    except ToolSkillError:
        env = {}                          # an estimate is never a reason to fail a plan
    if declared:
        # A step may leave its CPUs and memory to the job's environment (TOOL.md, or the skill it
        # names), so the resources are declared once and the profile only holds the timings.
        return {**declared, "steps": [{"cpus": env.get("cpus") or 1, "mem_gb": env.get("mem_gb") or 8,
                                       **step} for step in declared["steps"]]}
    return {"unit": "cells", "baseline": 10000, "rough": True, "steps": [
        {"name": skill, "cpus": env.get("cpus") or 1,
         "gpus": 1 if manifest and manifest.runs_on.startswith("gpu:") else 0,
         "mem_gb": env.get("mem_gb") or 8, "minutes": [5, 30]}]}


def input_units(p: dict, inputs: dict) -> tuple[float, bool]:
    key = p.get("unit", "samples")
    aliases = {"samples": ("samples", "n_samples"), "cells": ("cells", "n_cells"),
               "gb": ("gb", "size_gb")}
    sources = [inputs, inputs.get("dataset_result") or {}]
    layout = inputs.get("cellranger_layout") or sources[1].get("cellranger_layout") or {}
    if key == "samples" and layout.get("n_libraries"):
        sources.append({"samples": layout["n_libraries"]})
    for source in sources:
        for name in aliases.get(key, (key,)):
            value = source.get(name)
            if isinstance(value, (list, dict)):
                value = len(value)
            try:
                value = float(value)
                if math.isfinite(value) and value > 0:
                    return value, False
            except (ValueError, TypeError):
                pass
        size_bytes = source.get("size_bytes") or source.get("file_size_bytes")
        if key == "gb" and size_bytes:
            return float(size_bytes) / 1024**3, False
    return float(p.get("baseline", 1)), True


def history(skill: str) -> list[ComputeUsage]:
    """Successful calls of ``skill`` in the ``HISTORY_RUNS`` runs that called it most recently."""
    ok = (ComputeUsage.skill == skill, ComputeUsage.success.is_(True))
    recent = (select(ComputeUsage.run_id).where(*ok).group_by(ComputeUsage.run_id)
              .order_by(func.max(ComputeUsage.id).desc()).limit(HISTORY_RUNS))
    with session_scope() as db:
        return list(db.scalars(select(ComputeUsage).where(*ok, ComputeUsage.run_id.in_(recent))))


def per_run(rows: list) -> list:
    """One observation per run, its slowest call per unit. A run's later calls of the same tool are
    mostly re-runs that reuse earlier outputs (CellQC's Snakemake skips the rules already done), so
    they are not separate measurements of the work."""
    slowest = {}
    for r in rows:
        kept = slowest.get(r.run_id)
        if r.units > 0 and (kept is None or r.seconds / r.units > kept.seconds / kept.units):
            slowest[r.run_id] = r
    return list(slowest.values())


def estimate_skill(skill: str, inputs: dict, settings, *, previous=None) -> dict:
    p = profile(skill)
    units, unknown_size = input_units(p, inputs)
    factor = units / p.get("baseline", 1)
    rows = per_run(history(skill) if previous is None else previous)
    ranges = {key: [0.0, 0.0] for key in FIELDS}
    for step in p["steps"]:
        for i, minutes in enumerate(step["minutes"]):
            seconds = minutes * 60 * factor
            ranges["seconds"][i] += seconds
            ranges["cpu_hours"][i] += seconds / 3600 * step["cpus"]
            ranges["gpu_hours"][i] += seconds / 3600 * step.get("gpus", 0)
    # Resource hours are learned separately: elapsed time alone cannot predict changing allocations.
    if rows:
        for key in ("seconds", "cpu_hours", "gpu_hours"):
            values = [getattr(r, key) / r.units * units for r in rows]
            if values:
                center = statistics.median(values)
                # A far faster run (outputs reused, or stopped early while reporting success) does
                # not set the low end. Ignoring a fast real run costs little: the low end stays at
                # most 3/4 of the median.
                typical = [v for v in values if v >= center * LOW_OUTLIER]
                ranges[key] = [min(min(typical), center * .75), max(max(values), center * 1.5)]
    ranges["cost"] = [ranges["cpu_hours"][i] * settings.cpu_hour_rate
                      + ranges["gpu_hours"][i] * settings.gpu_hour_rate for i in range(2)]
    return {"skill": skill, **ranges, "rough": bool(p.get("rough")) or unknown_size or len(rows) < 3,
            "profile_steps": p["steps"], "units": units, "history_count": len(rows)}


def estimate_plan(agenda: list[str], tools: list, inputs: dict, settings,
                  step_tools: list | None = None) -> dict:
    """``step_tools`` is the tool the plan document names for each step, when it names one; a step
    without it is matched by the tool names its text mentions."""
    names = {t.name for t in tools}
    steps = []
    for number, text in enumerate(agenda, 1):
        named = (step_tools[number - 1] if step_tools and len(step_tools) == len(agenda) else None) or ""
        matched = [named] if named in names and named != "finish" else [
            t.name for t in tools if t.name != "finish" and
            re.search(r"(?<![\w])" + re.escape(t.name) + r"(?![\w])", text)]
        estimates = [estimate_skill(name, inputs, settings) for name in matched or ["unspecified"]]
        row = {key: [sum(e[key][i] for e in estimates) for i in range(2)] for key in FIELDS}
        steps.append({"step": number, "skill": ", ".join(matched) or "Unspecified",
                      **row, "rough": any(e["rough"] for e in estimates)})
    total = {key: [sum(e[key][i] for e in steps) for i in range(2)] for key in FIELDS}
    return {"steps": steps, "total": {**total, "rough": any(e["rough"] for e in steps)},
            "currency": "USD", "rates_configured": bool(settings.cpu_hour_rate or settings.gpu_hour_rate),
            "note": "HPC3 queue wait time is not included. Sequential compute time; parallel steps may overlap."}


def save_estimate(run_id: str, estimate: dict) -> None:
    with session_scope() as db:
        db.merge(ComputeEstimate(run_id=run_id, payload=json.dumps(estimate)))
        db.commit()


def record_usage(run_id: str, skill: str, inputs: dict, seconds: float, settings,
                 *, success: bool, jobs: list | None = None) -> dict:
    p = profile(skill)
    units, _ = input_units(p, inputs)
    if jobs:
        seconds = sum(j["seconds"] for j in jobs)
        cpu_hours = sum(j.get("cpu_hours", j["seconds"] / 3600 * max(s["cpus"] for s in p["steps"]))
                        for j in jobs)
        gpu_hours = sum(j.get("gpu_hours", j["seconds"] / 3600 * max(s.get("gpus", 0) for s in p["steps"]))
                        for j in jobs)
        source = "wall_clock" if any(j.get("source") == "wall_clock" for j in jobs) else "sacct"
    else:
        # Without allocation accounting these are requested-resource approximations, labelled rough.
        cpus = max(s["cpus"] for s in p["steps"])
        gpus = max(s.get("gpus", 0) for s in p["steps"])
        cpu_hours, gpu_hours = seconds / 3600 * cpus, seconds / 3600 * gpus
        source = "wall_clock"
    cost = cpu_hours * settings.cpu_hour_rate + gpu_hours * settings.gpu_hour_rate
    with session_scope() as db:
        db.add(ComputeUsage(run_id=run_id, skill=skill, units=units, seconds=seconds,
                            cpu_hours=cpu_hours, gpu_hours=gpu_hours, cost=cost,
                            source=source, success=success))
        db.commit()
    return {"seconds": seconds, "cpu_hours": cpu_hours, "gpu_hours": gpu_hours,
            "cost": cost, "rough": source != "sacct", "source": source}


def run_usage(run_id: str) -> dict:
    return runs_usage([run_id])[run_id]


def runs_usage(run_ids: list[str]) -> dict[str, dict]:
    """:func:`run_usage` of many runs in two queries (the runs list asks for all of a user's runs)."""
    ids = list(dict.fromkeys(run_ids))
    out = {rid: {"estimate": None, "actual": {**{key: 0 for key in FIELDS}, "rough": False, "calls": 0}}
           for rid in ids}
    if not ids:
        return out
    with session_scope() as db:
        for est in db.scalars(select(ComputeEstimate).where(ComputeEstimate.run_id.in_(ids))):
            out[est.run_id]["estimate"] = json.loads(est.payload)
        for r in db.scalars(select(ComputeUsage).where(ComputeUsage.run_id.in_(ids))):
            actual = out[r.run_id]["actual"]
            for key in FIELDS:
                actual[key] += getattr(r, key)
            actual["rough"] = actual["rough"] or r.source != "sacct"
            actual["calls"] += 1
    return out


def instrument_catalog(tools: list, run_id: str, settings) -> list:
    # Auth-free deployments use the same DB for accounting even without account tables initialized.
    from .db import get_engine

    try:
        ComputeEstimate.__table__.create(get_engine(), checkfirst=True)
        ComputeUsage.__table__.create(get_engine(), checkfirst=True)
    except Exception:  # no database: the run goes ahead, only unmeasured
        log.exception("Compute usage is not recorded for run %s", run_id)
        return tools

    def instrument(tool):
        @functools.wraps(tool.executor)
        def execute(args, ctx):
            started = time.monotonic()
            jobs = []
            token = job_usage.set(jobs)
            success = False
            try:
                result = tool.executor(args, ctx)
                success = not (isinstance(result, dict) and (
                    result.get("error") or str(result.get("status", "ok")).lower().startswith(
                        ("error", "fail", "blocked", "cancelled", "not_", "dependency_missing"))))
                return result
            finally:
                job_usage.reset(token)
                try:
                    record_usage(run_id, tool.name, {**ctx.decisions, **args},
                                 max(0, time.monotonic() - started), settings, success=success, jobs=jobs)
                except Exception:  # accounting must never fail an analysis
                    log.exception("Could not record compute usage for %s", tool.name)
        return replace(tool, executor=execute)
    return [instrument(t) if t.name != "finish" else t for t in tools]
