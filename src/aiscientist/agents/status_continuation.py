"""Narrow, typed recovery of existing MCP tasks; never a new scientific submission."""
from __future__ import annotations

import json
import re
from dataclasses import replace

from .mcp_bridge import configured_servers
from .plan_doc import PlanDoc, PlanStep, parse_plan_yaml
from .tool_names import STATUS_TOOLS, canonical_tool_name

_LINE = re.compile(r"(?m)^\s*s(\d+)\s*:\s*([A-Za-z_][A-Za-z0-9_-]*)\s+(.+?)\s*$")
_RUN = re.compile(r"\brun(?:_id)?\s*[:=]?\s*[`\"']?([a-f0-9]{12})\b", re.I)


def requested(text: str) -> bool:
    """An explicit numbered status recovery must be handled or refused, never re-routed."""
    return bool(re.search(r"(?mi)^\s*s\d+\s*:.*\b\w*job_status\b", text))


def target_run(text: str) -> str:
    matches = set(_RUN.findall(text))
    if len(matches) != 1:
        raise ValueError("Status recovery needs exactly one explicit original run ID.")
    return matches.pop().lower()


def status_server(name: str, settings: dict):
    """Recognize only an owned analysis-adapter status descriptor with valid complete args."""
    for server in configured_servers():
        if server.adapter != "analysis":
            continue
        for entry in server.tools:
            if server.planner_name(entry["name"]) != name:
                continue
            if entry["name"] not in STATUS_TOOLS or canonical_tool_name(name) != entry["name"]:
                break
            if (not isinstance(settings, dict) or set(settings) != {"analysis_id", "task_id"}
                    or any(not isinstance(v, str) or not v for v in settings.values())):
                raise ValueError("Status recovery requires only nonempty analysis_id and task_id.")
            from jsonschema import Draft202012Validator
            error = next(Draft202012Validator(entry["inputSchema"]).iter_errors(settings), None)
            if error:
                raise ValueError("Status recovery arguments do not match the registered schema: " + error.message)
            return server
    raise ValueError(f"Status recovery cannot execute unregistered or non-status tool {name!r}.")


def _rounds(state: dict) -> dict:
    out = {}
    for rd in state.get("rounds", []):
        if not isinstance(rd, dict) or rd.get("verdict", {}).get("verdict") != "accept":
            continue
        index = rd.get("step_index")
        if type(index) is int and index > 0:
            out[index - 1] = rd
    return out


def validate_tasks(state: dict, plan: PlanDoc, indices: frozenset[int]) -> None:
    """Each selected call must name a task actually returned by that original accepted step."""
    if not indices or any(type(i) is not int or not 0 <= i < len(plan.steps) for i in indices):
        raise ValueError("Status recovery step indices are invalid.")
    rounds = _rounds(state)
    if set(range(len(plan.steps))) - rounds.keys():
        raise ValueError("Status recovery requires accepted evidence for every retained original step.")
    for i in indices:
        step = plan.steps[i]
        server = status_server(step.tool, step.settings)
        owned_names = {server.planner_name(t["name"]) for t in server.tools}
        found = False
        for call in rounds[i].get("scientist_result", {}).get("steps", []):
            result = call.get("result", {})
            if (call.get("tool") in owned_names and isinstance(result, dict)
                    and isinstance(result.get("mcp"), dict)
                    and result["mcp"].get("name") == server.name
                    and result.get("state") == "COMPLETED"
                    and result.get("analysis_id") == step.settings["analysis_id"]
                    and result.get("task_id") == step.settings["task_id"]):
                found = True
                break
        if not found:
            raise ValueError(f"Step {step.id} has no matching completed original task evidence in this run.")


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate status argument " + name)
        result[name] = value
    return result


def revised_state(state: dict, text: str) -> tuple[dict, frozenset[int]]:
    """Build a review draft from explicit sN: tool {JSON} lines; keep all other steps exact."""
    if len(text) > 20_000:
        raise ValueError("Status recovery instructions exceed the review limit.")
    target_run(text)
    plan_yaml = state.get("approved_plan_yaml", "")
    review = state.get("resume_review")
    if not plan_yaml and isinstance(review, dict) and review.get("status_recovery"):
        # A paused/failed review stores the draft in its durable resume inputs, not
        # the top-level approved-plan slot. Only an unchanged accepted run may reuse it.
        if (review.get("agenda") != state.get("agenda") or review.get("rounds") != state.get("rounds")
                or ("question" in review and review["question"] != state.get("question"))):
            raise ValueError("The saved recovery review no longer matches its original run.")
        plan_yaml = review.get("approved_plan_yaml", "")
    plan = parse_plan_yaml(plan_yaml, request=state.get("question", ""))
    if not plan.steps or plan.agenda() != state.get("agenda"):
        raise ValueError("Status recovery requires the original matching typed plan.")
    numbered = re.findall(r"(?m)^\s*s\d+\s*:.*$", text)
    calls = list(_LINE.finditer(text))
    if not calls or len(calls) != len(numbered):
        raise ValueError("Every replacement must be sN: registered_status_tool {JSON} on one line.")
    steps, indices = list(plan.steps), set()
    for call in calls:
        i = int(call[1]) - 1
        if i in indices or not 0 <= i < len(steps) or steps[i].id != f"s{i + 1}":
            raise ValueError("Status recovery has a duplicate or unknown step number.")
        try:
            args = json.loads(call[3], object_pairs_hook=_unique_object)
        except ValueError as exc:
            raise ValueError("Status recovery arguments must be a complete JSON object.") from exc
        server = status_server(call[2], args)
        steps[i] = PlanStep(steps[i].id,
            f"**{i + 1}. Recover existing task evidence** — Read `{call[2]}` with the exact JSON "
            "settings shown. Preserve the original scientific result; do not submit a new analysis.",
            tool=call[2], settings=args, runs_on=server.runs_on, skill=server.skill)
        indices.add(i)
    updated = replace(plan, steps=tuple(steps))
    validate_tasks(state, updated, frozenset(indices))
    return {**state, "agenda": updated.agenda(), "approved_plan_yaml": updated.to_yaml()}, frozenset(indices)


def validate_review(original: PlanDoc, updated: PlanDoc, indices: frozenset[int]) -> None:
    """Approval cannot turn a checkpoint-free recovery into new science or a different task."""
    if original.request != updated.request or len(original.steps) != len(updated.steps):
        raise ValueError("Status recovery must preserve the original question and step count.")
    for i, (old, new) in enumerate(zip(original.steps, updated.steps)):
        if i not in indices:
            # runs_on is discarded by the parser and regenerated from the trusted registry.
            if replace(old, runs_on="") != replace(new, runs_on=""):
                raise ValueError("Status recovery must preserve every retained step unchanged.")
        elif (old.id, old.tool, old.settings, old.skill) != (
                new.id, new.tool, new.settings, new.skill):
            raise ValueError("Status recovery cannot change the verified task or execution identity.")
        if i in indices:
            status_server(new.tool, new.settings)
