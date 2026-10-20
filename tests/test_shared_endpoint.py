"""The lab's shared model server chosen as a session's endpoint, the way a saved API key is.

A session on it holds no GPU of its own (the API-session path) and EVERY call goes to the shared
model — planning, critique, the write-up, the Scientist's tool loop and the fast chat. Its address
and key are the keeper's (``gateway/shared_llm.py``): read at call time, never stored, never sent to
the page. When the shared server is not answering the session has nothing to fall back on, so it is
refused up front (connect, switch) and a run says why instead of hanging.

Imports the FastAPI app (needs the gateway extra installed). Skipped cleanly if fastapi is absent.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway import shared_llm, shared_llm_gate, vllm_client  # noqa: E402
from aiscientist.gateway.errors import GatewayError, VLLMNetworkError  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

KEY = "shared-key-example-not-real"


class _Keeper:
    """Enough of the keeper: whether it answers, on which local port, and how it is called."""

    def __init__(self, port=43943, up=True):
        self.port, self.up, self.failures = port, up, []
        self.settings = shared_llm.SharedLLMSettings(enabled=True)

    def binding(self):
        if not self.up:
            return None
        return shared_llm.Binding(port=self.port, model="deepseek-v4-flash", api_key=KEY, effort="low",
                                  think_kwargs={"enable_thinking": True})

    def status(self):
        return {"enabled": True, "state": "up" if self.up else "starting", "model": "deepseek-v4-flash",
                "expires_at": None}

    def note_failure(self, reason):
        self.failures.append(reason)


@pytest.fixture()
def keeper(monkeypatch):
    k = _Keeper()
    monkeypatch.setattr(shared_llm, "_MANAGER", k)
    # A call does not wait for a server that is not answering, unless a test says so; a fresh queue.
    monkeypatch.setenv("AISCIENTIST_SHARED_LLM_DOWN_WAIT_SECONDS", "0")
    monkeypatch.setattr(gw_app, "_SHARED_DOWN_POLL_S", 0.02)
    shared_llm_gate.reset()
    yield k
    shared_llm_gate.reset()


@pytest.fixture()
def calls(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(vllm_client, "complete_ex", lambda port, model, messages, **kw: seen.append(
        {"role": "lab", "port": port, "model": model, **kw}) or ("planned", {}))
    monkeypatch.setattr(vllm_client, "chat_tools", lambda port, model, messages, tools, **kw: seen.append(
        {"role": "sci", "port": port, "model": model, **kw}) or {"content": "", "tool_calls": []})
    monkeypatch.setattr(gw_app, "_record_lab_usage", lambda conn, **f: None)
    return seen


class _Conn:
    selected_model = "RedHatAI/Qwen3.8-27B-INT4"
    tunnel_port = 37219
    mock = False
    owner = "alice"

    def __init__(self, choice=shared_llm.ENDPOINT_ID):
        self.llm_choice = {"credential_id": choice} if choice else {}
        self.said: list = []
        self.pushed: list = []

    def emit(self, *a, **k):
        self.said.append(a)

    def push(self, payload, run=None):
        self.pushed.append(payload)


# --- every call of the session goes to the shared model -------------------------------------------------

def test_every_role_and_the_scientist_go_to_the_shared_model_with_its_key(keeper, calls):
    llm = gw_app._lab_llm(_Conn())
    assert llm.complete_fn([{"role": "user", "content": "plan"}], role="plan") == "planned"
    llm.complete_fn([{"role": "user", "content": "classify"}], role="classify")
    llm.scientist_chat([{"role": "user", "content": "run"}], [])
    assert [c["role"] for c in calls] == ["lab", "lab", "sci"]
    for c in calls:
        assert c["base_url"] == "http://127.0.0.1:43943/v1" and c["api_key"] == KEY
        assert c["model"] == "deepseek-v4-flash" and c["reasoning_effort"] == "low"
    assert calls[0]["think_kwargs"] == {"enable_thinking": True} and calls[0].get("max_tokens") is None
    assert llm.shared_endpoint is True and llm.scientist_remote is False and llm.lab_role_remote is False
    assert llm.context_window == 262144, "the configured window, when the server does not report one"


def test_the_scientist_window_is_what_the_running_server_reports(keeper, calls, monkeypatch):
    """A job submitted before the window was raised keeps its old one until it is renewed: trimming
    to the configured 262,144 would send prompts that server refuses."""
    monkeypatch.setattr(shared_llm, "served_window", lambda bound, timeout=5.0: 131072)
    assert gw_app._lab_llm(_Conn()).context_window == 131072


def test_the_address_is_read_again_at_every_call(keeper, calls):
    llm = gw_app._lab_llm(_Conn())
    keeper.port = 51000                       # the keeper re-opened its tunnel mid-run
    llm.scientist_chat([{"role": "user", "content": "run"}], [])
    assert calls[-1]["base_url"] == "http://127.0.0.1:51000/v1"


def test_a_run_says_why_when_the_shared_model_is_not_answering(keeper, calls):
    keeper.up = False
    llm = gw_app._lab_llm(_Conn())             # binding waits for nothing: it runs on the event loop
    assert llm.model == "deepseek-v4-flash"
    with pytest.raises(GatewayError, match="shared model is not answering"):
        llm.complete_fn([{"role": "user", "content": "plan"}], role="plan")
    assert not calls, "nothing is sent to another model instead"


def test_a_call_waits_while_the_job_is_renewed_and_the_run_is_told(keeper, calls, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_SHARED_LLM_DOWN_WAIT_SECONDS", "5")
    keeper.up = False
    conn = _Conn()
    llm = gw_app._lab_llm(conn)
    threading.Timer(0.15, lambda: setattr(keeper, "up", True)).start()
    assert llm.complete_fn([{"role": "user", "content": "plan"}], role="plan") == "planned"
    labels = [p["text"] for p in conn.pushed if p["type"] == "run_status"]
    assert labels[0].startswith("Waiting for the shared model to answer again") and labels[-1] == ""
    assert sum(1 for a in conn.said if a[1] == "shared_llm") == 1, "logged once, not at every look"


def test_a_dropped_call_is_reported_to_the_keeper_and_asked_once_more(keeper, monkeypatch):
    tries: list = []

    def drop(*a, **k):
        tries.append(1)
        raise VLLMNetworkError("connection reset", stage="vllm")
    monkeypatch.setattr(vllm_client, "chat_tools", drop)
    llm = gw_app._lab_llm(_Conn())
    with pytest.raises(VLLMNetworkError):
        llm.scientist_chat([{"role": "user", "content": "run"}], [])
    assert keeper.failures == ["connection reset", "connection reset"] and len(tries) == 2


def test_a_dropped_call_that_answers_the_second_time_is_not_an_error(keeper, monkeypatch):
    replies = iter([VLLMNetworkError("connection reset", stage="vllm"), ("planned", {})])

    def flaky(*a, **k):
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(vllm_client, "complete_ex", flaky)
    monkeypatch.setattr(gw_app, "_record_lab_usage", lambda conn, **f: None)
    conn = _Conn()
    assert gw_app._lab_llm(conn).complete_fn([{"role": "user", "content": "plan"}], role="plan") == "planned"
    assert keeper.failures == ["connection reset"]
    assert any("asking again" in a[2] for a in conn.said)


def test_an_account_s_calls_wait_beyond_its_limit_and_say_so(keeper, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_SHARED_LLM_PER_USER", "1")
    shared_llm_gate.reset()
    gate = shared_llm_gate.gate()
    gate._poll_s = 0.01
    release, inside, most = threading.Event(), [], []

    def slow(*a, **k):
        inside.append(1)
        most.append(len(inside))
        release.wait(2)
        inside.pop()
        return "planned", {}
    monkeypatch.setattr(vllm_client, "complete_ex", slow)
    monkeypatch.setattr(gw_app, "_record_lab_usage", lambda conn, **f: None)
    conn = _Conn()
    llm = gw_app._lab_llm(conn)
    workers = [threading.Thread(target=llm.complete_fn, args=([{"role": "user", "content": "q"}],),
                                kwargs={"role": "claim_audit"}) for _ in range(2)]
    for w in workers:
        w.start()
    time.sleep(0.15)
    assert gate.snapshot()["in_flight"] == 1 and gate.snapshot()["waiting"] == 1
    release.set()
    for w in workers:
        w.join(3)
    assert max(most) == 1, "never two calls of one account at once with a limit of one"
    labels = [p["text"] for p in conn.pushed if p["type"] == "run_status"]
    assert labels[0] == "Waiting for the shared model: you already have 1 request running, its limit per account"
    assert labels[-1] == ""


def test_a_fast_chat_turn_takes_its_turn_and_reads_the_address_when_it_goes(keeper, monkeypatch):
    seen: list = []

    def stream(port, model, messages, schemas, **kw):
        seen.append((port, model, kw["base_url"], kw["api_key"]))
        yield "content", "hello"
        yield "usage", {"prompt_tokens": 3, "completion_tokens": 1}
    monkeypatch.setattr(vllm_client, "chat_tools_stream", stream)
    rows: list = []
    monkeypatch.setattr(gw_app, "_record_chat_usage", lambda conn, **f: rows.append(f))
    keeper.port = 51001
    fn = gw_app._quickchat_stream_fn(_Conn(), "deepseek-v4-flash", None, None, False, shared=True)
    assert list(fn([{"role": "user", "content": "hi"}], [])) == [("content", "hello")]
    assert seen == [(0, "deepseek-v4-flash", "http://127.0.0.1:51001/v1", KEY)]
    assert rows[0]["shared"] is True and shared_llm_gate.gate().snapshot()["in_flight"] == 0


def test_a_session_on_its_own_gpu_stays_on_its_qwen_for_every_call(keeper, calls):
    llm = gw_app._lab_llm(_Conn(choice=None))   # the cluster GPU: Qwen throughout, shared server or not
    assert llm.shared_endpoint is False and llm.context_window is None
    llm.complete_fn([{"role": "user", "content": "plan"}], role="plan")
    llm.scientist_chat([{"role": "user", "content": "run"}], [])
    assert [(c["port"], c.get("base_url")) for c in calls] == [(37219, None), (37219, None)]


def test_the_report_says_where_every_call_ran(keeper, calls):
    note = gw_app._llm_provenance_note(gw_app._lab_llm(_Conn()))
    assert "tool-calling Scientist" in note and "deepseek-v4-flash" in note and "No prompt left UCI" in note


# --- the session: no GPU, a summary without the key ------------------------------------------------------

def test_the_session_is_provisioned_without_a_gpu(keeper):
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="alice")
    conn.llm_choice = {"credential_id": shared_llm.ENDPOINT_ID}
    assert gw_app._session_credential(conn)["builtin"] == "shared"
    gw_app._provision_api_session(conn)
    assert conn.status == "ready" and conn.selected_model == "deepseek-v4-flash"
    assert conn.llm_info["engine"] == "api"
    summary = conn._llm_endpoint_summary()
    assert summary["kind"] == "shared" and summary["remote"] is False and summary["up"] is True
    assert KEY not in json.dumps(conn.summary(), default=str)


def test_without_a_shared_server_the_choice_resolves_to_nothing(monkeypatch):
    monkeypatch.setattr(shared_llm, "_MANAGER", None)
    assert gw_app._session_credential(_Conn()) is None
    assert shared_llm.endpoint_public() is None


# --- the routes ---------------------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch, keeper):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path))
    from aiscientist.gateway import llm_credentials
    importlib.reload(llm_credentials)
    from fastapi.testclient import TestClient
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    return TestClient(gw_app.app)


@pytest.fixture()
def session(client):
    c = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="alice")
    c.status = "ready"
    gw_app.CONNECTIONS[c.id] = c
    yield c
    gw_app.CONNECTIONS.pop(c.id, None)


def test_the_status_says_how_busy_the_queue_is(client):
    queue = client.get("/api/shared-llm").json()["queue"]
    assert queue["per_user_limit"] == 4 and queue["max_in_flight"] == 12
    assert queue["in_flight"] == 0 and queue["waiting"] == 0


def test_the_pickers_list_the_shared_model_without_its_address_or_key(client):
    shared = client.get("/api/llm-credentials?user=alice").json()["shared"]
    assert shared == {"id": "lab-shared", "label": "Shared model on HPC3", "model": "deepseek-v4-flash",
                      "lab_model": "deepseek-v4-flash", "state": "up", "up": True, "expires_at": None}


def test_a_session_switches_to_the_shared_model_without_a_consent_card(client, session):
    res = client.post("/api/llm-endpoint", json={"connection_id": session.id, "credential_id": "lab-shared"})
    assert res.status_code == 200 and res.json()["endpoint"] == "shared"
    assert session.llm_choice == {"credential_id": "lab-shared"}


def test_switching_to_it_while_it_is_not_answering_is_refused(client, session, keeper):
    keeper.up = False
    res = client.post("/api/llm-endpoint", json={"connection_id": session.id, "credential_id": "lab-shared"})
    assert res.status_code == 503 and "not answering" in res.json()["error"]
    assert session.llm_choice == {}


def test_connecting_on_it_while_it_is_not_answering_is_refused(client, keeper):
    keeper.up = False
    res = client.post("/api/connect", json={"host": "hpc3.rcic.uci.edu", "ucinetid": "alice", "user": "alice",
                                            "auth_method": "password", "password": "x", "mock": True,
                                            "llm_credential_id": "lab-shared"})
    assert res.status_code == 503 and "not answering" in res.json()["error"]


def test_the_shared_endpoint_s_calls_are_recorded_as_not_billed(keeper, monkeypatch):
    """Every endpoint's calls are recorded (Account › Usage); the shared model is the lab's own GPUs."""
    recorded: list[dict] = []
    monkeypatch.setattr(gw_app, "_record_lab_usage", lambda conn, **f: recorded.append(f))
    monkeypatch.setattr(vllm_client, "complete_ex", lambda *a, **k: ("planned", {"completion_tokens": 7}))
    monkeypatch.setattr(vllm_client, "chat_tools", lambda *a, **k: {
        "content": "", "tool_calls": [], "usage": {"prompt_tokens": 11, "completion_tokens": 3}})
    llm = gw_app._lab_llm(_Conn())
    llm.complete_fn([{"role": "user", "content": "plan"}], role="plan")
    out = llm.scientist_chat([{"role": "user", "content": "run"}], [])
    assert "usage" not in out, "the usage is taken off the turn before the harness sees it"
    assert [(r["role"], r["provider"], r["remote"], r["model"]) for r in recorded] == [
        ("plan", "shared", False, "deepseek-v4-flash"), ("scientist", "shared", False, "deepseek-v4-flash")]
    assert recorded[1]["usage"] == {"prompt_tokens": 11, "completion_tokens": 3}


def test_a_fast_chat_turn_on_the_shared_model_is_recorded_as_shared_and_not_billed(monkeypatch):
    """Its address is a loopback URL, as an API key's would be; no key pays for it all the same."""
    from aiscientist.gateway import auth_routes
    rows: list[dict] = []
    monkeypatch.setattr(auth_routes, "record_llm_call", lambda **f: rows.append(f))
    for shared in (True, False):
        gw_app._record_chat_usage(_Conn(), role="chat", model="deepseek-v4-flash",
                                  base_url="http://127.0.0.1:43943/v1", native_anthropic=False,
                                  usage={"prompt_tokens": 5, "completion_tokens": 2}, duration_ms=3,
                                  shared=shared)
    assert [(r["provider"], r["remote"], r["endpoint"]) for r in rows] == [
        ("shared", False, "shared model"), ("openai", True, "http://127.0.0.1:43943/v1")]


# --- who may still start a GPU of their own ------------------------------------------------------------

def test_session_gpus_are_for_every_account_by_default_or_admins_only_or_off(monkeypatch):
    from types import SimpleNamespace
    admin, user = SimpleNamespace(role="admin"), SimpleNamespace(role="user")
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.delenv("AISCIENTIST_SESSION_GPU", raising=False)
    assert gw_app._session_gpu_allowed(user) and gw_app._other_endpoints() == "the cluster GPU or an API key"
    monkeypatch.setenv("AISCIENTIST_SESSION_GPU", "admins")
    assert gw_app._session_gpu_allowed(admin) and not gw_app._session_gpu_allowed(user)
    assert gw_app._other_endpoints() == "your own API key"
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    assert gw_app._session_gpu_allowed(None), "a console without accounts has no roles to check"
    monkeypatch.setenv("AISCIENTIST_SESSION_GPU", "off")
    assert not gw_app._session_gpu_allowed(admin)
    monkeypatch.setenv("AISCIENTIST_SESSION_GPU", "sometimes")
    assert gw_app._session_gpu_mode() == "all", "an unknown value keeps the default"


def test_without_session_gpus_a_connect_needs_an_endpoint(client, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_SESSION_GPU", "off")
    body = {"host": "hpc3.rcic.uci.edu", "ucinetid": "alice", "user": "alice", "auth_method": "password",
            "password": "x", "mock": False}
    res = client.post("/api/connect", json=body)
    assert res.status_code == 403 and res.json()["cause"] == "session_gpu"
    assert "shared model" in res.json()["error"]
    assert client.get("/api/cluster-models").json()["session_gpu"] is False
    monkeypatch.setenv("AISCIENTIST_SESSION_GPU", "all")
    assert client.get("/api/cluster-models").json()["session_gpu"] is True


def test_without_session_gpus_a_session_cannot_switch_to_the_cluster_gpu(client, monkeypatch):
    c = gw_app.Connection(HPCSettings(), mock=False, loop=asyncio.new_event_loop(), username="alice")
    c.status, c.llm_choice = "ready", {"credential_id": shared_llm.ENDPOINT_ID}
    gw_app.CONNECTIONS[c.id] = c
    try:
        monkeypatch.setenv("AISCIENTIST_SESSION_GPU", "off")
        res = client.post("/api/llm-endpoint", json={"connection_id": c.id, "credential_id": None})
        assert res.status_code == 403 and res.json()["cause"] == "session_gpu"
        assert c.llm_choice == {"credential_id": shared_llm.ENDPOINT_ID}, "the session stays where it was"
    finally:
        gw_app.CONNECTIONS.pop(c.id, None)
