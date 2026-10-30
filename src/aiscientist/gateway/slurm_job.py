"""Submit and supervise a one-shot Slurm **batch** job on HPC3 with robust startup.

Unlike the long-lived vLLM serve job (``gpu.ensure_serve_job``, which raises if the
GPU queue is slow), an analysis job is a batch job: it runs an Apptainer-contained
step (scanpy / CodeAct), writes its outputs to shared DFS, and exits. The hard part on
a shared cluster is the QUEUE — the job may sit PENDING a long time. This module
implements the lifecycle the product needs:

    submit   ->  wait PENDING -> RUNNING, in ``max_attempts`` windows of ``startup_timeout_s``
             ->  a job that is merely queued KEEPS ITS PLACE across windows (a resubmit
                 would send it to the back of the queue)
             ->  resubmit only a job Slurm holds or one that died on startup
             ->  out of windows: ``scancel`` it and fail (a long queue, not a broken job)
    RUNNING  ->  wait for COMPLETE within ``run_timeout_s`` (else scancel + fail)
             ->  return the terminal Slurm state + the node it ran on

Everything goes through the ``RemoteExecutor`` protocol, so the whole lifecycle runs
offline against a scripted fake in tests — no real Slurm, no SSH. The container
command builder (``container_exec``) bind-mounts the dataset **read-only** so even
contained code can't modify or delete the raw data; only work/artifacts are writable,
and ``--containall`` hides every other host path.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from .errors import GatewayError
from .executor import RemoteExecutor

EmitFn = Callable[[str, str, str], None]

# squeue one-letter states. PD = pending (in queue), R = running.
_PENDING = "PD"
_RUNNING = "R"
# Terminal "this attempt is dead" states — stop waiting and (re)submit/fail.
_DEAD_STATES = {"F", "CA", "TO", "NF", "OOM", "BF", "DL"}

# sacct State prefixes that mean the job has REALLY reached an end state. squeue and sacct
# are eventually-consistent: a job can vanish from squeue (empty output) a few seconds
# before sacct flips off RUNNING/COMPLETING. Treating that lagging non-terminal sacct state
# as final is what made a scGPT job that finished fine (and wrote predictions) get reported
# as "did not complete (state RUNNING)". So we only conclude on a terminal sacct state.
_SACCT_TERMINAL = (
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
    "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED", "REVOKED",
)
# How many consecutive (squeue-empty AND no sacct record) polls before we assume the job
# completed — a fallback for clusters with accounting disabled, where sacct never reports.
_GONE_CONFIRM = 2


def _is_terminal_sacct(state: str) -> bool:
    return any(state.upper().startswith(t) for t in _SACCT_TERMINAL)

# Heredoc sentinel (matches gpu.py so the mock host treats a script write as inert).
_EOF = "AISCIENTIST_EOF"


class SlurmJobError(RuntimeError):
    """A Slurm batch job could not be submitted, started, or completed."""

    def __init__(self, message: str, *, job_id: str | None = None, detail: object = None) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.detail = detail


class SlurmQueueTimeout(SlurmJobError):
    """The job waited out every window in the queue and never started: the cluster queue was too
    long, not a fault in the job. ``reason`` is Slurm's last pending reason (``Priority``,
    ``Resources``, ...). A caller can submit the same job again later; nothing ran."""

    def __init__(self, message: str, *, job_id: str | None = None, reason: str = "",
                 waited_s: float = 0.0) -> None:
        super().__init__(message, job_id=job_id)
        self.reason = reason
        self.waited_s = waited_s


class JobCancelled(SlurmJobError):
    """The user asked to Stop mid-run, so the in-flight job was ``scancel``-led. Distinct from a
    real failure so callers can end the run cleanly instead of falling back to a LOCAL re-run
    (which would defeat the Stop)."""


def _noop_emit(level: str, stage: str, message: str) -> None:  # pragma: no cover - default
    return None


@dataclass
class SlurmJobSpec:
    """A complete ``#!/bin/bash`` sbatch script + a per-user job name.

    ``submit_dir`` is where the script file is written. It has no default on purpose: every job
    line names a directory under the application's own storage (``<shared_root>/sessions/<id>/``),
    so nothing lands in the user's home directory because a caller left it out."""

    script: str
    job_name: str
    submit_dir: str


@dataclass
class AcquireConfig:
    """How patiently to wait for a node. The total wait is ``startup_timeout_s * max_attempts``;
    a queued job waits it out as ONE submission, and only a held or dead job is resubmitted."""

    startup_timeout_s: int = 180     # one waiting window (a progress line is emitted at each)
    max_attempts: int = 3            # windows to wait = the most submissions a broken job gets
    poll_interval_s: float = 3.0


@dataclass
class RunConfig:
    """How long to let a RUNNING job execute before cancelling it as stuck."""

    run_timeout_s: int = 3600
    poll_interval_s: float = 5.0


@dataclass
class JobResult:
    job_id: str
    node: str
    state: str             # terminal Slurm state: COMPLETED | FAILED | TIMEOUT | ...
    attempts: int
    completed: bool

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id, "node": self.node, "state": self.state,
            "attempts": self.attempts, "completed": self.completed,
        }


def slurm_time_to_seconds(hms: str, default: int = 7200) -> int:
    """Parse a Slurm ``--time`` string (``HH:MM:SS``, ``D-HH:MM:SS``, or ``MM:SS``) to seconds; returns
    ``default`` on anything unparseable. Used to size the gateway's job-wait (``run_timeout_s``) off the
    SBATCH ``--time`` so the gateway waits as long as Slurm allows and does NOT scancel a healthy,
    still-progressing job early (a WGS VEP job runs ~30-60 min, well past a 30-min wait). A completed
    job still returns the instant it leaves the queue — this only raises the ceiling for a stuck one."""
    try:
        s = str(hms).strip()
        days = 0
        if "-" in s:
            d, s = s.split("-", 1)
            days = int(d)
        parts = [int(p) for p in s.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, sec = parts[-3], parts[-2], parts[-1]
        return days * 86400 + h * 3600 + m * 60 + sec
    except (ValueError, AttributeError):
        return default


# --- Container command builder ----------------------------------------------


def container_exec(
    image: str,
    command: str,
    *,
    binds_ro: tuple[str, ...] = (),
    binds_rw: tuple[str, ...] = (),
    nv: bool = False,
    network: bool = False,
    container_bin: str = "apptainer",
    cleanenv: bool = False,
) -> str:
    """Build an ``apptainer exec`` line that runs ``command`` fully contained.

    - ``--containall`` hides every host path except the explicit binds (no $HOME, /tmp).
    - ``cleanenv`` also states ``--cleanenv`` explicitly (``--containall`` implies it), for the
      registry skills whose job must carry no host environment.
    - ``--writable-tmpfs`` gives an ephemeral writable overlay, discarded on exit.
    - ``binds_ro`` (the dataset) mount **read-only** — contained code cannot delete or
      modify the raw data; ``binds_rw`` (work/artifacts) are writable for outputs.
    - network is OFF unless ``network=True``; ``nv`` adds GPU passthrough.
    """
    parts = [container_bin, "exec", "--containall", "--writable-tmpfs"]
    if cleanenv:
        parts.append("--cleanenv")
    if not network:
        parts += ["--net", "--network", "none"]
    if nv:
        parts.append("--nv")
    for p in binds_ro:
        parts += ["-B", f"{p}:{p}:ro"]
    for p in binds_rw:
        parts += ["-B", f"{p}:{p}"]
    parts += [image, "bash", "-lc", shlex.quote(command)]
    return " ".join(parts)


# The name before the 2026-10-09 move to Apptainer; the CLI is the same.
singularity_exec = container_exec


def build_analysis_script(
    job_name: str,
    inner_command: str,
    *,
    partition: str,
    cpus: int,
    mem_gb: int,
    time_limit: str,
    account: str = "",
    gres: str = "",
    container_module: str = "",
    log_dir: str = ".",
    exclude: str = "",
    constraint: str = "",
) -> str:
    """A batch sbatch script that runs one contained analysis command and exits.

    ``inner_command`` is typically a ``container_exec(...)`` string. Pass ``gres``
    (e.g. ``"gpu:1"``) to request a GPU — the default empty value yields a CPU-only job.
    The log path is relative to the submit dir (Slurm does not expand ``$HOME`` in
    ``#SBATCH``)."""
    account_line = f"#SBATCH --account={account}\n" if account else ""
    gres_line = f"#SBATCH --gres={gres}\n" if gres else ""
    # Nodes to keep this job off. Only the vLLM serve job used to honour AISCIENTIST_SLURM_EXCLUDE,
    # so the GPU jobs built here — scGPT and the VL report review — could still be scheduled onto
    # hpc3-gpu-n54-01, whose GPU1 is dead but which Slurm keeps offering. A GPU job placed there
    # fails for a reason that has nothing to do with the model, and reads like a model problem.
    exclude_line = f"#SBATCH --exclude={exclude}\n" if exclude else ""
    # Node features the job needs (``--constraint``), e.g. ``nvme`` with ``gpu:1`` for an A30 or an
    # A100 and never a V100 (AISCIENTIST_CORPUS_SCAN_CONSTRAINT).
    constraint_line = f"#SBATCH --constraint={constraint}\n" if constraint else ""
    module_line = f"module load {container_module} 2>/dev/null || true\n" if container_module else ""
    return (
        "#!/bin/bash\n"
        f"#SBATCH --job-name={job_name}\n"
        f"#SBATCH --partition={partition}\n"
        f"{account_line}"
        f"{gres_line}"
        f"{exclude_line}"
        f"{constraint_line}"
        f"#SBATCH --cpus-per-task={cpus}\n"
        f"#SBATCH --mem={mem_gb}G\n"
        f"#SBATCH --time={time_limit}\n"
        f"#SBATCH --output={log_dir}/{job_name}-%j.log\n\n"
        "set -euo pipefail\n"
        "source /etc/profile.d/lmod.sh 2>/dev/null || true\n"
        f"{module_line}"
        'echo "AiScientist analysis job starting on $(hostname) at $(date)"\n'
        f"{inner_command}\n"
    )


# --- Slurm lifecycle ---------------------------------------------------------


def _submit(executor: RemoteExecutor, spec: SlurmJobSpec) -> str:
    path = f"{spec.submit_dir}/{spec.job_name}.sbatch"
    write = executor.exec(
        f"mkdir -p {spec.submit_dir} && cat > {path} <<'{_EOF}'\n{spec.script}\n{_EOF}"
    )
    if not write.ok:
        raise SlurmJobError("failed to write the sbatch script on the cluster", detail=write.stderr)
    submit = executor.exec(f"sbatch {path}")
    if not submit.ok or "Submitted batch job" not in submit.stdout:
        raise SlurmJobError(
            "sbatch rejected the job — check account/partition/resources",
            detail=(submit.stderr or submit.stdout),
        )
    return submit.stdout.strip().split()[-1]


def _query(executor: RemoteExecutor, command: str):
    """Run a READ-ONLY Slurm query, sending it once more if the SSH session failed under it.

    A poll is the command most likely to be in flight when the session drops, and one failed
    ``squeue`` used to end the whole step although the job itself was fine (run 78a707cd79e9). A
    query is safe to repeat, and the repeat is what reconnects the session (``SSHExecutor``).
    Never use this for ``sbatch``/``scancel``."""
    try:
        return executor.exec(command)
    except GatewayError:
        return executor.exec(command)


def _queue_state(executor: RemoteExecutor, job_id: str) -> tuple[str, str]:
    """``(state, node)`` from squeue, or ``("", "")`` if the job already left the queue."""
    r = _query(executor, f"squeue -j {job_id} --noheader --format='%t|%N'")
    row = r.out.splitlines()[0] if r.out else ""
    if not row:
        return "", ""
    fields = row.split("|")
    return fields[0].strip(), (fields[1].strip() if len(fields) > 1 else "")


def _pending_info(executor: RemoteExecutor, job_id: str) -> tuple[str, "float | None", str]:
    """Why a queued job has not started and when Slurm expects it to: ``(reason, seconds until the
    expected start, that start as text)``. The reason is squeue's (``Priority``, ``Resources``,
    ``JobHeldAdmin`` ...); the start is the backfill scheduler's estimate, ``None``/``""`` when it
    has none. ``date`` runs in the same command, so the difference is taken on the cluster's clock."""
    r = _query(executor, f"squeue -j {job_id} --noheader --format='%r|%S'; date +%Y-%m-%dT%H:%M:%S")
    lines = [ln.strip() for ln in (r.out or "").splitlines() if ln.strip()]
    if not lines:
        return "", None, ""
    reason, _, start = lines[0].partition("|")
    reason, start = reason.strip(), start.strip()
    try:
        fmt = "%Y-%m-%dT%H:%M:%S"
        wait_s = (datetime.strptime(start, fmt) - datetime.strptime(lines[-1], fmt)).total_seconds()
    except ValueError:                      # "N/A", "Unknown", or a host with no usable clock line
        return reason, None, ""
    return reason, wait_s, start


def _is_held(reason: str) -> bool:
    """A hold is on the JOB (``JobHeldAdmin``, ``JobHeldUser``, ``launch failed requeued held``,
    ``job requeued in held state``): it will not start however long it waits, and a new submission
    does not carry the hold. Every other pending reason is the queue, where waiting is what works."""
    return "held" in reason.lower()


def _terminal_state(executor: RemoteExecutor, job_id: str) -> str:
    """The job's sacct State (COMPLETED|FAILED|RUNNING|...), or ``""`` if sacct has no
    record yet. Unlike before, this does NOT optimistically default to ``COMPLETED`` —
    the caller distinguishes a real terminal state from a lagging/absent one."""
    r = _query(executor, f"sacct -j {job_id} --noheader --format=State --parsable2")
    line = r.out.splitlines()[0] if r.out else ""
    return line.strip()


def _check_cancel(executor: RemoteExecutor, job_id: str, should_cancel: "Callable[[], bool] | None",
                  emit: EmitFn) -> None:
    """Stop button honoured mid-Slurm: if ``should_cancel`` fires, ``scancel`` the job and raise
    :class:`JobCancelled` so the run ends promptly instead of blocking until the job finishes."""
    if should_cancel is not None and should_cancel():
        executor.exec(f"scancel {job_id}")
        emit("warning", "slurm", f"Job {job_id} cancelled by the user (scancelled).")
        raise JobCancelled("run cancelled by the user", job_id=job_id)


def acquire_allocation(
    executor: RemoteExecutor,
    spec: SlurmJobSpec,
    *,
    config: AcquireConfig | None = None,
    emit: EmitFn | None = None,
    on_submit: Callable[[str], None] | None = None,
    should_cancel: "Callable[[], bool] | None" = None,
) -> tuple[str, str, int]:
    """Submit and wait for RUNNING, for up to ``max_attempts`` windows of ``startup_timeout_s``.
    Returns ``(job_id, node, submissions)``; raises ``SlurmJobError`` if the job never starts.

    A job that is only QUEUED keeps its place from one window to the next. It used to be
    ``scancel``-led and resubmitted at every window, and a new job joins the back of the queue with
    no age: on a busy partition (2026-10-09, `standard`, ~40 min wait) three 10-minute attempts each
    started from the back, so a job Slurm would have started was cancelled three times over. Only a
    job Slurm HOLDS, or one that died on startup, is replaced by a new submission. When the windows
    run out and Slurm expects the job to start within one more, it gets that one more window rather
    than being cancelled minutes before its turn.

    ``on_submit`` (if given) is called with the ``job_id`` the instant ``sbatch`` accepts
    the job — before we wait for a node — so a caller can durably record it (see
    :class:`aiscientist.gateway.job_store.JobStore`). It fires again after a resubmit, so the
    persisted id always points at the attempt currently in the queue."""
    config = config or AcquireConfig()
    emit = emit or _noop_emit
    windows_left = config.max_attempts
    submissions = 0
    waited_s = 0.0
    extended = False
    job_id = ""                     # the job currently in the queue; "" = submit a new one
    reason = ""
    while windows_left > 0:
        if not job_id:
            submissions += 1
            job_id = _submit(executor, spec)
            if on_submit is not None:
                on_submit(job_id)
            emit("step", "slurm",
                 f"Submitted {spec.job_name} as job {job_id} (submission {submissions}); "
                 "waiting for a node ...")
        windows_left -= 1
        deadline = time.monotonic() + config.startup_timeout_s
        died = False
        announced = False               # one "pending" line per window, not one per poll
        while time.monotonic() < deadline:
            _check_cancel(executor, job_id, should_cancel, emit)   # Stop honoured while queued
            state, node = _queue_state(executor, job_id)
            if state == _RUNNING and node:
                emit("success", "slurm", f"Job {job_id} is RUNNING on {node}.")
                return job_id, node, submissions
            if state == "":
                # Already left the queue during startup — a fast batch job may have run
                # and exited; let the caller's completion check sort out the exit state.
                emit("info", "slurm", f"Job {job_id} already left the queue.")
                return job_id, node, submissions
            if state in _DEAD_STATES:
                emit("warning", "slurm", f"Job {job_id} died on startup (state {state}); re-requesting ...")
                died = True
                break
            if state == _PENDING and not announced:
                announced = True
                emit("info", "slurm", f"Job {job_id} pending in the queue ...")
            time.sleep(config.poll_interval_s)
        if died:
            job_id = ""
            continue
        waited_s += config.startup_timeout_s
        reason, eta_s, eta = _pending_info(executor, job_id)
        why = f" ({reason})" if reason else ""
        if _is_held(reason):
            emit("warning", "slurm",
                 f"Job {job_id} is held by Slurm{why} and will not start as it is; "
                 "cancelling and re-requesting.")
            executor.exec(f"scancel {job_id}")
            job_id = ""
            continue
        expects = f"; Slurm expects it to start at {eta}" if eta else ""
        if windows_left > 0:
            emit("info", "slurm",
                 f"Job {job_id} is still queued after {waited_s:.0f}s{why}{expects}. "
                 "Keeping its place in the queue.")
        elif not extended and eta_s is not None and eta_s <= config.startup_timeout_s:
            extended = True
            windows_left = 1
            emit("info", "slurm",
                 f"Job {job_id} is still queued after {waited_s:.0f}s{why}{expects}: "
                 "waiting for that rather than giving up its place.")
    if job_id:
        executor.exec(f"scancel {job_id}")
    total_s = config.startup_timeout_s * (config.max_attempts + (1 if extended else 0))
    why = f"; Slurm's reason: {reason}" if reason else ""
    raise SlurmQueueTimeout(
        f"{spec.job_name} did not get a node within {total_s:.0f}s over {submissions} submission(s) "
        f"(the cluster queue is too long right now, not a fault in the job{why}).",
        job_id=job_id or None, reason=reason, waited_s=float(total_s),
    )


def parse_accounting(text: str, job_id: str) -> dict | None:
    """Read only the terminal parent allocation; .batch/.extern would double-count hours."""
    import math
    import re

    for line in text.splitlines():
        fields = line.strip().split("|")
        if len(fields) < 5 or fields[0] != job_id or not _is_terminal_sacct(fields[4]):
            continue
        try:
            seconds, cpus = float(fields[1]), int(fields[2])
            if not math.isfinite(seconds) or seconds < 0 or cpus < 0:
                continue
            # AllocTRES can include both gres/gpu and typed GPU counts. Use the aggregate.
            tres = dict(part.split("=", 1) for part in fields[3].split(",") if "=" in part)
            gpus = int(tres.get("gres/gpu", 0))
            if "gres/gpu" not in tres:
                gpus = sum(int(v) for k, v in tres.items() if re.match(r"gres/gpu:", k))
            return {"seconds": seconds, "cpu_hours": seconds * cpus / 3600,
                    "gpu_hours": seconds * gpus / 3600}
        except (ValueError, TypeError):
            continue
    return None


def collect_accounting(executor: RemoteExecutor, job_id: str, elapsed: float | None = None) -> None:
    from .estimates import job_usage

    collector = job_usage.get()
    if collector is None:
        return
    try:
        result = _query(executor, f"sacct -j {job_id} --noheader --parsable2 "
                        "--format=JobIDRaw,ElapsedRaw,AllocCPUS,AllocTRES,State")
        usage = parse_accounting(result.stdout, job_id)
        if usage:
            collector.append(usage)
            return
    except Exception:  # accounting may be disabled
        pass
    if elapsed is not None:
        # This timer starts after allocation, unlike the outer tool timer (which includes queuing).
        collector.append({"seconds": elapsed, "source": "wall_clock"})


def supervise_job(executor: RemoteExecutor, job_id: str, node: str, attempts: int, **kwargs) -> JobResult:
    started = time.monotonic()
    try:
        return _supervise_job(executor, job_id, node, attempts, **kwargs)
    finally:
        collect_accounting(executor, job_id, max(0, time.monotonic() - started))


def _supervise_job(
    executor: RemoteExecutor,
    job_id: str,
    node: str,
    attempts: int,
    *,
    run: RunConfig | None = None,
    emit: EmitFn | None = None,
    should_cancel: "Callable[[], bool] | None" = None,
) -> JobResult:
    """Poll an ALREADY-RUNNING (or already-submitted) job to a terminal state.

    Factored out of :func:`run_batch_job` so a reconnecting session can drive the exact
    same completion logic on a job it did NOT submit in this process (see
    :func:`reattach_job`). A job that runs past ``run_timeout_s`` is ``scancel``-led and
    reported as failed."""
    emit = emit or _noop_emit
    run = run or RunConfig()
    deadline = time.monotonic() + run.run_timeout_s
    gone_polls = 0  # consecutive "squeue-empty AND no sacct record yet" polls
    while time.monotonic() < deadline:
        _check_cancel(executor, job_id, should_cancel, emit)   # Stop honoured while RUNNING
        state, live_node = _queue_state(executor, job_id)
        node = live_node or node
        if state == "":                              # left the queue (or a transient blip)
            final = _terminal_state(executor, job_id)
            if _is_terminal_sacct(final):            # genuinely finished
                ok = final.startswith("COMPLETED")
                emit("success" if ok else "error", "slurm", f"Job {job_id} finished: {final}.")
                return JobResult(job_id, node, final, attempts, completed=ok)
            # squeue is empty but sacct is NOT terminal yet: either sacct is lagging behind a
            # job that is actually still RUNNING/COMPLETING, or a transient empty squeue, or
            # accounting is off. Do NOT report failure — keep polling. Only after several
            # polls with no sacct record at all do we assume the job completed.
            if not final:
                gone_polls += 1
                if gone_polls >= _GONE_CONFIRM:
                    emit("success", "slurm", f"Job {job_id} left the queue (no sacct record); assuming COMPLETED.")
                    return JobResult(job_id, node, "COMPLETED", attempts, completed=True)
        else:
            gone_polls = 0
            if state in _DEAD_STATES:
                final = _terminal_state(executor, job_id) or state
                emit("error", "slurm", f"Job {job_id} failed: {final}.")
                return JobResult(job_id, node, final, attempts, completed=False)
        time.sleep(run.poll_interval_s)
    executor.exec(f"scancel {job_id}")
    raise SlurmJobError(
        f"job {job_id} exceeded the {run.run_timeout_s}s run budget and was cancelled.",
        job_id=job_id,
    )


def run_batch_job(
    executor: RemoteExecutor,
    spec: SlurmJobSpec,
    *,
    acquire: AcquireConfig | None = None,
    run: RunConfig | None = None,
    emit: EmitFn | None = None,
    on_submit: Callable[[str], None] | None = None,
    should_cancel: "Callable[[], bool] | None" = None,
) -> JobResult:
    """Acquire a node (with startup retry), then wait for the job to complete. A job
    that runs past ``run_timeout_s`` is ``scancel``-led and reported as failed. If
    ``should_cancel`` fires (the Stop button), the in-flight job is ``scancel``-led and a
    :class:`JobCancelled` is raised so the run ends promptly.

    ``on_submit`` is forwarded to :func:`acquire_allocation` so the ``job_id`` can be
    durably recorded the instant it is submitted — before it is supervised — closing the
    window where a gateway crash would orphan a running job with nothing tracking its id."""
    emit = emit or _noop_emit
    submitted = []

    def submitted_job(job_id: str) -> None:
        submitted.append(job_id)
        if on_submit:
            on_submit(job_id)

    try:
        job_id, node, attempts = acquire_allocation(
            executor, spec, config=acquire, emit=emit, on_submit=submitted_job, should_cancel=should_cancel
        )
    except Exception:
        for previous in submitted:
            collect_accounting(executor, previous, 0)
        if not submitted:
            from .estimates import job_usage

            collector = job_usage.get()
            if collector is not None:
                collector.append({"seconds": 0, "source": "wall_clock"})
        raise
    try:
        return supervise_job(executor, job_id, node, attempts, run=run, emit=emit, should_cancel=should_cancel)
    finally:
        for previous in submitted:
            if previous != job_id:
                collect_accounting(executor, previous, 0)


def reattach_job(
    executor: RemoteExecutor,
    job_id: str,
    *,
    node: str = "",
    attempts: int = 1,
    run: RunConfig | None = None,
    emit: EmitFn | None = None,
    wait: bool = True,
) -> JobResult:
    """Reconnect to a job submitted in an EARLIER process (persisted in a
    :class:`~aiscientist.gateway.job_store.JobStore`) and learn its outcome — this is the
    "resume where you left off" the tmux tip gestures at, done for a batch job rather than
    an interactive shell.

    It never resubmits: it observes the existing job via ``squeue``/``sacct`` only. If the
    job has already reached a terminal state, that :class:`JobResult` is returned at once.
    With ``wait=True`` (default) a still-running job is supervised to completion; with
    ``wait=False`` the current state is reported without blocking — the right choice at
    session reconnect, where we want a fast status sweep, not to hang on a long job."""
    emit = emit or _noop_emit
    state, live_node = _queue_state(executor, job_id)
    node = live_node or node
    if state == "":
        # Not in the queue: either finished (sacct knows) or a transient empty poll.
        final = _terminal_state(executor, job_id)
        if _is_terminal_sacct(final):
            ok = final.startswith("COMPLETED")
            emit("info", "slurm", f"Reattached job {job_id}: already finished ({final}).")
            return JobResult(job_id, node, final, attempts, completed=ok)
        if not wait:
            emit("info", "slurm", f"Reattached job {job_id}: left queue, awaiting sacct ({final or 'no record'}).")
            return JobResult(job_id, node, final or "UNKNOWN", attempts, completed=False)
    elif not wait:
        emit("info", "slurm", f"Reattached job {job_id}: still {state} on {node or '?'}.")
        return JobResult(job_id, node, state, attempts, completed=False)
    emit("step", "slurm", f"Reattaching to job {job_id} and supervising to completion ...")
    return supervise_job(executor, job_id, node, attempts, run=run, emit=emit)


class _JobRecordLike(Protocol):
    job_id: str
    node: str


class _StoreLike(Protocol):
    def incomplete(self, *, owner: str | None = ..., kind: str | None = ...) -> list[_JobRecordLike]: ...
    def mark(self, job_id: str, *, state: str | None = ..., node: str | None = ...,
             completed: bool | None = ...) -> object: ...


def resume_incomplete(
    executor: RemoteExecutor,
    store: _StoreLike,
    *,
    owner: str | None = None,
    kind: str | None = None,
    emit: EmitFn | None = None,
) -> list[JobResult]:
    """On reconnect, sweep the persisted job registry and refresh each still-incomplete
    job's state from the live cluster — non-blocking (``wait=False``), so a session that
    reconnects while a long job is mid-run learns "still RUNNING" instantly instead of
    hanging. Any job that has since reached a terminal state is marked done in ``store``.

    Duck-typed on ``store`` (anything with ``incomplete()`` + ``mark()``) so this module
    keeps no hard dependency on the persistence layer. Ownership/kind filters let a session
    reattach only the jobs it is entitled to. Returns the probed :class:`JobResult` list."""
    emit = emit or _noop_emit
    results: list[JobResult] = []
    pending = store.incomplete(owner=owner, kind=kind)
    if pending:
        emit("info", "slurm", f"Reattaching to {len(pending)} in-flight Slurm job(s) after reconnect ...")
    for rec in pending:
        try:
            res = reattach_job(executor, rec.job_id, node=rec.node, emit=emit, wait=False)
        except SlurmJobError as exc:
            emit("warning", "slurm", f"Could not reattach job {rec.job_id}: {exc}")
            continue
        results.append(res)
        store.mark(res.job_id, state=res.state, node=res.node, completed=res.completed)
    return results
