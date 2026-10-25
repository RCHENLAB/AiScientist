"""The console's confirmation card (``confirm_request``), run under node.

Before the agent acts at an edge on HPC3 (installs a package into the lab-shared area, writes outside
the user's folders, downloads, deletes), the server sends ``confirm_request`` and waits for
``POST /api/confirm``. The console had no card for it, so every such request waited out its timeout
and counted as no. ``frontend/console/app.js`` now holds the request in its own chat's record
(``chatOf(sid).confirm``), draws the card from it, and closes it on ``confirm_done``. This runs the
WebSocket handler, the confirmation section, ``renderChat``, ``renderSessionList`` and the run-end
path under node with a small fake DOM, and pins what a person depends on:

- the card says what the agent would do, in full, whether it can be undone and until when it waits,
  with Approve and Decline; text from the server is escaped;
- it is drawn only in its own chat, comes back when that chat is shown again or after a reload (the
  server sends it again), and is not drawn twice when it is sent again;
- the sidebar says the chat needs your OK while it waits;
- an answer posts the run's identity and closes the card with what was decided; a failed post
  brings the buttons back; ``confirm_done`` closes it with what became of it (another window, no
  answer in time, Stop); the end of the run closes it.

The server side is pinned in ``test_confirm_requests.py``. Skipped where node is not installed.
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
    ("async function submitDecision(", "// ---- uploads"),
]

ESCAPE = r'(s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")'

HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const store = new Map();
const log = [];
const posts = [];
const els = {};
const el = (id) => els[id] || (els[id] = { id, innerHTML: "", textContent: "", placeholder: "", value: "",
  scrollTop: 0, scrollHeight: 0, appendChild() {}, querySelector: () => null,
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false } });
// The chat stream: the cards appended to it, all dropped when renderChat rebuilds it.
let cards = [];
const stream = { id: "chatStream", scrollTop: 0, scrollHeight: 0, placeholder: "",
  set innerHTML(v) { cards.forEach((c) => { c.isConnected = false; }); cards = []; },
  get innerHTML() { return ""; },
  appendChild(child) { if (child && child.className) { child.isConnected = true; cards.push(child); } },
  querySelector: () => null, classList: { add() {}, remove() {}, toggle() {}, contains: () => false } };
els.chatStream = stream;
const document = { createElement: () => ({ className: "", dataset: {}, innerHTML: "", isConnected: false,
  remove() { this.isConnected = false; cards = cards.filter((c) => c !== this); }, querySelectorAll: () => [] }) };
let reply = { ok: true, status: 200, body: { status: "ok", approved: true } };
const sandbox = {
  state: {
    sessions: [{ id: "A", title: "Owner chat ", messages: [{ role: "user", content: "q" }] },
               { id: "B", title: "Other chat", messages: [{ role: "user", content: "other" }] }],
    activeId: "A", connectionId: "conn", planCardEl: null, planStatusEl: null, confirmCardEl: null,
  },
  PLANDRAFT_KEY: "draft", RUNOWNER_KEY: "owners", STREAMS_KEY: "streams", LASTRUN_KEY: "lastrun", COMPOSER_KEY: "composer",
  localStorage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
                  removeItem: (k) => store.delete(k) },
  document, $: el, escapeHtml: __ESCAPE__, planIcon: () => "", absTime: () => "", relTime: () => "1m ago",
  toast: (t) => log.push("toast:" + t),
  updateComposerButton() {}, refreshDownloads() {}, syncPlaceholder() {}, repaintWorking() {},
  renderChatUsage() {}, refreshChatUsage() {}, checkBudgetNotices() {}, onUsageUpdate() {},
  mountStream() {}, messageEl: (m) => ({ m }),
  ownerVisible: () => false,
  activeSession: () => sandbox.state.sessions.find((s) => s.id === sandbox.state.activeId) || null,
  showPlanPanel() {}, showClarify() {}, showDecision() {},
  renderPlanPanel() {}, selectSideTab() {}, openPlanPanel() {}, drawAnsweredPlan() {}, wirePlanEntry() {}, planEntryHtml: () => "",
  planSavedMessage: (p) => ({ role: "assistant", kind: "plan", content: "", plan: p }),
  setChatRunning: (c, r) => { c.running = r; },
  startAssistantStream: (c) => { c.run = { text: "", thinking: "", feed: [] }; },
  alreadyHaveRun: () => false, appendArtifacts() {}, renderContextMeter() {},
  pushToSession() {},
  fetch: async (url, init) => {
    posts.push({ url, body: JSON.parse(init.body) });
    if (reply === "down") throw new TypeError("Failed to fetch");
    return { ok: reply.ok, status: reply.status, json: async () => reply.body };
  },
  isUnreachable: () => false, setInterval, clearInterval,
};
const api = vm.runInNewContext(src + `
;({ handleWsMessage, renderChat, renderSessionList, chatOf, chats, answerConfirm, confirmCardHtml })`, sandbox);
const S = sandbox.state;
const A = () => api.chatOf("A");
A().owned = true;
const request = (extra) => ({ type: "confirm_request", conversation_id: "A", run_id: "r1", kind: "install",
  summary: "Install “pyranges” on HPC3 so this analysis can continue", detail: "pyranges into the lab-shared package area",
  reversible: true, timeout_s: 600, ...extra });
const live = () => cards.filter((c) => c.isConnected && c.dataset.prompt === "confirm");
const card = () => { const l = live(); return l.length ? l[l.length - 1].innerHTML : ""; };
const sidebar = () => { api.renderSessionList(); return els.sessionList.innerHTML; };
const reload = () => { api.chats.clear(); S.confirmCardEl = null; cards = []; A().owned = true; };
const flush = () => new Promise((r) => setTimeout(r, 0));
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


def test_the_card_says_what_the_agent_would_do_and_offers_both_answers():
    out = _run("""
  api.handleWsMessage(request({ kind: "destructive", reversible: false, summary: "Delete <b>old</b> runs",
                                detail: "rm -rf /dfs3b/x" }));
  out.cards = live().length; out.html = card(); out.sidebar = sidebar();
""")
    assert out["cards"] == 1
    html = out["html"]
    assert "Delete or overwrite on HPC3?" in html
    assert "Delete &lt;b&gt;old&lt;/b&gt; runs" in html and "<b>old</b>" not in html
    assert '<pre class="confirm-detail">rm -rf /dfs3b/x</pre>' in html
    assert "This cannot be undone." in html and "counts as no." in html
    assert 'data-confirm="approve">Approve</button>' in html and 'data-confirm="decline">Decline</button>' in html
    assert "Needs your OK" in out["sidebar"]


def test_it_waits_in_its_own_chat_and_comes_back_when_that_chat_is_shown():
    out = _run("""
  S.activeId = "B"; api.renderChat();
  api.handleWsMessage(request());
  out.inB = live().length; out.toast = log.filter((x) => x.startsWith("toast:"));
  out.sidebar = sidebar();
  S.activeId = "A"; api.renderChat();
  out.inA = live().length; out.html = card();
  // Switching away and back: the card is drawn again from the chat's record.
  S.activeId = "B"; api.renderChat(); S.activeId = "A"; api.renderChat();
  out.again = live().length;
""")
    assert out["inB"] == 0
    assert out["toast"] == ["toast:“Owner chat” needs your OK — open that chat to answer."]
    assert "Needs your OK" in out["sidebar"]
    assert out["inA"] == 1 and "Install software on HPC3?" in out["html"]
    assert out["again"] == 1


def test_sent_again_it_is_not_drawn_twice_and_after_a_reload_it_comes_back():
    out = _run("""
  api.handleWsMessage(request());
  api.handleWsMessage(request({ timeout_s: undefined, seconds_left: 120 }));   // a reconnect
  out.cards = live().length; out.toasts = log.filter((x) => x.startsWith("toast:")).length;
  reload(); api.renderChat();
  out.afterReload = live().length;
  api.handleWsMessage(request({ timeout_s: undefined, seconds_left: 90 }));
  out.replayed = live().length;
""")
    assert out["cards"] == 1 and out["toasts"] == 0
    assert out["afterReload"] == 0 and out["replayed"] == 1


def test_an_answer_posts_the_runs_identity_and_closes_the_card():
    out = _run("""
  api.handleWsMessage(request());
  await api.answerConfirm(A(), true);
  out.posts = posts; out.html = card(); out.held = A().confirm; out.sidebar = sidebar();
  api.handleWsMessage({ type: "confirm_done", conversation_id: "A", run_id: "r1", outcome: "approved" });
  out.after = card();
  api.renderChat();
  out.rebuilt = live().length;
""")
    assert out["posts"] == [{"url": "/api/confirm", "body": {"connection_id": "conn", "approved": True,
                                                              "run_id": "r1", "conversation_id": "A"}}]
    assert "✓ Approved — the agent continues." in out["html"] and "data-confirm" not in out["html"]
    assert out["held"] is None and "Needs your OK" not in out["sidebar"]
    assert out["after"] == out["html"]
    assert out["rebuilt"] == 0, "an answered request is not drawn again"


def test_a_failed_answer_brings_the_buttons_back():
    out = _run("""
  api.handleWsMessage(request());
  reply = "down";
  await api.answerConfirm(A(), false);
  out.html = card(); out.answer = A().confirm && A().confirm.answer;
  out.toast = log.filter((x) => x.startsWith("toast:Could not send"));
  reply = { ok: true, status: 200, body: { status: "stale", approved: false } };
  await api.answerConfirm(A(), false);
  out.stale = card(); out.heldAfterStale = A().confirm;
""")
    assert 'data-confirm="approve"' in out["html"] and out["answer"] is None
    assert out["toast"] == ["toast:Could not send your answer: Failed to fetch"]
    assert "This request is no longer waiting." in out["stale"] and out["heldAfterStale"] is None


@pytest.mark.parametrize("outcome,note", [
    ("timeout", "No answer in time — counted as no"),
    ("stopped", "The run stopped — nothing was done."),
    ("approved", "✓ Approved — the agent continues."),     # answered in another window
])
def test_done_closes_the_card_with_what_became_of_it(outcome, note):
    out = _run(f"""
  api.handleWsMessage(request());
  api.handleWsMessage({{ type: "confirm_done", conversation_id: "A", run_id: "r1", outcome: "{outcome}" }});
  out.html = card(); out.held = A().confirm;
""")
    assert note in out["html"] and "data-confirm" not in out["html"] and out["held"] is None


def test_the_end_of_the_run_closes_a_card_still_waiting():
    out = _run("""
  api.handleWsMessage({ type: "chat_start", conversation_id: "A", run_id: "r1" });
  api.handleWsMessage(request());
  out.before = live().length;
  api.handleWsMessage({ type: "chat_done", conversation_id: "A", run_id: "r1" });
  out.html = card(); out.held = A().confirm; out.sidebar = sidebar();
  api.renderChat();
  out.rebuilt = live().length;
""")
    # The chat is rebuilt around the run's answer: the card goes with it and is not drawn again.
    assert out["before"] == 1 and out["held"] is None
    assert "data-confirm" not in out["html"] and out["rebuilt"] == 0
    assert "Needs your OK" not in out["sidebar"]
