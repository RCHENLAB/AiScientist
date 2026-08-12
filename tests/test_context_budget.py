"""Offline tests for run-scope context management.

Two properties carry the feature, and both are tested against a MODEL THAT MISBEHAVES:

1. the decision to compact is arithmetic — thresholds, step counts, which rounds fold — so none of
   it may depend on what the model says;
2. evidence pointers survive compaction no matter what the digest contains, because downstream
   steps are told to verify claims by reading the cited artifact.
"""

from __future__ import annotations

import json

from bioagent.agents import context_budget as cb
from bioagent.agents.research_harness import (
    HarnessConfig, HarnessContext, ResearchHarness, default_catalog,
)
from bioagent.agents.research_lab import (
    CriticVerdict, LabConfig, LabRound, ResearchLab, make_run_code_tool,
)

_COMPACT_MARK = "HANDOVER SUMMARY"


# --- measurement --------------------------------------------------------------------------

def test_carry_budget_is_a_share_of_what_is_left_after_the_reply_reserve():
    assert cb.carry_budget(32768, reserve=4096, share=0.35) == int((32768 - 4096) * 0.35)
    assert cb.carry_budget(0) >= 1                      # never zero, never negative
    assert cb.carry_budget(1000, reserve=99999) >= 1


def test_assess_states_step_through_the_thresholds():
    w, r = 32768, 4096
    budget = cb.carry_budget(w, reserve=r)

    def payload(fraction):                        # a string that measures ~fraction of the budget
        return "x" * int(budget * fraction * cb.CHARS_PER_TOKEN)

    assert cb.assess(payload(0.10), w, reserve=r).state == "ok"
    assert cb.assess(payload(0.70), w, reserve=r).state == "tight"
    assert cb.assess(payload(0.95), w, reserve=r).state == "critical"
    assert cb.assess("", w, reserve=r).needs_attention is False
    assert 0.68 < cb.assess(payload(0.70), w, reserve=r).ratio < 0.72


# --- the trigger is deterministic ----------------------------------------------------------

def _p(state):
    return cb.Pressure(tokens=10, budget=100, state=state)


def test_manual_request_wins_over_everything():
    t = cb.should_compact(_p("ok"), manual=True)
    assert t and t.kind == "manual" and t.urgent is False


def test_critical_pressure_is_urgent_and_tight_is_not():
    assert cb.should_compact(_p("critical")).urgent is True
    assert cb.should_compact(_p("tight")).urgent is False


def test_running_out_of_plan_triggers_only_past_the_threshold():
    assert cb.should_compact(_p("ok"), steps_done=5, max_steps=20) is None
    t = cb.should_compact(_p("ok"), steps_done=15, max_steps=20)
    assert t and t.kind == "steps"


def test_a_quiet_run_triggers_nothing():
    assert cb.should_compact(_p("ok"), steps_done=1, max_steps=20) is None


# --- which rounds fold is arithmetic --------------------------------------------------------

def test_fold_keeps_the_most_recent_verbatim():
    assert cb.fold_rounds(10, keep_recent=3) == [0, 1, 2, 3, 4, 5, 6]


def test_fold_refuses_to_bother_with_fewer_than_two():
    assert cb.fold_rounds(4, keep_recent=3) == []
    assert cb.fold_rounds(3, keep_recent=3) == []
    assert cb.fold_rounds(0, keep_recent=3) == []


def test_pinned_rounds_are_never_folded():
    """A round an OPEN hypothesis points at must stay verbatim, or the hypothesis can no longer
    be adjudicated honestly."""
    assert cb.fold_rounds(10, keep_recent=3, pinned={2, 4}) == [0, 1, 3, 5, 6]


# --- provenance survives whatever the model writes -------------------------------------------

def test_evidence_is_reattached_even_when_the_digest_omits_it():
    block = cb.compact_block("Rods were 64% of cells.", ["tables/qc.csv", "figures/umap.png"], 4)
    assert "tables/qc.csv" in block and "figures/umap.png" in block
    assert "first 4 accepted step(s)" in block


def test_an_empty_digest_still_produces_a_block_that_keeps_the_pointers():
    block = cb.compact_block("", ["tables/qc.csv"], 2)
    assert "tables/qc.csv" in block and "no digest available" in block


def test_a_failing_model_yields_an_empty_digest_rather_than_raising():
    def boom(_m):
        raise RuntimeError("endpoint down")
    assert cb.make_digest("anything", boom) == ""


# --- end to end through a run ---------------------------------------------------------------

def _ctx():
    return HarnessContext(decisions={}, tunnel_port=1, model="m")


def _finish(ans="done"):
    return {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
        "name": "finish", "arguments": json.dumps({"answer": ans})}}]}


def _scientist(payload):
    """A Scientist whose run_code result carries a real artifact pointer, so the findings block
    has provenance to preserve."""
    def chat(messages, _tools):
        if any(m.get("role") == "tool" for m in messages):
            return _finish(payload)
        return {"content": "", "tool_calls": [{"id": "t", "type": "function", "function": {
            "name": "run_code", "arguments": json.dumps({"code": "print(1)"})}}]}
    return chat


def _lab(agenda, *, seen=None, digest="COMPACTED DIGEST", cfg=None, big=4000):
    def complete(messages):
        sys_p = messages[0]["content"]
        if seen is not None:
            seen.append(sys_p)
        if _COMPACT_MARK in sys_p:
            return digest
        if "rigorous scientific Critic" in sys_p:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        if "Principal Investigator of a bioinformatics lab" in sys_p:
            return json.dumps(agenda)
        return "FINAL REPORT"

    catalog = [*default_catalog(), make_run_code_tool(
        lambda _c: {"status": "ok", "artifacts": ["tables/result.csv"]})]
    # PIN the window. It otherwise comes from BIOAGENT_VLLM_MAX_MODEL_LEN, which another suite
    # leaks into os.environ — and a test about thresholds must not inherit a deployment setting.
    harness_cfg = HarnessConfig(max_model_len=32768)
    return ResearchLab(_ctx(), cfg or LabConfig(context_management=True, compact_keep_recent=2),
                       complete_fn=complete,
                       scientist=ResearchHarness(catalog=catalog, config=harness_cfg,
                                                 chat_fn=_scientist("x" * big)))


AGENDA = [f"Analysis step number {i}" for i in range(1, 7)]


def test_off_by_default_never_measures_and_never_compacts():
    seen: list[str] = []
    lab = _lab(AGENDA, seen=seen, cfg=LabConfig())
    lab.run("Q")
    assert not any(_COMPACT_MARK in s for s in seen)
    assert lab._compacted_block == ""


def test_a_run_that_outgrows_its_budget_compacts_and_keeps_the_pointers():
    events: list[dict] = []
    lab = _lab(AGENDA)
    lab.run("Q", on_event=events.append)

    compacted = [e for e in events if e["type"] == "context_compact" and e.get("action") == "compacted"]
    assert compacted, "a run with large findings should have compacted"
    assert compacted[0]["folded_steps"] >= 2
    assert compacted[0]["tokens_after"] < compacted[0]["tokens_before"]
    # the digest replaced the prose, and the artifact pointer survived into the carried block
    assert "COMPACTED DIGEST" in lab._compacted_block
    assert "tables/result.csv" in lab._compacted_block
    pressure = [e for e in events if e["type"] == "context_pressure"]
    assert pressure and pressure[0]["kind"] == "context"


def test_a_failed_digest_leaves_the_findings_uncompacted():
    """Degrading to 'we did not save room' is safe; degrading to 'we lost the findings' is not."""
    events: list[dict] = []
    lab = _lab(AGENDA, digest="   ")          # blank digest == failure
    lab.run("Q", on_event=events.append)
    assert any(e.get("action") == "failed" for e in events if e["type"] == "context_compact")
    assert lab._compacted_block == "" and lab._compacted_indices == set()


def test_the_compact_command_is_a_control_signal_not_a_parsed_note():
    """A user-invoked compaction arrives as should_compact(), so the trigger stays code-level."""
    events: list[dict] = []
    asked = {"v": True}
    lab = _lab([f"Step {i}" for i in range(1, 6)], big=50)   # small findings: no natural pressure
    lab.run("Q", on_event=events.append, should_compact=lambda: asked["v"])
    kinds = [e["kind"] for e in events if e["type"] == "context_pressure"]
    assert "manual" in kinds


def test_the_human_can_decline_and_the_run_continues_uncompacted():
    events: list[dict] = []
    lab = _lab(AGENDA)
    lab.run("Q", on_event=events.append,
            decision_review=lambda _fork: {"choice": "Keep going without compacting"})
    assert any(e.get("action") == "declined" for e in events if e["type"] == "context_compact")
    assert lab._compacted_block == ""


def test_the_human_can_stop_the_run_at_the_pause():
    events: list[dict] = []
    lab = _lab(AGENDA)
    result = lab.run("Q", on_event=events.append,
                     decision_review=lambda _fork: {"choice": "Stop and write the report"})
    assert any(e.get("reason") == "context_stop" for e in events if e["type"] == "run_cancelled")
    assert "stopped by the user" in result.final_answer


def test_an_urgent_compaction_does_not_ask_permission():
    """At critical pressure the next step may not fit at all — asking to avoid overflow is theatre."""
    asked = {"n": 0}

    def review(_fork):
        asked["n"] += 1
        return {"choice": "Keep going without compacting"}

    lab = _lab(AGENDA, big=200_000)           # far past critical on the first check
    lab.run("Q", decision_review=review)
    assert lab._compacted_block != ""
    assert asked["n"] == 0
