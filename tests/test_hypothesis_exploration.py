"""Offline tests for hypothesis-driven exploration — the plan's only mid-run GROWTH path.

Before this feature the agenda was drafted once, before any data was seen, and could afterwards only
SHRINK (pre-flight skip / post-step prune), so a result contradicting the plan's premise had nowhere
to go. These tests pin the new behaviour AND, just as importantly, the guards that stop it becoming
a step generator: a step is only added when a falsifiable hypothesis is behind it, it is new, and it
is real analysis.

Every completion is injected, so the whole loop runs with no GPU/LLM and no scanpy — same harness
style as test_step_meetings.py.
"""

from __future__ import annotations

import json

import pytest

from aiscientist.agents.dag import LabPlan, TaskNode, lift_agenda_to_dag
from aiscientist.agents.hypotheses import DesignFacts, HypothesisLedger
from aiscientist.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
from aiscientist.agents.research_lab import LabConfig, ResearchLab

# The exploration prompt's unique marker (distinct from the post-step review's "reviewing a step
# that JUST completed", so a router can tell the two PI turns apart).
_EXPLORE_MARK = "opened a research path the CURRENT PLAN DOES NOT COVER"


def _ctx(decisions=None) -> HarnessContext:
    return HarnessContext(decisions=decisions or {}, tunnel_port=1, model="m")


def _call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _finish():
    return {"content": "", "tool_calls": [
        {"id": "f1", "type": "function",
         "function": {"name": "finish", "arguments": json.dumps({"answer": "done this step"})}}]}


def _scientist():
    """Scripted Scientist: call run_qc, then finish."""
    def chat(messages, _tools):
        if any(m.get("role") == "tool" for m in messages):
            return _finish()
        return _call("run_qc", {})
    return chat


def _router(agenda, *, explore=None, seen=None, synth=None):
    """Route a completion by system-prompt marker; ``explore`` handles the exploration turn and is
    given the JSON payload. Everything else gets a benign accept/proceed default."""
    explore = explore or (lambda _u: {"surprise": "nothing", "hypotheses": [], "new_steps": []})

    def complete(messages):
        sys = messages[0]["content"]
        user = messages[-1]["content"] if len(messages) > 1 else ""
        if seen is not None:
            seen.append(sys)
        if _EXPLORE_MARK in sys:
            return json.dumps(explore(user))
        if "reviewing a step that JUST completed" in sys:
            return json.dumps({"contribution": "new", "prune": []})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "grounded"})
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(agenda)
        if synth is not None and "FINAL" not in sys:
            synth.append(user)
        return "FINAL REPORT: synthesized from accepted steps."

    return complete


def _lab(agenda, *, config=None, **router_kw):
    return ResearchLab(_ctx(), config or LabConfig(hypothesis_driven=True),
                       complete_fn=_router(agenda, **router_kw),
                       scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))


# A well-formed proposal: a falsifiable hypothesis plus the one step that tests it.
def _proposal(statement="Rod cells in this sample carry a stress signature of technical origin",
              step="Compare the stress-response genes in rod cells against the ambient RNA profile "
                   "to determine whether the signature is biological or a contamination artefact"):
    return lambda _u: {
        "surprise": "rods show a stress signature the plan did not anticipate",
        "hypotheses": [{"statement": statement, "rival": "ambient RNA from lysed photoreceptors", "discriminator": "a cell-type-specific pattern favours the hypothesis; a uniform one favours the rival",
                        "prediction": "the stress genes track ambient contamination, not cell state",
                        "test": "contrast the stress genes with the ambient profile"}],
        "new_steps": [{"step": step, "hypothesis": statement}],
    }


# --- the ledger (pure data) --------------------------------------------------

def test_ledger_dedupes_and_resolves():
    led = HypothesisLedger()
    h = led.add("Rods carry a stress signature", prediction="p", test="t",
                rival="ambient RNA", discriminator="cell-type specific vs uniform",
                origin_step="QC")
    assert h is not None and h.id == "h1" and h.status == "open"
    # same claim, different punctuation/casing -> not a second row
    assert led.add("rods carry a stress signature!") is None
    assert len(led) == 1
    # resolvable by id or by verbatim statement; evidence accumulates
    assert led.resolve("h1", "refuted", "ambient profile explains it").status == "refuted"
    assert led.open_items() == [] and len(led.resolved_items()) == 1
    assert "ambient profile explains it" in led.render()
    # a garbled reply must never corrupt the ledger
    assert led.resolve("h9", "supported") is None
    assert led.resolve("h1", "not-a-status") is None
    assert led.items[0].status == "refuted"


def test_ledger_round_trips_through_dicts():
    led = HypothesisLedger()
    led.add("A causes B", prediction="p", test="t", rival="C causes B",
            discriminator="X favours A; Y favours C")
    led.link_test("h1", "Run the discriminating comparison")
    led.resolve("h1", "supported", "the comparison held")
    back = HypothesisLedger.from_list(led.to_list())
    assert back.to_list() == led.to_list()
    assert back.items[0].tested_by == ("Run the discriminating comparison",)


# --- the DAG's growth primitive ---------------------------------------------

def test_plan_extend_appends_and_rejects_bad_nodes():
    plan = lift_agenda_to_dag(["QC", "Cluster"])
    grown = plan.extend([TaskNode(id=plan.next_id(), goal="Test the artefact hypothesis",
                                  depends_on=("s2",))])
    assert [n.id for n in grown.nodes] == ["s1", "s2", "x1"]
    assert grown.nodes[-1].depends_on == ("s2",)
    # the added node is ready only once its parent is done — it is a real graph node
    assert "x1" not in grown.ready_ids({"s1"}) and "x1" in grown.ready_ids({"s1", "s2"})
    # duplicate id, empty goal, and unknown dependency are all dropped (never raises)
    assert grown.extend([TaskNode(id="x1", goal="dup")]).nodes == grown.nodes
    assert grown.extend([TaskNode(id="x2", goal="  ")]).nodes == grown.nodes
    assert grown.extend([TaskNode(id="x2", goal="g", depends_on=("nope",))]).nodes[-1].depends_on == ()


def test_plan_extend_refuses_a_cycle():
    plan = LabPlan((TaskNode(id="a", goal="A", depends_on=("b",)),
                    TaskNode(id="b", goal="B")))
    # 'b' already depends on nothing; adding a node that b depends on is fine, but a node closing
    # a cycle through existing ids must leave the plan untouched.
    cyc = LabPlan((TaskNode(id="a", goal="A", depends_on=("b",)),
                   TaskNode(id="b", goal="B", depends_on=("c",))))
    assert cyc.extend([TaskNode(id="c", goal="C", depends_on=("a",))]).nodes == cyc.nodes
    assert len(plan.extend([TaskNode(id="c", goal="C", depends_on=("a",))]).nodes) == 3


# --- off by default ----------------------------------------------------------

def test_exploration_off_by_default_never_asks_and_never_grows():
    seen: list[str] = []
    agenda = ["Run QC on the dataset", "Identify marker genes"]
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=_router(agenda, explore=_proposal(), seen=seen),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("Characterize the dataset")
    assert result.agenda == agenda and result.accepted_steps == 2
    assert result.hypotheses == []
    assert not any(_EXPLORE_MARK in s for s in seen)   # the model is never even asked


# --- the core behaviour: a surprising result grows the plan and runs ---------

def test_surprising_result_adds_a_hypothesis_and_the_step_that_tests_it():
    agenda = ["Run QC on the dataset", "Identify marker genes"]
    new_step = ("Compare the stress-response genes in rod cells against the ambient RNA profile to "
                "determine whether the signature is biological or a contamination artefact")
    calls = {"n": 0}

    def explore(_user):
        calls["n"] += 1
        return _proposal(step=new_step)(_user) if calls["n"] == 1 else {
            "surprise": "nothing", "hypotheses": [], "new_steps": []}

    events: list[dict] = []
    lab = _lab(agenda, explore=explore)
    result = lab.run("Characterize the dataset", on_event=events.append)

    # the plan GREW and the discovered step actually ran
    assert len(result.agenda) == 3 and result.agenda[-1] == new_step
    assert new_step in [r.step for r in result.rounds]
    assert result.accepted_steps == 3

    formed = [e for e in events if e["type"] == "hypothesis_formed"]
    added = [e for e in events if e["type"] == "step_added"]
    extended = [e for e in events if e["type"] == "agenda_extended"]
    assert len(formed) == 1 and formed[0]["id"] == "h1"
    assert added and added[0]["hypothesis_id"] == "h1" and added[0]["step"] == new_step
    assert extended and extended[0]["agenda"] == 3

    # the hypothesis is carried on the result, linked to the step that tests it
    assert result.hypotheses[0]["statement"].startswith("Rod cells")
    assert result.hypotheses[0]["tested_by"] == [new_step]


def test_a_later_step_can_refute_an_open_hypothesis_and_the_report_says_so():
    agenda = ["Run QC on the dataset", "Identify marker genes"]
    statement = "Rod cells in this sample carry a stress signature of technical origin"
    calls = {"n": 0}

    def explore(_user):
        calls["n"] += 1
        if calls["n"] == 1:
            return _proposal(statement=statement)(_user)
        if calls["n"] == 2:   # a later step adjudicates the open hypothesis
            payload = json.loads(_user)
            assert [h["statement"] for h in payload["open_hypotheses"]] == [statement]
            return {"surprise": "nothing", "hypotheses": [], "new_steps": [],
                    "resolve": [{"hypothesis": statement, "favoured": "rival",
                                 "evidence": "the stress genes are absent from the ambient profile"}]}
        return {"surprise": "nothing", "hypotheses": [], "new_steps": []}

    events: list[dict] = []
    synth: list[str] = []
    lab = _lab(agenda, explore=explore, synth=synth)
    result = lab.run("Characterize the dataset", on_event=events.append)

    assert result.hypotheses[0]["status"] == "refuted"
    resolved = [e for e in events if e["type"] == "hypothesis_resolved"]
    assert resolved and resolved[0]["status"] == "refuted"
    # the refuted hypothesis reaches the report writer — a refutation is a result, not a failure
    brief = "\n".join(synth)
    assert statement in brief and "refuted" in brief.lower()


# --- the guards: exploration must not degenerate into a step generator ------

def test_orphan_step_without_a_hypothesis_is_refused():
    agenda = ["Run QC on the dataset", "Identify marker genes"]
    explore = lambda _u: {"surprise": "s", "hypotheses": [],
                          "new_steps": [{"step": "Do some more analysis of the rods",
                                         "hypothesis": "nothing we hold"}]}
    result = _lab(agenda, explore=explore).run("Characterize the dataset")
    assert result.agenda == agenda and result.hypotheses == []


def test_unfalsifiable_hypothesis_is_refused_so_its_step_has_nothing_to_stand_on():
    agenda = ["Run QC on the dataset", "Identify marker genes"]
    explore = lambda _u: {
        "surprise": "s",
        # no prediction and no test -> "investigate X further" dressed up as a hypothesis
        "hypotheses": [{"statement": "Rods are interesting", "prediction": "", "test": "", "rival": "ambient RNA from lysed photoreceptors", "discriminator": "a cell-type-specific pattern favours the hypothesis; a uniform one favours the rival"}],
        "new_steps": [{"step": "Characterise the rods in more detail",
                       "hypothesis": "Rods are interesting"}]}
    result = _lab(agenda, explore=explore).run("Characterize the dataset")
    assert result.hypotheses == [] and result.agenda == agenda


def test_step_duplicating_an_existing_plan_step_is_refused():
    agenda = ["Run QC on the dataset", "Identify marker genes"]
    # same work as agenda[1], reworded punctuation/casing only
    explore = _proposal(step="identify marker genes!")
    result = _lab(agenda, explore=explore).run("Characterize the dataset")
    assert result.agenda == agenda
    # the hypothesis is kept (it may still be testable another way); no step was added for it
    assert result.hypotheses and result.hypotheses[0].get("tested_by", []) == []


def test_report_packaging_busywork_is_refused():
    agenda = ["Run QC on the dataset", "Identify marker genes"]
    explore = _proposal(step="Compile the findings into a PDF report for the collaborators")
    result = _lab(agenda, explore=explore).run("Characterize the dataset")
    assert result.agenda == agenda


def test_max_new_steps_caps_a_model_that_finds_everything_surprising():
    agenda = ["Run QC on the dataset", "Identify marker genes"]
    calls = {"n": 0}

    def explore(_user):
        calls["n"] += 1
        n = calls["n"]
        return {"surprise": "everything",
                "hypotheses": [{"statement": f"Claim number {n}", "prediction": "p", "test": "t", "rival": "ambient RNA from lysed photoreceptors", "discriminator": "a cell-type-specific pattern favours the hypothesis; a uniform one favours the rival"}],
                "new_steps": [{"step": f"Run the discriminating comparison number {n}",
                               "hypothesis": f"Claim number {n}"}]}

    cfg = LabConfig(hypothesis_driven=True, max_new_steps=2)
    lab = ResearchLab(_ctx(), cfg, complete_fn=_router(agenda, explore=explore),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("Characterize the dataset")
    assert len(result.agenda) == len(agenda) + 2      # capped, run still terminates


def test_max_steps_caps_total_plan_length():
    agenda = ["Run QC on the dataset", "Identify marker genes"]
    calls = {"n": 0}

    def explore(_user):
        calls["n"] += 1
        n = calls["n"]
        return {"surprise": "everything",
                "hypotheses": [{"statement": f"Claim {n}", "prediction": "p", "test": "t", "rival": "ambient RNA from lysed photoreceptors", "discriminator": "a cell-type-specific pattern favours the hypothesis; a uniform one favours the rival"}],
                "new_steps": [{"step": f"Run comparison {n}", "hypothesis": f"Claim {n}"}]}

    cfg = LabConfig(hypothesis_driven=True, max_new_steps=99, max_steps=3)
    lab = ResearchLab(_ctx(), cfg, complete_fn=_router(agenda, explore=explore),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("Characterize the dataset")
    assert len(result.agenda) == 3


def test_malformed_exploration_reply_degrades_to_no_growth():
    agenda = ["Run QC on the dataset", "Identify marker genes"]

    def complete(messages):
        sys = messages[0]["content"]
        if _EXPLORE_MARK in sys:
            return "I'm afraid I can't answer that in JSON."
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(agenda)
        return "FINAL REPORT"

    lab = ResearchLab(_ctx(), LabConfig(hypothesis_driven=True), complete_fn=complete,
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("Characterize the dataset")
    assert result.agenda == agenda and result.accepted_steps == 2


# --- the DAG planner grows too ----------------------------------------------

def test_dag_planner_adds_a_node_that_depends_on_the_step_that_provoked_it():
    agenda = ["Run QC on the dataset", "Identify marker genes"]
    new_step = ("Compare the stress-response genes in rod cells against the ambient RNA profile to "
                "determine whether the signature is a contamination artefact")
    calls = {"n": 0}

    def explore(_user):
        calls["n"] += 1
        return _proposal(step=new_step)(_user) if calls["n"] == 1 else {
            "surprise": "nothing", "hypotheses": [], "new_steps": []}

    def complete(messages):
        sys = messages[0]["content"]
        if _EXPLORE_MARK in sys:
            return json.dumps(explore(messages[-1]["content"]))
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        if "dependency" in sys.lower() and "json" in sys.lower():   # DAG structuring turn
            return json.dumps([{"id": "s1", "goal": agenda[0]},
                               {"id": "s2", "goal": agenda[1], "depends_on": ["s1"]}])
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(agenda)
        return "FINAL REPORT"

    events: list[dict] = []
    lab = ResearchLab(_ctx(), LabConfig(hypothesis_driven=True, planner="dag"),
                      complete_fn=complete,
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    result = lab.run("Characterize the dataset", on_event=events.append)

    assert new_step in result.agenda and new_step in [r.step for r in result.rounds]
    ext = [e for e in events if e["type"] == "agenda_extended"]
    assert ext and ext[0]["after_node"] in ("s1", "s2")
    assert result.hypotheses and result.hypotheses[0]["tested_by"] == [new_step]


# --- cheapness: the deterministic pre-filter -------------------------------------------------
# Inference on the lab's own GPUs is free in cash, so exploration's real cost is latency and queue
# time. The lever is not issuing calls that can only come back empty. These tests use a stub that
# proposes NOTHING, so the count reflects only how many turns were issued.

_QUIET = lambda _u: {"surprise": "nothing", "hypotheses": [], "new_steps": []}


def _explore_calls(seen):
    return sum(1 for s in seen if _EXPLORE_MARK in s)


def test_no_exploration_call_for_a_literature_step():
    agenda = ["Run QC on the dataset",
              "Search the published literature for the key genes found above"]
    seen: list[str] = []
    _lab(agenda, explore=_QUIET, seen=seen).run("Characterize the dataset")
    # two accepted steps, but only the analysis one is worth a turn
    assert _explore_calls(seen) == 1


def test_no_exploration_call_when_the_step_produced_nothing_to_be_surprised_by():
    """No artifact and a one-word answer: the payload would be empty, so don't pay for the turn."""
    def chat(messages, _tools):          # finish immediately, no tool ever runs
        return {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
            "name": "finish", "arguments": json.dumps({"answer": "ok"})}}]}

    seen: list[str] = []
    agenda = ["Run QC on the dataset"]
    lab = ResearchLab(_ctx(), LabConfig(hypothesis_driven=True),
                      complete_fn=_router(agenda, explore=_QUIET, seen=seen),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=chat))
    lab.run("Characterize the dataset")
    assert _explore_calls(seen) == 0


def test_a_step_with_a_real_artifact_still_gets_explored():
    """The filter must not silence exploration on a normal analysis step."""
    seen: list[str] = []
    _lab(["Run QC on the dataset"], explore=_QUIET, seen=seen).run("Characterize the dataset")
    assert _explore_calls(seen) == 1


# --- the gate: what makes a hypothesis admissible ----------------------------------------------
# Run c135ae589d96 generated ONE hypothesis, gave it no rival, and marked it supported on a
# prediction its own design guaranteed. Each rule below is one half of that failure.

def _h1_kwargs(**over):
    """The real h1 from that run, so the regression is pinned to the actual text."""
    kw = dict(
        statement=("The transcriptional shifts between DDX41 and WT are primarily driven by a "
                   "1.6x sequencing depth imbalance rather than genuine regulatory changes."),
        prediction="Pseudobulk aggregation will collapse the massive log2FC values of the top DE genes",
        rival="DDX41 loss disrupts Muller-cell junction and morphogenesis programmes",
        discriminator="pseudobulk finds no significant genes -> artefact; it finds some -> biology",
    )
    kw.update(over)
    return kw


def test_a_hypothesis_with_no_rival_is_refused():
    led = HypothesisLedger()
    h, why = led.admit(**_h1_kwargs(rival=""))
    assert h is None and "no rival" in why
    assert len(led) == 0


def test_a_rival_that_restates_the_hypothesis_is_not_a_rival():
    led = HypothesisLedger()
    stmt = "The shift is a depth artefact"
    h, why = led.admit(statement=stmt, rival="the shift is a DEPTH artefact!",
                       discriminator="x favours one, y the other")
    assert h is None and "restates" in why


def test_a_discriminator_that_restates_the_prediction_draws_no_contrast():
    led = HypothesisLedger()
    h, why = led.admit(statement="A explains it", rival="B explains it",
                       prediction="the counts drop", discriminator="The counts drop!")
    assert h is None and "restates the prediction" in why


def test_the_h1_regression_a_design_that_forces_the_outcome_is_refused():
    # The whole point. With one library per arm, "pseudobulk finds no significant genes" happens
    # whether or not the biology is real, so it cannot separate the two explanations.
    led = HypothesisLedger()
    h, why = led.admit(**_h1_kwargs(), design=DesignFacts(replicates_per_arm=1))
    assert h is None
    assert "fixed by the design" in why and "1 replicate(s) per arm" in why


def test_the_same_hypothesis_is_admitted_once_the_discriminator_can_actually_fail():
    led = HypothesisLedger()
    h, why = led.admit(**_h1_kwargs(
        discriminator=("a uniform same-direction shift across every cell type favours the "
                       "artefact; a shift confined to Muller glia and running against the depth "
                       "gradient favours the biology")),
        design=DesignFacts(replicates_per_arm=1))
    assert why == "" and h is not None and h.id == "h1"
    assert h.rival.startswith("DDX41 loss")


def test_replicated_designs_are_not_blocked_by_the_significance_rule():
    led = HypothesisLedger()
    h, _ = led.admit(**_h1_kwargs(), design=DesignFacts(replicates_per_arm=3))
    assert h is not None


def test_an_unknown_design_never_blocks_a_hypothesis():
    led = HypothesisLedger()
    assert led.admit(**_h1_kwargs())[0] is not None            # design=None
    assert HypothesisLedger().admit(**_h1_kwargs(), design=DesignFacts())[0] is not None


# --- adjudication is a contest, not an assertion -----------------------------------------------

def test_supported_is_only_reachable_by_the_evidence_favouring_the_hypothesis():
    led = HypothesisLedger()
    led.add(statement="A explains it", rival="B explains it", discriminator="x vs y")
    assert led.adjudicate("h1", "hypothesis", "x was observed").status == "supported"


def test_favouring_the_rival_refutes_and_favouring_neither_is_inconclusive():
    led = HypothesisLedger()
    led.add(statement="A", rival="B", discriminator="x vs y")
    led.add(statement="C", rival="D", discriminator="x vs y")
    assert led.adjudicate("h1", "rival", "y was observed").status == "refuted"
    assert led.adjudicate("h2", "neither", "neither showed").status == "inconclusive"


def test_a_garbled_favoured_value_is_a_no_op_not_a_corrupt_ledger():
    led = HypothesisLedger()
    led.add(statement="A", rival="B", discriminator="x vs y")
    assert led.adjudicate("h1", "probably?") is None
    assert led.items[0].status == "open"


def test_the_render_shows_the_contest_so_the_adjudicator_sees_both_sides():
    led = HypothesisLedger()
    led.add(statement="depth artefact", rival="Muller biology", discriminator="uniform vs confined")
    out = led.render()
    assert "rival: Muller biology" in out and "tells them apart: uniform vs confined" in out
    assert "CONTEST" in out or "contest" in out


# --- the gate must not eat GOOD discriminators --------------------------------------------------
# Measured against a corpus of discriminators a competent scientist would write for the SAME n=1
# study: treating generic statistics vocabulary as disqualifying refused 2 of 7 of them. An ORA or
# GSEA FDR is computed on ONE ranking and is perfectly usable without replicates; only a statistic
# applied BETWEEN THE ARMS needs them.


def _admit(discriminator, replicates=1):
    led = HypothesisLedger()
    return led.admit(statement="a depth artefact explains the shifts",
                     rival="DDX41 loss disrupts Muller-cell junction programmes",
                     prediction="the top log2FCs collapse", discriminator=discriminator,
                     design=DesignFacts(replicates_per_arm=replicates))


@pytest.mark.parametrize("discriminator", [
    "a shift confined to Muller glia and running against the depth gradient favours the biology",
    "cell-type specificity favours the hypothesis; uniformity across all types favours the rival",
    "GSEA on the MG ranking showing adhesion terms at FDR < 0.05 favours the biology",
    "a significant over-representation of cell-junction GO terms among the down genes",
    "smFISH showing reduced Crb1 at the outer limiting membrane favours the biology",
])
def test_a_discriminator_that_works_without_replicates_is_admitted(discriminator):
    h, why = _admit(discriminator)
    assert h is not None, why


@pytest.mark.parametrize("discriminator", [
    "pseudobulk finds no significant genes -> artefact; it finds some -> biology",   # the real h1
    "a DESeq2 test between the two arms decides it",
    "run a t-test between DDX41 and WT samples and see if p < 0.05",
    "the adjusted p value between the two arms decides it",
    "whether the FDR between conditions clears 0.05",
])
def test_a_discriminator_the_design_cannot_answer_is_refused(discriminator):
    h, why = _admit(discriminator)
    assert h is None and "fixed by the design" in why


def test_the_same_discriminators_are_fine_once_the_design_has_replicates():
    assert _admit("pseudobulk between the arms decides it", replicates=3)[0] is not None
