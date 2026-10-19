"""Background staging of uploads: their check, and their copy to HPC3 dfs3b where that is still done.

With a data root configured an upload is kept on the gateway host (``StagingJob.dest == "server"``):
the queue then checks the file and publishes it into the owner's dataset folder, and nothing goes to
HPC3. Without one, and ``AISCIENTIST_UPLOADS_ON_HPC`` on, the queue copies it to HPC3 as below.

An upload used to be copied to HPC3 inside the request that delivered its last byte. Envoy, the
eyeserver ingress, ends a request that has not answered within 15 s, and at ~100 MB/s that is every
file over ~1.5 GB: the browser got a 504, retried the last chunk, found the ``.part`` already renamed
away, and started the whole upload again.

Now the request only hands the file over and a :class:`StagingQueue` copies it in the background::

    queued -> pushing -> verifying -> (unpacking ->) done      failed (retried, then left for the user)
                                                               cancelled

Each job is a small JSON file beside the uploads, and the local copy is kept until the remote one is
verified, so a gateway restart loses nothing: the user's next connection resumes the job (the copy
itself resumes from the remote ``.part``). A dataset row is written only for a verified copy.

The queue is per USER, not per browser connection: two tabs of one user share it, so a file is never
pushed twice. The gateway supplies the I/O as callables (``push`` / ``verify`` / ``on_change``), which
keeps this module free of SSH and HTTP and testable with plain functions.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable

ACTIVE = ("queued", "pushing", "verifying", "unpacking")
TERMINAL = ("done", "failed", "cancelled")

# Seconds between automatic retries of a failed copy; after these the job waits for the user.
RETRY_DELAYS: tuple[float, ...] = (5.0, 30.0)


class NoSession(Exception):
    """No live HPC3 session to copy with. The job goes back to ``queued`` without using up an
    attempt, and resumes at the user's next connection."""


class Cancelled(Exception):
    """Raised from the progress callback when the user cancelled the job."""


class VerifyError(Exception):
    """The remote copy does not match the local file."""


class Permanent(Exception):
    """A failure that a retry cannot fix (the file itself is wrong, an archive is refused): the job
    fails at once instead of being copied again."""


@dataclass
class StagingJob:
    id: str
    owner: str
    local_path: str
    remote_path: str
    name: str                 # display name; the dataset name for a single file
    size: int
    kind: str = "other"       # dataset kind for a single file
    folder: str = ""          # top-level folder this file belongs to ("" = a single file)
    user_id: int | None = None
    peek: dict | None = None
    hpc_user: str = ""        # the HPC3 account the copy belongs to; only its sessions may push it
    upload_id: str = ""       # the upload layer's own id for this file (e.g. the tus upload id)
    conversation_id: str = ""  # the console chat the upload was started from ("" = none known)
    client_sha256: str = ""   # SHA-256 the browser computed; "" = not given
    unpack: bool = False      # an archive to unpack on HPC3 once it is verified
    unpack_dest: str = ""     # the folder it unpacks to, chosen once so a retry reuses it
    unpacked: dict | None = None   # what unpacking produced: {"path", "files", "bytes"}
    unpack_error: str = ""    # why the archive was left as a file instead (it was refused)
    # Where the file is kept: "hpc3" (copied to the user's HPC3 uploads) or "server" (kept on the
    # gateway host in the owner's dataset folder; the queue then only checks and publishes it, and
    # ``remote_path`` is that local destination). Jobs written before this field are HPC3 copies.
    dest: str = "hpc3"
    state: str = "queued"
    sent: int = 0
    sha256: str = ""          # of the local file, filled while the copy runs
    verified: str = ""        # how the copy was checked: "sha256" or "size"
    error: str = ""
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public(self) -> dict:
        """What the browser sees: everything but the gateway-local path."""
        d = asdict(self)
        d.pop("local_path", None)
        d.pop("user_id", None)
        return d


def new_job(**kw) -> StagingJob:
    return StagingJob(id=uuid.uuid4().hex[:16], **kw)


class StagingStore:
    """Jobs and folder registrations as JSON files under ``root`` (one per job)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def _path(self, job_id: str) -> Path:
        safe = "".join(c for c in job_id if c.isalnum())
        return self.root / f"{safe}.json"

    def _write(self, path: Path, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with self._lock:
            tmp.write_text(json.dumps(data))
            os.replace(tmp, path)

    def save(self, job: StagingJob) -> None:
        job.updated_at = time.time()
        self._write(self._path(job.id), asdict(job))

    def load_all(self) -> list[StagingJob]:
        jobs = []
        for p in sorted(self.root.glob("*.json")) if self.root.is_dir() else []:
            if p.name.startswith("folder-"):
                continue
            try:
                data = json.loads(p.read_text())
                known = {f.name for f in fields(StagingJob)}
                jobs.append(StagingJob(**{k: v for k, v in data.items() if k in known}))
            except (OSError, ValueError, TypeError):
                continue
        return jobs

    def delete(self, job_id: str) -> None:
        self._path(job_id).unlink(missing_ok=True)

    # A folder becomes ONE dataset once the browser has sent all its files (register-folder) and
    # every file's copy is verified, in whichever order those two things happen.
    def _folder_path(self, folder: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in folder)
        return self.root / f"folder-{safe}.json"

    def start_folder(self, folder: str, **meta) -> dict:
        """A fresh record for a folder whose upload starts now, in place of an earlier upload's.
        That one says ``recorded``, and a folder that is recorded is never recorded again."""
        data = {**meta, "folder": folder}
        self._write(self._folder_path(folder), data)
        return data

    def register_folder(self, folder: str, **meta) -> dict:
        data = {**self.folder(folder), **meta, "folder": folder, "registered": True}
        self._write(self._folder_path(folder), data)
        return data

    def folder(self, folder: str) -> dict:
        p = self._folder_path(folder)
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {}

    def update_folder(self, folder: str, **changes) -> None:
        data = self.folder(folder)
        if data:
            data.update(changes)
            self._write(self._folder_path(folder), data)

    def delete_folder(self, folder: str) -> None:
        self._folder_path(folder).unlink(missing_ok=True)


def sha256_file(path: str, cancel: threading.Event | None = None, block: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                return ""
            b = f.read(block)
            if not b:
                return h.hexdigest()
            h.update(b)


class StagingQueue:
    """Copies jobs in a thread pool of ``streams`` workers.

    ``push(job, progress)`` copies ``job.local_path`` to ``job.remote_path`` (resuming a partial
    copy); ``progress(sent, total)`` raises :class:`Cancelled` once the job is cancelled.
    ``verify(job)`` checks the remote copy (``job.sha256`` holds the local hash) and returns how
    (``"sha256"`` or ``"size"``), raising :class:`VerifyError` on a mismatch. ``on_change(job)`` is
    told about every state change and, at most once a second, about progress. ``unpack(job)``, when
    given, runs for a verified job with ``job.unpack`` set and returns ``{"path", "files", "bytes"}``.
    It raises :class:`Permanent` for an archive it refuses; the job is then done all the same, as the
    plain file that was uploaded, with the reason in ``job.unpack_error`` — the copy is complete and
    verified, and failing it would leave that file on HPC3 with nothing pointing at it.
    """

    def __init__(self, store: StagingStore, *, push: Callable, verify: Callable,
                 on_change: Callable[[StagingJob], None], streams: int = 4,
                 unpack: Callable | None = None) -> None:
        self.store = store
        self.push = push
        self.verify = verify
        self.unpack = unpack
        self.on_change = on_change
        self.streams = max(1, int(streams))
        self._pool = ThreadPoolExecutor(max_workers=self.streams, thread_name_prefix="staging")
        self._lock = threading.RLock()
        self._jobs: dict[str, StagingJob] = {j.id: j for j in store.load_all()}
        self._futures: dict[str, Future] = {}
        self._cancel: dict[str, threading.Event] = {}
        self.folder_lock = threading.Lock()   # the gateway's folder finalisation runs under it

    # -- queries --------------------------------------------------------------

    def get(self, job_id: str) -> StagingJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs(self, folder: str | None = None) -> list[StagingJob]:
        with self._lock:
            js = list(self._jobs.values())
        if folder is not None:
            js = [j for j in js if j.folder == folder]
        return sorted(js, key=lambda j: j.created_at)

    def find_upload(self, upload_id: str) -> StagingJob | None:
        """The job the upload layer created for its upload ``upload_id`` (the newest, if re-sent)."""
        if not upload_id:
            return None
        hits = [j for j in self.jobs() if j.upload_id == upload_id]
        return hits[-1] if hits else None

    def active_names(self) -> set[str]:
        """Remote paths of copies still in flight — taken, even though nothing is there yet."""
        active = [j for j in self.jobs() if j.state in ACTIVE]
        return {j.remote_path for j in active} | {j.unpack_dest for j in active if j.unpack_dest}

    # -- control --------------------------------------------------------------

    def submit(self, job: StagingJob) -> Future:
        with self._lock:
            running = self._futures.get(job.id)
            if running is not None and not running.done():
                return running                   # already copying (e.g. resume() raced a retry)
            self._jobs[job.id] = job
            job.state = "queued"
            job.error = ""
            self.store.save(job)
            self._cancel[job.id] = threading.Event()
            fut = self._pool.submit(self._run, job.id)
            self._futures[job.id] = fut
        self._changed(job)
        return fut

    def resume(self) -> list[StagingJob]:
        """Re-queue every unfinished job (after a restart or a new connection). A failed job is left
        for the user to retry.

        A job found mid-attempt with nothing running it belonged to a process that died: it was
        interrupted, not failed, so it gets that attempt back (measured 2026-10-11: a restart
        otherwise cost one of the three). A job whose local file is gone can never finish; it is
        failed here instead of looking alive for ever."""
        out = []
        for job in self.jobs():
            fut = self._futures.get(job.id)
            if job.state not in ACTIVE or (fut is not None and not fut.done()):
                continue
            # A file kept on the server may already be at its destination (published just before
            # the process died, before the job said so): its check finds it there.
            published = job.dest == "server" and not job.folder and os.path.exists(job.remote_path)
            if not os.path.exists(job.local_path) and not published:
                self._finish(job, "failed", error="The copy of this upload on the server is gone. "
                                                  "Upload the file again.")
                continue
            if job.state != "queued":
                job.attempts = max(0, job.attempts - 1)
            self.submit(job)
            out.append(job)
        return out

    def retry(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.state not in ("failed", "queued") or not os.path.exists(job.local_path):
            return False
        fut = self._futures.get(job_id)
        if fut is not None and not fut.done():
            if job.state != "failed":
                return False
            # It failed a moment ago and its worker is still on its way out: the state is set
            # before the worker returns, so a retry sent at once would otherwise be refused.
            try:
                fut.result(timeout=2.0)
            except Exception:  # noqa: BLE001 - the outcome is on the job, not the future
                pass
            if not fut.done():
                return False
        job.attempts = 0
        self.submit(job)
        return True

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.state in TERMINAL:
            return False
        ev = self._cancel.get(job_id)
        if ev is not None:
            ev.set()
        fut = self._futures.get(job_id)
        if fut is None or fut.cancel() or fut.done():
            self._finish(job, "cancelled")      # never started (or already gone): settle it here
        return True

    def forget(self, job_id: str) -> StagingJob | None:
        """Drop a settled job from the list (the browser dismissed it, or it aged out). Returns the
        job, so the caller can remove what a failed one left behind; None if nothing was dropped."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state in ACTIVE:
                return None
            self._jobs.pop(job_id, None)
            self._futures.pop(job_id, None)
            self._cancel.pop(job_id, None)
        self.store.delete(job_id)
        return job

    def wait(self, job_id: str, timeout: float) -> StagingJob | None:
        """The job once it is settled, or None if it is still running after ``timeout`` seconds."""
        fut = self._futures.get(job_id)
        if fut is not None:
            try:
                fut.result(timeout=timeout)
            except Exception:  # noqa: BLE001 - the outcome is on the job, not the future
                pass
        job = self.get(job_id)
        return job if job is not None and job.state not in ACTIVE else None

    def shutdown(self) -> None:
        for ev in self._cancel.values():
            ev.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- the work -------------------------------------------------------------

    def _changed(self, job: StagingJob) -> None:
        try:
            self.on_change(job)
        except Exception:  # noqa: BLE001 - a broken listener must not break the copy
            pass

    def _set(self, job: StagingJob, state: str, **kw) -> None:
        job.state = state
        for k, v in kw.items():
            setattr(job, k, v)
        self.store.save(job)
        self._changed(job)

    def _finish(self, job: StagingJob, state: str, error: str = "") -> None:
        self._set(job, state, error=error)

    def _run(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None:
            return
        cancel = self._cancel.setdefault(job_id, threading.Event())
        if cancel.is_set():
            self._finish(job, "cancelled")
            return
        job.attempts += 1
        hasher: threading.Thread | None = None
        last = [0.0, 0.0]   # [last notify, last save]

        def progress(sent: int, total: int) -> None:
            if cancel.is_set():
                raise Cancelled()
            job.sent = sent
            now = time.monotonic()
            if now - last[0] >= 1.0:
                last[0] = now
                if now - last[1] >= 5.0:
                    last[1] = now
                    self.store.save(job)
                self._changed(job)

        try:
            if not job.verified:        # a retry after a verified copy has only the unpack left
                self._set(job, "pushing", sent=0)
                digest: dict[str, str] = {}
                # A file kept on the server that was published before a restart is read where it is now.
                source = (job.remote_path if job.dest == "server" and not os.path.exists(job.local_path)
                          else job.local_path)
                if job.client_sha256:
                    # The browser said what it sent: check BEFORE anything goes to HPC3. A mismatch
                    # means the file here is not the user's file; copying it again cannot fix that.
                    digest["sha"] = sha256_file(source, cancel)
                    if cancel.is_set():
                        raise Cancelled()
                    if digest["sha"] != job.client_sha256.lower():
                        raise Permanent("The file on the server does not match what the browser sent "
                                        "(SHA-256 differs). Upload it again.")
                else:
                    # Hash while the copy runs: both read the file, so the second read is from cache.
                    hasher = threading.Thread(
                        target=lambda: digest.update(sha=sha256_file(source, cancel)),
                        name=f"sha-{job_id}", daemon=True)
                    hasher.start()
                self.push(job, progress)
                if hasher is not None:
                    hasher.join()
                if cancel.is_set():
                    raise Cancelled()
                job.sha256 = digest.get("sha", "")
                self._set(job, "verifying", sent=job.size)
                job.verified = self.verify(job) or "size"
            if job.unpack and self.unpack is not None and job.unpacked is None and not job.unpack_error:
                self._set(job, "unpacking", sent=job.size)
                try:
                    job.unpacked = self.unpack(job)
                except Permanent as exc:           # refused: it stays the file that was uploaded
                    job.unpack_error = str(exc) or "The archive was refused."
            self._set(job, "done", sent=job.size, error="")
        except Cancelled:
            self._stop(cancel, hasher)
            self._finish(job, "cancelled")
        except Permanent as exc:
            self._stop(cancel, hasher)
            self._finish(job, "failed", error=str(exc) or "Refused.")
        except NoSession as exc:
            self._stop(cancel, hasher)
            job.attempts = max(0, job.attempts - 1)
            self._set(job, "queued", error=str(exc) or "Waiting for an HPC3 session.")
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job
            self._stop(cancel, hasher)
            msg = str(exc) or type(exc).__name__
            if job.attempts <= len(RETRY_DELAYS):
                delay = RETRY_DELAYS[job.attempts - 1]
                self._set(job, "queued", error=f"{msg} — retrying in {delay:.0f} s.")
                self._cancel[job_id] = threading.Event()
                timer = threading.Timer(delay, self._resubmit, args=(job_id,))
                timer.daemon = True
                timer.start()
            else:
                self._finish(job, "failed", error=msg)

    @staticmethod
    def _stop(cancel: threading.Event, hasher: "threading.Thread | None") -> None:
        """End the local hashing thread of a job that is not going to finish this attempt."""
        cancel.set()
        if hasher is not None:
            hasher.join()

    def _resubmit(self, job_id: str) -> None:
        job = self.get(job_id)
        with self._lock:
            running = self._futures.get(job_id)
            if job is None or job.state != "queued" or (running is not None and not running.done()):
                return
            self._futures[job_id] = self._pool.submit(self._run, job_id)
