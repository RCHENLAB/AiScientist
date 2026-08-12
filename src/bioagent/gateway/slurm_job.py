"""Submit and supervise a one-shot Slurm **batch** job on HPC3 with robust startup.

Unlike the long-lived vLLM serve job (``gpu.ensure_serve_job``, which raises if the
GPU queue is slow), an analysis job is a batch job: it runs a Singularity-contained
step (scanpy / CodeAct), writes its outputs to shared DFS, and exits. The hard part on
a shared cluster is the QUEUE — the job may sit PENDING a long time. This module
implements the lifecycle the product needs:

    submit   ->  wait PENDING -> RUNNING within ``startup_timeout_s``
             ->  if it never starts in time: ``scancel`` it and RESUBMIT
             ->  (bounded by ``max_attempts`` — a long queue is not a hard failure)
    RUNNING  ->  wait for COMPLETE within ``run_timeout_s`` (else scancel + fail)
             ->  return the terminal Slurm state + the node it ran on

Everything goes through the ``RemoteExecutor`` protocol, so the whole lifecycle runs
offline against a scripted fake in tests — no real Slurm, no SSH. The Singularity
command builder (``singularity_exec``) bind-mounts the dataset **read-only** so even
contained code can't modify or delete the raw data; only work/artifacts are writable,
and ``--containall`` hides every other host path.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from typing import Callable, Protocol

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
_EOF = "BIOAGENT_EOF"


class SlurmJobError(RuntimeError):
    """A Slurm batch job could not be submitted, started, or completed."""

    def __init__(self, message: str, *, job_id: str | None = None, detail: object = None) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.detail = detail


class JobCancelled(SlurmJobError):
    """The user asked to Stop mid-run, so the in-flight job was ``scancel``-led. Distinct from a
    real failure so callers can end the run cleanly instead of falling back to a LOCAL re-run
    (which would defeat the Stop)."""


def _noop_emit(level: str, stage: str, message: str) -> None:  # pragma: no cover - default
    return None


@dataclass
class SlurmJobSpec:
    """A complete ``#!/bin/bash`` sbatch script + a per-user job name."""

    script: str
    job_name: str
    submit_dir: str = "$HOME/.bioagent/jobs"


@dataclass
class AcquireConfig:
    """How patiently to wait for a node before cancelling and re-requesting."""

    startup_timeout_s: int = 180     # PENDING->RUNNING budget per attempt
    max_attempts: int = 3            # how many times to (re)submit if it won't start
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


# --- Singularity command builder --------------------------------------------


def singularity_exec(
    image: str,
    command: str,
    *,
    binds_ro: tuple[str, ...] = (),
    binds_rw: tuple[str, ...] = (),
    nv: bool = False,
    network: bool = False,
    container_bin: str = "singularity",
) -> str:
    """Build a ``singularity exec`` line that runs ``command`` fully contained.

    - ``--containall`` hides every host path except the explicit binds (no $HOME, /tmp).
    - ``--writable-tmpfs`` gives an ephemeral writable overlay, discarded on exit.
    - ``binds_ro`` (the dataset) mount **read-only** — contained code cannot delete or
      modify the raw data; ``binds_rw`` (work/artifacts) are writable for outputs.
    - network is OFF unless ``network=True``; ``nv`` adds GPU passthrough.
    """
    parts = [container_bin, "exec", "--containall", "--writable-tmpfs"]
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
) -> str:
    """A batch sbatch script that runs one contained analysis command and exits.

    ``inner_command`` is typically a ``singularity_exec(...)`` string. Pass ``gres``
    (e.g. ``"gpu:1"``) to request a GPU — the default empty value yields a CPU-only job.
    The log path is relative to the submit dir (Slurm does not expand ``$HOME`` in
    ``#SBATCH``)."""
    account_line = f"#SBATCH --account={account}\n" if account else ""
    gres_line = f"#SBATCH --gres={gres}\n" if gres else ""
    module_line = f"module load {container_module} 2>/dev/null || true\n" if container_module else ""
    return (
        "#!/bin/bash\n"
        f"#SBATCH --job-name={job_name}\n"
        f"#SBATCH --partition={partition}\n"
        f"{account_line}"
        f"{gres_line}"
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


def _queue_state(executor: RemoteExecutor, job_id: str) -> tuple[str, str]:
    """``(state, node)`` from squeue, or ``("", "")`` if the job already left the queue."""
    r = executor.exec(f"squeue -j {job_id} --noheader --format='%t|%N'")
    row = r.out.splitlines()[0] if r.out else ""
    if not row:
        return "", ""
    fields = row.split("|")
    return fields[0].strip(), (fields[1].strip() if len(fields) > 1 else "")


def _terminal_state(executor: RemoteExecutor, job_id: str) -> str:
    """The job's sacct State (COMPLETED|FAILED|RUNNING|...), or ``""`` if sacct has no
    record yet. Unlike before, this does NOT optimistically default to ``COMPLETED`` —
    the caller distinguishes a real terminal state from a lagging/absent one."""
    r = executor.exec(f"sacct -j {job_id} --noheader --format=State --parsable2")
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
    """Submit and wait for RUNNING. If startup exceeds ``startup_timeout_s`` (a long
    queue), ``scancel`` the job and resubmit, up to ``max_attempts``. Returns
    ``(job_id, node, attempt)``; raises ``SlurmJobError`` if no attempt ever starts.

    ``on_submit`` (if given) is called with the ``job_id`` the instant ``sbatch`` accepts
    the job — before we wait for a node — so a caller can durably record it (see
    :class:`bioagent.gateway.job_store.JobStore`). It fires again after a resubmit, so the
    persisted id always points at the attempt currently in the queue."""
    config = config or AcquireConfig()
    emit = emit or _noop_emit
    for attempt in range(1, config.max_attempts + 1):
        job_id = _submit(executor, spec)
        if on_submit is not None:
            on_submit(job_id)
        emit("step", "slurm",
             f"Submitted {spec.job_name} as job {job_id} (attempt {attempt}/{config.max_attempts}); "
             "waiting for a node ...")
        deadline = time.monotonic() + config.startup_timeout_s
        timed_out = True
        while time.monotonic() < deadline:
            _check_cancel(executor, job_id, should_cancel, emit)   # Stop honoured while queued
            state, node = _queue_state(executor, job_id)
            if state == _RUNNING and node:
                emit("success", "slurm", f"Job {job_id} is RUNNING on {node}.")
                return job_id, node, attempt
            if state == "":
                # Already left the queue during startup — a fast batch job may have run
                # and exited; let the caller's completion check sort out the exit state.
                emit("info", "slurm", f"Job {job_id} already left the queue.")
                return job_id, node, attempt
            if state in _DEAD_STATES:
                emit("warning", "slurm", f"Job {job_id} died on startup (state {state}); re-requesting ...")
                timed_out = False
                break
            if state == _PENDING:
                emit("info", "slurm", f"Job {job_id} pending in the queue ...")
            time.sleep(config.poll_interval_s)
        if timed_out:
            # The user-specified behaviour: startup took too long -> kill it and re-request.
            emit("warning", "slurm",
                 f"Job {job_id} did not start within {config.startup_timeout_s}s (queue too long); "
                 "cancelling and re-requesting.")
            executor.exec(f"scancel {job_id}")
    raise SlurmJobError(
        f"{spec.job_name} could not get a node after {config.max_attempts} attempts "
        "(the queue stayed too long).",
    )


def supervise_job(
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
    job_id, node, attempts = acquire_allocation(
        executor, spec, config=acquire, emit=emit, on_submit=on_submit, should_cancel=should_cancel
    )
    return supervise_job(executor, job_id, node, attempts, run=run, emit=emit, should_cancel=should_cancel)


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
    :class:`~bioagent.gateway.job_store.JobStore`) and learn its outcome — this is the
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
