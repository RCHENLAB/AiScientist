"""A connection, its workspace and its keys are the account's: nobody else may drive or read them.

A connection id is a uuid4 the browser keeps in localStorage, so on a shared browser it outlives the
login that made it. Before 2026-10-09 every route trusted it as a bearer token, a logged-out
``/api/connect`` named its workspace after the request's ``ucinetid``, and the credential routes
filed keys under the request's ``user``, so an anonymous caller could list someone's saved SSH keys
and log in to HPC3 with one. These pin the guard with accounts on: logged out is a 401, another
account's connection, results or keys a 403 (or simply not theirs), and the refused route does
nothing.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import types

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway import ssh_credentials  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

ALICE = types.SimpleNamespace(id=1, username="alice", role="user")
MALLORY = types.SimpleNamespace(id=2, username="mallory", role="user")
ADMIN = types.SimpleNamespace(id=3, username="boss", role="admin")


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Accounts on; alice owns a ready connection; whoever ``site.who["user"]`` names is calling."""
    runs = tmp_path / "console"
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", runs)
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    who: dict = {"user": ALICE}
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: who["user"])
    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="alice")
    conn.workspace = runs / "alice"
    conn.app_user_id = ALICE.id
    conn.status = "ready"
    gw_app.CONNECTIONS[conn.id] = conn
    try:
        yield types.SimpleNamespace(conn=conn, who=who, runs=runs, state=tmp_path / "state",
                                    client=TestClient(gw_app.app))
    finally:
        gw_app.CONNECTIONS.clear()
        loop.close()


# Every route that takes a connection id, called the way the console calls it.
def _routes(cid: str) -> list[tuple[str, str, dict]]:
    j = {"connection_id": cid}
    tus = {"headers": {"Tus-Resumable": "1.0.0", "Upload-Length": "3",
                       "Upload-Metadata": "connection_id " + base64.b64encode(cid.encode()).decode()}}
    return [
        ("get", f"/api/connections/{cid}", {}),
        ("post", "/api/disconnect", {"json": j}),
        ("post", "/api/auth/duo", {"json": {**j, "response": "123456"}}),
        ("post", "/api/stop-gpu", {"json": j}),
        ("get", f"/api/storage/{cid}", {}),
        ("post", "/api/storage/delete", {"json": {**j, "path": "/dfs3b/x/alice/y"}}),
        ("post", "/api/model", {"json": {**j, "model": "m"}}),
        ("post", "/api/llm-endpoint", {"json": {**j, "credential_id": "abc"}}),
        ("post", "/api/chat/stop", {"json": j}),
        ("post", "/api/chat/inject", {"json": {**j, "text": "use the other dataset"}}),
        ("post", "/api/lab/compact", {"json": j}),
        ("post", "/api/upload", {"data": j, "files": {"file": ("planted.csv", b"a,b\n1,2\n")}}),
        ("post", "/api/tus", tus),
        ("post", "/api/tus/", tus),
        ("post", "/api/tus/find", {"json": {**j, "name": "x.bin", "size": 9 * 2**20, "last_modified": 1,
                                            "head_sha256": "0" * 64}}),
        ("get", "/api/tus/unfinished", {"params": j}),
        ("post", "/api/upload/reserve-folder", {"json": {**j, "name": "data"}}),
        ("post", "/api/upload/register-folder", {"json": {**j, "folder": "data"}}),
        ("post", "/api/server-data/check", {"json": {**j, "path": "/data/x"}}),
        ("post", "/api/server-data/bind", {"json": {**j, "path": "/data/x"}}),
        ("get", "/api/upload/status", {"params": {**j, "upload_id": "u1"}}),
        ("post", "/api/upload/chunk", {"data": {**j, "upload_id": "u1", "name": "x.csv", "offset": "0",
                                                "done": "false"},
                                       "files": {"chunk": ("blob", b"abc")}}),
        ("post", "/api/upload/discard", {"json": {**j, "upload_id": "u1"}}),
        ("get", "/api/upload/staging", {"params": j}),
        ("post", "/api/upload/staging/retry", {"json": {**j, "job_id": "j1"}}),
        ("post", "/api/upload/staging/cancel", {"json": {**j, "job_id": "j1"}}),
        ("post", "/api/upload/staging/dismiss", {"json": {**j, "job_id": "j1"}}),
        ("post", "/api/upload/report", {"json": {**j, "name": "x.csv", "error": "planted"}}),
        ("get", "/api/upload/log", {"params": j}),
        ("post", "/api/dataset/describe", {"json": {**j, "path": "/etc/hosts"}}),
        ("post", "/api/lab", {"json": {**j, "question": "hi"}}),
        ("post", "/api/lab/continue", {"json": j}),
        ("post", "/api/lab/plan", {"json": {**j, "action": "approve"}}),
        ("post", "/api/confirm", {"json": {**j, "approved": True}}),
    ]


_ROUTE_IDS = [f"{m.upper()} {p.split('{')[0]}" for m, p, _ in _routes("{cid}")]


def _call(site, method, path, kw):
    return getattr(site.client, method)(path, **kw)


def _untouched(conn) -> dict:
    """What a refused call must not have changed."""
    return {"status": conn.status, "duo": conn.duo_value, "injections": list(conn.injections),
            "uploads": sorted(p.name for p in (conn.workspace / "uploads").glob("*"))
            if (conn.workspace / "uploads").is_dir() else []}


@pytest.mark.parametrize("route", range(len(_ROUTE_IDS)), ids=_ROUTE_IDS)
def test_another_account_cannot_drive_the_connection(site, route):
    method, path, kw = _routes(site.conn.id)[route]
    before = _untouched(site.conn)
    site.who["user"] = MALLORY
    r = _call(site, method, path, kw)
    assert r.status_code == 403, (path, r.status_code, r.text)
    assert "another account" in r.json()["error"]
    assert _untouched(site.conn) == before


@pytest.mark.parametrize("route", range(len(_ROUTE_IDS)), ids=_ROUTE_IDS)
def test_a_logged_out_caller_cannot_drive_the_connection(site, route):
    method, path, kw = _routes(site.conn.id)[route]
    before = _untouched(site.conn)
    site.who["user"] = None
    r = _call(site, method, path, kw)
    assert r.status_code == 401, (path, r.status_code, r.text)
    assert _untouched(site.conn) == before


def test_a_connection_opened_logged_out_belongs_to_nobody(site):
    """Strict: one made before this guard (or with accounts off) names no account, so no account
    may drive it — not even the one whose name its workspace happens to carry."""
    site.conn.app_user_id = None
    for method, path, kw in _routes(site.conn.id)[:3]:
        assert _call(site, method, path, kw).status_code == 403


def test_the_owner_still_gets_through(site):
    c = site.client
    assert c.get(f"/api/connections/{site.conn.id}").json()["id"] == site.conn.id
    assert c.post("/api/chat/stop", json={"connection_id": site.conn.id}).json() == {"status": "idle"}
    assert c.post("/api/upload/reserve-folder",
                  json={"connection_id": site.conn.id, "name": "data"}).json()["folder"] == "data"
    assert c.get("/api/upload/status",
                 params={"connection_id": site.conn.id, "upload_id": "u1"}).json()["received"] == 0


def test_an_unknown_connection_is_still_a_404(site):
    assert site.client.post("/api/disconnect", json={"connection_id": "nope"}).status_code == 404


@pytest.mark.parametrize("method", ("head", "patch", "get", "delete"))
@pytest.mark.parametrize("caller,expected", ((MALLORY, 403), (None, 401)))
def test_tus_resource_operations_require_the_upload_owner(site, method, caller, expected):
    cid = site.conn.id
    metadata = "connection_id " + base64.b64encode(cid.encode()).decode()
    made = site.client.post("/api/tus", headers={"Tus-Resumable": "1.0.0",
                                                  "Upload-Length": "3", "Upload-Metadata": metadata})
    assert made.status_code == 201
    path = made.headers["location"]
    before = _untouched(site.conn)
    site.who["user"] = caller
    kw = {"headers": {"Tus-Resumable": "1.0.0"}}
    if method == "patch":
        kw["headers"].update({"Content-Type": "application/offset+octet-stream", "Upload-Offset": "0"})
        kw["content"] = b"abc"
    response = _call(site, method, path, kw)
    assert response.status_code == expected
    assert _untouched(site.conn) == before


def test_a_tus_upload_without_a_live_connection_answers_only_its_owner(site):
    """After a gateway restart no connection exists until the page connects again. The upload's
    owner is told to wait (423, so tus-js-client keeps the upload); anyone else learns nothing."""
    metadata = "connection_id " + base64.b64encode(site.conn.id.encode()).decode()
    made = site.client.post("/api/tus", headers={"Tus-Resumable": "1.0.0",
                                                  "Upload-Length": "3", "Upload-Metadata": metadata})
    assert made.status_code == 201
    gw_app.CONNECTIONS.clear()
    head = {"headers": {"Tus-Resumable": "1.0.0"}}
    assert site.client.head(made.headers["location"], **head).status_code == 423
    site.who["user"] = MALLORY
    assert site.client.head(made.headers["location"], **head).status_code == 404
    site.who["user"] = None
    assert site.client.head(made.headers["location"], **head).status_code == 401


def test_another_accounts_partial_upload_is_never_offered(site, monkeypatch):
    """A browser with nothing stored asks the server for an unfinished upload of its file
    (POST /api/tus/find) and for the list of them (GET /api/tus/unfinished). Only the caller's own
    account's uploads are ever offered or listed."""
    import hashlib
    from aiscientist.gateway import tus_upload
    monkeypatch.setattr(tus_upload, "_HEAD_BYTES", 4)
    data = b"0123456789AB"
    meta = ",".join(f"{k} {base64.b64encode(v.encode()).decode()}" for k, v in
                    (("connection_id", site.conn.id), ("filename", "x.bin"), ("last_modified", "1")))
    made = site.client.post("/api/tus", headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(len(data)),
                                                  "Upload-Metadata": meta})
    url = made.headers["location"]
    assert site.client.patch(url, headers={"Tus-Resumable": "1.0.0", "Upload-Offset": "0",
                                           "Content-Type": "application/offset+octet-stream"},
                             content=data[:4]).status_code == 204
    ask = {"name": "x.bin", "size": len(data), "last_modified": 1,
           "head_sha256": hashlib.sha256(data[:4]).hexdigest()}

    def find(conn):
        r = site.client.post("/api/tus/find", json={"connection_id": conn.id, **ask})
        assert r.status_code == 200, r.text
        listed = site.client.get("/api/tus/unfinished", params={"connection_id": conn.id})
        assert listed.status_code == 200, listed.text
        urls = [f["url"] for f in listed.json()["files"]]
        assert urls == ([r.json()["upload"]["url"]] if r.json()["upload"] else [])   # listed = offered
        return r.json()["upload"]

    assert find(site.conn)["url"] == url                         # the owner, from any browser
    other = gw_app.Connection(HPCSettings(), mock=True, loop=site.conn.loop, username="mallory")
    other.workspace, other.app_user_id, other.status = site.runs / "mallory", MALLORY.id, "ready"
    gw_app.CONNECTIONS[other.id] = other
    site.who["user"] = MALLORY
    assert find(other) is None                                   # her own connection: not alice's file
    # A workspace of the same name under another account id (an account made again): the uploads
    # the earlier account left are not this one's.
    renamed = gw_app.Connection(HPCSettings(), mock=True, loop=site.conn.loop, username="alice")
    renamed.workspace, renamed.app_user_id, renamed.status = site.conn.workspace, 99, "ready"
    gw_app.CONNECTIONS[renamed.id] = renamed
    site.who["user"] = types.SimpleNamespace(id=99, username="alice", role="user")
    assert find(renamed) is None


def test_every_route_that_reads_a_connection_id_is_guarded():
    """Structural: a route added later that looks a connection up must go through the guard."""
    creates = {"/api/connect"}                 # makes connections; pinned in the connect tests below
    # finds the CALLER's own session by account, never one a request names; pinned below
    by_account = {"/api/session"}
    unguarded = []
    for route in gw_app.app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not getattr(route, "path", "").startswith(("/api/", "/ws/")):
            continue
        if route.path in creates or route.path in by_account:
            continue
        src = inspect.getsource(endpoint)
        if "CONNECTIONS" in src or "connection_id" in src:
            if "_owned_conn(" not in src and "_conn_owner_error(" not in src:
                unguarded.append(route.path)
    assert unguarded == []
    tested = {p.split("{")[0] for _, p, _ in _routes("{cid}")}
    for route in gw_app.app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None and "_owned_conn(" in inspect.getsource(endpoint):
            assert route.path.split("{")[0] in tested, f"{route.path} is guarded but not tested here"


# --- the event stream ----------------------------------------------------------------------------

def _ws_refusal(site) -> tuple[dict, int]:
    with site.client.websocket_connect(f"/ws/{site.conn.id}") as ws:
        first = ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    return first, closed.value.code


def test_the_stream_refuses_another_account(site):
    site.conn.emit("info", "ssh_auth", "alice's private event")
    site.who["user"] = MALLORY
    first, code = _ws_refusal(site)
    assert first["type"] == "error" and "another account" in first["message"]
    assert code == 4403                    # tells the console to stop reconnecting


def test_the_stream_refuses_a_logged_out_caller(site):
    site.who["user"] = None
    first, code = _ws_refusal(site)
    assert first["type"] == "error" and code == 4401


def test_the_stream_serves_its_owner(site):
    site.conn.emit("info", "ssh_auth", "alice's own event")
    with site.client.websocket_connect(f"/ws/{site.conn.id}") as ws:
        status = ws.receive_json()
        assert status["type"] == "status" and status["connection"]["id"] == site.conn.id
        assert "alice's own event" in json.dumps(ws.receive_json())


# --- connect -------------------------------------------------------------------------------------

@pytest.fixture
def no_provision(monkeypatch):
    """/api/connect without SSH: records what the provisioning thread would have been handed."""
    seen: list = []

    async def _idle(conn):
        return None

    monkeypatch.setattr(gw_app, "_provision_blocking", lambda conn, req: seen.append((conn, req)))
    monkeypatch.setattr(gw_app, "_monitor_gpu", _idle)
    return seen


def _connect(site, **body):
    return site.client.post("/api/connect", json={"ucinetid": "alice", "auth_method": "ssh_key", **body})


@pytest.mark.parametrize("old_page_field", [{}, {"campus_network_confirmed": False}])
def test_connect_asks_nothing_about_the_callers_network(site, no_provision, old_page_field):
    """The SSH to HPC3 starts on the gateway host, so a real connect needs no "on campus / VPN"
    confirmation; a page loaded before the checkbox was removed still sends the field, unticked."""
    site.who["user"] = MALLORY
    r = _connect(site, mock=False, **old_page_field)
    assert r.status_code == 200 and len(no_provision) == 1


def test_connect_needs_a_login(site, no_provision):
    site.who["user"] = None
    before = set(gw_app.CONNECTIONS)
    r = _connect(site, credential_id="abc")
    assert r.status_code == 401
    assert set(gw_app.CONNECTIONS) == before and no_provision == []


def test_a_connect_belongs_to_the_login_not_the_ucinetid(site, no_provision):
    site.who["user"] = MALLORY
    r = _connect(site, mock=False)          # names alice's UCInetID
    assert r.status_code == 200
    conn = gw_app.CONNECTIONS[r.json()["connection_id"]]
    assert conn.owner == "mallory" and conn.app_user_id == MALLORY.id
    assert conn.workspace == site.runs / "mallory"


def test_mock_mode_is_for_admins(site, no_provision):
    site.who["user"] = MALLORY
    assert _connect(site, mock=True).status_code == 403
    site.who["user"] = ADMIN
    assert _connect(site, mock=True).status_code == 200


def _saved_key(state, owner: str, cred_id: str = "k1", recorded: str | None = None):
    d = state / "ssh_creds" / owner
    d.mkdir(parents=True, exist_ok=True)
    key = d / f"{cred_id}.key"
    key.write_text("PRIVATE KEY")
    (d / "index.json").write_text(json.dumps([{
        "id": cred_id, "host": "hpc3.rcic.uci.edu", "hpc_user": owner,
        "key_path": recorded or str(key), "label": owner, "encrypted": False}]))
    return key


def test_a_typed_key_path_must_be_one_of_your_own_keys(site, no_provision):
    victim = _saved_key(site.state, "alice")
    site.who["user"] = MALLORY
    r = _connect(site, ucinetid="alice", key_path=str(victim))
    assert r.status_code == 403 and no_provision == []
    own = _saved_key(site.state, "mallory")
    assert _connect(site, ucinetid="mallory", key_path=str(own)).status_code == 200
    assert no_provision[-1][1].key_path == str(own.resolve())


def test_a_saved_credential_resolves_under_the_login_only(site, no_provision):
    """The credential id is looked up in the CONNECTION OWNER's store, which is now the login."""
    _saved_key(site.state, "alice", cred_id="alicekey")
    site.who["user"] = MALLORY
    r = _connect(site, ucinetid="alice", credential_id="alicekey")
    assert r.status_code == 200
    conn, _ = no_provision[-1]
    assert conn.owner == "mallory"
    assert ssh_credentials.get_credential(conn.owner, "alicekey") is None


@pytest.mark.parametrize("which, expect", [
    ("own", True), ("own_recorded_elsewhere", True), ("victim", False), ("traversal", False),
    ("missing", False), ("outside", False),
])
def test_own_key_path_admits_only_the_owners_keys(tmp_path, monkeypatch, which, expect):
    state = tmp_path / "state"
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(state))
    own = _saved_key(state, "mallory")
    legacy = tmp_path / "app" / "ssh_creds" / "mallory" / "old.key"      # pre-state-dir layout
    legacy.parent.mkdir(parents=True)
    legacy.write_text("PRIVATE KEY")
    _saved_key(state, "mallory", cred_id="old", recorded=str(legacy))
    (state / "ssh_creds" / "mallory" / "old.key").unlink()                 # only the recorded copy
    victim = _saved_key(state, "alice")
    outside = tmp_path / "id_ed25519"
    outside.write_text("PRIVATE KEY")
    path = {"own": str(own), "own_recorded_elsewhere": str(legacy), "victim": str(victim),
            "traversal": str(state / "ssh_creds" / "mallory" / ".." / "alice" / "k1.key"),
            "missing": str(state / "ssh_creds" / "mallory" / "nope.key"), "outside": str(outside)}[which]
    assert (ssh_credentials.own_key_path("mallory", path) is not None) is expect


# --- saved SSH and API keys ----------------------------------------------------------------------

def test_a_logged_out_caller_cannot_list_or_delete_someone_elses_keys(site):
    _saved_key(site.state, "alice")
    site.who["user"] = None
    c = site.client
    assert c.get("/api/ssh-credentials", params={"user": "alice"}).status_code == 401
    assert c.delete("/api/ssh-credentials/k1", params={"user": "alice"}).status_code == 401
    assert c.get("/api/llm-credentials", params={"user": "alice"}).status_code == 401
    assert c.delete("/api/llm-credentials/x", params={"user": "alice"}).status_code == 401
    assert c.post("/api/llm-credentials", json={"user": "alice", "base_url": "http://x",
                                                "api_key": "k"}).status_code == 401
    assert [k["id"] for k in ssh_credentials.list_credentials("alice")] == ["k1"]


def test_a_logged_in_callers_keys_are_their_own_whatever_user_says(site):
    _saved_key(site.state, "alice")
    site.who["user"] = MALLORY
    c = site.client
    assert c.get("/api/ssh-credentials", params={"user": "alice"}).json()["credentials"] == []
    assert c.delete("/api/ssh-credentials/k1", params={"user": "alice"}).status_code == 404
    assert [k["id"] for k in ssh_credentials.list_credentials("alice")] == ["k1"]


# --- a run's results on the gateway --------------------------------------------------------------

@pytest.fixture
def results(site):
    art = site.runs / "alice" / "run0001" / "artifacts"
    (art / "figures").mkdir(parents=True)
    (art / "figures" / "umap.png").write_bytes(b"\x89PNG-fake")
    (art / "report.md").write_text("# alice's findings\n")
    return site


_RESULT_URLS = ["/api/files/alice/run0001", "/api/file/alice/run0001/report.md",
                "/api/artifacts/alice/run0001/report", "/api/bundle/alice/run0001"]


@pytest.mark.parametrize("url", _RESULT_URLS)
def test_another_accounts_results_are_refused(results, url):
    results.who["user"] = MALLORY
    assert results.client.get(url).status_code == 403
    results.who["user"] = None
    assert results.client.get(url).status_code == 401


@pytest.mark.parametrize("url", _RESULT_URLS)
def test_the_owner_and_an_admin_read_the_results(results, url):
    for user in (ALICE, ADMIN):
        results.who["user"] = user
        assert results.client.get(url).status_code == 200, (user.username, url)


# --- server-side helpers -------------------------------------------------------------------------

def test_the_active_session_lookup_ignores_a_same_named_session_of_another_account(site):
    """Dataset deletion removes HPC3 files over this lookup's SSH session."""
    site.conn.executor = object()
    site.conn.app_user_id = None              # opened logged out under alice's name
    assert gw_app._active_conn_for_user(ALICE) is None
    site.conn.app_user_id = ALICE.id
    assert gw_app._active_conn_for_user(ALICE) is site.conn


def test_cluster_models_never_scan_over_someone_elses_session(site, monkeypatch):
    scanned: list = []
    monkeypatch.setattr(gw_app.cluster_models, "scan_disk", lambda ex, st: scanned.append(1) or {})
    site.conn.mock = False
    site.conn.executor = object()
    site.who["user"] = MALLORY
    r = site.client.get("/api/cluster-models", params={"connection_id": site.conn.id})
    assert r.status_code == 200 and r.json()["scanned"] is False and scanned == []
    site.who["user"] = ALICE
    assert site.client.get("/api/cluster-models",
                           params={"connection_id": site.conn.id}).json()["scanned"] is True


# --- one session per account ------------------------------------------------------------------------


def test_a_second_window_joins_the_accounts_open_session(site, no_provision):
    """Two sessions of one HPC3 user share its worker node, serve job, source tree and Slurm jobs;
    a second browser joins the open one instead of starting another."""
    site.who["user"] = ALICE
    site.conn.mock = False
    site.conn.hpc_login = "alice"
    r = _connect(site, mock=False)
    assert r.status_code == 200 and r.json()["joined"] is True
    assert r.json()["connection_id"] == site.conn.id and no_provision == []
    assert site.client.get("/api/session").json()["connection_id"] == site.conn.id
    site.who["user"] = MALLORY                                  # another account: its own session
    assert site.client.get("/api/session").json()["connection_id"] is None
    r = _connect(site, mock=False)
    assert r.status_code == 200 and "joined" not in r.json() and len(no_provision) == 1


def test_a_connect_for_a_different_kind_of_session_is_refused_not_joined(site, no_provision):
    site.who["user"] = ALICE
    r = _connect(site, mock=False)                              # alice's open session is a mock one
    assert r.status_code == 409 and r.json()["connection_id"] == site.conn.id
    site.conn.mock = False
    site.conn.hpc_login = "alice"
    r = _connect(site, mock=False, ucinetid="alice2")
    assert r.status_code == 409 and "alice" in r.json()["error"]
    assert no_provision == []


def test_a_session_that_ended_is_not_joined(site, no_provision):
    site.who["user"] = ALICE
    site.conn.status = "error"
    r = _connect(site, mock=False)
    assert r.status_code == 200 and r.json()["connection_id"] != site.conn.id and len(no_provision) == 1
