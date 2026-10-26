"""The Storage tab's Delete (My account → Storage), as a Slurm job on HPC3.

The Storage tab lists the user's own HPC3 folders (``app._storage_areas``: ``Temp/<user>``,
``uploads/<user>`` and the personal lab folder) and deletes an entry on the user's click. The
deletion is filesystem work, which RCIC keeps off the login nodes, so it is never an ``rm`` over
the SSH session: the gateway stages ``deploy/hpc3/aiscientist_storage_delete.sh`` and submits it as
a one-CPU job on the CPU partition (``cpu_partition``) through the compute backend, then answers
at once with the job id. The tab asks again while the job is queued or running; one ``sacct``
gives its state, and the job's log says what it deleted.

The guard is applied twice: here (:func:`allowed`, before anything reaches HPC3) and by the script,
which refuses to run outside Slurm, skips any path outside the user's own folders or with a
symbolic link above it, and removes a link itself, never what it points to.

No SSH here: the gateway supplies the executor and the backend.
"""

from __future__ import annotations

import shlex
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .hpc_originals import is_terminal
from .slurm_job import SlurmJobSpec

SCRIPT_SRC = Path(__file__).resolve().parents[3] / "deploy" / "hpc3" / "aiscientist_storage_delete.sh"
SCRIPT_NAME = "storage_delete.sh"
JOB_PREFIX = "aiscientist-delete-"
KEEP = 20                       # deletions a session remembers for the panel
LOG_MAX_BYTES = 64 * 1024

# ``slurm_job._submit`` writes ``<submit dir>/<job name>.sbatch`` and then submits it: two clicks
# at once must not submit the second script twice.
_SUBMIT_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def allowed(path: str, areas: Iterable[str]) -> bool:
    """True when ``path`` is a clean absolute path strictly inside one of ``areas`` (never an area
    itself): no ``..``, ``.`` or empty component, no control character."""
    if not path.startswith("/") or any(c in path for c in "\t\n\r\x00"):
        return False
    parts = path.split("/")[1:]
    if not parts or any(p in ("", ".", "..") for p in parts):
        return False
    for area in areas:
        base = (area or "").rstrip("/")
        if base and path.startswith(base + "/") and len(path) > len(base) + 1:
            return True
    return False


def remote_paths(shared_root: str, user: str, delete_id: str) -> dict:
    """Where a deletion's files go on HPC3, under ``<shared root>/bin/<user>/``."""
    bin_dir = f"{shared_root.rstrip('/')}/bin/{user}"
    return {"bin": bin_dir, "script": f"{bin_dir}/{SCRIPT_NAME}", "dir": f"{bin_dir}/deletes",
            "log": f"{bin_dir}/deletes/{delete_id}.log"}


def command(script: str, shared_root: str, user: str, paths: Iterable[str], *,
            personal_dir: str = "") -> str:
    """The script invocation the job runs."""
    parts = [f"bash {shlex.quote(script)}", f"--root {shlex.quote(shared_root.rstrip('/'))}",
             f"--user {shlex.quote(user)}"]
    if personal_dir:
        parts.append(f"--personal {shlex.quote(personal_dir.rstrip('/'))}")
    parts += [f"--path {shlex.quote(p)}" for p in paths]
    return " ".join(parts)


def job_spec(payload: str, user: str, log_path: str, submit_dir: str, *,
             partition: str, account: str = "") -> SlurmJobSpec:
    """A one-CPU job on the CPU partition (``standard``) that runs ``payload``."""
    lines = ["#!/bin/bash", f"#SBATCH --job-name={JOB_PREFIX}{user}", f"#SBATCH --partition={partition}"]
    if account:
        lines.append(f"#SBATCH --account={account}")
    lines += ["#SBATCH --cpus-per-task=1", "#SBATCH --mem=2G", "#SBATCH --time=02:00:00",
              f"#SBATCH --output={log_path}", "#SBATCH --open-mode=truncate", payload]
    return SlurmJobSpec(script="\n".join(lines), job_name=f"{JOB_PREFIX}{user}", submit_dir=submit_dir)


def stage(executor: Any, shared_root: str, user: str) -> str:
    """Put the script on HPC3 (through the transfer host) and make its log folder; returns the
    script's remote path."""
    paths = remote_paths(shared_root, user, "x")
    executor.exec(f"mkdir -p {shlex.quote(paths['dir'])}", timeout=30)
    executor.put_file(str(SCRIPT_SRC), paths["script"])
    return paths["script"]


def submit(backend: Any, path: str, *, shared_root: str, user: str, script: str,
           partition: str, account: str = "", personal_dir: str = "") -> dict:
    """Submit the deletion of ``path`` (the script already staged at ``script``); returns its
    record. Does not wait for the job."""
    delete_id = uuid.uuid4().hex[:12]
    paths = remote_paths(shared_root, user, delete_id)
    payload = command(script, shared_root, user, [path], personal_dir=personal_dir)
    spec = job_spec(payload, user, paths["log"], paths["bin"], partition=partition, account=account)
    with _SUBMIT_LOCK:
        job_id = backend.submit(spec)
    return {"id": delete_id, "path": path, "job_id": str(job_id), "status": "queued",
            "log": paths["log"], "submitted_at": _now()}


def parse_log(text: str) -> dict:
    """``{"results": [{"verdict": "deleted" | "skip", "path", "reason"?}], "summary": {...} | None}``
    from a job's log; ``summary`` is the ``DELETE_RESULT`` line (None when the job did not reach it)."""
    results: list[dict] = []
    summary = None
    for line in (text or "").splitlines():
        if line.startswith("DELETE_RESULT "):
            summary = {}
            for field in line.split()[1:]:
                key, _, value = field.partition("=")
                summary[key] = int(value) if value.isdigit() else value
            continue
        verdict, _, rest = line.partition("\t")
        if verdict == "DELETED" and rest:
            results.append({"verdict": "deleted", "path": rest})
        elif verdict == "SKIP":
            reason, _, path = rest.partition("\t")
            if path:
                results.append({"verdict": "skip", "reason": reason, "path": path})
    return {"results": results, "summary": summary}


def settle(job: dict, state: str, log_text: str) -> bool:
    """Bring a queued deletion up to date from its Slurm ``state`` and its log. True when it ended
    now. A job still queued or running, or one Slurm has no record of yet, is left."""
    if job.get("status") != "queued":
        return False
    head = (state or "").split()[0].upper() if state else ""
    if not is_terminal(state):
        if head:
            job["slurm_state"] = head
        return False
    parsed = parse_log(log_text)
    mine = next((r for r in parsed["results"] if r["path"] == job["path"]), None)
    job["slurm_state"] = head
    job["ended_at"] = _now()
    summary = parsed["summary"]
    if not head.startswith("COMPLETED") or summary is None or str(summary.get("job", "")) not in ("", job["job_id"]):
        job["status"] = "failed"
        job["error"] = (f"the job ended {head}" if not head.startswith("COMPLETED")
                        else "the job finished without its result line")
    elif mine and mine["verdict"] == "deleted":
        job["status"] = "done"
    elif mine and mine.get("reason") == "not there":
        job["status"] = "done"
        job["note"] = "it was not there any more"
    else:
        job["status"] = "failed"
        job["error"] = (mine or {}).get("reason") or "the job did not say it deleted it"
    return True


def remember(jobs: list[dict], job: dict) -> None:
    """Add ``job`` to a session's deletions, keeping the last :data:`KEEP` (queued ones always)."""
    jobs.append(job)
    while len(jobs) > KEEP:
        old = next((j for j in jobs if j.get("status") != "queued"), None)
        if old is None:
            break
        jobs.remove(old)


def public(job: dict) -> dict:
    """A deletion as the console reads it (no remote log path)."""
    return {k: v for k, v in job.items() if k != "log"}
