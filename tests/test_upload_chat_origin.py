"""An upload remembers the chat it was started from (RCHENLAB/AiScientist issue 7).

The console shows an upload's progress and its Cancel button only in the chat that started it, and
the Datasets page says which chat each upload came from. The server keeps that chat beside the upload
(the tus record), its background copy to HPC3 (the staging job, which a page restores its cards from
after a reload) and its dataset row. Driven through the real FastAPI app with a mock HPC3 executor and
an SQLite account database — no SSH, no network.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import time

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    url = f"sqlite:///{(tmp_path / 'accounts.db').as_posix()}"
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", url)
    from fastapi.testclient import TestClient

    from aiscientist.gateway import app as gw
    from aiscientist.gateway import db, models
    from aiscientist.gateway.mock_host import MockExecutor
    from aiscientist.gateway.settings import HPCSettings

    db.reset()                       # the next use opens the database named above
    db.init_db()
    with db.session_scope() as s:
        user = models.User(username="alice", password_hash="x", role="user", is_active=True)
        s.add(user)
        s.commit()
        user_id = user.id
    # Accounts on, so finished uploads are written to the dataset history. Who may use a connection
    # is test_connection_ownership.py's subject: here every request is the owner's.
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw, "_conn_owner_error", lambda conn, request: None)
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "workspace"
    conn.settings.uploads_on_hpc = True
    conn.executor = MockExecutor(username="tester")
    conn.app_user_id = user_id
    gw.CONNECTIONS[conn.id] = conn
    gw._STAGING.pop(conn.owner, None)
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
        db.reset()


def _metadata(**values):
    return ",".join(f"{key} {base64.b64encode(str(value).encode()).decode()}" for key, value in values.items())


def _upload(client, conn, data: bytes, **fields) -> str:
    """One whole file over tus; returns its upload URL."""
    made = client.post("/api/tus", headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(len(data)),
                                            "Upload-Metadata": _metadata(connection_id=conn.id, **fields)})
    assert made.status_code == 201, made.text
    url = made.headers["location"]
    sent = client.patch(url, content=data, headers={
        "Tus-Resumable": "1.0.0", "Content-Type": "application/offset+octet-stream", "Upload-Offset": "0",
        "Upload-Checksum": "sha256 " + base64.b64encode(hashlib.sha256(data).digest()).decode(),
        "Upload-Final-SHA256": hashlib.sha256(data).hexdigest()})
    assert sent.status_code == 204, sent.text
    return url


def _sidecar(conn, url: str) -> dict:
    return json.loads((conn.workspace / "uploads" / ".parts" / f"{url.rsplit('/', 1)[-1]}.json").read_text())


def _dataset_rows(timeout: float = 5.0, want: int = 1) -> list[dict]:
    """The dataset history once it holds ``want`` rows (the queue writes it after the copy settles)."""
    from aiscientist.gateway import db, models
    end = time.time() + timeout
    while True:
        with db.session_scope() as s:
            rows = [r.public() for r in s.query(models.Dataset).order_by(models.Dataset.id)]
        if len(rows) >= want or time.time() > end:
            return rows
        time.sleep(0.02)


def _settle(gw, conn) -> None:
    queue = gw._staging_queue(conn)
    for job in queue.jobs():
        queue.wait(job.id, 5.0)


def test_a_chat_id_is_kept_as_sent_and_anything_else_is_dropped():
    from aiscientist.gateway.app import chat_ref
    assert chat_ref("42") == "42"
    assert chat_ref(" s-1a2b.c_d ") == "s-1a2b.c_d"
    assert chat_ref(17) == "17"
    for bad in (None, "", "../42", "a b", "<script>", "x" * 65):
        assert chat_ref(bad) == ""


def test_a_file_copied_to_hpc3_keeps_its_chat_from_upload_to_dataset_row(env):
    client, conn, gw = env
    url = _upload(client, conn, b"gene,count\nRHO,42\n", filename="counts.csv", conversation_id="42")
    assert _sidecar(conn, url)["conversation_id"] == "42"
    job_id = client.get(url).json()["job_id"]
    assert gw._staging_queue(conn).get(job_id).conversation_id == "42"
    # what a reloaded page restores its cards from: the card knows its chat again
    listed = client.get("/api/upload/staging", params={"connection_id": conn.id, "summary": 1}).json()
    assert [(j["id"], j["conversation_id"]) for j in listed["jobs"]] == [(job_id, "42")]
    _settle(gw, conn)
    rows = _dataset_rows()
    assert [(r["name"], r["conversation_id"]) for r in rows] == [("counts.csv", "42")]


def test_a_file_kept_on_the_server_records_its_chat(env):
    client, conn, _gw = env
    conn.settings.uploads_on_hpc = False
    _upload(client, conn, b"abc", filename="panel.bed", conversation_id="7")
    assert [(r["name"], r["conversation_id"]) for r in _dataset_rows()] == [("panel.bed", "7")]


def test_an_upload_from_no_chat_or_a_bad_id_records_none(env):
    client, conn, gw = env
    first = _upload(client, conn, b"one", filename="one.txt")
    second = _upload(client, conn, b"two", filename="two.txt", conversation_id="../../etc")
    assert _sidecar(conn, first)["conversation_id"] == "" and _sidecar(conn, second)["conversation_id"] == ""
    _settle(gw, conn)
    assert [r["conversation_id"] for r in _dataset_rows(want=2)] == [None, None]


def test_a_folder_upload_records_the_chat_it_was_started_from(env):
    client, conn, gw = env
    folder = client.post("/api/upload/reserve-folder", json={
        "connection_id": conn.id, "name": "Sample1_WT", "conversation_id": "42"}).json()["folder"]
    for rel, data in (("a.mtx", b"matrix"), ("sub/barcodes.tsv", b"AAAC")):
        _upload(client, conn, data, filename=rel.rsplit("/", 1)[-1], folder=folder, rel_path=f"{folder}/{rel}",
                conversation_id="42")
    reg = client.post("/api/upload/register-folder", json={
        "connection_id": conn.id, "folder": folder, "count": 2, "conversation_id": "42"})
    assert reg.status_code == 200, reg.text
    _settle(gw, conn)
    rows = _dataset_rows()
    assert [(r["name"], r["kind"], r["conversation_id"]) for r in rows] == [(folder, "folder", "42")]


def test_a_folder_still_copying_names_its_chat_to_a_reloaded_page(env, monkeypatch):
    client, conn, gw = env
    import threading
    gate = threading.Event()
    real_put = conn.executor.put_file
    conn.executor.put_file = lambda local, remote: (gate.wait(5), real_put(local, remote))
    monkeypatch.setattr(gw, "_STAGE_WAIT_S", 0.05)
    try:
        folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "slow"}).json()["folder"]
        _upload(client, conn, b"data", filename="a.txt", folder=folder, rel_path=f"{folder}/a.txt")
        client.post("/api/upload/register-folder", json={"connection_id": conn.id, "folder": folder, "count": 1,
                                                          "conversation_id": "9"})
        line = client.get("/api/upload/staging", params={"connection_id": conn.id, "summary": 1}).json()["folders"]
        assert [(f["name"], f["conversation_id"]) for f in line] == [(folder, "9")]
    finally:
        gate.set()
        _settle(gw, conn)


def test_a_dataset_row_keeps_its_chat_and_a_folder_keeps_the_first_one(env):
    _client, _conn, _gw = env
    from aiscientist.gateway import auth_routes, db, models
    with db.session_scope() as s:
        alice = s.query(models.User).filter_by(username="alice").one().id
    auth_routes.record_dataset(alice, "a.h5ad", "/x/a.h5ad", 3, "h5ad", conversation_id="42")
    auth_routes.record_dataset(alice, "b.h5ad", "/x/b.h5ad", 3, "h5ad")
    first = auth_routes.record_folder_dataset(alice, "run1", "/x/run1", 10, conversation_id="5")
    # files added to the same folder from another chat: still the folder the first chat uploaded
    assert auth_routes.record_folder_dataset(alice, "run1", "/x/run1", 20, conversation_id="6") == first
    # a row from before chats were kept takes the chat that adds to it
    late = auth_routes.record_folder_dataset(alice, "run2", "/x/run2", 1)
    auth_routes.record_folder_dataset(alice, "run2", "/x/run2", 2, conversation_id="8")
    rows = {r["path"]: r for r in _dataset_rows(want=4)}
    assert rows["/x/a.h5ad"]["conversation_id"] == "42" and rows["/x/b.h5ad"]["conversation_id"] is None
    assert (rows["/x/run1"]["conversation_id"], rows["/x/run1"]["size_bytes"]) == ("5", 20)
    assert rows["/x/run2"]["id"] == late and rows["/x/run2"]["conversation_id"] == "8"


def test_a_deployed_datasets_table_gains_the_column(tmp_path, monkeypatch):
    """Prod's table predates the column; ``init_db`` adds it (``create_all`` never alters a table)."""
    pytest.importorskip("sqlalchemy")
    from aiscientist.gateway import auth_routes, db
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64) UNIQUE, email VARCHAR(255),
                password_hash VARCHAR(255), role VARCHAR(16), is_active BOOLEAN, created_at DATETIME);
            CREATE TABLE datasets (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id),
                name VARCHAR(255), path VARCHAR(1024), size_bytes BIGINT, kind VARCHAR(32), sha256 VARCHAR(64),
                uploaded_at DATETIME);
            INSERT INTO users (id, username, password_hash, role, is_active) VALUES (1, 'alice', 'x', 'user', 1);
            INSERT INTO datasets (user_id, name, path, size_bytes, kind) VALUES (1, 'old.h5ad', '/x/old.h5ad', 1, 'h5ad');
        """)
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{path.as_posix()}")
    db.reset()
    try:
        db.init_db()
        with sqlite3.connect(path) as after:
            assert "conversation_id" in {row[1] for row in after.execute("PRAGMA table_info(datasets)")}
        auth_routes.record_dataset(1, "new.h5ad", "/x/new.h5ad", 1, "h5ad", conversation_id="3")
        rows = {r["name"]: r["conversation_id"] for r in _dataset_rows(want=2)}
        assert rows == {"old.h5ad": None, "new.h5ad": "3"}
    finally:
        db.reset()


def test_an_unfinished_upload_listed_for_another_browser_names_its_chat(env, monkeypatch):
    """GET /api/tus/unfinished: the "Interrupted in another browser" card says which chat it came from."""
    client, conn, _gw = env
    from aiscientist.gateway import tus_upload
    monkeypatch.setattr(tus_upload, "_HEAD_BYTES", 4)        # a 4-byte "first chunk" keeps the file small
    made = client.post("/api/tus", headers={"Tus-Resumable": "1.0.0", "Upload-Length": "12",
                                            "Upload-Metadata": _metadata(connection_id=conn.id, filename="big.bam",
                                                                         last_modified=1_760_400_000_000,
                                                                         conversation_id="42")})
    url = made.headers["location"]
    first = b"0123"
    sent = client.patch(url, content=first, headers={
        "Tus-Resumable": "1.0.0", "Content-Type": "application/offset+octet-stream", "Upload-Offset": "0",
        "Upload-Checksum": "sha256 " + base64.b64encode(hashlib.sha256(first).digest()).decode()})
    assert sent.status_code == 204, sent.text
    listed = client.get("/api/tus/unfinished", params={"connection_id": conn.id}).json()
    assert [(f["url"], f["conversation_id"]) for f in listed["files"]] == [(url, "42")]
