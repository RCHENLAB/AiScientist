"""The plan as a YAML document (``agents/plan_doc.py``): what it must keep true.

A plan was prose only, so two things had no home: which of a step's numbers are its tool's
arguments, and what exactly was approved. The document carries both. What is pinned here is that
it does so without becoming a second, drifting copy of the plan — the text survives a round trip
unchanged, an edit touches only what was edited, a setting is admitted only when the step's own
text states it, and a finished run can be read back against the document step by step.
"""

from __future__ import annotations

import pytest

pytest.importorskip("yaml")

from aiscientist.agents.plan_doc import (  # noqa: E402
    PlanDoc,
    PlanError,
    PlanStep,
    adherence_markdown,
    apply_edit,
    apply_ops,
    bind_settings,
    call_settings,
    check_adherence,
    check_plan,
    parse_plan_ops,
    parse_plan_yaml,
    pin_args,
    split_step,
    sync_plan,
)

QC = ("**QC & normalization** — Measure each cell's quality with `run_scanpy_qc` and remove cells "
      "detecting fewer than 200 genes and cells whose reads are more than 10% mitochondrial.")
CLUSTER = ("**Clustering & UMAP** — Group the cells with `run_clustering` at resolution 0.5 and "
           "lay them out on a 2-D map.")
SUMMARY = "Summarise shared versus class-specific changes"
AGENDA = [QC, CLUSTER, SUMMARY]

CLUSTER_PROPS = {
    "resolution": {"type": "number", "default": 1.0},
    "n_pcs": {"type": "integer", "default": 30},
    "method": {"type": "string", "enum": ["leiden", "louvain"]},
    "select_resolution": {"type": "boolean", "default": False},
}
QC_PROPS = {"min_genes": {"type": "integer", "default": 200},
            "max_pct_mt": {"type": "number", "default": 20}}
SCHEMAS = {"run_clustering": CLUSTER_PROPS, "run_scanpy_qc": QC_PROPS}


def _tool_of(text: str) -> "str | None":
    return next((t for t in SCHEMAS if f"`{t}`" in text), None)


def _doc(settings: "dict | None" = None) -> PlanDoc:
    doc, _fresh = sync_plan(None, AGENDA, request="Compare DDX41 with WT: rods", tool_of=_tool_of,
                            runs_on_of=lambda _t: "hpc:analysis")
    steps = list(doc.steps)
    steps[1] = PlanStep(id="s2", text=CLUSTER, tool="run_clustering",
                        settings=settings if settings is not None else {"resolution": 0.5},
                        runs_on="hpc:analysis")
    return PlanDoc(doc.request, tuple(steps))


# --- the document reads back as what was written ------------------------------------------------

def test_a_plan_survives_the_round_trip_through_yaml_unchanged():
    doc = _doc()
    back = parse_plan_yaml(doc.to_yaml(), request=doc.request)
    assert back.agenda() == AGENDA, "the step text the Scientist is handed must not drift"
    assert [(s.id, s.tool, s.settings) for s in back.steps] == [
        ("s1", "run_scanpy_qc", {}), ("s2", "run_clustering", {"resolution": 0.5}), ("s3", None, {})]


def test_values_yaml_would_reinterpret_stay_what_they_were():
    """A group called ``no``, a column called ``off`` and a tiny threshold are all ordinary settings."""
    doc = _doc({"groupby": "no", "reference": "off", "padj": 1e-05, "label": "2026-10-12",
                "cols": ["a,b", "c"], "flag": True, "n": 3})
    back = parse_plan_yaml(doc.to_yaml()).steps[1].settings
    assert back == {"groupby": "no", "reference": "off", "padj": 1e-05, "label": "2026-10-12",
                    "cols": ["a,b", "c"], "flag": True, "n": 3}
    assert isinstance(back["padj"], float)


def test_a_title_with_yaml_punctuation_is_quoted_not_broken():
    step = PlanStep(id="s1", text="**DDX41 vs WT: rods #1** — Compare the arms.")
    back = parse_plan_yaml(PlanDoc("q", (step,)).to_yaml())
    assert back.steps[0].title == "DDX41 vs WT: rods #1"
    assert back.steps[0].text == step.text


def test_split_step_reads_a_titled_and_an_untitled_step():
    assert split_step(QC)[0] == "QC & normalization"
    assert split_step(SUMMARY) == ("", SUMMARY)


# --- a document a person edited: every defect, in one answer ---------------------------------------

def test_a_yaml_error_says_where():
    with pytest.raises(PlanError) as err:
        parse_plan_yaml("plan: 1\nsteps:\n  - id: s1\n    does: [unclosed\n")
    assert "line" in err.value.problems[0]


def test_every_structural_defect_is_reported_together():
    text = """
steps:
  - id: s1
    does: First step
  - id: s1
    does: Same id again
  - id: s3
    title: ""
  - id: s4
    does: Settings but no tool
    settings: {resolution: 1.0}
"""
    with pytest.raises(PlanError) as err:
        parse_plan_yaml(text)
    joined = " ".join(err.value.problems)
    assert "used by an earlier step" in joined
    assert "neither a title nor" in joined
    assert "names no `tool`" in joined


def test_an_empty_plan_is_refused_rather_than_run_as_nothing():
    with pytest.raises(PlanError):
        parse_plan_yaml("plan: 1\nsteps: []\n")


def test_a_step_added_without_an_id_gets_a_fresh_one():
    back = parse_plan_yaml("steps:\n  - id: s1\n    does: One\n  - does: Two\n")
    assert [s.id for s in back.steps] == ["s1", "s2"]


# --- carrying a plan across a revision ---------------------------------------------------------------

def test_an_untouched_step_keeps_its_id_and_its_settings_across_a_revision():
    doc = _doc({"resolution": 0.5, "n_pcs": 40})
    revised = [QC.replace("200 genes", "500 genes"), CLUSTER, SUMMARY]
    new, fresh = sync_plan(doc, revised, request=doc.request, tool_of=_tool_of)
    assert fresh == [0], "only the rewritten step needs its settings read again"
    assert new.steps[1] is not None and new.steps[1].settings == {"resolution": 0.5, "n_pcs": 40}
    assert [s.id for s in new.steps] == ["s1", "s2", "s3"], "a rewritten step keeps its place's id"


def test_a_dropped_step_does_not_shift_the_ids_of_the_others():
    doc = _doc()
    new, fresh = sync_plan(doc, [QC, SUMMARY], request=doc.request, tool_of=_tool_of)
    assert fresh == [] and [s.id for s in new.steps] == ["s1", "s3"]


# --- settings: only what the step's own text states ----------------------------------------------------

def test_a_setting_is_admitted_only_when_the_text_states_its_value():
    settings, dropped = bind_settings(
        CLUSTER, CLUSTER_PROPS,
        {"resolution": 0.5, "n_pcs": 30, "method": "louvain", "invented": 1})
    assert settings == {"resolution": 0.5}
    assert {d["param"]: d["why"] for d in dropped} == {
        "n_pcs": "not_in_text", "method": "not_in_text", "invented": "unknown_param"}


def test_a_percentage_in_the_text_matches_the_fraction_or_the_percent():
    text = "keep cells under 10% mitochondrial at an adjusted p-value below 5%"
    props = {"max_pct_mt": {"type": "number"}, "padj": {"type": "number"}}
    settings, _ = bind_settings(text, props, {"max_pct_mt": 10, "padj": 0.05})
    assert settings == {"max_pct_mt": 10.0, "padj": 0.05}


def test_a_hyphen_before_a_number_is_not_a_minus_sign():
    props = {"n_top": {"type": "integer"}, "lfc": {"type": "number"}}
    settings, _ = bind_settings("keep the top-50 markers with log2FC below -1", props,
                                {"n_top": 50, "lfc": -1})
    assert settings == {"n_top": 50, "lfc": -1.0}


def test_a_value_of_the_wrong_type_or_outside_the_closed_set_is_dropped():
    _settings, dropped = bind_settings(
        "cluster with louvain at resolution high", CLUSTER_PROPS,
        {"resolution": "high", "method": "kmeans"})
    assert {d["param"]: d["why"] for d in dropped} == {"resolution": "wrong_type", "method": "not_allowed"}


def test_a_multi_library_step_does_not_bind_one_species_or_a_subset_of_samples():
    from aiscientist.tools.run_cellqc.tool import make_tool

    text = ("Run `run_cellqc` on the three Cell Ranger libraries in this delivery. "
            "Sample1_WT and Sample2_Ddx41 are mouse (GRCm39) and are merged into one QC'd "
            "mouse object; Sample3_GSM5676874 is human (GRCh38) and is QC'd in its own "
            "CellQC run and kept as a separate object ...")
    settings, dropped = bind_settings(
        text, make_tool().parameters["properties"],
        {"species": "human", "samples": ["Sample3_GSM5676874"]})
    assert settings == {}
    assert {d["param"]: d["why"] for d in dropped} == {
        "species": "multiple_choices_in_text", "samples": "incomplete_list"}


def test_a_single_library_step_still_binds_its_species_and_sample():
    from aiscientist.tools.run_cellqc.tool import make_tool

    proposed = {"species": "mouse", "samples": ["Sample2_Ddx41"]}
    text = "Run run_cellqc on the Sample2_Ddx41 Cell Ranger library, which is mouse (GRCm39)."
    assert bind_settings(text, make_tool().parameters["properties"], proposed) == (proposed, [])


@pytest.mark.parametrize("text, expected", [
    ("Use auto or mouse", {"species": "mouse"}),
    ("Use mouse with a humanized reference", {"species": "mouse"}),
    ("Use human and mouse", {}),
])
def test_closed_set_scope_ignores_defaults_and_partial_words(text, expected):
    props = {"species": {"type": "string", "default": "auto",
                         "enum": ["auto", "human", "mouse"]}}
    assert bind_settings(text, props, {"species": "mouse"})[0] == expected


def test_a_sentinel_choice_in_prose_does_not_unbind_a_closed_set():
    from aiscientist.tools.run_cellqc.tool import make_tool

    text = ("Run run_cellqc with decontX for the ambient RNA; none of the libraries is "
            "corrected with SoupX.")
    assert bind_settings(text, make_tool().parameters["properties"],
                         {"ambient_method": "decontx"}) == ({"ambient_method": "decontx"}, [])


@pytest.mark.parametrize("selected", [["WT"], ["WT", "KO"]])
def test_list_scope_uses_named_items_from_the_schema(selected):
    props = {"groups": {"type": "array", "items": {"type": "string", "enum": ["WT", "KO"]}}}
    settings, dropped = bind_settings("Compare WT and KO", props, {"groups": selected})
    assert settings == ({"groups": selected} if len(selected) == 2 else {})
    assert bool(dropped) == (len(selected) == 1)


def test_a_multi_library_step_can_bind_the_complete_sample_list():
    samples = ["Sample1_WT", "Sample2_Ddx41", "Sample3_GSM5676874"]
    props = {"samples": {"type": "array", "items": {"type": "string"}}}
    assert bind_settings("Run on " + ", ".join(samples), props,
                         {"samples": samples}) == ({"samples": samples}, [])


@pytest.mark.parametrize("proposed", [["Sample3"], ["Sample3_GSM5676874", "Sample3"],
                                      ["Sample1", "Sample2", "Sample3"]])
def test_a_sample_list_that_misses_a_named_library_is_refused_even_with_other_items(proposed):
    # "Sample3" is in the text (inside "Sample3_GSM5676874"), so it passes the stated check; it is no
    # subset of the full names either. run_cellqc matches sample ids exactly and would refuse it.
    from aiscientist.tools.run_cellqc.tool import make_tool

    text = ("Run run_cellqc on Sample1_WT and Sample2_Ddx41 (mouse) and Sample3_GSM5676874 "
            "(human).")
    settings, dropped = bind_settings(text, make_tool().parameters["properties"],
                                      {"samples": proposed})
    assert settings == {}
    assert [d["why"] for d in dropped] == ["incomplete_list"]


def test_arguments_written_as_a_call_are_read_from_the_step():
    step = 'Contrast the arms with `run_de(groupby="sampleid", reference="WT", padj=0.05)`.'
    assert call_settings(step, "run_de") == {"groupby": "sampleid", "reference": "WT", "padj": "0.05"}


# --- an edited document is checked against the tools before it is adopted ------------------------------

def test_an_edit_naming_a_setting_the_tool_does_not_take_is_a_finding():
    doc = _doc({"resolution": "1.5", "resolutionn": 2, "method": "kmeans"})
    typed, findings = check_plan(doc, SCHEMAS)
    assert typed.steps[1].settings == {"resolution": 1.5}, "a number typed as text is still that number"
    assert {(f["kind"], f["param"]) for f in findings} == {
        ("unknown_setting", "resolutionn"), ("bad_value", "method")}


def test_an_unknown_tool_is_a_finding_unless_it_is_a_known_skill():
    doc = PlanDoc("q", (PlanStep(id="s1", text="x", tool="run_magic"),))
    assert check_plan(doc, SCHEMAS)[1][0]["kind"] == "unknown_tool"
    assert check_plan(doc, SCHEMAS, is_known=lambda n: n == "run_magic")[1] == []


# --- an edit changes what was edited, and says so ------------------------------------------------------

def test_changing_a_setting_rewrites_the_one_sentence_that_states_it():
    before = _doc()
    edited = parse_plan_yaml(before.to_yaml().replace("resolution: 0.5", "resolution: 1.2"))
    after, changes, notes = apply_edit(before, edited)
    assert after.steps[1].settings == {"resolution": 1.2}
    assert "resolution 1.2" in after.steps[1].text and "0.5" not in after.steps[1].text
    assert [(c["kind"], c.get("param"), c.get("before"), c.get("after")) for c in changes] == [
        ("setting", "resolution", 0.5, 1.2)]
    assert notes == []
    assert after.steps[0] is before.steps[0] and after.steps[2] is before.steps[2], \
        "a step the editor did not touch is the same object, not an equal copy"


def test_a_number_stated_twice_is_left_alone_and_reported():
    text = "**Clustering** — Cluster with `run_clustering` at resolution 0.5; 0.5 is the lab default."
    before = PlanDoc("q", (PlanStep(id="s1", text=text, tool="run_clustering",
                                    settings={"resolution": 0.5}),))
    edited = parse_plan_yaml(before.to_yaml().replace("resolution: 0.5", "resolution: 1.0"))
    after, _changes, notes = apply_edit(before, edited)
    assert after.steps[0].text == text, "which 0.5 is the setting cannot be told; neither is rewritten"
    assert notes and notes[0]["kind"] == "text_states_old_value"


def test_rewording_the_number_out_of_the_text_while_the_setting_stays_is_reported():
    before = _doc()
    edited = parse_plan_yaml(before.to_yaml().replace("at resolution 0.5 and", "at a fine granularity and"))
    _after, changes, notes = apply_edit(before, edited)
    assert [c["kind"] for c in changes] == ["text"]
    assert notes == [{"kind": "text_dropped_setting", "step": 2, "id": "s2",
                      "param": "resolution", "value": 0.5}]


def test_removing_adding_and_reordering_steps_are_each_named():
    before = _doc()
    removed = parse_plan_yaml("steps:\n  - {id: s1, title: A, does: a}\n  - {id: s3, does: c}\n")
    _after, changes, _ = apply_edit(PlanDoc("q", (
        PlanStep(id="s1", text="**A** — a"), PlanStep(id="s2", text="b"), PlanStep(id="s3", text="c"))), removed)
    assert [(c["kind"], c["id"]) for c in changes] == [("removed", "s2")]

    swapped_yaml = before.to_yaml()
    swapped = parse_plan_yaml(swapped_yaml)
    swapped = PlanDoc(swapped.request, (swapped.steps[1], swapped.steps[0], swapped.steps[2]))
    _after, changes, _ = apply_edit(before, swapped)
    assert {(c["kind"], c["id"]) for c in changes} == {("moved", "s2"), ("moved", "s1")}

    added = parse_plan_yaml(before.to_yaml() + "  - title: Doublets\n    does: Remove doublets.\n")
    after, changes, _ = apply_edit(before, added)
    assert [(c["kind"], c["step"]) for c in changes] == [("added", 4)]
    assert after.steps[3].text == "**Doublets** — Remove doublets."


def test_changing_a_steps_tool_renames_it_in_the_sentence_the_scientist_reads():
    text = "**DE** — Contrast the arms with `run_de`, reading `run_de`'s ranking table."
    before = PlanDoc("q", (PlanStep(id="s1", text=text, tool="run_de"),))
    edited = parse_plan_yaml(before.to_yaml().replace("tool: run_de", "tool: run_pseudobulk_de"))
    after, changes, _ = apply_edit(before, edited)
    assert [(c["kind"], c["before"], c["after"]) for c in changes] == [("tool", "run_de", "run_pseudobulk_de")]
    assert after.steps[0].text == text.replace("run_de", "run_pseudobulk_de")


def test_an_edit_that_changes_nothing_reports_no_changes():
    before = _doc()
    after, changes, notes = apply_edit(before, parse_plan_yaml(before.to_yaml()))
    assert changes == [] and notes == []
    assert all(a is b for a, b in zip(after.steps, before.steps))


# --- the approved value is the value that runs -----------------------------------------------------------

def test_the_approved_value_replaces_the_one_the_model_sent():
    args, overrode = pin_args({"resolution": 0.5, "n_pcs": 30}, {"resolution": 1.0})
    assert args == {"resolution": 1.0, "n_pcs": 30}
    assert overrode == [{"param": "resolution", "approved": 1.0, "model": 0.5}]


def test_filling_in_an_argument_the_model_left_out_is_not_an_override():
    args, overrode = pin_args({}, {"resolution": 1.0})
    assert args == {"resolution": 1.0} and overrode == []
    assert pin_args({"resolution": 1}, {"resolution": 1.0})[1] == [], "1 and 1.0 are the same setting"


# --- the finished run, read back against the approved plan -------------------------------------------------

def _call(tool, ok=True, **args):
    return {"tool": tool, "args": args, "ok": ok}


def test_each_approved_step_gets_the_status_the_records_support():
    doc = _doc({"resolution": 1.0})
    executed = [
        {"step": QC, "accepted": True, "calls": [_call("inspect_dataset"), _call("run_code")]},
        {"step": CLUSTER, "accepted": True, "calls": [_call("run_clustering", resolution=0.5)]},
        {"step": "**Ambient audit** — an extra look", "accepted": True, "calls": [_call("run_code")]},
    ]
    report = check_adherence(doc, executed)
    assert [(r["step"], r["status"]) for r in report["steps"]] == [
        (1, "tool_not_called"), (2, "settings_differ"), (3, "not_run")]
    assert report["steps"][0]["detail"] == ["inspect_dataset", "run_code"]
    assert report["steps"][1]["detail"] == [{"param": "resolution", "approved": 1.0, "used": 0.5}]
    assert report["unplanned"] == ["**Ambient audit** — an extra look"]
    assert report["ok"] is False and report["as_planned"] == 0


def test_a_step_the_researcher_approved_mid_run_is_an_addition_not_unapproved_work():
    doc = _doc({"resolution": 1.0})
    extra, stray = "**Doublet check** — approved when asked", "**Ambient audit** — nobody was asked"
    executed = [
        {"step": QC, "accepted": True, "calls": [_call("run_scanpy_qc")]},
        {"step": CLUSTER, "accepted": True, "calls": [_call("run_clustering", resolution=1.0)]},
        {"step": SUMMARY, "accepted": True, "calls": []},
        {"step": extra, "accepted": True, "calls": [_call("run_code")]},
    ]
    report = check_adherence(doc, executed, approved_additions=[extra])
    assert report["added"] == [extra] and report["unplanned"] == []
    assert report["ok"] is True, "a step they said yes to does not make the run differ from the plan"
    assert "with the researcher's approval" in adherence_markdown(report)

    report = check_adherence(doc, executed + [{"step": stray, "accepted": True, "calls": []}],
                             approved_additions=[extra])
    assert report["added"] == [extra] and report["unplanned"] == [stray] and report["ok"] is False


def test_a_run_that_did_what_was_approved_is_reported_as_such():
    doc = _doc({"resolution": 1.0})
    executed = [
        {"step": QC, "accepted": True, "calls": [_call("run_scanpy_qc", min_genes=200)]},
        # A failed first attempt does not count against a step whose later call used the value.
        {"step": CLUSTER, "accepted": False, "calls": [_call("run_clustering", ok=False, resolution=1.0)]},
        {"step": CLUSTER, "accepted": True, "calls": [_call("run_clustering", resolution=1)]},
        {"step": SUMMARY, "accepted": True, "calls": [_call("finish")]},
    ]
    report = check_adherence(doc, executed)
    assert report["ok"] is True and report["as_planned"] == report["total"] == 3
    assert "3 of 3 approved steps ran as approved" in adherence_markdown(report)


def test_a_step_whose_tool_never_succeeded_and_one_the_reviewer_refused():
    doc = _doc({"resolution": 1.0})
    report = check_adherence(doc, [
        {"step": QC, "accepted": False, "calls": [_call("run_scanpy_qc")]},
        {"step": CLUSTER, "accepted": True, "calls": [_call("run_clustering", ok=False, resolution=1.0)]},
    ])
    assert [r["status"] for r in report["steps"]] == ["not_accepted", "tool_failed", "not_run"]


# --- a revision as operations on the document -----------------------------------------------------------

def _ops_doc() -> PlanDoc:
    return PlanDoc("q", (PlanStep("s1", QC, "run_scanpy_qc", {"min_genes": 200}),
                         PlanStep("s2", CLUSTER, "run_clustering", {"resolution": 0.5}),
                         PlanStep("s3", SUMMARY)))


def test_operations_are_read_from_a_reply_and_a_declined_or_unreadable_reply_is_none():
    ops = parse_plan_ops('Here you go:\n{"ops": [{"op": "remove", "step": "s3"}]}')
    assert ops == [{"op": "remove", "step": "s3"}]
    assert parse_plan_ops('[{"op": "move", "step": "s3", "after": "start"}]')[0]["op"] == "move"
    for declined in ('{"ops": []}', '{"cannot": "a new plan"}', "No.", "", '{"ops": ["remove s3"]}'):
        assert parse_plan_ops(declined) is None, declined


def test_operations_touch_only_the_steps_they_name():
    before = _ops_doc()
    edited = apply_ops(before, [
        {"op": "change", "step": "s2", "settings": {"resolution": 1.2}},
        {"op": "add", "after": "s1", "title": "Doublets", "does": "Remove doublets with `run_scrublet`."},
        {"op": "move", "step": "s3", "after": "start"},
    ])
    assert [s.id for s in edited.steps] == ["s3", "s1", "s4", "s2"]
    doc, changes, _notes = apply_edit(before, edited)
    assert doc.steps[1] is before.steps[0], "a step no operation names is the same object"
    assert "resolution 1.2" in doc.steps[3].text and doc.steps[3].settings == {"resolution": 1.2}
    assert ("added", 3) in {(c["kind"], c["step"]) for c in changes}


def test_a_step_can_be_named_by_its_number_and_a_null_setting_removes_it():
    edited = apply_ops(_ops_doc(), [{"op": "change", "step": 1, "settings": {"min_genes": None}},
                                {"op": "remove", "step": "#3"}])
    assert edited.steps[0].settings == {} and [s.id for s in edited.steps] == ["s1", "s2"]


def test_operations_that_cannot_be_applied_are_all_named_and_nothing_is_applied():
    with pytest.raises(PlanError) as err:
        apply_ops(_ops_doc(), [{"op": "remove", "step": "s9"}, {"op": "rename", "step": "s1"},
                           {"op": "add", "title": ""}, {"op": "move", "step": "s1", "after": "s7"}])
    assert len(err.value.problems) == 4
    with pytest.raises(PlanError, match="cancel the plan"):
        apply_ops(_ops_doc(), [{"op": "remove", "step": s} for s in ("s1", "s2", "s3")])
