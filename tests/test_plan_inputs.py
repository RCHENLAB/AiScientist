"""A plan is not drafted with a tool whose input the chat does not hold (prod v0.4.16: an atlas
question in a chat with no atlas, and a VCF named only in the message, were each planned, started
and failed three times at the first call)."""
from __future__ import annotations

import pytest

from aiscientist.agents import atlas_expression, mcp_bridge, plan_inputs
from aiscientist.agents.research_lab.plan_parse import _split_not_planned


@pytest.fixture
def names(monkeypatch):
    monkeypatch.delenv("AISCIENTIST_MCP_CONFIG", raising=False)
    monkeypatch.delenv("AISCIENTIST_SHARED_DATASETS_ROOT", raising=False)
    monkeypatch.delenv("AISCIENTIST_HPC_SHARED_ROOT", raising=False)
    servers = mcp_bridge.configured_servers()
    atlas = [s.planner_name(t["name"]) for s in servers if s.skill == atlas_expression.SKILL for t in s.tools]
    variant = [s.planner_name(t["name"]) for s in servers if s.name == "variant-annotation" for t in s.tools]
    other = [s.planner_name(t["name"]) for s in servers if s.name == "cellqc" for t in s.tools]
    assert atlas and variant and other
    return atlas, variant, other


def test_atlas_tools_need_an_attached_atlas(names, monkeypatch):
    atlas, variant, other = names
    monkeypatch.setenv("AISCIENTIST_ATLAS_FILES", "hrca/v1=/atlases/hrca.h5ad")
    gaps = plan_inputs.missing_inputs(atlas + other, {"dataset_path": "/uploads/u/sample_matrix.h5ad"})
    assert set(gaps) == set(atlas) and "atlas" in gaps[atlas[0]]
    assert plan_inputs.missing_inputs(atlas + other, {"hpc_primary": "/atlases/hrca.h5ad"}) == {}
    # a session copy whose source is the listed atlas
    assert plan_inputs.missing_inputs(atlas, {"dataset_path": "/sessions/s1/hrca.h5ad",
                                              "datasets": [{"path": "/atlases/hrca.h5ad"}]}) == {}


def test_variant_annotation_needs_an_attached_vcf(names):
    atlas, variant, other = names
    annotate = next(n for n in variant if not n.endswith("_job_status"))
    gaps = plan_inputs.missing_inputs(variant + other, {"dataset_path": "/uploads/u/cells.h5ad"})
    assert set(gaps) == {n for n in variant if not n.endswith("_job_status")}
    assert "VCF" in gaps[annotate]
    for held in ({"dataset_path": "/uploads/u/case.vcf.gz"}, {"datasets": [{"name": "demo.VCF"}]},
                 {"input_folders": [{"hpc": "/x/a.bcf"}]}, {"content_modality": "variants"}):
        assert plan_inputs.missing_inputs(variant, held) == {}
    assert plan_inputs.missing_inputs(other, {}) == {}       # tools with no such precondition


def test_a_step_calling_such_a_tool_is_reported_with_what_to_attach(names):
    atlas, variant, other = names
    annotate = next(n for n in variant if not n.endswith("_job_status"))
    gaps = plan_inputs.missing_inputs(variant + other, {})
    agenda = [f"**QC** — Run `{other[0]}` on the library.",
              f"**Annotate** — Annotate the variants with `{annotate}(mode=offline)`.",
              f"**Note** — `{annotate}` is mentioned twice in one plan."]
    found = plan_inputs.steps_missing_input(agenda, gaps)
    assert found == [(2, annotate), (3, annotate)]
    message = plan_inputs.refusal(found, gaps)
    assert annotate in message and "Upload the VCF" in message and "Nothing was planned or run" in message


# --- an agenda item that only says an analysis is not done ---------------------------------------

TOOLS = {"mcp_run_cellqc", "run_clustering"}


def test_a_note_that_an_analysis_is_left_out_is_not_a_step():
    steps = ["**CellQC** — Run `mcp_run_cellqc` on both libraries, removing ambient RNA and doublets.",
             "**Clustering and cell-type annotation** — Not planned here. Clustering and labelling "
             "are not part of this request.",
             "**Markers** — Skipped: the request asks for quality control only."]
    kept, left_out = _split_not_planned(steps, TOOLS)
    assert kept == steps[:1] and left_out == steps[1:]


def test_real_steps_that_mention_what_they_leave_out_are_kept():
    steps = ["**QC** — Filter cells with `mcp_run_cellqc`; doublets are not removed at this stage.",
             "**Clustering** — Not planned in the first draft, now added: cluster with `run_clustering`.",
             "**Composition** — Compare cell-type proportions; conditions not included in the design "
             "are ignored."]
    assert _split_not_planned(steps, TOOLS) == (steps, [])


def test_a_plan_of_only_such_notes_is_left_as_it_is():
    steps = ["**Clustering** — Not planned here."]
    assert _split_not_planned(steps, TOOLS) == (steps, [])


def test_the_planner_is_told_quality_control_alone_is_one_step():
    from aiscientist.agents.research_lab import prompts

    text = " ".join(v for v in vars(prompts).values() if isinstance(v, str))
    assert "QUALITY CONTROL IS A COMPLETE REQUEST ON ITS OWN" in text
    assert "AN AGENDA ITEM IS WORK THAT WILL RUN" in text


def test_a_tool_the_run_does_not_register_is_still_checked(names):
    """Prod v0.4.17: in a chat with no file, variant annotation is not among the run's tools, the
    preset pipeline's guidance names it, and the plan that calls it was shown for approval."""
    atlas, variant, other = names
    annotate = next(n for n in variant if not n.endswith("_job_status"))
    assert plan_inputs.missing_inputs(other, {}) == {}          # asked about the run's tools only
    gaps = plan_inputs.missing_inputs(None, {})
    assert annotate in gaps and set(atlas) <= set(gaps)
    step = f"**Annotation** \u2014 Run `{annotate}` on the whole VCF."
    assert plan_inputs.steps_missing_input([step], gaps) == [(1, annotate)]
    assert plan_inputs.missing_inputs(None, {"dataset_path": "/uploads/u/demo.vcf"}).get(annotate) is None
