"""The plan document in the loop (``LabConfig.plan_yaml``): edit, approve, run, read back.

Three things had no mechanism while a plan was prose only, and each is pinned here end to end with
scripted models:

* the researcher's own edit of the plan is applied by code — no model redrafts anything, so the
  steps they did not touch cannot change, and an edit that cannot run is refused with its reasons;
* a number approved on the plan is the number the tool is called with, even when the Scientist
  sends another one;
* the finished run is compared with the approved plan, step by step.

With the flag off none of it exists: the loop, the Scientist call and the plan card are as before.
"""

from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("yaml")

from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness  # noqa: E402
from aiscientist.agents.research_lab import LabConfig, ResearchLab  # noqa: E402

QC = ("**QC & normalization** — Measure each cell's quality with `run_scanpy_qc` and remove cells "
      "detecting fewer than 200 genes and cells whose reads are more than 10% mitochondrial.")
CLUSTER = ("**Clustering & UMAP** — Group the cells with `run_clustering` at resolution 0.5 and "
           "lay them out on a 2-D map.")
AGENDA = [QC, CLUSTER]


def _catalog(calls: list) -> list[HarnessTool]:
    def tool(name: str, props: dict) -> HarnessTool:
        def run(args, _ctx):
            calls.append((name, dict(args)))
            return {"status": "ok", "tool": name}
        return HarnessTool(name, f"{name} test double", {"type": "object", "properties": props}, run)

    return [
        tool("run_scanpy_qc", {"min_genes": {"type": "integer", "default": 200},
                               "max_pct_mt": {"type": "number", "default": 20}}),
        tool("run_clustering", {"resolution": {"type": "number", "default": 1.0},
                                "n_pcs": {"type": "integer", "default": 30}}),
    ]


def _scientist_chat(messages, _tools):
    """Call the tool the step names with the SCIENTIST's own idea of the settings, then finish."""
    finish = {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
        "name": "finish", "arguments": json.dumps({"answer": "done"})}}]}
    if any(m.get("role") == "tool" for m in messages):
        return finish
    brief = next(m["content"] for m in messages if m.get("role") == "user")
    step = brief.split("Step to execute now:", 1)[1].split("\n", 1)[0]
    for name, args in (("run_clustering", {"resolution": 0.5, "n_pcs": 30}),
                       ("run_scanpy_qc", {"min_genes": 200})):
        if f"`{name}`" in step:
            return {"content": "", "tool_calls": [{"id": "t", "type": "function", "function": {
                "name": name, "arguments": json.dumps(args)}}]}
    return finish


def _complete(counts: dict):
    def complete(messages):
        system = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in system:
            counts["plan"] = counts.get("plan", 0) + 1
            return json.dumps({"agenda": AGENDA})
        if "filling in a form" in system:
            counts["bind"] = counts.get("bind", 0) + 1
            # n_pcs is the binder being helpful: the step never states 30, so it must not be kept.
            return json.dumps([{"step": 1, "settings": {"min_genes": 200, "max_pct_mt": 10}},
                               {"step": 2, "settings": {"resolution": 0.5, "n_pcs": 30}}])
        if "revising ONE step" in system:
            counts["patch"] = counts.get("patch", 0) + 1
            return '{"step": 0, "new_text": ""}'
        if "rigorous scientific Critic" in system:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        return "FINAL REPORT"
    return complete


def _lab(calls: list, counts: dict, *, plan_yaml: bool = True, scientist=None,
         planner: str = "linear") -> ResearchLab:
    scientist = scientist or ResearchHarness(catalog=_catalog(calls), chat_fn=_scientist_chat)
    return ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"),
                       LabConfig(plan_yaml=plan_yaml, plan_review=False, auto_select_skill=False,
                                 mode="single", planner=planner),
                       complete_fn=_complete(counts), scientist=scientist)


def _run(lab: ResearchLab, decide):
    """Run with a scripted reviewer. ``decide(n, payload, doc)`` gets the latest plan document."""
    events: list[dict] = []
    seen: list[list[str]] = []

    def review(_kind, payload):
        seen.append(list(payload))
        doc = next((e for e in reversed(events) if e.get("type") == "plan_doc"), None)
        return decide(len(seen), payload, doc)

    result = lab.run("Cluster the cells", events.append, review)
    return result, events, seen


def _of(events, kind):
    return [e for e in events if e.get("type") == kind]


def test_the_plan_card_gets_a_document_whose_settings_the_step_text_states():
    calls: list = []
    counts: dict = {}
    _result, events, _seen = _run(_lab(calls, counts), lambda *_a: {"action": "approve"})
    steps = _of(events, "plan_doc")[0]["steps"]
    assert [(s["id"], s["tool"], s.get("settings")) for s in steps] == [
        ("s1", "run_scanpy_qc", {"min_genes": 200, "max_pct_mt": 10.0}),
        ("s2", "run_clustering", {"resolution": 0.5})]
    assert _of(events, "plan_bind_dropped")[0]["dropped"][0]["param"] == "n_pcs"


def test_an_edit_is_applied_by_code_and_the_approved_number_is_the_one_that_runs():
    calls: list = []
    counts: dict = {}

    def decide(n, _payload, doc):
        if n == 1:
            return {"action": "edit", "plan_yaml": doc["yaml"].replace("resolution: 0.5", "resolution: 1.2")}
        return {"action": "approve"}

    lab = _lab(calls, counts)
    result, events, seen = _run(lab, decide)

    # The edit: no model drafted, patched or re-read anything.
    assert counts == {"plan": 1, "bind": 1}, "an edit must not call the PI, the patch or the binder again"
    assert seen[1][0] is seen[0][0], "the step that was not edited is the same string"
    assert "resolution 1.2" in seen[1][1], "the sentence follows its setting"
    assert [(c["kind"], c["param"], c["after"]) for c in _of(events, "plan_edited")[0]["changes"]] == [
        ("setting", "resolution", 1.2)]

    # The run: the Scientist sent resolution 0.5; the approved plan said 1.2.
    assert ("run_clustering", {"resolution": 1.2, "n_pcs": 30}) in calls
    assert ("run_scanpy_qc", {"min_genes": 200, "max_pct_mt": 10.0}) in calls, \
        "an approved setting the model left out is filled in"
    enforced = _of(events, "plan_setting_enforced")
    assert enforced and enforced[0]["overrode"] == [{"param": "resolution", "approved": 1.2, "model": 0.5}]

    # The read-back: both approved steps ran as approved.
    check = _of(events, "plan_adherence")[0]
    assert check["ok"] is True and check["as_planned"] == 2
    assert "resolution: 1.2" in lab.approved_plan_yaml
    assert result.agenda == seen[1]


def test_on_the_dag_planner_the_approved_number_is_the_one_that_runs_too():
    """The DAG planner (prod's default) hands the Scientist a node brief — the step's text plus
    reuse hints and "Do ONLY this task" — and the approved settings were looked up by that brief, so
    none was ever found: prod run caa188e8e672 called run_cellqc with species=auto against an approved
    "mouse", and its own read-back said 0 of 1 steps ran as approved. Every test above ran linear."""
    calls: list = []
    counts: dict = {}

    def decide(n, _payload, doc):
        if n == 1:
            return {"action": "edit", "plan_yaml": doc["yaml"].replace("resolution: 0.5", "resolution: 1.2")}
        return {"action": "approve"}

    lab = _lab(calls, counts, planner="dag")
    _result, events, _seen = _run(lab, decide)

    briefs = [e["step"] for e in _of(events, "scientist_start")]
    assert briefs and all("Do ONLY this task" in b for b in briefs), "the DAG path ran, with node briefs"
    assert ("run_clustering", {"resolution": 1.2, "n_pcs": 30}) in calls
    assert ("run_scanpy_qc", {"min_genes": 200, "max_pct_mt": 10.0}) in calls
    enforced = _of(events, "plan_setting_enforced")
    assert enforced and enforced[0]["overrode"] == [{"param": "resolution", "approved": 1.2, "model": 0.5}]
    check = _of(events, "plan_adherence")[0]
    assert check["ok"] is True and check["as_planned"] == 2


@pytest.mark.parametrize("bad, reason", [
    ("resolution: [unclosed", "does not parse"),
    ("resolutionn: 0.5", "no setting `resolutionn`"),
    ("resolution: fast", "cannot take resolution"),
])
def test_an_edit_that_cannot_run_is_refused_and_the_plan_is_untouched(bad, reason):
    calls: list = []
    counts: dict = {}

    def decide(n, _payload, doc):
        if n == 1:
            return {"action": "edit", "plan_yaml": doc["yaml"].replace("resolution: 0.5", bad)}
        return {"action": "approve"}

    _result, events, seen = _run(_lab(calls, counts), decide)
    assert reason in " ".join(_of(events, "plan_edit_rejected")[0]["problems"])
    assert seen[1] == seen[0] and not _of(events, "plan_edited")
    assert ("run_clustering", {"resolution": 0.5, "n_pcs": 30}) in calls


def test_a_step_the_run_never_executed_is_named_in_the_read_back():
    calls: list = []
    counts: dict = {}
    lab = _lab(calls, counts)
    result, events, _seen = _run(lab, lambda *_a: {"action": "approve"})
    assert _of(events, "plan_adherence")[0]["ok"] is True
    # The same approved plan, read against a run in which the clustering step did not happen.
    from aiscientist.agents.plan_doc import check_adherence
    report = check_adherence(lab._approved_plan, [
        {"step": r.step, "accepted": True, "calls": r.scientist_result.get("steps") or []}
        for r in result.rounds[:1]])
    assert [(r["step"], r["status"]) for r in report["steps"]][1] == (2, "not_run")


def test_with_the_flag_off_the_loop_is_as_it_was():
    calls: list = []
    counts: dict = {}

    class _OldScientist(ResearchHarness):
        """A scientist that predates ``pinned_args``: passing it would be a TypeError."""

        def run(self, brief, ctx, on_event=None, should_cancel=None, untrusted_text=None,  # noqa: D102
                extra_steps=0):
            return super().run(brief, ctx, on_event=on_event, should_cancel=should_cancel,
                               untrusted_text=untrusted_text, extra_steps=extra_steps)

    lab = _lab(calls, counts, plan_yaml=False,
               scientist=_OldScientist(catalog=_catalog(calls), chat_fn=_scientist_chat))

    def decide(n, _payload, doc):
        assert doc is None, "no plan document is built when the flag is off"
        return {"action": "edit", "plan_yaml": "steps: []"} if n == 1 else {"action": "approve"}

    result, events, seen = _run(lab, decide)
    assert "bind" not in counts
    assert _of(events, "plan_edit_rejected") and seen[1] == seen[0]
    assert not _of(events, "plan_adherence") and lab.approved_plan_yaml == ""
    assert ("run_clustering", {"resolution": 0.5, "n_pcs": 30}) in calls
    assert result.accepted_steps == 2


def test_the_harness_tells_the_model_which_value_actually_ran():
    calls: list = []
    fed: list = []

    def chat(messages, _tools):
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if tool_msgs:
            fed.append(tool_msgs[-1]["content"])
            return {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
                "name": "finish", "arguments": json.dumps({"answer": "done"})}}]}
        return {"content": "", "tool_calls": [{"id": "t", "type": "function", "function": {
            "name": "run_clustering", "arguments": json.dumps({"resolution": 0.5})}}]}

    harness = ResearchHarness(catalog=_catalog(calls), chat_fn=chat)
    result = harness.run("Cluster.", HarnessContext(decisions={}, tunnel_port=1, model="m"),
                         pinned_args={"run_clustering": {"resolution": 1.0}})
    assert calls == [("run_clustering", {"resolution": 1.0})]
    assert result.steps[0]["args"] == {"resolution": 1.0}, "the record shows the call that ran"
    assert result.steps[0]["plan_enforced"] == [{"param": "resolution", "approved": 1.0, "model": 0.5}]
    assert '"approved_settings_applied"' in fed[0]


# --- a change asked for in words is made ON the document ------------------------------------------------

def _complete_revising(counts: dict, revise_reply: str):
    """The models of ``_complete``, plus the plan reviser answering ``revise_reply``, and a binder that
    states what each listed step's own sentence says (so a reworded step can be read again)."""
    base = _complete(counts)

    def complete(messages):
        system = messages[0]["content"]
        if "changing an analysis plan" in system:
            counts["revise"] = counts.get("revise", 0) + 1
            assert "```yaml" in messages[1]["content"] and "id: s2" in messages[1]["content"], \
                "the reviser is shown the plan document itself"
            return revise_reply
        if "filling in a form" in system:
            counts["bind"] = counts.get("bind", 0) + 1
            listing = messages[1]["content"]
            out = []
            for n, text in re.findall(r"^(\d+)\. (.+)$", listing, re.M):
                found = {}
                if m := re.search(r"resolution (\d+(?:\.\d+)?)", text):
                    found["resolution"] = float(m.group(1))
                if m := re.search(r"fewer than (\d+) genes", text):
                    found["min_genes"] = int(m.group(1))
                if found:
                    out.append({"step": int(n), "settings": found})
            return json.dumps(out)
        return base(messages)
    return complete


def _revising_lab(calls: list, counts: dict, reply: str) -> ResearchLab:
    lab = _lab(calls, counts)
    lab._complete_fn = _complete_revising(counts, reply)
    return lab


def _ask_then_approve(request: str):
    def decide(n, _payload, _doc):
        return {"action": "revise", "feedback": request} if n == 1 else {"action": "approve"}
    return decide


def test_a_change_asked_for_in_words_is_applied_to_the_document_and_nothing_else_moves():
    calls: list = []
    counts: dict = {}
    reply = json.dumps({"ops": [
        {"op": "change", "step": "s2", "settings": {"resolution": 1.2}},
        {"op": "add", "after": "s2", "title": "Fine clustering",
         "does": "Re-cluster the cells with `run_clustering` at resolution 2 to split the large groups."},
    ]})
    lab = _revising_lab(calls, counts, reply)
    _result, events, seen = _run(lab, _ask_then_approve("use resolution 1.2, then add a finer clustering at 2"))

    assert counts.get("revise") == 1 and counts.get("plan") == 1 and "patch" not in counts, \
        "made on the document: no single-step patch, no redraft"
    assert seen[1][0] is seen[0][0], "the step the request did not concern is the same string"
    assert "resolution 1.2" in seen[1][1] and len(seen[1]) == 3
    edited = _of(events, "plan_edited")[0]
    assert edited["by"] == "pi"
    assert {(c["kind"], c["step"]) for c in edited["changes"]} >= {("setting", 2), ("added", 3)}
    assert ("run_clustering", {"resolution": 1.2, "n_pcs": 30}) in calls
    assert ("run_clustering", {"resolution": 2.0, "n_pcs": 30}) in calls, \
        "the added step's setting was read from its own sentence and pinned"


def test_a_step_reworded_without_settings_has_its_settings_read_again():
    calls: list = []
    counts: dict = {}
    reply = json.dumps({"ops": [{"op": "change", "step": "s2", "does":
                                 "Group the cells with `run_clustering` at resolution 0.8 and lay them out on a 2-D map."}]})
    _result, events, seen = _run(_revising_lab(calls, counts, reply), _ask_then_approve("cluster a bit finer, 0.8"))
    doc = _of(events, "plan_doc")[-1]["steps"]
    assert doc[1]["settings"] == {"resolution": 0.8}, "the old 0.5 is not left pinned under prose that says 0.8"
    assert ("setting", "resolution", 0.5, 0.8) in [
        (c["kind"], c.get("param"), c.get("before"), c.get("after")) for c in _of(events, "plan_edited")[0]["changes"]]
    assert ("run_clustering", {"resolution": 0.8, "n_pcs": 30}) in calls


@pytest.mark.parametrize("reply, why", [
    ('{"ops": []}', "no operations"),
    ("I would redraft the plan.", "no operations"),
    ('{"ops": [{"op": "change", "step": "s2", "settings": {"resolutionn": 1}}]}', "no setting `resolutionn`"),
    ('{"ops": [{"op": "remove", "step": "s9"}]}', "no step 's9'"),
])
def test_when_the_document_cannot_take_the_change_the_old_revision_path_runs(reply, why):
    calls: list = []
    counts: dict = {}
    _result, events, seen = _run(_revising_lab(calls, counts, reply), _ask_then_approve("change the clustering"))
    assert why in _of(events, "plan_revise_skipped")[0]["why"]
    assert counts.get("patch") == 1 and counts.get("plan") == 2, "patch first, then the redraft — as before"
    assert not _of(events, "plan_edited")


def test_a_code_step_the_reviser_adds_carries_no_code_as_a_setting():
    """Seen on the served model: asked for a dot plot, it added a run_code step with the whole snippet
    under `settings.code`, which pinning would then have forced on the Scientist."""
    calls: list = []
    counts: dict = {}
    code_tool = HarnessTool("run_code", "run Python", {"type": "object", "properties": {
        "code": {"type": "string"}}}, lambda args, _ctx: {"status": "ok"})
    scientist = ResearchHarness(catalog=[*_catalog(calls), code_tool], chat_fn=_scientist_chat)
    lab = _lab(calls, counts, scientist=scientist)
    lab._complete_fn = _complete_revising(counts, json.dumps({"ops": [
        {"op": "add", "after": "s2", "title": "Dot plot", "tool": "run_code",
         "does": "Draw a dot plot of the top markers per cluster with `run_code`.",
         "settings": {"code": "import scanpy as sc\nsc.pl.dotplot(adata, ['A'], groupby='leiden')"}}]}))
    _result, events, seen = _run(lab, _ask_then_approve("add a dot plot of the markers"))
    added = _of(events, "plan_doc")[-1]["steps"][2]
    assert added["tool"] == "run_code" and not added.get("settings")
    assert _of(events, "plan_edited")[0]["by"] == "pi" and len(seen[1]) == 3


# --- words after the researcher's own edit change the EDITED plan ----------------------------------------

def _recording(lab: ResearchLab, prompts: list) -> ResearchLab:
    """Keep every prompt the lab's models are given, by the system prompt's first words."""
    inner = lab._complete_fn

    def complete(messages):
        prompts.append((messages[0]["content"][:60], "\n".join(m["content"] for m in messages[1:])))
        return inner(messages)
    lab._complete_fn = complete
    return lab


def _reviser_prompts(prompts):
    return [p for s, p in prompts if s.startswith("You are changing an analysis plan")]


def test_words_after_a_hand_edit_are_made_on_the_edited_plan():
    calls: list = []
    counts: dict = {}
    prompts: list = []
    reply = json.dumps({"ops": [{"op": "change", "step": "s1", "settings": {"min_genes": 300}}]})
    lab = _recording(_revising_lab(calls, counts, reply), prompts)

    def decide(n, _payload, doc):
        if n == 1:
            return {"action": "edit", "plan_yaml": doc["yaml"].replace("resolution: 0.5", "resolution: 1.2")}
        if n == 2:
            return {"action": "revise", "feedback": "keep cells with at least 300 genes"}
        return {"action": "approve"}

    _result, events, seen = _run(lab, decide)
    shown = _reviser_prompts(prompts)
    assert len(shown) == 1 and "resolution: 1.2" in shown[0] and "resolution: 0.5" not in shown[0], \
        "the PI is shown the plan with the researcher's edit"
    assert "resolution 1.2" in seen[2][1] and "300 genes" in seen[2][0], "both changes are in the plan"
    assert ("run_clustering", {"resolution": 1.2, "n_pcs": 30}) in calls
    assert ("run_scanpy_qc", {"min_genes": 300}) in calls


def test_a_reply_sent_with_an_unapplied_edit_applies_the_edit_first():
    """The researcher typed an edit in the document, did not apply it, and then asked for a change
    in words: the edit is applied (by code) and the words change the edited plan."""
    calls: list = []
    counts: dict = {}
    prompts: list = []
    reply = json.dumps({"ops": [{"op": "change", "step": "s1", "settings": {"min_genes": 300}}]})
    lab = _recording(_revising_lab(calls, counts, reply), prompts)

    def decide(n, _payload, doc):
        if n == 1:
            return {"action": "revise", "feedback": "keep cells with at least 300 genes",
                    "plan_yaml": doc["yaml"].replace("resolution: 0.5", "resolution: 1.2")}
        return {"action": "approve"}

    _result, events, seen = _run(lab, decide)
    edits = _of(events, "plan_edited")
    assert [e.get("by") for e in edits] == [None, "pi"], "the researcher's edit first, then the PI's"
    assert [(c["kind"], c["param"], c["after"]) for c in edits[0]["changes"]] == [("setting", "resolution", 1.2)]
    assert "resolution: 1.2" in _reviser_prompts(prompts)[0]
    assert ("run_clustering", {"resolution": 1.2, "n_pcs": 30}) in calls
    assert ("run_scanpy_qc", {"min_genes": 300}) in calls


def test_an_unapplied_edit_that_cannot_be_applied_stops_the_reply_too():
    calls: list = []
    counts: dict = {}
    reply = json.dumps({"ops": [{"op": "change", "step": "s1", "settings": {"min_genes": 300}}]})

    def decide(n, _payload, doc):
        if n == 1:
            return {"action": "revise", "feedback": "keep cells with at least 300 genes",
                    "plan_yaml": doc["yaml"].replace("resolution: 0.5", "resolution: fast")}
        return {"action": "approve"}

    _result, events, seen = _run(_revising_lab(calls, counts, reply), decide)
    problems = " ".join(_of(events, "plan_edit_rejected")[0]["problems"])
    assert "cannot take resolution" in problems and "message was not sent to the PI" in problems
    assert "revise" not in counts and seen[1] == seen[0] and not _of(events, "plan_edited"), \
        "nothing changed: not the edit, not the words"


def test_an_unapplied_edit_that_changes_nothing_does_not_hold_the_reply():
    calls: list = []
    counts: dict = {}
    reply = json.dumps({"ops": [{"op": "change", "step": "s1", "settings": {"min_genes": 300}}]})

    def decide(n, _payload, doc):
        if n == 1:
            return {"action": "revise", "feedback": "keep cells with at least 300 genes",
                    "plan_yaml": doc["yaml"] + "\n"}
        return {"action": "approve"}

    _result, events, _seen = _run(_revising_lab(calls, counts, reply), decide)
    assert not _of(events, "plan_edit_rejected") and counts.get("revise") == 1
    assert ("run_scanpy_qc", {"min_genes": 300}) in calls


def test_a_redraft_after_a_hand_edit_is_told_to_keep_it():
    """When the change asked for in words cannot be made on the document, the plan is patched or
    redrafted from the feedback: the researcher's edit is part of that feedback, so a redraft
    cannot quietly put the old value back."""
    calls: list = []
    counts: dict = {}
    prompts: list = []
    lab = _recording(_revising_lab(calls, counts, '{"ops": []}'), prompts)

    def decide(n, _payload, doc):
        if n == 1:
            return {"action": "edit", "plan_yaml": doc["yaml"].replace("resolution: 0.5", "resolution: 1.2")}
        if n == 2:
            return {"action": "revise", "feedback": "make the clustering more thorough"}
        return {"action": "approve"}

    _run(lab, decide)
    redraft = [p for s, p in prompts if "Principal Investigator" in s and "did not object to" in p][-1]
    assert "edited the plan by hand; keep these changes: set resolution to 1.2 in step 2" in redraft
    assert "make the clustering more thorough" in redraft
