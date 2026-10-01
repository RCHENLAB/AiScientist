"""The session's standing CPU worker node — where the agent's shell actually runs.

RCIC's rule is not "no wget". It is that **login nodes are for logging in and submitting jobs**;
this repo already draws that line for the Temp sweeper (see ``docs/hpc3_storage_layout.md``, which
runs ``find``/``rm`` as a batch job and leaves only ``mkdir``/``tail``/``sbatch``/``du`` on the
login node). A held CPU allocation extends the same rule to the agent: on a node you legitimately
hold, ``wget``, decompression, and full-text search are ordinary work.

That is what turns "give the agent a real shell" and "obey RCIC policy" from opposing
requirements into one design. The boundary becomes mechanical — a question of *where* a command
runs — instead of a guessed-at list of forbidden command names:

    login node   metadata + job control   ls / stat / du / squeue / sbatch / tail
    worker node  everything else          wget / tar / grep -r / bcftools / pip / run_code
    GPU job      needs an accelerator     its own sbatch; the user waits on Slurm
    access-hpc3  bulk transfer            put_file / get_file (the DTN)

Mechanics: one holder job per user (``sleep`` for its wall limit) in the free CPU partition, and
``srun --jobid=<id> --overlap`` to run each command as a step on it. ``--overlap`` is required
since Slurm 20.11 to share the allocation with the holder step. Cost per command is one ``srun``
(~a second), not a queue wait — which is the entire point, because a queue wait per ``ls`` is what
makes ``run_code`` unusable as a filesystem tool.

Everything here goes through the ``RemoteExecutor`` protocol, so the whole flow is testable
offline against a scripted fake — no Slurm, no SSH.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from .errors import GatewayError
from .executor import RemoteExecutor
from .settings import HPCSettings
from .slurm_job import AcquireConfig, SlurmJobError, SlurmJobSpec, acquire_allocation

JOB_NAME = "aiscientist-worker"

# Slurm states that mean the allocation is gone and a command cannot run on it.
_DEAD = {"CD", "CA", "F", "TO", "NF", "PR", "OOM", "ST"}


def job_name(username: str) -> str:
    """Per-user job name, so ``squeue --me --name=`` can only ever match your own worker."""
    safe = "".join(c for c in (username or "user") if c.isalnum() or c in "-_")
    return f"{JOB_NAME}-{safe}"


@dataclass
class WorkerAllocation:
    job_id: str
    node: str
    reused: bool = False

    def as_dict(self) -> dict:
        return {"job_id": self.job_id, "node": self.node, "reused": self.reused}


def _holder_script(settings: HPCSettings, name: str, log_dir: str) -> str:
    """A batch script whose only job is to HOLD the allocation open.

    ``sleep infinity`` rather than a computed duration: Slurm enforces the wall limit itself, and
    a hand-computed sleep that disagrees with ``--time`` just adds a second, wrong deadline. The
    node is released by ``--time``, by ``scancel`` on disconnect, or by the job being preempted.
    """
    account = f"#SBATCH --account={settings.cpu_account}\n" if settings.cpu_account else ""
    return (
        "#!/bin/bash\n"
        f"#SBATCH --job-name={name}\n"
        f"#SBATCH --partition={settings.cpu_partition}\n"
        f"{account}"
        f"#SBATCH --cpus-per-task={settings.worker_cpus}\n"
        f"#SBATCH --mem={settings.worker_mem_gb}G\n"
        f"#SBATCH --time={settings.worker_time_limit}\n"
        f"#SBATCH --output={log_dir}/{name}-%j.log\n\n"
        'echo "AiScientist worker holding $(hostname) from $(date)"\n'
        "sleep infinity\n"
    )


def find_running_worker(executor: RemoteExecutor, username: str = "") -> WorkerAllocation | None:
    """This user's own running worker, if one is already up.

    Scoped with ``squeue --me`` and the per-user job name, so a reconnect reuses your own node and
    can never see or adopt another member's allocation.
    """
    user = username or getattr(executor, "username", "") or ""
    result = executor.exec(
        f"squeue --me --name={job_name(user)} --states=R --noheader --format='%i|%N'")
    line = (result.out or "").splitlines()[0] if getattr(result, "out", "") else ""
    if not line or "|" not in line:
        return None
    job_id, _, node = line.partition("|")
    if not job_id.strip() or not node.strip():
        return None
    return WorkerAllocation(job_id=job_id.strip(), node=node.strip(), reused=True)


def ensure_worker(executor: RemoteExecutor, settings: HPCSettings, emit=None,
                  scratch_dir: str = "$HOME/.bioagent") -> WorkerAllocation:
    """Reuse this user's running worker, or submit and wait for one.

    Reuse-first is what keeps a reconnect cheap and stops a user accumulating idle CPU
    allocations across browser reloads — the same reasoning as ``gpu.find_running_job``.
    """
    emit = emit or (lambda *a, **k: None)
    existing = find_running_worker(executor, getattr(executor, "username", ""))
    if existing is not None:
        emit("info", "worker", f"Reusing your running worker node {existing.node} (job {existing.job_id}).")
        return existing

    user = getattr(executor, "username", "") or "user"
    name = job_name(user)
    log_dir = scratch_dir.rstrip("/")
    executor.exec(f"mkdir -p {shlex.quote(log_dir)}")
    spec = SlurmJobSpec(job_name=name, script=_holder_script(settings, name, log_dir),
                        submit_dir=log_dir)
    emit("step", "worker",
         f"Requesting a CPU worker node ({settings.worker_cpus} CPU / {settings.worker_mem_gb}G "
         f"on {settings.cpu_partition}) for the agent's shell ...")
    try:
        job_id, node, _ = acquire_allocation(
            executor, spec,
            config=AcquireConfig(startup_timeout_s=settings.worker_startup_timeout_s),
            emit=emit)
    except SlurmJobError as exc:
        raise GatewayError(
            f"Could not get a CPU worker node for the agent's shell: {exc}",
            stage="worker_alloc", detail=str(exc)) from exc
    emit("success", "worker", f"Agent shell is on worker node {node} (job {job_id}).")
    return WorkerAllocation(job_id=job_id, node=node)


def worker_is_alive(executor: RemoteExecutor, alloc: WorkerAllocation) -> bool:
    """Is the allocation still RUNNING? Cheap enough to check before a long command, which is
    better than discovering it mid-``srun`` and reporting a confusing step failure."""
    res = executor.exec(f"squeue --me --job={shlex.quote(alloc.job_id)} --noheader --format='%t'")
    state = (getattr(res, "out", "") or "").strip()
    return state == "R"


def srun_command(alloc: WorkerAllocation, command: str, *, timeout_s: int,
                 cpus: int = 1, chdir: str | None = None) -> str:
    """The ``srun`` line that runs ``command`` as a step on the held allocation.

    ``--overlap`` is required (Slurm >= 20.11) to share resources with the holder step; without
    it the step blocks forever waiting for resources the ``sleep`` already owns.

    ``--ntasks=1`` is **not optional**. Measured on HPC3: passing ``--cpus-per-task`` alone
    against a 4-CPU allocation launched the command as several parallel tasks, so it ran (and
    printed) three times. For ``echo`` that is a curiosity; for ``wget`` or ``pip install`` it is
    duplicated work racing on the same destination.

    Two independent limits, deliberately: ``--time`` is Slurm's, and the shell ``timeout`` is
    ours. Slurm's applies to the step, but a step that never starts would otherwise hang the
    caller, so the outer bound is the one that guarantees the tool call returns.
    """
    inner = f"timeout {int(timeout_s)}s bash -lc {shlex.quote(command)}"
    parts = ["srun", f"--jobid={shlex.quote(alloc.job_id)}", "--overlap", "--ntasks=1",
             f"--cpus-per-task={int(cpus)}", "--quiet"]
    if chdir:
        parts.append(f"--chdir={shlex.quote(chdir)}")
    return " ".join(parts) + " " + inner


def run_on_worker(executor: RemoteExecutor, alloc: WorkerAllocation, command: str, *,
                  timeout_s: int = 900, cpus: int = 1, chdir: str | None = None):
    """Run one command ON the worker node and return the executor's result.

    The SSH-level timeout is given headroom over the command's own, so a command that hits its
    limit reports ITS timeout (with whatever output it produced) rather than the transport
    tearing down first and losing the diagnosis.
    """
    line = srun_command(alloc, command, timeout_s=timeout_s, cpus=cpus, chdir=chdir)
    return executor.exec(line, timeout=float(timeout_s) + 30.0)


def release_worker(executor: RemoteExecutor, alloc: WorkerAllocation | None, emit=None) -> None:
    """``scancel`` the holder job. Best-effort: a worker left running is released by its wall
    limit anyway, so a failure here must never break disconnect."""
    if alloc is None:
        return
    emit = emit or (lambda *a, **k: None)
    try:
        executor.exec(f"scancel {shlex.quote(alloc.job_id)}")
        emit("info", "worker", f"Released worker node {alloc.node} (job {alloc.job_id}).")
    except Exception as exc:  # noqa: BLE001 - disconnect must not fail over housekeeping
        emit("warning", "worker", f"Could not release worker job {alloc.job_id}: {exc}")
