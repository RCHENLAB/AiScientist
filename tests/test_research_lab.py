"""Offline tests for the role-based research lab (PI -> Scientist -> Critic -> converge).

Everything is injected — the PI/Critic/Synthesize completions and the Scientist's
tool-calling chat — so the whole multi-agent loop runs with no GPU/LLM.
"""

from __future__ import annotations

import json

from bioagent.agents.research_harness import (
    HarnessContext,
    HarnessTool,
    ResearchHarness,
    _msg_tokens,
    default_catalog,
)
from bioagent.agents.research_lab import (
    DEFAULT_SPECIALISTS,
    LabConfig,
    ResearchLab,
    make_run_code_tool,
)


def _ctx() -> HarnessContext:
    return HarnessContext(decisions={}, tunnel_port=1, model="m")


def _make_complete(agenda, critic_responses):
    """Route a completion to PI / Critic / Synthesize by system-prompt marker."""
    state = {"i": 0}

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:   # PI plan
            return json.dumps(agenda)
        if "rigorous scientific Critic" in sys:       # Critic
            r = critic_responses[min(state["i"], len(critic_responses) - 1)]
            state["i"] += 1
            return json.dumps(r)
        return "FINAL REPORT: synthesized from accepted steps."   # Synthesize

    return complete


def _scientist_calls(tool_name, tool_args):
    """A scripted Scientist: call <tool> first, then `finish` once a tool result is in.

    Native tool_calls carry an ``id`` like real OpenAI/vLLM responses, so the harness
    feeds the result back as a ``role:tool`` + ``tool_call_id`` message (the production
    path); the helper detects that to switch to `finish`."""

    def chat(messages, tools):
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [
                {"id": "f1", "type": "function",
                 "function": {"name": "finish", "arguments": json.dumps({"answer": "done this step"})}}]}
        return {"content": "", "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": tool_name, "arguments": json.dumps(tool_args)}}]}

    return chat


def test_injected_scientist_gets_progressive_disclosure_tools():
    # The GATEWAY builds the scientist itself (a catalog WITHOUT the skill tools) and injects it —
    # exactly like `default_catalog()` here. Progressive disclosure requires read_skill_reference /
    # search_skills to be DISPATCHABLE, or the brief tells the model to call tools that don't exist.
    injected = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    assert "read_skill_reference" not in injected._by_name          # gateway catalog lacks them...
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=lambda m: "", scientist=injected)
    # ...the lab must attach them to the injected scientist (both catalog + dispatch map)
    assert "read_skill_reference" in lab.scientist._by_name
    assert "search_skills" in lab.scientist._by_name
    assert {"read_skill_reference", "search_skills"} <= {t.name for t in lab.scientist.catalog}


def test_injected_scientist_can_read_the_source_of_its_own_tools():
    # Same prod hazard as the skill tools above: the gateway's catalog has no read_tool_source,
    # so without the attach the system prompt tells the model to read an implementation using a
    # tool that does not exist. And it must introspect the RUN's catalog — a list captured at
    # import time would not include whatever the gateway injected.
    injected = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    assert "read_tool_source" not in injected._by_name
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=lambda m: "", scientist=injected)
    assert "read_tool_source" in lab.scientist._by_name

    reader = lab.scientist._by_name["read_tool_source"]
    out = reader.executor({"tool": "run_qc"}, _ctx())
    assert "source" in out and out["source"].strip()
    assert out["tool"] == "run_qc"
    # The catalog it introspects is the RUN's, including the tools attached after injection —
    # so it can read itself, which is what makes auditing the auditor possible.
    listed = reader.executor({"tool": "__nope__"}, _ctx())["available"]
    assert "read_tool_source" in listed and "read_skill_reference" in listed


# --- happy path: PI plans 2 steps, Critic accepts both, lab converges --------

def test_lab_converges_when_critic_accepts_all_steps():
    complete = _make_complete(
        agenda=["Run QC on the dataset", "Identify marker genes"],
        critic_responses=[{"verdict": "accept", "score": 0.9, "critique": "grounded"}],
    )
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)

    events = []
    result = lab.run("Characterize the PBMC dataset", on_event=events.append)

    assert result.agenda == ["Run QC on the dataset", "Identify marker genes"]
    assert len(result.rounds) == 2
    assert result.accepted_steps == 2
    assert result.converged is True
    assert "FINAL REPORT" in result.final_answer
    # both rounds used the Scientist's QC tool and were accepted
    assert all(r.verdict.verdict == "accept" for r in result.rounds)
    assert {e["type"] for e in events} >= {"pi_agenda", "critic", "synthesize", "lab_done"}


# --- convergence is LLM-judged (no fixed score threshold) --------------------

def test_convergence_is_llm_judged_not_thresholded():
    # The Critic accepts with a LOW score (0.4). With the old 0.8 gate this would NOT
    # advance; now convergence is the LLM's call, so the step is accepted.
    complete = _make_complete(agenda=["Run QC"],
                              critic_responses=[{"verdict": "accept", "score": 0.4, "critique": "fine"}])
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)
    result = lab.run("QC the dataset")
    assert result.accepted_steps == 1 and result.converged is True


# --- multi-specialist scientist roles ----------------------------------------

def test_specialist_routing_by_step_wording():
    from bioagent.agents.research_lab import DEFAULT_SPECIALISTS, GENERALIST, _route_specialist
    r = DEFAULT_SPECIALISTS
    assert "QC" in _route_specialist("Run QC and filter cells", r).name
    assert "Pathway" in _route_specialist("Run GO pathway enrichment on the markers", r).name
    assert "Clustering" in _route_specialist("Cluster the cells and find markers", r).name
    assert _route_specialist("write an unrelated summary", r) is GENERALIST


def test_specialist_persona_is_injected_into_the_scientist_brief():
    seen = {}

    def recording_chat(messages, tools):
        seen["brief"] = messages[1]["content"]            # the harness user brief
        return {"content": "", "tool_calls": [
            {"id": "f", "type": "function",
             "function": {"name": "finish", "arguments": json.dumps({"answer": "done"})}}]}

    complete = _make_complete(agenda=["Run QC and filtering"],
                              critic_responses=[{"verdict": "accept", "score": 0.9}])
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=recording_chat)
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)
    result = lab.run("QC the data")
    assert "QC & preprocessing specialist" in seen["brief"]        # persona prepended
    assert result.rounds[0].specialist == "QC & preprocessing specialist"


# --- plan mode: human-in-the-loop review between PI plan and execution -------

def test_plan_mode_revise_replans_then_approves():
    # The user does NOT edit the agenda text — they reply in natural language and the PI
    # RE-DRAFTS. Here the PI proposes a 2-step plan, the user asks to drop a step, the PI
    # returns a 1-step plan, the user approves it, and that re-drafted plan is what runs.
    pi_calls = {"n": 0}
    plans = [["Run QC on the dataset", "Identify marker genes"], ["Only run QC and report"]]

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            i = min(pi_calls["n"], len(plans) - 1)
            pi_calls["n"] += 1
            return json.dumps({"agenda": plans[i]})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        return "FINAL REPORT"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)

    seen = []
    reviews = iter([
        {"action": "revise", "feedback": "drop the marker step, just QC"},
        {"action": "approve"},
    ])

    def review(kind, payload):
        seen.append((kind, list(payload) if isinstance(payload, list) else payload))
        return next(reviews)

    result = lab.run("Characterize the dataset", plan_review=review)
    assert seen[0] == ("agenda", ["Run QC on the dataset", "Identify marker genes"])  # first draft shown
    assert pi_calls["n"] == 2                              # PI re-planned after the NL feedback
    assert result.agenda == ["Only run QC and report"]    # the re-drafted plan is what ran
    assert result.accepted_steps == 1


def test_plan_mode_clarify_then_answer_then_run():
    # When the request is ambiguous the PI may ask a clarify question first; answering it
    # (a "revise" with the chosen option as feedback) re-plans into a concrete agenda.
    pi_calls = {"n": 0}

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            n = pi_calls["n"]
            pi_calls["n"] += 1
            if n == 0:
                return json.dumps({"clarify": [{"question": "Leiden resolution?", "options": ["0.4", "0.8"]}]})
            return json.dumps({"agenda": ["Run QC"]})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "REPORT"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)

    seen = []
    reviews = iter([{"action": "revise", "feedback": "use 0.8"}, {"action": "approve"}])

    def review(kind, payload):
        seen.append(kind)
        return next(reviews)

    result = lab.run("cluster the cells", plan_review=review)
    assert seen == ["clarify", "agenda"]      # PI asked first, then proposed a plan
    assert result.agenda == ["Run QC"]
    assert result.accepted_steps == 1


def test_plan_mode_cancel_runs_nothing():
    complete = _make_complete(agenda=["Run QC"], critic_responses=[{"verdict": "accept", "score": 0.9}])
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)

    events = []
    result = lab.run("anything", on_event=events.append,
                     plan_review=lambda kind, payload: {"action": "cancel"})
    assert result.rounds == [] and result.accepted_steps == 0 and result.converged is False
    assert "cancelled" in result.final_answer.lower()
    assert any(e["type"] == "plan_cancelled" for e in events)


# --- preset: STEERS the PI's planning (does not bypass it) -------------------

def test_preset_prompt_steers_pi_planning_without_bypassing_it():
    from bioagent.agents.presets import get_preset

    seen = {}

    def recording_complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            seen["pi_user"] = messages[1]["content"]
            return json.dumps(["Run QC on the dataset", "Cluster and find markers"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL REPORT"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    preset = get_preset("celltype_annotation")
    lab = ResearchLab(_ctx(), LabConfig(preset_prompt=preset.prompt),
                      complete_fn=recording_complete, scientist=scientist)

    result = lab.run("Annotate my retina snRNA-seq dataset")

    # the preset guidance reached the PI's planning prompt — it STEERS (the PI still
    # produced the agenda and the run still executed it), it did not bypass the PI.
    assert "Follow this research-path guidance" in seen["pi_user"]
    assert "marker-based single-cell cell-type annotation" in seen["pi_user"]
    assert result.agenda == ["Run QC on the dataset", "Cluster and find markers"]
    assert result.converged is True


def test_no_preset_means_no_guidance_line():
    seen = {}

    def recording_complete(messages):
        if "Principal Investigator of a bioinformatics lab" in messages[0]["content"]:
            seen["pi_user"] = messages[1]["content"]
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in messages[0]["content"]:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "REPORT"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=recording_complete, scientist=scientist)
    lab.run("anything")
    assert "research-path guidance" not in seen["pi_user"]


# --- the PI planner sees the dataset's design (condition columns + existing labels) ----

def _ctx_with_dataset(dataset_result):
    return HarnessContext(decisions={"dataset_result": dataset_result}, tunnel_port=1, model="m")


def _record_pi_user(seen, agenda=("Run QC",)):
    def complete(messages):
        if "Principal Investigator of a bioinformatics lab" in messages[0]["content"]:
            seen["pi_user"] = messages[1]["content"]
            return json.dumps(list(agenda))
        if "rigorous scientific Critic" in messages[0]["content"]:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "REPORT"
    return complete


def test_pi_planner_sees_condition_groups_and_existing_labels():
    """The dataset profile (a genotype 'sampleid' + existing 'majorclass' labels) reaches the PI's
    planning prompt verbatim, so it can plan a comparison / reuse labels — the fix for runs that
    ignored a KO-vs-WT design and re-derived annotations from scratch."""
    seen = {}
    dr = {
        "cells": 15307, "genes": 33696, "dataset_kind": "h5ad_single_cell",
        "obs_keys": ["sampleid", "majorclass", "celltype", "percent.mt"],
        "obs_categoricals": {
            "sampleid": {"n": 2, "values": ["DDX41", "WT"]},
            "majorclass": {"n": 11, "values": ["AC", "BC", "Cone", "MG", "Microglia", "Rod"]},
            "celltype": {"n": 130, "values": []},          # high-cardinality -> count only
        },
    }
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx_with_dataset(dr), LabConfig(),
                      complete_fn=_record_pi_user(seen), scientist=scientist)
    lab.run("complete the research and write the topic by yourself")

    pi = seen["pi_user"]
    assert "Dataset profile" in pi
    assert "sampleid: 2 categories [DDX41, WT]" in pi        # condition/group surfaced
    assert "majorclass" in pi and "AC" in pi                 # existing labels surfaced
    assert "celltype: 130 categories" in pi                  # high-card column: count, no flood
    assert "percent.mt" in pi                                # numeric column still named


def test_pi_system_prompt_carries_design_aware_rules():
    from bioagent.agents.research_lab import _PI_SYSTEM
    assert "DATASET PROFILE" in _PI_SYSTEM
    assert "COMPARES the groups" in _PI_SYSTEM               # condition -> comparison rule
    assert "REUSE it" in _PI_SYSTEM                          # existing labels -> reuse rule
    assert "MUST reserve one agenda step" in _PI_SYSTEM       # requested literature -> a literature step
    # The PI's literature step is `deep_literature` (PaperQA over the local corpus on HPC3) as of the
    # paperqa-embedding line; it replaced `literature_search` in the prompt. Pinned by name because
    # WHICH tool the PI reserves is the product decision — the lighter `literature_search` is still
    # in the catalog and still used elsewhere (see _READ_ONLY_TOOLS / focus_literature_query).
    assert "deep_literature" in _PI_SYSTEM


def test_pi_plan_guard_adds_literature_step_when_model_omits_it():
    lit_tool = HarnessTool(
        "literature_search",
        "Search published literature.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        lambda _args, _ctx: {"status": "ok", "results": []},
        category="literature",
    )

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps({"agenda": [
                "Run QC and report the metrics",
                "Cluster the cells and summarize the clusters",
                "Find marker genes per cluster",
                "Compare Ddx41 KO vs WT within each majorclass",
                "Pathway enrichment on top differential expression genes",
            ]})
        raise AssertionError("only planning should run")

    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete,
                      scientist=ResearchHarness(catalog=[lit_tool]))
    kind, agenda = lab._pi_plan(
        "Analyze DDX41 knockout mouse retina and include relevant literature context",
        lambda _event: None,
    )

    assert kind == "agenda"
    # Literature is APPENDED as an extra step — the analysis plan is no longer capped at 5, so no
    # real analysis (e.g. the enrichment step) is dropped to make room for the literature step.
    assert len(agenda) == 6
    assert any("Literature search" in step for step in agenda)
    assert any("Pathway enrichment" in step for step in agenda)
    assert "Literature search" in agenda[-1]   # literature sits at the end


def test_pi_plan_guard_collapses_duplicate_literature_steps():
    lit_tool = HarnessTool(
        "literature_search",
        "Search published literature.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        lambda _args, _ctx: {"status": "ok", "results": []},
        category="literature",
    )

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps({"agenda": [
                "literature_search",
                "Literature search for Search DDX41 retina Return citations evidence",
            ]})
        raise AssertionError("only planning should run")

    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete,
                      scientist=ResearchHarness(catalog=[lit_tool]))
    kind, agenda = lab._pi_plan(
        "Search DDX41 retina. Return citations evidence.",
        lambda _event: None,
    )

    assert kind == "agenda"
    assert agenda == ["Literature search for DDX41 retina"]


def test_literature_context_step_is_deterministically_routed_to_literature_search():
    calls = []

    def literature_exec(args, _ctx):
        calls.append(args)
        return {"status": "ok", "query": args["query"], "results": [{
            "title": "DDX41 in retina",
            "authors": "Mars Z",
            "year": "2026",
            "journal": "bioRxiv",
            "doi": "10.64898/2026.01.28.26344834",
            "pmid": "",
            "url": "https://doi.org/10.64898/2026.01.28.26344834",
            "citation": "Mars Z et al. (2026) DDX41 in retina. doi:10.64898/2026.01.28.26344834",
        }]}

    lit_tool = HarnessTool(
        "literature_search",
        "Search published literature.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        literature_exec,
        category="literature",
    )

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps({"agenda": ["Summarize findings with literature context"]})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 1.0, "critique": "citations found"})
        return "FINAL REPORT"

    def should_not_chat(_messages, _tools):
        raise AssertionError("literature context steps should bypass model tool selection")

    scientist = ResearchHarness(catalog=[lit_tool], chat_fn=should_not_chat)
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)
    result = lab.run("Analyze DDX41 knockout mouse retina and include relevant literature context")

    assert calls
    assert calls[0]["query"] == "DDX41 knockout mouse retina"
    step = result.rounds[0].scientist_result
    assert step["steps"][0]["tool"] == "literature_search"
    assert "10.64898/2026.01.28.26344834" in step["final_answer"]
    assert result.accepted_steps == 1


def test_literature_step_is_backfilled_when_round_budget_is_exhausted():
    # Repro of the empty-References bug: the literature step sits LAST and the shared round
    # budget (max_rounds) is spent on earlier analysis steps, so the loop exits before literature
    # ever runs. The guaranteed-grounding backfill must still run literature_search once and
    # produce an ACCEPTED DOI/PMID round, so the manuscript's ## References are not silently empty.
    lit_calls = []

    def literature_exec(args, _ctx):
        lit_calls.append(args)
        return {"status": "ok", "query": args["query"], "results": [{
            "title": "DDX41 in retina", "authors": "Mars Z", "year": "2026",
            "journal": "bioRxiv", "doi": "10.64898/2026.01.28.26344834", "pmid": "",
            "url": "https://doi.org/10.64898/2026.01.28.26344834",
            "citation": "Mars Z et al. (2026) DDX41 in retina. doi:10.64898/2026.01.28.26344834",
        }]}

    lit_tool = HarnessTool(
        "literature_search", "Search published literature.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        literature_exec, category="literature",
    )
    catalog = list(default_catalog()) + [lit_tool]
    complete = _make_complete(
        agenda=["Run QC and report the metrics", "Summarize findings with literature context"],
        critic_responses=[{"verdict": "accept", "score": 1.0, "critique": "ok"}],
    )
    scientist = ResearchHarness(catalog=catalog, chat_fn=_scientist_calls("run_qc", {}))
    # max_rounds=1: the single QC round exhausts the budget; literature (step 2) is never reached
    # by the main loop and can only be executed by the backfill.
    lab = ResearchLab(_ctx(), LabConfig(max_rounds=1), complete_fn=complete, scientist=scientist)
    result = lab.run("Analyze DDX41 knockout mouse retina and include literature context")

    assert lit_calls, "literature_search must be backfilled even though the round budget was spent"
    lit_rounds = [r for r in result.rounds if r.step_index == 2]
    assert len(lit_rounds) == 1                      # ran exactly once, outside the budget
    assert lit_rounds[0].verdict.verdict == "accept"  # DOI-backed -> deterministically accepted
    assert "10.64898/2026.01.28.26344834" in lit_rounds[0].scientist_result["final_answer"]


def test_default_round_budget_runs_every_planned_step_no_starvation():
    # Regression for the dropped-enrichment bug: the old fixed max_rounds=8 (and max_steps=5)
    # silently skipped later steps once the shared budget was spent. With the derived default
    # budget (max_rounds=None -> len(agenda)*(1+max_revisions)), EVERY planned step runs — the
    # whole point of "do all the planned work, don't cap by count". 9 steps > the old 8-round cap.
    agenda = [f"Analysis step {i}" for i in range(1, 10)]
    complete = _make_complete(
        agenda=agenda,
        critic_responses=[{"verdict": "accept", "score": 1.0, "critique": "ok"}],
    )
    scientist = ResearchHarness(catalog=list(default_catalog()),
                                chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)
    result = lab.run("Run a nine-step analysis")   # no literature requested -> agenda stays at 9

    assert len(result.agenda) == 9
    assert result.accepted_steps == 9                              # nothing starved
    assert {r.step_index for r in result.rounds} == set(range(1, 10))


def test_literature_backfill_does_not_rerun_a_step_already_executed():
    # If the literature step DID run inside the budget, the backfill must not run it a second time.
    lit_calls = []

    def literature_exec(args, _ctx):
        lit_calls.append(args)
        return {"status": "ok", "query": args["query"], "results": [{
            "title": "DDX41 in retina", "authors": "Mars Z", "year": "2026", "journal": "bioRxiv",
            "doi": "10.64898/2026.01.28.26344834", "pmid": "", "url": "u", "citation": "c",
        }]}

    lit_tool = HarnessTool(
        "literature_search", "Search published literature.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        literature_exec, category="literature",
    )

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps({"agenda": ["Summarize findings with literature context"]})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 1.0, "critique": "citations found"})
        return "FINAL REPORT"

    scientist = ResearchHarness(catalog=[lit_tool])
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)
    result = lab.run("Analyze DDX41 knockout mouse retina and include literature context")

    assert len(lit_calls) == 1                       # ran in-loop; backfill did not duplicate it
    assert len([r for r in result.rounds if r.step_index == 1]) == 1


def test_literature_query_uses_findings_not_the_raw_question():
    from bioagent.agents.research_lab import CriticVerdict, LabRound, _literature_query

    def _rd(step, result):
        return LabRound(1, 1, step, "S",
                        {"steps": [{"tool": "t", "ok": True, "result": result}], "final_answer": ""},
                        CriticVerdict("accept", 1.0, ""))

    rounds = [
        _rd("DE", {"top_genes_by_group": {"AC": ["GRIA4", "DLGAP1"], "Rod": ["RHO", "GNAT1"]}}),
        _rd("Enrichment", {"top_terms_by_group": {"AC": ["synaptic signaling"], "Rod": ["phototransduction"]}}),
    ]
    q = _literature_query("finish research and report",
                          "Literature search to ground key pathways in published retinal biology", rounds)
    # the raw-question instruction words are gone; real findings ground the query
    assert "finish" not in q.lower() and "research" not in q.lower()
    assert "GRIA4" in q and ("phototransduction" in q or "synaptic" in q)


def test_literature_query_uses_variant_pathogenic_genes():
    # A VCF/variant run's findings live in pathogenic_variants / high_priority_variants (gene fields),
    # NOT the scanpy top_genes_by_group map — the query must still be the real genes, not the question.
    from bioagent.agents.research_lab import CriticVerdict, LabRound, _literature_query

    def _rd(step, result):
        return LabRound(1, 1, step, "S",
                        {"steps": [{"tool": "annotate_variants", "ok": True, "result": result}], "final_answer": ""},
                        CriticVerdict("accept", 1.0, ""))

    rounds = [_rd("annotate", {
        "pathogenic_variants": [{"gene": "BRCA2"}, {"gene": "TP53"}],
        "high_priority_variants": [{"gene": "DDX41"}],
    })]
    q = _literature_query(
        "Annotate all variants in the uploaded VCF with Ensembl VEP + ClinVar (GRCh38). Filter to PASS.",
        "Literature search for the key genes and pathways found in the analysis", rounds)
    assert "BRCA2" in q and "DDX41" in q
    for junk in ("ensembl", "clinvar", "grch38", "pass", "annotate", "uploaded"):
        assert junk not in q.lower()   # garbled method words must NOT leak into the query


def test_literature_step_label_strips_method_keywords():
    # The plan LABEL must keep the biological subject but drop the VEP-pipeline method words that
    # produced the garbled "Literature search for Ensembl VEP ClinVar GRCh38 filter PASS ..." label.
    from bioagent.agents.research_lab import _literature_step_text
    label = _literature_step_text(
        "Annotate the retinal-disease variants in this VCF with Ensembl VEP and ClinVar on GRCh38, "
        "filter to PASS, and provide the pathogenic findings.").lower()
    assert "literature" in label and "retinal" in label   # biology survives
    for junk in ("ensembl", "clinvar", "grch38", "pass", "vep", "vcf", "annotate", "provide"):
        assert junk not in label


def test_literature_step_label_falls_back_when_prompt_is_pure_filler():
    # A thin/generic run prompt (the preset default when no biological question is given) is all
    # instruction filler — it must NOT leak in as "Literature search for complete the research";
    # the label falls back to the clean generic form. (The real query comes from the findings.)
    from bioagent.agents.research_lab import _literature_step_text
    clean = "Literature search for the key genes and pathways found"
    assert _literature_step_text("complete the research") == clean
    assert _literature_step_text("Interpret this VCF and complete the research") == clean
    # …but a real biological subject still survives untouched.
    assert _literature_step_text("DDX41 retina") == "Literature search for DDX41 retina"


def _dag_complete(agenda, structure_json, coordinator_next):
    """Route completions for a planner='dag' run: PI plan, DAG-structure, Coordinator, Critic."""
    picks = list(coordinator_next)

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(agenda)
        if "structuring an ordered analysis plan into a dependency DAG" in sys:
            return structure_json
        if "You are the Coordinator scheduling" in sys:
            nxt = picks.pop(0) if picks else ""
            return json.dumps({"next": nxt})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        return "FINAL REPORT"

    return complete


def test_dag_planner_runs_branch_and_coordinator_schedules():
    # QC -> Cluster; then DE and Summarize BOTH depend on Cluster (independent branch). The
    # Coordinator picks Summarize (s4) before DE (s3), proving the path is agent-chosen, not fixed.
    agenda = ["Run QC", "Cluster the cells",
              "Differential expression by majorclass", "Summarize cluster composition"]
    structure = json.dumps([
        {"id": "s1", "depends_on": []},
        {"id": "s2", "depends_on": ["s1"]},
        {"id": "s3", "depends_on": ["s2"]},
        {"id": "s4", "depends_on": ["s2"]},
    ])
    complete = _dag_complete(agenda, structure, coordinator_next=["s4"])
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(planner="dag", auto_select_skill=False),
                      complete_fn=complete, scientist=scientist)

    events = []
    result = lab.run("Characterize the retina dataset", on_event=events.append)

    steps = [r.step for r in result.rounds]
    assert steps == ["Run QC", "Cluster the cells",
                     "Summarize cluster composition",          # Coordinator ran s4 before s3
                     "Differential expression by majorclass"]
    assert result.accepted_steps == 4 and result.converged is True
    assert len(result.rounds) == 4                              # each node ran exactly once
    types = {e["type"] for e in events}
    assert "lab_plan_dag" in types and "coordinator_pick" in types


def test_dag_planner_falls_back_to_linear_when_structure_unparseable():
    agenda = ["Run QC", "Cluster", "DE"]
    complete = _dag_complete(agenda, structure_json="not json", coordinator_next=[])
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(planner="dag", auto_select_skill=False),
                      complete_fn=complete, scientist=scientist)
    events = []
    result = lab.run("anything", on_event=events.append)
    # unparseable structure -> linear DAG -> runs in plan order, no branch to coordinate
    assert [r.step for r in result.rounds] == ["Run QC", "Cluster", "DE"]
    assert result.converged is True
    assert "coordinator_pick" not in {e["type"] for e in events}


def test_default_planner_is_linear_and_skips_dag():
    complete = _dag_complete(["Run QC"], structure_json="[]", coordinator_next=[])
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(auto_select_skill=False),   # planner defaults to "linear"
                      complete_fn=complete, scientist=scientist)
    events = []
    lab.run("anything", on_event=events.append)
    assert "lab_plan_dag" not in {e["type"] for e in events}


def _concurrency_lab(max_concurrency=2):
    """A DAG lab whose fake Scientist finishes each node in one turn and whose Critic always accepts —
    so a test can drive _run_dag with a specific plan and observe the BATCHING, not the analysis."""
    def scientist_chat(messages, tools):
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"id": "f", "type": "function",
                    "function": {"name": "finish", "arguments": json.dumps({"answer": "done"})}}]}
        return {"content": "", "tool_calls": [{"id": "t", "type": "function",
                "function": {"name": "note", "arguments": json.dumps({"what": "ok"})}}]}

    def complete(messages):
        if "rigorous scientific Critic" in messages[0]["content"]:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL"          # coordinator etc. fall back to ready[0]

    note = HarnessTool("note", "note", {"type": "object", "properties": {"what": {"type": "string"}}},
                       lambda a, c: {"status": "ok"}, category="analysis")
    return ResearchLab(_ctx(), LabConfig(planner="dag", max_concurrency=max_concurrency),
                       complete_fn=complete, scientist=ResearchHarness(catalog=[note], chat_fn=scientist_chat))


def test_dag_agent_memory_writes_reflects_and_reads_across_runs(tmp_path):
    # Axis C end-to-end: run 1 writes an episode + reflects it into lessons; run 2 (fresh lab, SAME
    # persistent memory dir) reads the lesson into the expert's brief. Cross-run learning, no network.
    briefs: list[str] = []

    def scientist_chat(messages, tools):
        briefs.append(" ".join(str(m.get("content", "")) for m in (messages or [])))
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"id": "f", "type": "function",
                    "function": {"name": "finish", "arguments": json.dumps({"answer": "qc done"})}}]}
        return {"content": "", "tool_calls": [{"id": "t", "type": "function",
                "function": {"name": "note", "arguments": json.dumps({"what": "ok"})}}]}

    def complete(messages):
        sys = messages[0]["content"]
        if "PRIVATE lessons of one specialist" in sys:                 # reflection
            return "- Start mito < 5%; the reviewer rejects 10% as too permissive."
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps({"agenda": ["Run scanpy QC and report the metrics"]})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL"

    note = HarnessTool("note", "note", {"type": "object", "properties": {"what": {"type": "string"}}},
                       lambda a, c: {"status": "ok"}, category="analysis")
    cfg = LabConfig(planner="dag", auto_select_skill=False, agent_memory=True,
                    agent_memory_dir=str(tmp_path))

    def _lab():
        return ResearchLab(_ctx(), cfg, complete_fn=complete,
                           scientist=ResearchHarness(catalog=[note], chat_fn=scientist_chat))

    ev1 = []
    _lab().run("Analyze retina QC", on_event=ev1.append)
    assert any(e["type"] == "memory_reflect" for e in ev1)             # evolved at end of run 1
    assert list(tmp_path.glob("*/lessons.md"))                          # a lesson landed on disk

    briefs.clear()
    ev2 = []
    _lab().run("Analyze retina QC", on_event=ev2.append)               # run 2, same memory dir
    assert any(e["type"] == "memory_read" for e in ev2)                # the expert read its memory
    assert any("Start mito < 5%" in b for b in briefs)                 # …and the lesson reached its brief


def test_agent_memory_off_by_default_no_events(tmp_path):
    briefs: list[str] = []

    def scientist_chat(messages, tools):
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"id": "f", "type": "function",
                    "function": {"name": "finish", "arguments": json.dumps({"answer": "d"})}}]}
        return {"content": "", "tool_calls": [{"id": "t", "type": "function",
                "function": {"name": "note", "arguments": json.dumps({"what": "ok"})}}]}

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps({"agenda": ["Run scanpy QC"]})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL"

    note = HarnessTool("note", "note", {"type": "object", "properties": {"what": {"type": "string"}}},
                       lambda a, c: {"status": "ok"}, category="analysis")
    lab = ResearchLab(_ctx(), LabConfig(planner="dag", auto_select_skill=False),  # memory OFF (default)
                      complete_fn=complete, scientist=ResearchHarness(catalog=[note], chat_fn=scientist_chat))
    ev = []
    lab.run("Analyze", on_event=ev.append)
    assert not any(e["type"] in ("memory_read", "memory_reflect") for e in ev)
    assert not list(tmp_path.glob("*/*"))                              # nothing written


def test_concurrency_safe_classification():
    from bioagent.agents.dag import TaskNode
    from bioagent.agents.research_lab import _concurrency_safe

    qc = TaskNode(id="a", goal="Run scanpy QC")
    de = TaskNode(id="b", goal="Run differential expression")
    lit = TaskNode(id="c", goal="Literature search for the markers")
    assert not _concurrency_safe(qc, de)      # two analysis nodes share scanpy state + checkpoints
    assert _concurrency_safe(qc, lit)         # analysis + independent literature: safe
    assert _concurrency_safe(lit, lit)        # two read-only/external: safe


def test_concurrency_coruns_independent_literature_with_analysis():
    # After QC, an analysis node (enrichment) and an INDEPENDENT literature node are both ready —
    # they co-run in ONE batch. This is the "can be concurrent" half of the mixed scenario.
    from bioagent.agents.dag import LabPlan, TaskNode

    lab = _concurrency_lab(max_concurrency=2)
    plan = LabPlan((
        TaskNode(id="s1", goal="Run scanpy QC"),
        TaskNode(id="s2", goal="Run pathway enrichment", depends_on=("s1",)),
        TaskNode(id="s3", goal="Literature search for the markers", depends_on=("s1",)),
    ))
    events = []
    result = lab._run_dag("q", plan, events.append, None, None,
                          exec_roster=DEFAULT_SPECIALISTS, exec_multi=False, decision_review=None)
    batches = [set(e["nodes"]) for e in events if e["type"] == "concurrency_batch"]
    assert {"s2", "s3"} in batches                       # enrichment + literature ran together
    assert result.accepted_steps == 3                    # all three still completed + merged


def test_concurrency_never_coruns_two_analysis_nodes():
    # Two analysis nodes ready at once (cluster + DE both off QC) must NEVER co-run — they share the
    # checkpoint chain + scanpy global state. This is the "cannot be concurrent" half.
    from bioagent.agents.dag import LabPlan, TaskNode

    lab = _concurrency_lab(max_concurrency=3)
    plan = LabPlan((
        TaskNode(id="s1", goal="Run scanpy QC"),
        TaskNode(id="s2", goal="Cluster the cells", depends_on=("s1",)),
        TaskNode(id="s3", goal="Run differential expression", depends_on=("s1",)),
    ))
    events = []
    result = lab._run_dag("q", plan, events.append, None, None,
                          exec_roster=DEFAULT_SPECIALISTS, exec_multi=False, decision_review=None)
    multi = [e["nodes"] for e in events if e["type"] == "concurrency_batch" and len(e["nodes"]) > 1]
    assert multi == []                                   # no two-analysis batch ever formed
    assert result.accepted_steps == 3                    # but all ran (sequentially)


def test_concurrency_off_by_default_is_sequential():
    from bioagent.agents.dag import LabPlan, TaskNode

    lab = _concurrency_lab(max_concurrency=1)            # default
    plan = LabPlan((
        TaskNode(id="s1", goal="Run scanpy QC"),
        TaskNode(id="s2", goal="Run pathway enrichment", depends_on=("s1",)),
        TaskNode(id="s3", goal="Literature search for the markers", depends_on=("s1",)),
    ))
    events = []
    lab._run_dag("q", plan, events.append, None, None,
                 exec_roster=DEFAULT_SPECIALISTS, exec_multi=False, decision_review=None)
    assert not any(e["type"] == "concurrency_batch" for e in events)   # never batches when off


def test_claim_specialist_picks_the_llm_chosen_expert():
    from bioagent.agents.dag import TaskNode
    from bioagent.agents.research_lab import Specialist

    roster = (Specialist("QC expert", "single-cell QC and filtering", ("qc",)),
              Specialist("Pathway expert", "GO/Reactome enrichment interpretation", ("enrich",)))

    def complete(messages):
        if "assigning the next task" in messages[0]["content"]:
            return json.dumps({"member": 2})       # claim the Pathway expert
        return "{}"

    lab = ResearchLab(_ctx(), LabConfig(planner="dag"), complete_fn=complete)
    claimed = []
    chosen = lab._claim_specialist("q", TaskNode(id="s1", goal="Run pathway enrichment"),
                                   roster, lambda e: claimed.append(e))
    assert chosen.name == "Pathway expert"
    assert any(e["type"] == "node_claim" and e["specialist"] == "Pathway expert" for e in claimed)


def test_claim_specialist_falls_back_to_keyword_routing_on_bad_reply():
    from bioagent.agents.dag import TaskNode
    from bioagent.agents.research_lab import Specialist

    roster = (Specialist("QC expert", "single-cell QC", ("qc", "filter")),
              Specialist("Pathway expert", "enrichment", ("enrich", "pathway")))

    def complete(messages):
        return "not json, no number"               # unusable claim reply

    lab = ResearchLab(_ctx(), LabConfig(planner="dag"), complete_fn=complete)
    chosen = lab._claim_specialist("q", TaskNode(id="s1", goal="pathway enrichment analysis"),
                                   roster, lambda e: None)
    assert chosen.name == "Pathway expert"          # keyword routing still lands the right expert


def test_claim_specialist_single_roster_takes_no_llm_call():
    from bioagent.agents.dag import TaskNode
    from bioagent.agents.research_lab import Specialist

    calls = {"n": 0}

    def complete(messages):
        calls["n"] += 1
        return "{}"

    roster = (Specialist("Only expert", "does everything"),)
    lab = ResearchLab(_ctx(), LabConfig(planner="dag"), complete_fn=complete)
    chosen = lab._claim_specialist("q", TaskNode(id="s1", goal="anything"), roster, lambda e: None)
    assert chosen.name == "Only expert" and calls["n"] == 0


def test_structure_pass_all_roots_falls_back_to_linear():
    # If the structure pass infers NO dependency across >2 steps it almost certainly failed; the
    # guard must fall back to a linear chain so ordering isn't left to the Coordinator's luck.
    def complete(messages):
        if "dependency DAG" in messages[0]["content"]:
            return json.dumps([{"id": f"s{i}", "depends_on": []} for i in range(1, 5)])
        return "{}"
    lab = ResearchLab(_ctx(), LabConfig(planner="dag"), complete_fn=complete)
    plan = lab._structure_agenda_dag("q", ["QC", "Cluster", "DE", "Enrichment"], lambda e: None)
    assert plan.nodes[0].depends_on == ()
    assert all(plan.nodes[i].depends_on == (plan.nodes[i - 1].id,) for i in range(1, len(plan.nodes)))


def test_structure_pass_keeps_a_real_branch():
    def complete(messages):
        if "dependency DAG" in messages[0]["content"]:
            return json.dumps([{"id": "s1", "depends_on": []},
                               {"id": "s2", "depends_on": ["s1"]},
                               {"id": "s3", "depends_on": ["s1"]}])   # s2,s3 branch off s1
        return "{}"
    lab = ResearchLab(_ctx(), LabConfig(planner="dag"), complete_fn=complete)
    plan = lab._structure_agenda_dag("q", ["QC", "A", "B"], lambda e: None)
    s3 = next(n for n in plan.nodes if n.id == "s3")
    assert s3.depends_on == ("s1",)   # branch preserved (NOT collapsed to a chain)


def test_all_roots_fallback_preserves_decision_flag():
    def complete(messages):
        if "dependency DAG" in messages[0]["content"]:
            return json.dumps([{"id": "s1", "depends_on": []},
                               {"id": "s2", "depends_on": [], "decision": True, "options": ["A", "B"]},
                               {"id": "s3", "depends_on": []}])
        return "{}"
    lab = ResearchLab(_ctx(), LabConfig(planner="dag"), complete_fn=complete)
    plan = lab._structure_agenda_dag("q", ["QC", "Annotate", "DE"], lambda e: None)
    s2 = next(n for n in plan.nodes if n.id == "s2")
    assert s2.decision and s2.options == ("A", "B")   # decision survives the linear fallback
    assert s2.depends_on == ("s1",)                    # …and it's now a linear chain


def _dag_hitl_lab(complete, briefs):
    """A planner='dag' lab whose Scientist records each step's brief (so a test can assert the
    human's decision reached it), with revisions off to isolate scheduling."""
    def scientist_chat(messages, tools):
        briefs.append(" ".join(str(m.get("content", "")) for m in (messages or [])))
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"id": "f", "type": "function",
                    "function": {"name": "finish", "arguments": json.dumps({"answer": "done"})}}]}
        return {"content": "", "tool_calls": [{"id": "t", "type": "function",
                "function": {"name": "note", "arguments": json.dumps({"what": "ok"})}}]}

    note = HarnessTool("note", "note", {"type": "object", "properties": {"what": {"type": "string"}}},
                       lambda a, c: {"status": "ok"}, category="analysis")
    return ResearchLab(_ctx(), LabConfig(planner="dag", auto_select_skill=False, max_revisions=0),
                       complete_fn=complete, scientist=ResearchHarness(catalog=[note], chat_fn=scientist_chat))


def _dag_hitl_complete(messages):
    sys = messages[0]["content"]
    if "dependency DAG" in sys:                       # structure pass: mark s2 a decision node
        return json.dumps([
            {"id": "s1", "depends_on": []},
            {"id": "s2", "depends_on": ["s1"], "decision": True,
             "options": ["Use existing majorclass labels", "Re-cluster de-novo"]},
        ])
    if "alternative ways to accomplish" in sys:       # failure fork: LLM-proposed alternatives
        return json.dumps(["Lower the resolution to 0.5", "Reuse the existing labels"])
    if "Principal Investigator of a bioinformatics lab" in sys:
        return json.dumps({"agenda": ["Run QC", "Annotate the cells"]})
    if "rigorous scientific Critic" in sys:
        return json.dumps({"verdict": "accept", "score": 0.9})
    return "FINAL REPORT"


def test_dag_decision_node_pauses_and_injects_human_choice():
    briefs: list[str] = []
    lab = _dag_hitl_lab(_dag_hitl_complete, briefs)
    seen = []

    def decision_review(node):
        seen.append({"goal": node.goal, "options": list(node.options)})
        return {"action": "proceed", "choice": "Use existing majorclass labels"}

    events = []
    lab.run("Analyze retina and annotate the cells", on_event=events.append, decision_review=decision_review)

    assert len(seen) == 1                                   # decision_review invoked once (for s2)
    assert seen[0]["options"] == ["Use existing majorclass labels", "Re-cluster de-novo"]
    assert any(e["type"] == "decision_point" for e in events)
    assert any(e["type"] == "decision_made" and e["choice"] == "Use existing majorclass labels"
               for e in events)
    # the human's choice reached the Scientist's brief for the annotate step
    assert any("Use existing majorclass labels" in b for b in briefs)


def test_dag_decision_cancel_stops_the_run():
    briefs: list[str] = []
    lab = _dag_hitl_lab(_dag_hitl_complete, briefs)
    events = []
    result = lab.run("Analyze retina and annotate the cells", on_event=events.append,
                     decision_review=lambda node: {"action": "cancel"})
    assert any(e["type"] == "run_cancelled" for e in events)
    assert not result.converged


def test_dag_decision_without_hook_is_advisory_only():
    # No decision_review wired: the decision node still runs, but nothing pauses and no forced
    # choice is injected — the agent proceeds on its own judgment.
    briefs: list[str] = []
    lab = _dag_hitl_lab(_dag_hitl_complete, briefs)
    events = []
    lab.run("Analyze retina and annotate the cells", on_event=events.append)  # decision_review=None
    assert any(e["type"] == "decision_point" for e in events)
    assert not any(e["type"] == "decision_made" for e in events)
    assert not any("The user was asked how to proceed" in b for b in briefs)


# --- Failure → HITL decision fork (a hard-failed step asks retry/skip/abort) ---------------------

def _dag_always_revise_complete(agenda_steps):
    """A planner='dag' complete_fn whose Critic ALWAYS revises → every step hard-fails, so the failure
    fork is exercised. Also serves the LLM's alternative-approach proposals for the fork."""
    def _c(messages):
        sys = messages[0]["content"]
        if "dependency DAG" in sys:
            return json.dumps([{"id": f"s{i + 1}", "depends_on": []} for i in range(len(agenda_steps))])
        if "alternative ways to accomplish" in sys:
            return json.dumps(["Lower the resolution to 0.5", "Reuse the existing labels"])
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps({"agenda": agenda_steps})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "revise", "score": 0.1, "critique": "produced nothing usable"})
        return "FINAL REPORT"
    return _c


def test_failure_decision_offers_llm_alternatives_and_maps_choice():
    from bioagent.agents.dag import TaskNode
    lab = _dag_hitl_lab(_dag_hitl_complete, [])          # its complete_fn returns 2 alternatives
    node = TaskNode(id="s1", goal="Cluster the cells")
    seen = {}

    def dr(ans):
        def _review(fork):
            seen["options"] = list(fork.options)
            return ans
        return _review
    noop = lambda _e: None   # noqa: E731

    # the LLM's alternatives become the options, followed by the Skip / Abort controls
    out = lab._failure_decision("q", node, "boom", [], dr({"action": "proceed", "choice": "Lower the resolution to 0.5"}), noop)
    assert seen["options"][:2] == ["Lower the resolution to 0.5", "Reuse the existing labels"]
    assert seen["options"][-2:] == ["Skip this step", "Abort the run"]
    assert out == ("retry", "Lower the resolution to 0.5")       # a chosen alternative -> retry with it
    assert lab._failure_decision("q", node, "b", [], dr({"action": "proceed", "choice": "Skip this step"}), noop) == ("skip", "")
    assert lab._failure_decision("q", node, "b", [], dr({"action": "proceed", "choice": "Abort the run"}), noop) == ("abort", "")
    assert lab._failure_decision("q", node, "b", [], dr({"action": "cancel"}), noop) == ("abort", "")
    assert lab._failure_decision("q", node, "b", [], dr({"action": "proceed", "choice": ""}), noop) == ("skip", "")  # timeout


def test_failed_step_offers_alternatives_then_retries_then_skips():
    lab = _dag_hitl_lab(_dag_always_revise_complete(["Run QC"]), [])
    calls = {"n": 0, "opts": None}

    def decision_review(fork):
        calls["n"] += 1
        calls["opts"] = list(fork.options)
        return {"action": "proceed",
                "choice": "Lower the resolution to 0.5" if calls["n"] == 1 else "Skip this step"}

    events = []
    lab.run("q", on_event=events.append, decision_review=decision_review)
    assert calls["opts"][:2] == ["Lower the resolution to 0.5", "Reuse the existing labels"]  # LLM alts
    assert any(e["type"] == "step_failure" and e.get("alternatives") for e in events)
    assert any(e["type"] == "step_retry" for e in events)          # picked an alternative once
    assert calls["n"] == 2                                         # asked twice (alt, then skip)
    assert any(e["type"] == "step_force_advance" for e in events)


def test_failed_step_abort_cancels_the_run():
    lab = _dag_hitl_lab(_dag_always_revise_complete(["Run QC"]), [])
    events = []
    result = lab.run("q", on_event=events.append,
                     decision_review=lambda fork: {"action": "proceed", "choice": "Abort the run"})
    assert any(e["type"] == "step_failure" for e in events)
    assert any(e["type"] == "run_cancelled" for e in events)
    assert not result.converged


def test_failed_step_bypass_self_heals_with_alternative_then_advances():
    # Headless / bypass (decision_review=None): auto-apply the LLM's top alternative (bounded), instead
    # of a silent skip — then force-advance if it still fails. No human is asked.
    lab = _dag_hitl_lab(_dag_always_revise_complete(["Run QC"]), [])
    events = []
    lab.run("q", on_event=events.append)
    assert any(e["type"] == "step_retry" and e.get("approach") for e in events)   # self-healed
    assert any(e["type"] == "step_force_advance" for e in events)                  # then advanced


def test_failed_step_fork_is_capped_against_infinite_retry():
    lab = _dag_hitl_lab(_dag_always_revise_complete(["Run QC"]), [])
    calls = {"n": 0}

    def always_pick_alt(fork):
        calls["n"] += 1
        return {"action": "proceed", "choice": "Lower the resolution to 0.5"}

    events = []
    lab.run("q", on_event=events.append, decision_review=always_pick_alt)
    assert calls["n"] == 2                                          # capped at _MAX_FAILURE_FORKS asks
    assert any(e["type"] == "step_force_advance" for e in events)   # then force-advances despite retries


def test_celltype_column_detection():
    from bioagent.agents.research_lab import _looks_like_celltype_col

    for col in ("celltype", "cell_type", "majorclass", "major_class", "subclass",
                "annotation", "predicted_label", "cell_label"):
        assert _looks_like_celltype_col(col), col
    for col in ("leiden", "louvain", "donor", "age", "sampleid", "percent.mt", "nCount_RNA"):
        assert not _looks_like_celltype_col(col), col


def test_dataset_profile_flags_existing_annotation_for_groupby():
    # WITH a real experimental contrast (sampleid = KO vs WT): enrichment/DE IS meaningful, so the
    # profile steers DE/enrichment onto the annotation column (not de-novo leiden numbers).
    from bioagent.agents.research_harness import HarnessContext
    from bioagent.agents.research_lab import LabConfig, ResearchLab

    ctx = HarnessContext(decisions={"dataset_result": {
        "cells": 11977, "genes": 36601,
        "obs_categoricals": {
            "majorclass": {"n": 6, "values": ["AC", "BC", "Cone", "HC", "MG", "Rod"]},
            "celltype": {"n": 66, "values": []},
            "sampleid": {"n": 2, "values": ["DDX41", "WT"]},     # <-- a real 2-group contrast
        },
        "obs_keys": ["majorclass", "celltype", "sampleid", "leiden"],
    }}, tunnel_port=1, model="m")
    lab = ResearchLab(ctx, LabConfig(), complete_fn=lambda m: "x")
    prof = lab._dataset_context()
    assert "ALREADY annotated" in prof
    assert "majorclass" in prof and "groupby" in prof          # steers DE/enrichment onto it
    # de-novo cluster columns are NOT proposed as the annotation to group on
    assert 'groupby="leiden"' not in prof


def test_dataset_profile_no_contrast_suppresses_enrichment():
    # The single-annotated-sample case (Dr. Chen's "meaningless enrichment"): already cell-type
    # annotated AND no 2+-category non-annotation column → NO differential question, so the profile
    # must steer the planner AWAY from pathway enrichment / discovery-DE rather than onto a groupby.
    from bioagent.agents.research_harness import HarnessContext
    from bioagent.agents.research_lab import LabConfig, ResearchLab

    ctx = HarnessContext(decisions={"dataset_result": {
        "cells": 11977, "genes": 36601,
        "obs_categoricals": {
            "majorclass": {"n": 6, "values": ["AC", "BC", "Cone", "HC", "MG", "Rod"]},
            "celltype": {"n": 66, "values": []},
            "donor": {"n": 1, "values": ["Chen_19_D003"]},
            "sampleid": {"n": 1, "values": ["Chen_a_10x3_Lobe_19_D003_Nu"]},
            "tissue": {"n": 1, "values": ["lobe"]},
        },
        "obs_keys": ["majorclass", "celltype", "donor", "sampleid", "tissue", "leiden"],
    }}, tunnel_port=1, model="m")
    lab = ResearchLab(ctx, LabConfig(), complete_fn=lambda m: "x")
    prof = lab._dataset_context()
    assert "ALREADY annotated" in prof
    assert "NO experimental contrast" in prof
    assert "do NOT plan pathway/GO enrichment" in prof
    # it must NOT tell the planner to just run enrichment grouped by the annotation
    assert "groupby=" not in prof


def test_no_contrast_detection_and_enrichment_step_classification():
    from bioagent.agents.research_lab import _annotated_without_contrast, _is_enrichment_step

    # already annotated + single sample (donor n=1) → no contrast
    retina = {"obs_categoricals": {"majorclass": {"n": 6, "values": []},
                                   "donor": {"n": 1, "values": ["d"]}},
              "obs_keys": ["majorclass", "donor", "leiden"]}
    # already annotated + a real 2-group contrast → HAS contrast
    ko_wt = {"obs_categoricals": {"majorclass": {"n": 6, "values": []},
                                  "sampleid": {"n": 2, "values": ["KO", "WT"]}},
             "obs_keys": ["majorclass", "sampleid"]}
    # unannotated single sample → not the target regime (annotation is the real work there)
    raw = {"obs_categoricals": {"donor": {"n": 1, "values": ["d"]}}, "obs_keys": ["donor"]}
    assert _annotated_without_contrast(retina) is True
    assert _annotated_without_contrast(ko_wt) is False
    assert _annotated_without_contrast(raw) is False
    assert _annotated_without_contrast(None) is False

    assert _is_enrichment_step("Run pathway enrichment analysis on the DE results for each major class")
    assert _is_enrichment_step("Perform over-representation analysis to identify enriched Gene Ontology terms")
    assert _is_enrichment_step("Determine which biological pathways are over-represented in the marker genes")
    # marker DE / QC / clustering / literature are NOT enrichment (kept)
    assert not _is_enrichment_step("Run scanpy QC to filter low-quality cells and select HVGs")
    assert not _is_enrichment_step("Identify marker genes for each major cell class via differential expression")
    assert not _is_enrichment_step("Compute PCA, UMAP, and Leiden clustering")
    assert not _is_enrichment_step("Search literature to contextualize the marker genes")
    # a literature step that MENTIONS enriched pathways must NOT be misread as an enrichment step
    assert not _is_enrichment_step("Search the published literature for key marker genes and enriched pathways")
    assert not _is_enrichment_step("Synthesize the biological interpretation of the enriched pathways")


def test_run_prunes_enrichment_when_no_contrast(monkeypatch):
    # End-to-end wiring: a PI that plans an enrichment step on a single annotated retina sample must
    # have that step dropped before execution; QC / clustering / DE / literature survive.
    from bioagent.agents.research_harness import HarnessContext, ResearchHarness
    from bioagent.agents.research_lab import LabConfig, ResearchLab

    agenda = [
        "Run scanpy QC and select highly variable genes",
        "Compute PCA, UMAP, and Leiden clustering",
        "Identify marker genes per major cell class via differential expression",
        "Run pathway enrichment analysis on the DE results for each major class",
        "Search literature to contextualize the markers",
    ]
    ctx = HarnessContext(decisions={"dataset_result": {
        "cells": 100, "genes": 200,
        "obs_categoricals": {"majorclass": {"n": 6, "values": ["AC", "BC", "Cone", "HC", "MG", "Rod"]},
                             "donor": {"n": 1, "values": ["D003"]}},
        "obs_keys": ["majorclass", "donor"],
    }}, tunnel_port=1, model="m")
    lab = ResearchLab(ctx, LabConfig(auto_select_skill=False),
                      complete_fn=lambda m: "x",
                      scientist=ResearchHarness(catalog=[], chat_fn=lambda m, t: {"content": "", "tool_calls": []}))
    # stub planning + execution so run() reaches the prune and stops cheaply
    monkeypatch.setattr(lab, "_pi_plan", lambda *a, **k: ("agenda", list(agenda)))
    monkeypatch.setattr(lab, "_route_mode", lambda *a, **k: "single")
    captured = {}

    def fake_run_loop(question, ag, emit, *a, **k):
        captured["agenda"] = list(ag)
        from bioagent.agents.research_lab import LabResult
        return LabResult(question, ag, [], True, len(ag), "done")
    monkeypatch.setattr(lab, "_run_loop", fake_run_loop)

    events = []
    lab.run("complete the research", on_event=events.append)
    ran = captured["agenda"]
    assert not any("enrichment" in s.lower() or "pathway" in s.lower() for s in ran)   # dropped
    assert any("QC" in s for s in ran) and any("clustering" in s.lower() for s in ran)  # kept
    assert any("differential expression" in s.lower() for s in ran)                     # kept
    assert any("literature" in s.lower() for s in ran)                                  # kept
    assert any(e.get("type") == "steps_pruned" for e in events)


def test_methods_performed_lists_only_executed_tools():
    from bioagent.agents.research_lab import CriticVerdict, LabRound, _methods_performed

    def rnd(step, tools, verdict="accept"):
        return LabRound(1, 1, step, "sci",
                        {"final_answer": "x", "steps": [{"tool": t, "status": "ok"} for t in tools]},
                        CriticVerdict(verdict, 1.0, ""))

    qc = rnd("QC", ["run_scanpy_qc"])
    de = rnd("DE", ["run_de", "finish"])
    never_ran = rnd("scGPT annotation", ["scgpt_annotate"], verdict="revise")   # not accepted
    out = _methods_performed([qc, de, never_ran])
    assert "run_scanpy_qc" in out and "run_de" in out
    assert "scgpt_annotate" not in out          # never accepted -> not an executed method
    assert "finish" not in out                  # the sentinel is not a method
    assert _methods_performed([never_ran]) == ""   # nothing accepted -> empty allowlist


def test_run_emits_skills_loaded_after_planning(monkeypatch):
    from bioagent.agents.research_harness import HarnessContext, ResearchHarness
    from bioagent.agents.research_lab import LabConfig, LabResult, ResearchLab

    ctx = HarnessContext(decisions={}, tunnel_port=1, model="m")
    lab = ResearchLab(ctx, LabConfig(auto_select_skill=False), complete_fn=lambda m: "x",
                      scientist=ResearchHarness(catalog=[], chat_fn=lambda m, t: {"content": "", "tool_calls": []}))
    monkeypatch.setattr(lab, "_pi_plan", lambda *a, **k: ("agenda", ["Run QC"]))
    monkeypatch.setattr(lab, "_route_mode", lambda *a, **k: "single")
    monkeypatch.setattr(lab, "_run_loop",
                        lambda q, ag, emit, *a, **k: LabResult(q, ag, [], True, len(ag), "done"))
    events = []
    lab.run("q", on_event=events.append)
    sl = [e for e in events if e.get("type") == "skills_loaded"]
    assert len(sl) == 1 and sl[0]["skills"] == []   # no skill loaded -> emitted with empty list


def test_literature_step_label_ignores_meeting_feedback():
    from bioagent.agents.research_lab import _ensure_literature_agenda, _is_literature_step

    out = _ensure_literature_agenda(
        ["Run QC", "Cluster cells"],
        question="Analyze DDX41 retina and include literature context",
        guidance=None,
        # a team design-meeting synthesis used to leak verbatim into the literature step label
        feedback="Team design-meeting synthesis (incorporate into the plan): Convergence Divergences Core Conditional",
        max_steps=5, has_literature_tool=True,
    )
    lit = next(s for s in out if _is_literature_step(s))
    assert "Convergence" not in lit and "Divergences" not in lit and "design-meeting" not in lit


def test_accepted_findings_block_forbids_rerunning_upstream():
    from bioagent.agents.research_lab import CriticVerdict, LabConfig, LabRound, ResearchLab

    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=lambda m: "x")
    rounds = [LabRound(1, 1, "Run QC", "S",
                       {"steps": [{"tool": "run_scanpy_qc", "ok": True, "result": {"status": "ok"}}],
                        "final_answer": "QC done"},
                       CriticVerdict("accept", 1.0, ""))]
    block = lab._accepted_findings_block(rounds)
    assert "DO NOT RE-RUN" in block and "DESYNCHRONIZES" in block


def test_grounding_vocab_pins_classes_and_enrichment_terms():
    from bioagent.agents.research_lab import CriticVerdict, LabRound, _grounding_vocab

    def _rd(step, result):
        return LabRound(1, 1, step, "S",
                        {"steps": [{"tool": "t", "ok": True, "result": result}], "final_answer": ""},
                        CriticVerdict("accept", 1.0, ""))

    rounds = [
        _rd("DE", {"top_genes_by_group": {"Rod": ["PDE6A"], "MG": ["RLBP1"]}}),
        _rd("Enrichment", {"top_terms_by_group": {"Rod": ["Visual Phototransduction"]}}),
    ]
    vocab = _grounding_vocab(rounds)
    assert "Rod" in vocab and "MG" in vocab                       # exact class labels pinned
    assert "Visual Phototransduction" in vocab                    # only real enriched terms
    # a term that was never produced (the 407 hallucination) is NOT in the closed vocabulary
    assert "epithelial" not in vocab.lower() and "mesenchymal" not in vocab.lower()


def test_grounding_facts_pins_numbers_assembly_and_pass_split():
    # Anti-fabrication: the report echoed the plan's "GRCh38" (real build was GRCh37) and invented
    # "0 non-PASS" (QC had n_filtered=212935). _grounding_facts pins the authoritative figures so the
    # synthesize prompt cannot do either.
    from bioagent.agents.research_lab import CriticVerdict, LabRound, _grounding_facts

    def _rd(step, result, verdict="accept"):
        return LabRound(1, 1, step, "S",
                        {"steps": [{"tool": "t", "ok": True, "result": result}], "final_answer": ""},
                        CriticVerdict(verdict, 1.0, ""))

    rounds = [
        _rd("QC", {"n_pass": 4721988, "n_filtered": 212935, "ti_tv_ratio": 1.98}),
        _rd("Annotate", {"assembly": "GRCh37", "execution_mode": "offline_vep",
                         "variant_filters": {"n_input": 5000, "n_kept": 1213, "max_pop_af": 0.01},
                         "by_impact": {"HIGH": 5, "MODERATE": 22}}),
    ]
    facts = _grounding_facts(rounds)
    assert "assembly = GRCh37" in facts and "GRCh38" not in facts        # the REAL build is pinned
    assert "n_pass = 4721988" in facts and "n_filtered = 212935" in facts  # PASS split pinned...
    assert "0 non-PASS" in facts                                          # ...+ the rule against inventing it
    assert "n_kept = 1213" in facts and "max_pop_af = 0.01" in facts      # nested variant_filters pulled
    assert "by_impact" in facts and "HIGH: 5" in facts
    # a non-accepted step contributes nothing; no facts -> empty block
    not_accepted = _rd("x", {"assembly": "GRCh38", "n_pass": 1}, verdict="revise")
    assert _grounding_facts([not_accepted]) == ""


def test_verify_report_facts_corrects_wrong_assembly_and_pass_split():
    # The GUARANTEE layer catches fabrications the grounding prompt failed to prevent, deterministically.
    from bioagent.agents.research_lab import verify_report_facts

    facts = {"assembly": "GRCh37", "n_filtered": 212935}
    md = ("## Methods\n1. Processed the VCF using GRCh38 assembly parameters. "
          "PASS filter: 4,721,988 PASS vs 0 non-PASS records. VEP ran on the hg38 cache.\n")
    out, issues = verify_report_facts(md, facts)
    assert "GRCh38" not in out and "hg38" not in out             # wrong builds swapped to the real one
    assert out.count("GRCh37") >= 2                              # both assembly mentions corrected
    assert "0 non-PASS" not in out and "212,935 non-PASS" in out  # the fabricated split is corrected
    assert any(i.startswith("assembly") for i in issues) and any(i.startswith("pass-split") for i in issues)


def test_verify_report_facts_noop_when_consistent():
    from bioagent.agents.research_lab import verify_report_facts

    # assembly matches + n_nonpass IS genuinely 0 → nothing to correct, no false positives.
    facts = {"assembly": "GRCh37", "n_nonpass": 0}
    md = "Annotated against GRCh37. 4,721,988 PASS vs 0 non-PASS records.\n"
    out, issues = verify_report_facts(md, facts)
    assert out == md and issues == []


def test_grounding_vocab_empty_without_findings():
    from bioagent.agents.research_lab import _grounding_vocab

    assert _grounding_vocab([]) == ""


def test_synthesize_prompt_carries_grounding_vocab():
    from bioagent.agents.research_lab import CriticVerdict, LabRound, ResearchLab

    seen = {}

    def complete(messages):
        seen["synth_user"] = messages[1]["content"]
        return "FINAL REPORT"

    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete)
    rounds = [
        LabRound(1, 1, "Enrichment", "S",
                 {"steps": [{"tool": "run_enrichment", "ok": True,
                             "result": {"top_terms_by_group": {"Rod": ["Visual Phototransduction"]}}}],
                  "final_answer": "done"},
                 CriticVerdict("accept", 1.0, "")),
    ]
    lab._synthesize("Analyze retina", rounds, lambda _e: None)
    assert "Grounding vocabulary" in seen["synth_user"]
    assert "Visual Phototransduction" in seen["synth_user"]


def test_parse_query_list_sanitizes_dedupes_and_caps():
    from bioagent.agents.research_lab import _parse_query_list

    raw = json.dumps([
        "RHO GNAT1 rod photoreceptor",           # kept
        "Please search RHO GNAT1 rod photoreceptor references",  # sanitizes to a dup -> dropped
        "GRIA4 amacrine synaptic signaling",     # kept
        "phototransduction retina",              # kept
        "inherited retinal degeneration",        # kept but over the cap
    ])
    out = _parse_query_list(raw, 3)
    assert len(out) == 3                                   # capped
    assert all("please" not in q.lower() and "references" not in q.lower() for q in out)
    assert len({q.lower() for q in out}) == len(out)       # de-duplicated


def test_parse_query_list_returns_empty_on_non_json():
    from bioagent.agents.research_lab import _parse_query_list

    assert _parse_query_list("FINAL REPORT", 4) == []
    assert _parse_query_list('{"not": "a list"}', 4) == []


def test_findings_digest_groups_markers_and_pathways_by_class():
    from bioagent.agents.research_lab import CriticVerdict, LabRound, _literature_findings_digest

    def _rd(step, result):
        return LabRound(1, 1, step, "S",
                        {"steps": [{"tool": "t", "ok": True, "result": result}], "final_answer": ""},
                        CriticVerdict("accept", 1.0, ""))

    rounds = [
        _rd("DE", {"top_genes_by_group": {"Rod": ["RHO", "GNAT1"], "AC": ["GRIA4"]}}),
        _rd("Enrichment", {"top_terms_by_group": {"Rod": ["phototransduction"]}}),
    ]
    digest = _literature_findings_digest(rounds)
    assert "Rod:" in digest and "RHO" in digest and "phototransduction" in digest
    assert "AC:" in digest and "GRIA4" in digest


def test_literature_step_plans_multiple_queries_and_merges_citations():
    # The LLM planner returns SEVERAL angle-specific queries; the step searches each and merges
    # citations, de-duplicating a paper that shows up under more than one query (same DOI).
    calls = []

    def literature_exec(args, _ctx):
        calls.append(args["query"])
        idx = len(calls)
        return {"status": "ok", "query": args["query"], "results": [
            {"title": f"paper{idx}", "authors": "A", "year": "2026", "journal": "J",
             "doi": f"10.1/{idx}", "pmid": "", "url": "u", "citation": f"c{idx}"},
            {"title": "shared", "authors": "B", "year": "2025", "journal": "J",
             "doi": "10.1/shared", "pmid": "", "url": "u", "citation": "shared"},
        ]}

    lit_tool = HarnessTool(
        "literature_search", "Search published literature.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        literature_exec, category="literature",
    )

    planned = ["RHO rod phototransduction", "GRIA4 amacrine synaptic", "retinal degeneration mechanism"]

    def complete(messages):
        sys = messages[0]["content"]
        if "composing search queries for Europe PMC" in sys:
            return json.dumps(planned)
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps({"agenda": ["Summarize findings with literature context"]})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL REPORT"

    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete,
                      scientist=ResearchHarness(catalog=[lit_tool]))
    result = lab.run("Analyze DDX41 retina and include literature context")

    assert calls == planned                      # each distinct query was searched
    lit = next(r for r in result.rounds if r.step_index == 1)
    tcalls = lit.scientist_result.get("steps", [])
    assert len(tcalls) == 3                       # one tool call per query
    # merged answer keeps all 3 unique papers (rendered by their `citation` c1/c2/c3) plus the
    # single shared one, de-duplicated from 3 hits down to 1
    answer = lit.scientist_result.get("final_answer") or ""
    assert "c1" in answer and "c2" in answer and "c3" in answer
    assert answer.count("shared") == 1


def test_literature_step_falls_back_to_single_query_when_planner_unusable():
    calls = []

    def literature_exec(args, _ctx):
        calls.append(args["query"])
        return {"status": "ok", "query": args["query"], "results": [{
            "title": "x", "authors": "A", "year": "2026", "journal": "J",
            "doi": "10.1/x", "pmid": "", "url": "u", "citation": "c"}]}

    lit_tool = HarnessTool(
        "literature_search", "Search published literature.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        literature_exec, category="literature",
    )

    def complete(messages):
        sys = messages[0]["content"]
        if "composing search queries for Europe PMC" in sys:
            return "sorry, no JSON here"          # planner output unusable -> single fallback query
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps({"agenda": ["Summarize findings with literature context"]})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL REPORT"

    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete,
                      scientist=ResearchHarness(catalog=[lit_tool]))
    lab.run("Analyze DDX41 retina and include literature context")
    assert len(calls) == 1                        # fell back to ONE deterministic query


def test_literature_step_runs_once_even_if_critic_revises():
    # A literature step's query is deterministic — a "revise" would re-run the identical query.
    # The loop must take ONE attempt and advance, not call literature_search 3x.
    calls = []

    def literature_exec(args, _ctx):
        calls.append(args)
        return {"status": "ok", "query": args["query"], "results": [{
            "title": "x", "authors": "A", "year": "2026", "journal": "J",
            "doi": "10.1/x", "pmid": "", "url": "u", "citation": "c"}]}

    lit_tool = HarnessTool(
        "literature_search", "Search published literature.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        literature_exec, category="literature",
    )

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps({"agenda": ["Summarize findings with literature context"]})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "revise", "score": 0.2, "critique": "not good enough"})
        return "FINAL REPORT"

    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete,
                      scientist=ResearchHarness(catalog=[lit_tool]))
    result = lab.run("Analyze DDX41 retina and include literature context")

    assert len(calls) == 1                          # ran ONCE, not revise-looped
    assert len([r for r in result.rounds if r.step_index == 1]) == 1


def test_literature_step_detection_does_not_match_background_rna_cleanup():
    from bioagent.agents.research_lab import _is_literature_step

    assert _is_literature_step("Summarize findings with literature context")
    assert not _is_literature_step("Run ambient background RNA correction")


def test_no_dataset_means_no_profile_line():
    seen = {}
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=_record_pi_user(seen), scientist=scientist)
    lab.run("anything")
    assert "Dataset profile" not in seen["pi_user"]


def test_preset_registry_lookup():
    from bioagent.agents.presets import PRESETS, get_preset, list_presets

    p = get_preset("celltype_annotation")
    assert p is not None and p.key in PRESETS and "annotation" in p.label.lower()
    assert get_preset("nope") is None and get_preset(None) is None
    assert any(d["key"] == "celltype_annotation" and d["prompt"] for d in list_presets())


# --- Axis B: the PI autonomously selects a skill (researcher does not pick) ---

def test_pi_autonomously_selects_skill_when_no_preset_given():
    from bioagent.agents.presets import ResearchPreset

    lib = (
        ResearchPreset("celltype_annotation", "Cell-type annotation", "CANONICAL ANNOTATION GUIDANCE"),
        ResearchPreset("scgpt_annotation", "scGPT label transfer", "SCGPT GUIDANCE"),
    )
    seen = {}

    def complete(messages):
        sys = messages[0]["content"]
        if "research-protocol router" in sys:
            return "celltype_annotation"               # the PI picks the skill itself
        if "Principal Investigator of a bioinformatics lab" in sys:
            seen["pi_user"] = messages[1]["content"]
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL REPORT"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(skill_library=lib),   # auto_select_skill defaults True
                      complete_fn=complete, scientist=scientist)
    events = []
    lab.run("Annotate my snRNA-seq dataset", on_event=events.append)

    # the PI's own choice steered planning (the chosen skill's BODY reached the PI prompt)
    assert "Follow this research-path guidance" in seen["pi_user"]
    assert "CANONICAL ANNOTATION GUIDANCE" in seen["pi_user"]
    assert any(e.get("type") == "skill_selected" and e.get("key") == "celltype_annotation" for e in events)


def test_explicit_preset_overrides_pi_skill_selection():
    from bioagent.agents.presets import ResearchPreset

    lib = (ResearchPreset("celltype_annotation", "Cell-type annotation", "AUTO-PICKED GUIDANCE"),)
    seen = {}
    calls = {"router": 0}

    def complete(messages):
        sys = messages[0]["content"]
        if "research-protocol router" in sys:
            calls["router"] += 1
            return "celltype_annotation"
        if "Principal Investigator of a bioinformatics lab" in sys:
            seen["pi_user"] = messages[1]["content"]
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL REPORT"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(preset_prompt="USER FORCED GUIDANCE", skill_library=lib),
                      complete_fn=complete, scientist=scientist)
    lab.run("anything")

    # the user's explicit path wins; the PI router is never consulted
    assert calls["router"] == 0
    assert "USER FORCED GUIDANCE" in seen["pi_user"]
    assert "AUTO-PICKED GUIDANCE" not in seen["pi_user"]


def test_pinned_skills_are_mandatory_and_auto_augments():
    # The console multi-select PINS skills (mandatory); the PI's auto-select still runs and ADDS its
    # best-fit skill on top. Both reach the planning guidance, and skills_loaded lists both — emitted
    # BEFORE the plan (pi_agenda) so the user sees the active paths while reviewing.
    from bioagent.agents.presets import ResearchPreset

    pinned = ResearchPreset("variant_annotation", "Variant annotation", "PINNED GUIDANCE")
    auto_lib = (ResearchPreset("celltype_annotation", "Cell-type annotation", "AUTO GUIDANCE"),)
    seen, calls = {}, {"router": 0}

    def complete(messages):
        sys = messages[0]["content"]
        if "research-protocol router" in sys:
            calls["router"] += 1
            return "celltype_annotation"           # the PI's auto pick, ON TOP of the pinned one
        if "Principal Investigator of a bioinformatics lab" in sys:
            seen["pi_user"] = messages[1]["content"]
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL REPORT"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(pinned_skills=(pinned,), skill_library=auto_lib),
                      complete_fn=complete, scientist=scientist)
    events = []
    lab.run("anything", on_event=events.append)

    assert calls["router"] == 1                     # auto STILL runs alongside a pinned skill
    assert "PINNED GUIDANCE" in seen["pi_user"] and "AUTO GUIDANCE" in seen["pi_user"]
    types = [e.get("type") for e in events]
    sl = [e for e in events if e.get("type") == "skills_loaded"]
    assert len(sl) == 1
    assert {s["key"] for s in sl[0]["skills"]} == {"variant_annotation", "celltype_annotation"}
    assert types.index("skills_loaded") < types.index("pi_agenda")   # shown before the plan


def test_skill_selection_sees_the_dataset_profile():
    # Q1 — the skill router now gets the DATASET profile, not only the question, so a vague ask still
    # routes on what the data actually is.
    from bioagent.agents.presets import ResearchPreset

    lib = (ResearchPreset("variant_annotation", "Annotate a VCF's variants", "VG"),)
    seen = {}

    def complete(messages):
        sys = messages[0]["content"]
        if "research-protocol router" in sys:
            seen["router_user"] = messages[1]["content"]
            return "none"
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx_with_dataset({"dataset_kind": "vcf_variants"}),
                      LabConfig(skill_library=lib), complete_fn=complete, scientist=scientist)
    lab.run("complete the research")                # vague question — the dataset must carry the signal
    assert "vcf_variants" in seen["router_user"]


def test_no_skill_selected_means_free_planning():
    from bioagent.agents.presets import ResearchPreset

    lib = (ResearchPreset("celltype_annotation", "Cell-type annotation", "GUIDANCE"),)
    seen = {}

    def complete(messages):
        sys = messages[0]["content"]
        if "research-protocol router" in sys:
            return "none"                              # PI judges no protocol fits
        if "Principal Investigator of a bioinformatics lab" in sys:
            seen["pi_user"] = messages[1]["content"]
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL REPORT"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(skill_library=lib), complete_fn=complete, scientist=scientist)
    lab.run("something unrelated to any known protocol")

    assert "research-path guidance" not in seen["pi_user"]


def test_pipeline_loader_reads_tools_and_atomic_skill_library():
    from bioagent.agents.presets import PRESETS, get_preset
    from bioagent.agents.skills import SKILLS

    p = get_preset("scgpt_annotation")
    assert "scgpt_annotate" in p.tools                      # tools: frontmatter parsed
    # the CodeAct templates that used to be bundled per-pipeline now live in the shared atomic library
    # (each a folder skills/<name>/ with a SKILL.md + reference.py bundle)
    assert "crossvalidate_scgpt_vs_leiden" in SKILLS and SKILLS["crossvalidate_scgpt_vs_leiden"].files
    assert "differential_expression" in PRESETS and "gene_signature_scoring" in PRESETS
    assert "pairwise_de" in SKILLS                           # differential_expression's templates migrated


def test_atomic_skills_reach_the_scientist_by_manifest(monkeypatch):
    # The atomic-skill library is surfaced to the Scientist's brief by MANIFEST (name + summary)
    # via progressive disclosure — the full body is withheld until fetched with read_skill_reference.
    import bioagent.agents.research_lab as rl
    from bioagent.agents import skills as skills_mod

    demo = {"demo_skill": skills_mod.Skill("demo_skill", summary="does a demo thing",
                                           doc="how to use the demo",
                                           files={"reference.py": "BODY_MARKER_CODE"})}
    monkeypatch.setattr(skills_mod, "SKILLS", demo)     # skill_manifest() reads the global SKILLS
    monkeypatch.setattr(rl, "ATOMIC_SKILLS", demo)      # ...and the brief sizes the library from this
    captured = {}

    def recording_chat(messages, tools):
        captured.setdefault("brief", "\n".join(str(m.get("content") or "") for m in messages))
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"id": "f1", "type": "function",
                    "function": {"name": "finish", "arguments": json.dumps({"answer": "done"})}}]}
        return {"content": "", "tool_calls": [{"id": "t1", "type": "function",
                "function": {"name": "run_qc", "arguments": "{}"}}]}

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=recording_chat)
    lab = ResearchLab(_ctx(), LabConfig(auto_select_skill=False), complete_fn=complete, scientist=scientist)
    lab.run("do the analysis")

    assert "demo_skill" in captured["brief"]             # name advertised in the manifest
    assert "does a demo thing" in captured["brief"]       # one-line summary advertised
    assert "how to use the demo" not in captured["brief"]  # guidance withheld (fetched on demand)
    assert "BODY_MARKER_CODE" not in captured["brief"]    # code withheld (fetched one level deeper)


def test_read_skill_reference_fetches_guidance_then_code_on_demand():
    from bioagent.agents.skills import Skill, make_skill_reference_tool

    lib: dict = {}
    tool = make_skill_reference_tool(lambda: lib)   # getter read at call time

    # Empty library -> graceful error, never a crash.
    assert "error" in tool.executor({"name": "demo"}, _ctx())

    lib["demo"] = Skill("demo", summary="one-line summary", doc="WHEN AND HOW TO USE",
                        files={"reference.py": "BODY_CODE"})

    # Level 2: name only -> guidance + file list, NOT the code.
    guide = tool.executor({"name": "demo"}, _ctx())
    assert guide["doc"] == "WHEN AND HOW TO USE" and guide["summary"] == "one-line summary"
    assert guide["files"] == ["reference.py"] and "code" not in guide
    assert "reference.py" in guide["next"]

    # Level 3: name + file -> that file's code.
    code = tool.executor({"name": "demo", "file": "reference.py"}, _ctx())
    assert code["code"] == "BODY_CODE" and code["file"] == "reference.py"

    # Legacy ".py" name still resolves (tolerant lookup); unknown file / skill -> clear error.
    assert tool.executor({"name": "demo.py"}, _ctx())["summary"] == "one-line summary"
    assert "error" in tool.executor({"name": "demo", "file": "nope.py"}, _ctx())
    miss = tool.executor({"name": "nope"}, _ctx())
    assert "error" in miss and "demo" in miss["available"]


def test_required_skills_inject_a_mandatory_directive():
    # The console's skill multi-select: checked atomic skills are REQUIRED — the directive is added
    # to the PI's planning guidance (validated against the real library; a real name is used here).
    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    # A legacy ".py"-suffixed required name still resolves (tolerant lookup); the directive names the
    # canonical skill (no ".py") and points at the two-level read_skill_reference fetch.
    lab = ResearchLab(_ctx(),
                      LabConfig(auto_select_skill=False, required_skills=("perturbation_edistance.py",)),
                      complete_fn=complete, scientist=scientist)
    lab.run("do the analysis")
    assert "REQUIRED skills" in (lab.guidance or "")
    assert "perturbation_edistance" in (lab.guidance or "")


def test_search_skills_ranks_by_relevance_and_reports_no_match():
    from bioagent.agents.skills import Skill, search_skills, make_search_skills_tool

    lib = {
        "edist": Skill("edist", summary="rank perturbations by E-distance to control"),
        "markers": Skill("markers", summary="assign a cell-type label to each cluster from markers"),
        "vep": Skill("vep", summary="annotate variants with VEP consequence and ClinVar"),
    }
    hits = search_skills("which perturbation guides moved cells vs control", k=2, skills=lib)
    assert hits and hits[0].name == "edist"        # most relevant first

    tool = make_search_skills_tool(lambda: lib)
    res = tool.executor({"query": "annotate cell types from marker genes"}, _ctx())
    assert res["results"] and res["results"][0]["name"] == "markers"
    miss = tool.executor({"query": "zzzzz nonexistent qqqq"}, _ctx())
    assert miss["results"] == [] and "hint" in miss and miss["available_count"] == 3


def test_brief_switches_to_search_when_library_is_large(monkeypatch):
    # Small library -> inline manifest (tested elsewhere). Large library (> threshold) -> the brief
    # tells the agent to search_skills instead of listing all, so the manifest can't bloat context.
    import bioagent.agents.research_lab as rl
    from bioagent.agents.skills import Skill

    big = {f"skill_{i}": Skill(f"skill_{i}", summary=f"does thing {i}") for i in range(20)}
    monkeypatch.setattr(rl, "ATOMIC_SKILLS", big)
    monkeypatch.setattr(rl, "SKILL_MANIFEST_MAX", 12)
    captured = {}

    def recording_chat(messages, tools):
        captured.setdefault("brief", "\n".join(str(m.get("content") or "") for m in messages))
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"id": "f1", "type": "function",
                    "function": {"name": "finish", "arguments": json.dumps({"answer": "done"})}}]}
        return {"content": "", "tool_calls": [{"id": "t1", "type": "function",
                "function": {"name": "run_qc", "arguments": "{}"}}]}

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "FINAL"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=recording_chat)
    lab = ResearchLab(_ctx(), LabConfig(auto_select_skill=False), complete_fn=complete, scientist=scientist)
    lab.run("do the analysis")
    assert "search_skills" in captured["brief"]           # large library -> search instruction
    assert "20 atomic skills are available" in captured["brief"]
    assert "skill_0" not in captured["brief"]             # the full manifest is NOT dumped


def test_unknown_required_skill_is_dropped_not_injected():
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(),
                      LabConfig(auto_select_skill=False, required_skills=("does_not_exist.py",)),
                      complete_fn=lambda m: (json.dumps(["Run QC"])
                                             if "Principal Investigator" in m[0]["content"]
                                             else json.dumps({"verdict": "accept", "score": 0.9})
                                             if "Critic" in m[0]["content"] else "FINAL"),
                      scientist=scientist)
    lab.run("do the analysis")
    assert "REQUIRED skills" not in (lab.guidance or "")


# --- Axis A: Virtual-Lab team mode (dynamic team + multi-agent meetings) ------

def test_team_mode_runs_virtual_lab_meetings():
    seen = {}

    def complete(messages):
        sys = messages[0]["content"]
        if "assembling a small expert team" in sys:
            return json.dumps([
                {"title": "Single-cell biologist", "expertise": "scRNA-seq", "goal": "biology"},
                {"title": "Statistician", "expertise": "statistics", "goal": "rigor"},
            ])
        if "expert team member" in sys:
            return "my expert input"
        if "Scientific Critic in a meeting" in sys:
            return "meeting critique"
        if "synthesizing a team meeting" in sys:
            return "MEETING SYNTHESIS"
        if "Principal Investigator of a bioinformatics lab" in sys:
            seen["pi_user"] = messages[1]["content"]
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        if "writing the final research report" in sys:
            seen["synth_user"] = messages[1]["content"]
            return "FINAL TEAM REPORT"
        return "x"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(mode="team", auto_select_skill=False),
                      complete_fn=complete, scientist=scientist)
    events = []
    result = lab.run("Characterize cell states in my dataset", on_event=events.append)

    types = [e.get("type") for e in events]
    assert "team_formed" in types
    # both meetings ran: design (before planning) and interpretation (after execution)
    kinds = {e.get("kind") for e in events if e.get("type") == "team_meeting_start"}
    assert kinds == {"design", "interpretation"}
    # each expert contributed from its OWN context (independent perspectives)
    assert any(e.get("type") == "expert_contribution" and e.get("member") == "Single-cell biologist"
               for e in events)
    # the design synthesis steered the PI's plan; the interpretation reached the report
    assert "Team design-meeting synthesis" in seen["pi_user"]
    assert "Team interpretation of the results" in seen["synth_user"]
    assert result.final_answer == "FINAL TEAM REPORT"


def test_team_meeting_multi_round_collaborates_and_is_score_driven():
    saw_collab, saw_challenge = [], []

    def complete(messages):
        sys = messages[0]["content"]
        if "assembling a small expert team" in sys:
            return json.dumps([{"title": "Bio", "expertise": "x", "goal": "y"}])
        if "expert team member" in sys:
            body = messages[1]["content"]
            saw_collab.append("shared synthesis" in body)
            saw_challenge.append("push back HARD" in body)   # low-score feedback band
            return "input"
        if "Scientific Critic in a meeting" in sys:
            return json.dumps({"score": 0.3, "critique": "weak evidence"})   # low -> no early stop
        if "synthesizing a team meeting" in sys:
            return "SYNTH"
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        if "writing the final research report" in sys:
            return "REPORT"
        return "x"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(mode="team", meeting_rounds=2, auto_select_skill=False),
                      complete_fn=complete, scientist=scientist)
    events = []
    lab.run("characterize cell states", on_event=events.append)

    design = [e for e in events if e.get("type") == "expert_contribution" and e.get("kind") == "design"]
    assert {e["round"] for e in design} == {1, 2}             # two deliberation rounds ran (low score)
    assert saw_collab[0] is False and any(saw_collab)         # later rounds build on the shared synthesis
    assert any(saw_challenge)                                 # low Critic score -> "push back" feedback


def test_team_mode_default_is_two_rounds():
    def complete(messages):
        sys = messages[0]["content"]
        if "assembling a small expert team" in sys:
            return json.dumps([{"title": "Bio", "expertise": "x", "goal": "y"}])
        if "expert team member" in sys:
            return "input"
        if "Scientific Critic in a meeting" in sys:
            return json.dumps({"score": 0.4, "critique": "more needed"})   # below accept -> use both rounds
        if "synthesizing a team meeting" in sys:
            return "SYNTH"
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        if "writing the final research report" in sys:
            return "REPORT"
        return "x"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(mode="team", auto_select_skill=False),   # meeting_rounds defaults to 2
                      complete_fn=complete, scientist=scientist)
    events = []
    lab.run("characterize cell states", on_event=events.append)
    design = {e["round"] for e in events if e.get("type") == "expert_contribution" and e.get("kind") == "design"}
    assert design == {1, 2}


def test_high_meeting_score_ends_meeting_early():
    def complete(messages):
        sys = messages[0]["content"]
        if "assembling a small expert team" in sys:
            return json.dumps([{"title": "Bio", "expertise": "x", "goal": "y"}])
        if "expert team member" in sys:
            return "input"
        if "Scientific Critic in a meeting" in sys:
            return json.dumps({"score": 0.95, "critique": "solid"})   # high -> converge after round 1
        if "synthesizing a team meeting" in sys:
            return "SYNTH"
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        if "writing the final research report" in sys:
            return "REPORT"
        return "x"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(mode="team", meeting_rounds=3, auto_select_skill=False),
                      complete_fn=complete, scientist=scientist)
    events = []
    lab.run("characterize cell states", on_event=events.append)
    design = {e["round"] for e in events if e.get("type") == "expert_contribution" and e.get("kind") == "design"}
    assert design == {1}                                      # converged -> stopped after round 1
    assert any(e.get("type") == "meeting_converged" and e.get("kind") == "design" for e in events)


def test_auto_mode_lets_pi_route_single_vs_team():
    def complete(messages):
        sys = messages[0]["content"]
        if "deciding HOW to run" in sys:
            return "single"                       # PI routes a routine task to one scientist
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(["Run QC"])
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9})
        return "REPORT"

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(mode="auto", auto_select_skill=False),
                      complete_fn=complete, scientist=scientist)
    events = []
    lab.run("run scgpt on my file", on_event=events.append)

    assert any(e.get("type") == "mode_selected" and e.get("mode") == "single" for e in events)
    assert not any(e.get("type") == "team_formed" for e in events)   # single -> no team


# --- revise then accept: Critic rejects round 1, accepts round 2 -------------

def test_lab_revises_then_accepts():
    complete = _make_complete(
        agenda=["Run QC"],
        critic_responses=[
            {"verdict": "revise", "score": 0.4, "critique": "QC flags not interpreted — explain them"},
            {"verdict": "accept", "score": 0.95, "critique": "now grounded"},
        ],
    )
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)

    result = lab.run("QC the dataset")

    assert len(result.rounds) == 2
    assert result.rounds[0].verdict.verdict == "revise"
    assert result.rounds[1].verdict.verdict == "accept"
    assert result.accepted_steps == 1 and result.converged is True


# --- mid-run stop: cooperative cancel between steps + tool turns -------------

def test_cancel_before_any_step_runs_nothing_and_reports_partial():
    complete = _make_complete(agenda=["Run QC", "Cluster"],
                              critic_responses=[{"verdict": "accept", "score": 0.9}])
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)

    events = []
    result = lab.run("Annotate", on_event=events.append, should_cancel=lambda: True)

    assert result.rounds == [] and result.accepted_steps == 0 and result.converged is False
    assert "stopped by the user" in result.final_answer.lower()
    assert any(e["type"] == "run_cancelled" for e in events)


def test_cancel_after_step_one_keeps_partial_results():
    # Cancel flips true once step 1 has been judged (its "critic" event fires), so the
    # lab completes step 1 then stops before step 2 — preserving step 1's result.
    flag = {"stop": False}

    def on_event(e):
        if e.get("type") == "critic":
            flag["stop"] = True

    complete = _make_complete(agenda=["Run QC", "Identify markers"],
                              critic_responses=[{"verdict": "accept", "score": 0.9, "critique": "ok"}])
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=_scientist_calls("run_qc", {}))
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)

    result = lab.run("Annotate", on_event=on_event, should_cancel=lambda: flag["stop"])

    assert len(result.rounds) == 1 and result.accepted_steps == 1     # step 1 ran, step 2 did not
    assert result.converged is False
    assert "1/2 planned steps completed" in result.final_answer
    assert "Run QC" in result.final_answer                            # the partial is preserved


def test_harness_cancel_stops_before_calling_the_model():
    from bioagent.agents.research_harness import HarnessContext, ResearchHarness, default_catalog

    calls = {"n": 0}

    def chat(messages, tools):
        calls["n"] += 1
        return {"content": "", "tool_calls": []}

    h = ResearchHarness(catalog=default_catalog(), chat_fn=chat)
    res = h.run("brief", HarnessContext(decisions={}, tunnel_port=1, model="m"), should_cancel=lambda: True)
    assert res.stop_reason == "cancelled" and calls["n"] == 0          # never even called the model


# --- Critic guard: a failed Scientist run can't be rubber-stamped ------------

def test_critic_guard_downgrades_accept_when_scientist_errored():
    # Scientist keeps calling an unknown tool -> validation errors, no final answer.
    def boom_chat(messages, tools):
        return {"content": "", "tool_calls": [{"function": {"name": "boom", "arguments": "{}"}}]}

    # Even though the model-critic says "accept", the deterministic guard forces revise.
    complete = _make_complete(
        agenda=["Do the thing"],
        critic_responses=[{"verdict": "accept", "score": 1.0, "critique": "looks fine"}],
    )
    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=boom_chat)
    lab = ResearchLab(_ctx(), LabConfig(max_rounds=4, max_revisions=1), complete_fn=complete, scientist=scientist)

    result = lab.run("anything")

    # the Scientist run failed (no answer + errors), so NO round may be accepted
    assert result.accepted_steps == 0
    assert result.converged is False
    assert all(r.verdict.verdict == "revise" for r in result.rounds)
    assert any("auto-guard" in r.verdict.critique for r in result.rounds)
    # the failure was real (unknown-tool errors recorded), never faked as success
    assert result.rounds[0].scientist_result["status"] == "incomplete"
    assert result.rounds[0].scientist_result["errors"]


def test_step_with_artifact_but_no_final_answer_is_accepted():
    # The scgpt_annotate case: a tool produced a real result, but the harness loop ended
    # 'incomplete' with NO textual final_answer (the model never called finish). The
    # artifact-aware guard must let the Critic's "accept" STAND — the artifact is the
    # result, not the prose. (The old guard buried this as a failure.)
    from bioagent.agents.research_harness import HarnessConfig

    # Always call a real tool, never `finish` -> the harness runs out of max_steps and
    # ends 'incomplete' with no final_answer (exactly the scgpt_annotate trace: tool kept
    # producing output but the loop never converged on a finish).
    def chat(messages, tools):
        return {"content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "run_qc", "arguments": "{}"}}]}

    complete = _make_complete(
        agenda=["Run QC"],
        critic_responses=[{"verdict": "accept", "score": 0.9, "critique": "artifact produced"}],
    )
    scientist = ResearchHarness(
        catalog=default_catalog(), config=HarnessConfig(max_steps=2), chat_fn=chat)
    lab = ResearchLab(_ctx(), LabConfig(max_revisions=1), complete_fn=complete, scientist=scientist)

    result = lab.run("QC the dataset")

    r0 = result.rounds[0]
    # status is NOT "ok" (the loop never finished) — the OLD guard force-revised on this
    # alone; the new guard looks at the produced artifact instead.
    assert r0.scientist_result["status"] != "ok"
    assert any(s["tool"] == "run_qc" and s["ok"] for s in r0.scientist_result["steps"])  # a real result exists
    assert r0.verdict.verdict == "accept"                       # accept STANDS (not auto-revised)
    assert "auto-guard" not in r0.verdict.critique
    assert result.accepted_steps == 1


# --- hybrid: the Scientist uses the run_code (CodeAct) tool ------------------

def test_lab_scientist_uses_run_code_codeact_tool():
    ran = {}

    def code_executor(code):
        ran["code"] = code
        return {"status": "ok", "stdout": "42"}

    complete = _make_complete(
        agenda=["Compute something custom with code"],
        critic_responses=[{"verdict": "accept", "score": 0.9, "critique": "ok"}],
    )
    # hybrid catalog = curated function tools + the CodeAct run_code tool
    scientist = ResearchHarness(
        catalog=[*default_catalog(), make_run_code_tool(code_executor)],
        chat_fn=_scientist_calls("run_code", {"code": "print(6*7)"}),
    )
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)

    result = lab.run("Do a custom computation")

    # CodeAct executor was invoked with the model's snippet (a deterministic seed preamble is
    # prepended for reproducibility — see agents/provenance.py — so the snippet is the suffix).
    assert ran["code"].endswith("print(6*7)")
    steps = result.rounds[0].scientist_result["steps"]
    code_step = next(s for s in steps if s["tool"] == "run_code")
    assert code_step["ok"] is True and code_step["summary"] == "ok"
    assert result.converged is True


# --- run_code without an executor is safely 'not enabled' (no in-proc exec) --

def test_run_code_without_executor_is_not_enabled():
    tool = make_run_code_tool(None)
    out = tool.executor({"code": "import os; os.system('rm -rf /')"}, _ctx())
    assert out["status"] == "not_enabled"


# --- end-to-end with the REAL sandbox: lab -> Scientist -> run_code -> subprocess

def test_lab_run_code_executes_in_real_sandbox():
    from bioagent.agents.research_harness import ResearchHarness, default_catalog
    from bioagent.agents.sandbox import CodeSandbox

    complete = _make_complete(
        agenda=["Compute the answer with code"],
        critic_responses=[{"verdict": "accept", "score": 0.9, "critique": "ok"}],
    )
    scientist = ResearchHarness(
        catalog=[*default_catalog(), make_run_code_tool(CodeSandbox(timeout_s=10))],
        chat_fn=_scientist_calls("run_code", {"code": "print(6 * 7)"}),
    )
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)

    result = lab.run("custom computation")

    code_step = next(s for s in result.rounds[0].scientist_result["steps"] if s["tool"] == "run_code")
    assert code_step["ok"] is True
    assert result.converged is True


# --- mid-run prompt injection: user notes fold into the remaining steps -------

def test_lab_folds_midrun_injection_into_remaining_steps():
    """A note the user injects while the run executes reaches the Scientist's brief and
    keeps applying to later steps (standing guidance), and emits a ``user_injection``."""
    complete = _make_complete(
        agenda=["Step one", "Step two"],
        critic_responses=[{"verdict": "accept", "score": 0.9, "critique": "ok"}],
    )
    seen_briefs = []

    def chat(messages, tools):
        # The brief is the first user message; capture it so we can assert the note landed.
        seen_briefs.append(" ".join(m.get("content", "") for m in messages if m.get("role") == "user"))
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [
                {"id": "f1", "type": "function",
                 "function": {"name": "finish", "arguments": json.dumps({"answer": "done"})}}]}
        return {"content": "", "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "run_qc", "arguments": "{}"}}]}

    scientist = ResearchHarness(catalog=default_catalog(), chat_fn=chat)
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)

    # Inject exactly once (before the first step); it must persist to the second step too.
    box = {"pulled": False}

    def pull():
        if not box["pulled"]:
            box["pulled"] = True
            return ["focus on T cells only"]
        return []

    events = []
    result = lab.run("Q", on_event=events.append, pull_injections=pull)

    assert result.accepted_steps == 2
    assert any(e.get("type") == "user_injection" for e in events)
    # The note appears in BOTH steps' briefs (standing guidance), pulled only once.
    step_briefs = [b for b in seen_briefs if "Step one" in b or "Step two" in b]
    assert any("focus on T cells only" in b and "Step one" in b for b in step_briefs)
    assert any("focus on T cells only" in b and "Step two" in b for b in step_briefs)


# --- single-shot context budgeting (PI / Critic / synthesize _complete) -------


def _lab() -> ResearchLab:
    # complete_fn injected so construction needs no real catalog/GPU; we call the
    # private budgeter directly (it runs only on the real, non-injected path).
    return ResearchLab(_ctx(), LabConfig(), complete_fn=lambda _m: "", scientist=ResearchHarness())


def test_budget_single_shot_reserves_reply_room_without_truncating_small_prompt() -> None:
    lab = _lab()
    messages = [
        {"role": "system", "content": "synthesize"},
        {"role": "user", "content": "short payload"},
    ]
    out, max_tokens = lab._budget_single_shot(messages)

    assert out is messages                                   # untouched
    # Small prompt → reply may use most of the window, and never below the reserve.
    assert max_tokens >= lab.config.reply_reserve_tokens
    assert max_tokens < lab.config.scientist.max_model_len


def test_budget_single_shot_truncates_oversized_payload_and_guarantees_reply() -> None:
    lab = _lab()
    hc = lab.config.scientist
    huge = "X" * (hc.max_model_len * 4)                      # well over the window in chars
    messages = [
        {"role": "system", "content": "rigorous scientific Critic"},
        {"role": "user", "content": huge},
    ]
    out, max_tokens = lab._budget_single_shot(messages)

    assert max_tokens == lab.config.reply_reserve_tokens     # reply room guaranteed
    assert out is not messages and len(out[1]["content"]) < len(huge)   # payload trimmed
    assert out[1]["content"].endswith("…[truncated to fit the model context window]")
    # The trimmed prompt now leaves room for the reserved reply.
    prompt_tokens = sum(_msg_tokens(m) for m in out)
    assert prompt_tokens + max_tokens < hc.max_model_len


def test_budget_single_shot_tightens_with_exact_server_count() -> None:
    # The char estimate says the prompt fits, but the SERVER tokenizer (via the Scientist
    # harness's /tokenize counter) reports it's over — the single-shot budgeter must
    # truncate until the EXACT count fits, not trust the estimate.
    def fake_count(messages, tools):
        # "true" count is 2x the char estimate → an estimate-passing prompt is really over.
        return sum(len(m.get("content") or "") for m in messages) // 2

    scientist = ResearchHarness(count_tokens_fn=fake_count)
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=lambda _m: "", scientist=scientist)
    hc = lab.config.scientist
    min_reply = min(lab.config.reply_reserve_tokens, max(256, hc.max_model_len // 2))
    input_cap = hc.max_model_len - min_reply - hc.context_safety_margin

    # Sized so the char estimate (chars/2.6) is under the cap but fake_count (chars/2) is over.
    payload = "Z" * int(input_cap * 2.4)
    out, _max_tokens = lab._budget_single_shot([
        {"role": "system", "content": "critic"},
        {"role": "user", "content": payload},
    ])
    assert fake_count(out, []) <= input_cap                  # exact count now within the real window
    assert out[1]["content"].endswith("…[truncated to fit the model context window]")


def test_injected_complete_fn_receives_the_budgeted_prompt() -> None:
    # Regression: production injects a complete_fn (gateway _lab_llm). The budgeting MUST run
    # before dispatch, or it is dead code on the only path production takes — which is exactly
    # how the 32K overflow survived the first fix. The injected fn must see a TRIMMED prompt.
    seen: dict = {}

    def recording_complete(messages):
        seen["messages"] = messages
        return "ok"

    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=recording_complete, scientist=ResearchHarness())
    hc = lab.config.scientist
    huge = "Y" * (hc.max_model_len * 4)
    lab._complete([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": huge},
    ])

    got = seen["messages"]
    assert len(got[1]["content"]) < len(huge)                # the injected fn got the trimmed prompt
    # And the trimmed prompt fits the window with output room to spare (no 0-token budget).
    assert sum(_msg_tokens(m) for m in got) + lab.config.reply_reserve_tokens < hc.max_model_len


# --- Critic evidence pointers (branch: feat/critic-evidence-pointers) ---------
# The Critic payload binds each tool result to the concrete on-disk artifact paths it
# wrote, so a verdict can ground on WHAT was produced, not only the scientist's prose.
# Extraction is deterministic (no LLM); surfaced for grounding only — no new guard here.

def test_evidence_pointers_extracts_paths_across_tool_shapes():
    from bioagent.agents.research_harness import evidence_pointers

    # scrna_pack-style: figures/tables lists of relative paths.
    assert evidence_pointers({
        "status": "ok",
        "figures": ["figures/umap_clusters.png"],
        "tables": ["tables/de_leiden_all.csv"],
    }) == ["figures/umap_clusters.png", "tables/de_leiden_all.csv"]

    # datasets/report-style: scalar *_path keys, nested under `result`.
    got = evidence_pointers({
        "result": {"result_path": "data/dataset_results.json"},
        "summary_path": "data/summary.md",
        "pdf_path": "report/run.pdf",
    })
    assert set(got) == {"data/dataset_results.json", "data/summary.md", "report/run.pdf"}

    # None values, "None" strings, and hidden scaffolding dotfiles are skipped.
    assert evidence_pointers({
        "pdf_path": None, "docx_path": "None",
        "header_path": "report/.run_tables.tex",    # skip-key
        "lua_path": "report/.run_tables.lua",        # skip-key
        "md_path": "report/.hidden.md",              # dotfile basename -> skipped
    }) == []

    # Order-preserving dedup + non-dict input is safe.
    assert evidence_pointers({"a": {"path": "x.png"}, "b": {"path": "x.png"}}) == ["x.png"]
    assert evidence_pointers("just a string") == []
    assert evidence_pointers(None) == []


def test_critic_payload_carries_evidence_for_each_tool_and_a_step_union():
    from bioagent.agents.research_harness import HarnessTool

    def _exec(args, ctx):
        return {"status": "ok", "figures": ["figures/umap.png"],
                "tables": ["tables/de.csv"], "result_path": "data/x.json"}

    tool = HarnessTool("make_art", "writes artifacts",
                       {"type": "object", "properties": {}}, _exec, category="analysis")
    finish = default_catalog()[-1]   # the shared `finish` control tool
    scientist = ResearchHarness(catalog=[tool, finish], chat_fn=_scientist_calls("make_art", {}))

    captured: dict = {}

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(["Make some artifacts"])
        if "rigorous scientific Critic" in sys:
            captured["critic_user"] = messages[1]["content"]
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "grounded in artifacts"})
        return "FINAL REPORT"

    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)
    result = lab.run("Produce artifacts")

    payload = json.loads(captured["critic_user"])
    # Per-tool evidence is attached to the make_art result...
    art_tool = next(t for t in payload["tool_results"] if t["tool"] == "make_art")
    assert set(art_tool["evidence"]) == {"figures/umap.png", "tables/de.csv", "data/x.json"}
    # ...and the step-level union carries the same set (finish contributes nothing).
    assert set(payload["evidence"]) == {"figures/umap.png", "tables/de.csv", "data/x.json"}
    assert result.accepted_steps == 1


def test_critic_system_prompt_instructs_grounding_in_evidence():
    from bioagent.agents.research_lab import _CRITIC_SYSTEM
    assert "evidence" in _CRITIC_SYSTEM
    # Routing marker used by _make_complete must stay intact.
    assert "rigorous scientific Critic" in _CRITIC_SYSTEM


# --- downstream verification: read-only re-grounding on prior evidence --------
# A step surfaces each accepted upstream finding WITH the artifact paths that back it, framed
# as a CLAIM TO VERIFY (read the cited artifact) — NOT ground truth. Self-repair is NOT
# enabled: the brief forbids modifying upstream artifacts, so no tampering with intermediates.

def test_accepted_findings_block_frames_evidence_for_readonly_verification():
    from bioagent.agents.research_lab import CriticVerdict, LabRound

    lab = ResearchLab(_ctx(), LabConfig(), scientist=ResearchHarness())
    accepted = LabRound(
        1, 1, "Run DE", "spec",
        {"final_answer": "found markers X and Y",
         "steps": [{"tool": "make_art", "ok": True,
                    "result": {"tables": ["tables/de.csv"], "result_path": "data/x.json"}}]},
        CriticVerdict("accept", 0.9, ""))
    block = lab._accepted_findings_block([accepted])
    assert "found markers X and Y" in block
    assert "tables/de.csv" in block and "data/x.json" in block   # evidence pointers surfaced
    assert "VERIFY" in block                                     # framed as claim-to-verify
    assert "read-only" in block.lower()                          # read-only framing
    assert "never modify" in block.lower()                       # no tampering / no self-repair

    # A not-yet-accepted round contributes nothing (only accepted findings propagate).
    revised = LabRound(1, 1, "x", "s", {"final_answer": "a", "steps": []},
                       CriticVerdict("revise", 0.2, ""))
    assert lab._accepted_findings_block([revised]) == ""


def test_downstream_step_brief_carries_prior_evidence_and_verify_framing():
    from bioagent.agents.research_harness import HarnessTool

    def _exec(args, ctx):
        return {"status": "ok", "figures": ["figures/umap.png"], "result_path": "data/x.json"}

    tool = HarnessTool("make_art", "writes artifacts",
                       {"type": "object", "properties": {}}, _exec, category="analysis")
    finish = default_catalog()[-1]

    briefs: list[str] = []
    base = _scientist_calls("make_art", {})

    def recording_chat(messages, tools):
        briefs.append(messages[1]["content"])       # the harness user brief for this step
        return base(messages, tools)

    complete = _make_complete(
        agenda=["Produce artifacts", "Build on the prior finding"],
        critic_responses=[{"verdict": "accept", "score": 0.9, "critique": "ok"}])
    scientist = ResearchHarness(catalog=[tool, finish], chat_fn=recording_chat)
    lab = ResearchLab(_ctx(), LabConfig(), complete_fn=complete, scientist=scientist)
    result = lab.run("Two-step run")

    assert result.accepted_steps == 2
    # Step 2's brief re-grounds on step 1's evidence, read-only, as a claim to verify.
    last = briefs[-1]
    assert "figures/umap.png" in last and "data/x.json" in last
    assert "CLAIMS TO VERIFY" in last and "read-only" in last.lower()
    # Step 1's brief has no prior findings yet.
    assert "CLAIMS TO VERIFY" not in briefs[0]
