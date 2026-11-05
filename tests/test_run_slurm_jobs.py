"""A run's Slurm jobs and their states, sent to the run's chat.

The chat shows one line per run, and the run's Slurm jobs, with their states, when the line is opened.
Every job a run submits goes through ``gateway.slurm_job``, which reports each state it sees
(``report_job``); the gateway sends it to the chat of the run the calling code works for, as a
``run_job`` record that ``Connection`` keeps with the run's stream so a reconnecting window gets it
back. Offline: the scripted Slurm host of ``test_slurm_job``.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from aiscientist.gateway import slurm_job
from aiscientist.gateway.slurm_job import (
    AcquireConfig, JobCancelled, RunConfig, SlurmJobSpec, acquire_allocation, run_batch_job,
)
from test_slurm_job import FakeSlurm, _virtual_clock

SCRIPT = ("#!/bin/bash\n#SBATCH --job-name=bioagent_analysis_x\n#SBATCH --partition=standard\n"
          "#SBATCH --time=02:00:00\n#SBATCH --cpus-per-task 8\n#SBATCH --mem=64G\npython run.py\n")
SPEC = SlurmJobSpec(script=SCRIPT, job_name="bioagent_analysis_x", submit_dir="/tmp/x")
FAST = AcquireConfig(startup_timeout_s=30, max_attempts=1, poll_interval_s=10)
QUICK_RUN = RunConfig(run_timeout_s=300, poll_interval_s=10)


@pytest.fixture
def seen():
    """Every state reported while the test runs, in order."""
    updates: list[dict] = []
    previous = slurm_job.set_job_observer(updates.append)
    yield updates
    slurm_job.set_job_observer(previous)


def _states(updates, job_id=None):
    return [u["state"] for u in updates if job_id is None or u["job_id"] == job_id]


def test_a_job_reports_submitted_queued_running_and_completed_with_what_it_asked_for(seen, monkeypatch):
    _virtual_clock(monkeypatch)
    host = FakeSlurm([["PD", "R:hpc3-14-07", ""]], finals={"1000": "COMPLETED"})
    result = run_batch_job(host, SPEC, acquire=FAST, run=QUICK_RUN)
    assert result.completed
    assert _states(seen) == ["SUBMITTED", "PENDING", "RUNNING", "COMPLETED"]
    first = seen[0]
    assert first["job_id"] == "1000" and first["name"] == "bioagent_analysis_x"
    assert (first["partition"], first["time_limit"], first["cpus"], first["mem"]) == \
        ("standard", "02:00:00", "8", "64G")
    assert seen[2]["node"] == "hpc3-14-07"
    assert all(isinstance(u["at"], float) for u in seen)


def test_a_failed_job_reports_its_end_state_by_name(seen, monkeypatch):
    _virtual_clock(monkeypatch)
    host = FakeSlurm([["R:n1", ""]], finals={"1000": "OUT_OF_MEMORY"})
    result = run_batch_job(host, SPEC, acquire=FAST, run=QUICK_RUN)
    assert not result.completed
    assert _states(seen)[-1] == "OUT_OF_MEMORY"


def test_a_queue_that_never_starts_says_why_then_cancelled(seen, monkeypatch):
    _virtual_clock(monkeypatch)
    host = FakeSlurm([["PD"]], pending=[("Priority", "2026-10-09T17:40:00")])
    with pytest.raises(slurm_job.SlurmQueueTimeout):
        acquire_allocation(host, SPEC, config=FAST)
    waiting = [u for u in seen if u["state"] == "PENDING" and u.get("reason")]
    assert waiting and waiting[0]["reason"] == "Priority" and waiting[0]["eta"] == "2026-10-09T17:40:00"
    assert seen[-1]["state"] == "CANCELLED" and "no node" in seen[-1]["reason"]


def test_stop_reports_the_job_cancelled_by_you(seen, monkeypatch):
    _virtual_clock(monkeypatch)
    host = FakeSlurm([["R:n1"]])
    with pytest.raises(JobCancelled):
        run_batch_job(host, SPEC, acquire=FAST, run=QUICK_RUN, should_cancel=lambda: "1000" in host._poll)
    assert seen[-1] == {**seen[-1], "job_id": "1000", "state": "CANCELLED", "reason": "stopped by you"}


def test_a_broken_observer_never_changes_what_happens_to_the_job(monkeypatch):
    _virtual_clock(monkeypatch)

    def boom(update):
        raise RuntimeError("display down")
    previous = slurm_job.set_job_observer(boom)
    try:
        host = FakeSlurm([["R:n1", ""]])
        assert run_batch_job(host, SPEC, acquire=FAST, run=QUICK_RUN).completed
    finally:
        slurm_job.set_job_observer(previous)


def test_the_cpu_worker_is_reported_while_a_command_runs_on_it(seen):
    from aiscientist.gateway.worker import WorkerAllocation, run_on_worker
    host = FakeSlurm([])
    run_on_worker(host, WorkerAllocation(job_id="777", node="hpc3-14-03"), "ls")
    assert seen == [{**seen[0], "job_id": "777", "state": "RUNNING", "node": "hpc3-14-03", "kind": "worker"}]


@pytest.mark.parametrize("raw, name", [("PD", "PENDING"), ("R", "RUNNING"), ("CANCELLED by 123", "CANCELLED"),
                                       ("COMPLETED+", "COMPLETED"), ("TIMEOUT", "TIMEOUT"), ("", "")])
def test_state_names(raw, name):
    assert slurm_job.job_state_name(raw) == name


def test_sbatch_resources_reads_both_option_forms_and_ignores_the_rest():
    script = "#!/bin/bash\n#SBATCH -p gpu\n#SBATCH --gres=gpu:A100:1 -t 1:00:00\n# SBATCH --mem=9G\necho --mem=1G\n"
    assert slurm_job.sbatch_resources(script) == {"partition": "gpu", "gres": "gpu:A100:1", "time_limit": "1:00:00"}


# ---- the gateway: to the run's chat, kept for a reconnect -------------------------------------

gw = pytest.importorskip("aiscientist.gateway.app")


def _conn():
    from aiscientist.gateway.settings import HPCSettings
    return gw.Connection(HPCSettings.from_env(), mock=True, loop=asyncio.new_event_loop(), username="tester")


def _in_run(conn, fn, step=None):
    """Run ``fn`` as code working for a new run of ``conn`` (in its own context, as a run task is)."""
    def body():
        run = conn.begin_run(conversation_id="chat-A", run_id="r1")
        conn.push({"type": "chat_start"})
        if step:
            gw._USAGE_STEP.set(step)
        fn()
        return run
    return contextvars.copy_context().run(body)


def _jobs(payloads):
    return [p for p in payloads if p["type"] == "run_job"]


def test_job_states_go_to_the_run_that_submitted_them_with_its_step(monkeypatch):
    _virtual_clock(monkeypatch)
    conn = _conn()
    sent = []
    conn._publish = sent.append
    host = FakeSlurm([["PD", "R:hpc3-15-22", ""]])
    _in_run(conn, lambda: run_batch_job(host, SPEC, acquire=FAST, run=QUICK_RUN),
            step="Step 2: **Clustering** — Leiden at resolution 0.8")
    jobs = _jobs(sent)
    assert [j["state"] for j in jobs] == ["SUBMITTED", "PENDING", "RUNNING", "COMPLETED"]
    assert all(j["conversation_id"] == "chat-A" and j["run_id"] == "r1" for j in jobs)
    assert jobs[0]["step"].startswith("Step 2: **Clustering**")
    # Each record is the whole job so far: the RUNNING state still names the partition and the step.
    last = jobs[-1]
    assert last["partition"] == "standard" and last["node"] == "hpc3-15-22" and last["step"].startswith("Step 2")
    assert last["submitted_at"] <= last["started_at"] <= last["ended_at"]


def test_a_job_no_run_submitted_goes_nowhere():
    conn = _conn()
    sent = []
    conn._publish = sent.append
    contextvars.copy_context().run(lambda: slurm_job.report_job("5", "RUNNING", node="n1"))
    assert _jobs(sent) == []


def test_a_reconnecting_window_gets_the_runs_jobs_back_as_one_record_each():
    conn = _conn()
    conn._publish = lambda p: None

    def two_jobs():
        slurm_job.report_job("10", "SUBMITTED", name="a", script=SCRIPT)
        slurm_job.report_job("10", "PD", reason="Priority")
        slurm_job.report_job("10", "R", node="n1")
        slurm_job.report_job("11", "SUBMITTED", name="b")
    run = _in_run(conn, two_jobs)
    replay = conn.stream_replay_payloads(run.stream)
    jobs = _jobs(replay)
    assert [(j["job_id"], j["state"]) for j in jobs] == [("10", "RUNNING"), ("11", "SUBMITTED")]
    assert "reason" not in jobs[0], "a running job no longer says why it waited"
    assert jobs[0]["partition"] == "standard" and jobs[0]["conversation_id"] == "chat-A"


def test_a_run_lists_a_bounded_number_of_jobs():
    jobs: dict = {}
    for i in range(gw._RUN_JOBS_MAX + 5):
        gw._merge_run_job(jobs, {"job_id": str(i), "state": "SUBMITTED", "at": 1.0})
    assert len(jobs) == gw._RUN_JOBS_MAX
    assert gw._merge_run_job(jobs, {"state": "RUNNING"}) is None
