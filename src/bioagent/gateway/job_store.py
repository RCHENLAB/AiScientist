"""Durable registry of submitted Slurm jobs so gateway restarts don't orphan them.

The Slurm lifecycle in :mod:`bioagent.gateway.slurm_job` submits a batch job and then
polls ``squeue``/``sacct`` **in memory**. Slurm owns the job the moment ``sbatch`` returns,
so the compute keeps running even if the gateway process dies — but the in-memory poll
loop that supervises it is lost, and nothing knows the ``job_id`` to reattach to. That is
the fragility this module closes: every submission is written to a small atomic JSON
registry the instant the ``job_id`` is known, and a reconnecting session can look up its
still-incomplete jobs and reattach (see :func:`bioagent.gateway.slurm_job.reattach_job`).

This is deliberately a flat JSON file, not Postgres. It mirrors the atomic write-temp +
``os.replace`` pattern already used by :mod:`bioagent.lab.archive`, needs no service, and
is a clean seam to swap for the Postgres checkpointer when the LangGraph port lands. It is
NOT a scheduler and does not talk to Slurm — it only remembers what was submitted.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Terminal Slurm states — a record in any of these is finished and needs no reattach.
_TERMINAL = {
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
    "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED", "REVOKED",
}


def _is_terminal(state: str) -> bool:
    return bool(state) and any(state.upper().startswith(t) for t in _TERMINAL)


@dataclass
class JobRecord:
    """One submitted Slurm job we may need to reattach to after a restart.

    ``kind`` labels the workload (``runcode`` / ``scgpt`` / ``vlreview``) so a reconnecting
    session can reattach only the jobs it owns. ``owner`` is the cluster username that
    submitted it — reattach is ownership-guarded exactly like the GPU-alloc scancel path.
    """

    job_id: str
    job_name: str
    kind: str = "runcode"
    owner: str = ""
    submit_dir: str = ""
    node: str = ""
    state: str = "SUBMITTED"
    completed: bool = False
    submitted_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def terminal(self) -> bool:
        return self.completed or _is_terminal(self.state)


class JobStore:
    """Atomic JSON-backed map of ``job_id -> JobRecord`` at ``path``.

    Reads tolerate a missing or corrupt file (returns empty) so a bad write never wedges
    the gateway. Writes are whole-file atomic (temp + ``os.replace``) so a crash mid-write
    leaves the previous good registry intact — never a half-written one.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()

    # -- io -------------------------------------------------------------------

    def _load(self) -> dict[str, JobRecord]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {}
        out: dict[str, JobRecord] = {}
        for jid, obj in (raw.get("jobs") or {}).items():
            if not isinstance(obj, dict):
                continue
            fields = {k: obj[k] for k in JobRecord.__dataclass_fields__ if k in obj}
            try:
                out[str(jid)] = JobRecord(**fields)
            except TypeError:
                continue  # skip a record whose shape drifted; never crash on read
        return out

    def _dump(self, jobs: dict[str, JobRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "jobs": {jid: asdict(r) for jid, r in jobs.items()}}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic on POSIX

    # -- api ------------------------------------------------------------------

    def record(self, rec: JobRecord) -> JobRecord:
        """Insert or replace a job record (called the instant a ``job_id`` is known)."""
        jobs = self._load()
        jobs[rec.job_id] = rec
        self._dump(jobs)
        return rec

    def mark(self, job_id: str, *, state: str | None = None, node: str | None = None,
             completed: bool | None = None) -> JobRecord | None:
        """Update an existing record's state/node/completed and bump ``updated_at``.

        No-op returning ``None`` if the job_id is unknown (already pruned)."""
        jobs = self._load()
        rec = jobs.get(job_id)
        if rec is None:
            return None
        if state is not None:
            rec.state = state
        if node is not None:
            rec.node = node
        if completed is not None:
            rec.completed = completed
        rec.updated_at = time.time()
        self._dump(jobs)
        return rec

    def get(self, job_id: str) -> JobRecord | None:
        return self._load().get(job_id)

    def all(self) -> list[JobRecord]:
        return list(self._load().values())

    def incomplete(self, *, owner: str | None = None, kind: str | None = None) -> list[JobRecord]:
        """Non-terminal records, optionally filtered to one owner and/or kind — the jobs a
        reconnecting session should reattach to."""
        out = [r for r in self._load().values() if not r.terminal]
        if owner is not None:
            out = [r for r in out if r.owner == owner]
        if kind is not None:
            out = [r for r in out if r.kind == kind]
        return out

    def remove(self, job_id: str) -> bool:
        jobs = self._load()
        if job_id not in jobs:
            return False
        del jobs[job_id]
        self._dump(jobs)
        return True

    def prune_terminal(self) -> int:
        """Drop every finished record; returns how many were removed (housekeeping)."""
        jobs = self._load()
        dead = [jid for jid, r in jobs.items() if r.terminal]
        for jid in dead:
            del jobs[jid]
        if dead:
            self._dump(jobs)
        return len(dead)
