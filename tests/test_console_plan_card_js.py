"""The console's plan card, run under node.

It is built by plain functions in ``frontend/console/app.js`` that return HTML. This runs those
functions under node with the page's globals stubbed (like the upload cards' arithmetic in
``test_upload_pace_js.py``) and pins what a person depends on when reading them: a plan step is
shown with its title and prose whatever shape it arrives in, only the step a revision touched is
marked, and an edit of the plan document that was refused comes back with what was typed. Also:
a pipeline's hard-wrapped protocol text reads by paragraph on the System page, and the composer no
longer carries the run options or the "Pipelines & skills" picker (issue 21). Skipped where node is
not installed.
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
const api = vm.runInNewContext(src + "\n;({ planStepParts, planStepHtml, planCardHtml, planEditorHtml, planRunsOnText, planEntryHtml, planEmphasisHtml, planYamlHtml })", sandbox);
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
out.entry = api.planEntryHtml(agenda, doc, { pending: true, revised: [2] });
out.entryDirty = api.planEntryHtml(agenda, doc, { pending: true, dirty: true });
out.entryRefused = api.planEntryHtml(agenda, doc, { pending: true, problems: ["x"] });
out.entrySaved = api.planEntryHtml(agenda, doc, { status: "Approved" });
const prose = "Keep cells with 200 genes and under 5.0% mito, n=1 per arm, a 1.6x imbalance; compare KO to WT "
  + "in DDX41 with log1p, as <code>min_genes=200</code> and it&#039;s 3 cells in <a href=\"x\">link 7</a>.";
out.emphasis = api.planEmphasisHtml(prose, { min_genes: 200, max_mito_pct: 5.0, groups: ["KO", "WT"], fraction: 0.5 });
out.percent = api.planEmphasisHtml("flag if more than 50% were removed", { fraction: 0.5 });
const yaml = "# a comment\nplan: 1\nrequest: Compare <A> & B\nsteps:\n  - id: s1\n    title: QC & normalization\n"
  + "    tool: run_scanpy_qc\n    runs_on: hpc:analysis\n    settings:\n      min_genes: 200\n      groups: [KO, WT]\n"
  + "    does: >-\n      Measure quality: drop cells\n      below 200 genes.\n  - id: s2\n    title: Next  # why\n";
out.yaml = yaml;
out.yamlHtml = api.planYamlHtml(yaml);
out.withWords = api.planCardHtml(agenda, doc, [], { words: "drop <step 3> & rerun" });
out.single = api.planCardHtml([agenda[0]], { yaml: doc.yaml, steps: [steps[0]] }, []);
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
    assert '<span class="plan-setting-k">min_genes</span><span class="plan-setting-v">200</span>' in _card()["saved"]
    assert '<span class="plan-setting-k">min_genes</span><span class="plan-setting-v editable"' in card
    assert 'title="Change groups">KO, WT</span>' in card
    assert '<code class="plan-step-tool">run_clustering</code>' in card
    assert "Clustering &lt;UMAP&gt;" in card and "<UMAP>" not in card
    assert "3 steps · 3 settings passed to the tools as written" in card


def test_only_the_revised_step_is_marked():
    card = _card()["card"]
    assert card.count('class="plan-step revised') == 1 and card.count("plan-step-flag") == 1
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

def test_a_step_shows_two_lines_of_its_prose_until_it_is_opened_and_a_revised_one_opens():
    """A step is its title, tool and settings; the prose waits behind a chevron. The step a revision
    rewrote is open, because what changed is what the reviewer has to read."""
    out = _card()
    card = out["card"]
    assert card.count("data-step-more") == 3, "every step with prose has a chevron"
    assert card.count('class="plan-step revised open"') == 1 and card.count('class="plan-step"') == 2
    assert 'aria-expanded="true"' in card and card.count('aria-expanded="false"') == 2
    assert 'data-plan-expand>Expand all</button>' in card
    plain_steps = out["plain"].split('<ol class="plan-steps">', 1)[1]
    assert "plan-step open" not in plain_steps, "nothing was revised: every step starts folded"


def test_the_numbers_in_a_step_are_bold_and_the_values_the_plan_fixes_are_marked():
    html = _card()["emphasis"]
    assert '<strong class="plan-pin" title="Passed to the tool as min_genes">200</strong>' in html
    assert '<strong class="plan-pin" title="Passed to the tool as max_mito_pct">5.0%</strong>' in html
    assert '<strong class="plan-pin" title="Passed to the tool as groups">KO</strong>' in html
    assert '<strong class="plan-pin" title="Passed to the tool as groups">WT</strong>' in html
    assert '<strong class="plan-num">1</strong>' in html and '<strong class="plan-num">1.6x</strong>' in html
    assert "DDX41" in html and "log1p" in html and "<strong class=\"plan-num\">41" not in html, \
        "a digit inside a word is not a number"
    assert "<code>min_genes=200</code>" in html, "code is left as it is"
    assert "it&#039;s" in html, "an entity is never split"
    assert '<a href="x">link 7</a>' in html, "a link is left as it is"
    assert '<strong class="plan-pin" title="Passed to the tool as fraction">50%</strong>' in _card()["percent"], \
        "a percentage in the prose is the fraction the tool takes"


def test_the_plan_document_is_highlighted_without_changing_a_character():
    """The editor draws this copy under its own transparent text: one character more or less and the
    caret no longer sits where the text appears."""
    import html as html_lib
    import re

    out = _card()
    shown = out["yamlHtml"]
    assert html_lib.unescape(re.sub(r"<[^>]+>", "", shown)) == out["yaml"]
    assert '<span class="y-c"># a comment</span>' in shown
    assert '<span class="y-d">-</span> <span class="y-k">id</span><span class="y-p">:</span> <span class="y-id">s1</span>' in shown
    assert '<span class="y-title">QC &amp; normalization</span>' in shown
    assert '<span class="y-tool">run_scanpy_qc</span>' in shown
    assert '<span class="y-sk">min_genes</span><span class="y-p">:</span> <span class="y-val">200</span>' in shown
    assert '<span class="y-val">[KO, WT]</span>' in shown
    assert '<span class="y-ind">&gt;-</span>' in shown
    assert '<span class="y-prose">      Measure quality: drop cells</span>' in shown, "prose is not read as a key"
    assert '<span class="y-meta">Compare &lt;A&gt; &amp; B</span>' in shown
    assert '<span class="y-title">Next</span><span class="y-c">  # why</span>' in shown
    assert "y-val" not in shown.split("does")[1].split("s2")[1], "the next step's keys are not settings"


def test_the_editor_and_the_saved_document_show_the_highlighted_plan():
    out = _card()
    editor = out["editor"]
    assert '<pre class="plan-yaml-hl" aria-hidden="true">' in editor and "<textarea" in editor
    assert editor.count("&lt;QC&gt;") == 2, "the textarea and the copy under it hold the same text"
    assert '<pre class="plan-yaml-hl">' in out["saved"] and "<textarea" not in out["saved"]


def test_the_waiting_plan_can_be_changed_in_words_from_the_panel():
    """Beside the YAML editor, the panel takes a change in words, as the chat does: the two ways to
    change a plan are where the plan is."""
    out = _card()
    card = out["card"]
    box = card.split('<div class="plan-revise">', 1)[1]
    assert '<textarea class="plan-revise-text"' in box and 'data-plan="revise" disabled' in box, \
        "Send waits for text"
    assert card.index('class="plan-revise"') < card.index('class="plan-yaml'), "words first, then the document"
    assert "reply below" not in card, "the panel says how to change the plan itself, not where the composer is"
    assert 'class="plan-revise"' in out["plain"], "a plan without a document can still be changed in words"
    assert "drop &lt;step 3&gt; &amp; rerun</textarea>" in out["withWords"], "what was typed comes back, escaped"
    for done in (out["status"], out["saved"]):
        assert "plan-revise" not in done, "a plan that is no longer waiting cannot be changed"
    assert "reply below" in out["entry"], "the chat entry still points at the composer under it"


EDIT_HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const input = JSON.parse(process.env.PLAN_EDIT_INPUT);
const sandbox = { state: {}, escapeHtml: __ESCAPE__ };
const api = vm.runInNewContext(src + "\n;({ planYamlEdit })", sandbox);
console.log(JSON.stringify(input.changes.map((c) => api.planYamlEdit(input.yaml, c))));
""".replace("__ESCAPE__", ESCAPE)


def _edits(yaml_text: str, changes: list) -> list:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    import os

    source = APP_JS.read_text(encoding="utf-8")
    block = source[source.index("function planYamlUnquote"):source.index("// Make a change on the cards")]
    done = subprocess.run([node, "-e", EDIT_HARNESS], input=block, capture_output=True, text=True, timeout=30,
                          env={**os.environ, "PLAN_EDIT_INPUT": json.dumps({"yaml": yaml_text, "changes": changes})})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_waiting_plan_with_a_document_is_changed_on_its_cards():
    out = _card()
    card = out["card"]
    assert card.count('data-edit="title"') == 3 and card.count('data-edit="setting"') == 3
    assert 'data-edit="setting" data-key="min_genes" tabindex="0" role="button" title="Change min_genes"' in card
    assert '<li class="plan-step" data-id="s1">' in card and card.count("data-step-remove") == 3
    assert "Applied as written, without the model" in card
    for done in (out["saved"], out["status"], out["plain"]):
        assert "data-edit" not in done and "data-step-remove" not in done and "plan-edit-tip" not in done, \
            "only a waiting plan with a document is changed in place"
    assert "data-edit=\"title\"" in out["single"] and "data-step-remove" not in out["single"], \
        "the only step is not removed: to run nothing, the plan is cancelled"


def test_a_change_on_the_cards_is_one_line_of_the_document_and_the_server_reads_it_as_that_change():
    """The cards write the change into the document the server sent and send it as an edit. The
    server's own reader and apply_edit must see exactly that change and nothing else."""
    pytest.importorskip("yaml")
    from aiscientist.agents.plan_doc import PlanDoc, PlanStep, apply_edit, parse_plan_yaml

    qc = "**QC** — Keep cells with at least 200 genes and under 5.0% mitochondrial reads with `run_scanpy_qc`."
    de = "**DE: KO vs WT** — Compare KO with WT using `run_de` at p below 0.05."
    gsea = "**Enrichment** — Test the ranked genes with `run_enrichment`."
    doc = PlanDoc(request="Compare KO with WT", steps=(
        PlanStep("s1", qc, "run_scanpy_qc", {"min_genes": 200, "max_mito_pct": 5.0, "eps": 1e-05}, "hpc:analysis"),
        PlanStep("s2", de, "run_de", {"groups": ["KO", "WT"], "pval_cutoff": 0.05, "use_raw": False}, "hpc:analysis"),
        PlanStep("s3", gsea, "run_enrichment", {}, "hpc:analysis")))
    text = doc.to_yaml()
    changes = [
        {"id": "s1", "setting": "min_genes", "value": "300"},
        {"id": "s1", "setting": "max_mito_pct", "value": "10"},
        {"id": "s1", "setting": "eps", "value": "1e-7"},
        {"id": "s2", "setting": "groups", "value": "KO, WT, HET"},
        {"id": "s2", "setting": "use_raw", "value": "TRUE"},
        {"id": "s2", "title": 'DE: "KO" vs WT # per cell type'},
        {"id": "s3", "remove": True},
        {"id": "s1", "setting": "min_genes", "value": "lots"},
        {"id": "s2", "setting": "use_raw", "value": "maybe"},
        {"id": "s9", "remove": True},
        {"id": "s1", "setting": "not_there", "value": "1"},
        {"id": "s1", "title": "   "},
    ]
    out = _edits(text, changes)
    expected = [
        [("setting", "min_genes", 300)],
        [("setting", "max_mito_pct", 10.0)],
        [("setting", "eps", 1e-07)],
        [("setting", "groups", ["KO", "WT", "HET"])],
        [("setting", "use_raw", True)],
        [("title", None, 'DE: "KO" vs WT # per cell type')],
        [("removed", None, None)],
    ]
    for res, want in zip(out[:7], expected):
        assert "yaml" in res, res
        before, after = text.splitlines(), res["yaml"].splitlines()
        if want[0][0] == "removed":
            assert after == before[:len(after)] and "id: s3" not in res["yaml"], "only the step's lines go"
        else:
            assert sum(a != b for a, b in zip(before, after)) == 1 and len(before) == len(after), "one line changes"
        _new, got, _notes = apply_edit(doc, parse_plan_yaml(res["yaml"], request=doc.request))
        assert [(c["kind"], c.get("param"), c.get("after") if c["kind"] != "removed" else None)
                for c in got] == want
        if want[0][1] == "max_mito_pct":
            assert isinstance(parse_plan_yaml(res["yaml"]).steps[0].settings["max_mito_pct"], float), \
                "a float stays a float"
    assert [r.get("error") for r in out[7:]] == [
        "A number is expected.", "true or false is expected.", "This step is no longer in the plan document.",
        "not_there is not where the plan document keeps it: change it in the YAML editor.", "A step needs a title."]


# --- a pipeline's protocol text, as the System page shows it ---------------------------------------

UNWRAP_HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const api = vm.runInNewContext(src + "\n;({ methodUnwrap, METHOD_DATA_TYPES })", {});
const out = { types: api.METHOD_DATA_TYPES };
out.unwrap = api.methodUnwrap("One paragraph that the file\nwraps at ninety columns.\n\n1. first item that\n   continues here\n2. second\n\n| a | b |\n| c | d |\n```\nkeep\nthese\n```");
console.log(JSON.stringify(out));
"""


def test_a_hard_wrapped_protocol_is_rejoined_by_paragraph_and_list_item():
    out = _node(UNWRAP_HARNESS, "const METHOD_DATA_TYPES", "// The composer's placeholder, unless a plan is waiting")
    assert out["types"] == {"scrna": "scRNA-seq", "variants": "Variants (VCF)"}
    assert out["unwrap"].split("\n") == [
        "One paragraph that the file wraps at ninety columns.", "",
        "1. first item that continues here", "2. second", "",
        "| a | b |", "| c | d |", "```", "keep", "these", "```"]


def test_the_composer_has_no_run_options_and_no_pipelines_and_skills_picker():
    """Every request runs with the same options (issue 21): nothing to choose, nothing to pick."""
    page = (APP_JS.parent / "index.html").read_text(encoding="utf-8")
    for gone in ('id="modeSelect"', 'id="methodsBtn"', 'id="methodChips"', 'id="methodsModal"',
                 'id="planMode"', 'id="autonomousMode"', 'id="saveResults"',
                 'id="advancedPanel"', 'id="presetList"'):
        assert gone not in page
    source = APP_JS.read_text(encoding="utf-8")
    for gone in ('$("modeSelect")', '$("planMode")', '$("autonomousMode")', '$("saveResults")', '$("methodsBtn")'):
        assert gone not in source
    assert "...FIXED_RUN_OPTIONS" in source, "the run is sent with the fixed options"
