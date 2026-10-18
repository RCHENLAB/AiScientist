"""Each chat keeps its own composer and its own uploads, under node (RCHENLAB/AiScientist issue 7).

A tester started a folder upload in one chat, made a new chat, and the new chat showed the other
chat's "Uploading Sample1_WT/ … Cancel upload". Every chat must keep its own upload line, draft,
attached data and run options ("Plan first", "Bypass", "Save results to my data", mode, engine), a
switch of chats must not stop an upload, and after a reload every chat shows its own state and no
other's. This runs those parts of ``frontend/console/app.js`` with the page's globals stubbed, like
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
# The composer: draft, attached data and run options are the chat's own, and survive a reload.

COMPOSER = ELEMENTS + r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const sandbox = { localStorage, JSON, String, Array, Object, Set,
  state: { activeId: "A", connectionId: "conn-1",
           sessions: [{ id: "A", title: "Retina" }, { id: "B", title: "" }, { id: "C", title: "" }, { id: "D", title: "" }] },
  RUNOWNER_KEY: "owners", STREAMS_KEY: "streams", COMPOSER_KEY: "composer", UI_KEY: "ui",
  $: el, syncDatasetSelection() {}, renderUploadLine() {}, renderCaseNoteChip() {}, renderContextMeter() {},
  hideContextMeter() {}, updateComposerButton() {} };
const api = vm.runInNewContext(src + `
;({ chats, chatOf, saveComposer, restoreChatModes, pruneComposers, forgetComposer, startComposer,
    chatSavesResults, syncRunOptions, onRunOptionChange, leaveDialog, showDialog })`, sandbox);
const S = sandbox.state;
const screen = () => ({ text: el("chatInput").value, planFirst: el("planMode").checked,
                        bypass: el("autonomousMode").checked, save: el("saveResults").checked });
const tick = (id, on) => { el(id).checked = on; api.onRunOptionChange(); };
const out = {};
// Chat A: a question half typed, a file attached as reference data, every option changed.
api.showDialog();
el("chatInput").value = "Compare rods and cones";
S.boundPaths.push("/data/retina.h5ad", "/data/atlas.h5ad");
S.boundRoles["/data/atlas.h5ad"] = "reference";
S.boundRoles["/data/gone.h5ad"] = "reference";            // a role left from a file no longer attached
api.saveComposer(api.chatOf("A"));
tick("planMode", false); tick("autonomousMode", true); tick("saveResults", true);
out.onA = screen();
// "+" under CHATS: the new chat starts clean.
api.leaveDialog(); S.activeId = "B"; api.showDialog();
out.onB = { ...screen(), bound: [...S.boundPaths] };
el("chatInput").value = "Only B's words";
api.leaveDialog(); S.activeId = "A"; api.showDialog();
out.backOnA = { ...screen(), bound: [...S.boundPaths] };
// "Save results" is a standing choice for a NEW chat; from then on each chat keeps its own.
store.ui = JSON.stringify({ saveResults: true });
api.startComposer(api.chatOf("C"));
store.ui = JSON.stringify({ saveResults: false });
out.saves = { C: api.chatSavesResults(api.chatOf("C")), D: api.chatSavesResults(api.chatOf("D")) };
// Mode and engine are kept on the session; a corpus scan is one action, so its chat comes back on
// the engine it was using.
S.sessions[0].mode = "team"; S.sessions[0].route = "corpus"; S.sessions[0].routeBeforeCorpus = "chat";
api.saveComposer(api.chatOf("A"));
api.leaveDialog();
out.stored = JSON.parse(store.composer);
// A reload: the page's memory is gone, the browser's storage is not.
api.chats.clear();
const sessions = [{ id: "A" }, { id: "B" }, { id: "C" }, { id: "Z" }];
store.composer = JSON.stringify({ ...JSON.parse(store.composer), Z: { route: "corpus", mode: "nonsense", draft: "z" } });
api.restoreChatModes(sessions);
out.modes = sessions.map((s) => [s.id, s.mode || null, s.route || null]);
S.activeId = "A"; api.showDialog();
out.reloadA = { ...screen(), bound: [...S.boundPaths], roles: { ...S.boundRoles } };
S.activeId = "B"; api.showDialog();
out.reloadB = { ...screen(), bound: [...S.boundPaths] };
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
    code = source[source.index("const chats = new Map();"):source.index("function toast(")]
    code += "".join(_function(source, name) for name in (
        "savedResultsDefault", "chatSavesResults", "startComposer", "syncRunOptions", "onRunOptionChange",
        "leaveDialog", "showDialog"))
    return _run(COMPOSER, code)


def test_a_new_chat_starts_with_an_empty_composer_and_default_options(composer):
    assert composer["onA"] == {"text": "Compare rods and cones", "planFirst": False, "bypass": True, "save": True}
    assert composer["onB"] == {"text": "", "planFirst": True, "bypass": False, "save": False, "bound": []}


def test_going_back_to_a_chat_gives_back_its_own_text_data_and_options(composer):
    assert composer["backOnA"] == {"text": "Compare rods and cones", "planFirst": False, "bypass": True,
                                   "save": True, "bound": ["/data/retina.h5ad", "/data/atlas.h5ad"]}


def test_save_results_starts_as_the_users_choice_and_then_is_the_chats(composer):
    assert composer["saves"] == {"C": True, "D": False}     # C took the choice when made; D never had one


def test_what_is_stored_is_only_what_differs_from_a_new_chat(composer):
    stored = composer["stored"]
    assert stored["A"] == {"draft": "Compare rods and cones", "bound": ["/data/retina.h5ad", "/data/atlas.h5ad"],
                           "roles": {"/data/atlas.h5ad": "reference"}, "planFirst": False, "bypass": True,
                           "saveResults": True, "mode": "team", "route": "chat"}
    assert stored["B"] == {"draft": "Only B's words"}
    assert stored["C"] == {"saveResults": True}


def test_after_a_reload_every_chat_shows_its_own_state_and_no_others(composer):
    assert composer["reloadA"] == {"text": "Compare rods and cones", "planFirst": False, "bypass": True, "save": True,
                                   "bound": ["/data/retina.h5ad", "/data/atlas.h5ad"],
                                   "roles": {"/data/atlas.h5ad": "reference"}}
    assert composer["reloadB"] == {"text": "Only B's words", "planFirst": True, "bypass": False, "save": False,
                                   "bound": []}
    # mode and engine come back on the session; a stored corpus scan or an unknown value does not
    assert composer["modes"] == [["A", "team", "chat"], ["B", None, None], ["C", None, None], ["Z", None, None]]


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
