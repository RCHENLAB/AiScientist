"""Readable plan surfaces, using the real console renderers under Node."""

import json
import shutil
import subprocess

import pytest

from test_console_plan_card_js import APP_JS, ESCAPE, _card


def _console(plan, change=None, *, action=None, messages=()):
    """Use the actual renderer, field commit, submit and history functions with an offline transport."""
    source = APP_JS.read_text(encoding="utf-8")
    blocks = [
        ("function planStepParts", "function showPlanPanel"),
        ("function planSavedMessage", "function showClarify"),
        ("async function submitPlan", "// ---- uploads"),
        ("function msgFromServer", "// The payload to persist"),
        ("function serverMsgPayload", "async function"),
    ]
    code = "\n".join(source[source.index(a):source.index(b, source.index(a))] for a, b in blocks)
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const c = {sid: 'conv-A', plan: input.plan, planDraftYaml: '', planDraftChange: null};
const out = {requests: [], persisted: []};
const state = {connectionId: 'offline', planProblems: input.plan.problems || []};
for (const field of ['planDraftYaml', 'planDraftChange']) Object.defineProperty(state, field, {get: () => c[field]});
const sandbox = {state, escapeHtml: __ESCAPE__, renderMarkdown: (s) => String(s),
  activeChat: () => c, chatVisible: () => false, savePlanDraft() {},
  notePlanDraft(text) { c.planDraftYaml = text; c.planDraftBase = c.plan.doc.yaml; },
  dropPlanDraft() { c.planDraftYaml = ''; c.planDraftChange = null; },
  finalizePlanCard() {}, releasePlanPrompt() {c.plan = null;}, updateComposerButton() {},
  pushToSession(sid, m) {out.persisted.push(m);}, toast() {}, isUnreachable: () => false,
  fetch: async (url, req) => {out.requests.push(JSON.parse(req.body)); return {ok:true, json:async()=>({status:'ok'})};}
};
vm.createContext(sandbox); vm.runInContext(input.code, sandbox);
(async () => {
  out.review = sandbox.planCardHtml(c.plan.agenda, c.plan.doc, [], {});
  if (input.change) out.error = sandbox.commitPlanChange(c, input.change);
  else if (input.action) await sandbox.submitPlan(input.action, '', '', c);
  await new Promise(setImmediate);
  const saved = sandbox.planSavedMessage(input.plan);
  out.saved = saved;
  const restored = sandbox.msgFromServer(sandbox.serverMsgPayload(saved));
  out.restored = restored;
  out.savedHtml = sandbox.planCardHtml(restored.plan.agenda, restored.plan.doc, [], {mode:'saved'});
  out.internal = sandbox.planModelContent(restored);
  out.messages = input.messages.map((m) => ({shown:sandbox.planMessageMarkdown(m), internal:sandbox.planModelContent(m)}));
  console.log(JSON.stringify(out));
})().catch((e)=>{console.error(e);process.exit(1);});
""".replace("__ESCAPE__", ESCAPE)
    result = subprocess.run([node, "-e", harness], input=json.dumps({
        "code": code, "plan": plan, "change": change, "action": action, "messages": messages,
    }), text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _plan(card):
    return {"kind": "plan", "agenda": card["agenda"], "doc": card.get("plan"),
            "problems": card.get("problems", []), "saved": True, "run": card.get("run_id")}


def test_normal_plan_surfaces_have_no_raw_document_controls_or_content():
    rendered = _card()
    for surface in ("card", "saved", "status", "mixed", "single", "plain"):
        html = rendered[surface]
        assert "Edit as YAML" not in html, surface
        assert "plan.yaml" not in html, surface
        assert "plan-yaml" not in html, surface
        assert 'data-plan="yaml"' not in html, surface
        assert "QC" in html, surface


def _no_raw(html):
    for raw in ("plan-yaml", 'data-plan="yaml"', "plan.yaml", "Edit as YAML", "```yaml"):
        assert raw not in html


def test_saved_and_legacy_history_are_readable_but_model_context_keeps_the_document():
    from aiscientist.agents.plan_doc import PlanDoc, PlanStep

    doc = PlanDoc("QC", (PlanStep("s1", "**QC** — Filter cells", "run_scanpy_qc",
                                   {"min_genes": 300, "groups": ["KO, treated", "WT"], "optional": None}),))
    plan = {"agenda": doc.agenda(), "doc": {"yaml": doc.to_yaml(), "steps": doc.to_public()}}
    legacy = "## 📋 Research plan\n\n1. **QC** — Filter cells\n\n**Approved plan document**\n\n```yaml\n" + doc.to_yaml() + "\n```"
    examples = [
        {"role": "assistant", "content": legacy},
        {"role": "assistant", "kind": "plan", "content": legacy, "plan": None},
        {"role": "user", "content": legacy},
        {"role": "assistant", "content": "Here is the YAML example you asked for:\n```yaml\nsteps: []\n```"},
    ]
    out = _console(plan, messages=examples)
    assert out["restored"]["plan"]["doc"] == plan["doc"]
    assert "min_genes: 300" in out["saved"]["content"]
    _no_raw(out["saved"]["content"])
    _no_raw(out["savedHtml"])
    assert doc.to_yaml().strip() in out["internal"]
    for message in out["messages"][:2]:
        _no_raw(message["shown"])
        assert "min_genes: 300" in message["shown"]
        assert "KO, treated, WT" in message["shown"] and "optional: none" in message["shown"]
        assert message["internal"] == legacy
    for original, message in zip(examples[2:], out["messages"][2:]):
        assert message["shown"] == original["content"]


@pytest.mark.parametrize("title", ["Search the literature", "Review corpus coverage"])
def test_plans_without_documents_keep_word_revision_and_readable_history(title):
    plan = {"kind": "plan", "agenda": [f"**{title}** — Summarise the findings."], "doc": None}
    out = _console(plan, action="approve")
    assert 'data-plan="revise"' in out["review"] and title in out["savedHtml"]
    _no_raw(out["review"])
    _no_raw(out["saved"]["content"])
    assert out["requests"][0]["action"] == "approve"
    assert out["persisted"][0]["plan"]["doc"] is None


# Reuse the real gateway worker fixture, with only model/tool execution replaced by local doubles.
from test_saved_plan_gateway import _reply, _show, lab_gateway  # noqa: E402, F401


def test_structured_edit_validation_correction_approval_execution_and_reload(lab_gateway):  # noqa: F811
    from aiscientist.gateway import app as gw, saved_plans

    env = lab_gateway
    conn = env.connection()
    rid, art, _, _ = _show(conn)
    # Natural-language revision adds an integer setting, then the structured field edits it.
    _reply(conn, "revise", conversation_id="conv-A", feedback="Set min_genes to 300")
    revised = _plan(env.cards[-1])
    bad = _console(revised, {"id": "s1", "setting": "min_genes", "value": "1.5"})
    request = bad["requests"][0]
    assert request["action"] == "edit" and "min_genes: 1.5" in request["plan_yaml"]
    _reply(conn, request.pop("action"), **{k: v for k, v in request.items() if k not in {"connection_id", "tz"}})
    rejected = _plan(env.cards[-1])
    assert rejected["doc"] == revised["doc"] and rejected["problems"]
    correction = _console(rejected, {"id": "s1", "setting": "min_genes", "value": "400"})
    assert "Your change has not been applied" in correction["review"]
    _no_raw(correction["review"])
    request = correction["requests"][0]
    _reply(conn, request.pop("action"), **{k: v for k, v in request.items() if k not in {"connection_id", "tz"}})
    accepted = _plan(env.cards[-1])
    assert accepted["doc"]["steps"][0]["settings"]["min_genes"] == 400
    assert accepted["agenda"] == revised["agenda"], "a setting absent from the prose stays on its structured field"
    # Reconnect rehydrates the same document before the person approves it.
    replay = saved_plans.build_prompt(art)
    assert replay["plan"] == accepted["doc"]
    approved = _console(_plan({**replay, "run_id": rid}), action="approve")
    assert '>400</span>' in approved["review"]
    assert approved["restored"]["plan"]["doc"] == accepted["doc"]
    assert approved["persisted"][0]["plan"]["doc"] == accepted["doc"]
    request = approved["requests"][0]
    assert request["plan_yaml"] == "", "approval uses the server-validated document"
    _reply(conn, request.pop("action"), **{k: v for k, v in request.items() if k not in {"connection_id", "tz"}})
    assert env.calls[0]["min_genes"] == 400
    assert saved_plans.load(art)["yaml"] == accepted["doc"]["yaml"]
    assert (art / "report" / "plan.yaml").read_text() == accepted["doc"]["yaml"]
    assert not gw._saved_plan_replays(conn)
    _no_raw(approved["savedHtml"])
