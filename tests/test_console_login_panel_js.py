"""Fresh account login collapses the left panel without saving the override."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


APP = Path(__file__).resolve().parents[1] / "frontend/console/app.js"


def _run(harness: str, prefs: dict) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = APP.read_text(encoding="utf-8")
    block = (
        source[source.index("function applyUiPrefs("):source.index("// ---- auth +")]
        + source[source.index("async function bootstrap("):source.index("// Track whether the reader")]
        + source[source.index("async function doRegisterVerify("):source.index("function initShell(")]
    )
    # Use the real toolbar/sign-out listeners while stubbing unrelated shell setup.
    listeners = "\n".join(
        line for line in source.splitlines()
        if '$("toggleLeft").addEventListener' in line
        or '$("leftLogoutBtn").addEventListener' in line
    )
    prelude = r'''
const assert = require("assert");
const els = {};
const $ = (id) => els[id] || (els[id] = {
  value: "test", textContent: "", listeners: {},
  closest() { return null; },
  addEventListener(event, handler) { this.listeners[event] = handler; },
  classList: {
    values: new Set(),
    add(c) { this.values.add(c); },
    remove(c) { this.values.delete(c); },
    contains(c) { return this.values.has(c); },
    toggle(c, on) {
      if (on === undefined) on = !this.contains(c);
      if (on) this.add(c); else this.remove(c);
    },
  },
});
const document = { querySelectorAll: () => [] };
const UI_KEY = "ui", COMPOSER_KEY = "composer";
const stored = new Map(), writes = [], requests = [];
const localStorage = {
  getItem: (key) => stored.get(key) || null,
  setItem(key, value) { writes.push(key); stored.set(key, value); },
  removeItem: (key) => stored.delete(key),
};
let _appInited = false, _regEmail = "test@example.com", reloaded = false;
const location = { reload() { reloaded = true; } };
const state = { activeId: "chat1", planView: { id: "plan1" } };
const chat = { messages: ["question", "answer"], plan: { steps: ["run"] } };
const chats = new Map([["chat1", chat]]);
const initShell = () => {};
const wireRegister = () => {}, wireChatScroll = () => {};
const renderAccount = () => {}, switchView = () => {}, checkBudgetNotices = () => {};
const user = { username: "tester", role: "user" };
let response = { ok: true, json: async () => ({ user }) };
let fetch = async (url) => { requests.push(url); return response; };
const event = { preventDefault() {} };
'''
    script = (
        prelude
        + "\nstored.set(UI_KEY, " + json.dumps(json.dumps(prefs)) + ");\n"
        + "function init() { applyUiPrefs();\n" + listeners + "\n}\n"
        + block
        + "\n(async () => {\n" + harness
        + "\n})().catch((err) => { console.error(err); process.exit(1); });"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert out.returncode == 0, out.stderr or out.stdout


@pytest.mark.parametrize("handler", ["doLogin", "doRegisterVerify"])
@pytest.mark.parametrize("left", [None, True, False])
def test_fresh_login_collapses_without_saving_and_toggle_preserves_chat(handler, left):
    prefs = {"right": True}
    if left is not None:
        prefs["left"] = left
    _run(r'''
// Initialization is allowed to apply/save the existing preferences.
init(); _appInited = true;
const saved = stored.get(UI_KEY);
writes.length = 0;
const before = JSON.stringify({ state, chats: [...chats] });
await HANDLER(event);
assert.strictEqual(state.user, user);
assert($("layout").classList.contains("collapse-left"));
assert(!$("toggleLeft").classList.contains("active"));
assert(!$("leftLogoutBtn").classList.contains("hidden"));
assert.strictEqual(stored.get(UI_KEY), saved);
assert.deepStrictEqual(writes, []);
$("toggleLeft").listeners.click();
assert(!$("layout").classList.contains("collapse-left"));
assert($("toggleLeft").classList.contains("active"));
assert.strictEqual(JSON.parse(stored.get(UI_KEY)).left, true);
$("toggleLeft").listeners.click();
$("toggleLeft").listeners.click();
const { user: signedIn, ...rest } = state;
assert.strictEqual(JSON.stringify({ state: rest, chats: [...chats] }), before);
await $("leftLogoutBtn").listeners.click();
assert(requests.includes("/api/auth/logout"));
assert(reloaded);
'''.replace("HANDLER", handler), prefs)


@pytest.mark.parametrize("handler", ["doLogin", "doRegisterVerify"])
def test_first_login_collapses_after_initial_preferences(handler):
    _run(r'''
await HANDLER(event);
assert(_appInited);
assert($("layout").classList.contains("collapse-left"));
assert.strictEqual(JSON.parse(stored.get(UI_KEY)).left, true);
'''.replace("HANDLER", handler), {"left": True})


@pytest.mark.parametrize("left", [True, False])
@pytest.mark.parametrize("accounts_enabled", [True, False])
def test_bootstrap_keeps_saved_preference_on_session_restore(left, accounts_enabled):
    _run(r'''
response = { ok: ENABLED, json: async () => ({ authenticated: true, user }) };
await bootstrap();
assert.strictEqual($("layout").classList.contains("collapse-left"), !LEFT);
assert.strictEqual(JSON.parse(stored.get(UI_KEY)).left, LEFT);
'''.replace("ENABLED", str(accounts_enabled).lower()).replace("LEFT", str(left).lower()),
         {"left": left})


@pytest.mark.parametrize("handler", ["doLogin", "doRegisterVerify"])
@pytest.mark.parametrize("failure", ["rejected", "network"])
def test_failed_account_login_does_not_collapse(handler, failure):
    _run(r'''
init(); _appInited = true;
const saved = stored.get(UI_KEY);
writes.length = 0;
response = { ok: false, json: async () => ({ detail: "Rejected" }) };
if ("FAILURE" === "network") fetch = async () => { throw new Error("offline"); };
await HANDLER(event);
assert(!$("layout").classList.contains("collapse-left"));
assert.strictEqual(stored.get(UI_KEY), saved);
assert.deepStrictEqual(writes, []);
'''.replace("HANDLER", handler).replace("FAILURE", failure), {"left": True})
