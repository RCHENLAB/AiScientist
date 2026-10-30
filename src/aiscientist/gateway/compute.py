"""Where a session computes, behind one small interface (RCHENLAB/AiScientist issue 6).

The gateway host keeps the users' data (``dataset_storage``). A compute backend is a place a
session's runs execute that cannot see the gateway host's disks, so data moves to it for a session
and back. Five operations, and nothing about storage:

    stage_in    copy a dataset from this host into the session's workspace there
    submit      queue a batch job there; returns its id
    status      the state of jobs there
    stage_out   copy a file or a folder from there to this host
    clean_up    remove what an ended session left there

:class:`HPC3Backend` is the one backend: SFTP through the transfer host for the copies, Slurm for
the jobs, ``hpc_gc`` for the cleanup. It wraps the functions the gateway already used and changes
none of them. The gateway reaches HPC3 through it for a session's data movement and housekeeping
(``app._compute``); the tool job lines (``slurm_analysis``, ``slurm_sandbox``, ``slurm_report``,
``scgpt_job``, ``vlreview_job``) still build and supervise their own Slurm jobs with
``slurm_job.run_batch_job``. A second backend implements :class:`ComputeBackend` and is returned by
``app._compute`` for the sessions it serves; where data is stored does not change.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

from . import hpc_gc
from .slurm_job import SlurmJobSpec, _submit

# (bytes done, bytes in all, files done, files in all)
ByteCounter = Callable[[int, int, int, int], None]

_JOB_ID_RE = re.compile(r"[0-9]+(?:_[0-9]+)?")


@runtime_checkable
class ComputeBackend(Protocol):
    """A place a session's runs execute, seen from the gateway host."""

    name: str

    def stage_in(self, scan: Any, dest: str, *, progress: "Callable[[str], None] | None" = None,
                 should_cancel: "Callable[[], bool] | None" = None,
                 on_bytes: "ByteCounter | None" = None) -> dict:
        """Copy what ``scan`` (a ``server_data.Scan``) lists to ``dest`` there. Incremental: a file
        already there at its size is not copied again. Returns ``{"remote", "copied",
        "copied_bytes", "kept", "kept_bytes", "seconds"}``."""
        ...

    def submit(self, spec: SlurmJobSpec) -> str:
        """Queue ``spec`` as a batch job; returns the job id. Does not wait for it."""
        ...

    def status(self, job_ids: Iterable[str]) -> "dict[str, str]":
        """``{job id: state}`` for the jobs the backend knows (``PENDING``, ``RUNNING``,
        ``COMPLETED``, ``FAILED`` ...). A job it has no record of is left out."""
        ...

    def stage_out(self, src: str, dest: Path, *, directory: bool,
                  on_bytes: "ByteCounter | None" = None) -> int:
        """Copy ``src`` there (a file, or a folder's files) to ``dest`` here. Each file must arrive
        at the size it has there. Returns the bytes copied."""
        ...

    def clean_up(self, *, session_dirs: "Iterable[str]" = (), run_dirs: "dict[str, str] | None" = None,
                 runs: "Iterable[str]" = (), session: str = "", legacy_mirrors: "Iterable[str]" = (),
                 verify: "dict[str, str] | None" = None, tag: str = "", log_name: str = "") -> dict:
        """Queue the removal of what ended sessions left there: session folders, the named runs'
        folders (``run_dirs`` by run id; ``runs`` in the retired layout), old copies of server data.
        ``verify`` maps a run to the stage-out manifest its results must match there before its
        folder may go (``stage_out``). Returns ``{"status": "submitted" | "nothing" | "error",
        ...}``; never raises."""
        ...


class HPC3Backend:
    """HPC3 for one session: its SSH executor, its HPC3 account and the shared AiScientist root."""

    name = "hpc3"

    def __init__(self, executor: Any, *, shared_root: str, user: str, session_id: str = "",
                 partition: str = "standard", account: str = "",
                 clean_script: "str | None" = None) -> None:
        self.executor = executor
        self.shared_root = shared_root
        self.user = user
        self.session_id = session_id
        self.partition = partition
        self.account = account
        self.clean_script = clean_script

    def stage_in(self, scan: Any, dest: str, *, progress: "Callable[[str], None] | None" = None,
                 should_cancel: "Callable[[], bool] | None" = None,
                 on_bytes: "ByteCounter | None" = None) -> dict:
        from . import server_data
        return server_data.sync_to_hpc(self.executor, scan, dest, progress=progress,
                                       should_cancel=should_cancel, on_bytes=on_bytes)

    def submit(self, spec: SlurmJobSpec) -> str:
        return _submit(self.executor, spec)

    def status(self, job_ids: Iterable[str]) -> "dict[str, str]":
        ids = [j for j in dict.fromkeys(str(j).strip() for j in job_ids) if _JOB_ID_RE.fullmatch(j)]
        if not ids:
            return {}
        # One accounting query (control plane: allowed on a login node). -X: the job, not its steps.
        res = self.executor.exec(f"sacct -X -n -P -o JobID,State -j {','.join(ids)}", timeout=60)
        out: dict[str, str] = {}
        for line in (getattr(res, "stdout", "") or "").splitlines():
            job, _, state = line.strip().partition("|")
            if job in ids and state.strip():
                out[job] = state.strip()
        return out

    def stage_out(self, src: str, dest: Path, *, directory: bool,
                  on_bytes: "ByteCounter | None" = None) -> int:
        return pull_from_hpc(self.executor, src, dest, directory, on_bytes=on_bytes)

    def clean_up(self, *, session_dirs: "Iterable[str]" = (), run_dirs: "dict[str, str] | None" = None,
                 runs: "Iterable[str]" = (), session: str = "", legacy_mirrors: "Iterable[str]" = (),
                 verify: "dict[str, str] | None" = None, tag: str = "", log_name: str = "") -> dict:
        return hpc_gc.clean_session(
            self.executor, self.shared_root, self.user, session_id=self.session_id,
            session_dirs=list(session_dirs), run_dirs=run_dirs, runs=list(runs), session=session,
            legacy_mirrors=list(legacy_mirrors), verify=verify, tag=tag or "session",
            log_name=log_name or hpc_gc.CLEAN_LOG_NAME, partition=self.partition,
            account=self.account, script_path=self.clean_script)


def pull_from_hpc(ex: Any, src: str, target: Path, directory: bool, *,
                  on_bytes: "ByteCounter | None" = None) -> int:
    """Copy a file, or a directory's files, from HPC3 to ``target`` on this host, over SFTP on the
    transfer host. Each file goes through ``<file>.part`` (continued after an interruption) and must
    arrive at the size HPC3 reports. Returns the bytes copied."""
    from . import dataset_view as dv
    count = on_bytes or (lambda *_a: None)
    if directory:
        listing = ex.exec(f"find {shlex.quote(src.rstrip('/'))} -type f -printf '%P\\t%s\\n'",
                          timeout=300.0)
        if not listing.ok:
            raise OSError(listing.stderr or f"{src} cannot be listed on HPC3")
        entries = dv.parse_find_listing(listing.stdout or "", limit=10 ** 9)["entries"]
        pairs = []
        for entry in entries:
            rel = dv.safe_relpath(entry["path"])
            if rel is None:
                raise OSError(f"unexpected file name in {src}: {entry['path']!r}")
            pairs.append((f"{src.rstrip('/')}/{rel}", target / rel, int(entry["size"])))
    else:
        pairs = [(src, target, int(ex.remote_size(src)))]
    grand = sum(size for _, _, size in pairs)
    total = 0
    count(0, grand, 0, len(pairs))
    get = getattr(ex, "get_file_resumable", None)
    for i, (remote, local, size) in enumerate(pairs, 1):
        local.parent.mkdir(parents=True, exist_ok=True)
        if local.is_file() and local.stat().st_size == size:
            total += size                    # copied by an earlier attempt that stopped later on
            count(total, grand, i, len(pairs))
            continue
        if get is not None:
            done_before = total
            get(remote, str(local),
                progress=lambda got, _of, _d=done_before, _i=i: count(_d + got, grand, _i - 1, len(pairs)))
        else:
            ex.get_file(remote, str(local))
        if local.stat().st_size != size:
            raise OSError(f"{local.name} arrived as {local.stat().st_size} bytes, expected {size}")
        total += size
        count(total, grand, i, len(pairs))
    return total
