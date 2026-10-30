"""Per-client limits on login and self-registration (KrimsonSun/BioAgentPrototype issue 115).

On 2026-10-25 the public site was probed from rotating Tor exit addresses: logins, sign-ups and path
traversal. Nothing got in, but nothing limited the tries either. Pinned here:

- the sliding window itself, and the client address as the proxy saw it (a header the client wrote
  is never trusted);
- failed sign-ins are limited per account and address, per account and per address, with a 429 that
  says when to try again, and a successful sign-in clears that account's count;
- registration starts are limited per address and per email address, verification tries per address;
- expired pending signups are removed;
- ``AISCIENTIST_AUTH_RATE_LIMITS=0`` turns it off.

SQLite, no SMTP (dev mode returns the code), no network.
"""

from __future__ import annotations

import datetime as dt
import importlib
import types

import pytest

pytest.importorskip("bcrypt")
pytest.importorskip("itsdangerous")
pytest.importorskip("sqlalchemy")

from aiscientist.gateway import rate_limit  # noqa: E402


# --- the window and the address --------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_the_window_allows_its_limit_then_says_how_long_to_wait():
    clock = _Clock()
    w = rate_limit.SlidingWindow(3, 60, clock=clock)
    for _ in range(3):
        assert w.retry_after("k") == 0
        w.hit("k")
    assert w.retry_after("k") == pytest.approx(60)
    clock.t += 20
    assert w.retry_after("k") == pytest.approx(40)
    assert w.retry_after("other") == 0                 # keys are separate
    clock.t += 41                                       # the first event has left the window
    assert w.retry_after("k") == 0
    w.clear("k")
    assert w.retry_after("k") == 0


def test_the_window_keeps_its_memory_bounded():
    w = rate_limit.SlidingWindow(1, 60, clock=_Clock(), max_keys=50)
    for i in range(500):
        w.hit(f"k{i}")
    assert len(w._events) <= 50


def _req(peer, forwarded=None):
    headers = {"x-forwarded-for": forwarded} if forwarded is not None else {}
    return types.SimpleNamespace(client=types.SimpleNamespace(host=peer), headers=headers)


@pytest.mark.parametrize("peer,forwarded,expected", [
    ("10.42.0.229", "203.0.113.9", "203.0.113.9"),               # from the proxy: the address it saw
    ("10.42.0.229", "1.2.3.4, 203.0.113.9", "203.0.113.9"),      # a forged first entry is ignored
    ("127.0.0.1", "203.0.113.9:51234", "203.0.113.9"),           # a port is dropped
    ("10.42.0.229", "[2001:db8::7]:443", "2001:db8::7"),
    # Not from a proxy: the header is not trusted. (A real public address: Python counts the
    # documentation ranges 192.0.2/24, 198.51.100/24 and 203.0.113/24 as private.)
    ("128.195.73.177", "203.0.113.9", "128.195.73.177"),
    ("128.195.73.177", None, "128.195.73.177"),
    ("testclient", "203.0.113.9", "testclient"),
])
def test_the_client_address_is_the_one_the_proxy_saw(peer, forwarded, expected):
    assert rate_limit.client_ip(_req(peer, forwarded)) == expected


def test_wait_words():
    assert rate_limit.wait_words(5) == "1 minute"
    assert rate_limit.wait_words(61) == "2 minutes"


# --- the routes -----------------------------------------------------------------------------------

@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("AISCIENTIST_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AISCIENTIST_ADMIN_USER", "root")
    monkeypatch.setenv("AISCIENTIST_ADMIN_PASSWORD", "rootpass1")
    monkeypatch.delenv("AISCIENTIST_SMTP_HOST", raising=False)
    monkeypatch.delenv("AISCIENTIST_ALLOW_SELF_REGISTER", raising=False)
    monkeypatch.delenv("AISCIENTIST_AUTH_RATE_LIMITS", raising=False)

    from aiscientist.gateway import auth, auth_routes, db, email_send, models  # noqa: F401
    importlib.reload(db)
    importlib.reload(models)
    importlib.reload(auth)
    importlib.reload(email_send)
    importlib.reload(auth_routes)                       # fresh counters for every test
    db.reset(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    db.init_db()
    auth_routes.ensure_bootstrap_admin()
    # Each request says which address it comes from (the address logic is pinned above).
    monkeypatch.setattr(auth_routes.rate_limit, "client_ip",
                        lambda request: request.headers.get("x-test-address", "203.0.113.1"))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(auth_routes.router)
    return TestClient(app), auth_routes, db


def _login(client, password, user="root", address="203.0.113.1"):
    return client.post("/api/auth/login", json={"username": user, "password": password},
                       headers={"x-test-address": address})


def test_five_failed_sign_ins_from_one_address_lock_that_account_there(ctx):
    client, _, _ = ctx
    assert [_login(client, "wrong").status_code for _ in range(5)] == [401] * 5
    r = _login(client, "rootpass1")                     # even the right password waits
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert "Too many failed sign-ins. Try again in" in r.json()["detail"]
    # Another address is not locked for this account (it has 5 failures of 20).
    assert _login(client, "rootpass1", address="198.51.100.4").status_code == 200


def test_a_few_typos_then_the_right_password_signs_in_and_clears_the_count(ctx):
    client, _, _ = ctx
    assert [_login(client, "wrong").status_code for _ in range(4)] == [401] * 4
    assert _login(client, "rootpass1").status_code == 200
    # The count was cleared: four more typos are still allowed before a lock.
    assert [_login(client, "wrong").status_code for _ in range(4)] == [401] * 4
    assert _login(client, "rootpass1").status_code == 200


def test_guessing_one_account_from_rotating_addresses_is_limited(ctx):
    client, _, _ = ctx
    codes = [_login(client, "wrong", address=f"198.51.100.{i}").status_code for i in range(20)]
    assert codes == [401] * 20
    assert _login(client, "rootpass1", address="198.51.100.200").status_code == 429


def test_one_address_trying_many_accounts_is_limited(ctx):
    client, _, _ = ctx
    codes = [_login(client, "wrong", user=f"user{i}").status_code for i in range(30)]
    assert codes == [401] * 30
    assert _login(client, "rootpass1").status_code == 429
    assert _login(client, "rootpass1", address="198.51.100.4").status_code == 200


def _start(client, email, username, address="203.0.113.1"):
    return client.post("/api/auth/register/start", headers={"x-test-address": address},
                       json={"username": username, "email": email, "password": "secret12"})


def test_registration_starts_are_limited_per_email_and_per_address(ctx):
    client, _, _ = ctx
    # One inbox: three codes an hour, from any address.
    codes = [_start(client, "victim@uci.edu", f"name{i}", address=f"198.51.100.{i}").status_code for i in range(3)]
    assert codes == [200] * 3
    r = _start(client, "victim@uci.edu", "name9", address="198.51.100.9")
    assert r.status_code == 429 and "Too many registration attempts" in r.json()["detail"]
    # One address: five starts an hour, whatever the email.
    codes = [_start(client, f"p{i}@uci.edu", f"person{i}", address="192.0.2.7").status_code for i in range(5)]
    assert codes == [200] * 5
    assert _start(client, "p9@uci.edu", "person9", address="192.0.2.7").status_code == 429


def test_an_invalid_registration_is_not_counted(ctx):
    client, _, _ = ctx
    assert [_start(client, "x@gmail.com", f"n{i}").status_code for i in range(8)] == [400] * 8
    assert _start(client, "ok@uci.edu", "okname").status_code == 200


def test_verification_tries_are_limited_per_address(ctx):
    client, _, _ = ctx
    tries = [client.post("/api/auth/register/verify", headers={"x-test-address": "192.0.2.8"},
                         json={"email": f"nobody{i}@uci.edu", "code": "000000"}).status_code for i in range(20)]
    assert tries == [404] * 20
    r = client.post("/api/auth/register/verify", headers={"x-test-address": "192.0.2.8"},
                    json={"email": "nobody@uci.edu", "code": "000000"})
    assert r.status_code == 429 and "Too many verification attempts" in r.json()["detail"]


def test_expired_pending_signups_are_removed(ctx):
    client, auth_routes, db = ctx
    from aiscientist.gateway.models import PendingRegistration
    with db.session_scope() as s:
        s.add(PendingRegistration(email="old@uci.edu", username="oldname", password_hash="x", code_hash="x",
                                  attempts=0, expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)))
        s.commit()
    assert _start(client, "new@uci.edu", "newname").status_code == 200
    from sqlalchemy import select
    with db.session_scope() as s:
        emails = sorted(r.email for r in s.scalars(select(PendingRegistration)).all())
    assert emails == ["new@uci.edu"]


def test_the_limits_can_be_turned_off(ctx, monkeypatch):
    client, _, _ = ctx
    monkeypatch.setenv("AISCIENTIST_AUTH_RATE_LIMITS", "0")
    assert [_login(client, "wrong").status_code for _ in range(8)] == [401] * 8
    assert _login(client, "rootpass1").status_code == 200
