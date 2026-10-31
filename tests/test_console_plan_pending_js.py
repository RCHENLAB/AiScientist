"""The console's waiting prompt (a plan, a clarify question, a decision point), run under node.

A prompt waits in ONE chat, the one that owns the run, and its card is DOM that ``renderChat()``
throws away on every switch of chats. ``frontend/console/app.js`` therefore holds the prompt in that
chat's record (``chatOf(sid).plan``; ``state.planPrompt`` is the chat on screen's) until it is
resolved and draws the card from it. Several chats may each have one waiting. This runs the functions
involved (the WebSocket handler, the prompt section, ``renderChat``, ``renderSessionList``, the
field-edit feedback HTML, the submit and run-end paths) under node with the DOM-building functions
stubbed, and pins what a person depends on:

- switching away and back brings the card back, with its buttons;
- a prompt that arrives while another chat is on screen is not drawn into it, is drawn when its own
  chat is shown, and the sidebar and a toast say where it waits;
- a structured field change awaiting validation survives a redraw, a replay and a page reload
  for the same plan; old raw drafts are discarded and never submitted invisibly;
- nothing is drawn again once the prompt is answered or the run is over.

The chat-context meter of a fast-chat turn follows the same rule: it is shown only in its own chat.

The cards themselves (``planCardHtml``, ``planEditorHtml``) are pinned in
``test_console_plan_card_js.py``. Skipped where node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"

# The parts of app.js the harness runs, as (start, end) markers; everything else is a stub below.
BLOCKS = [
    ("const chats = new Map();", "function toast("),
    ("// What the sidebar says about a chat whose prompt is waiting", "// ---- chat rendering"),
    ("function renderChat()", "function refreshDownloads()"),
    ("function finishAssistantStream(c)", "function appendArtifacts("),
    ("const STREAM_TYPES", "// ---- actions"),
    ("const DEFAULT_PLACEHOLDER", "// A step arrives as"),
    ("function planProblemText(", "function showPlanPanel("),
    ("function planSettingText(", "function planRunsOnText("),
    ("function planSavedMessage(", "function planModelContent("),
    ("async function submitDecision(", "// ---- uploads"),
]

ESCAPE = r'(s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")'

HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const store = new Map();
const log = [];
const requests = [];
const savedMessages = [];
const els = {};
const el = (id) => els[id] || (els[id] = { id, innerHTML: "", textContent: "", placeholder: "", value: "",
  scrollTop: 0, scrollHeight: 0, appendChild() {}, querySelector: () => null,
  classList: { add() {}, remove() {}, toggle(name, value) { els[id][name] = value; }, contains: () => false } });
// What showPlanPanel / showClarify / showDecision leave behind: a card in the stream.
const drawn = (kind) => () => {
  log.push("draw:" + kind);
  sandbox.state.planCardEl = { kind, isConnected: true, querySelector: () => null, remove() { this.isConnected = false; } };
};
const sandbox = {
  // The per-chat module turns the plan / dialog fields of `state` into the chat on screen's.
  state: {
    sessions: [{ id: "A", title: "Owner chat ", messages: [{ role: "user", content: "q" }] },
               { id: "B", title: "Other chat", messages: [] }],
    activeId: "A", connectionId: "conn", planCardEl: null, planStatusEl: null,
  },
  PLANDRAFT_KEY: "draft", RUNOWNER_KEY: "owners", STREAMS_KEY: "streams", LASTRUN_KEY: "lastrun", COMPOSER_KEY: "composer",
  localStorage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
                  removeItem: (k) => store.delete(k) },
  $: el, escapeHtml: __ESCAPE__, planIcon: () => "", absTime: () => "", relTime: () => "1m ago",
  toast: (t) => log.push("toast:" + t),
  updateComposerButton: () => log.push("composer"),
  refreshDownloads() {}, syncPlaceholder() {}, repaintWorking() {},
  renderChatUsage() {}, refreshChatUsage() {}, checkBudgetNotices() {}, onUsageUpdate() {},   // usage counter: not under test here
  mountStream: () => log.push("mount"),
  messageEl: (m) => ({ m }),
  ownerVisible: () => { const c = sandbox.activeChat(); return !!(c && c.run); },
  activeSession: () => sandbox.state.sessions.find((s) => s.id === sandbox.state.activeId) || null,
  showPlanPanel: drawn("plan"), showClarify: drawn("clarify"), showDecision: drawn("decision"),
  // The side panel (its Plan tab) and the chat entry of an answered plan are drawn elsewhere.
  renderPlanPanel() {}, selectSideTab() {}, openPlanPanel: () => log.push("panel:open"), drawAnsweredPlan() {},
  wirePlanEntry() {}, planEntryHtml: () => "",
  planEstimateMarkdown: () => "",
  setChatRunning: (c, r) => { c.running = r; },
  startAssistantStream: (c) => { c.run = { text: "", thinking: "", feed: [] }; },
  alreadyHaveRun: () => false, appendArtifacts() {},
  renderContextMeter: (m) => log.push("meter:" + m.used),
  pushToSession: (sid, message) => { log.push("saved:" + sid); savedMessages.push({ sid, message }); },
  fetch: async (url, options) => { requests.push(JSON.parse(options.body)); return { ok: true, json: async () => ({}) }; },
  isUnreachable: () => false,
  setInterval, clearInterval,
};
const api = vm.runInNewContext(src + `
;({ handleWsMessage, renderChat, renderSessionList, planEditorHtml, notePlanDraft, dropPlanDraft, commitPlanChange, planInlineEdit,
    revisePlanInWords, submitPlan, submitDecision, chatOf, chats })`, sandbox);
const S = sandbox.state;
// This window launched chat A's run, so A's run events are this window's to handle.
const A = () => api.chatOf("A");
A().owned = true;
const YAML = "plan: 1\nsteps:\n  - id: s1\n    title: QC\n  - id: s2\n    title: Clustering\n    settings:\n      resolution: 1.2\n";
const EDIT = YAML.replace("resolution: 1.2", "resolution: 0.8");
const CHANGE = { id: "s2", setting: "resolution", value: "0.8" };
const editResolution = (typed) => {
  let input;
  const error = { textContent: "", remove() {} };
  const li = { querySelector: () => ({ querySelector: () => error }) };
  const field = { innerHTML: "1.2", querySelector: () => null, appendChild(el) { input = el; },
    classList: { contains: () => true, add() {}, remove() {} } };
  sandbox.document = { createElement: () => ({ style: {}, value: "", isConnected: true,
    setAttribute() {}, addEventListener() {}, focus() {}, select() {} }) };
  api.planInlineEdit(li, field, "1.2", "resolution", (value) => api.commitPlanChange(A(), { ...CHANGE, value }), A());
  input.value = typed;
  return { input, error };
};
const draft = () => { A().planDraftChange = { ...CHANGE }; return api.notePlanDraft(EDIT); };
const plan = (extra) => ({ type: "plan_prompt", conversation_id: "A", run_id: "r1",
  agenda: ["**QC** — filter.", "**Clustering** — resolution 1.2."], plan: { yaml: YAML, steps: [{ id: "s1", title: "QC" }, { id: "s2", title: "Clustering", settings: { resolution: 1.2 } }] },
  revised: [], problems: [], ...extra });
const draws = () => log.filter((x) => x.startsWith("draw:"));
const sidebar = () => { api.renderSessionList(); return els.sessionList.innerHTML; };
// A page reload: the page's memory is gone, the browser's storage is not (A's run owner included).
const reload = () => { api.chats.clear(); S.planCardEl = null; A().owned = true; };
const out = {};
(async () => {
__SCENARIO__
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
""".replace("__ESCAPE__", ESCAPE)


def _run(scenario: str, *, working: bool = False) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    code = "\n".join(source[source.index(start):source.index(end)] for start, end in BLOCKS)
    harness = HARNESS
    if working:
        for start, end in [
            ("let runTicker = null;", "function mountStream()"),
            ("function repaintWorking()", "function streamThinking("),
            ("function streamStatus(", "function streamCode("),
            ("function updateComposerButton()", "// Cancel a chat's in-flight run"),
        ]:
            code += "\n" + source[source.index(start):source.index(end)]
        harness = harness.replace("repaintWorking() {},", "")
        harness = harness.replace("setInterval, clearInterval,", "setInterval: () => 1, clearInterval() {}, Date: { now: () => sandbox.now }, now: 1000, renderServerPathOffer() {},")
        harness = harness.replace("submitPlan, submitDecision, chatOf, chats", "submitPlan, submitDecision, chatOf, chats, startAssistantStream, repaintWorking, streamStatus, streamToken, updateComposerButton")
    done = subprocess.run([node, "-e", harness.replace("__SCENARIO__", scenario)], input=code,
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_prompt_for_the_chat_on_screen_is_drawn_and_named_in_the_sidebar():
    out = _run("""
  api.handleWsMessage({ type: "chat_start", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(plan());
  out.draws = draws(); out.held = S.planPrompt.kind; out.pending = S.planPending; out.sidebar = sidebar();
""")
    assert out["draws"] == ["draw:plan"] and out["held"] == "plan" and out["pending"] is True
    sidebar = out["sidebar"]
    assert sidebar.count('class="session-waiting"') == 1 and ">Plan to review</span>" in sidebar
    assert sidebar.index('data-id="A"') < sidebar.index("session-waiting") < sidebar.index('data-id="B"')


def test_switching_away_and_back_draws_the_waiting_card_again():
    """renderChat() empties the stream; the card used to stay gone until a full page reload."""
    out = _run("""
  api.handleWsMessage({ type: "chat_start", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(plan());
  const card = S.planCardEl;
  S.activeId = "B"; log.length = 0; api.renderChat();
  out.away = { draws: draws(), cardEl: S.planCardEl, pending: A().planPending, composer: log.includes("composer") };
  S.activeId = "A"; log.length = 0; api.renderChat();
  out.back = { log: log.slice(), fresh: S.planCardEl !== card && S.planCardEl.isConnected };
""")
    assert out["away"] == {"draws": [], "cardEl": None, "pending": True, "composer": True}
    back = out["back"]["log"]
    assert "draw:plan" in back and back.index("mount") < back.index("draw:plan") < back.index("composer"), \
        "the card is drawn below the live bubble, then the composer is set for this chat"
    assert out["back"]["fresh"]


def test_a_prompt_that_arrives_while_another_chat_is_shown_waits_for_its_own_chat():
    out = _run("""
  S.activeId = "B";
  api.handleWsMessage({ type: "chat_start", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage({ type: "decision_prompt", conversation_id: "A", run_id: "r1", goal: "Batch correction?", options: ["Harmony"] });
  out.elsewhere = { draws: draws(), sid: A().plan.sid, toast: log.find((x) => x.startsWith("toast:")) || "", sidebar: sidebar() };
  S.activeId = "A"; api.renderChat();
  out.inOwner = draws();
""")
    elsewhere = out["elsewhere"]
    assert elsewhere["draws"] == [] and elsewhere["sid"] == "A"
    assert elsewhere["toast"] == "toast:“Owner chat” is waiting for your answer — open that chat to reply."
    assert ">Decision needed</span>" in elsewhere["sidebar"]
    assert out["inOwner"] == ["draw:decision"]


def test_an_untagged_prompt_belongs_to_the_chat_that_launched_the_run():
    """An older gateway sends no conversation id: the prompt goes to the chat this window launched."""
    out = _run("""
  S.activeId = "B";
  api.handleWsMessage({ type: "plan_clarify", questions: [{ question: "Which tissue?", options: ["retina"] }] });
  out.sid = A().plan.sid; out.draws = draws();
""")
    assert out == {"sid": "A", "draws": []}


def test_a_replay_of_the_prompt_on_screen_leaves_the_card_alone_and_keeps_its_revised_marks():
    """The server replays the pending plan on every subscribe, without `revised`."""
    out = _run("""
  api.handleWsMessage(plan({ revised: [2] }));
  const card = S.planCardEl;
  const { revised, ...replay } = plan();
  api.handleWsMessage(replay);
  out.onScreen = { draws: draws().length, sameCard: S.planCardEl === card, revised: S.planPrompt.revised, toasts: log.filter((x) => x.startsWith("toast:")).length };
  card.isConnected = false;          // the stream was rebuilt meanwhile
  api.handleWsMessage(replay);
  out.rebuilt = { draws: draws().length, revised: S.planRevisedSteps };
""")
    assert out["onScreen"] == {"draws": 1, "sameCard": True, "revised": [2], "toasts": 0}
    assert out["rebuilt"] == {"draws": 2, "revised": [2]}


def test_nothing_is_drawn_again_once_the_prompt_is_answered_or_the_run_is_over():
    out = _run("""
  const settle = () => { log.length = 0; api.renderChat(); return { held: S.planPrompt, pending: S.planPending, draws: draws(), marked: sidebar().includes("session-waiting") }; };
  api.handleWsMessage({ type: "chat_start", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(plan());
  await api.submitPlan("approve");
  out.saved = log.includes("saved:A");
  out.approved = settle();
  api.handleWsMessage({ type: "decision_prompt", conversation_id: "A", run_id: "r1", goal: "g", options: [] });
  await api.submitDecision("");
  out.decided = settle();
  api.handleWsMessage(plan());
  api.handleWsMessage({ type: "plan_done", conversation_id: "A", run_id: "r1" });
  out.done = settle();
  api.handleWsMessage(plan());
  draft();
  api.handleWsMessage({ type: "chat_done", conversation_id: "A", run_id: "r1" });   // a review that expired: no plan_done
  out.runOver = { ...settle(), draft: S.planDraftYaml, stored: store.has("draft"), placeholder: els.chatInput.placeholder };
""")
    gone = {"held": None, "pending": False, "draws": [], "marked": False}
    assert out["saved"] is True, "an approved plan is saved to the chat that owns it"
    assert out["approved"] == gone and out["decided"] == gone and out["done"] == gone
    run_over = out["runOver"]
    assert {k: run_over[k] for k in gone} == gone
    assert run_over["draft"] == "" and run_over["stored"] is False
    assert run_over["placeholder"].startswith("Ask a question, or describe"), "the composer is no longer the plan's reply box"


def test_a_structured_change_survives_a_redraw_and_replay_of_the_same_plan():
    out = _run("""
  api.handleWsMessage(plan());
  out.clean = api.planEditorHtml(S.planDoc);
  out.dirty = draft();
  out.typed = api.planEditorHtml(S.planDoc);
  api.renderChat();
  out.redrawn = api.planEditorHtml(S.planDoc);
  api.handleWsMessage(plan());
  out.replayed = api.planEditorHtml(S.planDoc);
""")
    assert out["clean"] == "" and out["dirty"] is True
    for key in ("typed", "redrawn", "replayed"):
        assert 'class="plan-edit-feedback"' in out[key]
        assert "Clustering: resolution → 0.8" in out[key]
        assert "plan: 1" not in out[key] and "<textarea" not in out[key]


def test_the_structured_draft_survives_a_reload_for_the_same_run_and_plan_only():
    out = _run("""
  api.handleWsMessage(plan());
  draft();
  out.stored = JSON.parse(store.get("draft"));
  const { revised, ...replay } = plan();
  reload(); api.handleWsMessage(replay);
  out.sameRun = { html: api.planEditorHtml(S.planDoc), change: A().planDraftChange };
  reload(); api.handleWsMessage({ ...replay, run_id: "r2" });
  out.otherRun = { html: api.planEditorHtml(S.planDoc), draft: A().planDraftYaml, stored: store.has("draft") };
""")
    stored = out["stored"]["A"]
    assert list(out["stored"]) == ["A"]
    change = {"id": "s2", "setting": "resolution", "value": "0.8"}
    assert stored == {"run": "r1", "base": stored["base"], "text": stored["text"], "change": change}
    assert "resolution: 0.8" in stored["text"] and "resolution: 1.2" in stored["base"]
    assert out["sameRun"]["change"] == change and "resolution → 0.8" in out["sameRun"]["html"]
    assert out["otherRun"] == {"html": "", "draft": "", "stored": False}


@pytest.mark.parametrize("legacy_single", [True, False])
def test_old_raw_editor_drafts_are_discarded_and_never_sent_invisibly(legacy_single):
    out = _run("""
  const old = { sid: "A", run: "r1", base: YAML, text: EDIT, open: true };
  store.set("draft", JSON.stringify(SINGLE ? old : { A: old }));
  reload(); api.handleWsMessage(plan());
  out.restored = { html: api.planEditorHtml(S.planDoc), draft: A().planDraftYaml, change: A().planDraftChange, stored: store.has("draft") };
  await api.submitPlan("approve");
  out.request = requests[0]; out.saved = savedMessages[0].message;
""".replace("SINGLE", json.dumps(legacy_single)))
    assert out["restored"] == {"html": "", "draft": "", "change": None, "stored": False}
    assert out["request"]["action"] == "approve" and out["request"]["plan_yaml"] == ""
    assert "resolution: 1.2" in out["saved"]["plan"]["doc"]["yaml"]
    assert "resolution: 0.8" not in out["saved"]["plan"]["doc"]["yaml"]


def test_a_new_plan_document_drops_the_draft_and_feedback():
    out = _run("""
  api.handleWsMessage(plan());
  draft();
  await api.submitPlan("edit", "", S.planDraftYaml);
  api.handleWsMessage({ type: "plan_done", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(plan({ plan: { yaml: EDIT, steps: [{ id: "s1" }, { id: "s2", settings: { resolution: 0.8 } }] }, revised: [2] }));
  out.html = api.planEditorHtml(S.planDoc); out.draft = S.planDraftYaml;
  out.change = A().planDraftChange; out.stored = store.has("draft");
""")
    assert out == {"html": "", "draft": "", "change": None, "stored": False}


def test_a_refused_change_keeps_the_requested_value_and_can_be_corrected():
    out = _run("""
  api.handleWsMessage(plan());
  draft();
  await api.submitPlan("edit", "", EDIT);
  api.handleWsMessage({ type: "plan_done", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(plan({ problems: ["step s2: resolution must be above 1"] }));
  out.refused = api.planEditorHtml(S.planDoc);
  await api.submitPlan("approve");
  out.blocked = { pending: A().planPending, count: requests.length, saved: savedMessages.length };
  const corrected = YAML.replace("resolution: 1.2", "resolution: 1.5");
  out.correctionError = api.commitPlanChange(A(), { ...CHANGE, value: "1.5" });
  out.correctedRequest = requests[1];
  api.handleWsMessage({ type: "plan_done", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(plan({ plan: { yaml: corrected, steps: [{ id: "s1" }, { id: "s2", settings: { resolution: 1.5 } }] }, revised: [2] }));
  out.applied = { html: api.planEditorHtml(S.planDoc), yaml: A().plan.doc.yaml, change: A().planDraftChange };
  await api.submitPlan("approve");
  out.saved = savedMessages[0].message;
""")
    assert "resolution → 0.8" in out["refused"] and "resolution must be above 1" in out["refused"]
    assert "plan: 1" not in out["refused"] and "<textarea" not in out["refused"]
    assert out["blocked"] == {"pending": True, "count": 1, "saved": 0}
    assert out["correctionError"] == "" and out["correctedRequest"]["action"] == "edit"
    assert out["correctedRequest"]["plan_yaml"] == out["applied"]["yaml"]
    assert out["applied"]["html"] == "" and out["applied"]["change"] is None
    assert out["saved"]["plan"]["doc"]["yaml"] == out["applied"]["yaml"]
    assert "resolution: 1.5" in out["saved"]["content"] and "```" not in out["saved"]["content"]


def test_a_reply_in_words_keeps_the_visible_change_and_cancelling_drops_it():
    out = _run("""
  api.handleWsMessage(plan());
  draft();
  await api.submitPlan("revise", "why resolution 1.2?", A().planDraftYaml);
  out.afterReply = { held: S.planPrompt, draft: S.planDraftYaml === EDIT, stored: store.has("draft"), sent: requests[0].plan_yaml === EDIT };
  api.handleWsMessage({ type: "plan_done", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(plan());
  out.samePlanBack = api.planEditorHtml(S.planDoc).includes("resolution → 0.8");
  await api.submitPlan("cancel");
  out.cancelled = { draft: S.planDraftYaml, change: A().planDraftChange, stored: store.has("draft") };
""")
    assert out["afterReply"] == {"held": None, "draft": True, "stored": True, "sent": True}
    assert out["samePlanBack"] is True
    assert out["cancelled"] == {"draft": "", "change": None, "stored": False}


def test_an_answer_no_run_was_waiting_for_says_so_and_ends_the_chats_run():
    """Prod 2026-10-28: the run behind a waiting plan was gone, /api/lab/plan answered
    {status: "stale"}, and the card said "Sent to the PI — reading your reply…" for good."""
    out = _run("""
  api.handleWsMessage({ type: "chat_start", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(plan());
  A().running = true;
  sandbox.fetch = async () => ({ ok: true, json: async () => ({ status: "stale", action: "ignored" }) });
  await api.submitPlan("revise", "change threshold of mitochondrial percentage to 25%.");
  out.running = A().running; out.run = A().run || null;
  out.status = A().answered ? A().answered.status : null;
  out.toasts = log.filter((x) => x.startsWith("toast:"));
""")
    assert out["running"] is False and out["run"] is None
    assert out["status"].startswith("Not delivered")
    assert [t for t in out["toasts"] if "Not delivered" in t] and not [t for t in out["toasts"] if "re-planning" in t]


def test_the_context_meter_of_a_turn_is_never_shown_in_another_chat():
    out = _run("""
  S.activeId = "B";
  api.handleWsMessage({ type: "chat_context", conversation_id: "A", run_id: "r1", used: 51234, allowed: 100000 });
  out.elsewhere = log.filter((x) => x.startsWith("meter:"));
  S.activeId = "A";
  api.handleWsMessage({ type: "chat_context", conversation_id: "A", run_id: "r1", used: 51234, allowed: 100000 });
  out.own = log.filter((x) => x.startsWith("meter:"));
""")
    assert out["elsewhere"] == [] and out["own"] == ["meter:51234"]


def test_two_chats_each_keep_their_own_waiting_prompt():
    """Several chats run at once, so several may be waiting: each keeps its own card and label."""
    out = _run("""
  api.chatOf("B").owned = true;
  api.handleWsMessage(plan());
  api.handleWsMessage({ type: "plan_clarify", conversation_id: "B", run_id: "r2", questions: [{ question: "Which tissue?", options: ["retina"] }] });
  out.held = { A: A().plan.kind, B: api.chatOf("B").plan.kind };
  out.sidebar = sidebar();
  await api.submitPlan("approve");                     // answers A's plan only
  out.after = { A: A().plan, B: api.chatOf("B").plan && api.chatOf("B").plan.kind };
  log.length = 0; S.activeId = "B"; api.renderChat();
  out.inB = draws();
""")
    assert out["held"] == {"A": "plan", "B": "clarify"}
    assert ">Plan to review</span>" in out["sidebar"] and ">Question for you</span>" in out["sidebar"]
    assert out["after"] == {"A": None, "B": "clarify"}
    assert out["inB"] == ["draw:clarify"]


@pytest.mark.parametrize("kind", ["plan", "clarify", "decision"])
def test_waiting_prompts_hide_working_keep_stop_and_restart_the_timer(kind):
    out = _run("""
  api.startAssistantStream(A());
  A().running = true;
  out.initialStart = A().run.startedAt;
  const working = { style: {}, textContent: "" };
  S.streamDom = { working, workingText: working };
  api.streamStatus(A(), "Drafting plan");
  sandbox.now = 61000;
  const prompt = __KIND__;
  api.handleWsMessage(prompt === "plan" ? plan() : {
    type: prompt === "clarify" ? "plan_clarify" : "decision_prompt",
    conversation_id: "A", run_id: "r1", questions: [], options: []
  });
  api.streamStatus(A(), "Stale backend activity");
  out.waiting = { display: working.style.display, start: A().run.startedAt };
  // The actual composer still exposes Stop while review is pending.
  api.updateComposerButton();
  out.stopHidden = els.chatStop.hidden;
  sandbox.now = 301000;
  if (prompt === "decision") await api.submitDecision("choice");
  else await api.submitPlan(prompt === "clarify" ? "revise" : "approve", "answer");
  if (prompt === "clarify") api.streamStatus(A(), "Working");
  // A successful approval redraws the chat; mountStream supplies a fresh working line.
  S.streamDom = { working, workingText: working };
  sandbox.now = 303000;
  api.repaintWorking();
  out.resumed = { display: working.style.display, text: working.textContent, start: A().run.startedAt };
""".replace("__KIND__", json.dumps(kind)), working=True)
    assert out["initialStart"] == 1000
    assert out["waiting"] == {"display": "none", "start": None}
    assert out["stopHidden"] is False
    assert out["resumed"]["display"] == "flex"
    assert out["resumed"]["start"] == 301000
    assert "2s" in out["resumed"]["text"]
    assert "Stale backend activity" not in out["resumed"]["text"]


def test_a_turn_without_a_plan_times_from_when_the_question_was_sent():
    out = _run("""
  api.startAssistantStream(A());
  const working = { style: {}, textContent: "" };
  S.streamDom = { working, workingText: working };
  sandbox.scheduleRepaint = () => {};
  sandbox.now = 5000;
  api.streamToken(A(), "Answer");
  sandbox.now = 8000;
  api.repaintWorking();
  out.start = A().run.startedAt; out.text = working.textContent;
""", working=True)
    assert out["start"] == 1000
    assert "7s" in out["text"]


def test_saved_plan_waits_without_a_running_stream_and_survives_replay():
    out = _run("""
  api.handleWsMessage({ type: "chat_start", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(plan({ saved: true }));
  draft();
  out.runningWhenShown = A().running;
  api.handleWsMessage({ type: "chat_stopped", conversation_id: "A", run_id: "r1", awaiting_plan: true });
  out.waiting = { pending: A().planPending, running: A().running, run: A().run, draft: A().planDraftYaml };
  reload(); A().owned = false;
  api.handleWsMessage(plan({ saved: true }));
  out.replayed = { pending: A().planPending, saved: A().plan.saved, running: A().running, draft: A().planDraftYaml };
  out.stoppedToasts = log.filter((x) => x.includes("Run stopped"));
""")
    assert out["runningWhenShown"] is False
    assert out["waiting"]["pending"] is True and out["waiting"]["running"] is False
    assert out["waiting"]["run"] is None and "resolution: 0.8" in out["waiting"]["draft"]
    assert out["replayed"] == {"pending": True, "saved": True, "running": False, "draft": out["waiting"]["draft"]}
    assert out["stoppedToasts"] == []


@pytest.mark.parametrize("status", ["stale", "not_delivered"])
def test_a_reply_without_a_saved_plan_says_not_delivered(status):
    out = _run("""
  api.handleWsMessage(plan({ saved: true }));
  sandbox.fetch = async (url, options) => {
    out.request = JSON.parse(options.body);
    return { ok: true, json: async () => ({ status: STATUS, message: "The plan was cancelled." }) };
  };
  await api.submitPlan("revise", "raise the mitochondrial limit to 25");
  out.toasts = log.filter((x) => x.startsWith("toast:"));
  out.status = A().answered.status;
""".replace("STATUS", json.dumps(status)))
    assert out["request"]["run_id"] == "r1"
    assert out["toasts"] == ["toast:Not delivered: The plan was cancelled."]
    assert out["status"] == "Not delivered: The plan was cancelled."


def test_saved_clarify_replays_in_a_new_browser_without_a_running_slot():
    out = _run("""
  A().owned = false;
  api.handleWsMessage({ type: "plan_clarify", saved: true, conversation_id: "A", run_id: "r1", questions: [{ question: "Which tissue?" }] });
  api.handleWsMessage({ type: "chat_stopped", conversation_id: "A", run_id: "r1", awaiting_plan: true });
  out.pending = A().planPending; out.kind = A().plan.kind; out.running = A().running; out.draws = draws();
""")
    assert out == {"pending": True, "kind": "clarify", "running": False, "draws": ["draw:clarify"]}


@pytest.mark.parametrize("action", ["approve", "edit", "revise"])
def test_a_refused_saved_plan_remains_retryable_with_its_visible_change(action):
    out = _run("""
  api.handleWsMessage(plan({ saved: true }));
  if (ACTION !== "approve") draft();
  sandbox.fetch = async () => ({ ok: false, json: async () => ({ error: "Not delivered: No run slot is available." }) });
  await api.submitPlan(ACTION, "raise the limit", ACTION === "approve" ? "" : EDIT);
  out.pending = A().planPending; out.yaml = A().planDraftYaml; out.html = api.planEditorHtml(S.planDoc);
  out.saved = log.filter((x) => x.startsWith("saved:"));
  out.toasts = log.filter((x) => x.startsWith("toast:"));
""".replace("ACTION", json.dumps(action)))
    assert out["pending"] is True
    if action == "approve":
        assert out["yaml"] == "" and out["html"] == ""
    else:
        assert "resolution: 0.8" in out["yaml"] and "resolution → 0.8" in out["html"]
    assert out["saved"] == []
    assert out["toasts"] == ["toast:Not delivered: No run slot is available."]


def test_saved_plan_approval_can_confirm_the_budget_and_retry():
    out = _run("""
  api.handleWsMessage(plan({ saved: true }));
  out.requests = []; sandbox.userTz = () => "America/Los_Angeles";
  sandbox.confirm = () => true;
  sandbox.fetch = async (url, options) => {
    out.requests.push(JSON.parse(options.body));
    return out.requests.length === 1
      ? { ok: false, json: async () => ({ needs_budget_confirm: true, error: "Budget reached." }) }
      : { ok: true, json: async () => ({ status: "ok" }) };
  };
  await api.submitPlan("approve");
  out.saved = log.filter((x) => x.startsWith("saved:"));
""")
    assert len(out["requests"]) == 2
    assert out["requests"][1]["budget_ack"] is True
    assert out["requests"][1]["tz"] == "America/Los_Angeles"
    assert out["saved"] == ["saved:A"]


@pytest.mark.parametrize("saved", [False, True])
def test_approval_is_saved_only_after_the_server_accepts_it(saved):
    out = _run("""
  api.handleWsMessage(plan({ saved: SAVED }));
  let answer;
  sandbox.fetch = async (url, options) => {
    out.request = JSON.parse(options.body);
    return new Promise((resolve) => { answer = resolve; });
  };
  const pending = api.submitPlan("approve");
  out.beforeResponse = { messages: savedMessages.length, approvedToast: log.some((x) => x.includes("Plan approved")) };
  answer({ ok: true, json: async () => ({ status: "ok" }) });
  await pending;
  out.afterResponse = { messages: savedMessages, approvedToast: log.some((x) => x.includes("Plan approved")) };
""".replace("SAVED", json.dumps(saved)))
    assert out["beforeResponse"] == {"messages": 0, "approvedToast": False}
    assert out["request"]["action"] == "approve"
    assert out["afterResponse"]["approvedToast"] is True
    assert len(out["afterResponse"]["messages"]) == 1
    message = out["afterResponse"]["messages"][0]
    assert message["sid"] == "A" and message["message"]["kind"] == "plan"
    assert "resolution: 1.2" in message["message"]["content"]
    assert "```" not in message["message"]["content"]
    assert "resolution: 1.2" in message["message"]["plan"]["doc"]["yaml"]


@pytest.mark.parametrize("typed, valid", [("1.5", True), ("lots", False)])
def test_clicking_run_finishes_the_active_field_and_waits_for_validation(typed, valid):
    out = _run("""
  api.handleWsMessage(plan());
  let input;
  const error = { textContent: "", remove() {} };
  const li = { querySelector: () => ({ querySelector: () => error }) };
  const field = { innerHTML: "1.2", querySelector: () => null, appendChild(el) { input = el; },
    classList: { contains: () => true, add() {}, remove() {} } };
  sandbox.document = { createElement: () => ({ style: {}, value: "", isConnected: true,
    setAttribute() {}, addEventListener() {}, focus() {}, select() {} }) };
  api.planInlineEdit(li, field, "1.2", "resolution", (value) => api.commitPlanChange(A(), { ...CHANGE, value }), A());
  input.value = TYPED;
  await api.submitPlan("approve");
  out.requests = requests; out.saved = savedMessages; out.error = error.textContent;
  out.pending = A().planPending; out.editing = !!A().planInlineFinish;
""".replace("TYPED", json.dumps(typed)))
    assert out["saved"] == [], "Run cannot save the old plan while a field edit is being checked"
    assert not any(request["action"] == "approve" for request in out["requests"])
    if valid:
        assert len(out["requests"]) == 1 and out["requests"][0]["action"] == "edit"
        assert "resolution: 1.5" in out["requests"][0]["plan_yaml"]
        assert out["pending"] is False and out["editing"] is False
    else:
        assert out["requests"] == [] and "number" in out["error"]
        assert out["pending"] is True and out["editing"] is True


@pytest.mark.parametrize("typed", ["1.5", "lots"])
def test_words_wait_for_the_active_field_without_being_lost_or_shown_as_sent(typed):
    out = _run("""
  api.handleWsMessage(plan());
  const editor = editResolution(TYPED);
  const messages = [];
  sandbox.appendUserMessage = (text) => messages.push(text);
  api.revisePlanInWords(A(), "Use a stricter cutoff in the next step");
  out.waiting = { requests: requests.slice(), messages: messages.slice(), words: A().planWords,
    error: editor.error.textContent };
  if (A().planInlineFinish) {
    editor.input.value = "1.5";
    A().planInlineFinish(true, false);
  }
  const edited = YAML.replace("resolution: 1.2", "resolution: 1.5");
  api.handleWsMessage(plan({
    agenda: ["**QC** — filter.", "**Clustering** — resolution 1.5."],
    plan: { yaml: edited, steps: [{ id: "s1", title: "QC" },
      { id: "s2", title: "Clustering", settings: { resolution: 1.5 } }] }, revised: [2]
  }));
  out.afterValidation = { words: A().planWords, draft: A().planDraftYaml };
  api.revisePlanInWords(A(), A().planWords);
  out.sent = { requests, messages, words: A().planWords };
""".replace("TYPED", json.dumps(typed)))
    words = "Use a stricter cutoff in the next step"
    waiting = out["waiting"]
    assert waiting["messages"] == [] and waiting["words"] == words
    assert [r["action"] for r in waiting["requests"]] == (["edit"] if typed == "1.5" else [])
    assert out["afterValidation"] == {"words": words, "draft": ""}
    assert [r["action"] for r in out["sent"]["requests"]] == ["edit", "revise"]
    assert "resolution: 1.5" in out["sent"]["requests"][0]["plan_yaml"]
    assert out["sent"]["requests"][1]["feedback"] == words
    assert out["sent"]["requests"][1]["plan_yaml"] == ""
    assert out["sent"]["messages"] == [words] and out["sent"]["words"] == ""


@pytest.mark.parametrize("action", ["approve", "revise"])
def test_a_field_removed_by_redrawing_never_submits_a_hidden_change(action):
    out = _run("""
  api.handleWsMessage(plan());
  const editor = editResolution("2.4");
  // The panel was rebuilt or switched to another saved plan. Its old input is gone.
  editor.input.isConnected = false;
  sandbox.appendUserMessage = () => {};
  if (ACTION === "revise") api.revisePlanInWords(A(), "Explain the current plan");
  else await api.submitPlan("approve");
  out.requests = requests; out.saved = savedMessages; out.activeField = !!A().planInlineFinish;
""".replace("ACTION", json.dumps(action)))
    assert len(out["requests"]) == 1 and out["requests"][0]["action"] == action
    assert out["requests"][0]["plan_yaml"] == ""
    assert out["activeField"] is False
    if action == "approve":
        assert "resolution: 1.2" in out["saved"][0]["message"]["plan"]["doc"]["yaml"]
