"""Dependency-DAG plan model for the research lab (branch: feat/dag-planner).

The linear planner emits ``agenda: list[str]`` and ``_run_loop`` walks it in order. This module
adds the DAG model that lets the PI express REAL dependencies between tasks (so the scheduler can
reuse finished work, scope each task, and — with a Coordinator — choose the path), while staying
100% back-compatible: a flat agenda lifts into a linear DAG via :func:`lift_agenda_to_dag`, so
nothing downstream breaks when the DAG planner is off.

Pure data + helpers only — no LLM, no I/O — so it is trivially unit-testable and portable to a
future LangGraph port.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str, fallback: str) -> str:
    s = _SLUG_RE.sub("_", str(text or "").strip().lower()).strip("_")
    return s or fallback


@dataclass(frozen=True)
class TaskNode:
    """One task in the plan. ``depends_on`` are node ids that must be DONE before this can run;
    ``consumes``/``produces`` are the checkpoint/artifact names it reads/writes (used to scope the
    task brief and let the Critic check the output actually appeared). ``suggested_tool`` steers the
    Scientist (not enforced). ``decision`` marks a human-in-the-loop decision point."""

    id: str
    goal: str
    depends_on: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    suggested_tool: str | None = None
    decision: bool = False
    options: tuple[str, ...] = ()   # concrete choices offered to the human at a decision node

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "goal": self.goal}
        if self.depends_on:
            d["depends_on"] = list(self.depends_on)
        if self.consumes:
            d["consumes"] = list(self.consumes)
        if self.produces:
            d["produces"] = list(self.produces)
        if self.suggested_tool:
            d["suggested_tool"] = self.suggested_tool
        if self.decision:
            d["decision"] = True
        if self.options:
            d["options"] = list(self.options)
        return d


@dataclass(frozen=True)
class LabPlan:
    """An ordered set of TaskNodes forming a DAG (validated acyclic + deps resolvable at build)."""

    nodes: tuple[TaskNode, ...]

    def by_id(self) -> dict[str, TaskNode]:
        return {n.id: n for n in self.nodes}

    def goals(self) -> list[str]:
        return [n.goal for n in self.nodes]

    def ready_ids(self, done_ids: "set[str] | frozenset[str]") -> list[str]:
        """Ids whose every dependency is DONE and which are not themselves done yet, in plan order.
        'Done' means terminal (accepted OR force-advanced) — dependency satisfaction is structural;
        answer quality is the Critic's separate concern, matching the lenient linear loop."""
        return [
            n.id for n in self.nodes
            if n.id not in done_ids and all(d in done_ids for d in n.depends_on)
        ]

    def to_list(self) -> list[dict[str, Any]]:
        return [n.to_dict() for n in self.nodes]

    def next_id(self, prefix: str = "x") -> str:
        """An id not already used by this plan — for a node ADDED mid-run (hypothesis-driven
        exploration). Prefixed distinctly (default ``x1``, ``x2``, …) so a discovered task is
        visibly not one of the originally planned ``s1``/slug nodes."""
        used = {n.id for n in self.nodes}
        i = 1
        while f"{prefix}{i}" in used:
            i += 1
        return f"{prefix}{i}"

    def extend(self, new_nodes: "list[TaskNode]") -> "LabPlan":
        """A NEW plan with ``new_nodes`` appended — the DAG's only growth path, used when a step's
        result opens a research path the original plan does not cover.

        Frozen-dataclass by design: the scheduler rebinds its ``plan`` local, so a half-built graph
        can never be observed. Nodes with a duplicate id, a dependency on an unknown id, or that
        would close a cycle are DROPPED (never raises) — a garbled model proposal degrades to "no
        new work", which is exactly the pre-existing behaviour."""
        known = {n.id for n in self.nodes}
        kept: list[TaskNode] = []
        for nd in new_nodes:
            if not nd.goal.strip() or nd.id in known:
                continue
            deps = tuple(d for d in nd.depends_on if d in known)
            kept.append(TaskNode(id=nd.id, goal=nd.goal, depends_on=deps, consumes=nd.consumes,
                                 produces=nd.produces, suggested_tool=nd.suggested_tool,
                                 decision=nd.decision, options=nd.options))
            known.add(nd.id)
        if not kept:
            return self
        merged = [*self.nodes, *kept]
        if _has_cycle(merged):
            return self
        return LabPlan(tuple(merged))


def lift_agenda_to_dag(agenda: "list[str]") -> LabPlan:
    """Back-compat: a flat agenda becomes a LINEAR DAG (node i depends on node i-1). The scheduler
    then reproduces the linear loop's order exactly, so turning the DAG planner on for a flat plan
    changes nothing observable."""
    nodes: list[TaskNode] = []
    prev: str | None = None
    for i, step in enumerate(agenda):
        nid = f"s{i + 1}"
        nodes.append(TaskNode(id=nid, goal=str(step), depends_on=(prev,) if prev else ()))
        prev = nid
    return LabPlan(tuple(nodes))


def _has_cycle(nodes: list[TaskNode]) -> bool:
    graph = {n.id: [d for d in n.depends_on if d in {m.id for m in nodes}] for n in nodes}
    WHITE, GREY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in graph}

    def visit(nid: str) -> bool:
        color[nid] = GREY
        for dep in graph[nid]:
            if color[dep] == GREY:
                return True
            if color[dep] == WHITE and visit(dep):
                return True
        color[nid] = BLACK
        return False

    return any(color[nid] == WHITE and visit(nid) for nid in graph)


def _coerce_str_list(v: Any) -> tuple[str, ...]:
    if isinstance(v, str):
        return (v,) if v.strip() else ()
    if isinstance(v, list):
        return tuple(str(x).strip() for x in v if str(x).strip())
    return ()


def parse_dag(raw: str, *, max_nodes: int = 12) -> LabPlan | None:
    """Parse the PI's DAG reply into a validated LabPlan, or None on any failure so the caller can
    fall back to lifting a flat agenda. Tolerates a ``{"nodes": [...]}`` / ``{"agenda": [...]}`` /
    bare-array reply in prose or code fences. Sanitizes ids to slugs, drops dependency references to
    unknown ids, caps node count, and REJECTS a cyclic graph (returns None)."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].strip()
    parsed: Any = None
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"[\[{].*[\]}]", s, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    if isinstance(parsed, dict):
        parsed = parsed.get("nodes") or parsed.get("agenda") or parsed.get("tasks")
    if not isinstance(parsed, list) or not parsed:
        return None

    # A plain agenda of strings → lift to a linear DAG (the model answered the old way).
    if all(isinstance(x, str) for x in parsed):
        return lift_agenda_to_dag([x for x in parsed if x.strip()][:max_nodes])

    raw_nodes = [x for x in parsed if isinstance(x, dict)][:max_nodes]
    if not raw_nodes:
        return None

    # First pass: assign unique ids (sanitized; de-duplicated).
    ids: list[str] = []
    seen: set[str] = set()
    for i, nd in enumerate(raw_nodes):
        base = _slug(nd.get("id") or nd.get("goal") or "", f"n{i + 1}")[:40]
        nid = base
        k = 2
        while nid in seen:
            nid, k = f"{base}_{k}", k + 1
        seen.add(nid)
        ids.append(nid)

    nodes: list[TaskNode] = []
    for i, nd in enumerate(raw_nodes):
        goal = str(nd.get("goal") or nd.get("step") or nd.get("description") or "").strip()
        if not goal:
            continue
        deps = tuple(d for d in _coerce_str_list(nd.get("depends_on") or nd.get("deps"))
                     if d in seen and d != ids[i])   # drop unknown / self references
        tool = nd.get("suggested_tool") or nd.get("tool")
        nodes.append(TaskNode(
            id=ids[i], goal=goal, depends_on=deps,
            consumes=_coerce_str_list(nd.get("consumes")),
            produces=_coerce_str_list(nd.get("produces")),
            suggested_tool=str(tool).strip() if tool else None,
            decision=bool(nd.get("decision", False)),
            options=_coerce_str_list(nd.get("options")),
        ))
    if not nodes or _has_cycle(nodes):
        return None
    return LabPlan(tuple(nodes))
