"""Offline tests for durable submit + reattach on the Slurm batch engine.

These cover the "resume where you left off" path added for gateway restarts: the
``on_submit`` persistence hook, reattaching to a job submitted in an earlier process
(:func:`reattach_job`), and the non-blocking reconnect sweep (:func:`resume_incomplete`).
A scripted fake drives ``squeue``/``sacct`` per job_id — no real Slurm, no SSH.
"""

from __future__ import annotations

from bioagent.gateway.executor import ExecResult
from bioagent.gateway.job_store import JobRecord, JobStore
from bioagent.gateway.slurm_job import (
    RunConfig,
    SlurmJobSpec,
    reattach_job,
    resume_incomplete,
    run_batch_job,
)

_FAST = RunConfig(run_timeout_s=5, poll_interval_s=0.01)


class SlurmFake:
    """Scripted HPC host keyed by job_id. ``squeue[jid]`` is a per-poll list of ``squeue``
    values (``"PD"`` / ``"R:node"`` / ``""``); ``sacct[jid]`` is the terminal State string
    (default ``""`` = no record yet). ``sbatch`` hands out ids from ``_next``."""

    def __init__(self, squeue=None, sacct=None, next_id=2000):
        self.host = "hpc3-mock"
        self.username = "alice"
        self.squeue = squeue or {}
        self.sacct = sacct or {}
        self.submits: list[str] = []
        self.cancels: list[str] = []
        self._poll: dict[str, int] = {}
        self._next = next_id

    def _ok(self, out=""):
        return ExecResult(command="", exit_status=0, stdout=out, stderr="")

    @staticmethod
    def _arg_after(flag, cmd):
        toks = cmd.split()
        return toks[toks.index(flag) + 1] if flag in toks else ""

    def exec(self, command, timeout=60.0):
        cmd = command.strip()
        if cmd.startswith("mkdir") or "<<" in cmd:
            return self._ok()
        if cmd.startswith("sbatch"):
            jid = str(self._next)
            self._next += 1
            self.submits.append(jid)
            return self._ok(f"Submitted batch job {jid}")
        if cmd.startswith("scancel"):
            self.cancels.append(cmd.split()[-1])
            return self._ok()
        if cmd.startswith("squeue"):
            jid = self._arg_after("-j", cmd)
            plan = self.squeue.get(jid, [""])
            n = self._poll.get(jid, 0)
            self._poll[jid] = n + 1
            val = plan[n] if n < len(plan) else plan[-1]
            if val == "":
                return self._ok("")
            if ":" in val:
                state, node = val.split(":", 1)
                return self._ok(f"{state}|{node}")
            return self._ok(f"{val}|")
        if cmd.startswith("sacct"):
            return self._ok(self.sacct.get(self._arg_after("-j", cmd), ""))
        return self._ok()

    def open_tunnel(self, *a, **k):
        return 1234

    def close(self):
        pass


# --- on_submit persistence hook ---------------------------------------------


def test_on_submit_hook_fires_with_job_id():
    fake = SlurmFake(squeue={"2000": ["R:node-1", ""]}, sacct={"2000": "COMPLETED"})
    captured: list[str] = []
    res = run_batch_job(
        fake, SlurmJobSpec(script="#!/bin/bash\n", job_name="runcode_1"),
        run=_FAST, on_submit=captured.append,
    )
    assert captured == ["2000"]            # recorded the instant sbatch accepted it
    assert res.completed and res.state == "COMPLETED"


# --- reattach_job ------------------------------------------------------------


def test_reattach_already_completed_returns_immediately():
    fake = SlurmFake(squeue={"5": [""]}, sacct={"5": "COMPLETED"})
    res = reattach_job(fake, "5", run=_FAST)
    assert res.completed and res.state == "COMPLETED"
    assert fake.cancels == [] and fake.submits == []   # never resubmits or cancels


def test_reattach_no_wait_reports_running_without_blocking():
    fake = SlurmFake(squeue={"5": ["R:node-3"]})
    res = reattach_job(fake, "5", wait=False)
    assert res.state == "R" and res.node == "node-3" and res.completed is False


def test_reattach_wait_supervises_running_job_to_completion():
    fake = SlurmFake(squeue={"5": ["R:node-3", ""]}, sacct={"5": "COMPLETED"})
    res = reattach_job(fake, "5", run=_FAST, wait=True)
    assert res.completed and res.state == "COMPLETED"


def test_reattach_reports_failure_state():
    fake = SlurmFake(squeue={"7": [""]}, sacct={"7": "OUT_OF_MEMORY"})
    res = reattach_job(fake, "7", run=_FAST)
    assert res.completed is False and res.state.startswith("OUT_OF_MEMORY")


# --- resume_incomplete sweep -------------------------------------------------


def test_resume_incomplete_marks_finished_and_keeps_running(tmp_path):
    store = JobStore(tmp_path / "slurm_jobs.json")
    store.record(JobRecord(job_id="10", job_name="a", owner="alice", kind="runcode", state="RUNNING"))
    store.record(JobRecord(job_id="11", job_name="b", owner="alice", kind="runcode", state="PENDING"))
    store.record(JobRecord(job_id="12", job_name="c", owner="alice", kind="runcode",
                           state="COMPLETED", completed=True))   # terminal — must be skipped
    # 10 is still running; 11 finished while the gateway was down.
    fake = SlurmFake(squeue={"10": ["R:node-1"], "11": [""]}, sacct={"11": "COMPLETED"})

    results = resume_incomplete(fake, store, owner="alice", kind="runcode")

    assert {r.job_id for r in results} == {"10", "11"}
    assert store.get("11").completed is True                     # marked done
    assert {r.job_id for r in store.incomplete()} == {"10"}      # only the still-running one
    assert fake.cancels == []                                    # observe only, never cancel


def test_resume_incomplete_empty_store_is_noop(tmp_path):
    store = JobStore(tmp_path / "slurm_jobs.json")
    assert resume_incomplete(SlurmFake(), store) == []
