"""The run card: a run's one line in the chat, its Slurm jobs and details on a click (under node).

A run used to print every milestone in full into the chat. Now the chat shows one line (what the run
does now, its Slurm job's state, warnings, time); opened, the card lists the run's Slurm jobs with
their states, then the progress lines, step code and activity log. A finished answer keeps the line,
closed. These run the real renderers of ``frontend/console/app.js``. Skipped where node is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"

BLOCKS = [
    ("function escapeHtml(value)", "// Backend progress/feed lines"),
    ("const EMOJI_SYMBOL", "// GFM pipe tables"),
    ("function feedItemHtml(entry)", "// The dataset the chat is bound to"),
    ("function fmtElapsed(ms)", "// Ensure the live bubble exists"),
    ("function icon(name)", "function fileIcon(kind)"),
    ("function planStepParts(text)", "function planSettingText("),
    ("function msgFromServer(m)", "function syncMessage("),
]

HARNESS = r"""
const vm = require("vm");
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(input.code, sandbox);
const out = vm.runInContext("(() => {" + input.scenario + "})()", sandbox);
console.log(JSON.stringify(out));
"""

T = 1_800_000_000  # a fixed "now", in seconds

FEED = [
    {"kind": "line", "text": "👥 Team mode — the PI will form an expert team (adds a few minutes).", "level": "info"},
    {"kind": "line", "text": "📋 Plan ready — 4 steps", "level": "info"},
    {"kind": "line", "text": "  1. **Quality control** — run CellQC.", "level": "info"},
    {"kind": "line", "text": "  2. **Clustering** — Leiden at 0.8.", "level": "info"},
    {"kind": "line", "text": "🔬 Scientist: **Quality control** — run CellQC on each library.", "level": "info"},
    {"kind": "code", "code": "import scanpy"},
    {"kind": "line", "text": "✅ Step done — Quality control · quality 0.82", "level": "success"},
    {"kind": "line", "text": "⚠️ Not available in this deployment: scvi.", "level": "warning"},
    {"kind": "line", "text": "🔬 Scientist: **Clustering** — Leiden at resolution 0.8.", "level": "info"},
]

JOBS = [
    {"job_id": "41872213", "state": "COMPLETED", "partition": "standard", "cpus": "8", "mem": "64G",
     "step": "Step 1: **Quality control** — run CellQC.", "submitted_at": T - 900, "started_at": T - 840,
     "ended_at": T - 420, "node": "hpc3-14-07"},
    {"job_id": "41872290", "state": "PENDING", "reason": "Priority", "eta": "2026-11-04T15:42:10",
     "step": "Step 2: **Clustering** — Leiden at 0.8.", "submitted_at": T - 260},
    {"job_id": "41869911", "state": "RUNNING", "kind": "worker", "node": "hpc3-14-03",
     "step": "Step 2: **Clustering** — Leiden at 0.8.", "submitted_at": T - 1500, "started_at": T - 1480},
]


def _run(scenario: str, **data) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    code = "\n".join(source[source.index(a):source.index(b, source.index(a))] for a, b in BLOCKS)
    code += "\nconst DATA = " + json.dumps(data) + ";"
    done = subprocess.run([node, "-e", HARNESS], input=json.dumps({"code": code, "scenario": scenario}),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_running_step_is_one_line_with_its_job_warnings_and_time():
    out = _run("""
  const v = runCardView(DATA.feed, DATA.jobs, "", { live: true, status: "⚙️ Running run_code…", elapsedMs: 935000 });
  return { v, line: runSummaryInner(v, true) };
""", feed=FEED, jobs=JOBS)
    v = out["v"]
    assert v["title"] == "Step 2 of 4 · Clustering"
    assert v["now"] == "Running run_code…", "the backend's emoji marker is dropped from the line"
    # The run's own queued job, not the CPU worker that holds its node between commands.
    assert v["jobs"] == {"tone": "wait", "text": "Slurm 41872290 queued"}
    assert v["warnings"] == 1 and v["time"] == "15m 35s"
    assert 'class="run-dot"' in out["line"] and "Not available" not in out["line"], \
        "the explanations stay in the card's body, not on its line"


def test_before_the_plan_the_line_is_the_last_milestones_head():
    out = _run("""
  return runCardView(DATA.feed.slice(0, 1), [], "", { live: true });
""", feed=FEED)
    assert out["title"] == "👥 Team mode"
    assert out["jobs"] is None and out["time"] == ""


def test_a_prompt_waiting_for_the_user_says_which_and_shows_no_stale_status():
    out = _run("""
  const v = (kind) => runCardView(DATA.feed.slice(0, 4), [], "", { live: true, waitingPlan: kind, status: "🧠 Model is reasoning" });
  return { plan: v("plan"), clarify: v("clarify").title, decision: v("decision").title };
""", feed=FEED)
    assert out["plan"]["title"] == "Plan ready — waiting for your review" and out["plan"]["now"] == ""
    assert out["clarify"] == "A question for you — waiting for your answer"
    assert out["decision"] == "A choice for you — waiting for your decision"


def test_the_cpu_worker_is_named_only_when_the_run_has_no_job_of_its_own_in_flight():
    out = _run("""
  const worker = DATA.jobs[2];
  const done = DATA.jobs[0];
  return {
    workerOnly: runJobChip([done, worker], false),
    ended: runJobChip(DATA.jobs.concat([{ job_id: "9", state: "OUT_OF_MEMORY" }]), true),
    running: runJobChip([{ job_id: "5", state: "RUNNING" }, { job_id: "6", state: "RUNNING" }, worker], false),
  };
""", jobs=JOBS)
    assert out["workerOnly"] == {"tone": "run", "text": "CPU worker 41869911 running"}
    assert out["running"] == {"tone": "run", "text": "2 Slurm jobs running"}
    assert out["ended"] == {"tone": "bad", "text": "4 Slurm jobs, 1 not completed"}


def test_the_slurm_rows_say_state_step_where_why_and_how_long():
    out = _run("""
  return {
    live: jobListHtml(DATA.jobs, { live: true, nowS: DATA.now, model: { job_id: "41870001", node: "gpu-2" } }),
    none: jobListHtml([], { live: true }),
    noneDone: jobListHtml([]),
  };
""", jobs=JOBS, now=T)
    html = out["live"]
    assert ">Completed<" in html and ">Queued<" in html and ">Running<" in html
    assert "Job 41872213" in html and "Step 1 · Quality control" in html
    assert "hpc3-14-07 · partition standard · 8 CPU · 64G" in html and "ran 7m 00s" in html
    assert "waiting: Priority" in html and "Slurm expects it to start 2026-11-04 15:42:10" in html
    assert "queued 4m 20s" in html
    assert "CPU worker (the agent&#039;s shell) · now Step 2 · Clustering" in html
    assert "Your model server is Slurm job 41870001 on gpu-2." in html
    assert "has not submitted a Slurm job yet" in out["none"]
    assert "submitted no Slurm job" in out["noneDone"]


def test_job_fields_are_escaped():
    out = _run("""
  return jobListHtml([{ job_id: "1<b>", state: "RUNNING", node: "<img src=x onerror=alert(1)>",
                        reason: "<script>", step: "Step 1: **<i>x</i>**" }], { live: true, nowS: 0 });
""")
    assert "<img" not in out and "<script>" not in out and "<i>" not in out and "<b>" not in out


def test_a_finished_answer_keeps_one_closed_line_with_its_jobs_inside():
    out = _run("""
  const m = { role: "assistant", content: "Result", feed: DATA.feed, jobs: DATA.jobs.slice(0, 1),
              thinking: "→ run_code(...)", outcome: "done", ms: 935000 };
  const stopped = { ...m, outcome: "stopped" };
  return { done: runCardHtml(m), stopped: runCardHtml(stopped),
           old: runCardHtml(msgFromServer({ role: "assistant", content: "x", kind: "text",
                                            meta: { feed: DATA.feed, thinking: "" } })),
           none: runCardHtml({ role: "assistant", content: "x", feed: DATA.feed, jobs: [], outcome: "done" }),
           thinkingOnly: runCardHtml({ role: "assistant", content: "x", thinking: "hmm" }) };
""", feed=FEED, jobs=JOBS)
    done = out["done"]
    assert done.startswith('<details class="run-card done">'), "closed: no open attribute"
    assert "Run finished · 4 steps" in done and "1 Slurm job" in done and "15m 35s" in done
    assert "Job 41872213" in done and "Activity log" in done and "Step code" in done
    assert "Run stopped · step 2 of 4" in out["stopped"]
    # A message saved before this change (no jobs, outcome or time) still opens to its progress, and
    # says nothing about Slurm: it never recorded its jobs.
    assert "Run finished · 4 steps" in out["old"] and "Slurm" not in out["old"]
    assert "submitted no Slurm job" in out["none"]
    assert "Thinking &amp; activity" in out["thinkingOnly"] and "Slurm jobs" not in out["thinkingOnly"]


def test_a_saved_answer_keeps_its_jobs_outcome_and_time_on_the_server():
    out = _run("""
  const m = { role: "assistant", content: "Result", feed: [], thinking: "", jobs: DATA.jobs, outcome: "error", ms: 1200, streamId: "s9" };
  const payload = serverMsgPayload(m);
  return { payload, back: msgFromServer({ role: "assistant", content: payload.content, kind: payload.kind, meta: payload.meta }) };
""", jobs=JOBS)
    assert out["payload"]["meta"]["jobs"][0]["job_id"] == "41872213"
    back = out["back"]
    assert back["jobs"] == JOBS and back["outcome"] == "error" and back["ms"] == 1200 and back["streamId"] == "s9"


def test_a_new_record_of_a_job_replaces_the_old_one_in_place():
    out = _run("""
  const jobs = [];
  upsertJob(jobs, { type: "run_job", job_id: "1", state: "PENDING", conversation_id: "A", at: 5 });
  upsertJob(jobs, { type: "run_job", job_id: "2", state: "SUBMITTED" });
  upsertJob(jobs, { type: "run_job", job_id: "1", state: "RUNNING", node: "n1" });
  return jobs;
""")
    assert out == [{"job_id": "1", "state": "RUNNING", "node": "n1"}, {"job_id": "2", "state": "SUBMITTED"}]
