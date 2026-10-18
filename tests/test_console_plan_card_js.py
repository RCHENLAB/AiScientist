"""The console's plan card and its "Pipelines & skills" picker, run under node.

Both are built by plain functions in ``frontend/console/app.js`` that return HTML. This runs those
functions under node with the page's globals stubbed (like the upload cards' arithmetic in
``test_upload_pace_js.py``) and pins what a person depends on when reading them: a plan step is
shown with its title and prose whatever shape it arrives in, only the step a revision touched is
marked, an edit of the plan document that was refused comes back with what was typed, and a
pipeline or a skill is shown whole — its description, every tool, its protocol — instead of in a
clipped list. Skipped where node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"


# --- the console's plan card, run under node ----------------------------------------------------------

ESCAPE = r'(s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")'

CARD_HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const escapeHtml = __ESCAPE__;
const sandbox = { state: { planDoc: null, planProblems: [], planDraftYaml: "" }, escapeHtml,
  renderMarkdown: (s) => escapeHtml(s).replace(/`([^`]+)`/g, "<code>$1</code>") };
const api = vm.runInNewContext(src + "\n;({ planStepParts, planStepHtml, planCardHtml, planEditorHtml, planRunsOnText, planEntryHtml })", sandbox);
const agenda = [
  "**QC & normalization** — Filter cells with `run_scanpy_qc` below 200 genes.",
  "2. **Clustering <UMAP>** — Cluster at resolution 1.2.",
  "Summarise shared versus class-specific changes. Then stop.",
];
const steps = [
  { id: "s1", tool: "run_scanpy_qc", runs_on: "hpc:analysis", settings: { min_genes: 200, groups: ["KO", "WT"] } },
  { id: "s2", tool: "run_clustering", runs_on: "hpc:analysis", settings: { resolution: 1.2 } },
  { id: "s3" },
];
const doc = { yaml: "plan: 1\nsteps:\n  - id: s1\n    title: <QC>\n", steps };
const out = {};
out.parts = agenda.map(api.planStepParts);
out.where = ["hpc:analysis", "gpu:scgpt", "inprocess", ""].map(api.planRunsOnText);
out.plain = api.planCardHtml(agenda, null, []);
out.card = api.planCardHtml(agenda, doc, [2]);
const mixed = { yaml: doc.yaml, steps: [steps[0], { ...steps[1], runs_on: "gpu:scgpt" }, steps[2]] };
out.mixed = api.planCardHtml(agenda, mixed, []);
out.editor = api.planEditorHtml(doc);
sandbox.state.planProblems = ["step 1: bad setting"];
sandbox.state.planDraftYaml = "plan: 1\nsteps:\n  - id: s1\n    title: MY EDIT\n";
out.refused = api.planEditorHtml(doc);
sandbox.state.planProblems = [];
sandbox.state.planDraftYaml = "";
out.status = api.planCardHtml(agenda, doc, [], { mode: "status", status: "✎ Applying your edit…" });
out.saved = api.planCardHtml(agenda, doc, [], { mode: "saved", status: "Approved" });
out.expiring = api.planCardHtml(agenda, doc, [], { expiry: "This plan expires at 9:30 PM — run it, change it or reply before then, or nothing will run." });
out.entry = api.planEntryHtml(agenda, doc, { pending: true, revised: [2] });
out.entryDirty = api.planEntryHtml(agenda, doc, { pending: true, dirty: true });
out.entryRefused = api.planEntryHtml(agenda, doc, { pending: true, problems: ["x"] });
out.entrySaved = api.planEntryHtml(agenda, doc, { status: "Approved" });
console.log(JSON.stringify(out));
""".replace("__ESCAPE__", ESCAPE)


def _node(harness: str, start: str, end: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    block = source[source.index(start):source.index(end)]
    done = subprocess.run([node, "-e", harness], input=block, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _card() -> dict:
    return _node(CARD_HARNESS, "function planStepParts", "function showPlanPanel")


def test_a_step_is_split_into_a_title_and_its_prose_whatever_shape_it_arrives_in():
    parts = _card()["parts"]
    assert parts[0]["title"] == "QC & normalization"
    assert parts[1]["title"] == "Clustering <UMAP>", "a step that echoes its own ordinal still has a title"
    assert parts[2] == {"title": "Summarise shared versus class-specific changes", "body": "Then stop."}


def test_the_card_shows_what_each_step_fixes_and_escapes_what_the_plan_says():
    card = _card()["card"]
    assert '<span class="plan-setting-k">min_genes</span><span class="plan-setting-v">200</span>' in card
    assert '<span class="plan-setting-v">KO, WT</span>' in card
    assert '<code class="plan-step-tool">run_clustering</code>' in card
    assert "Clustering &lt;UMAP&gt;" in card and "<UMAP>" not in card
    assert "3 steps · 3 settings passed to the tools as written" in card


def test_only_the_revised_step_is_marked():
    card = _card()["card"]
    assert card.count('class="plan-step revised"') == 1 and card.count("plan-step-flag") == 1
    assert "Step 2 was revised — every other step is unchanged." in card


def test_where_the_steps_run_is_said_once_unless_the_plan_mixes_places():
    out = _card()
    assert out["where"] == ["Slurm job on HPC3", "GPU job on HPC3", "Runs on the AiScientist server", ""]
    assert "runs as Slurm jobs on HPC3" in out["card"] and "plan-where" not in out["card"]
    assert out["mixed"].count('class="plan-where"') == 2 and "runs as" not in out["mixed"]


def test_without_a_document_the_card_is_the_steps_alone():
    plain = _card()["plain"]
    assert "plan-setting" not in plain and "plan-yaml" not in plain and 'data-plan="yaml"' not in plain
    assert 'data-plan="approve"' in plain and 'data-plan="cancel"' in plain
    assert '<div class="plan-sub">3 steps</div>' in plain


def test_the_editor_starts_closed_and_escapes_the_document():
    editor = _card()["editor"]
    assert editor.startswith('<div class="plan-yaml hidden">'), "closed until the person opens it"
    assert "&lt;QC&gt;" in editor and "<QC>" not in editor


def test_a_refused_edit_reopens_with_what_was_typed_and_the_reasons():
    refused = _card()["refused"]
    assert refused.startswith('<div class="plan-yaml">')
    assert "MY EDIT" in refused and "&lt;QC&gt;" not in refused
    assert "step 1: bad setting" in refused


def test_a_resolved_card_loses_its_editor_with_its_buttons():
    """Left in place, "Apply edits" stays clickable on a plan that is no longer waiting."""
    source = APP_JS.read_text(encoding="utf-8")
    finalize = source[source.index("function finalizePlanCard"):source.index("function setPlanStatus")]
    assert 'querySelector(".plan-yaml")' in finalize and "editor.remove()" in finalize



def test_a_plan_no_longer_waiting_has_no_editor_and_no_buttons():
    """The side panel shows a plan the PI has taken back, or one this chat approved: to read."""
    out = _card()
    for card in (out["status"], out["saved"]):
        assert "<textarea" not in card and 'data-plan="edit"' not in card and 'data-plan="approve"' not in card
    assert "✎ Applying your edit…" in out["status"]
    assert '<details class="plan-yaml plan-yaml-read">' in out["saved"] and "&lt;QC&gt;" in out["saved"], \
        "a saved plan keeps its document, to read"
    assert '<div class="plan-title">Plan</div>' in out["saved"]


def test_the_waiting_plan_in_the_panel_says_when_it_expires():
    row = _card()["expiring"].split('<div class="plan-actions-row">', 1)[1]
    assert row.startswith('<div class="plan-expiry" role="status">') and "This plan expires at 9:30 PM" in row


def test_the_chat_shows_the_plan_as_an_entry_titles_and_tools_with_the_decision():
    out = _card()
    entry = out["entry"]
    assert entry.count('<li class="plan-entry-step') == 3 and "Clustering &lt;UMAP&gt;" in entry
    assert "below 200 genes" not in entry, "the prose is in the side panel, not in the chat"
    assert '<code class="plan-step-tool">run_scanpy_qc</code>' in entry
    assert [b in entry for b in ('data-plan="approve"', 'data-plan="open"', 'data-plan="cancel"')] == [True] * 3
    assert entry.count("plan-entry-step revised") == 1 and "Step 2 was revised" in entry
    assert 'data-plan="approve" disabled' in out["entryDirty"], "Run waits while the document holds an unapplied edit"
    assert "plan-note bad" in out["entryRefused"]
    saved = out["entrySaved"]
    assert 'data-plan="approve"' not in saved and "View plan" in saved and "Approved" in saved

# --- the pipelines & skills picker, run under node -----------------------------------------------------

PICKER_HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const session = { presetKeys: ["differential_expression"], skillKeys: ["condition_by_celltype"] };
const label = "Compare an experimental condition vs control (e.g. KO vs WT) per cell type, with pathway interpretation (v2 — pseudobulk when replicates exist, and honest about it when they don't)";
const sandbox = { escapeHtml: __ESCAPE__, activeSession: () => session, state: {
  presetOrder: ["celltype_annotation", "differential_expression", "variant_annotation"],
  presets: {
    celltype_annotation: { key: "celltype_annotation", label: "Single-cell **cell-type** annotation", data_type: "scrna", tools: ["run_scanpy_qc", "run_clustering"], prompt: "p" },
    differential_expression: { key: "differential_expression", label, data_type: "scrna", prompt: "p",
      tools: ["run_scanpy_qc", "run_cellqc", "run_clustering", "run_de", "run_pseudobulk_de", "run_depth_matched_de", "run_composition", "run_enrichment", "run_gsea_prerank", "deep_literature", "run_code"] },
    variant_annotation: { key: "variant_annotation", label: "Interpret a VCF's variants", data_type: "variants", tools: ["annotate_variants"] },
  },
  skillOrder: ["condition_by_celltype", "crossvalidate_scgpt_vs_leiden"],
  skills: {
    condition_by_celltype: { name: "condition_by_celltype", summary: "Reference template — condition-vs-control differential expression, STRATIFIED BY CELL TYPE." },
    crossvalidate_scgpt_vs_leiden: { name: "crossvalidate_scgpt_vs_leiden", summary: "Cross-validate scGPT labels <against> structure." },
  } } };
const api = vm.runInNewContext(src + "\n;({ methodEntries, methodRowsHtml, methodSummaryText, methodChipsHtml, methodPicks, methodUnwrap })", sandbox);
const picks = api.methodPicks();
const out = { label };
out.pipelines = api.methodRowsHtml("pipelines", "", picks);
out.skills = api.methodRowsHtml("skills", "", picks);
out.by_tool = api.methodEntries("pipelines", "annotate_variants").map((e) => e.key);
out.by_text = api.methodEntries("skills", "scgpt").map((e) => e.key);
out.none = api.methodRowsHtml("pipelines", "zzzz", picks);
out.summary = [api.methodSummaryText(picks), api.methodSummaryText({ presets: new Set(), skills: new Set() })];
out.chips = api.methodChipsHtml(picks);
out.no_chips = api.methodChipsHtml({ presets: new Set(), skills: new Set() });
out.unwrap = api.methodUnwrap("One paragraph that the file\nwraps at ninety columns.\n\n1. first item that\n   continues here\n2. second\n\n| a | b |\n| c | d |\n```\nkeep\nthese\n```");
console.log(JSON.stringify(out));
""".replace("__ESCAPE__", ESCAPE)


def _picker() -> dict:
    return _node(PICKER_HARNESS, "const METHOD_DATA_TYPES", "// Paint everything that shows the active chat's picks")


def test_a_pipeline_is_shown_whole_its_description_and_every_tool_it_composes():
    """The list this replaces cut both off: a 176 px box of one-line labels."""
    out = _picker()
    rows = out["pipelines"]
    assert out["label"] in rows, "the description is not shortened"
    assert rows.count('class="method-tool"') == 2 + 11 + 1, "every tool of every pipeline"
    assert "Single-cell cell-type annotation" in rows and "**" not in rows
    assert '<span class="method-badge">scRNA-seq</span>' in rows and "Variants (VCF)" in rows


def test_what_this_chat_requires_is_ticked_and_nothing_else():
    out = _picker()
    assert out["pipelines"].count('class="method-row checked"') == 1
    assert 'class="method-row checked" data-kind="preset" data-key="differential_expression"' in out["pipelines"]
    assert 'class="method-row checked" data-kind="skill" data-key="condition_by_celltype"' in out["skills"]


def test_a_skill_shows_its_summary_not_only_its_name():
    skills = _picker()["skills"]
    assert "STRATIFIED BY CELL TYPE." in skills
    assert "&lt;against&gt;" in skills and "<against>" not in skills


def test_only_a_pipeline_with_a_protocol_offers_to_show_it():
    assert _picker()["pipelines"].count("Read the protocol") == 2


def test_the_search_reads_tools_and_descriptions():
    out = _picker()
    assert out["by_tool"] == ["variant_annotation"]
    assert out["by_text"] == ["crossvalidate_scgpt_vs_leiden"]
    assert "Nothing matches" in out["none"]


def test_the_composer_names_what_is_required_and_is_empty_when_nothing_is():
    out = _picker()
    assert out["summary"][0] == "1 pipeline and 1 skill required in this chat."
    assert out["summary"][1].startswith("Nothing required")
    assert 'data-kind="preset" data-key="differential_expression"' in out["chips"]
    assert 'data-kind="skill" data-key="condition_by_celltype"' in out["chips"]
    assert out["no_chips"] == ""


def test_a_hard_wrapped_protocol_is_rejoined_by_paragraph_and_list_item():
    assert _picker()["unwrap"].split("\n") == [
        "One paragraph that the file wraps at ninety columns.", "",
        "1. first item that continues here", "2. second", "",
        "| a | b |", "| c | d |", "```", "keep", "these", "```"]


def test_the_page_no_longer_carries_the_inline_advanced_panel():
    page = (APP_JS.parent / "index.html").read_text(encoding="utf-8")
    assert 'id="advancedPanel"' not in page and 'id="presetList"' not in page
    for needed in ('id="methodsBtn"', 'id="methodChips"', 'id="methodsModal"', 'id="methodsList"'):
        assert needed in page
