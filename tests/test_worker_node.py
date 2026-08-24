"""The session's standing CPU worker node: acquire, reuse, run on, release.

Offline — a scripted fake stands in for the SSH executor, so no Slurm and no cluster. What is
pinned here is the placement contract that makes the agent's shell RCIC-compliant: work runs as
an ``srun`` step on a held allocation, not on the login node.
"""

from __future__ import annotations

import re

import pytest

from bioagent.gateway import worker
from bioagent.gateway.errors import GatewayError
from bioagent.gateway.executor import ExecResult
from bioagent.gateway.settings import HPCSettings


class FakeExec:
    """Answers commands by regex, records everything, and defaults to a clean exit."""

    host = "hpc3.rcic.uci.edu"
    username = "alice"

    def __init__(self, rules=(), default=("", "", 0)) -> None:
        self.rules = list(rules)
        self.default = default
        self.cmds: list[str] = []

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.cmds.append(command)
        for pattern, out, err, code in self.rules:
            if re.search(pattern, command):
                return ExecResult(command, code, out, err)
        out, err, code = self.default
        return ExecResult(command, code, out, err)

    def matching(self, pattern: str) -> list[str]:
        return [c for c in self.cmds if re.search(pattern, c)]


def _settings(**kw) -> HPCSettings:
    return HPCSettings(**kw)


# --- naming + scoping --------------------------------------------------------


def test_job_name_is_per_user_and_sanitised():
    assert worker.job_name("alice") == "aiscientist-worker-alice"
    assert worker.job_name("a/../b") == "aiscientist-worker-ab"
    assert worker.job_name("") == "aiscientist-worker-user"


def test_lookup_is_scoped_to_your_own_jobs():
    """--me plus the per-user name: a worker search can never match another member's job."""
    ex = FakeExec()
    worker.find_running_worker(ex, "alice")
    (cmd,) = ex.matching("squeue")
    assert "--me" in cmd and "--name=aiscientist-worker-alice" in cmd and "--states=R" in cmd


@pytest.mark.parametrize("out", ["", "   ", "12345|", "|node-1", "garbage"])
def test_no_usable_worker_reads_as_none(out):
    assert worker.find_running_worker(FakeExec([(r"squeue", out, "", 0)]), "alice") is None


# --- acquire / reuse ---------------------------------------------------------


def test_an_existing_worker_is_reused_not_duplicated():
    """A browser reload must not leave the user accumulating idle CPU allocations."""
    ex = FakeExec([(r"squeue.*--name=", "998877|hpc3-14-05", "", 0)])
    alloc = worker.ensure_worker(ex, _settings())

    assert (alloc.job_id, alloc.node, alloc.reused) == ("998877", "hpc3-14-05", True)
    assert ex.matching("sbatch") == [], "no second allocation was requested"


def test_a_fresh_worker_is_submitted_to_the_free_cpu_partition():
    ex = FakeExec([
        (r"squeue.*--name=", "", "", 0),                      # nothing running yet
        (r"sbatch", "Submitted batch job 4242", "", 0),
        (r"squeue -j", "R|hpc3-20-01", "", 0),
    ])
    alloc = worker.ensure_worker(ex, _settings(cpu_partition="standard", cpu_account="ruic20_lab",
                                               worker_cpus=4, worker_mem_gb=16))
    assert (alloc.job_id, alloc.node, alloc.reused) == ("4242", "hpc3-20-01", False)

    script = next(c for c in ex.cmds if "sleep infinity" in c)
    assert "--partition=standard" in script
    assert "--account=ruic20_lab" in script
    assert "--cpus-per-task=4" in script and "--mem=16G" in script
    assert "--gres" not in script, "the worker holds a CPU node, never an accelerator"


def test_the_holder_sleeps_rather_than_computing_a_second_deadline():
    """Slurm's --time is the one wall limit; a hand-computed sleep would just disagree with it."""
    ex = FakeExec([(r"squeue.*--name=", "", "", 0), (r"sbatch", "Submitted batch job 1", "", 0),
                   (r"squeue -j", "R|n1", "", 0)])
    worker.ensure_worker(ex, _settings(worker_time_limit="08:00:00"))
    script = next(c for c in ex.cmds if "sleep infinity" in c)
    assert "--time=08:00:00" in script
    assert not re.search(r"sleep \d", script)


def test_a_worker_that_never_starts_reports_a_stated_cause():
    ex = FakeExec([(r"squeue.*--name=", "", "", 0), (r"sbatch", "Submitted batch job 7", "", 0),
                   (r"squeue -j", "PD|", "", 0)])
    with pytest.raises(GatewayError) as exc:
        worker.ensure_worker(ex, _settings(worker_startup_timeout_s=0))
    assert exc.value.stage == "worker_alloc"
    assert "CPU worker node" in str(exc.value)


# --- running commands on it --------------------------------------------------


def test_commands_run_as_a_step_on_the_allocation_not_the_login_node():
    """The whole point: `srun --overlap` against the held job, so byte-moving work never
    executes where RCIC forbids it."""
    line = worker.srun_command(worker.WorkerAllocation("4242", "n1"), "wget https://x/y.gz",
                               timeout_s=600)
    assert line.startswith("srun ")
    assert "--jobid=4242" in line
    assert "--overlap" in line, "required since Slurm 20.11 to share the holder's allocation"
    assert "wget https://x/y.gz" in line


def test_each_command_carries_its_own_wall_bound():
    """Distinct from the ALLOCATION's limit: this stops one runaway `find /` holding the session."""
    line = worker.srun_command(worker.WorkerAllocation("1", "n"), "find /", timeout_s=120)
    assert "timeout 120s" in line


def test_a_command_is_quoted_as_one_unit():
    """Shell metacharacters in the agent's command must reach the worker as DATA, not become a
    second command the login shell runs on its way there."""
    import shlex

    payload = "grep -r 'needle; rm -rf /' /data"
    line = worker.srun_command(worker.WorkerAllocation("1", "n"), payload, timeout_s=60)

    argv = shlex.split(line)
    assert argv[-1] == payload, "the whole command is one argv element after the login shell parses it"
    assert argv[:2] == ["srun", "--jobid=1"]


def test_chdir_is_passed_to_srun_when_given():
    line = worker.srun_command(worker.WorkerAllocation("1", "n"), "ls", timeout_s=10,
                               chdir="/dfs3b/x y")
    assert "--chdir=" in line and "'/dfs3b/x y'" in line


def test_ssh_timeout_exceeds_the_command_timeout():
    """So a command that hits its limit reports ITS timeout with partial output, instead of the
    transport tearing down first and losing the diagnosis."""
    seen = {}

    class E(FakeExec):
        def exec(self, command, timeout=60.0):
            seen["timeout"] = timeout
            return super().exec(command, timeout)

    worker.run_on_worker(E(), worker.WorkerAllocation("1", "n"), "sleep 5", timeout_s=300)
    assert seen["timeout"] > 300


def test_liveness_check_distinguishes_running_from_gone():
    alloc = worker.WorkerAllocation("4242", "n1")
    assert worker.worker_is_alive(FakeExec([(r"squeue", "R", "", 0)]), alloc) is True
    assert worker.worker_is_alive(FakeExec([(r"squeue", "", "", 0)]), alloc) is False
    assert worker.worker_is_alive(FakeExec([(r"squeue", "CG", "", 0)]), alloc) is False


# --- release -----------------------------------------------------------------


def test_release_cancels_the_holder_job():
    ex = FakeExec()
    worker.release_worker(ex, worker.WorkerAllocation("4242", "n1"))
    assert ex.matching(r"scancel 4242")


def test_release_never_breaks_disconnect():
    class Boom(FakeExec):
        def exec(self, command, timeout=60.0):
            raise RuntimeError("ssh already closed")

    worker.release_worker(Boom(), worker.WorkerAllocation("1", "n"))   # must not raise
    worker.release_worker(FakeExec(), None)


def test_a_command_runs_exactly_once():
    """Measured on HPC3: `--cpus-per-task` WITHOUT `--ntasks=1` launched the command as several
    parallel tasks on a multi-CPU allocation — it printed three times. Harmless for `echo`,
    duplicated work racing on one destination for `wget` or `pip install`."""
    line = worker.srun_command(worker.WorkerAllocation("1", "n"), "pip install x",
                               timeout_s=60, cpus=4)
    assert "--ntasks=1" in line
    assert "--cpus-per-task=4" in line
