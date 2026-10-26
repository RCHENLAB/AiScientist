"""Uploads with AISCIENTIST_UPLOADS_ON_HPC on: the request hands the file to the background staging
queue and answers within seconds, whatever the file's size (Envoy cuts any request that has not
answered in 15 s). Driven through the real FastAPI app with a mock HPC3 executor — no SSH."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'u.db').as_posix()}")
    from fastapi.testclient import TestClient

    from aiscientist.gateway import app as gw
    from aiscientist.gateway.mock_host import MockExecutor
    from aiscientist.gateway.settings import HPCSettings

    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)   # accounts on: test_connection_ownership.py
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "ws"
    conn.settings.uploads_on_hpc = True
    conn.executor = MockExecutor(username="tester")
    gw.CONNECTIONS[conn.id] = conn
    gw._STAGING.pop(conn.owner, None)
    # names on "HPC3" = what the mock was handed (MockExecutor answers the gateway's `test -e` and
    # its folder listing from that), so name collisions are checkable offline
    gate = threading.Event()
    gate.set()
    real_put = conn.executor.put_file

    def gated_put(local, remote):           # tests close the gate to make a copy "slow"
        gate.wait(5)
        real_put(local, remote)

    conn.executor.put_file = gated_put
    try:
        yield TestClient(gw.app), conn, gw, gate
    finally:
        gate.set()
        q = gw._STAGING.pop(conn.owner, None)
        if q is not None:
            for job in q.jobs():         # a worker left running would use the next test's connection
                q.wait(job.id, 5.0)
            q.shutdown()
        gw.CONNECTIONS.pop(conn.id, None)
        loop.close()


def _chunk(client, conn, uid, name, offset, data, done):
    return client.post("/api/upload/chunk", data={
        "connection_id": conn.id, "upload_id": uid, "name": name, "offset": str(offset),
        "done": "true" if done else "false"}, files={"chunk": ("chunk", data, "application/octet-stream")})


def _wait_job(client, conn, job_id, timeout=5.0):
    """The job once it has settled AND its worker has returned. The queue sets the state first and
    only then saves, announces and cleans up (a `done` job's local file is deleted in that last
    step), so a test that acted on the state alone raced the end of the job — on CI, 2026-10-12."""
    from aiscientist.gateway import app as gw
    end = time.time() + timeout
    while time.time() < end:
        job = client.get("/api/upload/staging", params={"connection_id": conn.id, "job_id": job_id}).json()["job"]
        if job and job["state"] in ("done", "failed", "cancelled"):
            queue = gw._STAGING.get(conn.owner)
            if queue is not None:
                queue.wait(job_id, timeout)
            return client.get("/api/upload/staging", params={"connection_id": conn.id, "job_id": job_id}).json()["job"]
        time.sleep(0.02)
    raise AssertionError("job did not settle")


def test_a_quick_copy_answers_uploaded_in_one_round_trip(env):
    client, conn, gw, _gate = env
    r = client.post("/api/upload", data={"connection_id": conn.id},
                    files={"file": ("pbmc.csv", b"a,b\n1,2\n", "text/csv")})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "uploaded"
    assert body["path"].endswith("/uploads/tester/pbmc.csv")
    assert not (conn.workspace / "uploads" / "pbmc.csv").exists()     # verified on HPC3 → local copy gone


def test_a_slow_copy_answers_staging_and_finishes_in_the_background(env, monkeypatch):
    client, conn, gw, gate = env
    monkeypatch.setattr(gw, "_STAGE_WAIT_S", 0.05)
    gate.clear()
    t0 = time.time()
    _chunk(client, conn, "big1", "big.h5ad", 0, b"x" * 10, done=False)
    body = _chunk(client, conn, "big1", "big.h5ad", 10, b"y" * 10, done=True).json()
    assert time.time() - t0 < 2.0                                      # never held for the copy
    assert body["status"] == "staging" and body["job_id"]
    assert (conn.workspace / "uploads" / "big.h5ad").exists()           # kept until the copy is verified
    gate.set()
    job = _wait_job(client, conn, body["job_id"])
    assert job["state"] == "done" and job["verified"] == "size" and job["size"] == 20
    assert not (conn.workspace / "uploads" / "big.h5ad").exists()


def test_a_resent_last_chunk_gets_the_same_outcome_not_a_restart(env, monkeypatch):
    """The bug this fixes: the browser re-sent a last chunk that Envoy had cut off, the server had
    already renamed the .part away and answered 409 received=0, and the upload started over."""
    client, conn, gw, gate = env
    monkeypatch.setattr(gw, "_STAGE_WAIT_S", 0.05)
    gate.clear()
    first = _chunk(client, conn, "big2", "m.h5ad", 0, b"z" * 8, done=True).json()
    again = _chunk(client, conn, "big2", "m.h5ad", 0, b"z" * 8, done=True)
    assert again.status_code == 200 and again.json()["job_id"] == first["job_id"]
    st = client.get("/api/upload/status", params={"connection_id": conn.id, "upload_id": "big2"}).json()
    assert st["received"] == 8 and st["finalized"] is True             # resume sees "complete", not 0
    gate.set()
    assert _wait_job(client, conn, first["job_id"])["state"] == "done"
    assert len([1 for _l, r in conn.executor.state.staged_files if r.endswith("/m.h5ad")]) == 1


def test_a_second_upload_of_the_same_name_does_not_overwrite_the_first(env):
    client, conn, gw, _gate = env
    for _ in range(2):
        r = client.post("/api/upload", data={"connection_id": conn.id},
                        files={"file": ("same.csv", b"1\n", "text/csv")})
        assert r.json()["status"] == "uploaded"
    remotes = [r for _l, r in conn.executor.state.staged_files]
    assert remotes[0].endswith("/same.csv") and remotes[1].endswith("/same (1).csv")


def test_a_folder_name_taken_on_hpc3_is_reserved_under_a_name_the_upload_can_use(env, monkeypatch):
    """An earlier folder is on HPC3 and its local copy is gone, so only the remote check sees it. The
    new name must survive the path cleaning every file's upload applies: "run1 (1)" did not."""
    client, conn, gw, _gate = env
    monkeypatch.setattr(gw, "_remote_name_taken", lambda c, rel: rel == "run1")
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "run1"}).json()["folder"]
    assert folder == "run1_2"
    assert gw.safe_relpath(f"{folder}/sub/b.csv") == f"{folder}/sub/b.csv"
    assert (conn.workspace / "uploads" / folder).is_dir()


def test_a_folder_becomes_one_dataset_when_its_last_copy_lands(env, monkeypatch):
    client, conn, gw, gate = env
    monkeypatch.setattr(gw, "_STAGE_WAIT_S", 0.05)
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "run1"}).json()["folder"]
    gate.clear()
    for rel in (f"{folder}/a.txt", f"{folder}/sub/b.txt"):
        r = client.post("/api/upload", data={"connection_id": conn.id, "rel_path": rel},
                        files={"file": ("f", b"data", "application/octet-stream")})
        assert r.status_code == 200 and r.json()["status"] == "staging"
    reg = client.post("/api/upload/register-folder", json={"connection_id": conn.id, "folder": folder}).json()
    assert reg["status"] == "staging" and reg["count"] == 2 and reg["pending"] == 2
    gate.set()
    end = time.time() + 5
    while time.time() < end:
        f = client.get("/api/upload/staging", params={"connection_id": conn.id, "folder": folder}).json()["folder"]
        if f.get("recorded"):
            break
        time.sleep(0.02)
    assert f["recorded"] is True and f["count"] == 2 and f["size"] == 8 and f["done"] == 2
    assert f["remote_root"].endswith(f"/uploads/tester/{folder}")
    for job in gw._staging_queue(conn).jobs(folder):
        gw._staging_queue(conn).wait(job.id, 5)  # cleanup runs after the state becomes done
    assert not (conn.workspace / "uploads" / folder).exists()            # local tree cleared


def test_a_folder_registered_after_its_copies_finished_records_at_once(env):
    client, conn, gw, _gate = env
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "quick"}).json()["folder"]
    client.post("/api/upload", data={"connection_id": conn.id, "rel_path": f"{folder}/a.txt"},
                files={"file": ("f", b"abc", "application/octet-stream")})
    reg = client.post("/api/upload/register-folder", json={"connection_id": conn.id, "folder": folder}).json()
    assert reg["status"] == "registered" and reg["count"] == 1 and reg["size"] == 3


def test_cancel_and_list(env, monkeypatch):
    client, conn, gw, gate = env
    monkeypatch.setattr(gw, "_STAGE_WAIT_S", 0.05)
    gate.clear()
    body = _chunk(client, conn, "big3", "c.h5ad", 0, b"q" * 4, done=True).json()
    listed = client.get("/api/upload/staging", params={"connection_id": conn.id}).json()["jobs"]
    assert [j["id"] for j in listed] == [body["job_id"]] and "local_path" not in listed[0]
    assert client.post("/api/upload/staging/cancel", json={"connection_id": conn.id,
                                                            "job_id": body["job_id"]}).json()["status"] == "cancelled"
    gate.set()
    assert _wait_job(client, conn, body["job_id"])["state"] == "cancelled"


def _folder_files(client, conn, folder, names):
    for name in names:
        r = client.post("/api/upload", data={"connection_id": conn.id, "rel_path": f"{folder}/{name}"},
                        files={"file": ("f", b"data", "application/octet-stream")})
        assert r.status_code == 200


def test_the_summary_is_single_files_plus_one_line_per_folder(env, monkeypatch):
    """The page restores its cards from this after a reload: a 5,000-file folder must be one line."""
    client, conn, gw, gate = env
    monkeypatch.setattr(gw, "_STAGE_WAIT_S", 0.05)
    gate.clear()
    single = _chunk(client, conn, "one", "one.bin", 0, b"abc", done=True).json()
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "study"}).json()["folder"]
    _folder_files(client, conn, folder, ("a.txt", "sub/b.txt"))
    everything = client.get("/api/upload/staging", params={"connection_id": conn.id}).json()
    assert len(everything["jobs"]) == 3 and "folders" not in everything          # unchanged without the flag
    brief = client.get("/api/upload/staging", params={"connection_id": conn.id, "summary": 1}).json()
    assert [j["id"] for j in brief["jobs"]] == [single["job_id"]]
    assert len(brief["folders"]) == 1
    line = brief["folders"][0]
    assert (line["name"], line["count"], line["pending"], line["failed"], line["size"]) == (folder, 2, 2, 0, 8)
    assert not line.get("registered") and "user_id" not in line
    client.post("/api/upload/register-folder", json={"connection_id": conn.id, "folder": folder, "count": 2})
    gate.set()
    for job in gw._staging_queue(conn).jobs():
        gw._staging_queue(conn).wait(job.id, 5)
    after = client.get("/api/upload/staging", params={"connection_id": conn.id, "summary": 1}).json()
    assert after["folders"] == []                                                # recorded: nothing left to follow


def test_cancel_takes_a_whole_folder(env, monkeypatch):
    client, conn, gw, gate = env
    monkeypatch.setattr(gw, "_STAGE_WAIT_S", 0.05)
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "halfway"}).json()["folder"]
    _folder_files(client, conn, folder, ("done.txt",))
    queue = gw._staging_queue(conn)
    first = queue.jobs(folder)[0]
    assert _wait_job(client, conn, first.id)["state"] == "done"
    gate.clear()
    _folder_files(client, conn, folder, ("b.txt", "c.txt"))
    answer = client.post("/api/upload/staging/cancel", json={"connection_id": conn.id, "folder": folder}).json()
    assert answer["status"] == "cancelled" and len(answer["job_ids"]) == 2
    gate.set()
    for job in queue.jobs(folder):
        queue.wait(job.id, 5)
    assert sorted(j.state for j in queue.jobs(folder)) == ["cancelled", "cancelled", "done"]   # the finished one stays
    assert client.post("/api/upload/staging/cancel",
                       json={"connection_id": conn.id, "folder": folder}).json()["status"] == "not_running"


def test_dismiss_takes_the_failed_files_of_a_folder(env, monkeypatch):
    client, conn, gw, _gate = env
    from aiscientist.gateway import staging
    monkeypatch.setattr(staging, "RETRY_DELAYS", (0.01, 0.01))
    monkeypatch.setattr(gw, "_STAGE_WAIT_S", 0.05)
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "partly"}).json()["folder"]
    _folder_files(client, conn, folder, ("ok.txt",))
    queue = gw._staging_queue(conn)
    assert _wait_job(client, conn, queue.jobs(folder)[0].id)["state"] == "done"
    real_put = conn.executor.put_file

    def broken(local, remote):
        raise OSError("connection reset")

    conn.executor.put_file = broken
    _folder_files(client, conn, folder, ("bad1.txt", "bad2.txt"))
    for job in queue.jobs(folder):
        _wait_job(client, conn, job.id)
    conn.executor.put_file = real_put
    assert sorted(j.state for j in queue.jobs(folder)) == ["done", "failed", "failed"]
    kept = conn.workspace / "uploads" / folder / "bad1.txt"
    assert kept.exists()                                                       # kept, so a retry could resume
    answer = client.post("/api/upload/staging/dismiss", json={"connection_id": conn.id, "folder": folder}).json()
    assert answer["status"] == "dismissed" and len(answer["job_ids"]) == 2
    assert [j.state for j in queue.jobs(folder)] == ["done"]                    # the copied file is not touched
    assert not kept.exists()


# --- the contract with the upload layer -----------------------------------------------------------

def test_a_job_is_found_by_the_upload_id(env, monkeypatch):
    client, conn, gw, gate = env
    monkeypatch.setattr(gw, "_STAGE_WAIT_S", 0.05)
    gate.clear()
    body = _chunk(client, conn, "myupload", "u.h5ad", 0, b"u" * 4, done=True).json()
    found = client.get("/api/upload/staging", params={"connection_id": conn.id, "upload_id": "myupload"}).json()["job"]
    assert found["id"] == body["job_id"] and found["upload_id"] == "myupload"
    assert client.get("/api/upload/staging",
                      params={"connection_id": conn.id, "upload_id": "other"}).json()["job"] is None


def test_a_folder_waits_for_the_number_of_files_the_browser_announced(env):
    """register-folder may arrive while later files are still being uploaded (parallel uploads):
    the folder must not be recorded with part of its files."""
    client, conn, gw, _gate = env
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "cr"}).json()["folder"]

    def send(rel):
        return client.post("/api/upload", data={"connection_id": conn.id, "rel_path": rel},
                           files={"file": ("f", b"1234", "application/octet-stream")}).json()

    assert send(f"{folder}/a.txt")["status"] == "uploaded"
    reg = client.post("/api/upload/register-folder",
                      json={"connection_id": conn.id, "folder": folder, "count": 2}).json()
    assert reg["status"] == "staging"                              # 1 of 2 files so far
    state = client.get("/api/upload/staging", params={"connection_id": conn.id, "folder": folder}).json()["folder"]
    assert not state.get("recorded") and state["done"] == 1
    assert send(f"{folder}/b.txt")["status"] == "uploaded"         # the second file completes it
    end = time.time() + 5
    while time.time() < end:
        state = client.get("/api/upload/staging", params={"connection_id": conn.id, "folder": folder}).json()["folder"]
        if state.get("recorded"):
            break
        time.sleep(0.02)
    assert state["recorded"] is True and state["count"] == 2 and state["size"] == 8


def test_a_copy_is_pushed_only_by_a_session_of_the_same_hpc_account(env):
    client, conn, gw, _gate = env
    from aiscientist.gateway import staging
    assert gw._staging_conn(conn.owner, "tester") is conn
    assert gw._staging_conn(conn.owner) is conn                    # a job that names no account
    with pytest.raises(staging.NoSession):
        gw._staging_conn(conn.owner, "someone_else")
    with pytest.raises(staging.NoSession):
        gw._staging_conn("another_owner", "tester")


def test_the_job_records_its_hpc_account_and_the_browser_checksum(env, monkeypatch):
    client, conn, gw, gate = env
    local = conn.workspace / "uploads" / "x.bin"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"abc")
    import hashlib
    job = gw._submit_staging(conn, local, "x.bin", name="x.bin", upload_id="t1",
                             client_sha256=hashlib.sha256(b"abc").hexdigest().upper(), unpack=False)
    assert job.hpc_user == "tester" and job.upload_id == "t1"
    assert job.client_sha256 == hashlib.sha256(b"abc").hexdigest()
    assert _wait_job(client, conn, job.id)["state"] == "done"


def _failed_job(client, conn, gw, monkeypatch, name="lost.bin"):
    """Hand over a file whose copy fails every attempt; returns (job, the commands sent to HPC3)."""
    from aiscientist.gateway import staging
    monkeypatch.setattr(staging, "RETRY_DELAYS", (0.01, 0.01))
    commands: list[str] = []
    real_exec = conn.executor.exec
    conn.executor.exec = lambda command, *a, **kw: (commands.append(command), real_exec(command, *a, **kw))[1]
    real_put = conn.executor.put_file

    def broken(local, remote):
        raise OSError("connection reset")

    conn.executor.put_file = broken
    local = conn.workspace / "uploads" / name
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"abc")
    job = gw._submit_staging(conn, local, name, name=name)
    assert _wait_job(client, conn, job.id)["state"] == "failed"
    conn.executor.put_file = real_put
    return job, commands


def test_dismissing_a_failed_copy_removes_what_it_kept_for_a_retry(env, monkeypatch):
    client, conn, gw, _gate = env
    job, commands = _failed_job(client, conn, gw, monkeypatch)
    assert (conn.workspace / "uploads" / "lost.bin").exists()      # kept, so a retry could resume
    del commands[:]
    assert client.post("/api/upload/staging/dismiss",
                       json={"connection_id": conn.id, "job_id": job.id}).json()["status"] == "dismissed"
    assert not (conn.workspace / "uploads" / "lost.bin").exists()
    assert [c for c in commands if c.startswith("rm ")] == [f"rm -f {job.remote_path}.part"]
    assert client.get("/api/upload/staging", params={"connection_id": conn.id, "job_id": job.id}).json()["job"] is None


def test_dismissing_never_touches_the_part_of_a_newer_copy_of_the_same_name(env, monkeypatch):
    client, conn, gw, gate = env
    old, commands = _failed_job(client, conn, gw, monkeypatch)
    gate.clear()                                                   # the newer copy is in flight
    again = conn.workspace / "uploads" / "again" / "lost.bin"
    again.parent.mkdir(parents=True)
    again.write_bytes(b"abcd")
    newer = gw._submit_staging(conn, again, "lost.bin", name="lost.bin")
    assert newer.remote_path == old.remote_path
    del commands[:]
    client.post("/api/upload/staging/dismiss", json={"connection_id": conn.id, "job_id": old.id})
    assert [c for c in commands if c.startswith("rm ")] == []
    gate.set()
    assert _wait_job(client, conn, newer.id)["state"] == "done"


def test_settled_copies_age_out_and_failed_ones_take_their_leftovers_along(env, monkeypatch):
    client, conn, gw, _gate = env
    failed, commands = _failed_job(client, conn, gw, monkeypatch, "old_failed.bin")
    recent, _ = _failed_job(client, conn, gw, monkeypatch, "recent_failed.bin")
    done = _chunk(client, conn, "u-done", "done.bin", 0, b"abc", True).json()
    assert _wait_job(client, conn, done["job_id"])["state"] == "done"
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "open"}).json()["folder"]
    client.post("/api/upload", data={"connection_id": conn.id, "rel_path": f"{folder}/a.bin"},
                files={"file": ("a.bin", b"abc", "application/octet-stream")})
    queue = gw._staging_queue(conn)
    in_folder = queue.jobs(folder)[0]
    assert _wait_job(client, conn, in_folder.id)["state"] == "done"
    week_ago = time.time() - 8 * 86400
    for job_id in (failed.id, done["job_id"], in_folder.id):
        queue.get(job_id).updated_at = week_ago
    del commands[:]

    assert gw._expire_settled(conn) == ["old_failed.bin"]
    left = {j.name for j in queue.jobs()}
    assert left == {"recent_failed.bin", f"{folder}/a.bin"}        # recent; and a file of a folder still open
    assert not (conn.workspace / "uploads" / "old_failed.bin").exists()
    assert (conn.workspace / "uploads" / "recent_failed.bin").exists()
    assert [c for c in commands if c.startswith("rm ")] == [f"rm -f {failed.remote_path}.part"]


def test_keep_settled_days_zero_keeps_everything(env, monkeypatch):
    client, conn, gw, _gate = env
    failed, _ = _failed_job(client, conn, gw, monkeypatch)
    gw._staging_queue(conn).get(failed.id).updated_at = time.time() - 400 * 86400
    monkeypatch.setenv("AISCIENTIST_UPLOAD_KEEP_SETTLED_DAYS", "0")
    assert gw._expire_settled(conn) == [] and gw._staging_queue(conn).get(failed.id) is not None


def test_handing_the_same_upload_over_twice_gives_one_copy(env):
    """A completion request answered late and the browser's retry of it both finish the upload: while
    the first copy runs, the second hand-over must return that job, not start another to the same path."""
    client, conn, gw, gate = env
    local = conn.workspace / "uploads" / "twice.bin"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"abc")
    gate.clear()                                                   # the copy waits
    first = gw._submit_staging(conn, local, "twice.bin", name="twice.bin", upload_id="same")
    again = gw._submit_staging(conn, local, "twice.bin", name="twice.bin", upload_id="same")
    assert again.id == first.id
    jobs = client.get("/api/upload/staging", params={"connection_id": conn.id}).json()["jobs"]
    assert [j["id"] for j in jobs if j["upload_id"] == "same"] == [first.id]
    gate.set()
    assert _wait_job(client, conn, first.id)["state"] == "done"
    # A settled job does not stand in for a new file: the old chunk protocol reuses an id for the
    # same file uploaded again.
    local.write_bytes(b"abcd")
    later = gw._submit_staging(conn, local, "twice.bin", name="twice.bin", upload_id="same")
    assert later.id != first.id and _wait_job(client, conn, later.id)["state"] == "done"


# --- the same folder chosen again (RCHENLAB/AiScientist issue 1: "uploads are not incremental") ----

def _reserve(client, conn, name, files=None):
    body = {"connection_id": conn.id, "name": name}
    if files is not None:
        body["files"] = [[rel, len(data)] for rel, data in files]
    return client.post("/api/upload/reserve-folder", json=body)


def _send(client, conn, folder, rel, data):
    r = client.post("/api/upload", data={"connection_id": conn.id, "rel_path": f"{folder}/{rel}"},
                    files={"file": ("f", data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def _settle(client, conn, gw, folder):
    for job in gw._staging_queue(conn).jobs(folder):
        _wait_job(client, conn, job.id)


def _copies(conn, suffix):
    return [r for _l, r in conn.executor.state.staged_files if r.endswith(suffix)]


RUN = [("web_summary.html", b"w" * 7), ("outs/matrix.h5", b"m" * 40), ("outs/sorted.bam", b"b" * 900)]


def test_a_folder_that_stopped_half_way_is_continued_not_started_again(env):
    """The first upload sent 2 of 3 files and stopped (the browser never registered the folder).
    Choosing the folder again goes to the same folder and is told which files are there."""
    client, conn, gw, _gate = env
    first = _reserve(client, conn, "run1", RUN).json()
    assert first == {"status": "reserved", "folder": "run1", "merged": False, "have": []}
    for rel, data in RUN[:2]:
        _send(client, conn, "run1", rel, data)
    _settle(client, conn, gw, "run1")

    again = _reserve(client, conn, "run1", RUN).json()
    assert again == {"status": "reserved", "folder": "run1", "merged": True, "have": [0, 1]}
    assert not (conn.workspace / "uploads" / "run1_2").exists()

    _send(client, conn, "run1", *RUN[2])
    reg = client.post("/api/upload/register-folder",
                      json={"connection_id": conn.id, "folder": "run1", "count": 3}).json()
    _settle(client, conn, gw, "run1")
    state = client.get("/api/upload/staging", params={"connection_id": conn.id, "folder": "run1"}).json()["folder"]
    assert reg["status"] in ("registered", "staging")
    assert state["recorded"] is True and state["count"] == 3 and state["size"] == 947 and state["done"] == 3
    for rel, _data in RUN:                                # every file was copied to HPC3 exactly once
        assert len(_copies(conn, f"/run1/{rel}")) == 1
    events = [e for e in gw.upload_log.tail(conn.workspace) if e["event"] in ("folder_reserved", "folder_recorded")]
    assert events[-2]["merged"] is True and events[-2]["already_there"] == 2 and events[-2]["to_send"] == 1
    assert events[-1]["count"] == 3 and events[-1]["added"] == 1


def test_a_folder_that_is_all_there_is_recognised_and_nothing_is_sent(env):
    client, conn, gw, _gate = env
    _reserve(client, conn, "run1", RUN)
    for rel, data in RUN:
        _send(client, conn, "run1", rel, data)
    assert client.post("/api/upload/register-folder",
                       json={"connection_id": conn.id, "folder": "run1", "count": 3}).json()["status"] == "registered"
    _settle(client, conn, gw, "run1")
    sent_before = len(conn.executor.state.staged_files)

    again = _reserve(client, conn, "run1", RUN).json()
    assert again["folder"] == "run1" and again["merged"] is True and again["have"] == [0, 1, 2]
    reg = client.post("/api/upload/register-folder",
                      json={"connection_id": conn.id, "folder": "run1", "count": 3}).json()
    assert reg["status"] == "registered" and reg["already"] is True
    assert reg["count"] == 3 and reg["size"] == 947
    assert len(conn.executor.state.staged_files) == sent_before
    assert not (conn.workspace / "uploads" / "run1").exists()        # the placeholder is gone again


@pytest.mark.parametrize("other, why", [
    ([("web_summary.html", b"w" * 8), ("outs/matrix.h5", b"m" * 40)], "a file of another size"),
    ([("web_summary.html", b"w" * 7)], "it lacks a file that is there"),
    ([("notes.txt", b"n")], "nothing in common"),
])
def test_another_folder_of_the_same_name_still_gets_its_own(env, other, why):
    client, conn, gw, _gate = env
    _reserve(client, conn, "run1", RUN[:2])
    for rel, data in RUN[:2]:
        _send(client, conn, "run1", rel, data)
    client.post("/api/upload/register-folder", json={"connection_id": conn.id, "folder": "run1", "count": 2})
    _settle(client, conn, gw, "run1")
    got = _reserve(client, conn, "run1", other).json()
    assert got == {"status": "reserved", "folder": "run1_2", "merged": False, "have": []}, why


def test_the_copy_of_the_same_folder_is_found_under_its_second_name(env):
    """``run1`` is someone else's data; the folder chosen now was uploaded before as ``run1_2``."""
    client, conn, gw, _gate = env
    _reserve(client, conn, "run1", [("other.txt", b"x")])
    _send(client, conn, "run1", "other.txt", b"x")
    assert _reserve(client, conn, "run1", RUN).json()["folder"] == "run1_2"
    _send(client, conn, "run1_2", *RUN[0])
    _settle(client, conn, gw, "run1")
    _settle(client, conn, gw, "run1_2")
    got = _reserve(client, conn, "run1", RUN).json()
    assert got["folder"] == "run1_2" and got["merged"] is True and got["have"] == [0]


def test_a_folder_whose_copies_are_still_running_is_not_added_to(env, monkeypatch):
    client, conn, gw, gate = env
    monkeypatch.setattr(gw, "_STAGE_WAIT_S", 0.05)
    _reserve(client, conn, "run1", RUN)
    _send(client, conn, "run1", *RUN[0])
    _settle(client, conn, gw, "run1")
    gate.clear()
    assert _send(client, conn, "run1", *RUN[1])["status"] == "staging"
    refused = _reserve(client, conn, "run1", RUN)
    assert refused.status_code == 409 and "still being copied" in refused.json()["error"]
    assert not (conn.workspace / "uploads" / "run1_2").exists()       # and no second copy is started
    gate.set()
    _settle(client, conn, gw, "run1")
    assert _reserve(client, conn, "run1", RUN).json()["have"] == [0, 1]


def test_a_file_whose_copy_failed_is_sent_again_with_the_missing_ones(env, monkeypatch):
    client, conn, gw, _gate = env
    from aiscientist.gateway import staging
    monkeypatch.setattr(staging, "RETRY_DELAYS", (0.01, 0.01))
    monkeypatch.setattr(gw, "_STAGE_WAIT_S", 0.05)
    _reserve(client, conn, "run1", RUN)
    _send(client, conn, "run1", *RUN[0])
    _settle(client, conn, gw, "run1")
    real_put = conn.executor.put_file
    conn.executor.put_file = lambda local, remote: (_ for _ in ()).throw(OSError("connection reset"))
    _send(client, conn, "run1", *RUN[1])
    _settle(client, conn, gw, "run1")
    conn.executor.put_file = real_put
    kept = conn.workspace / "uploads" / "run1" / "outs" / "matrix.h5"
    assert kept.exists() and [j.state for j in gw._staging_queue(conn).jobs("run1")] == ["done", "failed"]

    got = _reserve(client, conn, "run1", RUN).json()
    assert got["merged"] is True and got["have"] == [0]
    assert not kept.exists() and gw._staging_queue(conn).jobs("run1") == []   # the page sends it afresh
    for rel, data in RUN[1:]:
        _send(client, conn, "run1", rel, data)
    client.post("/api/upload/register-folder", json={"connection_id": conn.id, "folder": "run1", "count": 3})
    _settle(client, conn, gw, "run1")
    state = client.get("/api/upload/staging", params={"connection_id": conn.id, "folder": "run1"}).json()["folder"]
    assert state["recorded"] is True and state["count"] == 3 and state["size"] == 947


def test_a_folder_name_that_was_recorded_once_is_recorded_again(env):
    """The first ``run1`` was recorded, then deleted from Storage. A new ``run1`` used to be counted
    together with the old one's copies under a record that already said "recorded": the page showed
    it as uploaded and no dataset was ever recorded."""
    client, conn, gw, _gate = env
    for _ in range(2):
        assert _reserve(client, conn, "run1").json()["folder"] == "run1"
        _send(client, conn, "run1", "a.txt", b"abc")
        reg = client.post("/api/upload/register-folder",
                          json={"connection_id": conn.id, "folder": "run1", "count": 1}).json()
        assert reg["status"] == "registered" and reg["count"] == 1 and reg["size"] == 3
        _settle(client, conn, gw, "run1")
        conn.executor.state.staged_files.clear()          # deleted on HPC3: the name is free again
        conn.executor.state.remote_sizes.clear()


def test_which_folder_counts_as_the_same_folder():
    from aiscientist.gateway import app as gw
    chosen = [("a.txt", 1), ("sub/b.bin", 20), ("c.txt", 3)]
    assert gw._same_folder({"a.txt": 1, "sub/b.bin": 20}, chosen) == [0, 1]
    assert gw._same_folder({"sub/b.bin": 20, "sub/b.bin.part": 7}, chosen) == [1]   # a copy that never finished
    assert gw._same_folder({"a.txt": 1, "extra.txt": 5}, chosen) is None            # a file the chosen one lacks
    assert gw._same_folder({"a.txt": 2}, chosen) is None                            # another size
    assert gw._same_folder({}, chosen) is None                                      # nothing there: nothing shared
    assert gw._same_folder({"c.txt.part": 1}, chosen) is None                       # only a leftover: not evidence
    # a real file that happens to end in .part is a file like any other
    assert gw._same_folder({"x.part": 4}, [("x.part", 4), ("x", 9)]) == [0]


def test_the_chosen_files_are_cleaned_the_way_the_upload_cleans_them():
    from aiscientist.gateway import app as gw
    assert gw._chosen_files([["my data/a b.txt", 3], [".hidden", 0]]) == [("mydata/ab.txt", 3), ("hidden", 0)]
    assert gw._chosen_files([["a b.txt", 1], ["ab.txt", 1]]) is None      # two files, one cleaned path
    assert gw._chosen_files([["a.txt", -1]]) is None
    assert gw._chosen_files([["../..", 1]]) is None
    assert gw._chosen_files("a.txt") is None and gw._chosen_files([]) is None and gw._chosen_files(None) is None
