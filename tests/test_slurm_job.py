"""Offline tests for the Slurm batch-job engine (submit -> startup-timeout ->
scancel -> re-request -> run -> collect). No real Slurm, no SSH: a scripted fake
``RemoteExecutor`` drives the queue states deterministically.
"""

from __future__ import annotations

import pytest

from bioagent.gateway.executor import ExecResult
from bioagent.gateway.slurm_job import (
    AcquireConfig,
    JobCancelled,
    RunConfig,
    SlurmJobError,
    SlurmJobSpec,
    acquire_allocation,
    build_analysis_script,
    run_batch_job,
    singularity_exec,
)


class FakeSlurm:
    """A fake HPC host. ``plans`` is a list of per-submitted-job state scripts; the
    i-th submitted job reads ``plans[i]`` one entry per ``squeue`` poll. Entries:
    ``"PD"`` (queued, no node), ``"R:node-7"`` (running on a node), ``""`` (left the
    queue / finished). The last entry repeats if polled further."""

    def __init__(self, plans, finals=None):
        self.host = "hpc3-mock"
        self.username = "tester"
        self.plans = plans
        self.finals = finals or {}
        self.submits: list[str] = []
        self.cancels: list[str] = []
        self._poll: dict[str, int] = {}
        self._next = 1000

    def _ok(self, out=""):
        return ExecResult(command="", exit_status=0, stdout=out, stderr="")

    @staticmethod
    def _arg_after(flag, cmd):
        toks = cmd.split()
        return toks[toks.index(flag) + 1] if flag in toks else ""

    def exec(self, command, timeout=60.0):
        cmd = command.strip()
        if "BIOAGENT_EOF" in cmd or ("cat >" in cmd and "<<" in cmd):
            return self._ok()
        if cmd.startswith("mkdir"):
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
            idx = self.submits.index(jid)
            plan = self.plans[idx] if idx < len(self.plans) else [""]
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
            jid = self._arg_after("-j", cmd)
            return self._ok(self.finals.get(jid, "COMPLETED"))
        return self._ok()

    def open_tunnel(self, remote_host, remote_port, local_port=0):
        return local_port or 1234

    def close(self):
        pass


def _spec():
    return SlurmJobSpec(script="#!/bin/bash\necho hi\n", job_name="bioagent-analysis-tester")


# --- Singularity command builder --------------------------------------------


def test_singularity_exec_mounts_dataset_readonly_and_contains():
    cmd = singularity_exec(
        "/dfs3b/lab/analysis.sif", "python step.py",
        binds_ro=("/dfs3b/lab/ds.h5ad",), binds_rw=("/dfs3b/lab/run1",),
    )
    assert "--containall" in cmd and "--writable-tmpfs" in cmd
    assert "--network none" in cmd                      # no network by default
    assert "-B /dfs3b/lab/ds.h5ad:/dfs3b/lab/ds.h5ad:ro" in cmd   # dataset read-only
    assert "-B /dfs3b/lab/run1:/dfs3b/lab/run1" in cmd and ":ro" not in cmd.split("run1")[1][:4]
    assert "--nv" not in cmd                            # CPU job, no GPU passthrough


def test_build_analysis_script_has_sbatch_headers():
    inner = singularity_exec("/img.sif", "python x.py")
    script = build_analysis_script(
        "bioagent-analysis-tester", inner,
        partition="standard", cpus=8, mem_gb=64, time_limit="02:00:00", account="ruic20_lab",
    )
    assert script.startswith("#!/bin/bash")
    assert "#SBATCH --job-name=bioagent-analysis-tester" in script
    assert "#SBATCH --mem=64G" in script and "#SBATCH --cpus-per-task=8" in script
    assert "#SBATCH --account=ruic20_lab" in script
    assert inner in script


# --- acquire: startup, timeout->cancel->resubmit, give-up --------------------


def test_acquire_starts_on_first_attempt():
    fake = FakeSlurm(plans=[["R:node-1"]])
    job_id, node, attempts = acquire_allocation(fake, _spec())
    assert node == "node-1" and attempts == 1
    assert fake.submits == [job_id] and fake.cancels == []


def test_acquire_times_out_then_cancels_and_resubmits():
    # attempt 1 stays PENDING forever -> times out -> scancel -> attempt 2 runs.
    fake = FakeSlurm(plans=[["PD"], ["R:node-7"]])
    cfg = AcquireConfig(startup_timeout_s=0.03, max_attempts=3, poll_interval_s=0.01)
    job_id, node, attempts = acquire_allocation(fake, _spec(), config=cfg)
    assert attempts == 2 and node == "node-7"
    assert len(fake.submits) == 2                       # it re-requested
    assert fake.cancels == [fake.submits[0]]            # and killed the stuck first job


def test_acquire_raises_after_max_attempts():
    fake = FakeSlurm(plans=[["PD"], ["PD"]])            # never starts
    cfg = AcquireConfig(startup_timeout_s=0.02, max_attempts=2, poll_interval_s=0.01)
    try:
        acquire_allocation(fake, _spec(), config=cfg)
        assert False, "expected SlurmJobError"
    except SlurmJobError as exc:
        assert "after 2 attempts" in str(exc)
    assert len(fake.submits) == 2 and len(fake.cancels) == 2   # both attempts cancelled


def test_acquire_rejects_unsubmittable_job():
    class Reject(FakeSlurm):
        def exec(self, command, timeout=60.0):
            if command.strip().startswith("sbatch"):
                return ExecResult(command=command, exit_status=1, stdout="", stderr="Invalid account")
            return super().exec(command, timeout)
    try:
        acquire_allocation(Reject(plans=[["PD"]]), _spec())
        assert False, "expected SlurmJobError"
    except SlurmJobError as exc:
        assert "rejected" in str(exc)


# --- run_batch_job: completion + failure -------------------------------------


def test_run_batch_job_completes():
    # acquire poll#0 = R (returns), run poll#1 = "" (left queue) -> sacct COMPLETED.
    fake = FakeSlurm(plans=[["R:node-1", ""]], finals={"1000": "COMPLETED"})
    res = run_batch_job(fake, _spec(), run=RunConfig(run_timeout_s=5, poll_interval_s=0.01))
    assert res.completed is True and res.state == "COMPLETED" and res.node == "node-1"
    assert fake.cancels == []                           # a clean run is never cancelled


def test_run_batch_job_reports_failure():
    fake = FakeSlurm(plans=[["R:node-1", "F"]], finals={"1000": "FAILED"})
    res = run_batch_job(fake, _spec(), run=RunConfig(run_timeout_s=5, poll_interval_s=0.01))
    assert res.completed is False and res.state == "FAILED"


def test_run_batch_job_waits_out_sacct_lag():
    # Regression: the job left the queue (squeue empty) but sacct still reports the
    # non-terminal RUNNING for a couple of polls before flipping to COMPLETED — Slurm's
    # squeue/sacct eventual consistency. The engine must NOT read that lagging RUNNING as
    # the final state (which previously made a successful scGPT job get reported as
    # "did not complete (state RUNNING)"); it must keep polling and report success.
    class LaggySacct(FakeSlurm):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._sacct_calls = 0

        def exec(self, command, timeout=60.0):
            if command.strip().startswith("sacct"):
                self._sacct_calls += 1
                return self._ok("RUNNING" if self._sacct_calls <= 2 else "COMPLETED")
            return super().exec(command, timeout)

    fake = LaggySacct(plans=[["R:node-1", ""]])
    res = run_batch_job(fake, _spec(), run=RunConfig(run_timeout_s=5, poll_interval_s=0.01))
    assert res.completed is True and res.state == "COMPLETED"
    assert fake.cancels == []                           # finished on its own; never cancelled


def test_run_batch_job_completes_without_sacct_record():
    # Accounting-off fallback: squeue goes empty and sacct never returns a record. After a
    # couple of confirming polls the engine assumes COMPLETED rather than hanging to timeout.
    class NoSacct(FakeSlurm):
        def exec(self, command, timeout=60.0):
            if command.strip().startswith("sacct"):
                return self._ok("")                     # no accounting record
            return super().exec(command, timeout)

    fake = NoSacct(plans=[["R:node-1", ""]])
    res = run_batch_job(fake, _spec(), run=RunConfig(run_timeout_s=5, poll_interval_s=0.01))
    assert res.completed is True and res.state == "COMPLETED"
    assert fake.cancels == []


# --- Stop button: user cancel scancels the in-flight job ----------------------

def test_run_batch_job_scancels_running_job_on_stop():
    # Job is RUNNING; the user hits Stop mid-run → the job is scancelled and JobCancelled raised
    # (so the run ends promptly instead of blocking until the job's own timeout).
    fake = FakeSlurm(plans=[["R:node-3", "R:node-3", "R:node-3"]])
    calls = {"n": 0}
    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1          # not during acquire; fires on the first supervise poll
    with pytest.raises(JobCancelled):
        run_batch_job(fake, _spec(),
                      acquire=AcquireConfig(startup_timeout_s=1, poll_interval_s=0.01),
                      run=RunConfig(run_timeout_s=5, poll_interval_s=0.01),
                      should_cancel=cancel)
    assert fake.cancels == [fake.submits[0]]      # exactly the running job was scancelled


def test_acquire_scancels_pending_job_on_stop():
    # Stop while the job is still queued (PENDING) → scancel + JobCancelled, no resubmit.
    fake = FakeSlurm(plans=[["PD", "PD"]])
    with pytest.raises(JobCancelled):
        acquire_allocation(fake, _spec(),
                           config=AcquireConfig(startup_timeout_s=1, poll_interval_s=0.01),
                           should_cancel=lambda: True)
    assert fake.cancels == [fake.submits[0]] and len(fake.submits) == 1


def test_no_cancel_hook_completes_normally():
    # should_cancel=None (or never True) → unchanged behaviour, nothing scancelled.
    fake = FakeSlurm(plans=[["R:node-1", ""]], finals={"1000": "COMPLETED"})
    res = run_batch_job(fake, _spec(),
                        acquire=AcquireConfig(startup_timeout_s=1, poll_interval_s=0.01),
                        run=RunConfig(run_timeout_s=5, poll_interval_s=0.01),
                        should_cancel=lambda: False)
    assert res.completed is True and fake.cancels == []
