"""The request sets how much is planned; the data sets how it is done.

Until 2026-10-12 the PI was told to "plan EVERY analysis the question AND the data genuinely
warrant", a matched protocol was handed over as "follow this", the plan review was told that "a
narrowly worded question is not a reason to drop an analysis", and any protocol that mentions
literature forced a literature step. So "run QC and annotate" came back as the whole pipeline, the
user could not work a dataset one step at a time, and a limit had to be enforced by code afterwards
(agents/scope.py). Now a plan delivers what the request asks for plus the prerequisites those steps
cannot run without, and what it left out is offered as the next message.
"""
from __future__ import annotations

import json

from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness
from aiscientist.agents.research_lab import (
    LabConfig, MAX_LATER, ResearchLab, _PI_SYSTEM, _PLAN_REVIEW_CRITIC_SYSTEM,
    _PLAN_REVIEW_PI_SYSTEM, _is_literature_step, _parse_plan_extras, retick_tools,
)

_NAMES = ["run_cellqc", "run_scanpy_qc", "run_clustering", "run_de", "run_enrichment",
          "run_marker_annotation", "run_composition", "deep_literature", "literature_search",
          "run_code", "inspect_dataset"]
_QC = "**QC** — Quality-control the matrix with `run_scanpy_qc`."
_CLUSTER = "**Clustering** — Cluster the cells with `run_clustering`."
_ANNOTATE = "**Annotation** — Label the clusters with `run_marker_annotation`."
# A protocol that, like every real one, mentions the literature stage.
_PROTOCOL = "1. QC. 2. Cluster. 3. Markers. 4. Enrichment. 5. Ground the findings in the literature."


def _tool(name):
    return HarnessTool(name, f"{name} tool", {"type": "object", "properties": {}},
                       lambda _a, _c: {"status": "ok"}, category="analysis")


def _lab(complete, **config):
    return ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"), LabConfig(**config),
                       complete_fn=complete,
                       scientist=ResearchHarness(catalog=[_tool(n) for n in _NAMES]))


def _pi(reply: dict, seen: "dict | None" = None):
    def complete(messages):
        if "Principal Investigator of a bioinformatics lab" in messages[0]["content"]:
            if seen is not None:
                seen["pi_user"] = messages[1]["content"]
            return json.dumps(reply)
        return "{}"
    return complete


# --- what the planner is told ---------------------------------------------------------------------

def test_the_planner_is_told_to_deliver_the_request_not_everything_the_data_supports():
    assert "THE REQUEST SETS HOW MUCH IS PLANNED; THE DATA SETS HOW IT IS DONE" in _PI_SYSTEM
    assert "Do NOT add an analysis because the dataset could support it" in _PI_SYSTEM
    assert "PREREQUISITE" in _PI_SYSTEM                       # ...but what a requested step needs stays
    for gone in ("plan EVERY analysis the question AND the data genuinely warrant",
                 "completeness beats brevity", "AS MANY CONCRETE, ORDERED steps",
                 "up to ~20 is perfectly acceptable"):
        assert gone not in _PI_SYSTEM
    # A request for the whole analysis still gets the whole pipeline, uncompressed.
    assert "THE WHOLE ANALYSIS" in _PI_SYSTEM and "never compress or merge analyses" in _PI_SYSTEM
    # The data-driven additions are tied to the request's breadth.
    assert "When the request is for the whole analysis, or asks about the groups" in _PI_SYSTEM
    assert '"breadth"' in _PI_SYSTEM and '"later"' in _PI_SYSTEM


def test_the_protocol_is_a_reference_for_how_not_a_list_of_what():
    seen: dict = {}
    lab = _lab(_pi({"breadth": "specific", "agenda": [_QC]}, seen), preset_prompt=_PROTOCOL)
    lab._pi_plan("run QC", lambda _e: None)
    assert "Follow this research-path guidance" in seen["pi_user"]
    assert "plan all of its stages only for a whole-analysis request" in seen["pi_user"]
    assert "use it for HOW to do the stages you plan" in seen["pi_user"]


def test_the_plan_review_no_longer_measures_the_plan_against_the_dataset():
    assert "THE REQUEST SETS THE BREADTH, THE DATA DOES NOT" in _PLAN_REVIEW_CRITIC_SYSTEM
    assert "NARROWLY WORDED QUESTION IS NOT A REASON TO DROP" not in _PLAN_REVIEW_CRITIC_SYSTEM
    assert "tangential to a narrowly worded question, keep it" not in _PLAN_REVIEW_CRITIC_SYSTEM
    assert "never because the dataset would support one more analysis" in _PLAN_REVIEW_CRITIC_SYSTEM
    # A prerequisite is still protected, and the requested analysis must still be there.
    assert "PREREQUISITE" in _PLAN_REVIEW_CRITIC_SYSTEM
    assert "THE PRIMARY ANALYSIS IS PRESENT" in _PLAN_REVIEW_CRITIC_SYSTEM
    assert "adds no analysis the request did not ask for" in _PLAN_REVIEW_PI_SYSTEM


def test_the_plan_review_is_told_how_much_was_asked_for():
    payloads: list[dict] = []

    def complete(messages):
        system = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in system:
            return json.dumps({"breadth": "specific", "agenda": [_QC, _CLUSTER]})
        if "Scientific Critic reviewing a DRAFT analysis plan" in system:
            payloads.append(json.loads(messages[1]["content"]))
            return json.dumps({"issues": [], "revised_agenda": []})
        return "{}"

    lab = _lab(complete, prior_work="EARLIER IN THIS CONVERSATION — QC was done.")
    _, agenda = lab._pi_plan("cluster the cells", lambda _e: None)
    lab._plan_review("cluster the cells", agenda, lambda _e: None)
    assert payloads[0]["request_breadth"] == "specific"
    assert payloads[0]["earlier_work"].startswith("EARLIER IN THIS CONVERSATION")


# --- breadth and "later" ----------------------------------------------------------------------------

def test_breadth_and_later_are_optional_fields_of_the_plan_reply():
    reply = json.dumps({"breadth": "Specific", "agenda": ["a"],
                        "later": ["Find marker genes.", "find marker genes.", " ", "Run enrichment."]})
    assert _parse_plan_extras(reply) == ("specific", ["Find marker genes.", "Run enrichment."])
    assert _parse_plan_extras('{"agenda": ["a"]}') == ("", [])
    assert _parse_plan_extras('["a", "b"]') == ("", [])            # the bare-array form still parses
    assert _parse_plan_extras("not json") == ("", [])
    assert _parse_plan_extras(json.dumps({"breadth": "everything"}))[0] == ""
    many = json.dumps({"later": [f"analysis {i}" for i in range(9)]})
    assert len(_parse_plan_extras(many)[1]) == MAX_LATER


def test_what_was_left_out_is_offered_for_the_next_message_not_planned():
    events: list[dict] = []
    later = ["Find the marker genes of each cluster.", "Assign cell types to the clusters."]
    lab = _lab(_pi({"breadth": "specific", "agenda": [_QC, _CLUSTER], "later": later}))
    kind, agenda = lab._pi_plan("run QC and cluster the cells", events.append)
    assert kind == "agenda" and agenda == [_QC, _CLUSTER]
    assert lab.later == later and lab.request_breadth == "specific"
    assert {"type": "plan_later", "later": later, "breadth": "specific"} in events


# --- the literature step ------------------------------------------------------------------------------

def _plan_under_protocol(reply: dict, question: str, **config) -> list[str]:
    lab = _lab(_pi(reply), preset_prompt=_PROTOCOL, **config)
    return lab._pi_plan(question, lambda _e: None)[1]


def test_a_protocol_that_mentions_literature_no_longer_forces_it_on_a_specific_request():
    agenda = _plan_under_protocol({"breadth": "specific", "agenda": [_QC, _ANNOTATE]},
                                  "run QC and annotate the cell types")
    assert agenda == [_QC, _ANNOTATE]


def test_a_whole_analysis_gets_no_literature_step_from_the_protocol_either():
    """Literature is its own step, run on request (Yijun, 2026-10-12: "文献步骤不是在每一个研究
    pipeline 都必须的"). The protocol text names it in every pipeline; only the user's words add it."""
    for reply in ({"breadth": "whole", "agenda": [_QC, _CLUSTER]}, {"agenda": [_QC, _CLUSTER]}):
        assert _plan_under_protocol(reply, "analyse this dataset") == [_QC, _CLUSTER]
    # ... and a report is not a request for literature: every run ends with one.
    assert _plan_under_protocol({"agenda": [_QC]}, "Run CellQC on the uploaded sample to generate "
                                                   "quality control report.") == [_QC]


def test_naming_a_reference_to_analyse_against_is_not_asking_for_citations():
    from aiscientist.agents.research_lab import _requests_literature
    for q in ("Annotate the clusters with the retina reference.", "Align to the reference genome.",
              "Compare with the references atlas of mouse retina."):
        assert not _requests_literature(q), q
    for q in ("Annotate the cell types and give me references for the markers.",
              "Add citations for the top genes.", "Ground the result in the literature."):
        assert _requests_literature(q), q
    assert "LITERATURE IS A SEPARATE STEP, PLANNED ONLY WHEN THE USER ASKS FOR IT" in _PI_SYSTEM
    assert "A LITERATURE STEP IS NEVER A GAP" in _PLAN_REVIEW_CRITIC_SYSTEM


def test_the_users_own_words_still_ask_for_literature_on_a_specific_request():
    agenda = _plan_under_protocol({"breadth": "specific", "agenda": [_QC, _ANNOTATE]},
                                  "annotate the cell types and give me references for the markers")
    assert len(agenda) == 3 and _is_literature_step(agenda[-1])


def test_a_continuing_run_gets_no_literature_step_from_the_protocol():
    agenda = _plan_under_protocol({"agenda": [_ANNOTATE]}, "now annotate the cell types",
                                  prior_work="EARLIER IN THIS CONVERSATION — QC and clustering.")
    assert agenda == [_ANNOTATE]


# --- the design meeting --------------------------------------------------------------------------------

def test_the_design_meeting_is_asked_how_to_do_the_request_not_what_else_to_do():
    lab = _lab(_pi({"agenda": [_QC]}), mode="team", plan_review=False)
    topics: list[str] = []
    lab._form_team = lambda _q, _e: lab.config.specialists
    lab._team_meeting = lambda _q, topic, *_a, **_k: topics.append(topic) or "use pseudobulk"
    seen: dict = {}
    lab._complete = lambda messages, **_k: (seen.update(pi_user=messages[1]["content"])
                                            or json.dumps({"agenda": [_QC]}))
    lab.run("compare KO with WT", lambda _e: None, plan_review=lambda _k, _p: {"action": "cancel"})
    assert topics and "Stay inside the request" in topics[0]
    assert "it does not widen it" in seen["pi_user"] and "use pseudobulk" in seen["pi_user"]


# --- a reviewed plan still names its tools --------------------------------------------------------------

def test_a_rewritten_step_gets_its_tool_backticks_back():
    names = ["run_de", "run_depth_matched_de", "run_scanpy_qc", "deep_literature", "finish"]
    steps = ["**QC** — then run run_scanpy_qc, removing cells under 200 genes.",
             "**Depth** — Run run_depth_matched_de after `run_de`; finish with deep_literature.",
             "**Summary** — Describe what was found."]
    assert retick_tools(steps, names) == [
        "**QC** — then run `run_scanpy_qc`, removing cells under 200 genes.",
        "**Depth** — Run `run_depth_matched_de` after `run_de`; finish with `deep_literature`.",
        "**Summary** — Describe what was found."]
    assert retick_tools(steps, []) == steps


def test_the_plan_review_returns_steps_whose_tools_can_still_be_found():
    bare = ["**QC** — Check the counts, then run run_scanpy_qc at 200 genes.",
            "**Contrast** — Run run_de in contrast mode by sampleid."]

    def complete(messages):
        system = messages[0]["content"]
        if "Scientific Critic reviewing a DRAFT analysis plan" in system:
            return json.dumps({"issues": ["step 1 re-reads the profile"], "revised_agenda": bare})
        if "finalizing the analysis plan" in system:
            return json.dumps({"final_agenda": bare, "reason": "merged the pre-check into QC"})
        return "{}"

    draft = ["**Provenance** — Inspect the object with `run_code`.", _QC,
             "**Contrast** — Compare the arms with `run_de`."]
    final = _lab(complete)._plan_review("what changes?", draft, lambda _e: None)
    assert final == ["**QC** — Check the counts, then run `run_scanpy_qc` at 200 genes.",
                     "**Contrast** — Run `run_de` in contrast mode by sampleid."]
