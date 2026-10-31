"""Run-correlated timings from the production Slurm lifecycle; no cluster or real waits."""
from __future__ import annotations

import pytest

from aiscientist.agents.loop_utils import ContextThreadPoolExecutor
from aiscientist.gateway import slurm_job
from aiscientist.gateway.executor import ExecResult
from aiscientist.gateway.run_monitor import RunMonitor, bind_monitor, current_monitor
from aiscientist.gateway.run_timing import read_timing
from aiscientist.gateway.slurm_analysis import SlurmAnalysisExecutor
from aiscientist.gateway.slurm_job import (
    AcquireConfig, JobCancelled, RunConfig, SlurmJobSpec, parse_timing_accounting, run_batch_job,
)


class Host:
    """Only the SSH boundary is fake; submission, waiting, accounting and monitor are real."""
    host, username = "offline", "test"

    def __init__(self, states=None, *, accounting="known", final="COMPLETED", job_id="100"):
        self.states = list(states or ["R|node", ""])
        self.accounting, self.final, self.job_id = accounting, final, job_id
        self.commands = []
        self.cancelled = False

    def exec(self, command, timeout=60):
        self.commands.append((command, timeout))
        output = ""
        if command.startswith("sbatch"):
            output = f"Submitted batch job {self.job_id}"
        elif command.startswith("squeue"):
            output = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        elif command.startswith("sacct"):
            output = self.final
        elif "SLURM_TIME_FORMAT" in command:
            if self.accounting == "error":
                raise OSError("accounting disabled")
            if self.accounting == "known":
                # Child steps must not double-count and times come from Slurm, not our polling.
                if "AllocCPUS" in command:
                    output = (f"{self.job_id}.batch|900|4||{self.final}|1700000000|1700000000|1700000900\n"
                              f"{self.job_id}|120|4||{self.final}|1700000000|1700000030|1700000150\n")
                else:
                    output = (f"{self.job_id}.batch|1700000000|1700000000|1700000900|{self.final}\n"
                              f"{self.job_id}|1700000000|1700000030|1700000150|{self.final}\n")
        elif command.startswith("scancel"):
            self.cancelled = True
        elif command.startswith("echo "):
            output = command[5:]
        return ExecResult(command, 0, output, "")

    def read_bytes(self, path, max_bytes=None):
        output = b'AISCIENTIST_RESULT_JSON {"status":"ok","cells":42}\n' if ".result.json" in path else b""
        return output[:max_bytes] if max_bytes is not None else output

    def remote_size(self, path):
        return len(self.read_bytes(path))

    def put_file(self, *args, **kwargs):
        pass

    def get_file(self, *args, **kwargs):
        pass


def monitor_at(tmp_path, run_id="run-1"):
    path = tmp_path / run_id / "timing.jsonl"
    monitor = RunMonitor(run_id=run_id, conversation_id=run_id, owner_id=1)
    monitor.bind_timing(path)
    return monitor, path


def clock(monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(slurm_job.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(slurm_job.time, "time", lambda: 1800000000 + elapsed[0])
    monkeypatch.setattr(slurm_job.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    return elapsed


def run(host, **kwargs):
    return run_batch_job(host, SlurmJobSpec("#!/bin/sh\n", "private-job-name", "/private/path"),
                         acquire=AcquireConfig(1000, 1, 10), run=RunConfig(1000, 10), **kwargs)


def by_stage(path):
    return {span["stage"]: span for span in read_timing(path)["spans"]}


def test_slurm_actual_times_are_separate_from_observed_poll_wait(tmp_path, monkeypatch):
    clock(monkeypatch)
    monitor, path = monitor_at(tmp_path)
    host = Host(["PD|", "PD|", "R|node", "R|node", ""])
    with bind_monitor(monitor):
        assert run(host).completed
    spans = by_stage(path)
    assert spans["hpc_queue_observed"]["duration_seconds"] == 20
    assert spans["hpc_job_observed"]["duration_seconds"] == 10
    assert spans["hpc_queue"]["duration_seconds"] == 30
    assert spans["hpc_execution"]["duration_seconds"] == 120
    assert spans["hpc_execution"]["started_at"] == "2023-11-14T22:13:50Z"
    assert spans["hpc_execution"]["metadata"]["source"] == "sacct"
    assert spans["hpc_queue_observed"]["category"] == "observation"
    assert all(span["outcome"] != "unknown" for span in spans.values())
    accounting = [(cmd, timeout) for cmd, timeout in host.commands if "SLURM_TIME_FORMAT" in cmd]
    assert len(accounting) == 1 and accounting[0][1] == 5
    raw = path.read_text()
    assert "private-job-name" not in raw and "/private/path" not in raw and "sbatch" not in raw


@pytest.mark.parametrize("accounting", ["missing", "error"])
def test_unavailable_accounting_does_not_turn_polling_into_compute(tmp_path, monkeypatch, accounting):
    clock(monkeypatch)
    monitor, path = monitor_at(tmp_path)
    with bind_monitor(monitor):
        assert run(Host(["PD|", "R|node", "R|node", ""], accounting=accounting)).completed
    spans = by_stage(path)
    for stage in ("hpc_queue", "hpc_execution"):
        assert spans[stage]["started_at"] is None and spans[stage]["ended_at"] is None
        assert spans[stage]["duration_seconds"] is None and spans[stage]["outcome"] == "unknown"
    assert spans["hpc_job_observed"]["duration_seconds"] == 10


@pytest.mark.parametrize("states,observed", [(["PD|"], "hpc_queue_observed"),
                                           (["R|node"], "hpc_job_observed")])
def test_stop_preserves_cancelled_observations_without_inventing_accounting(
        tmp_path, monkeypatch, states, observed):
    elapsed = clock(monkeypatch)
    monitor, path = monitor_at(tmp_path)
    host = Host(states, accounting="missing")
    with bind_monitor(monitor), pytest.raises(JobCancelled):
        run(host, should_cancel=lambda: elapsed[0] >= 10)
    assert host.cancelled
    spans = by_stage(path)
    assert spans[observed]["outcome"] == "cancelled"
    assert spans[observed]["duration_seconds"] == 10
    assert spans["hpc_execution"]["duration_seconds"] is None


def test_job_failure_is_recorded_from_terminal_state(tmp_path, monkeypatch):
    clock(monkeypatch)
    monitor, path = monitor_at(tmp_path)
    with bind_monitor(monitor):
        assert not run(Host(final="FAILED")).completed
    spans = by_stage(path)
    assert spans["hpc_execution"]["outcome"] == "failed"
    assert spans["hpc_job_observed"]["outcome"] == "failed"
    assert spans["hpc_queue"]["outcome"] == "success"  # allocation itself was obtained


def test_poll_count_does_not_grow_journal(tmp_path, monkeypatch):
    clock(monkeypatch)
    lengths = []
    for polls in (1, 70):
        monitor, path = monitor_at(tmp_path, f"run-{polls}")
        with bind_monitor(monitor):
            assert run(Host(["PD|"] * polls + ["R|node", ""])).completed
        lengths.append(len(path.read_text().splitlines()))
    assert lengths[0] == lengths[1]


def test_submitted_job_has_durable_partial_before_first_poll(tmp_path):
    monitor, path = monitor_at(tmp_path)

    class InspectDuringPoll(Host):
        def exec(self, command, timeout=60):
            if command.startswith("squeue"):
                span = by_stage(path)["hpc_queue_observed"]
                # A fresh reader, without the live monitor, can recover the job/run association.
                assert span["partial"] and span["outcome"] == "unknown"
                assert span["duration_seconds"] is None
                assert span["metadata"]["job_id"] == self.job_id
                assert read_timing(path)["run_id"] == "run-1"
                raise OSError("connection lost before first observation")
            return super().exec(command, timeout)

    with bind_monitor(monitor), pytest.raises(OSError):
        run(InspectDuringPoll(accounting="missing"))
    assert by_stage(path)["hpc_queue_observed"]["outcome"] == "failed"


def test_parallel_dag_workers_keep_their_own_run_context(tmp_path):
    journals = []
    with ContextThreadPoolExecutor(max_workers=2) as pool:
        futures = []
        for job_id in ("101", "202"):
            monitor, path = monitor_at(tmp_path, f"run-{job_id}")
            journals.append((job_id, path))
            with bind_monitor(monitor):
                futures.append(pool.submit(run, Host(job_id=job_id)))
        assert all(f.result().completed for f in futures)
        assert pool.submit(current_monitor).result() is None
    for job_id, path in journals:
        ids = {s["metadata"]["job_id"] for s in read_timing(path)["spans"] if "job_id" in s["metadata"]}
        assert ids == {job_id}
        assert read_timing(path)["run_id"] == f"run-{job_id}"


def test_analysis_executor_reaches_real_instrumented_job_lifecycle(tmp_path):
    monitor, path = monitor_at(tmp_path)
    executor = SlurmAnalysisExecutor(remote=Host(), container_image="/img.sif", scratch_dir="/scratch",
                                     remote_workspace="/work", remote_dataset="/dataset")
    with bind_monitor(monitor):
        result = executor.run_tool("annotate_variants", {}, ctx=None)
    assert result["status"] == "ok"
    spans = by_stage(path)
    assert spans["hpc_submit"]["outcome"] == "success"
    assert spans["hpc_execution"]["duration_seconds"] == 120


def test_cost_collection_and_stage_timing_share_one_accounting_query(tmp_path):
    from aiscientist.gateway.estimates import job_usage

    monitor, path = monitor_at(tmp_path)
    host = Host()
    usage = []
    token = job_usage.set(usage)
    try:
        with bind_monitor(monitor):
            assert run(host).completed
    finally:
        job_usage.reset(token)
    queries = [cmd for cmd, _ in host.commands if "--format=JobIDRaw" in cmd]
    assert len(queries) == 1
    assert usage == [{"seconds": 120, "cpu_hours": 4 * 120 / 3600, "gpu_hours": 0}]
    assert by_stage(path)["hpc_execution"]["duration_seconds"] == 120


def test_reattached_finished_job_can_record_accounting_without_resubmission(tmp_path):
    monitor, path = monitor_at(tmp_path)
    host = Host([""])
    with bind_monitor(monitor):
        assert slurm_job.reattach_job(host, "100").completed
    assert "hpc_submit" not in by_stage(path)
    assert by_stage(path)["hpc_execution"]["duration_seconds"] == 120
    assert not any(cmd.startswith("sbatch") for cmd, _ in host.commands)


@pytest.mark.parametrize("text", ["100.batch|1|2|3|COMPLETED", "COMPLETED", "other|1|2|3|COMPLETED"])
def test_accounting_never_uses_step_or_unidentified_rows(text):
    assert parse_timing_accounting(text, "100") is None


def test_truncated_cost_row_cannot_be_misread_as_timestamps():
    assert parse_timing_accounting("100|120|4|0|COMPLETED", "100", combined=True) is None


@pytest.mark.parametrize("text", ["100|1|Unknown|Unknown|CANCELLED", "100|1|nan|inf|FAILED",
                                 "100|3|2|1|COMPLETED", "100|1|2|3|RUNNING"])
def test_incomplete_or_inconsistent_accounting_has_no_execution_end(text):
    record = parse_timing_accounting(text, "100")
    assert record["ended"] is None
