"""A limit the user puts on a run ("only QC", "no DE, no enrichment", "a short report") is read once
and enforced by code (agents/scope.py).

Asked for on 2026-10-11 to test CellQC alone: nothing in planning could honour it — the PI plans
"every analysis the data warrant", a matched protocol lists the whole pipeline, the plan review
restores what looks missing, and a literature step is appended when the request says "report"."""
from __future__ import annotations

import json

import pytest

from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness
from aiscientist.agents.research_lab import LabConfig, ResearchLab, _primary_tool
from aiscientist.agents.scope import LITERATURE_TOOLS, RunScope, read_scope

_NAMES = ["run_cellqc", "run_scanpy_qc", "run_doublet_detection", "run_clustering", "run_de",
          "run_pseudobulk_de", "run_depth_matched_de", "run_enrichment", "run_gsea_prerank",
          "run_marker_annotation", "scgpt_annotate", "run_composition", "run_integration",
          "deep_literature", "search_corpus", "literature_search", "run_code", "inspect_dataset"]
_TOOLS = {n: f"{n} tool" for n in _NAMES}

_QC_ONLY_ZH = ("只做 QC：用 run_cellqc 对这个 Cell Ranger 文件夹做质控并输出结果矩阵，"
               "不做 run_de、enrichment、聚类和注释，报告尽量简短，不需要完整报告")


def test_an_explicit_limit_is_read_without_asking_the_model():
    def never(_m):
        raise AssertionError("explicit wording needs no model")

    s = read_scope(_QC_ONLY_ZH, _TOOLS, never)
    assert s.source == "rules" and s.brief_report
    assert "run_cellqc" in s.keep and "run_clustering" not in s.keep
    assert {"run_de", "run_enrichment", "run_gsea_prerank", "run_clustering",
            "run_marker_annotation"} <= set(s.exclude)

    s = read_scope("Only run run_cellqc on this delivery and give me the result dataset. "
                   "No DE, no enrichment, no literature. A brief report is enough.", _TOOLS, never)
    assert s.keep == ("run_cellqc",) and s.brief_report and not s.allows_literature()
    assert not s.allows("run_de") and s.allows("run_cellqc") and not s.allows("run_code")


@pytest.mark.parametrize("question", [
    "help me analyse this dataset and give me the result dataset and an report",
    "Which genes are expressed only in cluster 3?",
    "Find genes with no differential expression between KO and WT",
    "Are there cells without annotation? annotate them",
])
def test_a_question_about_biology_is_not_a_limit(question):
    assert not read_scope(question, _TOOLS).limited


@pytest.mark.parametrize("question", [
    "Do cellqc quality controll and give me a result.",
    "Do cellqc quality control and give me a result.",
    "Please run CellQC and return the result dataset.",
    "Run run_cellqc and show me its results!",
])
def test_a_named_method_and_its_result_are_limited_to_that_tool(question):
    def never(_messages):
        raise AssertionError("the whole request is read by the rules")

    scope = read_scope(question, _TOOLS, never)
    assert scope.keep == ("run_cellqc",) and scope.source == "rules"
    assert not scope.allows("run_code") and not scope.allows_literature()
    assert not read_scope(question, {"run_de": "DE"}, never).limited


@pytest.mark.parametrize("question", [
    "Do cellqc quality controll and give me a result. Then cluster the cells.",
    "Do cellqc quality controll and give me a result and compare KO with WT.",
    "Do cellqc quality controll and give me a result, then run differential expression.",
    "Run CellQC and analyse the data and give me a result.",
    "Run CellQC and give me a result and a written report.",
])
def test_asking_for_more_than_a_methods_result_does_not_limit_the_run(question):
    assert not read_scope(question, _TOOLS).limited


def test_an_exclusion_keeps_the_rest_and_keep_wins_over_exclude():
    s = read_scope("What changes between DDX41 and WT retina? Do not run enrichment.", _TOOLS)
    assert s.exclude == ("run_enrichment", "run_gsea_prerank") and not s.keep
    assert s.allows("run_de") and s.allows(None) and not s.allows("run_enrichment")
    # "only annotation" needs clusters; ruling annotation out does not rule clustering out.
    s = read_scope("只做 QC 和细胞注释，不做差异表达和富集", _TOOLS)
    assert {"run_cellqc", "run_clustering", "run_marker_annotation"} <= set(s.keep)
    assert "run_de" in s.exclude and "run_clustering" not in s.exclude


def test_a_reference_the_analysis_runs_against_is_not_literature():
    # "reference" here is mostly an analysis input: an atlas, a genome, a panel, a control group.
    s = read_scope("Only annotate the clusters with the retina reference.", _TOOLS)
    assert s.keep == ("run_clustering", "run_marker_annotation", "scgpt_annotate")
    assert not s.allows_literature()
    s = read_scope("Only align to the reference genome", _TOOLS)
    assert not LITERATURE_TOOLS & set(s.keep + s.exclude)
    # The plural, followed by what it is a reference OF, is still an input.
    assert read_scope("Only integrate with the references datasets", _TOOLS).keep == ("run_integration",)
    # ...and a reference named where something else is ruled out does not rule literature out.
    s = read_scope("What changes in the KO? Skip integration with the reference atlas.", _TOOLS)
    assert s.exclude == ("run_integration",) and s.allows_literature()


@pytest.mark.parametrize("question", [
    "Compare KO and WT per cell type. No references.",
    "Compare KO and WT per cell type, without citations",
    "Compare KO and WT, skip the literature search",
    "比较 KO 和 WT，不做文献检索",
    "比较 KO 和 WT，不要参考文献",
])
def test_ruling_literature_out_still_excludes_its_tools(question):
    s = read_scope(question, _TOOLS)
    assert set(s.exclude) == LITERATURE_TOOLS and not s.keep and not s.allows_literature()


def test_asking_for_literature_by_name_still_keeps_its_tools():
    s = read_scope("only QC and literature", _TOOLS)
    assert LITERATURE_TOOLS <= set(s.keep) and "run_cellqc" in s.keep and not s.allows("run_de")
    assert LITERATURE_TOOLS <= set(read_scope("Just run QC and add references", _TOOLS).keep)


def test_looser_wording_asks_the_model_and_checks_its_answer_against_the_catalog():
    asked = []

    def model(messages):
        asked.append(messages)
        return json.dumps({"keep": ["run_cellqc", "run_magic"], "exclude": [], "brief_report": True})

    s = read_scope("I don't care about anything beyond cleaning the matrix", _TOOLS, model)
    assert asked and s.keep == ("run_cellqc",) and s.brief_report and s.source == "model"
    # No limiting word at all: the model is not asked.
    asked.clear()
    assert not read_scope("Compare KO and WT per cell type", _TOOLS, model).limited and not asked
    # An unreadable or failing reply is "no limit", never an error.
    assert not read_scope("don't overthink it", _TOOLS, lambda _m: "sure!").limited
    assert not read_scope("don't overthink it", _TOOLS, lambda _m: 1 / 0).limited


def test_filter_never_empties_a_plan():
    plan = ["**QC** — run `run_cellqc` on the folder.", "**Clusters** — `run_clustering`.",
            "**Markers** — `run_de` per cluster.", "A step that names no tool."]
    kept, dropped = RunScope(keep=("run_cellqc",)).filter(plan, _primary_tool)
    assert kept == plan[:1] and len(dropped) == 3
    kept, dropped = RunScope(exclude=("run_de",)).filter(plan, _primary_tool)
    assert kept == [plan[0], plan[1], plan[3]] and dropped == [plan[2]]
    # Nothing in the plan matches the limit: return it whole rather than nothing.
    kept, dropped = RunScope(keep=("run_integration",)).filter(plan, _primary_tool)
    assert kept == plan and dropped == []


def test_scope_survives_a_round_trip_and_describes_itself():
    s = RunScope(("run_cellqc",), ("run_de",), True, "rules")
    assert RunScope.from_dict(s.to_dict()) == s
    assert s.describe() == "only run_cellqc; not run_de; brief report"
    assert "binding" in s.prompt_block() and "`run_cellqc`" in s.prompt_block()
    assert RunScope().prompt_block() == "" and not RunScope.from_dict(None).limited


# --- the lab enforces it ----------------------------------------------------------------------

def _tool(name):
    return HarnessTool(name, f"{name} tool", {"type": "object", "properties": {}},
                       lambda _a, _c: {"status": "ok"}, category="analysis")


_FULL_PLAN = ["**QC** — Quality-control the delivery with `run_cellqc`.",
              "**Clustering** — Cluster with `run_clustering`.",
              "**Markers** — Marker genes with `run_de`.",
              "**ORA** — Pathways with `run_enrichment`.",
              "**Annotation** — Label clusters with `run_marker_annotation`.",
              "**Literature** — Ground it with `deep_literature`."]


def _lab(complete, **config):
    catalog = [_tool(n) for n in _NAMES]
    return ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"), LabConfig(**config),
                       complete_fn=complete, scientist=ResearchHarness(catalog=catalog))


def test_a_qc_only_request_plans_qc_only_whatever_the_models_say():
    seen = {"pi_user": "", "systems": []}

    def complete(messages):
        system = messages[0]["content"]
        seen["systems"].append(system[:60])
        if "Principal Investigator of a bioinformatics lab" in system:
            seen["pi_user"] = messages[1]["content"]
            return json.dumps({"agenda": _FULL_PLAN})        # the PI ignores the limit entirely
        if "Scientific Critic reviewing a DRAFT analysis plan" in system:
            assert "user_scope" in messages[1]["content"]
            return json.dumps({"issues": ["no DE step"], "revised_agenda": _FULL_PLAN})
        if "finalizing the analysis plan" in system:
            return json.dumps({"final_agenda": _FULL_PLAN})  # ...and so does the review
        return "{}"

    events = []
    lab = _lab(complete, auto_select_skill=True, mode="auto", plan_review=True)
    plans = []

    def review(kind, payload):
        plans.append(list(payload))
        return {"action": "cancel"}

    lab.run(_QC_ONLY_ZH, events.append, plan_review=review)
    assert plans and plans[-1] == _FULL_PLAN[:1]
    assert "SCOPE SET BY THE USER" in seen["pi_user"]
    types = [e.get("type") for e in events]
    assert "scope" in types and "skill_selected" not in types      # no protocol was matched
    assert {"type": "mode_selected", "mode": "single", "reason": "scope"} in events
    assert any(e.get("type") == "steps_pruned" and e.get("reason") == "user_scope" for e in events)
    assert lab.scope.brief_report and lab.scope.keep


def test_an_unlimited_request_is_planned_as_before():
    def complete(messages):
        if "Principal Investigator of a bioinformatics lab" in messages[0]["content"]:
            return json.dumps({"agenda": _FULL_PLAN})
        return "{}"

    lab = _lab(complete)
    kind, agenda = lab._pi_plan("help me analyse this dataset", lambda _e: None)
    assert kind == "agenda" and agenda[:5] == _FULL_PLAN[:5]


_NOTHING_TO_DISCLOSE = ("none — every choice in this plan comes from the guidance and the dataset "
                        "profile.")


@pytest.mark.parametrize("written", [_NOTHING_TO_DISCLOSE, f"`{_NOTHING_TO_DISCLOSE}`",
                                     f"**none** — {_NOTHING_TO_DISCLOSE[7:]}", "None."])
def test_the_plans_nothing_to_disclose_line_is_not_a_step_left_out(written):
    """The #56 re-test's A2 ("Do cellqc quality controll and give me a result."): its feed read "Left
    out 1 step(s) your request ruled out: none". The PI had put the prompt's "none — every choice …"
    disclosure in `agenda`; it named no tool, so the limit dropped it as a step (RCHENLAB/AiScientist
    issue 75)."""
    def complete(messages):
        if "Principal Investigator of a bioinformatics lab" in messages[0]["content"]:
            return json.dumps({"agenda": [_FULL_PLAN[0], written]})
        return "{}"

    lab = _lab(complete)
    lab._scope = RunScope(keep=("run_cellqc",))
    events = []
    kind, agenda = lab._pi_plan("Do cellqc quality controll and give me a result.", events.append)
    assert kind == "agenda" and agenda == _FULL_PLAN[:1]
    assert not [e for e in events if e.get("type") == "steps_pruned"]
    assert lab.self_sourced.lower().startswith("none")
    # A step that only opens with the word is still a step.
    from aiscientist.agents.research_lab import _split_self_sourced
    step = "None of the libraries is filtered yet: run `run_cellqc` on each."
    assert _split_self_sourced([step]) == ([step], "")


def test_the_feed_names_each_step_the_limit_left_out_and_is_silent_on_none():
    from aiscientist.gateway.app import _lab_event_to_chat

    def feed(dropped):
        return [p["text"] for p in _lab_event_to_chat({"type": "steps_pruned", "reason": "user_scope",
                                                       "dropped": dropped})
                if p.get("type") == "lab_progress"]

    assert feed(_FULL_PLAN[1:3]) == ["✂️ Left out 2 step(s) your request ruled out: Clustering; "
                                     "Markers"]
    assert feed(["", "  ", "**  **"]) == [] and feed([]) == []


def test_a_limited_run_does_not_grow_new_steps():
    lab = _lab(lambda _m: "{}", hypothesis_driven=True)
    lab._scope = RunScope(keep=("run_cellqc",))
    assert lab._explore_after_step("q", "step", None, {}, [], [], lambda _e: None) == []


# A run that only did QC answers in the chat unless the request asks for a written report
# (Yijun, 2026-10-18). A short report is still a report; only one ruled out altogether is not.
@pytest.mark.parametrize("question, wanted", [
    ("Do only cellqc quality controll part and generate the report and result to me", True),
    ("Run CellQC on the uploaded sample to generate quality control report.", True),
    ("只做 QC：用 run_cellqc 对这个 Cell Ranger 文件夹做质控，输出结果矩阵。报告尽量简短，不需要完整报告。", True),
    ("只做质控并写一份书面报告", True),
    ("QC only and write a short PDF", True),
    ("Only run CellQC, no report needed", False),
    ("Run QC only, without a report", False),
    ("Only QC, I don't need a report", False),
    ("只做 QC，不需要报告", False),
    ("只做质控，不要写报告，结果矩阵给我", False),
    ("Just run QC and give me the matrix", False),
])
def test_a_written_report_is_read_from_the_request(question, wanted):
    from aiscientist.agents.scope import wants_written_report
    assert wants_written_report(question) is wanted
