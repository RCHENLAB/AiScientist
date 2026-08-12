"""Offline tests for the dependency-DAG plan model (feat/dag-planner)."""

from __future__ import annotations

import json

from bioagent.agents.dag import (
    LabPlan,
    TaskNode,
    lift_agenda_to_dag,
    parse_dag,
)


def test_lift_agenda_is_a_linear_chain():
    plan = lift_agenda_to_dag(["Run QC", "Cluster", "DE"])
    assert plan.goals() == ["Run QC", "Cluster", "DE"]
    ids = [n.id for n in plan.nodes]
    assert plan.nodes[0].depends_on == ()
    assert plan.nodes[1].depends_on == (ids[0],)
    assert plan.nodes[2].depends_on == (ids[1],)
    # a linear chain exposes exactly ONE ready node at a time (reproduces the linear loop)
    assert plan.ready_ids(set()) == [ids[0]]
    assert plan.ready_ids({ids[0]}) == [ids[1]]


def test_ready_ids_surfaces_independent_branches():
    plan = LabPlan((
        TaskNode("qc", "QC"),
        TaskNode("cluster", "Cluster", depends_on=("qc",)),
        TaskNode("de", "DE by class", depends_on=("cluster",)),
        TaskNode("lit", "Literature", depends_on=("cluster",)),   # parallel to de
    ))
    assert plan.ready_ids(set()) == ["qc"]
    # after cluster, BOTH de and lit are ready — the Coordinator gets a real choice
    assert set(plan.ready_ids({"qc", "cluster"})) == {"de", "lit"}
    # a done node is never re-surfaced
    assert plan.ready_ids({"qc", "cluster", "de"}) == ["lit"]


def test_parse_dag_full_nodes():
    raw = json.dumps({"nodes": [
        {"id": "qc", "goal": "Run QC", "produces": ["adata_qc.h5ad"]},
        {"id": "cluster", "goal": "Cluster", "depends_on": ["qc"],
         "consumes": ["adata_qc.h5ad"], "produces": ["adata_clustered.h5ad"]},
        {"id": "de", "goal": "DE by majorclass", "depends_on": ["cluster"], "suggested_tool": "run_de"},
        {"id": "lit", "goal": "Literature", "depends_on": ["cluster"]},
    ]})
    plan = parse_dag(raw)
    assert plan is not None
    byid = plan.by_id()
    assert byid["cluster"].consumes == ("adata_qc.h5ad",)
    assert byid["de"].suggested_tool == "run_de"
    assert set(plan.ready_ids({"qc", "cluster"})) == {"de", "lit"}


def test_parse_dag_drops_unknown_and_self_deps():
    raw = json.dumps([
        {"id": "a", "goal": "A", "depends_on": ["a", "ghost"]},   # self + unknown → dropped
        {"id": "b", "goal": "B", "depends_on": ["a"]},
    ])
    plan = parse_dag(raw)
    assert plan is not None
    assert plan.by_id()["a"].depends_on == ()
    assert plan.by_id()["b"].depends_on == ("a",)


def test_parse_dag_rejects_cycles():
    raw = json.dumps([
        {"id": "a", "goal": "A", "depends_on": ["b"]},
        {"id": "b", "goal": "B", "depends_on": ["a"]},
    ])
    assert parse_dag(raw) is None


def test_parse_dag_lifts_plain_string_agenda():
    plan = parse_dag(json.dumps({"agenda": ["Run QC", "Cluster", "DE"]}))
    assert plan is not None and plan.goals() == ["Run QC", "Cluster", "DE"]
    assert plan.nodes[1].depends_on == (plan.nodes[0].id,)   # lifted to linear chain


def test_parse_dag_tolerates_prose_and_fences():
    raw = "Here is the plan:\n```json\n[{\"id\":\"x\",\"goal\":\"do x\"}]\n```\nthanks"
    plan = parse_dag(raw)
    assert plan is not None and plan.goals() == ["do x"]


def test_parse_dag_bad_input_returns_none():
    assert parse_dag("not json at all") is None
    assert parse_dag("{}") is None
    assert parse_dag(json.dumps([])) is None


def test_parse_dag_dedupes_ids_and_caps():
    raw = json.dumps([{"id": "dup", "goal": f"g{i}"} for i in range(20)])
    plan = parse_dag(raw, max_nodes=5)
    assert plan is not None
    ids = [n.id for n in plan.nodes]
    assert len(ids) == 5 and len(set(ids)) == 5     # capped + unique
