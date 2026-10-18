"""The console's per-chat dialog state, run under node.

Every chat has its own dialog — what is attached for its next run, the case note, the plan card it
waits on — and its own run. The code that draws the composer and the plan card reads these off
``state``; they must be the ACTIVE chat's, so a chat opened while another one uploads or runs
starts empty. This runs the per-chat module of ``frontend/console/app.js`` under node with the
page's globals stubbed, like ``test_console_plan_card_js.py``. Skipped where node is not installed.
"""

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
const store = {};
const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; } };
const sandbox = { localStorage, JSON, String, Array, Object,
  state: { activeId: "A", connectionId: "conn-1", sessions: [{ id: "A", title: "Retina" }, { id: "B", title: "" }] },
  RUNOWNER_KEY: "bioagent_runowner_v1", STREAMS_KEY: "aiscientist_streams_v1", COMPOSER_KEY: "aiscientist_composer_v1" };
const api = vm.runInNewContext(src + "\n;({ chatOf, activeChat, chatVisible, chatTitle, saveRunOwners, loadRunOwners, seenStream, rememberStream })", sandbox);
const st = sandbox.state;
const out = {};
st.boundPaths.push("/data/a.h5ad");
st.boundRoles["/data/a.h5ad"] = "reference";
st.caseNote = { name: "note.txt", text: "night blindness" };
st.planDoc = { yaml: "plan: 1\n" };
out.a = { bound: [...st.boundPaths], note: st.caseNote && st.caseNote.name, planDoc: st.planDoc };
st.activeId = "B";                                   // a new chat: nothing carried over
out.b = { bound: [...st.boundPaths], roles: { ...st.boundRoles }, note: st.caseNote, planDoc: st.planDoc,
          problems: st.planProblems, running: st.running, pending: st.planPending };
st.activeId = "A";
out.backOnA = { bound: [...st.boundPaths], note: st.caseNote && st.caseNote.name };
out.titles = [api.chatTitle("A"), api.chatTitle("B")];
const a = api.chatOf("A"), b = api.chatOf("B");
a.owned = true; a.running = true; b.owned = true; b.planPending = true;
api.chatOf("C").owned = true;                        // owned, but nothing in flight
api.saveRunOwners();
out.owners = JSON.parse(store.bioagent_runowner_v1);
out.loaded = api.loadRunOwners("conn-1");
out.otherConn = api.loadRunOwners("conn-2");
store.bioagent_runowner_v1 = JSON.stringify({ connId: "conn-1", sid: "legacy" });   // an older page's record
out.legacy = api.loadRunOwners("conn-1");
a.running = false; b.planPending = false;
api.saveRunOwners();
out.cleared = store.bioagent_runowner_v1 === undefined;
out.unseen = api.seenStream("s1");
api.rememberStream("s1");
out.seen = api.seenStream("s1");
console.log(JSON.stringify(out));
"""


def _run() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    block = source[source.index("const chats = new Map();"):source.index("function toast(")]
    done = subprocess.run([node, "-e", HARNESS], input=block, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_new_chat_starts_with_an_empty_dialog():
    out = _run()
    assert out["a"]["bound"] == ["/data/a.h5ad"] and out["a"]["note"] == "note.txt"
    assert out["b"] == {"bound": [], "roles": {}, "note": None, "planDoc": None, "problems": [],
                        "running": False, "pending": False}
    assert out["backOnA"] == {"bound": ["/data/a.h5ad"], "note": "note.txt"}    # A kept its own
    assert out["titles"] == ["Retina", "New chat"]


def test_the_chats_with_a_run_in_flight_survive_a_reload():
    out = _run()
    assert out["owners"] == {"connId": "conn-1", "sids": ["A", "B"]}           # running, or waiting on a plan
    assert out["loaded"] == ["A", "B"] and out["otherConn"] == []
    assert out["legacy"] == ["legacy"]                                          # an older page's {sid}
    assert out["cleared"] is True                                               # nothing in flight: no record


def test_a_saved_answer_is_recognised_when_replayed():
    out = _run()
    assert out["unseen"] is False and out["seen"] is True
