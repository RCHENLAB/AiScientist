"""Background staging of uploads onto HPC3 (gateway/staging.py), with no SSH: ``push`` copies into a
tmp "remote" dir and ``verify`` compares hashes, so what is pinned is the job lifecycle — states,
retries, cancellation, and surviving a restart."""

from __future__ import annotations

import hashlib
import shutil
import threading
import time
from pathlib import Path

import pytest

from aiscientist.gateway import staging as st


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Harness:
    def __init__(self, tmp: Path, streams: int = 2) -> None:
        self.tmp = tmp
        self.remote = tmp / "remote"
        self.remote.mkdir()
        self.events: list[tuple[str, str]] = []
        self.fail_pushes = 0          # this many pushes raise before one succeeds
        self.pushes = 0               # copies that really ran
        self.unpack_errors: list[Exception] = []   # raised by the next unpack calls, in order
        self.unpacks = 0
        self.no_session = False
        self.push_gate: threading.Event | None = None
        self.store = st.StagingStore(tmp / "uploads" / ".staging")
        self.q = self.queue(streams)

    def queue(self, streams: int = 2) -> st.StagingQueue:
        return st.StagingQueue(self.store, push=self.push, verify=self.verify,
                               on_change=lambda j: self.events.append((j.id, j.state)), streams=streams)

    def push(self, job: st.StagingJob, progress) -> None:
        if self.no_session:
            raise st.NoSession("no HPC3 session")
        if self.fail_pushes > 0:
            self.fail_pushes -= 1
            raise OSError("connection reset")
        if self.push_gate is not None:
            while not self.push_gate.wait(0.01):
                progress(0, job.size)      # raises Cancelled once the job is cancelled
        dst = Path(job.remote_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(job.local_path, dst)
        self.pushes += 1
        progress(job.size, job.size)

    def unpack(self, job: st.StagingJob) -> dict:
        self.unpacks += 1
        if self.unpack_errors:
            raise self.unpack_errors.pop(0)
        return {"path": job.remote_path + ".d", "files": 3, "bytes": 30}

    def verify(self, job: st.StagingJob) -> str:
        if _sha(Path(job.remote_path)) != job.sha256:
            raise st.VerifyError("checksum mismatch")
        return "sha256"

    def job(self, name: str = "a.h5ad", data: bytes = b"x" * 1000, folder: str = "") -> st.StagingJob:
        local = self.tmp / "uploads" / name
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        return st.new_job(owner="tester", local_path=str(local), remote_path=str(self.remote / name),
                          name=name, size=len(data), folder=folder)

    def states(self, job_id: str, timeout: float = 2.0) -> list[str]:
        """The states announced for a job, in order. The queue sets a state before it announces it,
        so a caller that has just seen the job settle waits here for the last announcement to land
        (reading at once failed on CI, 2026-10-12: the list still ended at "verifying")."""
        end = time.time() + timeout
        while True:
            out: list[str] = []
            for jid, s in list(self.events):
                if jid == job_id and (not out or out[-1] != s):
                    out.append(s)
            if (out and out[-1] in st.TERMINAL) or time.time() > end:
                return out
            time.sleep(0.005)


@pytest.fixture()
def h(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "RETRY_DELAYS", (0.01, 0.01))
    harness = Harness(tmp_path)
    yield harness
    harness.q.shutdown()


def _settle(q: st.StagingQueue, job_id: str, timeout: float = 5.0) -> st.StagingJob:
    end = time.time() + timeout
    while time.time() < end:
        j = q.get(job_id)
        if j is not None and j.state in st.TERMINAL:
            # The state is set before the worker has saved it, announced it and returned. Wait for
            # the worker, or what the test does next races the end of the job.
            q.wait(job_id, timeout)
            return j
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not settle: {q.get(job_id)}")


def test_a_job_is_copied_hashed_verified_and_persisted(h):
    job = h.job()
    h.q.submit(job)
    done = _settle(h.q, job.id)
    assert done.state == "done" and done.verified == "sha256"
    assert done.sha256 == _sha(Path(job.local_path))
    assert Path(job.remote_path).read_bytes() == Path(job.local_path).read_bytes()
    assert h.states(job.id) == ["queued", "pushing", "verifying", "done"]
    # the record on disk agrees, so a restart would not redo it
    on_disk = {j.id: j for j in h.store.load_all()}[job.id]
    assert on_disk.state == "done" and on_disk.sha256 == done.sha256


def test_queued_is_announced_before_the_copy_can_move_the_job_on(tmp_path):
    """CI 2026-10-28: the worker started before submit() announced "queued" and moved the shared job
    to "pushing", so the upload log's first copy line was copy_pushing. A slow first listener makes
    that window wide: the job must still read "queued" when its announcement ends."""
    seen: list[str] = []

    def on_change(job: st.StagingJob) -> None:
        if not seen:
            time.sleep(0.2)
        seen.append(job.state)

    harness = Harness(tmp_path)
    q = st.StagingQueue(harness.store, push=harness.push, verify=harness.verify, on_change=on_change)
    try:
        job = harness.job()
        q.submit(job)
        _settle(q, job.id)
        assert seen[0] == "queued" and harness.pushes == 1
        assert [s for i, s in enumerate(seen) if i == 0 or seen[i - 1] != s] == [
            "queued", "pushing", "verifying", "done"]
    finally:
        q.shutdown()
        harness.q.shutdown()


def test_wait_returns_none_while_running_and_the_job_once_settled(h):
    h.push_gate = threading.Event()
    job = h.job()
    h.q.submit(job)
    assert h.q.wait(job.id, timeout=0.05) is None            # still copying: the request answers "staging"
    h.push_gate.set()
    assert h.q.wait(job.id, timeout=5).state == "done"


def test_no_session_requeues_without_using_an_attempt_and_resume_finishes(h):
    h.no_session = True
    job = h.job()
    h.q.submit(job)
    time.sleep(0.1)
    j = h.q.get(job.id)
    assert j.state == "queued" and j.attempts == 0 and "session" in j.error
    h.no_session = False
    assert [x.id for x in h.q.resume()] == [job.id]          # the next connection picks it up
    assert _settle(h.q, job.id).state == "done"


def test_a_transient_failure_is_retried(h):
    h.fail_pushes = 2
    job = h.job()
    h.q.submit(job)
    done = _settle(h.q, job.id)
    assert done.state == "done" and done.attempts == 3


def test_after_the_retries_the_job_fails_and_the_user_can_retry(h):
    h.fail_pushes = 10
    job = h.job()
    h.q.submit(job)
    failed = _settle(h.q, job.id)
    assert failed.state == "failed" and "connection reset" in failed.error
    assert failed.attempts == len(st.RETRY_DELAYS) + 1
    assert Path(job.local_path).exists()                     # the local copy is kept for the retry
    h.fail_pushes = 0
    assert h.q.retry(job.id) is True
    assert _settle(h.q, job.id).state == "done"


def test_a_checksum_mismatch_is_a_failure_not_a_dataset(h, monkeypatch):
    def corrupting_push(job, progress):
        Path(job.remote_path).write_bytes(b"y" * job.size)
    h.q.push = corrupting_push
    job = h.job()
    h.q.submit(job)
    failed = _settle(h.q, job.id)
    assert failed.state == "failed" and "mismatch" in failed.error


def test_cancel_stops_a_running_copy(h):
    h.push_gate = threading.Event()
    job = h.job()
    h.q.submit(job)
    time.sleep(0.05)
    assert h.q.cancel(job.id) is True
    assert _settle(h.q, job.id).state == "cancelled"
    assert not Path(job.remote_path).exists()


def test_cancel_of_a_queued_job_settles_it(h):
    h.q.shutdown()
    h.q = h.queue(streams=1)
    h.push_gate = threading.Event()
    first, second = h.job("a.bin"), h.job("b.bin")
    h.q.submit(first)
    h.q.submit(second)                                       # waits behind `first` (one stream)
    assert h.q.cancel(second.id) is True
    assert h.q.get(second.id).state == "cancelled"
    h.push_gate.set()
    assert _settle(h.q, first.id).state == "done"


def test_unfinished_jobs_survive_a_gateway_restart(h):
    job = h.job()
    job.state = "pushing"                                    # the gateway died mid-copy
    h.store.save(job)
    h.q.shutdown()
    fresh = h.queue()                                        # a new process reads the store
    try:
        assert [j.id for j in fresh.resume()] == [job.id]
        assert _settle(fresh, job.id).state == "done"
    finally:
        fresh.shutdown()


def test_resume_fails_a_job_whose_local_file_is_gone(h):
    """It can never finish, so it must not sit in the list looking alive."""
    job = h.job()
    job.state = "queued"
    h.store.save(job)
    Path(job.local_path).unlink()
    fresh = h.queue()
    try:
        assert fresh.resume() == []
        gone = fresh.get(job.id)
        assert gone.state == "failed" and "Upload the file again" in gone.error
    finally:
        fresh.shutdown()


def test_a_restart_does_not_use_up_an_attempt(h):
    """Measured on HPC3 2026-10-11: a copy killed with its process came back as attempt 2 of 3."""
    job = h.job()
    job.state, job.attempts = "pushing", 1                   # the process died during attempt 1
    h.store.save(job)
    h.q.shutdown()
    fresh = h.queue()
    try:
        fresh.resume()
        assert _settle(fresh, job.id).attempts == 1          # it ran once more, as attempt 1 again
    finally:
        fresh.shutdown()


def test_a_job_waiting_for_its_retry_keeps_its_count_across_a_restart(h):
    job = h.job()
    job.state, job.attempts = "queued", 2                    # two attempts failed; the timer was pending
    h.store.save(job)
    h.q.shutdown()
    fresh = h.queue()
    try:
        fresh.resume()
        assert _settle(fresh, job.id).attempts == 3
    finally:
        fresh.shutdown()


def test_forget_hands_back_a_settled_job_and_refuses_a_running_one(h):
    h.push_gate = threading.Event()
    job = h.job()
    h.q.submit(job)
    assert h.q.forget(job.id) is None and h.q.get(job.id) is not None     # still copying
    h.push_gate.set()
    _settle(h.q, job.id)
    assert h.q.forget(job.id).id == job.id
    assert h.q.get(job.id) is None and h.q.forget(job.id) is None


def test_folder_registration_round_trips(h):
    assert h.store.folder("data") == {}
    h.store.register_folder("data", remote_root="/dfs/u/data")
    assert h.store.folder("data")["registered"] is True
    h.store.update_folder("data", recorded=True)
    assert h.store.folder("data") == {"folder": "data", "registered": True,
                                      "remote_root": "/dfs/u/data", "recorded": True}
    h.store.delete_folder("data")
    assert h.store.folder("data") == {}


def test_public_view_hides_the_gateway_path(h):
    job = h.job()
    pub = job.public()
    assert "local_path" not in pub and "user_id" not in pub
    assert pub["remote_path"] == job.remote_path and pub["state"] == "queued"


def test_active_names_cover_copies_in_flight(h):
    h.push_gate = threading.Event()
    job = h.job()
    h.q.submit(job)
    assert job.remote_path in h.q.active_names()
    h.push_gate.set()
    _settle(h.q, job.id)
    assert job.remote_path not in h.q.active_names()


# --- the contract between the upload layer and this queue ----------------------------------------

def test_a_matching_browser_checksum_lets_the_copy_through(h):
    job = h.job()
    job.client_sha256 = _sha(Path(job.local_path)).upper()      # case does not matter
    h.q.submit(job)
    done = _settle(h.q, job.id)
    assert done.state == "done" and done.sha256 == _sha(Path(job.local_path))


def test_a_wrong_browser_checksum_fails_before_anything_is_copied(h):
    job = h.job()
    job.client_sha256 = "0" * 64
    h.q.submit(job)
    failed = _settle(h.q, job.id)
    assert failed.state == "failed" and "does not match what the browser sent" in failed.error
    assert h.pushes == 0 and failed.attempts == 1                # not copied, and not retried
    assert Path(job.local_path).exists()


def test_an_archive_is_unpacked_after_it_is_verified(h):
    h.q.shutdown()
    h.q = st.StagingQueue(h.store, push=h.push, verify=h.verify, unpack=h.unpack,
                          on_change=lambda j: h.events.append((j.id, j.state)))
    job = h.job("run.zip")
    job.unpack = True
    h.q.submit(job)
    done = _settle(h.q, job.id)
    assert done.state == "done" and done.unpacked == {"path": job.remote_path + ".d", "files": 3, "bytes": 30}
    assert h.states(job.id) == ["queued", "pushing", "verifying", "unpacking", "done"]


def test_a_failed_unpack_is_retried_without_copying_the_archive_again(h):
    h.q.shutdown()
    h.q = st.StagingQueue(h.store, push=h.push, verify=h.verify, unpack=h.unpack,
                          on_change=lambda j: h.events.append((j.id, j.state)))
    h.unpack_errors = [OSError("srun: job step aborted")]
    job = h.job("run.zip")
    job.unpack = True
    h.q.submit(job)
    done = _settle(h.q, job.id)
    assert done.state == "done" and h.unpacks == 2
    assert h.pushes == 1                                          # 20 GB is not sent twice for this


def test_a_refused_archive_stays_the_file_that_was_uploaded(h):
    """The copy is complete and verified; failing the job would orphan that file on HPC3."""
    h.q.shutdown()
    h.q = st.StagingQueue(h.store, push=h.push, verify=h.verify, unpack=h.unpack,
                          on_change=lambda j: h.events.append((j.id, j.state)))
    h.unpack_errors = [st.Permanent("The archive unpacks to more than the limit.")]
    job = h.job("bomb.zip")
    job.unpack = True
    h.q.submit(job)
    done = _settle(h.q, job.id)
    assert done.state == "done" and done.unpacked is None
    assert "more than the limit" in done.unpack_error
    assert h.unpacks == 1 and done.attempts == 1                  # refused once, not tried again


def test_unpack_is_ignored_when_the_queue_has_no_unpacker(h):
    job = h.job("run.zip")
    job.unpack = True
    h.q.submit(job)
    done = _settle(h.q, job.id)
    assert done.state == "done" and done.unpacked is None


def test_a_job_is_found_by_the_upload_layers_id(h):
    job = h.job()
    job.upload_id = "tus-123"
    h.q.submit(job)
    _settle(h.q, job.id)
    assert h.q.find_upload("tus-123").id == job.id
    assert h.q.find_upload("nope") is None and h.q.find_upload("") is None


def test_a_job_file_with_unknown_fields_still_loads(h):
    """A job written by a newer gateway (after a rollback) must not vanish from the list."""
    job = h.job()
    h.store.save(job)
    path = h.store._path(job.id)
    import json as _json
    data = _json.loads(path.read_text())
    data["a_field_from_the_future"] = 1
    path.write_text(_json.dumps(data))
    assert [j.id for j in h.store.load_all()] == [job.id]
