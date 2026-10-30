"""Protecting users' API keys (RCHENLAB/AiScientist issue 16): the hardening tasks.

1. A provider's error text and a run's technical log carry no key and no key-store path.
2. A key can be used for one browser session without being saved.
3. The budget's hard limit is checked between the calls of a running run, not only at its start.
4. A key can be paused (owner, admin, the spike check); a spike pauses it and tells its owner.
5. Every saved key can be deleted at once, and every account emailed (a dry run by default).

Offline: SQLite, stubbed HTTP, no provider, no SMTP.
"""

from __future__ import annotations

import datetime as dt
import importlib
import io
import json
import urllib.error

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")


def _fake_key(*parts: str) -> str:
    """A key-shaped test value, assembled at run time: a literal one would trip the secret scans of the
    PR gate and of the public-mirror snapshot, which is what they are for."""
    return "-".join(("sk",) + parts)


KEY = _fake_key("ant", "api03", "EXAMPLEexampleEXAMPLE0123456789abcdefABCDEF_", "zz")


# --- fixtures -------------------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AISCIENTIST_LLM_KEY_ENCRYPTION", raising=False)
    monkeypatch.delenv("AISCIENTIST_LLM_KEY_MASTER_FILE", raising=False)
    from aiscientist.gateway import llm_credentials
    importlib.reload(llm_credentials)
    return llm_credentials


@pytest.fixture()
def db(tmp_path, monkeypatch, store):
    """SQLite accounts + usage, with alice (id from the table) and a key store."""
    pytest.importorskip("bcrypt")
    url = f"sqlite:///{(tmp_path / 'u.db').as_posix()}"
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", url)
    monkeypatch.setenv("AISCIENTIST_SECRET_KEY", "test-secret-key")
    from aiscientist.gateway import auth, auth_routes, db as gw_db, key_guard, llm_usage, models
    importlib.reload(gw_db)
    importlib.reload(models)
    importlib.reload(auth)
    importlib.reload(auth_routes)
    importlib.reload(llm_usage)
    importlib.reload(key_guard)
    gw_db.reset(url)
    gw_db.init_db()
    with gw_db.session_scope() as s:
        user = models.User(username="alice", email="alice@uci.edu", password_hash="x", role="user")
        s.add(user)
        s.commit()
        uid = user.id
    return {"db": gw_db, "models": models, "routes": auth_routes, "usage": llm_usage,
            "guard": key_guard, "uid": uid}


def _cred(store, owner="alice", **kw):
    body = dict(provider="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-5-5",
                api_key=KEY, label="My Claude")
    body.update(kw)
    return store.create(owner, **body)


def _record(ctx, *, fp, when=None, usd=0.01, n=1):
    """``n`` recorded calls on the key ``fp``, each priced ``usd`` (the provider's figure)."""
    with ctx["db"].session_scope() as s:
        for _ in range(n):
            row = ctx["models"].LlmCall(user_id=ctx["uid"], role="reason", model="claude-sonnet-5-5",
                                        endpoint="x", remote=True, provider="anthropic", cost_usd=usd,
                                        est_cost_usd=usd, status="ok", key_fp=fp)
            if when is not None:
                row.created_at = when
            s.add(row)
        s.commit()


def _all_bytes(root) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


# --- 1. errors and the run log carry no key -------------------------------------------------------


def test_scrub_removes_the_key_its_pieces_and_anything_key_shaped():
    from aiscientist.gateway.errors import scrub_secrets
    assert KEY not in scrub_secrets(f"401: bad key {KEY}", KEY)
    # A provider that prints the first characters of the key it got.
    out = scrub_secrets(f"Incorrect API key provided: {KEY[:20]}****", KEY)
    assert KEY[:20] not in out and "<key>" in out
    # Key-shaped text that is not our key (a header copied verbatim, a masked key, a form field).
    for text in ("Authorization: Bearer abcdefgh12345678", "sk-proj-********abcd and more",
                 'api_key' + '="0123456789abcdef"'):
        assert "<key>" in scrub_secrets(text)
    # Ordinary words survive.
    assert scrub_secrets("model not found: claude-sonnet-5-5", KEY) == "model not found: claude-sonnet-5-5"


def _http_error(body: str, code: int = 401):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(body.encode()))
    return boom


@pytest.mark.parametrize("call", ["complete_ex", "chat_tools", "chat_tools_stream"])
def test_an_openai_compatible_error_echoing_the_key_is_scrubbed(monkeypatch, call):
    """A gateway that copies the Authorization header into its 401 body: the key must not reach
    the error message or its detail (which goes to the run log and the browser)."""
    from aiscientist.gateway import vllm_client
    from aiscientist.gateway.errors import GatewayError, error_detail
    monkeypatch.setattr(vllm_client.urllib.request, "urlopen",
                        _http_error(json.dumps({"error": {"message": f"invalid key {KEY}",
                                                          "headers": {"Authorization": f"Bearer {KEY}"}}})))
    kw = dict(base_url="https://api.example.com/v1", api_key=KEY)
    with pytest.raises(GatewayError) as info:
        if call == "complete_ex":
            vllm_client.complete_ex(0, "m", [{"role": "user", "content": "x"}], **kw)
        elif call == "chat_tools":
            vllm_client.chat_tools(0, "m", [{"role": "user", "content": "x"}], [], **kw)
        else:
            list(vllm_client.chat_tools_stream(0, "m", [{"role": "user", "content": "x"}], [], **kw))
    dumped = json.dumps(error_detail(info.value), default=str) + str(info.value) + str(info.value.detail)
    assert KEY not in dumped and KEY[:16] not in dumped
    assert "<key>" in str(info.value)


def test_an_error_inside_a_reply_body_is_scrubbed(monkeypatch):
    """chat_tools: a 200 whose body is an ``error`` object that names the key."""
    from aiscientist.gateway import vllm_client
    from aiscientist.gateway.errors import GatewayError

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(vllm_client.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(json.dumps({"error": f"key {KEY} revoked"}).encode()))
    with pytest.raises(GatewayError) as info:
        vllm_client.chat_tools(0, "m", [], [], base_url="https://x/v1", api_key=KEY)
    assert KEY not in str(info.value) + str(info.value.detail)


def test_the_run_log_names_no_key_store_path_and_no_key(store, tmp_path):
    from aiscientist.gateway import app as gw_app
    from aiscientist.gateway import ssh_credentials
    llm_store = store.private_paths()[0]
    ssh_store = ssh_credentials.private_paths()[0]
    events = [
        {"created_at": "2026-10-29T10:00:00", "level": "warning", "stage": "ssh_transfer",
         "message": f'The OpenSSH client could not copy a.h5ad (Load key "{ssh_store}/alice/0123abcd.key": '
                    "bad permissions); continuing with paramiko."},
        {"created_at": "2026-10-29T10:00:01", "level": "error", "stage": "llm",
         "message": "could not read the key", "detail": {"path": f"{llm_store}/alice/abc.key",
                                                          "master": str(store.private_paths()[1]),
                                                          "echo": f"Bearer {KEY}"}},
    ]
    text = gw_app._event_log_text(events)
    assert str(ssh_store) not in text and str(llm_store) not in text
    assert "0123abcd.key" not in text and "abc.key" not in text and "llm_key.master" not in text
    assert "<key store>" in text
    assert KEY not in text
    assert "could not copy a.h5ad" in text, "the log still says what happened"


# --- 2. a key for this browser session only -------------------------------------------------------


def test_a_session_key_is_never_written_and_is_listed_only_for_its_browser_session(store, tmp_path):
    pub = store.create_session("alice", session_tag="tab-A", provider="anthropic",
                               base_url="https://api.anthropic.com", model="m", api_key=KEY, label="Temp")
    assert pub["session_only"] is True and pub["id"].startswith("s") and pub["expires_at"]
    assert KEY.encode() not in _all_bytes(tmp_path), "nothing on disk holds the key"
    assert [c["id"] for c in store.list_credentials("alice", session_tag="tab-A")] == [pub["id"]]
    assert store.list_credentials("alice", session_tag="tab-B") == []
    assert store.list_credentials("bob", session_tag="tab-A") == []
    assert store.resolve_secret("alice", pub["id"]) == KEY
    assert store.resolve_secret("bob", pub["id"]) is None, "an id never resolves for another owner"
    row = store.get_credential("alice", pub["id"])
    assert KEY not in json.dumps(row, default=str) and KEY not in repr(store._SESSION)
    assert store.end_session("alice", "tab-A") == 1
    assert store.resolve_secret("alice", pub["id"]) is None


def test_a_session_key_ends_after_its_idle_hours(store, monkeypatch):
    pub = store.create_session("alice", session_tag="t", provider="openai", base_url="https://x/v1",
                               model="m", api_key=KEY)
    monkeypatch.setenv("AISCIENTIST_SESSION_KEY_IDLE_HOURS", "1")
    store._SESSION["alice"][pub["id"]]["_last_used"] -= 2 * 3600
    assert store.get_credential("alice", pub["id"]) is None


def test_the_routes_add_a_session_key_and_logout_ends_it(tmp_path, monkeypatch, store):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from aiscientist.gateway import app as gw_app
    from aiscientist.gateway import llm_providers
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    monkeypatch.setattr(llm_providers, "verify",
                        lambda *a, **k: llm_providers.VerifyResult(True, "ok", "fine", ["m"], verified_model="m"))
    from aiscientist.gateway import auth
    client = TestClient(gw_app.app)
    client.cookies.set(auth.COOKIE_NAME, "browser-A-login-cookie")
    r = client.post("/api/llm-credentials", json={"provider": "openai", "base_url": "https://x/v1",
                                                  "model": "m", "api_key": KEY, "user": "alice",
                                                  "session_only": True})
    assert r.status_code == 200, r.text
    cid = r.json()["credential"]["id"]
    listed = client.get("/api/llm-credentials?user=alice").json()
    assert [c["id"] for c in listed["credentials"]] == [cid] and listed["session_idle_hours"] == 12
    assert KEY.encode() not in _all_bytes(tmp_path)
    other = TestClient(gw_app.app)                       # another browser: not listed there
    other.cookies.set(auth.COOKIE_NAME, "browser-B-login-cookie")
    assert other.get("/api/llm-credentials?user=alice").json()["credentials"] == []
    # Logout ends the browser session's keys through its hook (with the tag of ITS cookie).
    import hashlib
    gw_app._end_browser_session("alice", hashlib.sha256(b"browser-A-login-cookie").hexdigest()[:16])
    assert client.get("/api/llm-credentials?user=alice").json()["credentials"] == []


# --- 3. the budget between the calls of a run -----------------------------------------------------


def _budget(ctx, limit, mode):
    with ctx["db"].session_scope() as s:
        s.merge(ctx["models"].UsageBudget(user_id=ctx["uid"], hard_limit_usd=limit, hard_limit_mode=mode,
                                          reset_day=1, thresholds="50,80,100"))
        s.commit()


def _use(ctx, store, cred, **kw):
    return ctx["guard"].KeyUse(owner="alice", cred_id=cred["id"], fingerprint=cred["key_fingerprint"],
                               label=cred["label"], user_id=ctx["uid"], **kw)


def test_block_mode_stops_a_running_run_once_its_spend_reaches_the_limit(db, store):
    from aiscientist.gateway.errors import GatewayError
    cred = _cred(store)
    _budget(db, 1.00, "block")
    use = _use(db, store, cred)
    use.before_call()                                   # $0 spent: goes
    _record(db, fp=cred["key_fingerprint"], usd=0.60)
    use.after_call()                                    # what the recorder does after each call
    use.before_call()                                   # $0.60: goes
    _record(db, fp=cred["key_fingerprint"], usd=0.60)
    use.after_call()
    with pytest.raises(GatewayError) as info:           # $1.20 >= $1.00: the next call is refused
        use.before_call()
    assert info.value.stage == "budget" and "$1.00" in str(info.value)


def test_confirm_mode_asks_once_per_run_and_stops_on_no(db, store):
    from aiscientist.gateway.errors import GatewayError
    cred = _cred(store)
    _budget(db, 0.10, "confirm")
    _record(db, fp=cred["key_fingerprint"], usd=0.50)
    asked = []
    yes = _use(db, store, cred, confirm=lambda s, d: asked.append(s) or True)
    yes.before_call()
    yes.after_call()
    yes.before_call()
    assert len(asked) == 1 and "Continue this run" in asked[0]
    no = _use(db, store, cred, confirm=lambda s, d: False)
    with pytest.raises(GatewayError):
        no.before_call()
    # Accepted at the start of the run (budget_ack): not asked again mid-run.
    ack = _use(db, store, cred, confirm=lambda s, d: pytest.fail("asked again"), budget_ack=True)
    ack.before_call()


def test_a_run_on_a_saved_key_stops_between_two_calls(db, store, monkeypatch):
    """Through _lab_llm: the first call goes, the spend it records crosses the limit, the next is
    refused before anything is sent."""
    from aiscientist.gateway import app as gw_app
    from aiscientist.gateway import anthropic_client
    from aiscientist.gateway.errors import GatewayError
    cred = _cred(store)
    _budget(db, 0.05, "block")
    sent = []
    usage = {"prompt_tokens": 100_000, "completion_tokens": 10_000, "total_tokens": 110_000,
             "input_tokens": 100_000, "model": "claude-sonnet-5-5"}
    monkeypatch.setattr(anthropic_client, "complete_ex", lambda *a, **k: sent.append(1) or ("x", dict(usage)))

    class _Conn:
        owner, selected_model, tunnel_port, mock, app_user_id = "alice", "q", None, False, db["uid"]
        llm_choice = {"credential_id": cred["id"]}
        def emit(self, *a, **k): pass

    llm = gw_app._lab_llm(_Conn())
    llm.complete_fn([{"role": "user", "content": "plan"}])
    with pytest.raises(GatewayError) as info:
        llm.complete_fn([{"role": "user", "content": "again"}])
    assert info.value.stage == "budget" and len(sent) == 1


# --- 4. pause, spikes, telling the owner ----------------------------------------------------------


def test_a_paused_key_is_refused_at_the_next_call_and_only_an_admin_lifts_an_admin_pause(db, store):
    from aiscientist.gateway.errors import GatewayError
    cred = _cred(store)
    use = _use(db, store, cred)
    use.before_call()
    store.set_paused("alice", cred["id"], True, by="owner", reason="mine")
    with pytest.raises(GatewayError) as info:
        use.before_call()
    assert info.value.stage == "llm_key_paused" and "paused by you" in str(info.value)
    store.set_paused("alice", cred["id"], False, by="owner")
    use.before_call()
    store.set_paused("alice", cred["id"], True, by="admin:root", reason="investigating")
    with pytest.raises(GatewayError):
        store.set_paused("alice", cred["id"], False, by="owner")
    assert store.get_public("alice", cred["id"])["paused_by"] == "admin", "the admin's name is not shown"
    store.set_paused("alice", cred["id"], False, by="admin:root")
    use.before_call()


def test_a_spike_pauses_the_key_and_tells_its_owner(db, store, monkeypatch):
    cred = _cred(store)
    fp = cred["key_fingerprint"]
    now = dt.datetime.now(dt.timezone.utc)
    _record(db, fp=fp, when=now - dt.timedelta(days=3), n=40, usd=0.01)   # its usual busiest hour
    _record(db, fp=fp, when=now - dt.timedelta(minutes=10), n=1100, usd=0.001)
    pushed, mailed = [], []
    db["guard"].install(notify=lambda owner, payload: pushed.append((owner, payload)))
    from aiscientist.gateway import email_send
    monkeypatch.setattr(email_send, "send_email", lambda to, subj, body: mailed.append((to, subj, body)) or True)
    spike = db["guard"].check_spike(_use(db, store, cred), force=True)
    assert spike and spike["calls"] == 1100 and spike["baseline"]["calls"] == 40
    row = store.get_public("alice", cred["id"])
    assert row["paused_at"] and row["paused_by"] == "auto" and "1100 calls" in row["pause_reason"]
    assert pushed and pushed[0][0] == "alice" and pushed[0][1]["type"] == "key_paused"
    assert mailed and mailed[0][0] == "alice@uci.edu" and "paused" in mailed[0][1]
    assert KEY not in mailed[0][2] and KEY[-4:] not in mailed[0][2]


def test_a_heavy_but_usual_key_and_a_capped_scan_are_not_spikes(db, store):
    cred = _cred(store)
    fp = cred["key_fingerprint"]
    now = dt.datetime.now(dt.timezone.utc)
    _record(db, fp=fp, when=now - dt.timedelta(minutes=10), n=1100, usd=0.001)
    # A corpus scan announced its cap: its calls are its allowance.
    scan = _use(db, store, cred, allowance_usd=20, allowance_calls=20000)
    assert db["guard"].check_spike(scan, force=True) is None
    scan.close()
    # The same hour against a history that was already this busy.
    _record(db, fp=fp, when=now - dt.timedelta(days=2), n=900, usd=0.001)
    assert db["guard"].check_spike(_use(db, store, cred), force=True) is None
    assert not store.get_public("alice", cred["id"])["paused_at"]


def test_owner_and_admin_pause_routes(db, store, monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from aiscientist.gateway import app as gw_app
    from aiscientist.gateway import email_send
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    mailed = []
    monkeypatch.setattr(email_send, "send_email", lambda to, s, b: mailed.append(to) or True)
    cred = _cred(store)
    client = TestClient(gw_app.app)
    assert client.post(f"/api/llm-credentials/{cred['id']}/pause?user=alice").json()["credential"]["paused_by"] == "owner"
    assert client.post(f"/api/llm-credentials/{cred['id']}/resume?user=alice").json()["credential"]["paused_at"] is None

    class _Admin:
        username, role = "root", "admin"
    monkeypatch.setattr(gw_app.auth_routes, "require_admin", lambda request: _Admin())
    keys = client.get("/api/admin/llm-keys").json()["keys"]
    assert [(k["owner"], k["id"]) for k in keys] == [("alice", cred["id"])]
    assert KEY not in json.dumps(keys) and "key_path" not in keys[0]
    r = client.post("/api/admin/llm-keys/pause", json={"owner": "alice", "credential_id": cred["id"],
                                                       "reason": "unusual traffic"}).json()
    assert r["credential"]["paused_by"] == "admin" and mailed == ["alice@uci.edu"]
    assert client.post(f"/api/llm-credentials/{cred['id']}/resume?user=alice").status_code == 403
    assert client.post("/api/admin/llm-keys/resume", json={"owner": "alice", "credential_id": cred["id"]}).status_code == 200


# --- 5. incident readiness -------------------------------------------------------------------------


def test_purge_deletes_every_saved_key_and_revokes_those_a_run_holds(db, store, tmp_path):
    from aiscientist.gateway.errors import GatewayError
    a = _cred(store, "alice")
    _cred(store, "bob", api_key=_fake_key("bob", "0123456789abcdef"))
    s = store.create_session("carol", session_tag="t", provider="openai", base_url="https://x/v1",
                             model="m", api_key=_fake_key("carol", "0123456789abcdef"))
    running = _use(db, store, a)
    running.before_call()
    done = store.purge_all()
    assert done["credentials"] == 2 and done["owners"] == 2 and done["session_keys"] == 1
    assert done["left"] == {"owners": 0, "key_files": 0}
    assert store.list_credentials("alice") == [] and store.resolve_secret("carol", s["id"]) is None
    with pytest.raises(GatewayError) as info:
        running.before_call()
    assert info.value.stage == "llm_key_revoked"
    # A key added after the purge works.
    fresh = _cred(store, "alice", api_key=_fake_key("new", "0123456789abcdef"))
    _use(db, store, fresh).before_call()


def test_the_purge_command_needs_a_state_dir_and_a_confirmation(store, tmp_path, monkeypatch, capsys):
    from aiscientist.gateway import admin_cli
    _cred(store)
    state = tmp_path / "state"
    monkeypatch.delenv("AISCIENTIST_STATE_DIR")
    monkeypatch.delenv("BIOAGENT_STATE_DIR", raising=False)     # the legacy alias would win it back
    assert admin_cli.main(["purge-llm-keys", "--dry-run"]) == 2          # no state dir: refuses
    assert admin_cli.main(["purge-llm-keys", "--state-dir", str(state), "--dry-run"]) == 0
    assert store.list_credentials("alice"), "a dry run removes nothing"
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    assert admin_cli.main(["purge-llm-keys", "--state-dir", str(state)]) == 1
    assert store.list_credentials("alice")
    monkeypatch.setattr("builtins.input", lambda prompt="": "delete all keys")
    assert admin_cli.main(["purge-llm-keys", "--state-dir", str(state)]) == 0
    assert store.list_credentials("alice") == []
    assert "Removed 1 saved key(s)" in capsys.readouterr().out


def test_email_users_is_a_dry_run_unless_sent_and_writes_to_each_account_alone(db, monkeypatch, capsys):
    from aiscientist.gateway import admin_cli, email_send
    with db["db"].session_scope() as s:
        s.add(db["models"].User(username="bob", email="bob@uci.edu", password_hash="x"))
        s.add(db["models"].User(username="nomail", email=None, password_hash="x"))
        s.add(db["models"].User(username="gone", email="gone@uci.edu", password_hash="x", is_active=False))
        s.commit()
    sent = []
    monkeypatch.setattr(email_send, "send_email", lambda to, subj, body: sent.append((to, subj)) or True)
    users = db["routes"].all_users()
    args = admin_cli.argparse.Namespace(template="key-incident", subject=None, body_file=None, role=None,
                                        send=False, yes=True, pause=0)
    assert admin_cli._email_users(args, users) == 0 and sent == []
    assert "Dry run" in capsys.readouterr().out
    args.send = True
    assert admin_cli._email_users(args, users) == 0
    assert sorted(t for t, _ in sent) == ["alice@uci.edu", "bob@uci.edu"], "one message per active account"
    assert all(subj == "AiScientist: rotate your API key" for _, subj in sent)


def test_logout_hands_its_browser_sessions_tag_to_the_session_hooks(monkeypatch):
    import hashlib

    from aiscientist.gateway import auth, auth_routes
    user = type("U", (), {"id": 7, "username": "alice"})()
    monkeypatch.setattr(auth_routes, "current_user", lambda request: user)
    got = []
    monkeypatch.setattr(auth_routes, "SESSION_END_HOOKS", [lambda name, tag: got.append((name, tag))])

    class Req:
        cookies = {auth.COOKIE_NAME: "login-cookie-1"}

    class Resp:
        def delete_cookie(self, *a, **k):
            pass
    assert auth_routes.logout(Req(), Resp()) == {"status": "ok"}
    assert got == [("alice", hashlib.sha256(b"login-cookie-1").hexdigest()[:16])]


def test_a_paused_key_cannot_be_tested(store, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from aiscientist.gateway import app as gw_app
    from aiscientist.gateway import llm_providers
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    called = []
    monkeypatch.setattr(llm_providers, "verify", lambda *a, **k: called.append(1))
    cred = _cred(store)
    store.set_paused("alice", cred["id"], True, by="owner")
    r = TestClient(gw_app.app).post(f"/api/llm-credentials/{cred['id']}/verify?user=alice")
    assert r.status_code == 409 and r.json()["cause"] == "paused" and called == []
