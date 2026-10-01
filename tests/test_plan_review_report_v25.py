"""The plan-mode defects Ziyao measured in ``plan_mode_report_v2_5``, each pinned to the
production string that produced it.

The report tested build ``107825cd`` against 8 first-draft plans, 10 revisions, 9 questions and 3
ordered combinations on ``Ddx41_DEG.h5ad``. Every case below is one of its observations, not a
hypothetical: the point of a regression test for a UX defect is that the NEXT redraft of this code
cannot quietly reintroduce a failure that took a human two days to characterise.
"""

from __future__ import annotations

import json

import pytest

from bioagent.agents.research_harness import (
    HarnessContext,
    ResearchHarness,
    default_catalog,
)
from bioagent.agents.research_lab import (
    LabConfig,
    ResearchLab,
    _strip_step_ordinal,
    check_plan_tooling,
    diff_plans,
    is_noise_reply,
    resolve_plan_reference,
    stale_downstream_settings,
)
from bioagent.agents.registry import build_scientist_catalog


# The plan the report ran nearly everything against: a real production draft, verbatim in shape.
BASE_PLAN = [
    "**QC & normalization** — Assess each cell's quality with `run_scanpy_qc`, filtering at "
    "percent.mt > 10 and keeping the top 2000 highly-variable genes.",
    "**Differential expression per majorclass** — Contrast DDX41 versus WT within each cell type "
    'using `run_de(stratify_by="majorclass", reference="WT", padj=0.05)`.',
    "**Pathway enrichment** — Test the significant genes with `run_enrichment(padj=0.05)` against "
    "the measured universe.",
    "**Coordinated pathway shifts (GSEA)** — Walk the full ranking with `run_gsea_prerank`.",
]


# --------------------------------------------------------------------------- B-1
# "改动提示渲染为裸数字" — the bare number, 6/6 on the in-place-revision path.

def test_a_patched_step_does_not_keep_the_ordinal_the_model_echoed_back():
    """The patch prompt shows the plan NUMBERED, so the model returned '1. **QC …** — …'. Stored
    unchanged, that step no longer starts with its bold title, and the console's renderer — which
    reads the title off a leading `**` — fell through to a bare '1' heading over raw markdown."""
    assert _strip_step_ordinal("1. **QC & normalization** — Assess each cell's quality.") == \
        "**QC & normalization** — Assess each cell's quality."
    assert _strip_step_ordinal("(3) **Pathways** — Test the significant set.") == \
        "**Pathways** — Test the significant set."
    assert _strip_step_ordinal("10) **Synthesis** — Combine.") == "**Synthesis** — Combine."


@pytest.mark.parametrize("text", [
    "**QC & normalization** — keep the top 2,000 highly-variable genes.",
    "0.25 is the single-cell convention for |log2FC|.",
    "2000 highly-variable genes are selected for the embedding.",
    "Step 3: contrast the arms.",          # content, not a list marker — the model owns it
])
def test_stripping_the_ordinal_leaves_real_prose_alone(text):
    """A number that is CONTENT must survive: the separator and a following space are both
    required, so a threshold or a gene count at the start of a step is never eaten."""
    assert _strip_step_ordinal(text) == text


# --------------------------------------------------------------------------- B-3
# "计划中出现未注册的工具名与非法参数值" — in a revision AND in a plan nobody had edited.

def test_the_three_production_tooling_defects_are_caught_before_approval():
    catalog = build_scientist_catalog(code_executor=None)
    agenda = [
        # From a C-group baseline plan that had NEVER been revised.
        "Compare DDX41 versus WT within each majorclass using `run_scanpy_de` (Wilcoxon).",
        # From B7, the in-place revision path.
        'Aggregate with run_pseudobulk(groupby="sampleid", groupby_col="majorclass") then '
        'compare with run_de(groupby="sampleid", reference="WT", method="DESeq2").',
    ]
    fixed, findings = check_plan_tooling(agenda, catalog)
    by_kind = {(f["kind"], f["tool"]): f["detail"] for f in findings}

    # Names with exactly one obvious referent are corrected, and the correction is announced.
    assert by_kind[("renamed", "run_scanpy_de")] == "run_de"
    assert by_kind[("renamed", "run_pseudobulk")] == "run_pseudobulk_de"
    assert "`run_de`" in fixed[0] and "run_scanpy_de" not in fixed[0]

    # An invented argument name is reported against the tool that does not declare it.
    assert ("unknown_param", "run_pseudobulk_de") in by_kind

    # A value outside the closed set — scanpy has no DESeq2 backend, so this could only fail at
    # run time, after the reviewer had committed the compute.
    assert "DESeq2" in by_kind[("bad_value", "run_de")]
    assert "wilcoxon" in by_kind[("bad_value", "run_de")]


def test_a_correct_plan_produces_no_tooling_findings():
    """The check must be silent on a good plan, or its warnings stop being read."""
    _, findings = check_plan_tooling(BASE_PLAN, build_scientist_catalog(code_executor=None))
    assert findings == []


def test_an_empty_catalog_judges_nothing():
    """A caller with no registry cannot tell an invented tool from a real one, and inventing
    findings from an unknown catalog would be worse than silence."""
    assert check_plan_tooling(["Use `run_whatever`"], []) == (["Use `run_whatever`"], [])


# --------------------------------------------------------------------------- B-5 / B-6
# "引用不存在的对象时静默补全" and "指代歧义按最大范围处理".

def test_changing_a_step_the_plan_does_not_have_is_a_question_not_a_construction():
    """B2: 'use resolution 0.5 for the clustering step' on a plan with no clustering step
    conjured one — with an n_pcs and an n_neighbors the user never mentioned, consumed by no
    later step. Asked as a QUESTION, the same system correctly said the plan does not cluster."""
    verdict, topic, _ = resolve_plan_reference(
        "use resolution 0.5 for the clustering step", BASE_PLAN)
    assert (verdict, topic) == ("missing", "clustering")


def test_naming_one_of_two_matching_steps_asks_which():
    """B6: 'drop the enrichment step' — singular — deleted BOTH the ORA and the GSEA step."""
    verdict, topic, steps = resolve_plan_reference("drop the enrichment step", BASE_PLAN)
    assert verdict == "ambiguous"
    assert topic == "pathway enrichment"
    assert steps == [3, 4], "both enrichment-family steps are candidates"


@pytest.mark.parametrize("request_text", [
    "add a doublet detection step after QC",         # B5: an ADD may name what is not there yet
    "add a doublet removal step before QC",
    "stratify the DE step by majorclass",            # B4: one DE step; step 3 only READS its table
    "change the mitochondrial threshold in step 1 to 5%",
    "use pseudobulk DE instead of the current DE approach",
    "could you use padj 0.01 instead?",              # names no step in particular
    "also run GSEA and add a literature review step at the end",
])
def test_ordinary_change_requests_are_not_interrupted(request_text):
    """A clarification nobody needed costs an exchange; these all have one honest reading."""
    assert resolve_plan_reference(request_text, BASE_PLAN)[0] == "ok"


# --------------------------------------------------------------------------- B-4
# "整体重画路径静默丢失内容" — including a significance threshold moved by the word "hmm".

@pytest.mark.parametrize("text", ["hmm", "  ", "ok", "嗯", "?", "thanks", "..."])
def test_a_reply_that_asks_for_nothing_is_recognised(text):
    assert is_noise_reply(text)


@pytest.mark.parametrize("text", [
    "ok, drop the GSEA step",           # starts with an interjection, IS an instruction
    "no, use padj 0.01",
    "use resolution 0.5",
    "为什么第3步用 Wilcoxon?",
])
def test_a_real_instruction_is_never_mistaken_for_noise(text):
    assert not is_noise_reply(text)


def test_a_redraft_reports_the_steps_and_numbers_it_changed_on_its_own():
    """The three production redrafts dropped steps nobody mentioned and moved an ORA threshold
    from 0.05 to 0.01 unasked — none of it reported anywhere in the console."""
    after = [
        BASE_PLAN[0],
        BASE_PLAN[1],
        # same step, reworded, with padj quietly moved
        "**Pathway enrichment** — Test the significant genes with `run_enrichment(padj=0.01)` "
        "against the measured universe.",
        # the GSEA step is gone
    ]
    delta = diff_plans(BASE_PLAN, after)
    assert delta["dropped"] == [BASE_PLAN[3]]
    assert delta["added"] == []
    assert [(c["param"], c["before"], c["after"]) for c in delta["changed"]] == [("padj", "0.05", "0.01")]


def test_an_untouched_plan_diffs_to_nothing():
    assert diff_plans(BASE_PLAN, list(BASE_PLAN)) == {"dropped": [], "added": [], "changed": []}


# --------------------------------------------------------------------------- B-7
# "参数改动不向下游传播" — the DE threshold moved, the step reading its output did not.

def test_a_changed_threshold_flags_the_downstream_step_still_using_the_old_one():
    """B3: asked for padj 0.01, the revision changed step 2 and left step 3 saying it filters at
    padj=0.05 — while reading a table that by then held only genes at padj<0.01."""
    revised = list(BASE_PLAN)
    revised[1] = revised[1].replace("padj=0.05", "padj=0.01")
    assert stale_downstream_settings(revised, 1, "padj", "0.05", "0.01") == [3]


def test_nothing_is_flagged_when_the_downstream_step_agrees():
    revised = [s.replace("padj=0.05", "padj=0.01") for s in BASE_PLAN]
    assert stale_downstream_settings(revised, 1, "padj", "0.05", "0.01") == []


def test_a_step_ABOVE_the_change_is_never_flagged():
    """Only a CONSUMER of the changed step's output can be made stale by it."""
    assert stale_downstream_settings(BASE_PLAN, 2, "padj", "0.05", "0.01") == []


# --------------------------------------------------------------------------- C-3
# "超时被错误归因为用户操作" — two of three end-of-run messages blamed a user who clicked nothing.

def _lab_with_plan(plan, complete=None):
    def _complete(messages):
        sys_prompt = messages[0]["content"]
        if "reviewing a DRAFT analysis plan" in sys_prompt:
            return json.dumps({"issues": [], "revised_agenda": []})
        if "finalizing the analysis plan" in sys_prompt:
            return json.dumps({"final_agenda": list(plan)})
        if "Principal Investigator of a bioinformatics lab" in sys_prompt:
            return json.dumps({"agenda": list(plan)})
        return "FINAL REPORT: done."

    return ResearchLab(
        HarnessContext(decisions={}, tunnel_port=1, model="m"), LabConfig(),
        complete_fn=complete or _complete,
        scientist=ResearchHarness(
            catalog=default_catalog(),
            chat_fn=lambda *_a: {"content": "", "tool_calls": [
                {"id": "f", "type": "function",
                 "function": {"name": "finish", "arguments": '{"answer": "ok"}'}}]}))


def test_an_expired_review_does_not_say_the_user_cancelled_it():
    """The review window closing and a person clicking Cancel are different events. Reporting the
    first as the second told a reviewer they had stopped their own run, and left them with no way
    to tell a system limit from their own mistake — or to report it as a defect."""
    lab = _lab_with_plan(BASE_PLAN)
    events: list[dict] = []
    result = lab.run("q", on_event=events.append,
                     plan_review=lambda kind, payload: {"action": "timeout"})

    assert "expired" in result.final_answer
    assert "by the user" not in result.final_answer
    assert "Nobody cancelled" in result.final_answer
    assert [e for e in events if e["type"] == "plan_cancelled"][0]["reason"] == "timeout"


def test_a_real_cancel_still_reads_as_a_cancel():
    lab = _lab_with_plan(BASE_PLAN)
    events: list[dict] = []
    result = lab.run("q", on_event=events.append,
                     plan_review=lambda kind, payload: {"action": "cancel"})

    assert "cancelled by the user" in result.final_answer
    assert [e for e in events if e["type"] == "plan_cancelled"][0]["reason"] == "user"


# --------------------------------------------------------------------------- end-to-end
# The review loop's behaviour, driven through the real plan_review callback.

def test_a_noise_reply_re_presents_the_plan_instead_of_redrafting_it():
    """'hmm' (checklist item B8) used to reach the PI as a revision; the plan came back with the
    ORA step's padj changed. Now it is answered, and the plan is handed back untouched."""
    replies = iter([{"action": "revise", "feedback": "hmm"}, {"action": "approve"}])
    lab = _lab_with_plan(BASE_PLAN)
    events: list[dict] = []
    result = lab.run("q", on_event=events.append, plan_review=lambda *_a: next(replies))

    assert [e for e in events if e["type"] == "plan_no_request"], "the empty request is named"
    assert list(result.agenda) == BASE_PLAN, "and the plan is byte-identical"


def test_a_change_naming_an_absent_step_re_presents_the_plan_instead_of_building_one():
    replies = iter([{"action": "revise", "feedback": "use resolution 0.5 for the clustering step"},
                    {"action": "approve"}])
    lab = _lab_with_plan(BASE_PLAN)
    events: list[dict] = []
    result = lab.run("q", on_event=events.append, plan_review=lambda *_a: next(replies))

    asked = [e for e in events if e["type"] == "plan_reference"]
    assert asked and asked[0]["verdict"] == "missing" and asked[0]["topic"] == "clustering"
    assert list(result.agenda) == BASE_PLAN


# --------------------------------------------------------------------------- live-run findings
# Not from the report: found by running the real Qwen3.6-35B-A3B-AWQ server end-to-end on
# Ddx41_DEG.h5ad (2026-08-19, plan+execution, one round, Critic off).

def test_a_packaging_step_whose_verb_is_innocent_is_still_pruned():
    """Live run 2, step 8 of 8. The busywork guard keys on a VERB, and this plan opened with
    "Generate" — which is also how a real analysis step opens — so nothing fired. It then ran 8
    run_code turns over 54 s and wrote two summary CSVs, while every figure it promised was
    already on disk: `run_de` writes the volcanoes and `run_enrichment` writes the bar plots. The
    reliable signal is the stated PURPOSE — no genuine analysis step exists in order to be put in
    the report."""
    from bioagent.agents.research_lab import _is_report_busywork

    step = ("**Figures & tables** — Generate the visual summaries required for the report using "
            "`run_code`. The script will read the DE and enrichment tables to produce: (1) a "
            "per-cell-type summary table of significant gene counts and top markers; (2) volcano "
            "plots per major cell class; (3) heatmaps displaying the top shared genes; and (4) "
            "enrichment bar plots showing the top pathways per cell type. All figures will be "
            "saved to `figures/` and tables to `tables/` for automatic inclusion in the final "
            "PDF+DOCX report.")
    assert _is_report_busywork(step)


@pytest.mark.parametrize("step", [
    # Every analysis step from the two live plans that mentions figures, tables or reporting.
    "**Clustering & UMAP** — Group the cells into transcriptionally distinct populations with "
    "`run_clustering` and lay them out on a 2-D map, so the major cell types present in the "
    "tissue can be seen, separated, and counted.",
    "**Cell-type composition shift** — Assess whether the relative abundance of each major cell "
    "class differs between conditions with `run_composition`, computing the proportion of cells "
    "assigned to each `majorclass` level. The tool will report the observed proportions and "
    "centered-log-ratio (CLR) values but will not perform a formal statistical test.",
    "**Cross-cell-type synthesis** — Identify a shared pan-tissue transcriptional signature by "
    "running `run_code` to extract the DE tables across all tested cell types. Output the shared "
    "gene lists to `tables/shared_deg_up.csv` and `tables/shared_deg_down.csv`.",
    "**Exploratory expression ranking per major cell type** — Rank genes by their differential "
    "expression using `run_de` in contrast mode. Report the gene rankings, direction, and effect "
    "sizes for the remaining major cell types.",
])
def test_real_analysis_steps_that_mention_figures_or_tables_survive(step):
    """A guard that eats analysis is worse than one that misses busywork: the third of these
    writes CSVs on purpose, and it is a real synthesis step."""
    from bioagent.agents.research_lab import _is_report_busywork

    assert not _is_report_busywork(step)
