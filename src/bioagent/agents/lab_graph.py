"""LangGraph execution shell for the research DAG — the orchestration layer, and ONLY that.

The split this module encodes: LangGraph owns nodes, edges, state and checkpointing; everything
that makes the runs scientific stays ours and is called unchanged from inside the graph nodes —
:meth:`ResearchLab._run_one_node` (Scientist → Critic → revise), the context compaction, the
HPC/Slurm executors, the evidence-grounding layer. Nothing about the science moves into LangGraph,
and nothing here re-implements it.

WHY THIS IS NOT A DROP-IN
-------------------------
LangGraph runs every node whose dependencies are satisfied CONCURRENTLY. Our nodes cannot always
allow that: scanpy carries global state and the analysis nodes read and write a shared checkpoint
chain (``work/adata_*.h5ad``), so two analysis nodes running at once corrupt each other. The hand
written scheduler enforced that at dispatch time with ``_concurrency_safe`` — a runtime check
LangGraph has no equivalent of.

The fix is to express the constraint where LangGraph *can* see it: as edges.
:func:`serialize_conflicting_nodes` adds a dependency between any two nodes that must not co-run,
so the graph's own topology makes the unsafe interleaving unreachable. Independent work (a
literature branch alongside the analysis chain) still runs in parallel, exactly as before. That
function is pure — no LLM, no I/O — so the guarantee is unit-testable on its own.

STATUS: opt-in via ``LabConfig.planner = "langgraph"``. The hand-written ``_run_dag`` remains the
tested default and is untouched, so this can be exercised without any risk to a deployed run.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Callable, TypedDict

from .dag import LabPlan, TaskNode, _has_cycle

EmitFn = Callable[[dict], None]

# The graph's dependency edges are read from ``TaskNode.depends_on``. An edge ADDED to serialize a
# resource conflict is indistinguishable from a real data dependency at execution time (both just
# mean "not before"), so the added ones are reported separately for the UI/logs rather than being
# silently folded into the plan the user reviewed.


def _reachable(plan: LabPlan) -> dict[str, set[str]]:
    """id -> every id it transitively depends on."""
    by_id = plan.by_id()
    memo: dict[str, set[str]] = {}

    def walk(nid: str, seen: set[str]) -> set[str]:
        if nid in memo:
            return memo[nid]
        if nid in seen:                      # defensive: parse_dag rejects cycles, but never loop
            return set()
        out: set[str] = set()
        for dep in by_id[nid].depends_on:
            if dep in by_id:
                out.add(dep)
                out |= walk(dep, seen | {nid})
        memo[nid] = out
        return out

    return {nid: walk(nid, set()) for nid in by_id}


def serialize_conflicting_nodes(
    plan: LabPlan, conflicts: "Callable[[TaskNode, TaskNode], bool]",
) -> "tuple[LabPlan, list[tuple[str, str]]]":
    """Add the edges that make an unsafe co-run structurally impossible.

    ``conflicts(a, b)`` is True when a and b must NOT run at the same time (for the lab: their
    resource footprints overlap — the shared AnnData checkpoint chain, scanpy's global state).
    For every conflicting pair that is not ALREADY ordered by a transitive dependency, an edge is
    added from the earlier node in plan order to the later one. Plan order is the PI's own ordering,
    so the serialization follows the intended sequence rather than an arbitrary one.

    Returns the new plan and the list of ``(from_id, to_id)`` edges that were added. An edge that
    would close a cycle is skipped — a conflict is a performance constraint, and refusing to run at
    all would be a worse answer than running those two nodes in whatever order the plan already
    forces. Pure: no LLM, no I/O.
    """
    nodes = list(plan.nodes)
    index = {n.id: i for i, n in enumerate(nodes)}
    deps: dict[str, list[str]] = {n.id: list(n.depends_on) for n in nodes}
    added: list[tuple[str, str]] = []

    def ordered(a: str, b: str, table: dict[str, set[str]]) -> bool:
        """True when a and b can never be ready at the same time (one precedes the other)."""
        return a in table.get(b, set()) or b in table.get(a, set())

    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            table = _reachable(LabPlan(tuple(
                TaskNode(id=n.id, goal=n.goal, depends_on=tuple(deps[n.id]), consumes=n.consumes,
                         produces=n.produces, suggested_tool=n.suggested_tool,
                         decision=n.decision, options=n.options) for n in nodes)))
            if ordered(a.id, b.id, table) or not conflicts(a, b):
                continue
            first, second = (a.id, b.id) if index[a.id] < index[b.id] else (b.id, a.id)
            candidate = {**deps, second: [*deps[second], first]}
            probe = [TaskNode(id=n.id, goal=n.goal, depends_on=tuple(candidate[n.id]))
                     for n in nodes]
            if _has_cycle(probe):
                continue
            deps = candidate
            added.append((first, second))

    if not added:
        return plan, []
    rebuilt = tuple(
        TaskNode(id=n.id, goal=n.goal, depends_on=tuple(deps[n.id]), consumes=n.consumes,
                 produces=n.produces, suggested_tool=n.suggested_tool, decision=n.decision,
                 options=n.options)
        for n in nodes
    )
    return LabPlan(rebuilt), added


class LabGraphState(TypedDict, total=False):
    """What flows through the graph.

    Every field a parallel branch may write needs a reducer, because two branches finishing in the
    same superstep both return an update for it. ``rounds`` accumulates (order is normalised
    afterwards by plan position, so a run is reproducible regardless of which branch lands first);
    ``executed`` sums; ``accepted``/``cancelled`` accumulate ids.
    """

    rounds: Annotated[list, operator.add]          # list[tuple[node_id, LabRound]]
    executed: Annotated[int, operator.add]
    accepted: Annotated[list, operator.add]        # node ids
    cancelled: Annotated[list, operator.add]       # node ids that cancelled; non-empty = stop


def build_lab_graph(
    plan: LabPlan,
    run_node: "Callable[[TaskNode, list], dict[str, Any]]",
    *,
    checkpointer: Any | None = None,
) -> Any:
    """Compile ``plan`` into a LangGraph ``StateGraph``.

    ``run_node(node, prior_rounds)`` executes ONE task and returns the same dict
    :meth:`ResearchLab._run_one_node` returns — that indirection is what keeps every scientific
    decision outside this module. ``plan`` must already be serialized for resource conflicts (see
    :func:`serialize_conflicting_nodes`); this function only translates dependencies into edges.

    Imports LangGraph lazily so the package stays optional: the default planner must keep working
    on a host that has never installed it.
    """
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(LabGraphState)
    by_id = plan.by_id()

    def _make(node: TaskNode) -> "Callable[[LabGraphState], dict[str, Any]]":
        def _step(state: LabGraphState) -> dict[str, Any]:
            # A cancel anywhere in the graph stops the remaining nodes. LangGraph has no "abort the
            # run" primitive that fits here, so downstream nodes no-op instead — the same outcome
            # the hand-written scheduler got by breaking its while loop.
            if state.get("cancelled"):
                return {}
            prior = [r for _nid, r in state.get("rounds", [])]
            oc = run_node(node, prior)
            out: dict[str, Any] = {
                "rounds": [(node.id, r) for r in oc.get("rounds", [])],
                "executed": int(oc.get("executed", 0)),
            }
            if oc.get("accepted"):
                out["accepted"] = [node.id]
            if oc.get("cancelled"):
                out["cancelled"] = [node.id]
            return out

        return _step

    for node in plan.nodes:
        graph.add_node(node.id, _make(node))
    for node in plan.nodes:
        deps = [d for d in node.depends_on if d in by_id]
        if deps:
            for dep in deps:
                graph.add_edge(dep, node.id)
        else:
            graph.add_edge(START, node.id)
    # Sinks (nothing depends on them) terminate the graph.
    depended_on = {d for n in plan.nodes for d in n.depends_on if d in by_id}
    for node in plan.nodes:
        if node.id not in depended_on:
            graph.add_edge(node.id, END)

    return graph.compile(checkpointer=checkpointer)


def order_rounds(plan: LabPlan, tagged: "list[tuple[str, Any]]") -> list:
    """Rounds in PLAN order, not completion order.

    Parallel branches land in whatever order they finish, which would make the report's step
    numbering depend on scheduling luck. Sorting by the node's position in the plan makes a run
    reproducible and keeps the write-up in the order the user reviewed. Stable within a node, so a
    node's own revise-then-accept sequence is preserved.
    """
    position = {n.id: i for i, n in enumerate(plan.nodes)}
    return [r for _nid, r in sorted(tagged, key=lambda t: position.get(t[0], len(position)))]
