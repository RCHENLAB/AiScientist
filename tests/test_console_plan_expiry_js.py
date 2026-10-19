"""The waiting card's expiry warning (``plan_expiring``), run under node.

The gateway sends ``plan_expiring`` once, when three quarters of a plan review's window have passed
with no answer. The console used to hand it to ``setPlanStatus``, which writes only to a RESOLVED
card's status line, so on the card that was actually waiting it never appeared. ``frontend/console/
app.js`` now keeps the warning on the waiting prompt its chat holds (``chatOf(sid).plan.expiry``;
``state.planPrompt`` is the chat on screen's) and draws it on that prompt's card. This runs the WebSocket handler, the prompt and plan-card section,
``renderChat``, the sidebar and ``finishAssistantStream`` under node, on a small DOM whose
``innerHTML`` is parsed (so the real card markup is what gets queried), and pins:

- the warning is a line on the waiting plan card, heading its actions row, with the time it expires;
- a question and a decision point carry it too, in their own words, under their options;
- it survives a switch of chats and a replay of the same prompt; a different prompt starts clean;
- a warning for a prompt that waits in another chat is drawn when that chat is shown;
- it goes with the prompt: answered, resolved by the server (``plan_done``) or the run over.

The prompt's other behaviour across chat switches is pinned in ``test_console_plan_pending_js.py``.
Skipped where node is not installed.
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
    ("const DEFAULT_PLACEHOLDER", "// ---- uploads"),
]

ESCAPE = r'(s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")'

HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const log = [];

// ---- a DOM just big enough for the plan cards: elements, a parsed innerHTML, simple selectors ----
const VOID = new Set(["br", "hr", "img", "input", "meta", "link", "wbr"]);
const unescape = (s) => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
function textNode(value) {
  return { tagName: "#text", value: String(value), parent: null, kids: [], get textContent() { return this.value; },
           remove() { detach(this); } };
}
function detach(n) { if (n.parent) { const k = n.parent.kids; k.splice(k.indexOf(n), 1); n.parent = null; } }
function makeEl(tag) {
  const el = {
    tagName: tag, attrs: {}, kids: [], parent: null, dataset: {}, on: {}, value: "", disabled: false, title: "",
    placeholder: "", scrollTop: 0, scrollHeight: 0,
    get className() { return this.attrs.class || ""; }, set className(v) { this.attrs.class = String(v); },
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    addEventListener(t, f) { (this.on[t] = this.on[t] || []).push(f); },
    click() { (this.on.click || []).forEach((f) => f({ preventDefault() {} })); },
    appendChild(c) { detach(c); c.parent = this; this.kids.push(c); return c; },
    prepend(c) { detach(c); c.parent = this; this.kids.unshift(c); },
    remove() { detach(this); },
    get isConnected() { let n = this; while (n.parent) n = n.parent; return n === stream; },
    get textContent() { return this.kids.map((k) => k.textContent).join(""); },
    set textContent(v) { this.kids.forEach((k) => { k.parent = null; }); this.kids = []; this.appendChild(textNode(v)); },
    set innerHTML(html) { this.kids.forEach((k) => { k.parent = null; }); this.kids = []; parseInto(this, String(html)); },
    get children() { return this.kids.filter((k) => k.tagName !== "#text"); },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    querySelectorAll(sel) {
      const out = [];
      const walk = (n) => { for (const k of n.kids) { if (k.tagName !== "#text" && matches(k, sel)) out.push(k); walk(k); } };
      walk(this);
      return out;
    },
  };
  const classes = () => el.className.split(/\s+/).filter(Boolean);
  el.classList = {
    add: (...c) => { el.className = [...new Set([...classes(), ...c])].join(" "); },
    remove: (...c) => { el.className = classes().filter((x) => !c.includes(x)).join(" "); },
    toggle: (c, on) => { const has = classes().includes(c); const want = on === undefined ? !has : !!on;
                         if (want) el.classList.add(c); else el.classList.remove(c); return want; },
    contains: (c) => classes().includes(c),
  };
  return el;
}
function parseInto(root, html) {
  const re = /<!--[\s\S]*?-->|<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z0-9]+)((?:\s+[^\s=>\/]+(?:="[^"]*")?)*)\s*\/?>|[^<]+/g;
  let cur = root, m;
  while ((m = re.exec(html))) {
    if (m[1]) { if (cur !== root) cur = cur.parent; continue; }
    if (m[2]) {
      const el = makeEl(m[2].toLowerCase());
      for (const a of (m[3] || "").matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) el.setAttribute(a[1], unescape(a[2] || ""));
      cur.appendChild(el);
      if (!VOID.has(el.tagName)) cur = el;
      continue;
    }
    if (!m[0].startsWith("<!--")) cur.appendChild(textNode(unescape(m[0])));
  }
}
// One compound selector: tag, .class, [attr="v"], :last-child — all the cards ask for.
function matches(el, sel) {
  let rest = sel.trim();
  const tag = /^[a-z][a-z0-9]*/i.exec(rest);
  if (tag) { if (el.tagName !== tag[0].toLowerCase()) return false; rest = rest.slice(tag[0].length); }
  for (const part of rest.match(/\.[\w-]+|\[[^\]]+\]|:last-child/g) || []) {
    if (part[0] === ".") { if (!el.classList.contains(part.slice(1))) return false; }
    else if (part === ":last-child") { const sib = el.parent ? el.parent.children : []; if (sib[sib.length - 1] !== el) return false; }
    else { const a = /^\[([^=\]]+)(?:="([^"]*)")?\]$/.exec(part); const v = el.getAttribute(a[1]); if (a[2] === undefined ? v === null : v !== a[2]) return false; }
  }
  return true;
}

const stream = makeEl("div"); stream.attrs.id = "chatStream";
const els = { chatStream: stream };
const escapeHtml = __ESCAPE__;
const store = new Map();
const sandbox = {
  // The per-chat module turns the plan / dialog fields of `state` into the chat on screen's.
  state: {
    sessions: [{ id: "A", title: "Owner chat", messages: [{ role: "user", content: "q" }] },
               { id: "B", title: "Other chat", messages: [] }],
    activeId: "A", connectionId: "conn", planCardEl: null, planStatusEl: null,
  },
  PLANDRAFT_KEY: "draft", RUNOWNER_KEY: "owners", STREAMS_KEY: "streams", LASTRUN_KEY: "lastrun", COMPOSER_KEY: "composer",
  localStorage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
                  removeItem: (k) => store.delete(k) },
  document: { createElement: (t) => makeEl(String(t).toLowerCase()) },
  $: (id) => els[id] || (els[id] = makeEl("div")),
  escapeHtml, renderMarkdown: (s) => "<p>" + escapeHtml(s) + "</p>", absTime: () => "", relTime: () => "1m ago",
  toast: (t) => log.push("toast:" + t), updateComposerButton() {}, mountStream() {}, repaintWorking() {},
  refreshDownloads() {}, syncPresetUI() {}, messageEl: () => makeEl("div"),
  renderChatUsage() {}, refreshChatUsage() {}, checkBudgetNotices() {}, onUsageUpdate() {},   // usage counter: not under test here
  setPanel() {},   // the side panel opening for a plan; its tab and contents are drawn into $() elements
  getComputedStyle: () => ({ lineHeight: "" }),   // no layout here: every step keeps its chevron
  activeSession: () => sandbox.state.sessions.find((x) => x.id === sandbox.state.activeId) || null,
  pushToSession: (sid) => log.push("saved:" + sid), appendArtifacts() {},
  ownerVisible: () => false, setChatRunning: (c, r) => { c.running = r; },
  startAssistantStream: (c) => { c.run = null; }, alreadyHaveRun: () => false,
  fetch: async () => ({ ok: true, json: async () => ({}) }), isUnreachable: () => false,
  setInterval, clearInterval,
};
const api = vm.runInNewContext(src + `
;({ handleWsMessage, renderChat, renderSessionList, submitPlan, submitDecision, chatOf })`, sandbox);
// This window launched chat A's run, so A's run events are this window's to handle.
const A = () => api.chatOf("A");
A().owned = true;
// The clock is fixed, so the time a line names is known.
const NOW = Date.UTC(2026, 9, 14, 21, 30, 0);
vm.runInContext(`Date.now = () => ${NOW};`, sandbox);
const clock = (secs) => vm.runInContext(`new Date(${NOW + secs * 1000}).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })`, sandbox);
const S = sandbox.state;
const tag = { conversation_id: "A", run_id: "r1" };
const send = (msg) => api.handleWsMessage({ ...tag, ...msg });
const plan = () => send({ type: "plan_prompt", agenda: ["**QC** — filter.", "**Clustering** — resolution 1.2."], revised: [], problems: [] });
const expiring = (secs) => send(secs === undefined ? { type: "plan_expiring" } : { type: "plan_expiring", seconds_left: secs });
// What a person sees of the warning: every line in the chat, and where the one on the waiting card sits.
// The line is an icon (its ligature name is text) and the words: what is read is the words.
const words = (line) => line.querySelector("span:last-child").textContent;
const lines = () => stream.querySelectorAll(".plan-expiry").map(words);
const where = () => {
  const card = S.planCardEl, line = card && card.querySelector(".plan-expiry");
  if (!line) return null;
  const row = card.querySelector(".plan-actions-row");
  return { text: words(line), role: line.getAttribute("role"),
           inRowFirst: !!row && row.children[0] === line, lastOfCard: card.children[card.children.length - 1] === line };
};
// A switch of chats: renderChat() empties the stream and draws what the chat on screen holds.
const show = (sid) => { S.activeId = sid; api.renderChat(); };
const out = { at5: clock(300) };
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


PLAN_LINE = "This plan expires at {} — run it, change it or reply before then, or nothing will run."


def test_the_warning_is_a_line_on_the_waiting_plan_card():
    """It used to go to the RESOLVED card's status line, which does not exist while a card waits."""
    out = _run("""
  plan();
  out.statusElBefore = S.planStatusEl;
  expiring(300);
  out.where = where(); out.lines = lines(); out.held = S.planPrompt.expiry;
""")
    assert out["statusElBefore"] is None, "the card is waiting: there is no resolved status line to write to"
    assert out["where"] == {"text": PLAN_LINE.format(out["at5"]), "role": "status",
                            "inRowFirst": True, "lastOfCard": False}
    assert out["lines"] == [PLAN_LINE.format(out["at5"])], "one line, on the waiting card"
    assert out["held"] is not None, "the warning is kept on the prompt the card is drawn from"


def test_a_question_and_a_decision_point_carry_it_in_their_own_words():
    out = _run("""
  send({ type: "plan_clarify", questions: [{ question: "Which tissue?", options: ["retina", "RPE"] }] });
  expiring(300);
  out.clarify = where();
  send({ type: "plan_done" });
  out.afterDone = { lines: lines(), held: S.planPrompt };
  send({ type: "decision_prompt", goal: "Batch correction?", options: ["Harmony"] });
  out.decisionBefore = where();
  expiring(300);
  out.decision = where();
""")
    at = out["at5"]
    assert out["clarify"] == {"text": f"This question expires at {at} — answer it before then, or nothing will run.",
                              "role": "status", "inRowFirst": False, "lastOfCard": True}
    assert out["afterDone"] == {"lines": [], "held": None}
    assert out["decisionBefore"] is None, "a new prompt starts a new review window"
    assert out["decision"]["text"] == (f"This decision expires at {at} — choose before then, "
                                       "or the run goes on without your answer.")
    assert out["decision"]["lastOfCard"] is True


def test_the_warning_survives_a_switch_of_chats_and_a_replay_of_the_same_prompt():
    out = _run("""
  plan(); expiring(300);
  const card = S.planCardEl;
  plan();                                   // the server replays the waiting prompt (a reconnect)
  out.replayed = { sameCard: S.planCardEl === card, lines: lines() };
  show("B");
  out.away = { lines: lines(), held: !!A().plan.expiry };
  show("A");
  out.back = { fresh: S.planCardEl !== card && S.planCardEl.isConnected, where: where(), lines: lines().length };
  S.planCardEl.remove();                    // the stream was rebuilt meanwhile, then the prompt is replayed
  plan();
  out.rebuilt = where();
""")
    line = PLAN_LINE.format(out["at5"])
    assert out["replayed"] == {"sameCard": True, "lines": [line]}
    assert out["away"] == {"lines": [], "held": True}, "not drawn into the other chat, still held"
    assert out["back"]["fresh"] is True and out["back"]["lines"] == 1
    assert out["back"]["where"]["text"] == line and out["back"]["where"]["inRowFirst"] is True
    assert out["rebuilt"]["text"] == line


def test_a_different_prompt_starts_without_the_warning():
    """Only a repeat of the waiting prompt keeps it. A replay that differs from what is held (the gateway
    replays a plan without its document) is followed by the gateway's own replay of the warning."""
    out = _run("""
  plan(); expiring(300);
  send({ type: "plan_prompt", agenda: ["**QC** — filter."], revised: [], problems: [] });
  out.different = { held: S.planPrompt.expiry || null, lines: lines() };
  expiring(120);
  out.rewarned = where();
""")
    assert out["different"] == {"held": None, "lines": []}
    assert out["rewarned"]["text"].startswith("This plan expires at ")


def test_a_warning_for_a_prompt_waiting_in_another_chat_is_drawn_when_that_chat_is_shown():
    out = _run("""
  expiring(300);                            // nothing waits: there is nothing for it to be about
  out.nothingHeld = S.planPrompt;
  show("B");
  plan(); expiring(300);
  out.elsewhere = { lines: lines(), held: !!A().plan.expiry, sidebar: (api.renderSessionList(), els.sessionList.querySelectorAll(".session-waiting").length) };
  show("A");
  out.inOwner = where();
""")
    assert out["nothingHeld"] is None
    assert out["elsewhere"] == {"lines": [], "held": True, "sidebar": 1}
    assert out["inOwner"]["text"] == PLAN_LINE.format(out["at5"])


def test_the_warning_goes_with_the_prompt():
    out = _run("""
  const gone = () => ({ held: S.planPrompt, lines: lines() });
  plan(); expiring(300);
  api.submitPlan("revise", "why resolution 1.2?");
  out.replied = { ...gone(), status: S.planStatusEl && S.planStatusEl.textContent };
  send({ type: "plan_status", text: "The PI is answering your question…" });
  out.statusStillWorks = S.planStatusEl.textContent;
  send({ type: "plan_done" });
  plan();
  out.nextPlan = where();
  expiring(300);
  api.submitPlan("approve");
  out.approved = gone();
  send({ type: "decision_prompt", goal: "g", options: [] }); expiring(300);
  api.submitDecision("");
  out.decided = gone();
  plan(); expiring(300);
  send({ type: "plan_done" });              // answered in another window: the card here stays, unwarned
  out.doneElsewhere = gone();
  plan(); expiring(300);
  send({ type: "chat_done" });              // a review that ran out: no plan_done
  out.runOver = { ...gone(), card: S.planCardEl };
  show("B"); show("A");
  out.afterSwitch = lines();
""")
    gone = {"held": None, "lines": []}
    assert out["replied"] == {**gone, "status": "✎ Sent to the PI — reading your reply…"}
    assert out["statusStillWorks"] == "The PI is answering your question…"
    assert out["nextPlan"] is None
    for key in ("approved", "decided", "doneElsewhere"):
        assert out[key] == gone, key
    assert out["runOver"] == {**gone, "card": None}
    assert out["afterSwitch"] == []


def test_a_warning_without_a_time_still_says_it_is_coming():
    out = _run("""
  plan(); expiring();
  out.where = where();
""")
    assert out["where"]["text"] == ("This plan expires in a few minutes — run it, change it or reply "
                                    "before then, or nothing will run.")
