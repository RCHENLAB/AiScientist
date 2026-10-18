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
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("AISCIENTIST_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AISCIENTIST_ADMIN_USER", "root")
    monkeypatch.setenv("AISCIENTIST_ADMIN_PASSWORD", "rootpass1")

    from aiscientist.gateway import auth, auth_routes, db, models  # noqa: F401
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
    from aiscientist.gateway import auth
    h = auth.hash_password("hunter2")
    assert h != "hunter2" and auth.verify_password("hunter2", h)
    assert not auth.verify_password("wrong", h)


def test_session_token_signed_and_tamper_proof():
    from aiscientist.gateway import auth
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
    # Seeding from AISCIENTIST_ADMIN_PASSWORD_HASH means NO plaintext is ever needed.
    # auth_routes imports fastapi and db/models import sqlalchemy — both heavier
    # extras the lightweight CI does not install; skip there (like the app_ctx tests).
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("fastapi")
    import importlib
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'h.db').as_posix()}")
    monkeypatch.setenv("AISCIENTIST_SECRET_KEY", "k")
    from aiscientist.gateway import auth, auth_routes, db, models
    for mod in (db, models, auth, auth_routes):
        importlib.reload(mod)
    db.reset(f"sqlite:///{(tmp_path / 'h.db').as_posix()}")
    db.init_db()
    pw_hash = auth.hash_password("seeded-secret")
    monkeypatch.setenv("AISCIENTIST_ADMIN_USER", "hashadmin")
    monkeypatch.setenv("AISCIENTIST_ADMIN_PASSWORD_HASH", pw_hash)
    monkeypatch.delenv("AISCIENTIST_ADMIN_PASSWORD", raising=False)
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
    from aiscientist.gateway import auth

    monkeypatch.delenv("AISCIENTIST_PUBLIC_HTTPS", raising=False)
    assert auth.secure_cookies() is False
    for on in ("1", "true", "YES"):
        monkeypatch.setenv("AISCIENTIST_PUBLIC_HTTPS", on)
        assert auth.secure_cookies() is True
    monkeypatch.setenv("AISCIENTIST_PUBLIC_HTTPS", "0")
    assert auth.secure_cookies() is False


# --- the session secret lives in a 0600 file in the state dir -----------------
#
# The secret signs every login cookie: whoever can read it can sign a cookie for any account. So it
# is kept in <AISCIENTIST_STATE_DIR>/session_secret (0600, generated on first start), not in the
# environment; AISCIENTIST_SECRET_KEY still wins when set, for older deployments.

_SECRET_ENV = ("AISCIENTIST_SECRET_KEY", "BIOAGENT_SECRET_KEY",
               "AISCIENTIST_SECRET_KEY_FILE", "BIOAGENT_SECRET_KEY_FILE",
               "AISCIENTIST_STATE_DIR", "BIOAGENT_STATE_DIR", "AISCIENTIST_PUBLIC_HTTPS")


@pytest.fixture()
def fresh_auth(tmp_path, monkeypatch):
    """The auth module with no secret configured anywhere and an empty per-process cache."""
    for name in _SECRET_ENV:
        monkeypatch.delenv(name, raising=False)
    from aiscientist.gateway import auth
    importlib.reload(auth)
    yield auth
    importlib.reload(auth)


def _mode(path):
    import stat
    return stat.S_IMODE(path.stat().st_mode)


def test_session_secret_file_is_created_0600_and_reused_across_reloads(fresh_auth, tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(state))
    auth = fresh_auth

    secret = auth.secret_key()
    path = state / "session_secret"
    assert path.read_text().strip() == secret
    assert len(secret) == 64 and int(secret, 16) >= 0          # 32 random bytes, hex
    assert _mode(path) == 0o600
    assert not auth.using_dev_secret() and auth.secret_source() == "file"
    notes = auth.startup_notes()
    assert len(notes) == 1 and "created a new session secret" in notes[0]
    token = auth.make_session_token(7)

    importlib.reload(auth)                                       # a restart
    assert auth.secret_key() == secret
    assert auth.read_session_token(token) == 7                   # sessions survive a restart
    assert auth.startup_notes() == [f"[auth] session secret: {path}"]
    assert [p.name for p in state.iterdir()] == ["session_secret"]   # no temp file left behind


def test_secret_in_the_environment_still_wins_and_warns_once(fresh_auth, tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(state))
    monkeypatch.setenv("AISCIENTIST_SECRET_KEY", "from-the-env")
    auth = fresh_auth

    assert auth.secret_key() == "from-the-env" and auth.secret_source() == "env"
    assert not (state / "session_secret").exists()               # nothing written while env wins
    notes = auth.startup_notes()
    assert len(notes) == 1
    assert "AISCIENTIST_SECRET_KEY is set in the environment" in notes[0]
    assert "session_secret" in notes[0]


def test_a_cookie_signed_with_one_secret_is_refused_under_another(app_ctx, tmp_path, monkeypatch):
    client, auth, _routes, _db = app_ctx
    monkeypatch.delenv("AISCIENTIST_SECRET_KEY")
    monkeypatch.delenv("BIOAGENT_SECRET_KEY", raising=False)
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state-a"))
    assert _login(client, "root", "rootpass1").status_code == 200
    assert client.get("/api/auth/me").json()["authenticated"] is True

    # A new secret (the old file deleted, or a different state dir): the same cookie no longer passes.
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state-b"))
    assert client.get("/api/auth/me").json()["authenticated"] is False
    token = auth.make_session_token(1)
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state-a"))
    assert auth.read_session_token(token) is None
    assert client.get("/api/auth/me").json()["authenticated"] is True   # it was only the secret


def test_secret_key_file_setting_names_the_file(fresh_auth, tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    custom = tmp_path / "secrets" / "aiscientist-session"
    monkeypatch.setenv("AISCIENTIST_SECRET_KEY_FILE", str(custom))
    auth = fresh_auth

    secret = auth.secret_key()
    assert custom.read_text().strip() == secret and _mode(custom) == 0o600
    assert not (tmp_path / "state" / "session_secret").exists()


def test_dev_secret_only_when_no_state_dir_is_configured(fresh_auth, monkeypatch):
    auth = fresh_auth
    assert auth.secret_file() is None and auth.secret_source() == "dev"
    assert auth.using_dev_secret()
    assert "dev secret" in auth.startup_notes()[0] and "SECURITY" not in auth.startup_notes()[0]
    monkeypatch.setenv("AISCIENTIST_PUBLIC_HTTPS", "1")
    notes = auth.startup_notes()
    assert len(notes) == 1 and "SECURITY" in notes[0] and "AISCIENTIST_STATE_DIR" in notes[0]


def test_a_hand_made_secret_file_is_closed_and_an_empty_one_refused(fresh_auth, tmp_path, monkeypatch):
    path = tmp_path / "state" / "session_secret"
    path.parent.mkdir()
    path.write_text("operator-chosen-secret\n")
    path.chmod(0o644)
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(path.parent))
    auth = fresh_auth

    assert auth.secret_key() == "operator-chosen-secret"
    assert _mode(path) == 0o600
    assert not any("SECURITY" in n for n in auth.startup_notes())

    empty = tmp_path / "empty" / "session_secret"
    empty.parent.mkdir()
    empty.write_text("\n")
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(empty.parent))
    with pytest.raises(RuntimeError, match="empty"):
        auth.secret_key()                    # never an empty key, never the dev secret instead


def test_two_processes_creating_the_secret_at_once_share_one(fresh_auth, tmp_path, monkeypatch):
    """The new file is linked into place, which never replaces: the process that loses the race
    reads the winner's secret instead of signing with its own."""
    state = tmp_path / "state"
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(state))
    auth = fresh_auth

    def other_process_got_there_first(src, dst):
        open(dst, "w").write("the-winners-secret\n")
        raise FileExistsError(dst)

    monkeypatch.setattr(auth.os, "link", other_process_got_there_first)
    assert auth.secret_key() == "the-winners-secret"
    assert "created" not in auth.startup_notes()[0]
    assert [p.name for p in state.iterdir()] == ["session_secret"]


# --- runs left open by a dead process --------------------------------------
#
# A run row opens as "running" and is closed by record_run_finish. Nothing closes it when the
# gateway dies mid-run — a deploy restart, a crash, an OOM. Twelve had piled up in prod since
# July, and they are not just untidy: every success rate computed from this table counts them as
# still in flight. (One of them was a run a deploy of mine killed while the user was watching.)

def test_a_run_left_running_by_a_dead_process_is_closed_at_startup(app_ctx):
    client, _auth, routes, _db = app_ctx
    _login(client, "root", "rootpass1")
    uid = client.get("/api/auth/me").json()["user"]["id"]

    routes.record_run_start(uid, "run_orphan", "killed by a restart", plan_mode=False)
    routes.record_run_start(uid, "run_done", "finished normally", plan_mode=False)
    routes.record_run_finish("run_done", "done", None, "all good")

    assert routes.mark_interrupted_runs() == 1          # only the open one
    runs = {r["run_id"]: r for r in client.get("/api/runs").json()["runs"]}
    assert runs["run_orphan"]["status"] == "interrupted"
    assert runs["run_done"]["status"] == "done"         # a closed run is never rewritten


def test_the_sweep_is_idempotent_and_records_why(app_ctx):
    client, _auth, routes, _db = app_ctx
    _login(client, "root", "rootpass1")
    uid = client.get("/api/auth/me").json()["user"]["id"]
    routes.record_run_start(uid, "run_orphan2", "q", plan_mode=False)

    assert routes.mark_interrupted_runs() == 1
    assert routes.mark_interrupted_runs() == 0          # a second start must not re-sweep

    row = next(r for r in client.get("/api/runs").json()["runs"] if r["run_id"] == "run_orphan2")
    assert row["status"] == "interrupted"
    # "interrupted", not "error": nothing went wrong with the analysis, it was cut off, and the
    # two must stay distinguishable when the question is whether the pipeline works.
    assert row["status"] != "error"


def test_an_interrupted_run_keeps_a_summary_it_already_had(app_ctx):
    """The sweep explains itself only where there is nothing to overwrite — a run that managed to
    say something before it died keeps what it said."""
    client, _auth, routes, _db = app_ctx
    _login(client, "root", "rootpass1")
    uid = client.get("/api/auth/me").json()["user"]["id"]
    routes.record_run_start(uid, "run_spoke", "q", plan_mode=False)

    from aiscientist.gateway.db import session_scope
    from aiscientist.gateway.models import Run
    from sqlalchemy import select
    with session_scope() as s:
        s.scalar(select(Run).where(Run.run_id == "run_spoke")).summary = "got to step 4"
        s.commit()

    routes.mark_interrupted_runs()
    row = next(r for r in client.get("/api/runs").json()["runs"] if r["run_id"] == "run_spoke")
    assert row["status"] == "interrupted"
    with session_scope() as s:
        assert s.scalar(select(Run).where(Run.run_id == "run_spoke")).summary == "got to step 4"
