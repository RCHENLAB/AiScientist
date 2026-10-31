"""Offline tests for server-side chat history (conversations + messages) — SQLite.

Covers create/list/get/rename/delete of conversations, appending messages (with
ordering + on-disk-artifact refs in ``meta``), the auth gate, per-user scoping, and
cross-user ownership (you can't read another user's chat by guessing its id). All
local, no network — mirrors ``test_auth_accounts.py``.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def app_ctx(tmp_path, monkeypatch):
    """Fresh SQLite DB + a bootstrapped admin + a TestClient mounting the auth router."""
    pytest.importorskip("httpx")
    url = f"sqlite:///{(tmp_path / 'chat.db').as_posix()}"
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", url)
    monkeypatch.setenv("AISCIENTIST_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AISCIENTIST_ADMIN_USER", "root")
    monkeypatch.setenv("AISCIENTIST_ADMIN_PASSWORD", "rootpass1")

    from aiscientist.gateway import auth, auth_routes, db, models  # noqa: F401
    importlib.reload(db)
    importlib.reload(models)
    from aiscientist.gateway import metadata_store
    importlib.reload(metadata_store)
    importlib.reload(auth)
    importlib.reload(auth_routes)
    db.reset(url)
    db.init_db()
    auth_routes.ensure_bootstrap_admin()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(auth_routes.router)
    return TestClient(app)


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_anonymous_cannot_touch_history(app_ctx):
    client = app_ctx
    assert client.get("/api/conversations").status_code == 401
    assert client.post("/api/conversations", json={"title": "x"}).status_code == 401
    assert client.post("/api/conversations/1/messages",
                       json={"role": "user", "content": "hi"}).status_code == 401


def test_create_append_and_read_back_in_order(app_ctx):
    client = app_ctx
    _login(client, "root", "rootpass1")
    cid = client.post("/api/conversations", json={"title": "Characterize PBMC"}).json()["conversation"]["id"]

    client.post(f"/api/conversations/{cid}/messages", json={"role": "user", "content": "What cell types?"})
    client.post(f"/api/conversations/{cid}/messages", json={"role": "assistant", "content": "10 clusters found"})
    # an artifacts turn: text empty, blobs referenced by URL in meta (files stay on disk)
    client.post(f"/api/conversations/{cid}/messages", json={
        "role": "assistant", "kind": "artifacts",
        "meta": {"bundleUrl": "/api/bundle/root/run_abc", "items": [{"name": "report.pdf", "url": "/x"}]},
    })

    data = client.get(f"/api/conversations/{cid}").json()
    assert data["conversation"]["title"] == "Characterize PBMC"
    msgs = data["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "assistant"]
    assert [m["seq"] for m in msgs] == [1, 2, 3]          # ordered + monotonic
    assert msgs[2]["kind"] == "artifacts"
    assert msgs[2]["meta"]["bundleUrl"] == "/api/bundle/root/run_abc"   # JSON round-trips


def test_list_sorted_by_recent_activity(app_ctx):
    client = app_ctx
    _login(client, "root", "rootpass1")
    a = client.post("/api/conversations", json={"title": "A"}).json()["conversation"]["id"]
    b = client.post("/api/conversations", json={"title": "B"}).json()["conversation"]["id"]
    # a new message in A bumps it above B
    client.post(f"/api/conversations/{a}/messages", json={"role": "user", "content": "ping"})
    ids = [c["id"] for c in client.get("/api/conversations").json()["conversations"]]
    assert ids[0] == a and b in ids


def test_rename_and_delete(app_ctx):
    client = app_ctx
    _login(client, "root", "rootpass1")
    cid = client.post("/api/conversations", json={}).json()["conversation"]["id"]
    assert client.get(f"/api/conversations/{cid}").json()["conversation"]["title"] == "New chat"
    client.patch(f"/api/conversations/{cid}", json={"title": "Renamed"})
    assert client.get(f"/api/conversations/{cid}").json()["conversation"]["title"] == "Renamed"
    # delete cascades its messages and 404s afterwards
    client.post(f"/api/conversations/{cid}/messages", json={"role": "user", "content": "hi"})
    assert client.delete(f"/api/conversations/{cid}").status_code == 200
    assert client.get(f"/api/conversations/{cid}").status_code == 404


def test_conversation_carries_research_path_context(app_ctx):
    client = app_ctx
    _login(client, "root", "rootpass1")
    cid = client.post("/api/conversations", json={}).json()["conversation"]["id"]

    c = client.get(f"/api/conversations/{cid}").json()["conversation"]
    assert c["preset_key"] is None and c["context_prompt"] is None       # default: no path

    # select a path + store the (edited) methodology guidance
    client.patch(f"/api/conversations/{cid}", json={
        "preset_key": "celltype_annotation", "context_prompt": "QC -> cluster -> annotate."})
    c = client.get(f"/api/conversations/{cid}").json()["conversation"]
    assert c["preset_key"] == "celltype_annotation" and c["context_prompt"] == "QC -> cluster -> annotate."

    # a title-only patch leaves the research-path context intact
    client.patch(f"/api/conversations/{cid}", json={"title": "Retina run"})
    c = client.get(f"/api/conversations/{cid}").json()["conversation"]
    assert c["title"] == "Retina run" and c["preset_key"] == "celltype_annotation"

    # clearing the path
    client.patch(f"/api/conversations/{cid}", json={"preset_key": "", "context_prompt": ""})
    c = client.get(f"/api/conversations/{cid}").json()["conversation"]
    assert c["preset_key"] is None and c["context_prompt"] is None


def test_history_scoped_and_ownership_enforced(app_ctx):
    client = app_ctx
    _login(client, "root", "rootpass1")
    cid = client.post("/api/conversations", json={"title": "root's chat"}).json()["conversation"]["id"]
    client.post("/api/admin/users", json={"username": "mallory", "password": "mallory1"})
    client.post("/api/auth/logout")

    _login(client, "mallory", "mallory1")
    # mallory sees an empty list and CANNOT read/rename/delete/append to root's chat
    assert client.get("/api/conversations").json()["conversations"] == []
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    assert client.patch(f"/api/conversations/{cid}", json={"title": "pwned"}).status_code == 404
    assert client.post(f"/api/conversations/{cid}/messages",
                       json={"role": "user", "content": "x"}).status_code == 404
    assert client.delete(f"/api/conversations/{cid}").status_code == 404


def test_bad_role_rejected(app_ctx):
    client = app_ctx
    _login(client, "root", "rootpass1")
    cid = client.post("/api/conversations", json={}).json()["conversation"]["id"]
    assert client.post(f"/api/conversations/{cid}/messages",
                       json={"role": "robot", "content": "x"}).status_code == 400


def test_an_answer_saved_by_two_windows_is_stored_once(app_ctx):
    """Two windows of one account share a session, and a reconnect replays a finished answer, so
    the same answer may be saved twice; its stream id keeps one copy."""
    client = app_ctx
    _login(client, "root", "rootpass1")
    cid = client.post("/api/conversations", json={"title": "t"}).json()["conversation"]["id"]
    answer = {"role": "assistant", "content": "10 clusters", "kind": "text", "meta": {"stream_id": "s1"}}
    first = client.post(f"/api/conversations/{cid}/messages", json=answer).json()
    second = client.post(f"/api/conversations/{cid}/messages", json=answer).json()
    assert second.get("duplicate") is True and second["message"]["id"] == first["message"]["id"]
    files = {"role": "assistant", "kind": "artifacts", "meta": {"stream_id": "s1", "items": []}}
    client.post(f"/api/conversations/{cid}/messages", json=files)     # the same run's files: kept
    client.post(f"/api/conversations/{cid}/messages", json=files)
    client.post(f"/api/conversations/{cid}/messages", json={**answer, "meta": {"stream_id": "s2"}})
    msgs = client.get(f"/api/conversations/{cid}").json()["messages"]
    assert [(m["kind"], (m.get("meta") or {}).get("stream_id")) for m in msgs] == [
        ("text", "s1"), ("artifacts", "s1"), ("text", "s2")]


def _saved_datasets(count=3):
    from aiscientist.gateway import auth_routes, db, models
    with db.session_scope() as s:
        uid = s.query(models.User).filter_by(username="root").one().id
    return [auth_routes.record_dataset(uid, f"sample{i}.h5ad", f"/data/sample{i}.h5ad", 12, "h5ad")
            for i in range(count)]


def test_chat_datasets_batch_roles_survive_new_signin(app_ctx):
    client = app_ctx
    _login(client, "root", "rootpass1")
    ids = _saved_datasets()
    cid = client.post("/api/conversations", json={}).json()["conversation"]["id"]
    entries = [{"id": ids[0]}, {"id": ids[1], "role": "primary"},
               {"id": ids[2], "role": "reference"}]
    response = client.put(f"/api/conversations/{cid}/datasets", json={"datasets": entries})
    assert response.status_code == 200
    assert [d["id"] for d in response.json()["datasets"]] == ids
    client.post("/api/auth/logout")
    # A fresh client has no browser state or cookies from the original chat.
    from fastapi.testclient import TestClient
    with TestClient(client.app) as other:
        _login(other, "root", "rootpass1")
        data = other.get(f"/api/conversations/{cid}").json()
    assert [(d["id"], d["role"]) for d in data["datasets"]] == [
        (ids[0], None), (ids[1], "primary"), (ids[2], "reference")]
    from aiscientist.gateway import app as gateway
    bound = gateway._select_bound_datasets(gateway.LabRequest(
        connection_id="c", question="q", datasets=data["datasets"]))
    assert bound[0]["path"] == data["datasets"][1]["path"]


def test_chat_dataset_limit_rejection_is_atomic(app_ctx):
    client = app_ctx
    _login(client, "root", "rootpass1")
    ids = _saved_datasets(13)
    cid = client.post("/api/conversations", json={}).json()["conversation"]["id"]
    url = f"/api/conversations/{cid}/datasets"
    assert client.get("/api/datasets").json()["max_bound_datasets"] == 12
    assert client.put(url, json={"datasets": [{"id": i} for i in ids[:12]]}).status_code == 200
    refused = client.put(url, json={"datasets": [{"id": i} for i in ids]})
    assert refused.status_code == 400 and "At most 12" in refused.json()["detail"]
    assert len(client.get(f"/api/conversations/{cid}").json()["datasets"]) == 12
    from aiscientist.gateway import app as gateway
    from fastapi.testclient import TestClient
    response = TestClient(gateway.app).post("/api/lab", json={
        "connection_id": "unused", "question": "q",
        "datasets": [{"path": f"/data/{i}.csv"} for i in ids]})
    assert response.status_code == 400 and "At most 12" in response.json()["detail"]


def test_deleting_dataset_detaches_every_chat(app_ctx):
    client = app_ctx
    _login(client, "root", "rootpass1")
    ids = _saved_datasets(2)
    chats = [client.post("/api/conversations", json={}).json()["conversation"]["id"] for _ in range(2)]
    for cid in chats:
        assert client.put(f"/api/conversations/{cid}/datasets", json={
            "datasets": [{"id": i} for i in ids]}).status_code == 200
    from aiscientist.gateway import auth_routes, db, models
    with db.session_scope() as s:
        uid = s.query(models.User).filter_by(username="root").one().id
    assert auth_routes.delete_dataset_record(uid, ids[0])
    for cid in chats:
        assert [d["id"] for d in client.get(f"/api/conversations/{cid}").json()["datasets"]] == ids[1:]
    # Deleting a chat cascades its attachments without removing the dataset.
    assert client.delete(f"/api/conversations/{chats[0]}").status_code == 200
    assert client.get(f"/api/conversations/{chats[1]}").json()["datasets"][0]["id"] == ids[1]


def test_chat_attachments_enforce_ownership_and_roles(app_ctx):
    client = app_ctx
    _login(client, "root", "rootpass1")
    ids = _saved_datasets(2)
    cid = client.post("/api/conversations", json={}).json()["conversation"]["id"]
    url = f"/api/conversations/{cid}/datasets"
    assert client.put(url, json={"datasets": [{"id": i, "role": "primary"} for i in ids]}).status_code == 400
    assert client.put(url, json={"datasets": [{"id": ids[0], "role": "bad"}]}).status_code == 400
    assert client.put(url, json={"datasets": [{"id": 99999}]}).status_code == 404
    client.post("/api/admin/users", json={"username": "other", "password": "otherpass1"})
    client.post("/api/auth/logout")
    _login(client, "other", "otherpass1")
    assert client.put(url, json={"datasets": []}).status_code == 404
    other_cid = client.post("/api/conversations", json={}).json()["conversation"]["id"]
    assert client.put(f"/api/conversations/{other_cid}/datasets", json={
        "datasets": [{"id": ids[0]}]}).status_code == 404
