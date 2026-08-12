"""Offline tests for the account/auth subsystem (Phase 1) — SQLite, no PostgreSQL.

Covers the DB models, bcrypt hashing, signed-cookie sessions, the login/logout/me
flow, and admin-only user management (create / list / disable / reset-password),
plus the is_active gate and the admin bootstrap. All local, no network.
"""

from __future__ import annotations

import importlib

import pytest

# The whole auth subsystem needs the `auth` extra (bcrypt + itsdangerous + sqlalchemy); the
# offline CI subset installs only the base package, so skip the module cleanly there.
pytest.importorskip("bcrypt")
pytest.importorskip("itsdangerous")
pytest.importorskip("sqlalchemy")


@pytest.fixture()
def app_ctx(tmp_path, monkeypatch):
    """Fresh SQLite DB + a clean engine + a bootstrapped admin, with a TestClient."""
    pytest.importorskip("httpx")
    monkeypatch.setenv("BIOAGENT_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("BIOAGENT_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("BIOAGENT_ADMIN_USER", "root")
    monkeypatch.setenv("BIOAGENT_ADMIN_PASSWORD", "rootpass1")

    from bioagent.gateway import auth, auth_routes, db, models  # noqa: F401
    importlib.reload(db)            # rebuild Base/engine bound to the temp URL
    importlib.reload(models)
    importlib.reload(auth)
    importlib.reload(auth_routes)
    db.reset(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    db.init_db()
    auth_routes.ensure_bootstrap_admin()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(auth_routes.router)
    return TestClient(app), auth, auth_routes, db


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_password_hash_roundtrip_and_rejects_wrong():
    from bioagent.gateway import auth
    h = auth.hash_password("hunter2")
    assert h != "hunter2" and auth.verify_password("hunter2", h)
    assert not auth.verify_password("wrong", h)


def test_session_token_signed_and_tamper_proof():
    from bioagent.gateway import auth
    tok = auth.make_session_token(42)
    assert auth.read_session_token(tok) == 42
    assert auth.read_session_token(tok + "x") is None          # tampered → rejected
    assert auth.read_session_token("") is None


def test_bootstrap_admin_then_login_me_logout(app_ctx):
    client, _auth, _routes, _db = app_ctx
    # bootstrapped admin can log in
    r = _login(client, "root", "rootpass1")
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    me = client.get("/api/auth/me")
    assert me.json()["authenticated"] is True and me.json()["user"]["username"] == "root"
    # wrong password rejected
    assert _login(client, "root", "nope").status_code == 401
    # logout clears the session
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").json()["authenticated"] is False


def test_admin_creates_user_who_can_login(app_ctx):
    client, _auth, _routes, _db = app_ctx
    _login(client, "root", "rootpass1")
    r = client.post("/api/admin/users", json={"username": "alice", "password": "alicepass", "role": "user"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "user"
    # the new user can authenticate
    client.post("/api/auth/logout")
    assert _login(client, "alice", "alicepass").status_code == 200


def test_admin_routes_require_admin(app_ctx):
    client, _auth, _routes, _db = app_ctx
    # anonymous → 401
    assert client.get("/api/admin/users").status_code == 401
    # a non-admin user → 403
    _login(client, "root", "rootpass1")
    client.post("/api/admin/users", json={"username": "bob", "password": "bobpass1", "role": "user"})
    client.post("/api/auth/logout")
    _login(client, "bob", "bobpass1")
    assert client.get("/api/admin/users").status_code == 403


def test_disable_user_blocks_login_and_session(app_ctx):
    client, _auth, _routes, _db = app_ctx
    _login(client, "root", "rootpass1")
    uid = client.post("/api/admin/users", json={"username": "carol", "password": "carolpass"}).json()["user"]["id"]
    # disable carol
    r = client.post(f"/api/admin/users/{uid}/active", params={"active": "false"})
    assert r.status_code == 200 and r.json()["user"]["is_active"] is False
    client.post("/api/auth/logout")
    assert _login(client, "carol", "carolpass").status_code == 401   # disabled → cannot log in


def test_admin_reset_password(app_ctx):
    client, _auth, _routes, _db = app_ctx
    _login(client, "root", "rootpass1")
    uid = client.post("/api/admin/users", json={"username": "dave", "password": "davepass"}).json()["user"]["id"]
    client.post(f"/api/admin/users/{uid}/reset-password", json={"new_password": "newdavepass"})
    client.post("/api/auth/logout")
    assert _login(client, "dave", "davepass").status_code == 401      # old password no longer works
    assert _login(client, "dave", "newdavepass").status_code == 200   # new one does


def test_duplicate_username_rejected(app_ctx):
    client, _auth, _routes, _db = app_ctx
    _login(client, "root", "rootpass1")
    client.post("/api/admin/users", json={"username": "eve", "password": "evepass1"})
    dup = client.post("/api/admin/users", json={"username": "eve", "password": "evepass2"})
    assert dup.status_code == 409


def test_change_my_password_requires_correct_old(app_ctx):
    client, _auth, _routes, _db = app_ctx
    _login(client, "root", "rootpass1")
    assert client.post("/api/account/password",
                       json={"old_password": "wrong", "new_password": "rootpass2"}).status_code == 401
    assert client.post("/api/account/password",
                       json={"old_password": "rootpass1", "new_password": "rootpass2"}).status_code == 200
    client.post("/api/auth/logout")
    assert _login(client, "root", "rootpass1").status_code == 401   # old no longer valid
    assert _login(client, "root", "rootpass2").status_code == 200


def test_datasets_and_runs_history_scoped_to_user(app_ctx):
    client, _auth, routes, _db = app_ctx
    # anonymous can't read history
    assert client.get("/api/datasets").status_code == 401
    assert client.get("/api/runs").status_code == 401

    # find root's id, record some history for them
    _login(client, "root", "rootpass1")
    uid = client.get("/api/auth/me").json()["user"]["id"]
    routes.record_dataset(uid, "pbmc3k.h5ad", "/data/u/pbmc3k.h5ad", 5_855_727, "data")
    routes.record_run_start(uid, "run_abc123", "Characterize PBMC", plan_mode=True)
    routes.record_run_finish("run_abc123", "done", "/data/u/run_abc123/artifacts", "10 clusters found")

    ds = client.get("/api/datasets").json()["datasets"]
    assert len(ds) == 1 and ds[0]["name"] == "pbmc3k.h5ad" and ds[0]["size_bytes"] == 5_855_727
    runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["run_id"] == "run_abc123" and runs[0]["status"] == "done"
    assert runs[0]["plan_mode"] is True

    # a different user sees an empty history (scoping)
    client.post("/api/admin/users", json={"username": "grace", "password": "gracepass"})
    client.post("/api/auth/logout")
    _login(client, "grace", "gracepass")
    assert client.get("/api/datasets").json()["datasets"] == []
    assert client.get("/api/runs").json()["runs"] == []


def test_cli_create_admin_helper_creates_then_promotes(app_ctx):
    client, _auth, routes, _db = app_ctx
    # core helper the interactive CLI calls — no plaintext touches disk
    name, created = routes.create_admin_account("frank", "frankpass1")
    assert name == "frank" and created is True
    assert _login(client, "frank", "frankpass1").json()["user"]["role"] == "admin"
    # calling again on an existing user promotes/repasswords (created=False)
    _name, created2 = routes.create_admin_account("frank", "frankpass2")
    assert created2 is False
    client.post("/api/auth/logout")
    assert _login(client, "frank", "frankpass2").status_code == 200


def test_bootstrap_prefers_password_hash_no_plaintext(tmp_path, monkeypatch):
    # Seeding from BIOAGENT_ADMIN_PASSWORD_HASH means NO plaintext is ever needed.
    # auth_routes imports fastapi and db/models import sqlalchemy — both heavier
    # extras the lightweight CI does not install; skip there (like the app_ctx tests).
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("fastapi")
    import importlib
    monkeypatch.setenv("BIOAGENT_DATABASE_URL", f"sqlite:///{(tmp_path / 'h.db').as_posix()}")
    monkeypatch.setenv("BIOAGENT_SECRET_KEY", "k")
    from bioagent.gateway import auth, auth_routes, db, models
    for mod in (db, models, auth, auth_routes):
        importlib.reload(mod)
    db.reset(f"sqlite:///{(tmp_path / 'h.db').as_posix()}")
    db.init_db()
    pw_hash = auth.hash_password("seeded-secret")
    monkeypatch.setenv("BIOAGENT_ADMIN_USER", "hashadmin")
    monkeypatch.setenv("BIOAGENT_ADMIN_PASSWORD_HASH", pw_hash)
    monkeypatch.delenv("BIOAGENT_ADMIN_PASSWORD", raising=False)
    assert auth_routes.ensure_bootstrap_admin() == "hashadmin"
    assert auth_routes.ensure_bootstrap_admin() is None      # idempotent

    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(auth_routes.router)
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "hashadmin", "password": "seeded-secret"}).status_code == 200


def test_secure_cookies_follows_public_https_env(monkeypatch):
    """Session cookies are marked Secure only when the console is served behind HTTPS."""
    from bioagent.gateway import auth

    monkeypatch.delenv("BIOAGENT_PUBLIC_HTTPS", raising=False)
    assert auth.secure_cookies() is False
    for on in ("1", "true", "YES"):
        monkeypatch.setenv("BIOAGENT_PUBLIC_HTTPS", on)
        assert auth.secure_cookies() is True
    monkeypatch.setenv("BIOAGENT_PUBLIC_HTTPS", "0")
    assert auth.secure_cookies() is False
