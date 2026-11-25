"""Where each step of a running plan stands (RCHENLAB/AiScientist#121), from the lab to the console.

The lab says it with ``step_state`` events from its run loops (``ResearchLab.mark_step``): a step is
processing while it runs (each revision too), done when the Critic accepted its result, failed when
it ended without one. The gateway keeps the latest record of each step with the run's stream, so a
window that reloads mid-run gets the same marks back; links the Slurm job a step submits to it; ends a
step still running when the run ends; and writes the marks into the run's saved state. Every tool
call, with its arguments, goes to ``process/tool_calls.jsonl``; the chat says the call in words.
Offline: scripted models, a mock connection.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading

import pytest

from aiscientist.agents.dag import LabPlan, TaskNode
from aiscientist.agents.plan_doc import PlanDoc, PlanStep
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness
from aiscientist.agents.research_lab import DEFAULT_SPECIALISTS, LabConfig, ResearchLab
from aiscientist.gateway import run_steps

AGENDA = ["**Quality control** — run QC on each library.",
          "**Clustering** — Leiden at resolution 0.8.",
          "**Markers** — rank the marker genes of each cluster."]


def _ctx() -> HarnessContext:
    return HarnessContext(decisions={}, tunnel_port=1, model="m")


def _note_tool() -> HarnessTool:
    return HarnessTool("note", "note", {"type": "object", "properties": {"what": {"type": "string"}}},
                       lambda a, c: {"status": "ok"}, category="analysis")


def _scientist_chat(messages, tools):
    if any(m.get("role") == "tool" for m in messages):
        return {"content": "", "tool_calls": [{"id": "f", "type": "function",
                "function": {"name": "finish", "arguments": json.dumps({"answer": "done"})}}]}
    return {"content": "", "tool_calls": [{"id": "t", "type": "function",
            "function": {"name": "note", "arguments": json.dumps({"what": "ok"})}}]}


def _complete(verdicts):
    """PI plans AGENDA; the Critic answers ``verdicts`` in turn (the last one repeats)."""
    seen = {"i": 0}

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(AGENDA)
        if "rigorous scientific Critic" in sys:
            v = verdicts[min(seen["i"], len(verdicts) - 1)]
            seen["i"] += 1
            return json.dumps(v)
        return "FINAL"
    return complete


def _lab(config: LabConfig, verdicts) -> ResearchLab:
    return ResearchLab(_ctx(), config, complete_fn=_complete(verdicts),
                       scientist=ResearchHarness(catalog=[_note_tool()], chat_fn=_scientist_chat))


def _states(events):
    return [(e.get("step"), e["state"]) for e in events if e["type"] == "step_state"]


ACCEPT = {"verdict": "accept", "score": 0.9, "critique": "grounded"}
REVISE = {"verdict": "revise", "score": 0.2, "critique": "no result yet"}


# --- the lab ---------------------------------------------------------------------------------------

# The scripted plans name no tool, so these run with model-written code on (RCHENLAB/AiScientist#150);
# the switch does not change how a step is marked.
@pytest.mark.usefixtures("model_written_code")
@pytest.mark.parametrize("planner", ["linear", "dag"])
def test_each_step_is_processing_then_done_by_its_number_and_id_in_the_approved_plan(planner):
    events: list[dict] = []
    result = _lab(LabConfig(planner=planner, plan_yaml=True, auto_select_skill=False), [ACCEPT]).run(
        "Characterise the dataset", on_event=events.append)
    assert result.accepted_steps == 3
    assert _states(events) == [(1, "processing"), (1, "done"), (2, "processing"), (2, "done"),
                               (3, "processing"), (3, "done")]
    marks = [e for e in events if e["type"] == "step_state"]
    plan_ids = [s.id for s in result_plan(events)]
    assert [e["id"] for e in marks[::2]] == plan_ids, "the plan document's own step ids"
    assert all(e["text"] == AGENDA[e["step"] - 1] for e in marks)
    # The step is marked processing before its work starts, and done after the Critic accepted it.
    order = [e["type"] for e in events if e["type"] in ("step_state", "scientist_start", "critic")]
    assert order[:3] == ["step_state", "scientist_start", "critic"] and order[3] == "step_state"


def result_plan(events):
    from aiscientist.agents.plan_doc import parse_plan_yaml
    approved = next(e for e in events if e["type"] == "plan_approved")
    return parse_plan_yaml(approved["yaml"]).steps


# The scripted plans name no tool, so these run with model-written code on (RCHENLAB/AiScientist#150);
# the switch does not change how a step is marked.
@pytest.mark.usefixtures("model_written_code")
def test_a_revised_step_stays_processing_and_one_that_is_never_accepted_fails():
    # Step 1 is revised once and then accepted; step 2 is never accepted (max_revisions 1).
    verdicts = [REVISE, ACCEPT, REVISE, REVISE, ACCEPT]
    events: list[dict] = []
    _lab(LabConfig(plan_yaml=True, max_revisions=1, auto_select_skill=False), verdicts).run(
        "Characterise the dataset", on_event=events.append)
    assert _states(events) == [(1, "processing"), (1, "processing"), (1, "done"),
                               (2, "processing"), (2, "processing"), (2, "failed"),
                               (3, "processing"), (3, "done")]
    again = [e for e in events if e["type"] == "step_state"][1]
    assert again["reason"] == "revising after the Critic's review"


def test_two_steps_that_run_side_by_side_are_both_processing():
    # After QC, enrichment and an independent literature step are ready together and co-run. While
    # both are inside their work, both are processing and neither is done.
    lab = ResearchLab(_ctx(), LabConfig(planner="dag", max_concurrency=2),
                      complete_fn=_complete([ACCEPT]),
                      scientist=ResearchHarness(catalog=[_note_tool()], chat_fn=_scientist_chat))
    plan = LabPlan((
        TaskNode(id="s1", goal="Run scanpy QC"),
        TaskNode(id="s2", goal="Run pathway enrichment", depends_on=("s1",)),
        TaskNode(id="s3", goal="Literature search for the markers", depends_on=("s1",)),
    ))
    events: list[dict] = []
    seen_at_barrier: dict[str, str] = {}

    def snapshot() -> None:
        records: dict = {}
        for e in list(events):
            if e["type"] == "step_state":
                run_steps.merge_step_state(records, e)
        seen_at_barrier.update({r["text"]: r["state"] for r in records.values()})

    both_inside = threading.Barrier(2, action=snapshot, timeout=10)
    original = lab._scientist

    def scientist(question, step, *args, **kwargs):
        if "enrichment" in step or "Literature" in step:
            both_inside.wait()
        return original(question, step, *args, **kwargs)

    lab._scientist = scientist
    result = lab._run_dag("q", plan, events.append, None, None, exec_roster=DEFAULT_SPECIALISTS,
                          exec_multi=False, decision_review=None)
    assert result.accepted_steps == 3
    assert seen_at_barrier == {"Run scanpy QC": "done", "Run pathway enrichment": "processing",
                               "Literature search for the markers": "processing"}
    # The DAG's node ids number the steps when no plan document does (s2 -> 2).
    assert sorted(e["index"] for e in events if e["type"] == "step_state") == [1, 1, 2, 2, 3, 3]


# The scripted plans name no tool, so these run with model-written code on (RCHENLAB/AiScientist#150);
# the switch does not change how a step is marked.
@pytest.mark.usefixtures("model_written_code")
def test_a_step_of_the_approved_plan_left_out_before_the_run_is_skipped():
    # The re-read guard drops a step that only reports a table an earlier step wrote: it never runs,
    # so its mark is skipped, not "not started" for good.
    readback = "**Report the tables** — Parse the generated DE tables and report the exact tabular output."
    agenda = AGENDA[:2] + [readback]

    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(agenda)
        if "rigorous scientific Critic" in sys:
            return json.dumps(ACCEPT)
        return "FINAL"

    lab = ResearchLab(_ctx(), LabConfig(plan_yaml=True, auto_select_skill=False), complete_fn=complete,
                      scientist=ResearchHarness(catalog=[_note_tool()], chat_fn=_scientist_chat))
    events: list[dict] = []
    # A person approved the plan with the step on it (plan mode), and the guard drops it afterwards.
    lab.run("Characterise the dataset", on_event=events.append,
            plan_review=lambda kind, payload: {"action": "approve"})
    marks = [e for e in events if e["type"] == "step_state"]
    assert any(e["type"] == "steps_pruned" and e.get("reason") == "reads_back_earlier_output" for e in events)
    assert [(e["step"], e["state"]) for e in marks] == [(3, "skipped"), (1, "processing"), (1, "done"),
                                                        (2, "processing"), (2, "done")]
    assert "left out before the run" in marks[0]["reason"]


def test_a_step_added_during_the_run_is_announced_not_started():
    lab = _lab(LabConfig(plan_yaml=True, auto_select_skill=False), [ACCEPT])
    events: list[dict] = []
    lab._plan_doc = lab._approved_plan = PlanDoc("q", tuple(PlanStep(id=f"s{i}", text=t)
                                                              for i, t in enumerate(AGENDA, 1)))
    lab.mark_step(events.append, "**Check the doublets** — added by exploration.", "pending",
                  index=4, reason="added during the run")
    assert events == [{"type": "step_state", "text": "**Check the doublets** — added by exploration.",
                       "state": "pending", "index": 4, "reason": "added during the run"}]
    lab.mark_step(events.append, "  **Clustering** —   Leiden at resolution 0.8. ", "processing")
    assert (events[-1]["step"], events[-1]["id"]) == (2, "s2"), "a re-wrapped step still matches"


# --- the gateway -----------------------------------------------------------------------------------

gw = pytest.importorskip("aiscientist.gateway.app")


def _conn():
    from aiscientist.gateway.settings import HPCSettings
    return gw.Connection(HPCSettings.from_env(), mock=True, loop=asyncio.new_event_loop(), username="tester")


def _in_run(conn, fn):
    def body():
        run = conn.begin_run(conversation_id="chat-A", run_id="r1")
        conn.push({"type": "chat_start"})
        fn()
        return run
    return contextvars.copy_context().run(body)


def _mark(n, state, **kw):
    return {"type": "step_state", "step": n, "id": f"s{n}", "text": AGENDA[n - 1], "state": state, **kw}


def _push_lab(conn, ev, flags=None):
    """What the run's on_event does with one lab event: note the step, push its payloads."""
    gw._note_usage_step(ev, flags if flags is not None else {})
    for payload in gw._lab_event_to_chat(ev):
        conn.push(payload)


def test_the_gateway_sends_one_record_per_step_and_a_reload_gets_the_same_marks_back():
    conn = _conn()
    sent: list[dict] = []
    conn._publish = sent.append

    def three_steps():
        for ev in (_mark(1, "processing"), _mark(1, "done"), _mark(2, "processing"), _mark(3, "processing")):
            _push_lab(conn, ev)
    run = _in_run(conn, three_steps)
    live = [p for p in sent if p["type"] == "step_state"]
    assert [(p["step"], p["state"]) for p in live] == [(1, "processing"), (1, "done"), (2, "processing"),
                                                       (3, "processing")]
    assert all(p["key"] == f"id:s{p['step']}" and p["conversation_id"] == "chat-A" for p in live)
    assert live[1]["title"] == "Quality control" and live[1]["started_at"] <= live[1]["ended_at"]

    # The reload case: a window that reconnects mid-run is sent each step's latest record, in order.
    replay = [p for p in conn.stream_replay_payloads(run.stream) if p["type"] == "step_state"]
    assert [(p["step"], p["state"]) for p in replay] == [(1, "done"), (2, "processing"), (3, "processing")]
    latest = {p["key"]: p for p in live}
    assert all({k: v for k, v in p.items() if k != "type"} == {k: v for k, v in latest[p["key"]].items()
                                                                 if k != "type"} for p in replay)
    assert run.step_states() == [{k: v for k, v in p.items() if k not in ("type", "run_id", "conversation_id")}
                                 for p in replay]


def test_a_slurm_job_a_step_submits_is_named_on_that_step():
    from aiscientist.gateway import slurm_job
    conn = _conn()
    sent: list[dict] = []
    conn._publish = sent.append

    def step_with_a_job():
        _push_lab(conn, _mark(2, "processing"))
        slurm_job.report_job("41872290", "SUBMITTED", name="bioagent_analysis_x")
        slurm_job.report_job("41872290", "RUNNING", node="hpc3-14-07")
    run = _in_run(conn, step_with_a_job)
    jobs = [p for p in sent if p["type"] == "run_job"]
    assert all(j["step_key"] == "id:s2" and j["step"].startswith("Step 2: **Clustering**") for j in jobs)
    marks = [p for p in sent if p["type"] == "step_state"]
    assert [(m["state"], m.get("job_id")) for m in marks] == [("processing", None), ("processing", "41872290")], \
        "the job is named on the step once, not on every state of the job"
    assert run.step_states()[0]["job_id"] == "41872290"


def test_a_step_still_running_when_the_run_ends_is_stopped_and_a_plan_wait_ends_nothing():
    conn = _conn()
    conn._publish = lambda p: None

    def stopped_mid_step():
        _push_lab(conn, _mark(1, "done"))
        _push_lab(conn, _mark(2, "processing"))
        conn.push({"type": "chat_stopped", "awaiting_plan": True})   # only waiting for a plan review
        assert [r["state"] for r in conn.active_run.step_states()] == ["done", "processing"]
        conn.push({"type": "chat_done"})
    run = _in_run(conn, stopped_mid_step)
    assert [(r["step"], r["state"]) for r in run.step_states()] == [(1, "done"), (2, "stopped")]


def test_a_failed_step_has_one_progress_line_and_the_scheduler_has_none():
    out = gw._lab_event_to_chat(_mark(2, "failed", reason="no result"))
    assert [p["type"] for p in out] == ["step_state", "lab_progress"]
    assert out[1]["level"] == "warning" and "Step 2 ended without an accepted result: Clustering" in out[1]["text"]
    assert gw._lab_event_to_chat({"type": "step_state", "state": "nonsense", "text": "x"}) == []
    # A step kept from the run this one continues failed back then: it is marked, not warned again.
    kept = gw._lab_event_to_chat(_mark(2, "failed", reason=run_steps.KEPT_REASON))
    assert [p["type"] for p in kept] == ["step_state"]


def test_the_lab_marks_a_kept_step_with_the_reason_the_gateway_knows():
    import inspect
    from aiscientist.agents import research_lab
    assert f'reason="{run_steps.KEPT_REASON}"' in inspect.getsource(research_lab)


def test_the_saved_run_keeps_where_each_step_ended(tmp_path):
    art = tmp_path / "artifacts"
    (art / "process").mkdir(parents=True)
    (art / "process" / "run_state.json").write_text(json.dumps({"agenda": AGENDA, "rounds": []}))
    states = [{"key": "id:s1", "step": 1, "state": "done"}, {"key": "id:s2", "step": 2, "state": "stopped"}]
    gw._save_step_states(art, states)
    saved = json.loads((art / "process" / "run_state.json").read_text())
    assert saved["step_states"] == states and saved["agenda"] == AGENDA
    gw._save_step_states(tmp_path / "no-run", states)          # a run with no state file: nothing written
    assert not (tmp_path / "no-run").exists()


# --- the record of every tool call ------------------------------------------------------------------

def test_every_tool_call_is_written_with_its_arguments_and_how_it_ended(tmp_path):
    log = run_steps.ToolCallLog(tmp_path / "process" / "tool_calls.jsonl")
    code = "import scanpy as sc\nprint(1)"
    log.observe({"type": "tool_start", "tool": "run_code", "args": {"code": code}}, step="Step 2: Clustering")
    log.observe({"type": "tool_result", "tool": "run_code", "summary": "ok", "args": {"code": code}})
    log.observe({"type": "tool_error", "tool": "run_cellqc", "error": "tool is outside this step"})
    log.observe({"type": "expert_tool", "member": "Geneticist", "tool": "search_corpus", "state": "start",
                 "args": {"queries": ["USH2A"]}})
    log.observe({"type": "tool_start", "tool": "run_de", "args": {"groupby": "condition"}})
    log.close()                                              # the run ended while run_de ran
    rows = [json.loads(line) for line in (tmp_path / "process" / "tool_calls.jsonl").read_text().splitlines()]
    assert [(r["call"], r["tool"], r["status"]) for r in rows] == [
        (1, "run_code", "ok"), (2, "run_cellqc", "error"), (3, "search_corpus", "unfinished"),
        (4, "run_de", "unfinished")]
    assert rows[0]["args"] == {"code": code} and rows[0]["summary"] == "ok"
    assert rows[0]["step"] == "Step 2: Clustering" and rows[0]["started_at"] <= rows[0]["ended_at"]
    assert rows[1]["error"] == "tool is outside this step"
    assert rows[2]["by"] == "Geneticist" and rows[2]["args"] == {"queries": ["USH2A"]}


def test_calls_of_steps_that_run_side_by_side_are_kept_apart(tmp_path):
    log = run_steps.ToolCallLog(tmp_path / "tool_calls.jsonl")
    started = threading.Barrier(2, timeout=10)

    def step(name, value):
        log.observe({"type": "tool_start", "tool": "note", "args": {"what": value}}, step=name)
        started.wait()                                       # both calls are open at once
        log.observe({"type": "tool_result", "tool": "note", "summary": value})

    threads = [threading.Thread(target=step, args=(f"Step {n}", f"v{n}")) for n in (2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = [json.loads(line) for line in (tmp_path / "tool_calls.jsonl").read_text().splitlines()]
    assert sorted((r["step"], r["args"]["what"], r["summary"]) for r in rows) == [
        ("Step 2", "v2", "v2"), ("Step 3", "v3", "v3")]


def test_a_server_path_in_a_call_is_cut_to_its_file_name(tmp_path, monkeypatch):
    from aiscientist.gateway import errors
    monkeypatch.setattr(errors, "_server_roots", lambda: ["/data/aiscientist"])
    log = run_steps.ToolCallLog(tmp_path / "tool_calls.jsonl")
    log.observe({"type": "tool_start", "tool": "read_text", "args": {"path": "/data/aiscientist/state/keys.json"}})
    log.observe({"type": "tool_result", "tool": "read_text", "summary": "read /data/aiscientist/state/keys.json"})
    row = json.loads((tmp_path / "tool_calls.jsonl").read_text())
    assert "/data/aiscientist" not in json.dumps(row) and row["args"]["path"].endswith("keys.json")


@pytest.mark.parametrize("tool, args, words", [
    ("run_code", {"code": "a = 1\n\nb = 2\n"}, "run_code — Python, 2 lines"),
    ("run_shell", {"command": "ls -la"}, "run_shell — shell command, 1 line"),
    ("run_cellqc", {"dataset": "/dfs3b/u/x/retina.h5ad", "output_dir": "/dfs3b/u/x/out/"},
     "run_cellqc — dataset retina.h5ad · output dir out"),
    ("get_gene", {"gene": "USH2A", "fields": ["inheritance", "phenotype"], "raw": {"a": 1}},
     "get_gene — gene USH2A · fields inheritance, phenotype · +1 more"),   # a nested input: in the file
    ("finish_report", {}, "finish_report"),
])
def test_a_call_in_words(tool, args, words):
    assert run_steps.tool_call_words(tool, args) == words


def test_the_record_of_a_step_keeps_its_first_start_and_the_latest_state():
    steps: dict = {}
    a = run_steps.merge_step_state(steps, {"id": "s1", "step": 1, "text": AGENDA[0], "state": "processing", "at": 10.0})
    b = run_steps.merge_step_state(steps, {"id": "s1", "state": "processing", "reason": "revising", "at": 20.0})
    c = run_steps.merge_step_state(steps, {"id": "s1", "state": "done", "at": 30.0})
    assert a["started_at"] == b["started_at"] == c["started_at"] == 10.0
    assert b["reason"] == "revising" and "reason" not in c and c["ended_at"] == 30.0
    assert run_steps.merge_step_state(steps, {"key": "id:missing", "job_id": "9"}) is None
    # A reason does not outlive the update that gave it, even when the state stays the same: a decision
    # node "waiting for your choice" says nothing of the kind once it runs. A job update keeps it.
    held: dict = {}
    run_steps.merge_step_state(held, {"id": "s3", "state": "processing", "reason": "waiting for your choice"})
    run_steps.merge_step_state(held, {"id": "s3", "state": "processing", "reason": "revising"})
    assert run_steps.merge_step_state(held, {"key": "id:s3", "job_id": "7"})["reason"] == "revising"
    assert "reason" not in run_steps.merge_step_state(held, {"id": "s3", "state": "processing"})
    assert run_steps.merge_step_state(steps, {"id": "s9", "state": "weird"}) is None
    assert run_steps.ordered_steps({"t:x": {"key": "t:x", "index": 9}, "n:2": {"key": "n:2", "step": 2},
                                    "n:1": {"key": "n:1", "step": 1}}) == [
        {"key": "n:1", "step": 1}, {"key": "n:2", "step": 2}, {"key": "t:x", "index": 9}]
