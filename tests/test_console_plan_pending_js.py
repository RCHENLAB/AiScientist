"""The console's waiting prompt (a plan, a clarify question, a decision point), run under node.

A prompt waits in ONE chat, the one that owns the run, and its card is DOM that ``renderChat()``
throws away on every switch of chats. ``frontend/console/app.js`` therefore holds the prompt in that
chat's record (``chatOf(sid).plan``; ``state.planPrompt`` is the chat on screen's) until it is
resolved and draws the card from it. Several chats may each have one waiting. This runs the functions
involved (the WebSocket handler, the prompt section, ``renderChat``, ``renderSessionList``, the
plan editor's HTML, the submit and run-end paths) under node with the DOM-building functions
stubbed, and pins what a person depends on:

- switching away and back brings the card back, with its buttons;
- a prompt that arrives while another chat is on screen is not drawn into it, is drawn when its own
  chat is shown, and the sidebar and a toast say where it waits;
- text typed into the plan editor and not applied, and whether the editor is open, survive a
  redraw, a replay of the prompt and a page reload for as long as the plan document is the same;
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
    ("function planEditorHtml(", "function showPlanPanel("),
    ("async function submitDecision(", "// ---- uploads"),
]

ESCAPE = r'(s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")'

HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const store = new Map();
const log = [];
const els = {};
const el = (id) => els[id] || (els[id] = { id, innerHTML: "", textContent: "", placeholder: "", value: "",
  scrollTop: 0, scrollHeight: 0, appendChild() {}, querySelector: () => null,
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false } });
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
  refreshDownloads() {}, syncPresetUI() {}, repaintWorking() {},
  renderChatUsage() {}, refreshChatUsage() {}, checkBudgetNotices() {}, onUsageUpdate() {},   // usage counter: not under test here
  mountStream: () => log.push("mount"),
  messageEl: (m) => ({ m }),
  ownerVisible: () => { const c = sandbox.activeChat(); return !!(c && c.run); },
  activeSession: () => sandbox.state.sessions.find((s) => s.id === sandbox.state.activeId) || null,
  showPlanPanel: drawn("plan"), showClarify: drawn("clarify"), showDecision: drawn("decision"),
  // The side panel (its Plan tab) and the chat entry of an answered plan are drawn elsewhere.
  renderPlanPanel() {}, selectSideTab() {}, openPlanPanel: () => log.push("panel:open"), drawAnsweredPlan() {},
  wirePlanEntry() {}, planEntryHtml: () => "",
  planSavedMessage: (p) => ({ role: "assistant", kind: "plan", content: "", plan: p }),
  setChatRunning: (c, r) => { c.running = r; },
  startAssistantStream: (c) => { c.run = { text: "", thinking: "", feed: [] }; },
  alreadyHaveRun: () => false, appendArtifacts() {},
  renderContextMeter: (m) => log.push("meter:" + m.used),
  pushToSession: (sid) => log.push("saved:" + sid),
  fetch: async () => ({ ok: true, json: async () => ({}) }), isUnreachable: () => false,
  setInterval, clearInterval,
};
const api = vm.runInNewContext(src + `
;({ handleWsMessage, renderChat, renderSessionList, planEditorHtml, notePlanDraft, notePlanEditorOpen,
    submitPlan, submitDecision, chatOf, chats })`, sandbox);
const S = sandbox.state;
// This window launched chat A's run, so A's run events are this window's to handle.
const A = () => api.chatOf("A");
A().owned = true;
const YAML = "plan: 1\nsteps:\n  - id: s1\n    title: QC\n  - id: s2\n    title: Clustering\n    settings:\n      resolution: 1.2\n";
const EDIT = YAML.replace("resolution: 1.2", "resolution: 0.8");
const plan = (extra) => ({ type: "plan_prompt", conversation_id: "A", run_id: "r1",
  agenda: ["**QC** — filter.", "**Clustering** — resolution 1.2."], plan: { yaml: YAML, steps: [{ id: "s1" }, { id: "s2" }] },
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


def _run(scenario: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    code = "\n".join(source[source.index(start):source.index(end)] for start, end in BLOCKS)
    done = subprocess.run([node, "-e", HARNESS.replace("__SCENARIO__", scenario)], input=code,
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
  api.submitPlan("approve");
  out.saved = log.includes("saved:A");
  out.approved = settle();
  api.handleWsMessage({ type: "decision_prompt", conversation_id: "A", run_id: "r1", goal: "g", options: [] });
  api.submitDecision("");
  out.decided = settle();
  api.handleWsMessage(plan());
  api.handleWsMessage({ type: "plan_done", conversation_id: "A", run_id: "r1" });
  out.done = settle();
  api.handleWsMessage(plan());
  api.notePlanDraft(EDIT);
  api.handleWsMessage({ type: "chat_done", conversation_id: "A", run_id: "r1" });   // a review that expired: no plan_done
  out.runOver = { ...settle(), draft: S.planDraftYaml, stored: store.has("draft"), placeholder: els.chatInput.placeholder };
""")
    gone = {"held": None, "pending": False, "draws": [], "marked": False}
    assert out["saved"] is True, "an approved plan is saved to the chat that owns it"
    assert out["approved"] == gone and out["decided"] == gone and out["done"] == gone
    run_over = out["runOver"]
    assert {k: run_over[k] for k in gone} == gone
    assert run_over["draft"] == "" and run_over["stored"] is False
    assert run_over["placeholder"].startswith("Ask AiScientist to plan"), "the composer is no longer the plan's reply box"


def test_an_unapplied_edit_and_the_open_editor_survive_a_redraw_of_the_same_plan():
    out = _run("""
  api.handleWsMessage(plan());
  out.closed = api.planEditorHtml(S.planDoc);
  api.notePlanEditorOpen(true);
  out.openOnly = api.planEditorHtml(S.planDoc);
  out.dirty = api.notePlanDraft(EDIT);
  out.typed = api.planEditorHtml(S.planDoc);
  api.notePlanEditorOpen(false);     // hidden with an edit not applied: Run is disabled, so it shows anyway
  out.hiddenWithEdit = api.planEditorHtml(S.planDoc);
  api.handleWsMessage(plan());       // the same plan sent again
  out.replayed = api.planEditorHtml(S.planDoc);
""")
    assert out["closed"].startswith('<div class="plan-yaml hidden">')
    assert out["openOnly"].startswith('<div class="plan-yaml">') and "resolution: 1.2" in out["openOnly"]
    assert out["dirty"] is True
    for key in ("typed", "hiddenWithEdit", "replayed"):
        assert out[key].startswith('<div class="plan-yaml">') and "resolution: 0.8" in out[key], key


def test_the_draft_survives_a_reload_for_the_same_run_and_plan_only():
    out = _run("""
  api.handleWsMessage(plan());
  api.notePlanEditorOpen(true);
  api.notePlanDraft(EDIT);
  out.stored = JSON.parse(store.get("draft"));
  const { revised, ...replay } = plan();
  reload(); api.handleWsMessage(replay);
  out.sameRun = { html: api.planEditorHtml(S.planDoc), open: S.planEditorOpen };
  reload(); api.handleWsMessage({ ...replay, run_id: "r2" });
  out.otherRun = api.planEditorHtml(S.planDoc);
""")
    stored = out["stored"]["A"]                       # one entry per chat: several may wait at once
    assert list(out["stored"]) == ["A"]
    assert stored == {"run": "r1", "base": stored["base"], "text": stored["text"], "open": True}
    assert "resolution: 0.8" in stored["text"] and "resolution: 1.2" in stored["base"]
    assert out["sameRun"]["open"] is True and "resolution: 0.8" in out["sameRun"]["html"]
    assert out["otherRun"].startswith('<div class="plan-yaml hidden">') and "resolution: 1.2" in out["otherRun"]


def test_a_new_plan_document_drops_the_draft_and_closes_the_editor():
    out = _run("""
  api.handleWsMessage(plan());
  api.notePlanEditorOpen(true);
  api.notePlanDraft(EDIT.replace("QC", "Quality control"));
  api.submitPlan("edit", "", S.planDraftYaml);
  api.handleWsMessage({ type: "plan_done", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(plan({ plan: { yaml: EDIT, steps: [{ id: "s1" }, { id: "s2" }] }, revised: [2] }));   // applied
  out.html = api.planEditorHtml(S.planDoc); out.draft = S.planDraftYaml; out.stored = store.has("draft");
""")
    assert out["html"].startswith('<div class="plan-yaml hidden">') and "Quality control" not in out["html"]
    assert out["draft"] == "" and out["stored"] is False


def test_a_refused_edit_comes_back_with_what_was_typed():
    out = _run("""
  api.handleWsMessage(plan());
  api.notePlanDraft(EDIT);
  api.submitPlan("edit", "", EDIT);
  api.handleWsMessage({ type: "plan_done", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(plan({ problems: ["step s2: resolution must be above 1"] }));
  out.html = api.planEditorHtml(S.planDoc);
""")
    assert out["html"].startswith('<div class="plan-yaml">')
    assert "resolution: 0.8" in out["html"] and "resolution must be above 1" in out["html"]


def test_a_reply_in_words_keeps_the_draft_and_running_or_cancelling_drops_it():
    """The PI may answer a question and send the same plan back: the edit in progress is still wanted."""
    out = _run("""
  api.handleWsMessage(plan());
  api.notePlanDraft(EDIT);
  api.submitPlan("revise", "why resolution 1.2?");
  out.afterReply = { held: S.planPrompt, draft: S.planDraftYaml === EDIT, stored: store.has("draft") };
  api.handleWsMessage({ type: "plan_done", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(plan());
  out.samePlanBack = api.planEditorHtml(S.planDoc).includes("resolution: 0.8");
  api.submitPlan("cancel");
  out.cancelled = { draft: S.planDraftYaml, stored: store.has("draft") };
""")
    assert out["afterReply"] == {"held": None, "draft": True, "stored": True}
    assert out["samePlanBack"] is True
    assert out["cancelled"] == {"draft": "", "stored": False}


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
  api.submitPlan("approve");                     // answers A's plan only
  out.after = { A: A().plan, B: api.chatOf("B").plan && api.chatOf("B").plan.kind };
  log.length = 0; S.activeId = "B"; api.renderChat();
  out.inB = draws();
""")
    assert out["held"] == {"A": "plan", "B": "clarify"}
    assert ">Plan to review</span>" in out["sidebar"] and ">Question for you</span>" in out["sidebar"]
    assert out["after"] == {"A": None, "B": "clarify"}
    assert out["inB"] == ["draw:clarify"]
