"""The console's Stop button names its chat, run under node.

The button was wired as ``addEventListener("click", stopRun)``: the click event became ``stopRun``'s
first argument in place of the chat, so the request carried no conversation id, and with two chats
running the server stopped the newest one. Pressing Stop in one chat cancelled another chat's Slurm
job (2026-11-06). This runs ``stopRun`` from ``frontend/console/app.js`` with a click event and with
a chat, and checks the id each request sends. Skipped where node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"


def _stop_run_source(js: str) -> str:
    start = js.index("async function stopRun(")
    end = js.index("\n}\n", start) + 3
    return js[start:end]


def test_the_stop_button_does_not_hand_its_click_event_to_stop_run():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'addEventListener("click", stopRun)' not in js
    assert 'addEventListener("click", () => stopRun())' in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_stop_names_the_chat_on_screen_or_the_chat_it_is_given():
    harness = """
const calls = [];
const state = { connectionId: "conn-1", activeId: "195" };
const activeChat = () => ({ sid: state.activeId });
const chatVisible = (c) => !!c && c.sid === String(state.activeId);
const $ = () => null;
const fetch = async (url, opts) => { calls.push({ url, body: JSON.parse(opts.body) }); return {}; };
%s
(async () => {
  await stopRun({ type: "click", target: {} });   // what addEventListener hands a listener
  await stopRun();                                  // the button's listener now
  await stopRun({ sid: "196" });                    // deleting another chat stops its run
  console.log(JSON.stringify(calls));
})();
""" % _stop_run_source(APP_JS.read_text(encoding="utf-8"))
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True).stdout
    calls = json.loads(out)
    assert [c["url"] for c in calls] == ["/api/chat/stop"] * 3
    assert [c["body"].get("conversation_id") for c in calls] == ["195", "195", "196"]
    assert all(c["body"]["connection_id"] == "conn-1" for c in calls)
