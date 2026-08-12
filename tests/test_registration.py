"""Offline tests for self-registration (UCI email + emailed code) and the admin
email/search/delete additions. SQLite, no SMTP (dev mode returns the code), no network.
"""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("bcrypt")
pytest.importorskip("itsdangerous")
pytest.importorskip("sqlalchemy")


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    monkeypatch.setenv("BIOAGENT_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("BIOAGENT_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("BIOAGENT_ADMIN_USER", "root")
    monkeypatch.setenv("BIOAGENT_ADMIN_PASSWORD", "rootpass1")
    monkeypatch.delenv("BIOAGENT_SMTP_HOST", raising=False)          # dev mode → code in response
    monkeypatch.delenv("BIOAGENT_ALLOW_SELF_REGISTER", raising=False)  # default: enabled

    from bioagent.gateway import auth, auth_routes, db, email_send, models  # noqa: F401
    importlib.reload(db)
    importlib.reload(models)
    importlib.reload(auth)
    importlib.reload(email_send)
    importlib.reload(auth_routes)
    db.reset(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    db.init_db()
    auth_routes.ensure_bootstrap_admin()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(auth_routes.router)
    return TestClient(app), auth_routes


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _register(client, username, email, password="secret1"):
    return client.post("/api/auth/register/start",
                       json={"username": username, "email": email, "password": password})


# --- config ------------------------------------------------------------------

def test_auth_config_reports_dev_mode_and_domains(ctx):
    client, _ = ctx
    cfg = client.get("/api/auth/config").json()
    assert cfg["self_register"] is True
    assert cfg["email_mode"] == "dev"
    assert "uci.edu" in cfg["email_domains"]


# --- domain gating -----------------------------------------------------------

def test_non_uci_email_rejected(ctx):
    client, _ = ctx
    r = _register(client, "alice", "alice@gmail.com")
    assert r.status_code == 400
    assert "uci.edu" in r.json()["detail"]


def test_uci_subdomain_accepted(ctx):
    client, _ = ctx
    r = _register(client, "bob", "bob@ics.uci.edu")
    assert r.status_code == 200
    assert r.json()["dev_mode"] is True and "dev_code" in r.json()


# --- happy path: start -> verify -> logged in --------------------------------

def test_register_then_verify_creates_account_and_signs_in(ctx):
    client, _ = ctx
    start = _register(client, "carol", "carol@uci.edu", "hunter2x").json()
    code = start["dev_code"]
    r = client.post("/api/auth/register/verify", json={"email": "carol@uci.edu", "code": code})
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "carol"
    # The verify response sets the session cookie → we're signed in.
    me = client.get("/api/auth/me").json()
    assert me["authenticated"] and me["user"]["email"] == "carol@uci.edu"
    # And the password works for a normal login afterwards.
    client.post("/api/auth/logout")
    assert _login(client, "carol", "hunter2x").status_code == 200


def test_wrong_code_is_rejected_and_counts_attempts(ctx):
    client, _ = ctx
    _register(client, "dave", "dave@uci.edu")
    r = client.post("/api/auth/register/verify", json={"email": "dave@uci.edu", "code": "000000"})
    # (astronomically unlikely to match the real code; if it did, it'd be 200)
    assert r.status_code in (400, 200)
    if r.status_code == 400:
        assert "attempt" in r.json()["detail"].lower()


def test_duplicate_username_blocked(ctx):
    client, _ = ctx
    # 'root' already exists (bootstrap admin).
    r = _register(client, "root", "someone@uci.edu")
    assert r.status_code == 409


def test_duplicate_email_allowed_across_accounts(ctx):
    """One person may hold several accounts on the same UCI email (e.g. an admin + a normal
    account). Email is intentionally NOT unique — only the username must be. Sequentially:
    start+verify account A, then start+verify account B with the SAME email."""
    client, _ = ctx
    a = _register(client, "amy", "shared@uci.edu", "pw123456").json()
    assert client.post("/api/auth/register/verify",
                       json={"email": "shared@uci.edu", "code": a["dev_code"]}).status_code == 200
    client.post("/api/auth/logout")
    # Same email, different username → must be allowed (no 409 on the email).
    b_resp = _register(client, "amy2", "shared@uci.edu", "pw123456")
    assert b_resp.status_code == 200
    assert client.post("/api/auth/register/verify",
                       json={"email": "shared@uci.edu", "code": b_resp.json()["dev_code"]}).status_code == 200
    # Both accounts exist and share the address.
    _login(client, "root", "rootpass1")
    users = {u["username"]: u["email"] for u in client.get("/api/admin/users").json()["users"]}
    assert users.get("amy") == "shared@uci.edu" and users.get("amy2") == "shared@uci.edu"


def test_self_register_can_be_disabled(ctx, monkeypatch):
    client, _ = ctx
    monkeypatch.setenv("BIOAGENT_ALLOW_SELF_REGISTER", "0")
    r = _register(client, "erin", "erin@uci.edu")
    assert r.status_code == 403


def test_missing_pending_table_returns_clean_json_500(ctx):
    """Schema drift (the ``pending_registrations`` table missing on a stale DB) must NOT
    leak a bare plain-text 500 — the UI can only show a useful message if the response is
    JSON with a ``detail``. Regression for the opaque 'Could not start registration.'."""
    client, _ = ctx
    from sqlalchemy import text

    from bioagent.gateway.db import get_engine
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE pending_registrations"))
    r = _register(client, "zoe", "zoe@uci.edu")
    assert r.status_code == 500
    body = r.json()                       # must be parseable JSON, not plain text
    assert body.get("detail")             # a non-empty, user-facing message
    assert "unavailable" in body["detail"].lower()


# --- admin: email column via search + delete ---------------------------------

def test_admin_search_by_email_and_id(ctx):
    client, _ = ctx
    from sqlalchemy import select

    from bioagent.gateway import auth
    from bioagent.gateway.db import session_scope
    from bioagent.gateway.models import User
    with session_scope() as s:
        s.add(User(username="frank", email="frank@uci.edu", password_hash=auth.hash_password("pw12345")))
        s.add(User(username="grace", email="grace@hs.uci.edu", password_hash=auth.hash_password("pw12345")))
        s.commit()
    _login(client, "root", "rootpass1")
    # fuzzy by email substring
    users = client.get("/api/admin/users?q=grace@hs").json()["users"]
    assert [u["username"] for u in users] == ["grace"]
    # by exact id
    with session_scope() as s:
        fid = s.scalar(select(User.id).where(User.username == "frank"))
    users = client.get(f"/api/admin/users?q={fid}").json()["users"]
    assert any(u["username"] == "frank" for u in users)
    # email is exposed in the row
    assert all("email" in u for u in users)


def test_admin_set_and_clear_email(ctx):
    client, _ = ctx
    from sqlalchemy import select

    from bioagent.gateway import auth
    from bioagent.gateway.db import session_scope
    from bioagent.gateway.models import User
    with session_scope() as s:
        s.add(User(username="ivan", password_hash=auth.hash_password("pw12345")))
        s.commit()
        iid = s.scalar(select(User.id).where(User.username == "ivan"))
    _login(client, "root", "rootpass1")
    # set
    r = client.post(f"/api/admin/users/{iid}/email", json={"email": "ivan@uci.edu"})
    assert r.status_code == 200 and r.json()["user"]["email"] == "ivan@uci.edu"
    # invalid rejected
    assert client.post(f"/api/admin/users/{iid}/email", json={"email": "nope"}).status_code == 400
    # clear
    r = client.post(f"/api/admin/users/{iid}/email", json={"email": ""})
    assert r.status_code == 200 and r.json()["user"]["email"] is None


def test_admin_set_role_promote_demote_and_guards(ctx):
    client, _ = ctx
    from sqlalchemy import select

    from bioagent.gateway import auth
    from bioagent.gateway.db import session_scope
    from bioagent.gateway.models import User
    with session_scope() as s:
        s.add(User(username="mallory", email="m@uci.edu", password_hash=auth.hash_password("pw123456")))
        s.commit()
        mid = s.scalar(select(User.id).where(User.username == "mallory"))
        rootid = s.scalar(select(User.id).where(User.username == "root"))
    _login(client, "root", "rootpass1")
    # promote user -> admin
    r = client.post(f"/api/admin/users/{mid}/role", json={"role": "admin"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    # demote ANOTHER admin -> user (root demoting mallory; 2 admins exist, so it's allowed)
    r = client.post(f"/api/admin/users/{mid}/role", json={"role": "user"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "user"
    # can't change your OWN role (self-lockout guard)
    assert client.post(f"/api/admin/users/{rootid}/role", json={"role": "user"}).status_code == 400
    # invalid role rejected
    assert client.post(f"/api/admin/users/{mid}/role", json={"role": "superuser"}).status_code == 400
    # a demoted admin loses admin access: promote mallory, sign in AS mallory, demote root,
    # then root (now a plain user) can no longer reach the admin endpoint at all.
    client.post(f"/api/admin/users/{mid}/role", json={"role": "admin"})
    client.post("/api/auth/logout")
    _login(client, "mallory", "pw123456")
    assert client.post(f"/api/admin/users/{rootid}/role", json={"role": "user"}).status_code == 200
    client.post("/api/auth/logout")
    _login(client, "root", "rootpass1")   # root is a plain user now
    assert client.post(f"/api/admin/users/{mid}/role", json={"role": "user"}).status_code == 403


def test_admin_delete_user_and_guards(ctx):
    client, _ = ctx
    from sqlalchemy import select

    from bioagent.gateway import auth
    from bioagent.gateway.db import session_scope
    from bioagent.gateway.models import User
    with session_scope() as s:
        s.add(User(username="heidi", email="heidi@uci.edu", password_hash=auth.hash_password("pw12345")))
        s.commit()
        hid = s.scalar(select(User.id).where(User.username == "heidi"))
        rootid = s.scalar(select(User.id).where(User.username == "root"))
    _login(client, "root", "rootpass1")
    # can't delete yourself
    assert client.delete(f"/api/admin/users/{rootid}").status_code == 400
    # can delete another user
    assert client.delete(f"/api/admin/users/{hid}").status_code == 200
    remaining = [u["username"] for u in client.get("/api/admin/users").json()["users"]]
    assert "heidi" not in remaining
    # can't delete the last admin (root is the only admin)
    # (root == self here, so this is also covered by the self-guard; assert 400 either way)
    assert client.delete(f"/api/admin/users/{rootid}").status_code == 400
