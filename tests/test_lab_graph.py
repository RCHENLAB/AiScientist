"""The LangGraph execution shell (`planner="langgraph"`).

The interesting part is not "does it run a graph" — it is the constraint LangGraph does NOT have.
LangGraph executes every node whose dependencies are met CONCURRENTLY, while our analysis nodes
share one AnnData checkpoint chain and scanpy's global state and must never overlap. The hand
written scheduler enforced that at dispatch time; a graph has to enforce it as topology.

So these tests pin, in order of importance:
  1. conflicting nodes are never ready at the same time (extra edges are added), while genuinely
     independent work still runs in parallel — the property that makes the port safe at all;
  2. the serialization is pure and cannot deadlock (no cycle is ever introduced);
  3. a compiled graph executes every node and reports rounds in PLAN order, not completion order.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("langgraph")

from aiscientist.agents.dag import LabPlan, TaskNode, _has_cycle  # noqa: E402
from aiscientist.agents.lab_graph import (  # noqa: E402
    build_lab_graph,
    order_rounds,
    serialize_conflicting_nodes,
)


def _plan(*specs: tuple[str, tuple[str, ...]]) -> LabPlan:
    return LabPlan(tuple(TaskNode(id=i, goal=f"goal {i}", depends_on=d) for i, d in specs))


# --- 1. the constraint LangGraph does not have --------------------------------


def test_conflicting_independent_nodes_get_an_edge():
    """Two nodes with nothing between them, that must not co-run, end up ordered."""
    plan = _plan(("qc", ()), ("de", ()), ("lit", ()))
    conflicts = lambda a, b: {a.id, b.id} <= {"qc", "de"}  # noqa: E731

    out, added = serialize_conflicting_nodes(plan, conflicts)

    assert added == [("qc", "de")], "plan order decides the direction"
    assert out.by_id()["de"].depends_on == ("qc",)
    assert out.by_id()["lit"].depends_on == (), "independent work must stay parallel"


def test_nodes_already_ordered_are_left_alone():
    """A real data dependency already prevents the overlap — do not pile on a redundant edge."""
    plan = _plan(("qc", ()), ("de", ("qc",)))

    out, added = serialize_conflicting_nodes(plan, lambda a, b: True)

    assert added == []
    assert out.by_id()["de"].depends_on == ("qc",)


def test_transitively_ordered_nodes_are_left_alone():
    plan = _plan(("a", ()), ("b", ("a",)), ("c", ("b",)))

    _out, added = serialize_conflicting_nodes(plan, lambda x, y: {x.id, y.id} == {"a", "c"})

    assert added == [], "a already precedes c through b"


def test_no_conflicts_returns_the_plan_untouched():
    plan = _plan(("a", ()), ("b", ()))
    out, added = serialize_conflicting_nodes(plan, lambda _a, _b: False)
    assert out is plan and added == []


def test_serialization_never_introduces_a_cycle():
    """A conflict is a performance constraint. Deadlocking the run to honour it would be worse
    than running the two nodes in whatever order the plan already forces."""
    plan = _plan(("a", ()), ("b", ("a",)), ("c", ("b",)))

    out, _added = serialize_conflicting_nodes(plan, lambda _a, _b: True)

    assert not _has_cycle(list(out.nodes))


def test_every_conflicting_pair_ends_up_ordered_in_a_wide_plan():
    """The property that matters, checked over a fan-out: after serialization no two conflicting
    nodes can ever be in the ready set together."""
    plan = _plan(("qc", ()), ("de1", ("qc",)), ("de2", ("qc",)), ("de3", ("qc",)), ("lit", ()))
    analysis = {"qc", "de1", "de2", "de3"}
    conflicts = lambda a, b: a.id in analysis and b.id in analysis  # noqa: E731

    out, _added = serialize_conflicting_nodes(plan, conflicts)

    by_id = out.by_id()

    def precedes(a: str, b: str) -> bool:
        seen, stack = set(), list(by_id[b].depends_on)
        while stack:
            n = stack.pop()
            if n == a:
                return True
            if n not in seen:
                seen.add(n)
                stack.extend(by_id[n].depends_on)
        return False

    for x in analysis:
        for y in analysis:
            if x != y:
                assert precedes(x, y) or precedes(y, x), f"{x} and {y} could still co-run"
    assert by_id["lit"].depends_on == (), "the literature branch stays parallel"


# --- 2. execution -------------------------------------------------------------


class _Round:
    def __init__(self, step: str) -> None:
        self.step = step
        self.specialist = "Generalist"
        self.scientist_result = {"status": "ok"}
        self.verdict = None


def test_graph_runs_every_node_and_orders_rounds_by_plan():
    plan = _plan(("a", ()), ("b", ("a",)), ("c", ("a",)))
    ran: list[str] = []

    def run_node(node, _prior):
        ran.append(node.id)
        return {"node": node, "rounds": [_Round(node.id)], "accepted": True, "cancelled": False,
                "executed": 1}

    app = build_lab_graph(plan, run_node)
    final = app.invoke({"rounds": [], "executed": 0, "accepted": [], "cancelled": []})

    assert sorted(ran) == ["a", "b", "c"]
    assert final["executed"] == 3
    assert sorted(final["accepted"]) == ["a", "b", "c"]
    # Completion order among b and c is scheduling luck; the reported order must not be.
    assert [r.step for r in order_rounds(plan, final["rounds"])] == ["a", "b", "c"]


def test_a_node_sees_the_rounds_of_its_dependencies():
    plan = _plan(("a", ()), ("b", ("a",)))
    seen: dict[str, list[str]] = {}

    def run_node(node, prior):
        seen[node.id] = [r.step for r in prior]
        return {"node": node, "rounds": [_Round(node.id)], "accepted": True, "cancelled": False,
                "executed": 1}

    build_lab_graph(plan, run_node).invoke(
        {"rounds": [], "executed": 0, "accepted": [], "cancelled": []})

    assert seen["a"] == []
    assert seen["b"] == ["a"], "downstream work must see upstream findings"


def test_a_cancel_stops_the_remaining_nodes():
    plan = _plan(("a", ()), ("b", ("a",)), ("c", ("b",)))
    ran: list[str] = []

    def run_node(node, _prior):
        ran.append(node.id)
        cancelled = node.id == "a"
        return {"node": node, "rounds": [], "accepted": not cancelled, "cancelled": cancelled,
                "executed": 1}

    final = build_lab_graph(plan, run_node).invoke(
        {"rounds": [], "executed": 0, "accepted": [], "cancelled": []})

    assert ran == ["a"], "b and c must not do work after a cancel"
    assert final["cancelled"] == ["a"]


def test_research_lab_runs_end_to_end_on_the_langgraph_planner(tmp_path):
    """`planner="langgraph"` is a real execution path, not just a module: the lab plans, runs every
    step through the SAME Scientist/Critic node code, and produces the same LabResult shape."""
    import json

    from aiscientist.agents.research_harness import HarnessContext, HarnessResult
    from aiscientist.agents.research_lab import LabConfig, ResearchLab

    agenda = ["Step 1: QC the cells", "Step 2: differential expression"]

    def complete_fn(messages):
        system = messages[0]["content"]
        if "Critic" in system:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        if "final research report" in system:
            return "The report."
        return json.dumps({"agenda": agenda})

    class _Scientist:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

        def run(self, *_a, **_k):
            return HarnessResult(
                status="ok", stop_reason=None, final_answer="did it",
                steps=[{"tool": "run_scanpy_qc", "args": {}, "ok": True, "summary": "ok",
                        "result": {"status": "ok", "figures": ["figures/a.png"]}}],
                errors=[])

    lab = ResearchLab(
        HarnessContext(decisions={}, workspace=tmp_path),
        LabConfig(planner="langgraph", max_steps=2, auto_select_skill=False),
        complete_fn=complete_fn, scientist=_Scientist())

    result = lab.run("compare KO vs WT")

    assert result.accepted_steps == 2
    assert len(result.rounds) == 2
    assert [r.step for r in result.rounds] == agenda, "plan order, not completion order"
    assert result.final_answer == "The report."
    # Round numbering is renormalised, so the report never shows gaps or duplicates.
    assert [r.round_no for r in result.rounds] == [1, 2]


def test_serialized_conflicting_nodes_never_execute_concurrently():
    """End to end: with the added edges in place, two conflicting nodes cannot overlap in time
    even though LangGraph would happily co-run them."""
    plan = _plan(("qc", ()), ("de", ()), ("lit", ()))
    analysis = {"qc", "de"}
    graph_plan, _ = serialize_conflicting_nodes(
        plan, lambda a, b: a.id in analysis and b.id in analysis)

    live: set[str] = set()
    overlaps: list[tuple[str, ...]] = []
    lock = threading.Lock()

    def run_node(node, _prior):
        with lock:
            if node.id in analysis and any(o in analysis for o in live):
                overlaps.append(tuple(sorted(live | {node.id})))
            live.add(node.id)
        try:
            return {"node": node, "rounds": [_Round(node.id)], "accepted": True,
                    "cancelled": False, "executed": 1}
        finally:
            with lock:
                live.discard(node.id)

    build_lab_graph(graph_plan, run_node).invoke(
        {"rounds": [], "executed": 0, "accepted": [], "cancelled": []})

    assert overlaps == [], f"conflicting nodes overlapped: {overlaps}"
