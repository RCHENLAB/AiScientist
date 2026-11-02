"""A chat message changes a CellQC cutoff in the waiting plan (issue 61).

"Raise the mitochondrial limit to 20" sets ``max_pct_mt`` to 20 on the plan's ``run_cellqc`` step —
read by code, shown on the card as changed from its default, kept through a later revision, passed to
the tool as written, and reported in the QC reply. A request that cannot be used (out of range, no
value, no cutoff named) is explained back and the plan stays as it was.
"""

from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("yaml")

from aiscientist.agents.cellqc_cutoffs import cutoff_rows, read_cutoff_request  # noqa: E402
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness  # noqa: E402
from aiscientist.agents.research_lab import LabConfig, ResearchLab  # noqa: E402
from aiscientist.reporting.qc_summary import QC_SUMMARY_POLICY, cutoff_lines, qc_result_summary  # noqa: E402

QC = ("**CellQC** — Filter the barcodes with `run_cellqc`: drop barcodes with fewer than 500 counts, "
      "fewer than 300 genes, or more than 10% mitochondrial reads.")
CLUSTER = ("**Clustering & UMAP** — Group the cells with `run_clustering` at resolution 0.5 and "
           "lay them out on a 2-D map.")
SCHEMA = {"min_counts": {"type": "integer", "default": 500},
          "min_features": {"type": "integer", "default": 300},
          "max_pct_mt": {"type": "number", "default": 10.0}}


# --- reading a message ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text, changes", [
    ("raise the mitochondrial limit to 20", {"max_pct_mt": 20.0}),
    ("Increase the mito cutoff from 10 to 15%", {"max_pct_mt": 15.0}),
    ("use 25% mitochondrial", {"max_pct_mt": 25.0}),
    ("set max_pct_mt=12.5", {"max_pct_mt": 12.5}),
    ("set min_counts to 1000", {"min_counts": 1000}),
    ("lower the minimum genes per cell threshold to 200", {"min_features": 200}),
    ("set min_counts to 1000 and min_features to 250", {"min_counts": 1000, "min_features": 250}),
    ("set the mito cutoff to 20 then cluster at resolution 0.8", {"max_pct_mt": 20.0}),
])
def test_a_cutoff_and_its_number_are_read_from_the_sentence(text, changes):
    assert read_cutoff_request(text) == (changes, [])


@pytest.mark.parametrize("text", ["cluster at resolution 0.8", "drop the enrichment step",
                                  "use a stricter cutoff in the next step"])
def test_a_message_that_is_not_about_a_cutoff_is_left_alone(text):
    assert read_cutoff_request(text) == ({}, [])


@pytest.mark.parametrize("text, why", [
    ("set max_pct_mt to 150", "cannot be above 100"),
    ("set max_pct_mt to 0", "would remove every barcode"),
    ("set min_counts to 1000.5", "not a whole number"),
    ("set min_counts to 5000000", "must be between"),
    ("raise the mito limit", "not the value"),
    ("set mito to 5 and mito to 8", "two different values"),
    ("change the QC cutoff to 20 for these cells", "Which cutoff?"),
])
def test_a_request_that_cannot_be_used_is_explained(text, why):
    _changes, problems = read_cutoff_request(text)
    assert problems and why in " ".join(problems)


def test_the_card_rows_give_value_unit_and_whether_it_is_the_default():
    rows = {r["param"]: r for r in cutoff_rows({"max_pct_mt": 20.0}, SCHEMA)}
    assert rows["max_pct_mt"] == {"param": "max_pct_mt", "label": "mitochondrial limit", "value": 20.0,
                                  "unit": "%", "default": 10.0, "changed": True}
    assert rows["min_counts"]["value"] == 500 and rows["min_counts"]["changed"] is False
    assert rows["min_features"]["unit"] == "genes"
    assert cutoff_rows({}, {"resolution": {"default": 1.0}}) == [], "a tool without cutoffs has none"
    assert [r["changed"] for r in cutoff_rows({"max_pct_mt": 10.0}, SCHEMA)] == [False] * 3, \
        "setting the default value again is not a change"


# --- in the plan loop ------------------------------------------------------------------------------------

def _catalog(calls: list) -> list[HarnessTool]:
    def tool(name: str, props: dict) -> HarnessTool:
        def run(args, _ctx):
            calls.append((name, dict(args)))
            return {"status": "ok", "tool": name}
        return HarnessTool(name, f"{name} test double", {"type": "object", "properties": props}, run)

    return [tool("run_cellqc", SCHEMA),
            tool("run_clustering", {"resolution": {"type": "number", "default": 1.0}})]


def _scientist_chat(messages, _tools):
    """Calls the tool the step names with NO arguments: whatever runs came from the approved plan."""
    finish = {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
        "name": "finish", "arguments": json.dumps({"answer": "done"})}}]}
    if any(m.get("role") == "tool" for m in messages):
        return finish
    brief = next(m["content"] for m in messages if m.get("role") == "user")
    step = brief.split("Step to execute now:", 1)[1].split("\n", 1)[0]
    for name, args in (("run_cellqc", {}), ("run_clustering", {"resolution": 0.5})):
        if f"`{name}`" in step:
            return {"content": "", "tool_calls": [{"id": "t", "type": "function", "function": {
                "name": name, "arguments": json.dumps(args)}}]}
    return finish


def _complete(counts: dict, revise_reply: str = '{"ops": []}'):
    def complete(messages):
        system = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in system:
            counts["plan"] = counts.get("plan", 0) + 1
            return json.dumps({"agenda": [QC, CLUSTER]})
        if "filling in a form" in system:
            counts["bind"] = counts.get("bind", 0) + 1
            out = []
            for n, text in re.findall(r"^(\d+)\. (.+)$", messages[1]["content"], re.M):
                found = {}
                if m := re.search(r"fewer than (\d+) counts", text):
                    found["min_counts"] = int(m.group(1))
                if m := re.search(r"fewer than (\d+) genes", text):
                    found["min_features"] = int(m.group(1))
                if m := re.search(r"more than (\d+(?:\.\d+)?)% mitochondrial", text):
                    found["max_pct_mt"] = float(m.group(1))
                if m := re.search(r"resolution (\d+(?:\.\d+)?)", text):
                    found["resolution"] = float(m.group(1))
                if found:
                    out.append({"step": int(n), "settings": found})
            return json.dumps(out)
        if "changing an analysis plan" in system:
            counts["revise"] = counts.get("revise", 0) + 1
            return revise_reply
        if "revising ONE step" in system:
            counts["patch"] = counts.get("patch", 0) + 1
            return '{"step": 0, "new_text": ""}'
        if "rigorous scientific Critic" in system:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        return "FINAL REPORT"
    return complete


def _lab(calls: list, counts: dict, revise_reply: str = '{"ops": []}') -> ResearchLab:
    scientist = ResearchHarness(catalog=_catalog(calls), chat_fn=_scientist_chat)
    return ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"),
                       LabConfig(plan_yaml=True, plan_review=False, auto_select_skill=False,
                                 mode="single", planner="linear"),
                       complete_fn=_complete(counts, revise_reply), scientist=scientist)


def _run(lab: ResearchLab, *replies: str):
    """Run with a reviewer who sends each of ``replies`` in turn, then approves."""
    events: list[dict] = []
    seen: list[list[str]] = []

    def review(_kind, payload):
        seen.append(list(payload))
        n = len(seen)
        return {"action": "revise", "feedback": replies[n - 1]} if n <= len(replies) else {"action": "approve"}

    result = lab.run("QC the cells then cluster them", events.append, review)
    return result, events, seen


def _of(events, kind):
    return [e for e in events if e.get("type") == kind]


def _cutoffs(events, step: int = 0) -> dict:
    """The cutoff rows of the latest plan card, by setting name."""
    return {r["param"]: r for r in _of(events, "plan_doc")[-1]["steps"][step]["cutoffs"]}


def test_the_first_plan_card_shows_every_cutoff_as_its_default():
    calls: list = []
    _result, events, _seen = _run(_lab(calls, {}))
    rows = _cutoffs(events)
    assert {k: (r["value"], r["unit"], r["changed"]) for k, r in rows.items()} == {
        "min_counts": (500, "counts", False), "min_features": (300, "genes", False),
        "max_pct_mt": (10.0, "%", False)}
    assert "cutoffs" not in _of(events, "plan_doc")[-1]["steps"][1], "only a step whose tool takes them"


def test_a_message_sets_the_cutoff_without_the_model_and_the_card_marks_it_changed():
    calls: list = []
    counts: dict = {}
    _result, events, seen = _run(_lab(calls, counts), "raise the mitochondrial limit to 20")

    assert "revise" not in counts and "patch" not in counts and counts["plan"] == 1, \
        "read by code: no reviser call, no patch, no redraft"
    assert seen[1][1] is seen[0][1], "the step the message did not concern is the same string"
    assert "20" in seen[1][0] and "10% mitochondrial" not in seen[1][0], "the sentence follows its setting"
    rows = _cutoffs(events)
    assert (rows["max_pct_mt"]["value"], rows["max_pct_mt"]["changed"], rows["max_pct_mt"]["default"]) == (20.0, True, 10.0)
    assert rows["min_counts"]["changed"] is False and rows["min_features"]["changed"] is False
    edited = _of(events, "plan_edited")[0]
    assert edited["by"] == "pi" and ("setting", "max_pct_mt", 10.0, 20.0) in [
        (c["kind"], c.get("param"), c.get("before"), c.get("after")) for c in edited["changes"]]


def test_the_value_survives_a_later_revision_and_reaches_the_tool_unchanged():
    calls: list = []
    reply = json.dumps({"ops": [{"op": "change", "step": "s2", "settings": {"resolution": 0.8}}]})
    _result, events, _seen = _run(_lab(calls, {}, reply),
                                  "set max_pct_mt to 20", "cluster at resolution 0.8")
    assert _cutoffs(events)["max_pct_mt"]["value"] == 20.0 and _cutoffs(events)["max_pct_mt"]["changed"]
    run = [args for name, args in calls if name == "run_cellqc"]
    assert run == [{"min_counts": 500, "min_features": 300, "max_pct_mt": 20.0}], \
        "the Scientist sent nothing; the approved value is exactly what ran"
    assert ("run_clustering", {"resolution": 0.8}) in calls


def test_two_cutoffs_in_one_message_are_both_set():
    calls: list = []
    _result, events, _seen = _run(_lab(calls, {}), "set min_counts to 1000 and min_features to 250")
    rows = _cutoffs(events)
    assert (rows["min_counts"]["value"], rows["min_features"]["value"]) == (1000, 250)
    assert [args for name, args in calls if name == "run_cellqc"] == [
        {"min_counts": 1000, "min_features": 250, "max_pct_mt": 10.0}]


@pytest.mark.parametrize("request_text, why", [
    ("set max_pct_mt to 150", "cannot be above 100"),
    ("raise the mito limit", "not the value"),
    ("change the QC cutoff to 20", "Which cutoff?"),
])
def test_a_request_that_cannot_be_used_is_explained_before_anything_runs(request_text, why):
    calls: list = []
    counts: dict = {}
    _result, events, seen = _run(_lab(calls, counts), request_text)
    answer = _of(events, "plan_answer")[0]["answer"]
    assert why in answer and answer.startswith("Nothing was changed.")
    assert seen[1] == seen[0] and not _of(events, "plan_edited"), "the plan is as it was"
    assert "revise" not in counts and "patch" not in counts and counts["plan"] == 1
    assert [args for name, args in calls if name == "run_cellqc"] == [
        {"min_counts": 500, "min_features": 300, "max_pct_mt": 10.0}], "it ran with the defaults, as approved"


def test_a_cutoff_the_plan_already_uses_is_said_so_and_nothing_is_redrafted():
    counts: dict = {}
    _result, events, seen = _run(_lab([], counts), "set max_pct_mt to 10")
    assert "already uses max_pct_mt = 10" in _of(events, "plan_answer")[0]["answer"]
    assert seen[1] == seen[0] and counts["plan"] == 1 and "patch" not in counts


def test_a_plan_with_no_cellqc_step_leaves_the_message_to_the_other_ways_of_revising():
    calls: list = []
    counts: dict = {}
    lab = _lab(calls, counts)
    lab.scientist = ResearchHarness(catalog=[t for t in _catalog(calls) if t.name != "run_cellqc"],
                                    chat_fn=_scientist_chat)
    _result, events, _seen = _run(lab, "raise the mitochondrial limit to 20")
    assert not [e for e in events if e.get("type") == "plan_answer"], "the cutoff path explains nothing here"
    assert not _of(events, "plan_edited") or _of(events, "plan_edited")[0]["by"] == "pi"


# --- the reply --------------------------------------------------------------------------------------------

def test_the_qc_reply_states_the_cutoffs_used_and_which_were_changed():
    result = {"cells_before": 1000, "cells_after": 900,
              "thresholds": {"min_counts": 500, "min_features": 300, "max_pct_mt": 20.0},
              "threshold_defaults": {"min_counts": 500, "min_features": 300, "max_pct_mt": 10.0}}
    assert cutoff_lines(result) == [
        "Cutoffs used: min_counts 500 counts (default); min_features 300 genes (default); "
        "max_pct_mt 20% (changed from the default 10%)."]
    assert "max_pct_mt 20% (changed from the default 10%)" in qc_result_summary(result)
    assert cutoff_lines({}) == [] and cutoff_lines({"thresholds": {}}) == []
    assert "threshold_defaults" in QC_SUMMARY_POLICY and "max_pct_mt" in QC_SUMMARY_POLICY
