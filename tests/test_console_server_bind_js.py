"""Server-path binding feedback and chat ownership, run under node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"

HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const SECOND = "__SECOND__";   // a second Bind: "", "after_card" (the card waits) or "in_flight" (both checks run)
const messages = [], posts = [], cards = [];
let nativeConfirmCalls = 0, listRenders = 0;
const document = { createElement: () => ({ dataset: {}, innerHTML: "", isConnected: true,
  remove() { this.isConnected = false; },
  querySelectorAll() {
    this.buttons = ["approve", "decline"].map((answer) => ({ dataset: { confirm: answer },
      addEventListener(event, handler) { this.click = handler; } }));
    return this.buttons;
  },
}) };
const stream = { querySelector: () => null, appendChild: (el) => cards.push(el) };
const chats = new Map([ ["A", { sid: "A", serverBoundTyped: new Set() }], ["B", { sid: "B", serverBoundTyped: new Set() }] ]);
chats.get("A").bound = __BOUND__;
chats.get("B").bound = ["/server"];
const titles = { A: "Retina QC ", B: "Other chat" };
const state = { activeId: "A", connectionId: "conn" };
const sandbox = {
  state, JSON, String, Error, document, chats,
  chatTitle: (sid) => titles[sid],
  renderSessionList: () => { listRenders++; },
  $: () => stream,
  escapeHtml: (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"),
  activeChat: () => chats.get(state.activeId),
  chatVisible: (c) => c === chats.get(state.activeId),
  chatOf: (sid) => chats.get(sid),
  toast: (text) => messages.push(text),
  setUploadProgress() {}, renderServerPathOffer() {},
  isFolderKind: () => false, addDataset() {},
  confirm: () => { nativeConfirmCalls++; throw new Error("Native dialogs are blocked"); },
  fetch: async (url) => {
    posts.push(url);
    if (url.endsWith("/check")) {
      state.activeId = "B"; // leaving the chat while its check is in flight
      return { ok: true, json: async () => ({ path: __RESOLVED__, confirm: __LARGE__, message: "Copy 50 GB\nSkip <cache> & temporary files" }) };
    }
    return { ok: true, json: async () => ({ name: "sample", path: "/resolved/sample", kind: "file" }) };
  },
};
const api = vm.runInNewContext(src + "\n;({ bindServerPath, drawConfirmPrompt, confirmPromptVisible, sessionBadge })", sandbox);
(async () => {
  const pending = api.bindServerPath(__PATH__);
  let raced = null;
  if (SECOND === "in_flight") { state.activeId = "A"; raced = api.bindServerPath(__PATH__); }
  await new Promise((resolve) => setImmediate(resolve));
  const beforeAnswer = [...posts];
  const cardsInOtherChat = cards.filter((c) => c.isConnected).length;
  state.activeId = "A";
  api.drawConfirmPrompt();
  const pendingVisible = api.confirmPromptVisible();
  const card = cards.find((c) => c.isConnected);
  const html = card ? card.innerHTML : "";
  const badgeWhileWaiting = api.sessionBadge("A");
  let second = null;
  if (SECOND === "after_card") second = await api.bindServerPath("/server/sample");
  const racedResult = raced && await raced;
  if (__LARGE__) {
    if (!card) throw new Error("Large copy did not show a confirmation card");
    // Leaving and reopening the originating chat restores the pending choice.
    state.activeId = "B"; api.drawConfirmPrompt();
    state.activeId = "A"; api.drawConfirmPrompt();
    const restored = cards.filter((c) => c.isConnected).pop();
    restored.buttons[__CONFIRM__ ? 0 : 1].click();
  }
  const result = await pending;
  const cardsLeft = cards.filter((c) => c.isConnected).length;
  console.log(JSON.stringify({ result, messages, posts, html, beforeAnswer, cardsInOtherChat, nativeConfirmCalls, pendingVisible,
    badgeWhileWaiting, badgeAfter: api.sessionBadge("A"), listRenders, second, racedResult, cardsLeft,
    a: [...chats.get("A").serverBoundTyped], b: [...chats.get("B").serverBoundTyped] }));
})().catch((err) => { console.error(err.stack); process.exit(1); });
"""


def _run(confirmed: bool, large: bool = True, second: str = "", *, path="/server/sample", bound=(),
         resolved=None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    block = (source[source.index("function sessionBadge("):source.index("// ---- chat rendering")]
             + source[source.index("const CONFIRM_TITLES"):source.index("function refreshDownloads()")]
             + source[source.index("async function bindServerPath("):source.index("// Mark/unmark one bound file")])
    done = subprocess.run(
        [node, "-e", HARNESS.replace("__CONFIRM__", str(confirmed).lower())
         .replace("__LARGE__", str(large).lower()).replace("__SECOND__", second)
         .replace("__BOUND__", json.dumps(bound)).replace("__PATH__", json.dumps(path))
         .replace("__RESOLVED__", json.dumps(resolved))],
        input=block, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


WAITS_ELSEWHERE = "“Retina QC” needs your OK to bind server data — open that chat to answer."
ALREADY_WAITING = "A Bind is already waiting for your answer in this chat."


def test_large_copy_cancel_is_visible_and_does_not_bind():
    out = _run(False)
    assert out["result"] is False
    assert out["messages"] == [WAITS_ELSEWHERE, "Bind cancelled."]
    assert out["posts"] == ["/api/server-data/check"]
    assert out["a"] == out["b"] == []


def test_binding_after_chat_switch_remembers_the_originating_chat():
    out = _run(True)
    assert out["result"] is True
    assert out["a"] == ["/server/sample"]
    assert out["b"] == []


@pytest.mark.parametrize("confirmed", [True, False])
def test_large_copy_uses_the_console_card_without_native_dialogs(confirmed):
    out = _run(confirmed)
    assert out["beforeAnswer"] == ["/api/server-data/check"]
    assert out["cardsInOtherChat"] == 0
    assert out["pendingVisible"] is True
    assert "Copy 50 GB\nSkip &lt;cache&gt; &amp; temporary files" in out["html"]
    assert 'data-confirm="approve">Bind</button>' in out["html"]
    assert 'data-confirm="decline">Cancel</button>' in out["html"]
    assert out["nativeConfirmCalls"] == 0
    assert out["posts"] == (["/api/server-data/check", "/api/server-data/bind"]
                            if confirmed else ["/api/server-data/check"])


def test_small_copy_binds_without_a_card():
    out = _run(True, large=False)
    assert out["result"] is True
    assert out["html"] == ""
    assert out["nativeConfirmCalls"] == 0
    assert out["posts"] == ["/api/server-data/check", "/api/server-data/bind"]


def test_a_bind_waiting_in_a_left_chat_is_named_and_marked_in_the_chat_list():
    out = _run(True)
    assert out["messages"][0] == WAITS_ELSEWHERE
    assert "Needs your OK" in out["badgeWhileWaiting"]
    assert out["badgeAfter"] == ""
    assert out["listRenders"] == 2   # marked when it starts waiting, cleared when answered


@pytest.mark.parametrize("confirmed", [True, False])
def test_a_second_bind_while_the_card_waits_says_so_and_does_not_scan_again(confirmed):
    out = _run(confirmed, second="after_card")
    assert out["second"] is False
    assert out["messages"][1] == ALREADY_WAITING
    assert "Bind cancelled." not in out["messages"][:2]
    # One check only; the first card still decides.
    assert out["posts"] == (["/api/server-data/check", "/api/server-data/bind"]
                            if confirmed else ["/api/server-data/check"])
    assert out["result"] is confirmed
    assert out["a"] == (["/server/sample"] if confirmed else [])


def test_two_binds_whose_checks_ran_together_leave_one_card():
    out = _run(True, second="in_flight")
    assert out["racedResult"] is False
    assert out["messages"][:2] == [WAITS_ELSEWHERE, ALREADY_WAITING]
    assert "Bind cancelled." not in out["messages"]
    assert out["posts"] == ["/api/server-data/check", "/api/server-data/check", "/api/server-data/bind"]
    assert out["result"] is True and out["cardsLeft"] == 0


@pytest.mark.parametrize("large", [True, False])
@pytest.mark.parametrize("path,bound,notice", [
    ("/server/CellQC_testdata/Sample1_WT", ["/server/CellQC_testdata/"],
     "Inside CellQC_testdata/, already bound in this chat: its libraries are QC'd once."),
    ("/server/CellQC_testdata/", ["/server/CellQC_testdata/Sample1_WT/"],
     "Contains Sample1_WT/, already bound in this chat: its libraries are QC'd once."),
])
def test_bind_message_names_overlaps_in_both_directions(large, path, bound, notice):
    out = _run(True, large=large, path=path, bound=bound)
    assert notice in out["messages"][-1]
    if large:
        assert "\n" + notice in out["html"]
    assert out["messages"][-1].count("already bound") == 1
    assert out["a"] == [path] and out["b"] == []


@pytest.mark.parametrize("bound", [[], ["/server/sample-other"], ["/server/samp"],
                                    ["/server/sample/"], ["/elsewhere/sample/child"]])
def test_bind_overlap_uses_path_boundaries_and_the_originating_chat(bound):
    out = _run(True, bound=bound)
    assert "already bound" not in out["html"]
    assert "already bound" not in out["messages"][-1]


def test_overlap_compares_the_resolved_path_and_escapes_the_confirmation():
    notice = "Inside data&lt;old&gt;&amp;/, already bound in this chat: its libraries are QC'd once."
    out = _run(True, path="/alias/sample", resolved="/server/data<old>&/sample",
               bound=["/server/data<old>&"])
    assert notice in out["html"]
    assert "Inside data<old>&/" in out["messages"][-1]
