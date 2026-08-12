"""Offline tests for the multi-CYCLE campaign loop (``LabConfig.max_cycles``).

A cycle re-plans wholesale from what the previous cycles found — the complement to within-cycle
exploration, which reacts to a single step. The tests that matter most here are the TERMINATION
ones: an outer loop whose exit condition is an LLM opinion is how a run costs a weekend of GPU
time, so each deterministic exit gets its own test.

Every completion is injected — no GPU/LLM, no scanpy.
"""

from __future__ import annotations

import json

from bioagent.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
from bioagent.agents.research_lab import LabConfig, ResearchLab

_NEXT_CYCLE_MARK = "deciding whether to run ANOTHER cycle"
_EXPLORE_MARK = "opened a research path the CURRENT PLAN DOES NOT COVER"

CYCLE1 = ["Run QC on the dataset", "Identify marker genes"]
CYCLE2 = ["Compare the knockout and wild-type animals within each population",
          "Quantify how much of that difference depends on the outlier animal"]


def _ctx() -> HarnessContext:
    return HarnessContext(decisions={}, tunnel_port=1, model="m")


def _call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _finish():
    return {"content": "", "tool_calls": [
        {"id": "f1", "type": "function",
         "function": {"name": "finish", "arguments": json.dumps({"answer": "done this step"})}}]}


def _scientist():
    def chat(messages, _tools):
        if any(m.get("role") == "tool" for m in messages):
            return _finish()
        return _call("run_qc", {})
    return chat


def _router(first_agenda, *, next_cycle=None, explore=None, seen=None, synth=None):
    """``next_cycle(payload) -> dict`` answers the follow-up-cycle planning turn."""
    next_cycle = next_cycle or (lambda _u: {"continue": False, "reason": "answered", "agenda": []})
    explore = explore or (lambda _u: {"surprise": "nothing", "hypotheses": [], "new_steps": []})

    def complete(messages):
        sys_p = messages[0]["content"]
        user = messages[-1]["content"] if len(messages) > 1 else ""
        if seen is not None:
            seen.append(sys_p)
        if _NEXT_CYCLE_MARK in sys_p:
            return json.dumps(next_cycle(user))
        if _EXPLORE_MARK in sys_p:
            return json.dumps(explore(user))
        if "rigorous scientific Critic" in sys_p:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "grounded"})
        if "Principal Investigator of a bioinformatics lab" in sys_p:
            return json.dumps(first_agenda)
        if synth is not None:
            synth.append(user)
        return "FINAL REPORT: synthesized from accepted steps."

    return complete


def _lab(first_agenda, *, cycles=3, **router_kw):
    return ResearchLab(_ctx(), LabConfig(max_cycles=cycles),
                       complete_fn=_router(first_agenda, **router_kw),
                       scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))


# --- off by default ----------------------------------------------------------

def test_single_cycle_is_the_default_and_never_asks_about_another():
    seen: list[str] = []
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=_router(CYCLE1, seen=seen),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("Characterize the dataset")
    assert result.agenda == CYCLE1 and result.accepted_steps == 2
    assert not any(_NEXT_CYCLE_MARK in s for s in seen)


# --- the core behaviour ------------------------------------------------------

def test_second_cycle_is_planned_from_the_first_cycles_findings_and_runs():
    calls = {"n": 0}

    def next_cycle(user):
        calls["n"] += 1
        payload = json.loads(user)
        if calls["n"] == 1:
            # the re-plan sees what actually ran, with results — not just the question
            assert [w["step"] for w in payload["work_already_done"]] == CYCLE1
            assert payload["cycle"] == 2
            return {"continue": True, "reason": "the genotype effect is still unmeasured",
                    "agenda": list(CYCLE2)}
        return {"continue": False, "reason": "answered", "agenda": []}

    events: list[dict] = []
    result = _lab(CYCLE1, next_cycle=next_cycle).run("Characterize the dataset",
                                                     on_event=events.append)

    assert result.agenda == CYCLE1 + CYCLE2
    assert [r.step for r in result.rounds] == CYCLE1 + CYCLE2
    assert result.accepted_steps == 4 and result.converged is True
    # rounds are renumbered into ONE continuous sequence, not two restarts
    assert [r.round_no for r in result.rounds] == [1, 2, 3, 4]
    assert [r.step_index for r in result.rounds] == [1, 2, 3, 4]
    starts = [e for e in events if e["type"] == "cycle_start"]
    assert [e["cycle"] for e in starts] == [1, 2]
    done = next(e for e in events if e["type"] == "campaign_done")
    assert done["cycles"] == 2 and done["reason"] == "pi_declined"
    # exactly ONE manuscript, written after the last cycle
    assert sum(1 for e in events if e["type"] == "lab_done") == 1


def test_the_report_is_written_once_over_every_cycles_rounds():
    def next_cycle(_u):
        return ({"continue": True, "reason": "r", "agenda": list(CYCLE2)}
                if not next_cycle.done else {"continue": False, "reason": "done", "agenda": []})
    next_cycle.done = False

    def wrapped(user):
        out = next_cycle(user)
        next_cycle.done = True
        return out

    synth: list[str] = []
    events: list[dict] = []
    result = _lab(CYCLE1, next_cycle=wrapped, synth=synth).run("Q", on_event=events.append)
    assert sum(1 for e in events if e["type"] == "synthesize") == 1
    brief = "\n".join(synth)
    for step in CYCLE1 + CYCLE2:                      # every cycle's work reaches the writer
        assert step in brief
    assert result.final_answer.startswith("FINAL REPORT")


# --- termination: each deterministic exit gets its own test ------------------

def test_max_cycles_is_a_hard_ceiling_even_when_the_pi_always_wants_more():
    calls = {"n": 0}

    def next_cycle(_u):
        calls["n"] += 1
        return {"continue": True, "reason": "always more to do",
                "agenda": [f"Run the follow-up comparison number {calls['n']}"]}

    events: list[dict] = []
    result = _lab(CYCLE1, cycles=3, next_cycle=next_cycle).run("Q", on_event=events.append)
    assert len([e for e in events if e["type"] == "cycle_start"]) == 3
    done = next(e for e in events if e["type"] == "campaign_done")
    assert done["reason"] == "max_cycles" and done["cycles"] == 3
    assert len(result.agenda) == len(CYCLE1) + 2


def test_pi_declining_stops_the_campaign_after_one_cycle():
    events: list[dict] = []
    result = _lab(CYCLE1, next_cycle=lambda _u: {
        "continue": False, "reason": "the question is answered", "agenda": []}
    ).run("Q", on_event=events.append)
    assert len([e for e in events if e["type"] == "cycle_start"]) == 1
    assert next(e for e in events if e["type"] == "campaign_done")["reason"] == "pi_declined"
    assert result.agenda == CYCLE1
    declined = [e for e in events if e["type"] == "cycle_declined"]
    assert declined and declined[0]["reason"] == "the question is answered"


def test_a_replan_that_repeats_the_same_plan_is_treated_as_no_progress():
    events: list[dict] = []
    # the PI insists on continuing but proposes the cycle just run (reworded only)
    _lab(CYCLE1, next_cycle=lambda _u: {
        "continue": True, "reason": "again", "agenda": ["run qc on the dataset!",
                                                        "IDENTIFY marker genes"]}
    ).run("Q", on_event=events.append)
    assert next(e for e in events if e["type"] == "campaign_done")["reason"] == "no_progress"
    assert len([e for e in events if e["type"] == "cycle_start"]) == 1


def test_nothing_left_to_chase_stops_before_the_pi_is_even_asked():
    """With exploration ON and no hypothesis raised or outstanding, a further cycle would re-plan
    against an unchanged picture — so the loop exits WITHOUT spending a re-plan call."""
    calls = {"n": 0}

    def next_cycle(_u):
        calls["n"] += 1
        return {"continue": True, "reason": "more", "agenda": list(CYCLE2)}

    cfg = LabConfig(max_cycles=3, hypothesis_driven=True)
    events: list[dict] = []
    lab = ResearchLab(_ctx(), cfg, complete_fn=_router(CYCLE1, next_cycle=next_cycle),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    lab.run("Q", on_event=events.append)
    assert next(e for e in events if e["type"] == "campaign_done")["reason"] == "nothing_left_to_chase"
    assert calls["n"] == 0        # the re-plan turn was never issued


def test_a_replan_failure_ends_the_campaign_instead_of_killing_the_run():
    def complete(messages):
        sys_p = messages[0]["content"]
        if _NEXT_CYCLE_MARK in sys_p:
            raise RuntimeError("endpoint down")
        if "rigorous scientific Critic" in sys_p:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        if "Principal Investigator of a bioinformatics lab" in sys_p:
            return json.dumps(CYCLE1)
        return "FINAL REPORT"

    lab = ResearchLab(_ctx(), LabConfig(max_cycles=3), complete_fn=complete,
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("Q")
    assert result.accepted_steps == 2 and result.final_answer.startswith("FINAL REPORT")


def test_cancelling_between_cycles_stops_without_writing_a_manuscript():
    state = {"stop": False}

    def next_cycle(_u):
        state["stop"] = True          # user hits Stop while cycle 1 is finishing
        return {"continue": True, "reason": "more", "agenda": list(CYCLE2)}

    events: list[dict] = []
    lab = ResearchLab(_ctx(), LabConfig(max_cycles=3),
                      complete_fn=_router(CYCLE1, next_cycle=next_cycle),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("Q", on_event=events.append, should_cancel=lambda: state["stop"])
    assert next(e for e in events if e["type"] == "campaign_done")["reason"] == "cancelled"
    assert "stopped by the user" in result.final_answer
    assert not any(e["type"] == "synthesize" for e in events)   # no LLM call after a Stop


# --- composition with exploration -------------------------------------------

def test_exploration_and_cycles_compose_within_and_across_cycles():
    statement = "The knockout effect is driven by one degraded animal, not by genotype"
    new_step = ("Repeat the comparison with and without the outlier animal to measure how much of "
                "the difference depends on it")
    ecalls, ncalls = {"n": 0}, {"n": 0}

    def explore(_u):
        ecalls["n"] += 1
        if ecalls["n"] == 1:
            return {"surprise": "QC loss is concentrated in one animal",
                    "hypotheses": [{"statement": statement, "prediction": "p", "test": "t"}],
                    "new_steps": [{"step": new_step, "hypothesis": statement}]}
        return {"surprise": "nothing", "hypotheses": [], "new_steps": []}

    def next_cycle(user):
        ncalls["n"] += 1
        payload = json.loads(user)
        if ncalls["n"] == 1:
            # the re-plan sees the ledger, including the hypothesis raised mid-cycle
            assert [h["statement"] for h in payload["hypotheses"]] == [statement]
            return {"continue": True, "reason": "settle it properly", "agenda": list(CYCLE2)}
        return {"continue": False, "reason": "done", "agenda": []}

    cfg = LabConfig(max_cycles=3, hypothesis_driven=True)
    lab = ResearchLab(_ctx(), cfg,
                      complete_fn=_router(CYCLE1, explore=explore, next_cycle=next_cycle),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("Q")

    # cycle 1 grew by the discovered step; cycle 2 was planned on top of the whole picture
    assert result.agenda == CYCLE1 + [new_step] + CYCLE2
    assert result.hypotheses[0]["statement"] == statement
    assert result.accepted_steps == 5
