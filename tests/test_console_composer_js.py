"""Each chat keeps its own composer and its own uploads, under node (RCHENLAB/AiScientist issue 7).

A tester started a folder upload in one chat, made a new chat, and the new chat showed the other
chat's "Uploading Sample1_WT/ … Cancel upload". Every chat must keep its own upload line, draft,
attached data and engine, a switch of chats must not stop an upload, and after a reload every chat
shows its own state and no other's. The run options are no longer the chat's: every request runs
with the same ones (issue 21), and what an older page stored of them is dropped. This runs those
parts of ``frontend/console/app.js`` with the page's globals stubbed, like
``test_console_chat_state_js.py``. Skipped where node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"


def _function(source: str, name: str) -> str:
    """The whole text of ``function name(…) { … }`` (or ``async function``), by matching braces."""
    start = source.index(f"function {name}(")
    if source[start - 6:start] == "async ":
        start -= 6
    depth, i = 0, source.index("{", source.index(")", start))
    while True:
        ch = source[i]
        depth += ch == "{"
        depth -= ch == "}"
        i += 1
        if depth == 0:
            return source[start:i] + "\n"


def _run(harness: str, code: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "-e", harness], input=code, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


ELEMENTS = r"""
const els = {};
const el = (id) => els[id] || (els[id] = { id, value: "", checked: false, textContent: "", disabled: false,
  classes: new Set(["hidden"]),
  get classList() { const c = this.classes; return { add: (k) => c.add(k), remove: (k) => c.delete(k),
    contains: (k) => c.has(k), toggle: (k, on) => { if (on === undefined ? !c.has(k) : on) c.add(k); else c.delete(k); } }; } });
const store = {};
const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; } };
"""

# --------------------------------------------------------------------------------------------------
# The composer: draft and attached data are the chat's own, and survive a reload.

COMPOSER = ELEMENTS + r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
// What an older page stored for chat C: its run options and its engine, beside a draft.
store.composer = JSON.stringify({ C: { draft: "old words", planFirst: false, bypass: true, saveResults: true, mode: "team", route: "chat" } });
const sandbox = { localStorage, JSON, String, Array, Object, Set,
  state: { activeId: "A", connectionId: "conn-1",
           sessions: [{ id: "A", title: "Retina" }, { id: "B", title: "" }, { id: "C", title: "" }, { id: "D", title: "" }] },
  RUNOWNER_KEY: "owners", STREAMS_KEY: "streams", COMPOSER_KEY: "composer", UI_KEY: "ui",
  $: el, syncDatasetSelection() {}, renderUploadLine() {}, renderContextMeter() {},
  hideContextMeter() {}, updateComposerButton() {} };
const api = vm.runInNewContext(src + `
;({ chats, chatOf, saveComposer, pruneComposers, forgetComposer, leaveDialog, showDialog,
    FIXED_RUN_OPTIONS })`, sandbox);
const S = sandbox.state;
const out = {};
out.fixed = api.FIXED_RUN_OPTIONS;
// Chat A: a question half typed, a file attached as reference data.
api.showDialog();
el("chatInput").value = "Compare rods and cones";
S.boundPaths.push("/data/retina.h5ad", "/data/atlas.h5ad");
S.boundRoles["/data/atlas.h5ad"] = "reference";
S.boundRoles["/data/gone.h5ad"] = "reference";            // a role left from a file no longer attached
api.saveComposer(api.chatOf("A"));
out.onA = { text: el("chatInput").value };
// "+" under CHATS: the new chat starts clean.
api.leaveDialog(); S.activeId = "B"; api.showDialog();
out.onB = { text: el("chatInput").value, bound: [...S.boundPaths] };
el("chatInput").value = "Only B's words";
api.leaveDialog(); S.activeId = "A"; api.showDialog();
out.backOnA = { text: el("chatInput").value, bound: [...S.boundPaths] };
// Chat C, from the older page: its draft comes back, its run options do not, and its next save drops them.
api.leaveDialog(); S.activeId = "C"; api.showDialog();
out.onC = { text: el("chatInput").value, keys: Object.keys(api.chatOf("C")).filter((k) => ["planFirst", "bypass", "saveResults"].includes(k)) };
api.leaveDialog(); S.activeId = "A"; api.showDialog();
// A session that still carries an engine from an older page: the composer stores none.
S.sessions[0].mode = "team"; S.sessions[0].route = "chat";
api.saveComposer(api.chatOf("A"));
api.leaveDialog();
out.stored = JSON.parse(store.composer);
// A reload: the page's memory is gone, the browser's storage is not.
api.chats.clear();
store.composer = JSON.stringify({ ...JSON.parse(store.composer), Z: { route: "corpus", mode: "team", draft: "z" } });
S.activeId = "A"; api.showDialog();
out.reloadA = { text: el("chatInput").value, bound: [...S.boundPaths], roles: { ...S.boundRoles } };
S.activeId = "B"; api.showDialog();
out.reloadB = { text: el("chatInput").value, bound: [...S.boundPaths] };
// A chat with nothing of its own keeps no entry; deleted chats and other accounts' chats go.
api.chatOf("B").draft = ""; api.saveComposer(api.chatOf("B"));
api.pruneComposers([{ id: "A" }, { id: "B" }, { id: "C" }]);
out.afterPrune = Object.keys(JSON.parse(store.composer)).sort();
api.forgetComposer("A");
out.afterForget = Object.keys(JSON.parse(store.composer)).sort();
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def composer():
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("const FIXED_RUN_OPTIONS")
    code = source[start:source.index(");\n", start) + 3]
    code += source[source.index("const chats = new Map();"):source.index("function toast(")]
    code += "".join(_function(source, name) for name in ("leaveDialog", "showDialog"))
    return _run(COMPOSER, code)


def test_every_request_is_sent_with_the_same_run_options(composer):
    """Plan review, no Bypass, Auto, nothing required by hand, results saved (issue 21)."""
    assert composer["fixed"] == {"plan_mode": True, "autonomous": False, "mode": "auto", "save_results": True,
                                 "presets": [], "skills": [], "preset_prompt": None, "route": "auto"}


def test_a_new_chat_starts_with_an_empty_composer(composer):
    assert composer["onA"] == {"text": "Compare rods and cones"}
    assert composer["onB"] == {"text": "", "bound": []}


def test_going_back_to_a_chat_gives_back_its_own_text_and_data(composer):
    assert composer["backOnA"] == {"text": "Compare rods and cones", "bound": ["/data/retina.h5ad", "/data/atlas.h5ad"]}


def test_run_options_and_engine_an_older_page_stored_are_not_read_and_are_dropped(composer):
    assert composer["onC"] == {"text": "old words", "keys": []}
    assert composer["stored"]["C"] == {"draft": "old words"}


def test_what_is_stored_is_only_what_differs_from_a_new_chat(composer):
    stored = composer["stored"]
    assert stored["A"] == {"draft": "Compare rods and cones", "bound": ["/data/retina.h5ad", "/data/atlas.h5ad"],
                           "roles": {"/data/atlas.h5ad": "reference"}}
    assert stored["B"] == {"draft": "Only B's words"}


def test_after_a_reload_every_chat_shows_its_own_state_and_no_others(composer):
    assert composer["reloadA"] == {"text": "Compare rods and cones", "bound": ["/data/retina.h5ad", "/data/atlas.h5ad"],
                                   "roles": {"/data/atlas.h5ad": "reference"}}
    assert composer["reloadB"] == {"text": "Only B's words", "bound": []}


def test_entries_of_chats_that_are_gone_are_dropped(composer):
    assert composer["afterPrune"] == ["A", "C"]              # B has nothing of its own; Z is not a chat any more
    assert composer["afterForget"] == ["C"]


# --------------------------------------------------------------------------------------------------
# The upload line and its Cancel button: only in the chat that started the upload.

UPLOADS = ELEMENTS + r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const aborted = [];
const card = (id, chat, status = "uploading") => ({ id, chat, status, name: id, ctrl: { abort: () => aborted.push(id) } });
const opened = [];
const sandbox = { $: el, JSON, String, Array, Object, Map, Set,
  state: { activeId: "A", uploads: [], sessions: [{ id: "A", title: "Retina" }, { id: "B", title: "" }] },
  switchView: (v) => opened.push("view:" + v), selectSession: (sid) => opened.push("chat:" + sid) };
const api = vm.runInNewContext(src + `
;({ setUploadProgress, renderUploadLine, cancelActiveUploads, chatUploads, uploadChatField, chatLabel, openChat })`, sandbox);
const S = sandbox.state;
const line = () => ({ text: el("uploadStatus").classList.contains("hidden") ? "" : el("uploadStatus").textContent,
                      cancel: !el("uploadCancel").classList.contains("hidden") });
const on = (sid) => { S.activeId = sid; api.renderUploadLine(); return line(); };
const out = {};
// Chat A uploads a folder.
S.uploads.push(card("F1", "A"));
api.setUploadProgress("F1", "Uploading Sample1_WT/ — 12/13 files · 11.1 MB/s · 7 min left");
out.onA = line();
// "+" under CHATS while it uploads: the new chat shows nothing of it, and its Cancel stops nothing.
S.sessions.push({ id: "C", title: "" });
out.onNew = on("C");
api.cancelActiveUploads();
out.abortedFromNew = [...aborted];
// The upload goes on meanwhile; back in A its progress is there, as it is now.
api.setUploadProgress("F1", "Uploading Sample1_WT/ — 13/13 files · 10.9 MB/s · 1 min left");
out.stillOnNew = line();
out.backOnA = on("A");
// B starts its own upload: each chat sees its own only.
S.uploads.push(card("U2", "B"));
api.setUploadProgress("U2", "Uploading counts.h5ad… 40%");
out.onB = on("B");
out.onAWithB = on("A");
// A second upload in A: one line, and how many more there are.
S.uploads.push(card("U3", "A"));
api.setUploadProgress("U3", "Uploading notes.csv… 5%");
out.twoInA = line();
// Cancel in A stops A's uploads, never B's.
api.cancelActiveUploads();
out.aborted = [...aborted];
for (const u of S.uploads) if (aborted.includes(u.id)) u.status = "cancelled";   // what failUpload makes of them
// A card restored after a reload by a server that did not know its chat is shown in no chat.
S.uploads.push(card("J1", null, "staging"));
api.setUploadProgress("J1", "Copying old.h5ad to HPC3… 50%");
api.setUploadProgress("F1", null); api.setUploadProgress("U3", null);
out.unknownChatOnA = on("A");
out.unknownChatOnB = on("B");
// "Checking /data/… on the server" belongs to the chat the path was bound in.
api.setUploadProgress("bind:A", "Checking /data/shared/run1 on the server…", "A");
out.bindOnA = on("A");
out.bindOnB = on("B");
out.fields = [api.uploadChatField("F1"), api.uploadChatField("J1")];
out.labels = [api.chatLabel("A"), api.chatLabel("Z"), api.chatLabel(null)];
S.activeId = "A"; api.openChat("B"); api.openChat("Z");
out.opened = opened;
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def uploads():
    source = APP_JS.read_text(encoding="utf-8")
    code = source[source.index("const UPLOAD_ACTIVE"):source.index("\n", source.index("const UPLOAD_ACTIVE"))] + "\n"
    code += "const uploadLines = new Map();\n"
    code += "".join(_function(source, name) for name in (
        "setUploadProgress", "chatUploads", "renderUploadLine", "cancelUpload", "cancelActiveUploads",
        "uploadChat", "uploadChatField", "chatLabel", "openChat"))
    return _run(UPLOADS, code)


def test_the_upload_line_and_cancel_are_in_the_chat_that_started_the_upload(uploads):
    assert uploads["onA"] == {"text": "Uploading Sample1_WT/ — 12/13 files · 11.1 MB/s · 7 min left", "cancel": True}


def test_a_new_chat_shows_no_other_chats_upload_and_cannot_cancel_it(uploads):
    assert uploads["onNew"] == {"text": "", "cancel": False}
    assert uploads["abortedFromNew"] == []
    assert uploads["stillOnNew"] == {"text": "", "cancel": False}


def test_switching_chats_does_not_stop_an_upload_and_coming_back_shows_its_progress(uploads):
    assert uploads["backOnA"] == {"text": "Uploading Sample1_WT/ — 13/13 files · 10.9 MB/s · 1 min left", "cancel": True}
    assert uploads["onB"] == {"text": "Uploading counts.h5ad… 40%", "cancel": True}
    assert uploads["onAWithB"]["text"].startswith("Uploading Sample1_WT/")


def test_several_uploads_in_one_chat_and_cancel_takes_only_that_chats(uploads):
    assert uploads["twoInA"]["text"].endswith("(+1 more)")
    assert uploads["aborted"] == ["F1", "U3"]


def test_an_upload_whose_chat_is_unknown_is_shown_in_no_chat(uploads):
    assert uploads["unknownChatOnA"] == {"text": "", "cancel": False}
    assert uploads["unknownChatOnB"] == {"text": "Uploading counts.h5ad… 40%", "cancel": True}


def test_a_server_path_check_is_shown_in_the_chat_it_was_asked_in(uploads):
    assert uploads["bindOnA"] == {"text": "Checking /data/shared/run1 on the server…", "cancel": False}
    assert uploads["bindOnB"]["text"] == "Uploading counts.h5ad… 40%"


def test_the_server_is_told_the_chat_and_the_datasets_page_names_it(uploads):
    assert uploads["fields"] == [{"conversation_id": "A"}, {}]
    assert uploads["labels"] == [{"text": "from “Retina”", "open": True}, {"text": "from a deleted chat", "open": False},
                                 None]
    assert uploads["opened"] == ["view:research", "chat:B"]    # a deleted chat cannot be opened
