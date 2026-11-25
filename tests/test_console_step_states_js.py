"""Step marks on the console's two plan cards and the top of the run card, run under node (#121).

RCHENLAB/AiScientist#121: while a plan runs, the chat's plan entry and the side panel's plan card both
show which step is processing and which are done, and agree; two steps that run side by side are both
processing; after a reload the marks are the same as before; a saved run keeps its final marks. The
opened run card leads with the step running now, its Slurm job and its command. These run the real
renderers of ``frontend/console/app.js`` with the page's globals stubbed. Skipped where node is missing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"

BLOCKS = [
    ("function planStepParts(text)", "function showPlanPanel("),
    ("// ---- the run's step records (RCHENLAB/AiScientist#121)", "function appendArtifacts("),
    ("function runStepsTitle(", "// The Slurm chip on the line"),
    ("function runNowParts(", "function nowHeadHtml("),
]

HARNESS = r"""
const vm = require("vm");
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const sandbox = { escapeHtml, state: { planDoc: null, planProblems: [], planDraftYaml: "" },
  renderMarkdown: (s) => escapeHtml(s), icon: (n) => `<i>${n}</i>` };
vm.createContext(sandbox);
vm.runInContext(input.code, sandbox);
const out = vm.runInContext("(() => {" + input.scenario + "})()", sandbox);
console.log(JSON.stringify(out));
"""

AGENDA = ["**Quality control** — run CellQC on each library.",
          "**Clustering** — Leiden at resolution 0.8.",
          "**Markers** — rank the marker genes.",
          "**Literature** — search the markers in PubMed."]
DOC = {"yaml": "plan: 1\n", "steps": [{"id": f"s{i}"} for i in range(1, 5)]}


def _rec(n, state, **kw):
    return {"key": f"id:s{n}", "id": f"s{n}", "step": n, "text": AGENDA[n - 1], "state": state, **kw}


def _run(scenario: str, **data) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    code = "\n".join(source[source.index(a):source.index(b, source.index(a))] for a, b in BLOCKS)
    code += "\nconst DATA = " + json.dumps({"agenda": AGENDA, "doc": DOC, **data}) + ";"
    done = subprocess.run([node, "-e", HARNESS], input=json.dumps({"code": code, "scenario": scenario}),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _rows(html: str, item: str) -> list[dict]:
    """Each step row of a card: its state class, its number cell, and its mark's label."""
    rows = []
    for li in re.findall(rf'<li class="{item}([^"]*)"[^>]*>(.*?)</li>', html, re.S):
        cls, body = li
        state = re.search(r"\bst-(\w+)", cls)
        chip = re.search(r'<span class="plan-state (\w+)"[^>]*>(?:<span class="step-spin"[^>]*></span>)?([^<]*)</span>',
                         body)
        rows.append({"state": state.group(1) if state else None,
                     "check": 'aria-label="Step' in body and ">check<" in body,
                     "chip": chip.group(1) if chip else None,
                     "spin": "step-spin" in body,
                     "label": chip.group(2) if chip else None,
                     "added": "added" in cls})
    return rows


RUNNING = [_rec(1, "done"), _rec(2, "processing"), _rec(3, "processing")]


def test_both_plan_cards_mark_the_same_steps_while_the_plan_runs():
    out = _run("""
  return { entry: planEntryHtml(DATA.agenda, DATA.doc, { status: "Approved", states: DATA.states }),
           card: planCardHtml(DATA.agenda, DATA.doc, [], { mode: "saved", status: "Approved", states: DATA.states }) };
""", states=RUNNING)
    entry, card = _rows(out["entry"], "plan-entry-step"), _rows(out["card"], "plan-step")
    assert [r["state"] for r in entry] == [r["state"] for r in card] == ["done", "processing", "processing", None]
    for rows in (entry, card):
        assert rows[0]["check"] and rows[0]["chip"] is None, "done is a check on the step's number"
        # Steps 2 and 3 run side by side: both say so, with a label and a moving indicator.
        assert [(r["chip"], r["label"], r["spin"]) for r in rows[1:3]] == [("processing", "Processing", True)] * 2
        assert (rows[3]["chip"], rows[3]["label"]) == ("pending", "Not started")
    assert "2 processing · 1 done" in out["entry"] and "2 processing · 1 done" in out["card"]


def test_a_plan_that_has_not_run_carries_no_mark_and_a_waiting_plan_none_either():
    out = _run("""
  return { entry: planEntryHtml(DATA.agenda, DATA.doc, { pending: true }),
           card: planCardHtml(DATA.agenda, DATA.doc, []),
           review: planCardHtml(DATA.agenda, DATA.doc, [], { mode: "review", states: DATA.states }) };
""", states=RUNNING)
    for html in out.values():
        assert "plan-state" not in html and " st-" not in html and "step-spin" not in html


def test_failed_skipped_stopped_and_a_step_added_during_the_run():
    added = {"key": "t:Check the doublets", "text": "**Check the doublets** — added by exploration.",
             "state": "pending", "index": 5}
    out = _run("""
  return { entry: planEntryHtml(DATA.agenda, DATA.doc, { states: DATA.states }),
           card: planCardHtml(DATA.agenda, DATA.doc, [], { mode: "saved", states: DATA.states }) };
""", states=[_rec(1, "failed", reason="no accepted result"), _rec(2, "skipped"), _rec(3, "stopped"), _rec(4, "done"), added])
    for rows in (_rows(out["entry"], "plan-entry-step"), _rows(out["card"], "plan-step")):
        assert [(r["state"], r["label"]) for r in rows] == [
            ("failed", "Failed"), ("skipped", "Skipped"), ("stopped", "Stopped"), ("done", None),
            ("pending", "Not started")]
        assert rows[4]["added"] and not any(r["added"] for r in rows[:4])
    assert "Check the doublets" in out["entry"] and "Added during the run" in out["entry"]
    assert 'title="no accepted result"' in out["entry"], "the reason is on the mark"


def test_a_saved_run_keeps_its_marks_and_a_later_plan_starts_clean():
    out = _run("""
  const plan = { kind: "plan", plan: { agenda: DATA.agenda, doc: DATA.doc, run: "r1" } };
  const later = { kind: "plan", plan: { agenda: DATA.agenda, doc: DATA.doc, run: "r2" } };
  const s = { id: "A", messages: [
    { role: "user", content: "QC these" }, plan,
    { role: "assistant", content: "Step record", streamId: "provenance:r1:s1" },
    { role: "assistant", content: "Done.", feed: [], steps: DATA.states, stepsRun: "r1" },
    { role: "user", content: "again" }, later ] };
  const liveChat = { sid: "A", run: { runId: "r2", steps: { "id:s1": DATA.states[0] } } };
  return { saved: planStatesFor(s, plan, null), laterIdle: planStatesFor(s, later, null),
           laterLive: planStatesFor(s, later, liveChat), earlierWhileLaterRuns: planStatesFor(s, plan, liveChat),
           otherChat: planStatesFor(s, later, { sid: "B", run: liveChat.run }) };
""", states=[_rec(1, "done"), _rec(2, "done"), _rec(3, "stopped")])
    assert [r["state"] for r in out["saved"]] == ["done", "done", "stopped"]
    assert out["laterIdle"] is None and out["otherChat"] is None
    assert [r["state"] for r in out["laterLive"]] == ["done"], "the plan being run now follows its run"
    assert [r["state"] for r in out["earlierWhileLaterRuns"]] == ["done", "done", "stopped"]


def test_a_run_marks_only_the_plan_it_was_approved_for():
    # A run with no plan card in this chat (a plan nobody reviewed, a run without a plan document)
    # must not draw its steps on the chat's earlier plan, live or once it is saved; nor does a plan
    # saved before it knew its run get any marks.
    out = _run("""
  const old = { kind: "plan", plan: { agenda: DATA.agenda, doc: DATA.doc, run: "r1" } };
  const legacy = { kind: "plan", plan: { agenda: DATA.agenda, doc: DATA.doc } };
  const s = { id: "A", messages: [ old, { role: "assistant", content: "Old answer." }, legacy,
    { role: "user", content: "now this" },
    { role: "assistant", content: "Done.", steps: DATA.states, stepsRun: "r9" } ] };
  const live = { sid: "A", run: { runId: "r9", steps: { "id:s1": DATA.states[0] } } };
  return { oldSaved: planStatesFor(s, old, null), oldLive: planStatesFor(s, old, live),
           legacy: planStatesFor(s, legacy, live) };
""", states=[_rec(1, "done"), _rec(2, "failed")])
    assert out == {"oldSaved": None, "oldLive": None, "legacy": None}


def test_a_continued_run_shows_its_own_marks_over_the_stopped_ones():
    # Stopped at step 2, then continued (the same run id): live, and after it ends, the plan shows the
    # continuation's marks, with the steps it did not run again as the first run left them.
    out = _run("""
  const plan = { kind: "plan", plan: { agenda: DATA.agenda, doc: DATA.doc, run: "r1" } };
  const first = { role: "assistant", content: "Stopped.", steps: DATA.first, stepsRun: "r1" };
  const s = { id: "A", messages: [ plan, first, { role: "user", content: "continue" } ] };
  const steps = {}; for (const r of DATA.second) upsertStep(steps, r);
  const live = planStatesFor(s, plan, { sid: "A", run: { runId: "r1", steps } });
  s.messages.push({ role: "assistant", content: "Done.", steps: DATA.second, stepsRun: "r1" });
  return { live: live.map((r) => r.state), after: planStatesFor(s, plan, null).map((r) => r.state) };
""", first=[_rec(1, "done"), _rec(2, "stopped")],
        second=[_rec(2, "done"), _rec(3, "processing")])
    assert out["live"] == ["done", "done", "processing"]
    assert out["after"] == ["done", "done", "processing"]


def test_a_cleared_reason_is_gone_on_the_card():
    out = _run("""
  const steps = {};
  upsertStep(steps, DATA.waiting);
  upsertStep(steps, DATA.running);
  upsertStep(steps, { type: "step_state", key: "id:s3", job_id: "7" });
  return steps["id:s3"];
""", waiting=_rec(3, "processing", reason="waiting for your choice"), running=_rec(3, "processing"))
    assert "reason" not in out and out["job_id"] == "7" and out["state"] == "processing"


def test_after_a_reload_the_marks_are_the_ones_shown_before():
    # Live: every event as it came, a job update that names only the step's key among them. A window
    # that reloads gets each step's latest record (the gateway's replay) and must show the same.
    live = [_rec(1, "processing"), _rec(1, "done"), _rec(2, "processing"),
            {"type": "step_state", "key": "id:s2", "job_id": "41872290"}, _rec(3, "processing"),
            {"type": "step_state", "key": "id:s9", "job_id": "1"}]          # no such step: ignored
    replay = [_rec(1, "done"), _rec(2, "processing", job_id="41872290"), _rec(3, "processing")]
    out = _run("""
  const draw = (events) => { const steps = {}; for (const e of events) upsertStep(steps, e);
    return { html: planEntryHtml(DATA.agenda, DATA.doc, { states: orderedSteps(steps) }), steps: orderedSteps(steps) }; };
  const before = draw(DATA.live), after = draw(DATA.replay);
  return { before: before.html, after: after.html, keys: before.steps.map((r) => r.key), job: before.steps[1].job_id };
""", live=live, replay=replay)
    assert out["before"] == out["after"]
    assert out["keys"] == ["id:s1", "id:s2", "id:s3"] and out["job"] == "41872290"


def test_the_run_card_leads_with_the_steps_running_now_their_job_and_command():
    jobs = [{"job_id": "41872290", "state": "RUNNING", "node": "hpc3-14-07"},
            {"job_id": "41872213", "state": "COMPLETED"}]
    out = _run("""
  const run = { steps: {}, jobs: DATA.jobs, commands: {
    "id:s2": { tool: "run_code", words: "run_code — Python, 2 lines", code: "import scanpy\\nprint(1)", state: "running" },
    "": { tool: "write_report", words: "write_report", code: "", state: "running" } } };
  for (const r of DATA.states) upsertStep(run.steps, r);
  const parts = runNowParts(run);
  finishSteps(run.steps);
  return { parts: parts.map((p) => ({ key: p.key, job: p.job && p.job.job_id, cmd: p.cmd && p.cmd.tool })),
           title: runStepsTitle(DATA.states, 4), one: runStepsTitle([DATA.states[1]], 4),
           none: runStepsTitle([DATA.states[0]], 4),
           added: runStepsTitle([{ key: "t:x", text: "**Doublets** — added", state: "processing" }], 4),
           ended: orderedSteps(run.steps).map((r) => r.state) };
""", jobs=jobs, states=[_rec(1, "done"), _rec(2, "processing", job_id="41872290"), _rec(3, "processing")])
    assert out["parts"] == [{"key": "id:s2", "job": "41872290", "cmd": "run_code"},
                            {"key": "id:s3", "job": None, "cmd": None},
                            {"key": "", "job": None, "cmd": "write_report"}]
    assert out["title"] == "Steps 2, 3 of 4 · Clustering, Markers"
    assert out["one"] == "Step 2 of 4 · Clustering" and out["none"] == ""
    assert out["added"] == "Added step · Doublets"
    assert out["ended"] == ["done", "stopped", "stopped"], "a step still running when the run ends is stopped"


def test_the_opened_run_card_puts_now_and_the_slurm_jobs_above_the_progress_lines():
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index('wrap.className = "msg assistant streaming";')
    template = source[start:source.index("stream.appendChild(wrap);", start)]
    order = [template.index(k) for k in ("run-now-sec", "run-jobs", "run-steps", "Activity log")]
    assert order == sorted(order)
