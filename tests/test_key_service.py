"""The key service (RCHENLAB/AiScientist issue 16): the web server holds no API key; a separate
process does, and every model call goes through its proxy.

End to end on loopback: a stub provider (records the key it receives, can echo it in an error), the
key service as a real subprocess with its own store and token, and ``llm_credentials`` in service
mode as the web server runs it.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
pytest.importorskip("httpx")

SRC = Path(__file__).resolve().parents[1] / "src"


def _fake_key(*parts: str) -> str:
    """A key-shaped test value assembled at run time (a literal one would trip the secret scans)."""
    return "-".join(("sk",) + parts)


KEY = _fake_key("test", "KEYSERVICEexample0123456789abcdefXYZ")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Provider(BaseHTTPRequestHandler):
    """An OpenAI-compatible and Anthropic-shaped stub that records what it was sent."""

    seen: list[dict] = []

    def log_message(self, *a):  # quiet
        pass

    def _reply(self, status: int, body: dict | str, ctype: str = "application/json") -> None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        _Provider.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                               "x-api-key": self.headers.get("x-api-key")})
        # Both shapes at once: an OpenAI list reads data[].id, the Anthropic SDK the rest.
        self._reply(200, {"data": [{"id": m, "type": "model", "display_name": m,
                                    "created_at": "2026-01-01T00:00:00Z"} for m in ("m1", "m2")],
                          "has_more": False, "first_id": "m1", "last_id": "m2"})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        _Provider.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                               "x-api-key": self.headers.get("x-api-key"), "body": body})
        if self.path.startswith("/bad/"):
            # A provider that copies the key it was given into its error.
            self._reply(401, {"error": {"message": f"invalid key {self.headers.get('Authorization')}"}})
            return
        if self.path.endswith("/v1/messages"):
            self._reply(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x",
                              "content": [{"type": "text", "text": "hello from claude"}],
                              "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 2}})
            return
        if body.get("stream"):
            chunks = [{"choices": [{"delta": {"content": "hel"}}]}, {"choices": [{"delta": {"content": "lo"}}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 5}}]
            text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            self._reply(200, text, "text/event-stream")
            return
        self._reply(200, {"model": "m1", "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})


@pytest.fixture(scope="module")
def provider():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    root = tmp_path_factory.mktemp("keysvc")
    keys, token = root / "keys", root / "token"
    keys.mkdir()
    token.write_text("svc-token-" + os.urandom(8).hex())
    token.chmod(0o600)
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AISCIENTIST_", "BIOAGENT_"))}
    env.update(AISCIENTIST_STATE_DIR=str(keys), AISCIENTIST_KEY_SERVICE_TOKEN_FILE=str(token),
               PYTHONPATH=str(SRC))
    proc = subprocess.Popen([sys.executable, "-m", "aiscientist.gateway.key_service", "--port", str(port)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            urllib.request.urlopen(url + "/health", timeout=1).read()
            break
        except (urllib.error.URLError, OSError):
            if proc.poll() is not None:
                pytest.fail(proc.stdout.read().decode())
            time.sleep(0.1)
    yield {"url": url, "keys": keys, "token": token}
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture()
def store(tmp_path, monkeypatch, service):
    """``llm_credentials`` as the web server runs it with the key service on."""
    gw = tmp_path / "gw"
    gw.mkdir()
    # Both brand prefixes, so both are removed after the test: importing the app mirrors one into
    # the other (core.config.apply_brand_env_aliases), and a leftover legacy name would come back.
    for prefix in ("AISCIENTIST_", "BIOAGENT_"):
        monkeypatch.setenv(prefix + "STATE_DIR", str(gw))
        monkeypatch.setenv(prefix + "KEY_SERVICE", service["url"])
        monkeypatch.setenv(prefix + "KEY_SERVICE_TOKEN_FILE", str(service["token"]))
    from aiscientist.gateway import key_client, llm_credentials
    importlib.reload(key_client)
    importlib.reload(llm_credentials)
    return llm_credentials


def _cred(store, owner, base, **kw):
    body = dict(provider="custom", base_url=base, model="m1", api_key=KEY, label="Mine")
    body.update(kw)
    return store.create(owner, **body)


def _bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


def test_a_saved_key_lives_only_in_the_service_and_calls_go_through_its_proxy(store, service, provider, tmp_path):
    from aiscientist.gateway import vllm_client
    from aiscientist.gateway.errors import GatewayError
    cred = _cred(store, "alice", f"{provider}/v1")
    assert cred["key_hint"] and cred["encrypted"]
    assert KEY.encode() not in _bytes(tmp_path / "gw"), "the web server's store holds no key"
    assert not list((tmp_path / "gw").rglob("*.key")) and not (tmp_path / "gw" / "llm_key.master").exists()
    assert KEY.encode() not in _bytes(service["keys"]), "the service encrypts it at rest"
    with pytest.raises(GatewayError):
        store.resolve_secret("alice", cred["id"])
    base, token = store.call_target("alice", cred["id"])
    assert base.startswith(service["url"] + "/proxy/alice/") and token != KEY
    _Provider.seen.clear()
    text, usage = vllm_client.complete_ex(0, "m1", [{"role": "user", "content": "hi"}], base_url=base, api_key=token)
    assert text == "hello" and usage["total_tokens"] == 4
    assert _Provider.seen[-1]["auth"] == f"Bearer {KEY}", "the provider got the real key"
    assert _Provider.seen[-1]["path"] == "/v1/chat/completions"
    streamed = list(vllm_client.chat_tools_stream(0, "m1", [{"role": "user", "content": "hi"}], None,
                                                  base_url=base, api_key=token))
    assert "".join(c for k, c in streamed if k == "content") == "hello"
    from aiscientist.gateway import llm_providers
    assert llm_providers.list_models(base, token) == ["m1", "m2"]


def test_a_provider_error_that_echoes_the_key_is_scrubbed_by_the_service(store, provider):
    from aiscientist.gateway import vllm_client
    from aiscientist.gateway.errors import GatewayError
    cred = _cred(store, "bob", f"{provider}/bad/v1")
    base, token = store.call_target("bob", cred["id"])
    with pytest.raises(GatewayError) as info:
        vllm_client.complete_ex(0, "m1", [{"role": "user", "content": "hi"}], base_url=base, api_key=token)
    assert KEY not in str(info.value) and KEY not in str(info.value.detail)


def test_an_anthropic_key_goes_in_x_api_key(store, provider):
    from aiscientist.gateway import anthropic_client
    if not anthropic_client.available():
        pytest.skip("anthropic SDK not installed")
    cred = _cred(store, "carol", provider, provider="anthropic", model="claude-x")
    base, token = store.call_target("carol", cred["id"])
    _Provider.seen.clear()
    # The SDK, given the proxy as its base URL and the token as its key.
    ok, cause, _msg, models = anthropic_client.probe_models(token, base)
    assert ok and models == ["m1", "m2"], cause
    assert _Provider.seen[-1]["path"].startswith("/v1/models") and _Provider.seen[-1]["x-api-key"] == KEY
    # A Messages call as the SDK sends it.
    req = urllib.request.Request(base + "/v1/messages", method="POST",
                                 data=json.dumps({"model": "claude-x", "max_tokens": 5,
                                                  "messages": [{"role": "user", "content": "hi"}]}).encode(),
                                 headers={"x-api-key": token, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    reply = json.loads(urllib.request.urlopen(req, timeout=10).read())
    assert reply["content"][0]["text"] == "hello from claude"
    hit = _Provider.seen[-1]
    assert hit["path"] == "/v1/messages" and hit["x-api-key"] == KEY and not hit["auth"]


def test_nothing_answers_without_the_token(service):
    for path in ("/proxy/alice/0123456789ab/v1/models", "/v1/status"):
        req = urllib.request.Request(service["url"] + path, headers={"Authorization": "Bearer wrong"})
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(req, timeout=5)
        assert info.value.code == 401


def test_a_key_moves_to_another_address_only_with_the_key(store, provider):
    from aiscientist.gateway.errors import GatewayError
    cred = _cred(store, "dave", f"{provider}/v1")
    with pytest.raises(GatewayError) as info:
        store.update("dave", cred["id"], base_url="https://attacker.example/v1")
    assert "paste the key again" in str(info.value)
    store.update("dave", cred["id"], base_url=f"{provider}/v2", key_follows=True)
    store.rotate_key("dave", cred["id"], KEY)
    from aiscientist.gateway import key_client
    assert key_client.get("dave", cred["id"])["readable"]


def test_delete_session_keys_purge_and_the_check(store, service, provider, tmp_path, capsys):
    from aiscientist.gateway import admin_cli, key_client, vllm_client
    cred = _cred(store, "erin", f"{provider}/v1")
    assert admin_cli._key_service_check() == 0
    assert "held and readable" in capsys.readouterr().out
    # A session-only key: in the service's memory, nowhere on disk.
    before = _bytes(service["keys"])
    s = store.create_session("erin", session_tag="tab", provider="custom", base_url=f"{provider}/v1",
                             model="m1", api_key=_fake_key("session", "0123456789abcdefSESSION"))
    assert _bytes(service["keys"]) == before or b"session" not in _bytes(service["keys"])
    base, token = store.call_target("erin", s["id"])
    assert vllm_client.complete_ex(0, "m1", [{"role": "user", "content": "x"}], base_url=base, api_key=token)[0] == "hello"
    store.end_session("erin", "tab")
    assert key_client.get("erin", s["id"]) is None
    # Delete one key: the service forgets it first.
    assert store.delete_credential("erin", cred["id"])
    assert key_client.get("erin", cred["id"]) is None
    # Purge: every key the service holds is gone.
    _cred(store, "frank", f"{provider}/v1")
    done = store.purge_all()
    assert done["service"]["credentials"] >= 1 and key_client.status()["held"] == 0
    assert store.list_credentials("frank") == []


def test_a_run_s_model_client_sends_through_the_proxy(store, provider):
    """``_lab_llm`` with a saved key in service mode: the call reaches the provider with the real key,
    while the web server only ever passed the token."""
    from aiscientist.gateway import app as gw_app
    cred = _cred(store, "gina", f"{provider}/v1")

    class _Conn:
        owner, selected_model, tunnel_port, mock = "gina", "q", None, False
        llm_choice = {"credential_id": cred["id"]}

        def emit(self, *a, **k):
            pass

    _Provider.seen.clear()
    llm = gw_app._lab_llm(_Conn())
    assert llm.complete_fn([{"role": "user", "content": "plan"}]) == "hello"
    assert _Provider.seen[-1]["auth"] == f"Bearer {KEY}"
