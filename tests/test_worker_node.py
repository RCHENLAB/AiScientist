"""The session's standing CPU worker node: acquire, reuse, run on, release.

Offline — a scripted fake stands in for the SSH executor, so no Slurm and no cluster. What is
pinned here is the placement contract that makes the agent's shell RCIC-compliant: work runs as
an ``srun`` step on a held allocation, not on the login node.
"""

from __future__ import annotations

import re

import pytest

from aiscientist.gateway import worker
from aiscientist.gateway.errors import GatewayError
from aiscientist.gateway.executor import ExecResult
from aiscientist.gateway.settings import HPCSettings


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


# A line only the holder's batch script contains: finds it among the recorded commands.
HOLDER = "AiScientist worker holding"


# The directory the gateway gives the worker: <shared_root>/Temp/<user>/scratch/worker.
SCRATCH = "/dfs/lab/Temp/alice/scratch/worker"
# A directory given with a shell variable in it — what the worker had before the gateway named an
# absolute one. It is still resolved on the cluster, never taken as written.
HOME_DIR = "$HOME/.bioagent"


@pytest.fixture(autouse=True)
def _forget_resolved_dirs():
    """The home lookup is cached per user for the process; every FakeExec here is "alice"."""
    worker._RESOLVED_DIRS.clear()
    yield
    worker._RESOLVED_DIRS.clear()


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
    alloc = worker.ensure_worker(ex, _settings(), scratch_dir=SCRATCH)

    assert (alloc.job_id, alloc.node, alloc.reused) == ("998877", "hpc3-14-05", True)
    assert ex.matching("sbatch") == [], "no second allocation was requested"


def test_a_fresh_worker_is_submitted_to_the_free_cpu_partition():
    ex = FakeExec([
        (r"squeue.*--name=", "", "", 0),                      # nothing running yet
        (r"sbatch", "Submitted batch job 4242", "", 0),
        (r"squeue -j", "R|hpc3-20-01", "", 0),
    ])
    alloc = worker.ensure_worker(ex, _settings(cpu_partition="standard", cpu_account="ruic20_lab",
                                               worker_cpus=4, worker_mem_gb=16), scratch_dir=SCRATCH)
    assert (alloc.job_id, alloc.node, alloc.reused) == ("4242", "hpc3-20-01", False)

    script = next(c for c in ex.cmds if HOLDER in c)
    assert "--partition=standard" in script
    assert "--account=ruic20_lab" in script
    assert "--cpus-per-task=4" in script and "--mem=16G" in script
    assert "--gres" not in script, "the worker holds a CPU node, never an accelerator"


def test_the_holder_script_and_log_go_to_the_scratch_dir_not_the_home_directory():
    """The holder's log path sat in ``#SBATCH --output`` as ``$HOME/.bioagent/...``; Slurm does not
    expand that, so the log went to a directory literally named ``$HOME`` in the user's home."""
    ex = FakeExec([(r"squeue.*--name=", "", "", 0), (r"sbatch", "Submitted batch job 1", "", 0),
                   (r"squeue -j", "R|n1", "", 0)])
    worker.ensure_worker(ex, _settings(), scratch_dir=SCRATCH)
    name = "aiscientist-worker-alice"
    script = next(c for c in ex.cmds if HOLDER in c)
    assert f"#SBATCH --output={SCRATCH}/{name}-%j.log" in script
    assert f"cat > {SCRATCH}/{name}.sbatch" in script
    assert ex.matching(r"^sbatch ") == [f"sbatch {SCRATCH}/{name}.sbatch"]
    assert not any("$HOME" in c or ".bioagent" in c for c in ex.cmds)


def test_the_holder_ends_itself_when_nothing_uses_it():
    """Issue 23: the holder was `sleep infinity`, so it kept 4 CPUs and 16 GB of the lab's
    allocation for its whole 8-hour limit, idle or not, and outlived a gateway that restarted. It
    now ends itself once its heartbeat goes stale; Slurm's --time is still the hard limit."""
    ex = FakeExec([(r"squeue.*--name=", "", "", 0), (r"sbatch", "Submitted batch job 1", "", 0),
                   (r"squeue -j", "R|n1", "", 0)])
    st = _settings(worker_time_limit="08:00:00", worker_idle_timeout_s=900)
    worker.ensure_worker(ex, st, scratch_dir=SCRATCH)
    script = next(c for c in ex.cmds if HOLDER in c)

    assert "sleep infinity" not in script
    assert "--time=08:00:00" in script
    limit = worker.holder_idle_limit_s(st)
    assert limit == 900 + worker.HOLDER_GRACE_S, "the gateway's idle release always acts first"
    assert f"-gt {limit} ]" in script and "exit 0" in script
    assert f'hb={SCRATCH}/aiscientist-worker-alice-"$SLURM_JOB_ID".alive' in script


# What a fresh submission answers, with and without a shell that expands $HOME.
_SUBMIT = [(r"squeue.*--name=", "", "", 0), (r"sbatch", "Submitted batch job 4242", "", 0),
           (r"squeue -j", "R|hpc3-20-01", "", 0)]
_HOME = (r"^echo \$HOME", "/data/homezvol0/alice/.bioagent\n", "", 0)


def _directives(ex: FakeExec) -> list[str]:
    script = next(c for c in ex.cmds if HOLDER in c)
    return [line for line in script.splitlines() if line.startswith("#SBATCH")]


def test_the_holder_log_path_is_absolute_because_slurm_does_not_expand_home():
    """Slurm takes ``#SBATCH`` lines as written. Left as ``$HOME/.bioagent``, the log landed in a
    directory NAMED ``$HOME`` under the user's home (seen on HPC3) — and the quoted ``mkdir`` is
    what created it. The path is resolved on the cluster first; nothing after that names $HOME."""
    ex = FakeExec([_HOME, *_SUBMIT])
    worker.ensure_worker(ex, _settings(), scratch_dir=HOME_DIR)

    directives = _directives(ex)
    assert directives and not [d for d in directives if "$" in d]
    assert ("#SBATCH --output=/data/homezvol0/alice/.bioagent/aiscientist-worker-alice-%j.log"
            in directives)
    assert ex.matching(r"^mkdir -p /data/homezvol0/alice/\.bioagent$")
    assert ex.matching(r"^sbatch /data/homezvol0/alice/\.bioagent/aiscientist-worker-alice\.sbatch$")
    assert [c for c in ex.cmds if "$HOME" in c] == ["echo $HOME/.bioagent"]


def test_the_home_lookup_is_made_once_per_user():
    """A worker is requested again whenever its allocation ends; the home has not moved."""
    ex = FakeExec([_HOME, *_SUBMIT])
    worker.ensure_worker(ex, _settings(), scratch_dir=HOME_DIR)
    worker.ensure_worker(ex, _settings(), scratch_dir=HOME_DIR)

    assert len(ex.matching(r"^sbatch ")) == 2
    assert len(ex.matching(r"^echo \$HOME")) == 1

    class Bob(FakeExec):
        username = "bob"

    bob = Bob([(r"^echo \$HOME", "/data/homezvol1/bob/.bioagent", "", 0), *_SUBMIT])
    worker.ensure_worker(bob, _settings(), scratch_dir=HOME_DIR)
    assert "#SBATCH --output=/data/homezvol1/bob/.bioagent/aiscientist-worker-bob-%j.log" \
        in _directives(bob), "one member's home is never another's"


@pytest.mark.parametrize("answer", [("", "", 0), ("$HOME/.bioagent", "", 0),
                                    ("/data/homezvol0/alice/.bioagent", "no shell", 1),
                                    (".bioagent", "", 0)])
def test_a_home_the_cluster_cannot_expand_keeps_the_given_path(answer):
    """Offline and mock hosts echo nothing usable: the worker is still submitted with the path as
    given, and the miss is not remembered — the next submission asks again."""
    ex = FakeExec([(r"^echo \$HOME", *answer), *_SUBMIT])
    worker.ensure_worker(ex, _settings(), scratch_dir=HOME_DIR)
    assert "#SBATCH --output=$HOME/.bioagent/aiscientist-worker-alice-%j.log" in _directives(ex)

    again = FakeExec([_HOME, *_SUBMIT])
    worker.ensure_worker(again, _settings(), scratch_dir=HOME_DIR)
    assert not [d for d in _directives(again) if "$" in d]


def test_a_worker_that_never_starts_reports_a_stated_cause():
    ex = FakeExec([(r"squeue.*--name=", "", "", 0), (r"sbatch", "Submitted batch job 7", "", 0),
                   (r"squeue -j", "PD|", "", 0)])
    with pytest.raises(GatewayError) as exc:
        worker.ensure_worker(ex, _settings(worker_startup_timeout_s=0), scratch_dir=SCRATCH)
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


# --- held only while used: heartbeat, self-exit, leftover sweep (issue 23) ----


def test_a_fresh_and_a_reused_worker_both_carry_their_heartbeat_file():
    """Commands on a REUSED holder must refresh its heartbeat too, or it ends under them."""
    fresh = FakeExec([(r"squeue.*--name=", "", "", 0), (r"sbatch", "Submitted batch job 4242", "", 0),
                      (r"squeue -j", "R|n1", "", 0)])
    alloc = worker.ensure_worker(fresh, _settings(), scratch_dir=SCRATCH)
    assert alloc.heartbeat == f"{SCRATCH}/aiscientist-worker-alice-4242.alive"

    reused = worker.ensure_worker(FakeExec([(r"squeue.*--name=", "998877|n2", "", 0)]),
                                  _settings(), scratch_dir=SCRATCH)
    assert reused.reused and reused.heartbeat == f"{SCRATCH}/aiscientist-worker-alice-998877.alive"


def test_a_command_refreshes_the_heartbeat_and_stays_one_unit():
    import shlex

    hb = f"{SCRATCH}/aiscientist-worker-alice-7.alive"
    payload = "grep -r 'needle; rm -rf /' /data"
    line = worker.srun_command(worker.WorkerAllocation("7", "n", heartbeat=hb), payload, timeout_s=60)

    argv = shlex.split(line)
    assert argv[:2] == ["srun", "--jobid=7"] and argv[-3:-1] == ["bash", "-c"]
    script = argv[-1]
    assert script.startswith(f"touch {hb} ") and f"touch {hb} 2>/dev/null; exit $rc" in script
    assert f"sleep {worker.HEARTBEAT_EVERY_S}; do touch {hb}" in script
    assert "timeout 60s bash -lc " + shlex.quote(payload) in script


def _timeout_shim(tmp_path):
    """GNU `timeout` where the host has none (macOS): drop the duration and run the rest."""
    import os
    import shutil

    if shutil.which("timeout"):
        return os.environ["PATH"]
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "timeout").write_text('#!/bin/bash\nshift\nexec "$@"\n')
    (shim / "timeout").chmod(0o755)
    return f"{shim}:{os.environ['PATH']}"


def test_the_heartbeat_wrapper_keeps_the_commands_output_and_exit_status(tmp_path):
    """Executed, not just read: the wrapper passes the command's output and exit status through,
    leaves the heartbeat touched, and returns at once — its refresher does not hold the step's
    stdout open for another minute."""
    import shlex
    import subprocess
    import time

    hb = tmp_path / "w-1.alive"
    line = worker.srun_command(worker.WorkerAllocation("1", "n", heartbeat=str(hb)),
                               "echo out; echo err >&2; exit 3", timeout_s=30)
    script = shlex.split(line)[-1]
    started = time.monotonic()
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                          env={"PATH": _timeout_shim(tmp_path), "HOME": str(tmp_path)})
    assert time.monotonic() - started < 10, "the refresher kept the command from returning"
    assert done.returncode == 3
    assert "out" in done.stdout and "err" in done.stderr
    assert hb.exists()


def _gnu_tools() -> bool:
    import subprocess

    try:
        return subprocess.run(["bash", "-c", "stat -c %Y / && date -d @0"], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _gnu_tools(), reason="the holder runs on Linux (GNU stat/date)")
def test_the_holder_exits_once_idle_and_stays_while_touched(tmp_path):
    """Executed with a 1-second check and a 3-second limit: touched, it keeps holding; left
    alone, it removes its heartbeat and exits 0 (the job ends, the node goes back)."""
    import os
    import subprocess
    import time

    script = worker._holder_script(_settings(cpu_account=None), "aiscientist-worker-alice",
                                   str(tmp_path), check_every_s=1, idle_limit_s=3)
    path = tmp_path / "holder.sh"
    path.write_text(script)
    hb = tmp_path / "aiscientist-worker-alice-55.alive"
    proc = subprocess.Popen(["bash", str(path)], stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "SLURM_JOB_ID": "55"})
    try:
        for _ in range(6):                     # 6 s of use: longer than the limit
            time.sleep(1)
            hb.touch()
            assert proc.poll() is None, "a holder in use must not end itself"
        assert proc.wait(timeout=15) == 0
        assert not hb.exists()
        assert "releasing" in proc.stdout.read()
    finally:
        proc.kill()


SWEEP_BEAT = f"{SCRATCH}/aiscientist-worker-alice-{{}}.alive"


def _sweep(ex: FakeExec, **kw) -> list[str]:
    return worker.sweep_stale_workers(ex, log_dir=SCRATCH, stale_after_s=900, **kw)


def test_the_sweep_cancels_only_holders_nothing_is_using():
    now = 1_000_000
    ex = FakeExec([
        (r"^squeue --me --name=aiscientist-worker-alice --states=R",
         "11|1:02:03\n22|40:00\n33|5:00\n44|0:30\n55|2:00:00\n", "", 0),
        (r"^date \+%s; stat",
         f"{now}\n{SWEEP_BEAT.format(11)}|{now - 60}\n{SWEEP_BEAT.format(22)}|{now - 3600}\n"
         f"{SWEEP_BEAT.format(55)}|{now - 5000}\n", "", 1),
    ])
    gone = _sweep(ex, keep={"55"})
    # 11 in use (fresh heartbeat); 22 idle an hour; 33 has no heartbeat and is 5 min old (a holder
    # from before the fix); 44 has none but is 30 s old (still writing it); 55 a live session's.
    assert gone == ["22", "33"]
    assert ex.matching(r"^scancel ") == ["scancel 22 33"]


def test_the_sweep_is_scoped_to_your_own_named_jobs():
    ex = FakeExec()
    assert _sweep(ex) == []
    (cmd,) = ex.matching("squeue")
    assert "--me" in cmd and "--name=aiscientist-worker-alice" in cmd
    assert ex.matching("scancel") == [] and ex.matching(r"stat -c") == []


def test_the_sweep_never_cancels_on_a_guess():
    """No clock from the cluster: nothing to compare a heartbeat with, so nothing is cancelled."""
    ex = FakeExec([(r"^squeue", "22|9:00:00", "", 0), (r"^date", "", "ssh hiccup", 255)])
    assert _sweep(ex) == [] and ex.matching("scancel") == []


@pytest.mark.parametrize("text,secs", [("0:30", 30), ("5:00", 300), ("1:02:03", 3723),
                                       ("2-01:00:00", 176400), ("INVALID", None), ("", None)])
def test_squeue_elapsed_time_is_read_in_seconds(text, secs):
    assert worker._elapsed_s(text) == secs


def test_release_says_why():
    said = []
    worker.release_worker(FakeExec(), worker.WorkerAllocation("4242", "n1"),
                          lambda *a: said.append(a), reason=" because you logged out")
    assert said == [("info", "worker", "Released worker node n1 (job 4242) because you logged out.")]
