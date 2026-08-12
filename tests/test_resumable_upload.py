"""Mock-mode test for resumable (chunked) dataset upload — no cluster, no SSH.

Drives the real gateway app's ``/api/upload/chunk`` + ``/api/upload/status`` against
an in-process mock ``Connection`` (its workspace is a tmp dir on the test host),
simulating an interrupted upload that RESUMES from the server's byte offset instead
of restarting. Mirrors the offline philosophy of the other gateway tests — no
network, no paramiko.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
def client_conn(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    # keep any auth-startup DB off the repo (the upload path needs no login)
    monkeypatch.setenv("BIOAGENT_DATABASE_URL", f"sqlite:///{(tmp_path / 'u.db').as_posix()}")
    from fastapi.testclient import TestClient

    from bioagent.gateway.app import CONNECTIONS, Connection, app
    from bioagent.gateway.settings import HPCSettings

    loop = asyncio.new_event_loop()
    conn = Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "ws"          # uploads land here, not the real runs dir
    CONNECTIONS[conn.id] = conn
    try:
        yield TestClient(app), conn            # no `with`: skip lifespan/startup (no DB needed)
    finally:
        CONNECTIONS.pop(conn.id, None)
        loop.close()


def _chunk(client, conn_id, upload_id, name, offset, data, done):
    return client.post(
        "/api/upload/chunk",
        data={"connection_id": conn_id, "upload_id": upload_id, "name": name,
              "offset": str(offset), "done": "true" if done else "false"},
        files={"chunk": ("chunk", data, "application/octet-stream")},
    )


def test_chunked_upload_resumes_after_interruption(client_conn):
    client, conn = client_conn
    uid, name = "upload123", "big.csv"

    # nothing uploaded yet
    st = client.get("/api/upload/status", params={"connection_id": conn.id, "upload_id": uid})
    assert st.status_code == 200 and st.json()["received"] == 0

    # send the first chunk, then "lose the connection" — a partial .part is on disk
    r1 = _chunk(client, conn.id, uid, name, 0, b"hello ", done=False)
    assert r1.status_code == 200 and r1.json() == {"status": "partial", "received": 6}

    # the browser comes back and asks where it left off — resumes from 6, not 0
    st = client.get("/api/upload/status", params={"connection_id": conn.id, "upload_id": uid})
    assert st.json()["received"] == 6

    # resume from offset 6 and finalize
    body = _chunk(client, conn.id, uid, name, 6, b"world", done=True).json()
    assert body["status"] == "uploaded" and body["name"] == "big.csv" and body["size"] == 11

    # the assembled file is byte-correct and lives under the per-user uploads dir
    dest = conn.workspace / "uploads" / "big.csv"
    assert dest.read_bytes() == b"hello world"
    # the .part scratch file is gone after finalize
    assert not (conn.workspace / "uploads" / ".parts" / f"{uid}.part").exists()


def test_offset_mismatch_returns_409_with_true_offset(client_conn):
    client, conn = client_conn
    uid, name = "u2", "x.csv"
    assert _chunk(client, conn.id, uid, name, 0, b"abc", done=False).json()["received"] == 3
    # a duplicate/old chunk at the wrong offset is rejected with the real offset so the
    # client can re-sync instead of corrupting the file
    bad = _chunk(client, conn.id, uid, name, 0, b"abc", done=False)
    assert bad.status_code == 409 and bad.json()["received"] == 3


def test_unknown_connection_is_404(client_conn):
    client, _conn = client_conn
    assert _chunk(client, "no-such-conn", "u", "x.csv", 0, b"x", done=False).status_code == 404
    assert client.get("/api/upload/status",
                      params={"connection_id": "no-such-conn", "upload_id": "u"}).status_code == 404
