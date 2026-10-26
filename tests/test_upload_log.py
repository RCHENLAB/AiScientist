"""The server's record of an upload: every step of it, and the failures only the browser saw.

RCHENLAB/AiScientist issue 1: an upload stopped with no error and nothing on the server said where
it had got to. Each step is now a line in ``<workspace>/uploads/.upload.log``.
"""
import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from aiscientist.gateway import upload_log


@pytest.fixture()
def env(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'users.db').as_posix()}")
    from fastapi.testclient import TestClient
    from aiscientist.gateway import app as gw
    from aiscientist.gateway.mock_host import MockExecutor
    from aiscientist.gateway.settings import HPCSettings

    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "workspace"
    conn.settings.uploads_on_hpc = False
    conn.executor = MockExecutor(username="tester")
    gw.CONNECTIONS[conn.id] = conn
    try:
        yield TestClient(gw.app), conn, gw
    finally:
        queue = gw._STAGING.pop(conn.owner, None)
        if queue:
            for job in queue.jobs():     # a worker left running would use the next test's connection
                queue.wait(job.id, 5.0)
            queue.shutdown()
        gw.CONNECTIONS.pop(conn.id, None)
        loop.close()


def _events(conn):
    return upload_log.tail(conn.workspace)


def _create(client, conn, size, **fields):
    meta = {"connection_id": conn.id, "filename": "data.csv", **fields}
    return client.post("/api/tus", headers={
        "Tus-Resumable": "1.0.0", "Upload-Length": str(size),
        "Upload-Metadata": ",".join(f"{k} {base64.b64encode(str(v).encode()).decode()}"
                                    for k, v in meta.items())})


def _patch(client, url, offset, data, digest=None):
    headers = {"Tus-Resumable": "1.0.0", "Content-Type": "application/offset+octet-stream",
               "Upload-Offset": str(offset)}
    if digest is not None:
        headers["Upload-Checksum"] = "sha256 " + base64.b64encode(digest).decode()
    return client.patch(url, headers=headers, content=data)


def test_an_upload_leaves_its_steps_in_the_log(env):
    client, conn, _gw = env
    payload = b"gene,count\nRHO,42\n"
    url = _create(client, conn, len(payload)).headers["location"]
    upload_id = url.rsplit("/", 1)[1]
    assert _patch(client, url, 0, payload[:5], b"wrong".ljust(32, b"!")).status_code == 460
    assert _patch(client, url, 0, payload[:5], hashlib.sha256(payload[:5]).digest()).status_code == 204
    assert _patch(client, url, 0, payload[:5]).status_code == 409
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).status_code == 200
    assert _patch(client, url, 5, payload[5:]).status_code == 204

    events = _events(conn)
    assert [e["event"] for e in events] == ["created", "chunk_refused", "chunk_refused",
                                            "resume_asked", "received"]
    assert all(e["owner"] == "workspace" and e["upload_id"] == upload_id for e in events)
    created, bad_sum, bad_offset, resume, received = events
    assert created["name"] == "data.csv" and created["size"] == len(payload)
    assert "checksum" in bad_sum["error"] and bad_sum["offset"] == 0
    assert "offset 0" in bad_offset["error"] and "has 5" in bad_offset["error"]
    assert resume["offset"] == 5
    assert received["size"] == len(payload) and received["next"] == "kept on the server"
    # one JSON object per line: what an admin greps, and what `tail` reads back
    lines = upload_log.log_path(conn.workspace).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lines] == [e["event"] for e in events]


def test_a_copy_to_hpc3_is_logged_once_per_state_not_once_per_progress_tick(env):
    client, conn, gw = env
    conn.settings.uploads_on_hpc = True
    url = _create(client, conn, 3).headers["location"]
    response = _patch(client, url, 0, b"abc", hashlib.sha256(b"abc").digest())
    job_id = response.headers["upload-staging-job"]
    assert gw._staging_queue(conn).wait(job_id, 5).state == "done"

    events = _events(conn)
    received = next(e for e in events if e["event"] == "received")
    assert received["next"] == "copy to HPC3" and received["job_id"] == job_id
    copy = [e for e in events if e["event"].startswith("copy_")]
    assert [e["event"] for e in copy] == ["copy_queued", "copy_pushing", "copy_verifying", "copy_done"]
    assert all(e["job_id"] == job_id for e in copy)
    done = copy[-1]
    assert done["remote_path"].endswith("/data.csv") and done["verified"] and "total_seconds" in done


def test_progress_does_not_repeat_a_state_and_a_failure_keeps_its_reason(tmp_path):
    job = SimpleNamespace(id="j1", upload_id="u1", name="big.h5ad", folder="", size=4_000_000,
                          sent=0, attempts=1, verified="", remote_path="/dfs/u/big.h5ad",
                          unpacked=None, unpack_error="", state="pushing", error="",
                          created_at=0.0)
    assert upload_log.record_staging(tmp_path, job)["event"] == "copy_pushing"
    job.sent = 2_000_000                                  # a progress notification
    assert upload_log.record_staging(tmp_path, job) is None
    job.state, job.error = "queued", "Connection reset — retrying in 5 s."
    waiting = upload_log.record_staging(tmp_path, job)
    assert waiting["event"] == "copy_queued" and "Connection reset" in waiting["error"]
    job.state, job.error = "failed", "Connection reset"
    failed = upload_log.record_staging(tmp_path, job)
    assert failed["event"] == "copy_failed" and failed["sent"] == 2_000_000
    assert "remote_path" not in failed                    # nothing usable is there
    assert [e["event"] for e in upload_log.tail(tmp_path)] == ["copy_pushing", "copy_queued", "copy_failed"]


def test_the_browser_reports_a_failure_the_server_could_not_see(env):
    client, conn, _gw = env
    r = client.post("/api/upload/report", json={
        "connection_id": conn.id, "name": "Sample3_GSM5676874/", "kind": "folder",
        "size": 9_000_000_000, "sent": 1_200_000_000, "error": "Failed to fetch"})
    assert r.status_code == 200
    (event,) = _events(conn)
    assert event["event"] == "browser_error" and event["error"] == "Failed to fetch"
    assert event["name"] == "Sample3_GSM5676874/" and event["sent"] == 1_200_000_000
    shown = client.get("/api/upload/log", params={"connection_id": conn.id}).json()["events"]
    assert shown == [event]


def test_a_full_log_is_rotated_and_an_unwritable_one_does_not_fail_the_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_log, "_MAX_BYTES", 200)
    for i in range(6):
        upload_log.record(tmp_path, "created", name=f"file{i}.csv", size=i)
    path = upload_log.log_path(tmp_path)
    assert path.with_name(".upload.log.1").is_file()
    assert path.stat().st_size < 400
    # the uploads dir is a file here, so nothing can be written under it
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "uploads").write_text("not a directory")
    assert upload_log.record(blocked, "created", name="x")["event"] == "created"
    assert upload_log.tail(blocked) == []
