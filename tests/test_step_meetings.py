"""Offline tests for the PI↔Critic step-meeting protocol (docs/pi_critic_meeting_protocol.md).

Every completion (PI plan / pre-flight Critic gate / PI adjudication / per-step Critic /
post-step PI review / synthesis) is injected, so the whole two-way protocol runs with no
GPU/LLM and no scanpy — mirroring test_research_lab.py.
"""

from __future__ import annotations

import json

from bioagent.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
from bioagent.agents.research_lab import LabConfig, ResearchLab


def _ctx(decisions=None) -> HarnessContext:
    return HarnessContext(decisions=decisions or {}, tunnel_port=1, model="m")


def _call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _finish():
    return {"content": "", "tool_calls": [
        {"id": "f1", "type": "function",
         "function": {"name": "finish", "arguments": json.dumps({"answer": "done this step"})}}]}


def _scientist(briefs=None):
    """Scripted Scientist: call run_qc, then finish. Optionally record each brief it is handed."""
    def chat(messages, _tools):
        if briefs is not None:
            u = next((m["content"] for m in messages if m.get("role") == "user"), "")
            if u:
                briefs.append(u)
        if any(m.get("role") == "tool" for m in messages):
            return _finish()
        return _call("run_qc", {})
    return chat


def _router(agenda, *, gate=None, pi=None, poststep=None, critic=None,
            plan_critic=None, plan_pi=None, seen=None):
    """Route a completion by system-prompt marker. Handlers get the user-message content (a JSON
    payload) and return a JSON-able value. Unmatched prompts (e.g. skill selection) get a benign
    fallback, exactly like test_research_lab.py's router tolerates the auto-skill call."""
    gate = gate or (lambda _u: {"action": "proceed"})
    pi = pi or (lambda _u: {"action": "proceed"})
    poststep = poststep or (lambda _u: {"contribution": "new", "prune": []})
    critic = critic or (lambda _u: {"verdict": "accept", "score": 0.9, "critique": "grounded"})
    plan_critic = plan_critic or (lambda _u: {"issues": [], "revised_agenda": []})
    plan_pi = plan_pi or (lambda _u: {"final_agenda": list(agenda)})

    def complete(messages):
        sys = messages[0]["content"]
        user = messages[-1]["content"] if len(messages) > 1 else ""
        if seen is not None:
            seen.append(sys)
        if "reviewing a DRAFT analysis plan" in sys:    # plan-time Critic review
            return json.dumps(plan_critic(user))
        if "finalizing the analysis plan after the Critic" in sys:  # plan-time PI finalize
            return json.dumps(plan_pi(user))
        if "adjudicating a PRE-FLIGHT" in sys:          # PI adjudication (check before the gate)
            return json.dumps(pi(user))
        if "PRE-FLIGHT review with the PI" in sys:      # pre-flight Critic gate
            return json.dumps(gate(user))
        if "reviewing a step that JUST completed" in sys:  # post-step PI review
            return json.dumps(poststep(user))
        if "rigorous scientific Critic" in sys:         # per-step Critic
            return json.dumps(critic(user))
        if "Principal Investigator of a bioinformatics lab" in sys:  # PI plan
            return json.dumps(agenda)
        return "FINAL REPORT: synthesized from accepted steps."      # synth + benign fallback

    return complete


def _current(user):
    p = json.loads(user)
    return next((s["step"] for s in p["plan"] if s["state"] == "current"), "")


# --- off by default: the protocol adds no meetings and changes nothing -------

def test_step_meetings_off_by_default_holds_no_meetings():
    seen: list[str] = []
    complete = _router(["Run QC on the dataset", "Identify marker genes"], seen=seen)
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete,
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("Characterize the dataset")
    assert result.converged is True and result.accepted_steps == 2
    # no meeting prompt was ever issued — plan-time review, pre-flight gate, or post-step review
    assert not any("DRAFT analysis plan" in s for s in seen)
    assert not any("PRE-FLIGHT" in s for s in seen)
    assert not any("JUST completed" in s for s in seen)


# --- deterministic floor: enrichment w/o contrast is skipped without an LLM --

def test_preflight_floor_skips_enrichment_without_contrast_and_asks_no_model():
    calls = {"n": 0}

    def complete(_messages):
        calls["n"] += 1
        return "{}"

    dr = {"dataset_kind": "h5ad_single_cell",
          "obs_categoricals": {"celltype": {"n": 6, "values": ["Rod", "Cone"]},
                               "sample": {"n": 1, "values": ["s1"]}},
          "obs_keys": ["celltype", "sample"]}
    lab = ResearchLab(_ctx({"dataset_result": dr}), LabConfig(step_meetings=True),
                      complete_fn=complete,
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    d = lab._preflight_gate("q", "Run pathway enrichment on the markers", ["s"], 0, [], lambda _e: None)
    assert d.action == "skip" and d.by == "guard"
    assert calls["n"] == 0   # the floor never asks a model


# --- pre-flight skip prunes a step; the run still converges on the rest ------

def test_preflight_skip_prunes_step_and_still_converges():
    agenda = ["Run QC on the dataset", "Run de-novo clustering", "Identify marker genes"]

    def gate(user):
        if "clustering" in _current(user):
            return {"action": "skip", "reason": "redundant with the provided labels", "amendment": ""}
        return {"action": "proceed"}

    def pi(user):   # PI upholds the Critic's objection verbatim
        return json.loads(user).get("critic_objection", {"action": "proceed"})

    events: list[dict] = []
    lab = ResearchLab(_ctx(), LabConfig(step_meetings=True), complete_fn=_router(agenda, gate=gate, pi=pi),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("Characterize the dataset", on_event=events.append)

    assert result.converged is True and result.accepted_steps == 2
    steps_run = [r.step for r in result.rounds]
    assert "Run de-novo clustering" not in steps_run and len(result.rounds) == 2
    pruned = [e for e in events if e["type"] == "steps_pruned" and e.get("reason") == "preflight"]
    assert pruned and "Run de-novo clustering" in pruned[0]["dropped"]
    assert next(e for e in events if e["type"] == "lab_done")["pruned"] == 1


# --- post-step review prunes a now-moot downstream step ----------------------

def test_poststep_review_prunes_downstream_step():
    agenda = ["Run QC on the dataset", "Identify marker genes", "Run pathway enrichment"]

    def poststep(user):
        p = json.loads(user)
        remaining = p.get("remaining_steps", [])
        if "QC" in p["completed_step"]["step"]:
            return {"contribution": "new", "prune": [s for s in remaining if "enrichment" in s],
                    "reason": "no experimental contrast → enrichment is circular"}
        return {"contribution": "confirmed", "prune": []}

    events: list[dict] = []
    lab = ResearchLab(_ctx(), LabConfig(step_meetings=True), complete_fn=_router(agenda, poststep=poststep),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("Characterize the dataset", on_event=events.append)

    assert result.converged is True and result.accepted_steps == 2
    steps_run = [r.step for r in result.rounds]
    assert "Run pathway enrichment" not in steps_run
    pruned = [e for e in events if e["type"] == "steps_pruned" and e.get("reason") == "poststep_review"]
    assert pruned and "Run pathway enrichment" in pruned[0]["dropped"]


# --- amend threads the PI's adjustment into the Scientist's brief ------------

def test_preflight_amend_reaches_the_scientist_brief():
    agenda = ["Identify marker genes grouped by cluster"]

    def gate(_user):
        return {"action": "amend", "reason": "labels exist",
                "amendment": "group differential expression by the existing majorclass labels"}

    def pi(user):
        return json.loads(user).get("critic_objection", {"action": "proceed"})

    briefs: list[str] = []
    lab = ResearchLab(_ctx(), LabConfig(step_meetings=True), complete_fn=_router(agenda, gate=gate, pi=pi),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist(briefs)))
    result = lab.run("Characterize the dataset")

    assert result.accepted_steps == 1
    assert any("Plan review — adjust how you run THIS step" in b
               and "existing majorclass labels" in b for b in briefs)


# --- plan-time review: an incoherent draft is fixed BEFORE any step runs -----

def test_plan_review_revises_incoherent_agenda_before_any_step_runs():
    # The retina failure in miniature: de-novo clustering that is never reconciled with the provided
    # labels (orphan) + enrichment with no contrast (circular). The plan-time review should catch it
    # at the SOURCE — before a single step executes — not step-by-step at runtime.
    draft = ["Run QC on the dataset",
             "Re-cluster the cells de-novo into Leiden clusters",
             "Differential expression grouped by the provided major-class labels",
             "Run pathway enrichment on the DE results"]
    revised = ["Run QC on the dataset",
               "Differential expression grouped by the provided major-class labels"]

    def plan_critic(_u):
        return {"issues": ["de-novo clusters are never reconciled with the provided labels (orphan)",
                           "enrichment on identity markers with no contrast is circular"],
                "revised_agenda": revised}

    def plan_pi(_u):
        return {"final_agenda": revised, "reason": "drop the orphan clustering + circular enrichment"}

    events: list[dict] = []
    lab = ResearchLab(_ctx(), LabConfig(step_meetings=True),
                      complete_fn=_router(draft, plan_critic=plan_critic, plan_pi=plan_pi),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("complete the research", on_event=events.append)

    # the run executes the REVISED plan, not the incoherent draft
    assert result.agenda == revised
    assert [r.step for r in result.rounds] == revised
    assert result.converged is True and result.accepted_steps == 2
    ev = next(e for e in events if e["type"] == "plan_review")
    assert ev["before"] == draft and ev["after"] == revised and ev["issues"]


# --- plan-time decision HITL: linear path asks "labels vs re-cluster" --------

_ANNOTATED_DR = {"dataset_kind": "h5ad_single_cell",
                 "obs_categoricals": {"majorclass": {"n": 6, "values": ["Rod", "Cone"]}},
                 "obs_keys": ["majorclass"]}


def test_linear_label_decision_is_asked_and_choice_threads_into_the_run():
    agenda = ["Run QC on the dataset", "Leiden clustering of the cells", "Marker genes per group"]
    asked: dict = {}

    def review(node):
        asked["goal"] = node.goal
        asked["options"] = list(node.options)
        return {"action": "proceed", "choice": "Use the existing 'majorclass' labels"}

    briefs: list[str] = []
    lab = ResearchLab(_ctx({"dataset_result": _ANNOTATED_DR}), LabConfig(),  # planner defaults to linear
                      complete_fn=_router(agenda),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist(briefs)))
    result = lab.run("complete the research", decision_review=review)

    assert asked and "majorclass" in asked["goal"]                       # the fork was put to the user
    assert any("existing" in o.lower() for o in asked["options"])        # with real options
    assert result.accepted_steps == 3
    assert any("Use the existing 'majorclass' labels" in b for b in briefs)  # choice steers the run


def test_linear_no_decision_hook_runs_without_asking():
    agenda = ["Run QC on the dataset", "Leiden clustering of the cells", "Marker genes per group"]
    lab = ResearchLab(_ctx({"dataset_result": _ANNOTATED_DR}), LabConfig(), complete_fn=_router(agenda),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("complete the research")   # no decision_review → nothing to ask, runs straight through
    assert result.accepted_steps == 3 and result.converged is True


def test_linear_unlabeled_dataset_asks_no_decision():
    called = {"n": 0}

    def review(node):
        called["n"] += 1
        return {"action": "proceed"}

    agenda = ["Run QC on the dataset", "Leiden clustering of the cells", "Marker genes per group"]
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=_router(agenda),   # no dataset_result → not labeled
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    lab.run("complete the research", decision_review=review)
    assert called["n"] == 0


def test_dag_structurer_deterministically_flags_label_decision():
    agenda = ["Run QC on the dataset", "Leiden clustering of the cells", "Marker genes per group"]

    def complete(messages):
        sys = messages[0]["content"]
        if "structuring an ordered analysis plan" in sys:   # _DAG_STRUCTURE_SYSTEM — flag NO decision
            return json.dumps([{"id": "s1", "depends_on": []},
                               {"id": "s2", "depends_on": ["s1"]},
                               {"id": "s3", "depends_on": ["s2"]}])
        return "{}"

    lab = ResearchLab(_ctx({"dataset_result": _ANNOTATED_DR}), LabConfig(planner="dag"),
                      complete_fn=complete,
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    plan = lab._structure_agenda_dag("complete the research", agenda, lambda _e: None)

    clustering = [n for n in plan.nodes if _CLUSTERS(n.goal)]
    assert clustering and clustering[0].decision is True        # flagged even though the LLM didn't
    assert any("existing" in o.lower() for o in clustering[0].options)
    # non-clustering nodes are untouched
    assert all(not n.decision for n in plan.nodes if not _CLUSTERS(n.goal))


def _CLUSTERS(goal: str) -> bool:
    g = (goal or "").lower()
    return "clustering" in g or "leiden" in g
