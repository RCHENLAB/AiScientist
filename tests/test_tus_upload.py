"""End-to-end tus creation, offset recovery, checksum and staging handoff."""
import asyncio
import base64
import hashlib
import json
import os
import time
from types import SimpleNamespace

import pytest


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


def _metadata(**values):
    return ",".join(f"{key} {base64.b64encode(str(value).encode()).decode()}"
                    for key, value in values.items())


def _create(client, conn, size, **fields):
    return client.post("/api/tus", headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(size),
                                             "Upload-Metadata": _metadata(connection_id=conn.id,
                                                                          filename="data.csv", **fields)})


def _patch(client, url, offset, data, digest=None, final_sha=None):
    headers = {"Tus-Resumable": "1.0.0", "Content-Type": "application/offset+octet-stream",
               "Upload-Offset": str(offset)}
    if digest is not None:
        headers["Upload-Checksum"] = "sha256 " + base64.b64encode(digest).decode()
    if final_sha is not None:
        headers["Upload-Final-SHA256"] = final_sha
    return client.patch(url, headers=headers, content=data)


def test_resume_checksum_and_local_result(env):
    client, conn, _gw = env
    payload = b"gene,count\nRHO,42\n"
    created = _create(client, conn, len(payload), client_sha256=hashlib.sha256(payload).hexdigest())
    assert created.status_code == 201
    url = created.headers["location"]
    assert url.startswith("/api/tus/")
    assert _patch(client, url, 0, payload[:5], b"wrong".ljust(32, b"!")).status_code == 460
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).headers["upload-offset"] == "0"
    first = payload[:5]
    assert _patch(client, url, 0, first, hashlib.sha256(first).digest()).status_code == 204
    assert _patch(client, url, 0, first).status_code == 409
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).headers["upload-offset"] == "5"
    rest = payload[5:]
    assert _patch(client, url, 5, rest, hashlib.sha256(rest).digest()).status_code == 204
    result = client.get(url).json()
    assert result["status"] == "complete" and result["size"] == len(payload)
    assert (conn.workspace / "uploads" / result["name"]).read_bytes() == payload
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).headers["upload-offset"] == str(len(payload))


def test_folder_path_and_termination(env):
    client, conn, _gw = env
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id,
                                                               "name": "study"}).json()["folder"]
    bad = _create(client, conn, 3, folder=folder, rel_path="wrong/a.csv")
    assert bad.status_code == 400
    made = _create(client, conn, 3, folder=folder, rel_path=f"{folder}/nested/a.csv")
    url = made.headers["location"]
    assert _patch(client, url, 0, b"abc", hashlib.sha256(b"abc").digest()).status_code == 204
    assert (conn.workspace / "uploads" / folder / "nested" / "a.csv").read_bytes() == b"abc"
    unfinished = _create(client, conn, 9).headers["location"]
    assert client.delete(unfinished, headers={"Tus-Resumable": "1.0.0"}).status_code == 204
    assert client.head(unfinished, headers={"Tus-Resumable": "1.0.0"}).status_code == 404


def test_head_recovers_a_finalization_interrupted_after_rename(env):
    client, conn, _gw = env
    made = _create(client, conn, 3)
    upload_id = made.headers["location"].rsplit("/", 1)[-1]
    meta_path = conn.workspace / "uploads" / ".parts" / f"{upload_id}.json"
    part_path = meta_path.with_suffix(".part")
    dest = conn.workspace / "uploads" / "recovered.csv"
    part_path.write_bytes(b"abc")
    meta = json.loads(meta_path.read_text())
    meta.update(offset=3, status="finalizing", rel_path=dest.name)
    meta_path.write_text(json.dumps(meta))
    os.replace(part_path, dest)
    response = client.head(made.headers["location"], headers={"Tus-Resumable": "1.0.0"})
    assert response.status_code == 200 and response.headers["upload-offset"] == "3"
    assert client.get(made.headers["location"]).json()["status"] == "complete"
    assert dest.read_bytes() == b"abc"


def test_staging_handoff(env):
    client, conn, gw = env
    conn.settings.uploads_on_hpc = True
    made = _create(client, conn, 3)
    url = made.headers["location"]
    full_sha = hashlib.sha256(b"abc").hexdigest()
    response = _patch(client, url, 0, b"abc", hashlib.sha256(b"abc").digest(), final_sha=full_sha)
    assert response.status_code == 204 and response.headers["upload-staging-job"]
    result = client.get(url).json()
    assert result["job_id"] == response.headers["upload-staging-job"]
    assert result["status"] == "staging"
    assert gw._staging_queue(conn).get(result["job_id"]).client_sha256 == full_sha
    assert gw._staging_queue(conn).wait(result["job_id"], 5).state == "done"


def test_hpc_final_patch_does_not_hash_the_whole_file_in_request(env, monkeypatch):
    client, conn, gw = env
    from aiscientist.gateway import tus_upload

    conn.settings.uploads_on_hpc = True
    monkeypatch.setattr(gw, "_submit_staging", lambda *args, **kwargs: SimpleNamespace(id="queued"))
    monkeypatch.setattr(gw, "_peek_local_upload", lambda path: None)
    real_sha256 = hashlib.sha256
    calls = []

    def counted_sha256(*args, **kwargs):
        calls.append(1)
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr(tus_upload.hashlib, "sha256", counted_sha256)
    url = _create(client, conn, 3).headers["location"]
    response = _patch(client, url, 0, b"abc", final_sha=real_sha256(b"abc").hexdigest())
    assert response.status_code == 204 and response.headers["upload-staging-job"] == "queued"
    assert len(calls) == 1  # PATCH chunk; no second pass over the complete file


def test_new_connection_to_same_workspace_resumes_upload(env):
    client, conn, gw = env
    url = _create(client, conn, 6).headers["location"]
    assert _patch(client, url, 0, b"abc", hashlib.sha256(b"abc").digest()).status_code == 204
    gw.CONNECTIONS.pop(conn.id)
    next_conn = gw.Connection(conn.settings, mock=True, loop=conn.loop, username="tester")
    next_conn.workspace = conn.workspace
    next_conn.executor = conn.executor
    gw.CONNECTIONS[next_conn.id] = next_conn
    try:
        assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).headers["upload-offset"] == "3"
        assert _patch(client, url, 3, b"def", hashlib.sha256(b"def").digest()).status_code == 204
        assert (conn.workspace / "uploads" / "data.csv").read_bytes() == b"abcdef"
    finally:
        gw.CONNECTIONS.pop(next_conn.id, None)


def test_a_stalled_patch_never_holds_up_head_or_another_patch(env, monkeypatch):
    """Prod, 2026-10-13: the browser's network path vanished mid-PATCH with no FIN or RST. That PATCH
    held the upload until the proxy's 300 s idle timeout, the browser's HEAD waited behind it until
    the proxy's 15 s request timeout answered 504, and tus-js-client sent the 14.57 GiB file again
    from byte 0. Here the body stalls (it neither finishes nor disconnects): a HEAD and a second
    PATCH are answered 423 at once with the true offset, the stalled PATCH is given up after the idle
    timeout without moving the offset, and the same upload then continues."""
    import httpx
    import time
    from aiscientist.gateway import tus_upload

    client, conn, gw = env
    monkeypatch.setattr(tus_upload, "_IDLE_SECONDS", 1.0)
    payload = b"0123456789"
    url = _create(client, conn, len(payload)).headers["location"]
    parts = conn.workspace / "uploads" / ".parts"
    upload_id = url.rsplit("/", 1)[-1]
    tus = {"Tus-Resumable": "1.0.0"}
    chunk = {**tus, "Content-Type": "application/offset+octet-stream", "Upload-Offset": "0"}
    seen = {}

    async def scenario():
        asked_again = asyncio.Event()

        async def stalled_body():
            yield payload[:4]
            asked_again.set()                 # the server has the first block and waits for more
            await asyncio.Event().wait()      # none comes, and the connection stays open

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://test") as http:
            stuck = asyncio.create_task(http.patch(url, headers=chunk, content=stalled_body()))
            await asyncio.wait_for(asked_again.wait(), 5)
            started = time.monotonic()
            seen["head"] = await http.head(url, headers=tus)
            seen["patch"] = await http.patch(url, headers=chunk, content=payload)
            seen["quick"] = time.monotonic() - started
            seen["stalled"] = await asyncio.wait_for(stuck, 10)
            seen["files"] = sorted(p.name for p in parts.iterdir())
            seen["after"] = await http.head(url, headers=tus)
            seen["resumed"] = await http.patch(url, headers=chunk, content=payload)

    asyncio.run(scenario())
    assert seen["head"].status_code == 423 and seen["head"].headers["upload-offset"] == "0"
    assert seen["patch"].status_code == 423
    assert seen["quick"] < 0.5                                  # neither waited for the stalled PATCH
    assert seen["stalled"].status_code == 408 and seen["stalled"].headers["upload-offset"] == "0"
    # one upload, nothing staged left over, nothing appended
    assert seen["files"] == [f"{upload_id}.json", f"{upload_id}.part"]
    assert seen["after"].status_code == 200 and seen["after"].headers["upload-offset"] == "0"
    assert seen["resumed"].status_code == 204
    assert (conn.workspace / "uploads" / "data.csv").read_bytes() == payload
    from aiscientist.gateway import upload_log
    events = [e["event"] for e in upload_log.tail(conn.workspace)]
    assert events == ["created", "busy", "busy", "chunk_dropped", "received"]


def test_an_upload_without_a_live_connection_is_kept_not_forgotten(env, monkeypatch):
    """After a gateway restart the account has no connection until the page connects again. The
    404 answered then made tus-js-client forget the upload and send the file again from byte 0;
    while the server still has it, the answer is 423 and the browser asks again."""
    client, conn, gw = env
    monkeypatch.setattr(gw, "CONSOLE_RUNS_DIR", conn.workspace.parent / "runs")
    conn.workspace = gw.CONSOLE_RUNS_DIR / conn.owner
    url = _create(client, conn, 6).headers["location"]
    assert _patch(client, url, 0, b"abc", hashlib.sha256(b"abc").digest()).status_code == 204
    gw.CONNECTIONS.pop(conn.id)                                  # the gateway restarted
    try:
        head = client.head(url, headers={"Tus-Resumable": "1.0.0"})
        assert head.status_code == 423 and head.headers["upload-offset"] == "3"
        assert _patch(client, url, 3, b"def").status_code == 423
        assert client.head(f"/api/tus/{'0' * 32}", headers={"Tus-Resumable": "1.0.0"}).status_code == 404
        assert client.get(url).status_code == 423
    finally:
        gw.CONNECTIONS[conn.id] = conn                           # the page connected again
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).headers["upload-offset"] == "3"
    assert _patch(client, url, 3, b"def", hashlib.sha256(b"def").digest()).status_code == 204
    assert (conn.workspace / "uploads" / "data.csv").read_bytes() == b"abcdef"


def test_head_reloads_metadata_inside_lock(env, monkeypatch):
    client, conn, _gw = env
    from aiscientist.gateway import tus_upload

    url = _create(client, conn, 3).headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 204
    real_load = tus_upload._load

    def stale_load(request, upload_id):
        found_conn, meta, paths, error = real_load(request, upload_id)
        if meta:
            meta = {**meta, "status": "uploading"}
        return found_conn, meta, paths, error

    monkeypatch.setattr(tus_upload, "_load", stale_load)
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).status_code == 200
    assert (conn.workspace / "uploads" / "data.csv").read_bytes() == b"abc"


def test_delete_reloads_staging_job_inside_lock(env, monkeypatch):
    client, conn, gw = env
    from aiscientist.gateway import tus_upload

    conn.settings.uploads_on_hpc = True
    url = _create(client, conn, 3).headers["location"]
    response = _patch(client, url, 0, b"abc")
    job_id = response.headers["upload-staging-job"]
    queue = gw._staging_queue(conn)
    real_cancel = queue.cancel
    cancelled = []

    def record_cancel(value):
        cancelled.append(value)
        return real_cancel(value)

    monkeypatch.setattr(queue, "cancel", record_cancel)
    real_load = tus_upload._load

    def stale_load(request, upload_id):
        found_conn, meta, paths, error = real_load(request, upload_id)
        if meta:
            meta = {**meta, "job_id": "", "status": "uploading"}
        return found_conn, meta, paths, error

    monkeypatch.setattr(tus_upload, "_load", stale_load)
    assert client.delete(url, headers={"Tus-Resumable": "1.0.0"}).status_code == 204
    assert cancelled == [job_id]


def test_terminal_finalization_error_is_not_left_pending(env):
    client, conn, _gw = env
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id,
                                                               "name": "study"}).json()["folder"]
    dest = conn.workspace / "uploads" / folder / "a.csv"
    dest.write_bytes(b"existing")
    url = _create(client, conn, 3, folder=folder, rel_path=f"{folder}/a.csv").headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 422
    assert client.get(url).json()["status"] == "failed"
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).status_code == 410
    assert not (conn.workspace / "uploads" / ".parts" / f"{url.rsplit('/', 1)[-1]}.part").exists()


def test_bad_version_and_unknown_connection(env):
    client, conn, _gw = env
    assert client.options("/api/tus").headers["tus-extension"] == "creation,termination,checksum"
    assert client.post("/api/tus", headers={"Upload-Length": "3"}).status_code == 412
    bad = client.post("/api/tus", headers={"Tus-Resumable": "1.0.0", "Upload-Length": "3",
                                           "Upload-Metadata": _metadata(connection_id="not-a-connection")})
    assert bad.status_code == 404


def test_a_second_folder_of_the_same_name_gets_its_own_folder(env):
    client, conn, _gw = env
    first = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "study"}).json()["folder"]
    second = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "study"}).json()["folder"]
    assert first == "study" and second == "study_2"
    made = _create(client, conn, 3, folder=second, rel_path=f"{second}/nested/a.csv")
    assert made.status_code == 201, made.text
    assert _patch(client, made.headers["location"], 0, b"abc", hashlib.sha256(b"abc").digest()).status_code == 204
    assert (conn.workspace / "uploads" / second / "nested" / "a.csv").read_bytes() == b"abc"
    assert not any((conn.workspace / "uploads" / first).rglob("*.csv"))      # nothing merged into the first


FOLDER = [("nested/a.csv", b"abc"), ("b.csv", b"defg")]


def _reserve_files(client, conn, name, files):
    return client.post("/api/upload/reserve-folder", json={
        "connection_id": conn.id, "name": name, "files": [[rel, len(data)] for rel, data in files]}).json()


def _send_folder_file(client, conn, folder, rel, data):
    made = _create(client, conn, len(data), folder=folder, rel_path=f"{folder}/{rel}")
    assert made.status_code == 201, made.text
    done = _patch(client, made.headers["location"], 0, data, hashlib.sha256(data).digest(),
                  final_sha=hashlib.sha256(data).hexdigest())
    assert done.status_code == 204, done.text
    return done


def test_the_same_folder_chosen_again_gets_only_its_missing_files(env):
    """Kept on the server (no copy to HPC3): the folder that is there is the one on its disk."""
    client, conn, _gw = env
    assert _reserve_files(client, conn, "study", FOLDER) == {
        "status": "reserved", "folder": "study", "merged": False, "have": []}
    _send_folder_file(client, conn, "study", *FOLDER[0])
    again = _reserve_files(client, conn, "study", FOLDER)
    assert again == {"status": "reserved", "folder": "study", "merged": True, "have": [0]}
    _send_folder_file(client, conn, "study", *FOLDER[1])
    reg = client.post("/api/upload/register-folder",
                      json={"connection_id": conn.id, "folder": "study", "count": 2}).json()
    assert reg["status"] == "registered" and reg["count"] == 2 and reg["size"] == 7
    uploads = conn.workspace / "uploads"
    assert (uploads / "study" / "nested" / "a.csv").read_bytes() == b"abc"
    assert not (uploads / "study_2").exists()
    # the whole folder again: all of it is there
    assert _reserve_files(client, conn, "study", FOLDER)["have"] == [0, 1]
    # a folder of that name with other content is not this folder
    assert _reserve_files(client, conn, "study", [("nested/a.csv", b"abcd")])["folder"] == "study_2"


def test_the_same_folder_chosen_again_with_copies_to_hpc3(env):
    client, conn, gw = env
    conn.settings.uploads_on_hpc = True
    assert _reserve_files(client, conn, "study", FOLDER)["merged"] is False
    first = _send_folder_file(client, conn, "study", *FOLDER[0])
    assert gw._staging_queue(conn).wait(first.headers["upload-staging-job"], 5).state == "done"

    again = _reserve_files(client, conn, "study", FOLDER)       # another browser: nothing remembered
    assert again["folder"] == "study" and again["merged"] is True and again["have"] == [0]
    second = _send_folder_file(client, conn, "study", *FOLDER[1])
    assert gw._staging_queue(conn).wait(second.headers["upload-staging-job"], 5).state == "done"
    client.post("/api/upload/register-folder", json={"connection_id": conn.id, "folder": "study", "count": 2})
    state = client.get("/api/upload/staging", params={"connection_id": conn.id, "folder": "study"}).json()["folder"]
    assert state["recorded"] is True and state["count"] == 2 and state["size"] == 7
    remotes = [r for _l, r in conn.executor.state.staged_files]
    assert sorted(r.rsplit("/study/", 1)[1] for r in remotes) == ["b.csv", "nested/a.csv"]   # each copied once


# --- continuing an upload from another browser ------------------------------------------------------
# A browser keeps its unfinished uploads in its own storage. Another computer, a private window or
# cleared site data had none, and the file started again from byte 0 while the server held most of
# it. Here the "first chunk" is 4 bytes, so the files stay small.

MODIFIED = 1_760_400_000_000          # File.lastModified, ms
BIG = b"0123456789AB"                 # three chunks of 4


@pytest.fixture()
def small_head(monkeypatch):
    from aiscientist.gateway import tus_upload
    monkeypatch.setattr(tus_upload, "_HEAD_BYTES", 4)
    return tus_upload


def _other_browser(gw, conn):
    """A second connection of the same account: another browser, with nothing stored."""
    other = gw.Connection(conn.settings, mock=True, loop=conn.loop, username="tester")
    other.workspace = conn.workspace
    other.executor = conn.executor
    gw.CONNECTIONS[other.id] = other
    return other


def _find(client, conn, data=BIG, *, name="data.csv", rel_path="", modified=MODIFIED, head=None):
    head = head if head is not None else hashlib.sha256(data[:4]).hexdigest()
    response = client.post("/api/tus/find", json={"connection_id": conn.id, "name": name, "rel_path": rel_path,
                                                   "size": len(data), "last_modified": modified,
                                                   "head_sha256": head})
    assert response.status_code == 200, response.text
    return response.json()["upload"]


def _started(client, conn, data=BIG, chunks=1, **fields):
    """An upload begun in one browser and left after ``chunks`` chunks of 4."""
    fields.setdefault("last_modified", MODIFIED)
    if fields["last_modified"] is None:                             # a page that does not send it
        del fields["last_modified"]
    url = _create(client, conn, len(data), **fields).headers["location"]
    for i in range(chunks):
        piece = data[4 * i:4 * i + 4]
        assert _patch(client, url, 4 * i, piece, hashlib.sha256(piece).digest()).status_code == 204
    return url


def test_another_browser_continues_a_partial_upload_from_its_offset(env, small_head):
    client, conn, gw = env
    url = _started(client, conn)
    other = _other_browser(gw, conn)
    try:
        found = _find(client, other)
        assert found == {"url": url, "offset": 4, "length": len(BIG)}
        assert client.head(url, headers={"Tus-Resumable": "1.0.0", "X-Connection-ID": other.id}).headers["upload-offset"] == "4"
        rest = BIG[4:]
        assert _patch(client, url, 4, rest, hashlib.sha256(rest).digest(),
                      final_sha=hashlib.sha256(BIG).hexdigest()).status_code == 204
        assert (conn.workspace / "uploads" / "data.csv").read_bytes() == BIG
        assert _find(client, other) is None                        # complete: nothing left to continue
    finally:
        gw.CONNECTIONS.pop(other.id, None)
    from aiscientist.gateway import upload_log
    events = [e["event"] for e in upload_log.tail(conn.workspace)]
    assert events.count("created") == 1 and "resume_found" in events


def test_a_different_file_of_the_same_name_size_and_time_gets_a_new_upload(env, small_head):
    client, conn, _gw = env
    url = _started(client, conn)
    other = BIG.replace(b"0123", b"wxyz")                           # same name, size and time
    assert _find(client, conn, other) is None
    fresh = _started(client, conn, other, chunks=0)
    assert fresh != url
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).headers["upload-offset"] == "4"   # untouched
    from aiscientist.gateway import upload_log
    assert [e["event"] for e in upload_log.tail(conn.workspace)].count("resume_mismatch") == 1


def test_an_expired_failed_or_unmatched_partial_is_not_offered(env, small_head):
    client, conn, _gw = env
    url = _started(client, conn)
    sidecar = conn.workspace / "uploads" / ".parts" / f"{url.rsplit('/', 1)[-1]}.json"
    assert _find(client, conn)["url"] == url
    assert _find(client, conn, modified=MODIFIED + 1) is None       # another modification time
    assert _find(client, conn, name="other.csv") is None
    assert _find(client, conn, BIG + b"!") is None                  # another size
    week_ago = time.time() - 7 * 86400 - 60
    os.utime(sidecar, (week_ago, week_ago))
    assert _find(client, conn) is None                              # expired, even before the sweep removes it
    os.utime(sidecar, None)
    meta = json.loads(sidecar.read_text())
    sidecar.write_text(json.dumps({**meta, "status": "failed"}))
    assert _find(client, conn) is None
    sidecar.write_text(json.dumps(meta))
    assert _find(client, conn)["url"] == url


def test_nothing_is_offered_before_the_first_chunk_or_for_an_older_page(env, small_head):
    client, conn, _gw = env
    _started(client, conn, chunks=0)                                # created, nothing received
    assert _find(client, conn) is None
    _started(client, conn, chunks=1, last_modified=None)            # a page that did not send the time
    assert _find(client, conn) is None
    assert _find(client, conn, b"0123") is None                     # one chunk or less: never part-way


def test_of_two_partial_uploads_the_one_further_along_is_offered(env, small_head):
    client, conn, _gw = env
    _started(client, conn, chunks=1)
    further = _started(client, conn, chunks=2)
    _started(client, conn, chunks=1)
    assert _find(client, conn) == {"url": further, "offset": 8, "length": len(BIG)}


def test_find_needs_the_file_described(env):
    client, conn, _gw = env
    base = {"connection_id": conn.id, "name": "data.csv", "size": 12, "last_modified": MODIFIED}
    assert client.post("/api/tus/find", json={**base, "head_sha256": "zz"}).status_code == 400
    assert client.post("/api/tus/find", json={**base, "size": None, "head_sha256": "0" * 64}).status_code == 400
    assert client.post("/api/tus/find", json={**base, "connection_id": "nope", "head_sha256": "0" * 64}).status_code == 404


@pytest.mark.parametrize("on_hpc", (False, True), ids=("kept-on-server", "copied-to-hpc3"))
def test_a_folder_whose_files_all_stopped_part_way_is_continued(env, small_head, on_hpc):
    """Before any file of it was complete, choosing the folder again in another browser reserved a
    new folder (study_2) and sent everything again."""
    client, conn, gw = env
    conn.settings.uploads_on_hpc = on_hpc
    files = [("big.bin", BIG), ("small.csv", b"abc")]
    assert _reserve_files(client, conn, "study", files)["folder"] == "study"
    url = _started(client, conn, folder="study", rel_path="study/big.bin")
    other = _other_browser(gw, conn)
    try:
        again = _reserve_files(client, other, "study", files)
        assert again == {"status": "reserved", "folder": "study", "merged": True, "have": []}
        assert _find(client, other, rel_path="study/big.bin", name="big.bin")["url"] == url
        assert _find(client, other, rel_path="study_2/big.bin", name="big.bin") is None
        # a folder of that name holding a different file is still not this folder
        assert _reserve_files(client, other, "study", [("big.bin", BIG + b"!")])["folder"] == "study_2"
    finally:
        gw.CONNECTIONS.pop(other.id, None)


def _unfinished(client, conn):
    response = client.get("/api/tus/unfinished", params={"connection_id": conn.id})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_new_browser_is_told_which_uploads_wait(env, small_head):
    """Nothing stored in this browser, so nothing said the account had uploads waiting: the user had
    to remember to choose the file again. The server lists the ones choosing it again continues."""
    client, conn, gw = env
    single = _started(client, conn)
    _reserve_files(client, conn, "study", [("raw/a.bin", BIG), ("b.bin", BIG)])
    in_a = _started(client, conn, chunks=2, folder="study", rel_path="study/raw/a.bin")
    in_b = _started(client, conn, folder="study", rel_path="study/b.bin")
    _started(client, conn, chunks=0)                                # created, nothing received
    _started(client, conn, last_modified=None)                      # a page that did not send the time
    _started(client, conn, chunks=3)                                # complete
    failed = _started(client, conn)
    expired = _started(client, conn)
    parts = conn.workspace / "uploads" / ".parts"
    sidecar = parts / f"{failed.rsplit('/', 1)[-1]}.json"
    sidecar.write_text(json.dumps({**json.loads(sidecar.read_text()), "status": "failed"}))
    week_ago = time.time() - 7 * 86400 - 60
    os.utime(parts / f"{expired.rsplit('/', 1)[-1]}.json", (week_ago, week_ago))
    other = _other_browser(gw, conn)
    try:
        listed = _unfinished(client, other)
    finally:
        gw.CONNECTIONS.pop(other.id, None)
    (one,) = listed["files"]
    assert {k: one[k] for k in ("id", "url", "name", "rel_path", "folder", "size", "offset")} == {
        "id": single.rsplit("/", 1)[-1], "url": single, "name": "data.csv", "rel_path": "", "folder": "",
        "size": len(BIG), "offset": 4}
    assert one["created_at"] > 0
    (study,) = listed["folders"]
    assert {k: study[k] for k in ("folder", "size", "offset")} == {"folder": "study", "size": 24, "offset": 12}
    assert [(f["url"], f["rel_path"], f["offset"]) for f in study["files"]] == [
        (in_b, "study/b.bin", 4), (in_a, "study/raw/a.bin", 8)]
    # Discard on a card: the server forgets the upload, and it is not listed again
    assert client.delete(single, headers={"Tus-Resumable": "1.0.0"}).status_code == 204
    assert _unfinished(client, conn)["files"] == []


def test_the_list_names_a_file_as_the_browser_does(env, small_head):
    """The server keeps "my data.csv" as mydata.csv; the card says what the user picked."""
    client, conn, _gw = env
    made = client.post("/api/tus", headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(len(BIG)),
                                             "Upload-Metadata": _metadata(connection_id=conn.id, filename="my data.csv",
                                                                          last_modified=MODIFIED)})
    url = made.headers["location"]
    assert _patch(client, url, 0, BIG[:4], hashlib.sha256(BIG[:4]).digest()).status_code == 204
    (listed,) = _unfinished(client, conn)["files"]
    assert listed["name"] == "my data.csv"
    assert _find(client, conn, name="my data.csv")["url"] == url    # the page asks with the same name


def test_a_listed_upload_continues_when_its_file_is_chosen_again(env, small_head):
    client, conn, gw = env
    url = _started(client, conn)
    other = _other_browser(gw, conn)
    try:
        (listed,) = _unfinished(client, other)["files"]
        assert listed["url"] == url
        assert _find(client, other) == {"url": url, "offset": listed["offset"], "length": listed["size"]}
        rest = BIG[4:]
        assert _patch(client, url, 4, rest, hashlib.sha256(rest).digest()).status_code == 204
        assert _unfinished(client, other) == {"files": [], "folders": []}
    finally:
        gw.CONNECTIONS.pop(other.id, None)
    from aiscientist.gateway import upload_log
    assert [e["event"] for e in upload_log.tail(conn.workspace)].count("created") == 1


def test_the_list_needs_a_connection(env):
    client, conn, _gw = env
    assert _unfinished(client, conn) == {"files": [], "folders": []}    # no uploads yet
    assert client.get("/api/tus/unfinished", params={"connection_id": "nope"}).status_code == 404
    assert client.get("/api/tus/unfinished").status_code == 404


def test_the_sweep_keeps_an_index_entry_while_its_upload_lives(env, small_head):
    client, conn, _gw = env
    url = _started(client, conn)
    parts = conn.workspace / "uploads" / ".parts"
    sidecar = parts / f"{url.rsplit('/', 1)[-1]}.json"
    (entry,) = (parts / "by-file").iterdir()
    long_ago = time.time() - 8 * 86400
    os.utime(entry, (long_ago, long_ago))                           # created long ago, still receiving bytes
    small_head._last_cleanup.clear()
    small_head._cleanup_parts(parts)
    assert entry.exists() and _find(client, conn)["url"] == url
    os.utime(sidecar, (long_ago, long_ago))                         # and now abandoned
    small_head._last_cleanup.clear()
    small_head._cleanup_parts(parts)
    assert not sidecar.exists() and not entry.exists()


def test_the_first_chunk_is_the_browsers_chunk():
    from pathlib import Path
    from aiscientist.gateway import tus_upload
    app_js = (Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js").read_text()
    assert "const UPLOAD_CHUNK = 8 * 1024 * 1024;" in app_js
    assert tus_upload._HEAD_BYTES == 8 * 1024 * 1024
