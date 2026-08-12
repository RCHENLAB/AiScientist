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
    monkeypatch.setenv("BIOAGENT_DATABASE_URL", url)
    monkeypatch.setenv("BIOAGENT_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("BIOAGENT_ADMIN_USER", "root")
    monkeypatch.setenv("BIOAGENT_ADMIN_PASSWORD", "rootpass1")

    from bioagent.gateway import auth, auth_routes, db, models  # noqa: F401
    importlib.reload(db)
    importlib.reload(models)
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
