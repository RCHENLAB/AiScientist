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

Mechanics: one holder job per user in the CPU partition, and ``srun --jobid=<id> --overlap`` to
run each command as a step on it. ``--overlap`` is required since Slurm 20.11 to share the
allocation with the holder step. Cost per command is one ``srun`` (~a second), not a queue wait —
which is the entire point, because a queue wait per ``ls`` is what makes ``run_code`` unusable as a
filesystem tool.

The holder is charged to the lab's account and keeps its CPUs and memory from everyone else for as
long as it runs, so it is held only while the agent is using it (RCHENLAB/AiScientist issue 23):

* the gateway releases it after ``worker_idle_timeout_s`` without a command (``app.py``), and when
  the session disconnects, the user logs out or the server stops;
* the holder ends itself once its heartbeat file has gone that long without a touch (every command
  refreshes it, see ``srun_command``), so a gateway that crashed or restarted cannot strand it;
* each new session cancels the user's leftover holders that nothing is using
  (``sweep_stale_workers``).

The next command after a release requests a new holder; on the CPU partition that is a short wait.

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

# How often a running command refreshes the heartbeat, and how often the holder reads it.
HEARTBEAT_EVERY_S = 60
# How long the holder outlives the gateway's own idle release before it ends itself. The gateway
# acts first; the holder's limit is only the backstop for a gateway that is gone.
HOLDER_GRACE_S = 300
# A holder younger than this may not have written its first heartbeat yet.
_NEW_HOLDER_S = 120


def job_name(username: str) -> str:
    """Per-user job name, so ``squeue --me --name=`` can only ever match your own worker."""
    safe = "".join(c for c in (username or "user") if c.isalnum() or c in "-_")
    return f"{JOB_NAME}-{safe}"


def heartbeat_path(log_dir: str, name: str, job_id: str) -> str:
    """The file a holder watches: every command on it refreshes the file's mtime."""
    return f"{log_dir.rstrip('/')}/{name}-{job_id}.alive"


@dataclass
class WorkerAllocation:
    job_id: str
    node: str
    reused: bool = False
    # The holder's heartbeat file (see ``heartbeat_path``); "" runs commands without touching one.
    heartbeat: str = ""

    def as_dict(self) -> dict:
        return {"job_id": self.job_id, "node": self.node, "reused": self.reused}


# (host, user, directory as given) -> the absolute path the cluster expands it to. A user's home
# does not move, so one ``echo`` serves every later worker this gateway process submits for them.
_RESOLVED_DIRS: dict[tuple[str, str, str], str] = {}


def _resolved_dir(executor: RemoteExecutor, path: str) -> str:
    """Expand shell vars (``$HOME``) in ``path`` to an absolute remote path, ONCE per user.

    The directory is used in the SSH shell, where ``$HOME`` expands, but also in
    ``#SBATCH --output``, where **Slurm does not expand it**: the holder's log went to a directory
    literally named ``$HOME`` under the user's home. Resolving up front fixes every use site (same
    as ``SlurmSandboxExecutor._resolved_scratch``). If the cluster cannot expand it (offline/mock),
    keep the literal so behaviour is unchanged rather than guessed — and do not cache the miss.
    """
    if "$" not in path:
        return path
    key = (getattr(executor, "host", "") or "", getattr(executor, "username", "") or "", path)
    if key not in _RESOLVED_DIRS:
        got = executor.exec(f"echo {path}")
        lines = (getattr(got, "out", "") or "").splitlines()
        if not (getattr(got, "ok", False) and lines and lines[0].startswith("/")
                and "$" not in lines[0]):
            return path
        _RESOLVED_DIRS[key] = lines[0].strip().rstrip("/")
    return _RESOLVED_DIRS[key]


def holder_idle_limit_s(settings: HPCSettings) -> int:
    """Seconds without a command after which the holder ends itself."""
    return max(60, int(settings.worker_idle_timeout_s)) + HOLDER_GRACE_S


def _holder_script(settings: HPCSettings, name: str, log_dir: str, *,
                   check_every_s: int = HEARTBEAT_EVERY_S, idle_limit_s: int | None = None) -> str:
    """A batch script that holds the allocation open while the agent uses it, then ends itself.

    It was ``sleep infinity``, which held 4 CPUs and 16 GB of the lab's allocation for the whole
    8-hour wall limit whether or not the agent ever ran another command, and outlived the gateway
    that asked for it (issue 23). Now it watches a heartbeat file that every command refreshes
    (``srun_command``) and exits once the file has gone ``holder_idle_limit_s`` without a touch. The
    gateway normally releases an idle holder first; this limit is what still applies when the
    gateway restarted, crashed or lost the session. ``--time`` remains Slurm's hard wall limit.

    A heartbeat that cannot be read keeps the last value seen, so a deleted file or a slow stat
    still ends an idle holder and never makes a busy one look idle for longer than a check.

    ``log_dir`` must already be absolute (see ``_resolved_dir``): Slurm takes ``--output`` as
    written, relative to the submit directory.
    """
    account = f"#SBATCH --account={settings.cpu_account}\n" if settings.cpu_account else ""
    limit = int(idle_limit_s) if idle_limit_s is not None else holder_idle_limit_s(settings)
    # Quoted prefix + the job id from the environment: the file name carries this job's id.
    hb = shlex.quote(f"{log_dir.rstrip('/')}/{name}-") + '"$SLURM_JOB_ID".alive'
    return (
        "#!/bin/bash\n"
        f"#SBATCH --job-name={name}\n"
        f"#SBATCH --partition={settings.cpu_partition}\n"
        f"{account}"
        f"#SBATCH --cpus-per-task={settings.worker_cpus}\n"
        f"#SBATCH --mem={settings.worker_mem_gb}G\n"
        f"#SBATCH --time={settings.worker_time_limit}\n"
        f"#SBATCH --output={log_dir}/{name}-%j.log\n\n"
        f"hb={hb}\n"
        'touch "$hb"\n'
        'echo "AiScientist worker holding $(hostname) from $(date);'
        f' it ends itself after {limit}s without a command"\n'
        "last=$(date +%s)\n"
        f"while sleep {int(check_every_s)}; do\n"
        '  m=$(stat -c %Y "$hb" 2>/dev/null) && last=$m\n'
        f"  if [ $(( $(date +%s) - last )) -gt {limit} ]; then\n"
        '    echo "AiScientist worker: no command since $(date -d @$last), releasing $(hostname) at $(date)"\n'
        '    rm -f "$hb"\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
    )


def find_running_worker(executor: RemoteExecutor, username: str = "", *,
                        log_dir: str = "") -> WorkerAllocation | None:
    """This user's own running worker, if one is already up.

    Scoped with ``squeue --me`` and the per-user job name, so a reconnect reuses your own node and
    can never see or adopt another member's allocation. With ``log_dir`` the allocation carries its
    heartbeat file, so the commands run on a reused holder keep it from ending itself.
    """
    user = username or getattr(executor, "username", "") or ""
    name = job_name(user)
    result = executor.exec(
        f"squeue --me --name={name} --states=R --noheader --format='%i|%N'")
    line = (result.out or "").splitlines()[0] if getattr(result, "out", "") else ""
    if not line or "|" not in line:
        return None
    job_id, _, node = line.partition("|")
    if not job_id.strip() or not node.strip():
        return None
    return WorkerAllocation(job_id=job_id.strip(), node=node.strip(), reused=True,
                            heartbeat=heartbeat_path(log_dir, name, job_id.strip()) if log_dir else "")


def _elapsed_s(text: str) -> int | None:
    """squeue's ``%M`` (``M:SS``, ``H:MM:SS`` or ``D-HH:MM:SS``) in seconds; None if unreadable."""
    text = (text or "").strip()
    days = 0
    if "-" in text:
        d, _, text = text.partition("-")
        if not d.isdigit():
            return None
        days = int(d)
    parts = text.split(":")
    if not parts or len(parts) > 3 or not all(p.isdigit() for p in parts):
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return days * 86400 + secs


def sweep_stale_workers(executor: RemoteExecutor, *, log_dir: str, keep=(), stale_after_s: int,
                        username: str = "", emit=None) -> list[str]:
    """``scancel`` this user's running holders that nothing is using; returns the cancelled ids.

    Called when a session starts, so a holder left by a gateway that restarted or crashed (or by
    the code before issue 23, whose ``sleep infinity`` never ended) does not wait out its 8-hour
    limit. A holder is left alone when its job id is in ``keep`` (a live session of this process
    holds it) or its heartbeat was touched within ``stale_after_s`` (another process is using it).
    One with no heartbeat file is stale once it is older than a fresh holder needs to write one.

    Login-node work only: one ``squeue``, one ``stat``, one ``scancel``.
    """
    emit = emit or (lambda *a, **k: None)
    user = username or getattr(executor, "username", "") or ""
    name = job_name(user)
    res = executor.exec(f"squeue --me --name={name} --states=R --noheader --format='%i|%M'")
    jobs: dict[str, int | None] = {}
    for line in (getattr(res, "out", "") or "").splitlines():
        jid, _, elapsed = line.strip().partition("|")
        if jid.strip().isdigit() and jid.strip() not in keep:
            jobs[jid.strip()] = _elapsed_s(elapsed)
    if not jobs:
        return []
    beats = {heartbeat_path(log_dir, name, jid): jid for jid in jobs}
    got = executor.exec("date +%s; stat -c '%n|%Y' "
                        + " ".join(shlex.quote(p) for p in beats) + " 2>/dev/null")
    lines = (getattr(got, "out", "") or "").splitlines()
    if not lines or not lines[0].strip().isdigit():
        return []                  # no clock to compare against: never cancel on a guess
    now = int(lines[0].strip())
    touched: dict[str, int] = {}
    for line in lines[1:]:
        path, _, mtime = line.strip().rpartition("|")
        if path in beats and mtime.isdigit():
            touched[beats[path]] = int(mtime)
    stale = []
    for jid, elapsed in sorted(jobs.items()):
        if jid in touched:
            if now - touched[jid] > stale_after_s:
                stale.append(jid)
        elif elapsed is not None and elapsed > _NEW_HOLDER_S:
            stale.append(jid)
    if not stale:
        return []
    executor.exec("scancel " + " ".join(stale))       # every id came from `squeue --me`
    emit("info", "worker", "Cancelled your CPU worker job(s) " + ", ".join(stale)
         + " on HPC3: no session was using them.")
    return stale


def ensure_worker(executor: RemoteExecutor, settings: HPCSettings, emit=None, *,
                  scratch_dir: str) -> WorkerAllocation:
    """Reuse this user's running worker, or submit and wait for one.

    ``scratch_dir`` holds the holder job's script and log. It must be an absolute directory under
    the application's storage: Slurm does not expand ``$HOME`` in ``#SBATCH --output``, so a path
    with a shell variable in it is created literally (a directory named ``$HOME``).

    Reuse-first is what keeps a reconnect cheap and stops a user accumulating idle CPU
    allocations across browser reloads — the same reasoning as ``gpu.find_running_job``.
    """
    emit = emit or (lambda *a, **k: None)
    user = getattr(executor, "username", "") or "user"
    name = job_name(user)
    # Absolute before it reaches `#SBATCH --output`, the quoted `mkdir`, the submit directory and
    # the heartbeat path a reused holder is given.
    log_dir = _resolved_dir(executor, scratch_dir.rstrip("/"))
    existing = find_running_worker(executor, getattr(executor, "username", ""), log_dir=log_dir)
    if existing is not None:
        emit("info", "worker", f"Reusing your running worker node {existing.node} (job {existing.job_id}).")
        return existing

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
    return WorkerAllocation(job_id=job_id, node=node,
                            heartbeat=heartbeat_path(log_dir, name, job_id))


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

    With a heartbeat file the command refreshes it when it starts, every ``HEARTBEAT_EVERY_S``
    while it runs and when it ends, so the holder never ends itself under a long command. The
    refresher's output goes to /dev/null: it must not hold the step's stdout open after the
    command is done.
    """
    inner = f"timeout {int(timeout_s)}s bash -lc {shlex.quote(command)}"
    if alloc.heartbeat:
        inner = "bash -c " + shlex.quote(_with_heartbeat(alloc.heartbeat, inner))
    parts = ["srun", f"--jobid={shlex.quote(alloc.job_id)}", "--overlap", "--ntasks=1",
             f"--cpus-per-task={int(cpus)}", "--quiet"]
    if chdir:
        parts.append(f"--chdir={shlex.quote(chdir)}")
    return " ".join(parts) + " " + inner


def _with_heartbeat(heartbeat: str, inner: str) -> str:
    """``inner`` wrapped so it keeps ``heartbeat`` fresh, and keeps ``inner``'s exit status."""
    hb = shlex.quote(heartbeat)
    return (f"touch {hb} 2>/dev/null; "
            f"( while sleep {HEARTBEAT_EVERY_S}; do touch {hb} 2>/dev/null; done ) "
            "</dev/null >/dev/null 2>&1 & beat=$!; "
            f"{inner}; rc=$?; kill $beat 2>/dev/null; touch {hb} 2>/dev/null; exit $rc")


def run_on_worker(executor: RemoteExecutor, alloc: WorkerAllocation, command: str, *,
                  timeout_s: int = 900, cpus: int = 1, chdir: str | None = None):
    """Run one command ON the worker node and return the executor's result.

    The SSH-level timeout is given headroom over the command's own, so a command that hits its
    limit reports ITS timeout (with whatever output it produced) rather than the transport
    tearing down first and losing the diagnosis.
    """
    line = srun_command(alloc, command, timeout_s=timeout_s, cpus=cpus, chdir=chdir)
    return executor.exec(line, timeout=float(timeout_s) + 30.0)


def release_worker(executor: RemoteExecutor, alloc: WorkerAllocation | None, emit=None, *,
                   reason: str = "") -> None:
    """``scancel`` the holder job. Best-effort: a holder left running ends itself once its
    heartbeat goes stale, so a failure here must never break disconnect, logout or shutdown.
    ``reason`` completes the sentence "Released worker node N (job J)"."""
    if alloc is None:
        return
    emit = emit or (lambda *a, **k: None)
    try:
        executor.exec(f"scancel {shlex.quote(alloc.job_id)}", timeout=20.0)
        emit("info", "worker", f"Released worker node {alloc.node} (job {alloc.job_id}){reason}.")
    except Exception as exc:  # noqa: BLE001 - disconnect must not fail over housekeeping
        emit("warning", "worker", f"Could not release worker job {alloc.job_id}: {exc}")
