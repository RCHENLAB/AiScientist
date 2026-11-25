"""The console's decision card after the run stops waiting for it, and its "Abort the run", under node.

Run 6e5a33230a46 (RCHENLAB/AiScientist issue 154, item 1): a failed step's decision card timed out,
the run went on with the agent's judgment, and the card stayed on screen with live options, drawn
again whenever the chat was opened. "Abort the run" clicked on it reached nobody and the run went on.
The server now closes a lapsed card (``plan_done``) and treats an Abort as Stop; here the page drops
the options of a decision it was not answered on, and shows an Abort as the run stopping.
The server side is in ``test_cellqc_mixed_species.py``. Skipped where node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _function(js: str, head: str) -> str:
    start = js.index(head)
    return js[start:js.index("\n}\n", start) + 3]


_HARNESS = r"""
const log = [];
const toasts = [];
class El {
  constructor(cls) { this.className = cls || ""; this.dataset = {}; this.children = []; this.textContent = ""; this.parent = null; }
  querySelector(sel) { return this.children.find((c) => "." + c.className === sel) || null; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  replaceWith(n) { const i = this.parent.children.indexOf(this); this.parent.children[i] = n; n.parent = this.parent; }
}
const document = { createElement: () => new El() };
const stopBtn = { disabled: false, textContent: "■ Stop" };
const $ = (id) => (id === "chatStop" ? stopBtn : { placeholder: "" });
const DEFAULT_PLACEHOLDER = "Ask";
const state = { connectionId: "conn-1", planCardEl: null };
let visible = true;
const chatVisible = () => visible;
const releasePlanPrompt = (c) => { c.plan = null; c.planPending = false; };
const removePlanCard = () => log.push("removePlanCard");
const drawAnsweredPlan = () => log.push("drawAnsweredPlan");
const renderPlanPanel = () => log.push("renderPlanPanel");
const updateComposerButton = () => {};
const finalizePlanCard = (note) => log.push("finalize:" + note);
const mountStream = () => {};
const repaintWorking = () => {};
const toast = (t) => toasts.push(t);
const planNotDelivered = () => log.push("notDelivered");
const activeChat = () => null;
let reply = { status: "ok", action: "revise" };
const fetch = async (url, opts) => {
  log.push(url + " " + opts.body);
  const body = reply;
  const res = { ok: true, json: async () => body, clone: () => ({ json: async () => body }) };
  return res;
};
%s

(async () => {
  const out = {};
  // A decision held on this page that the server closed (timed out, or the run stopped).
  const card = new El("msg assistant plan-card"); card.dataset.prompt = "decision";
  const opts = card.appendChild(new El("clarify-options"));
  opts.appendChild(new El("clarify-chip"));
  state.planCardEl = card;
  const c = { sid: "B2", plan: { kind: "decision", goal: "Step failed", options: ["Abort the run"] } };
  hidePlanPanel(c);
  out.lapsed = { plan: c.plan, kids: card.children.map((k) => [k.className, k.textContent]), held: state.planCardEl };

  // An answer this page sent itself: the card was finalized when it was clicked; plan_done changes nothing.
  const card2 = new El("msg assistant plan-card"); card2.dataset.prompt = "decision";
  card2.appendChild(new El("clarify-options"));
  state.planCardEl = card2;
  hidePlanPanel({ sid: "B2", plan: null });
  out.answered = card2.children.map((k) => k.className);

  // Abort: the server answers "stopping"; the page shows the run stopping, not "continuing".
  reply = { status: "stopping", action: "abort" };
  await submitDecision("Abort the run", { sid: "B2", run: {} });
  out.abort = { toasts: toasts.slice(), stop: [stopBtn.disabled, stopBtn.textContent] };
  toasts.length = 0;
  reply = { status: "ok", action: "revise" };
  await submitDecision("Lower the resolution to 0.5", { sid: "B2", run: {} });
  out.choice = toasts.slice();
  out.posts = log.filter((l) => l.startsWith("/api/lab/plan")).map((l) => JSON.parse(l.split(" ").slice(1).join(" ")));
  console.log(JSON.stringify(out));
})();
"""


def _run() -> dict:
    js = APP_JS.read_text(encoding="utf-8")
    code = _HARNESS % (_function(js, "function hidePlanPanel(") + _function(js, "async function submitDecision(")
                       + _function(js, "async function planAnswerStale("))
    done = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_decision_the_run_no_longer_waits_for_loses_its_options():
    out = _run()
    assert out["lapsed"]["plan"] is None
    assert out["lapsed"]["kids"] == [["plan-actions-status", "This decision is no longer waiting for an answer."]]
    assert out["lapsed"]["held"] is None
    assert out["answered"] == ["clarify-options"], "a card answered on this page is left as it was"


def test_abort_shows_the_run_stopping_and_a_choice_shows_it_continuing():
    out = _run()
    assert out["abort"]["toasts"] == ["Aborting — the run is ending…"]
    assert out["abort"]["stop"] == [True, "■ Stopping…"]
    assert out["choice"] == ["Decision recorded — continuing…"]
    assert [p["feedback"] for p in out["posts"]] == ["Abort the run", "Lower the resolution to 0.5"]
    assert all(p["conversation_id"] == "B2" and p["action"] == "revise" for p in out["posts"])
