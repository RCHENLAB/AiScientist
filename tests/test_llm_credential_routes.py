"""Offline tests for the /api/llm-credentials routes: create, rotate, verify, list, delete.

Network is stubbed at :mod:`llm_providers`, so these cover the ROUTE contract — owner
scoping, the masked response shape, the cause field the UI keys its error message on, and
the rotation ordering — rather than re-testing the provider layer.
"""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("BIOAGENT_LLM_KEY_ENCRYPTION", raising=False)

    from bioagent.gateway import llm_credentials
    importlib.reload(llm_credentials)

    from fastapi.testclient import TestClient
    from bioagent.gateway import app as gw_app
    return TestClient(gw_app.app)


class _Endpoint:
    """A switchable stand-in for the provider layer.

    Switchable rather than one-shot because the interesting rotation test needs a failure
    FOLLOWED BY a success on the same credential — that sequence is the whole point of
    verify-before-commit, and a fixture that can only fail or only succeed cannot express it.
    """

    def __init__(self) -> None:
        self.seen: list[tuple] = []
        self._fail: tuple[str, str] | None = None

    def fail(self, cause: str = "auth", message: str = "bad key") -> None:
        self._fail = (cause, message)

    def succeed(self) -> None:
        self._fail = None

    def verify(self, base_url, api_key, model=None, timeout=15.0):
        from bioagent.gateway import llm_providers
        self.seen.append((base_url, api_key, model))
        if self._fail:
            return llm_providers.VerifyResult(False, self._fail[0], self._fail[1])
        return llm_providers.VerifyResult(True, "ok", "fine", ["m1", "m2"], verified_model=model)


@pytest.fixture()
def ok_endpoint(monkeypatch):
    from bioagent.gateway import llm_providers

    ep = _Endpoint()
    monkeypatch.setattr(llm_providers, "verify", ep.verify)
    monkeypatch.setattr(llm_providers, "list_models", lambda b, k, timeout=15.0: ["m1", "m2"])
    return ep


def _create(client, **kw):
    body = {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
            "model": "m1", "api_key": "sk-original-000000000", "user": "alice"}
    body.update(kw)
    return client.post("/api/llm-credentials", json=body)


# --- presets -----------------------------------------------------------------


def test_presets_are_served(client):
    providers = client.get("/api/llm-providers").json()["providers"]
    assert {"openrouter", "openai", "custom"} <= {p["key"] for p in providers}
    assert all("api_key" not in p for p in providers)


# --- create ------------------------------------------------------------------


def test_create_verifies_then_returns_masked_metadata(client, ok_endpoint):
    res = _create(client)
    assert res.status_code == 200
    cred = res.json()["credential"]

    assert "sk-original-000000000" not in res.text
    assert cred["key_hint"] == "sk-ori…0000"
    assert cred["verified_at"] is not None
    assert ok_endpoint.seen == [("https://api.deepseek.com/v1", "sk-original-000000000", "m1")]


def test_create_normalises_a_pasted_full_endpoint(client, ok_endpoint):
    _create(client, base_url="https://api.deepseek.com/v1/chat/completions")
    assert ok_endpoint.seen[0][0] == "https://api.deepseek.com/v1"


def test_create_falls_back_to_the_preset_base_url(client, ok_endpoint):
    _create(client, provider="openrouter", base_url="")
    assert ok_endpoint.seen[0][0] == "https://openrouter.ai/api/v1"


def test_create_without_an_owner_is_refused_rather_than_filed_under_guest(client, ok_endpoint):
    """With accounts off the owner is the UCInetID the caller sends. An empty one used to store a
    verified key under the anonymous ``guest`` bucket, which the session — resolving its owner from
    the UCInetID it logged in with — never reads: the key saved, then vanished."""
    res = _create(client, user="")

    assert res.status_code == 400
    assert res.json()["cause"] == "owner"
    assert ok_endpoint.seen == []                                   # refused before touching the provider
    assert client.get("/api/llm-credentials?user=").json()["credentials"] == []


def test_rejected_key_reports_the_cause_and_stores_nothing(client, ok_endpoint):
    ok_endpoint.fail("credit", "no credit left")
    res = _create(client)

    assert res.status_code == 400
    assert res.json()["cause"] == "credit"
    assert res.json()["error"] == "no credit left"
    assert client.get("/api/llm-credentials?user=alice").json()["credentials"] == []


@pytest.mark.parametrize("cause", ["auth", "credit", "model", "endpoint", "network"])
def test_every_cause_reaches_the_client_verbatim(client, ok_endpoint, cause):
    ok_endpoint.fail(cause)
    assert _create(client).json()["cause"] == cause


# --- rotation ----------------------------------------------------------------


def test_rotation_keeps_the_id_and_the_old_key_until_the_new_one_verifies(client, ok_endpoint):
    cred_id = _create(client).json()["credential"]["id"]

    ok_endpoint.fail("auth", "revoked")
    bad = client.put(f"/api/llm-credentials/{cred_id}",
                     json={"api_key": "sk-broken-1111111111", "user": "alice"})
    assert bad.status_code == 400 and bad.json()["cause"] == "auth"

    from bioagent.gateway import llm_credentials
    assert llm_credentials.resolve_secret("alice", cred_id) == "sk-original-000000000"

    ok_endpoint.succeed()
    good = client.put(f"/api/llm-credentials/{cred_id}",
                      json={"api_key": "sk-replacement-2222222", "user": "alice"})
    assert good.status_code == 200
    assert good.json()["credential"]["id"] == cred_id, "references to this credential must stay valid"
    assert llm_credentials.resolve_secret("alice", cred_id) == "sk-replacement-2222222"


def test_rotation_is_verified_against_the_endpoint_being_moved_to(client, ok_endpoint):
    """Metadata is applied before the key is checked, so switching provider and key in one
    submit tests the NEW pair rather than the one being left behind."""
    cred_id = _create(client).json()["credential"]["id"]
    ok_endpoint.seen.clear()

    client.put(f"/api/llm-credentials/{cred_id}",
               json={"api_key": "sk-moonshot-333333333", "base_url": "https://api.moonshot.cn/v1",
                     "model": "m2", "user": "alice"})
    assert ok_endpoint.seen == [("https://api.moonshot.cn/v1", "sk-moonshot-333333333", "m2")]


def test_metadata_only_edit_needs_no_key(client, ok_endpoint):
    cred_id = _create(client).json()["credential"]["id"]
    ok_endpoint.seen.clear()

    res = client.put(f"/api/llm-credentials/{cred_id}",
                     json={"label": "cheap endpoint", "lab_model": "m2", "user": "alice"})
    assert res.status_code == 200
    assert res.json()["credential"]["label"] == "cheap endpoint"
    assert res.json()["credential"]["lab_model"] == "m2"
    assert ok_endpoint.seen == [], "no key was submitted, so nothing needed verifying"


def test_unknown_credential_is_404(client, ok_endpoint):
    assert client.put("/api/llm-credentials/nope", json={"user": "alice"}).status_code == 404
    assert client.post("/api/llm-credentials/nope/verify?user=alice").status_code == 404
    assert client.delete("/api/llm-credentials/nope?user=alice").status_code == 404


# --- verify / models / list / delete -----------------------------------------


def test_reverify_records_a_key_that_stopped_working(client, ok_endpoint):
    cred_id = _create(client).json()["credential"]["id"]

    ok_endpoint.fail("credit", "out of credit")
    res = client.post(f"/api/llm-credentials/{cred_id}/verify?user=alice")
    assert res.status_code == 200
    assert res.json()["result"]["cause"] == "credit"
    assert res.json()["credential"]["last_error"] == "out of credit"

    listed = client.get("/api/llm-credentials?user=alice").json()["credentials"]
    assert listed[0]["last_error"] == "out of credit", "the UI can flag it without re-testing"


def test_model_list_comes_from_the_live_endpoint(client, ok_endpoint):
    cred_id = _create(client).json()["credential"]["id"]
    assert client.get(f"/api/llm-credentials/{cred_id}/models?user=alice").json()["models"] == ["m1", "m2"]


def test_list_never_leaks_a_key(client, ok_endpoint):
    _create(client)
    res = client.get("/api/llm-credentials?user=alice")
    assert "sk-original-000000000" not in res.text
    assert res.json()["encryption"] is False


def test_credentials_are_scoped_to_their_owner(client, ok_endpoint):
    cred_id = _create(client).json()["credential"]["id"]

    assert client.get("/api/llm-credentials?user=bob").json()["credentials"] == []
    assert client.post(f"/api/llm-credentials/{cred_id}/verify?user=bob").status_code == 404
    assert client.delete(f"/api/llm-credentials/{cred_id}?user=bob").status_code == 404
    assert client.get("/api/llm-credentials?user=alice").json()["credentials"][0]["id"] == cred_id


def test_delete_removes_it(client, ok_endpoint):
    cred_id = _create(client).json()["credential"]["id"]
    assert client.delete(f"/api/llm-credentials/{cred_id}?user=alice").status_code == 200
    assert client.get("/api/llm-credentials?user=alice").json()["credentials"] == []


# --- selecting an endpoint for a session, and the consent gate ---------------


@pytest.fixture()
def conn(client):
    """A ready mock session owned by "alice", registered so /api/llm-endpoint can find it."""
    import asyncio
    from bioagent.gateway import app as gw_app
    from bioagent.gateway.settings import HPCSettings

    c = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="alice")
    c.status = "ready"
    gw_app.CONNECTIONS[c.id] = c
    yield c
    gw_app.CONNECTIONS.pop(c.id, None)


def _select(client, conn, cred_id=None, **kw):
    body = {"connection_id": conn.id, "credential_id": cred_id}
    body.update(kw)
    return client.post("/api/llm-endpoint", json=body)


def test_selecting_a_remote_endpoint_asks_for_consent_first(client, ok_endpoint, conn):
    """No scanner can decide whether a gene list identifies a patient, so the person who knows
    the data is asked — once per credential, before any prompt is routed."""
    cred_id = _create(client).json()["credential"]["id"]

    res = _select(client, conn, cred_id)
    assert res.status_code == 409
    body = res.json()
    assert body["needs_consent"] is True
    assert body["endpoint"]["base_url"] == "https://api.deepseek.com/v1"
    assert any("dataset profile" in s for s in body["sends"])
    assert any("run on HPC3" in s for s in body["stays"])
    assert conn.llm_choice == {}, "nothing was switched while consent was outstanding"


def test_consent_is_recorded_and_not_asked_again(client, ok_endpoint, conn):
    cred_id = _create(client).json()["credential"]["id"]

    assert _select(client, conn, cred_id, accept_egress=True).status_code == 200
    assert conn.llm_choice == {"credential_id": cred_id}

    conn.llm_choice = {}
    assert _select(client, conn, cred_id).status_code == 200, "consent persists on the credential"
    assert client.get("/api/llm-credentials?user=alice").json()["credentials"][0]["egress_consent_at"]


def test_a_loopback_endpoint_needs_no_consent(client, ok_endpoint, conn):
    """Nothing leaves the host, so there is nothing to consent to."""
    cred_id = _create(client, provider="custom", base_url="http://127.0.0.1:8001/v1").json()["credential"]["id"]
    assert _select(client, conn, cred_id).status_code == 200


def test_consent_does_not_carry_across_credentials(client, ok_endpoint, conn):
    """Trusting your own vLLM is not the same decision as trusting a commercial API."""
    trusted = _create(client, provider="custom", base_url="http://127.0.0.1:8001/v1").json()["credential"]["id"]
    _select(client, conn, trusted)

    commercial = _create(client, provider="openrouter", base_url="https://openrouter.ai/api/v1",
                         api_key="sk-or-second-key-00000").json()["credential"]["id"]
    assert _select(client, conn, commercial).status_code == 409


def test_switching_back_to_the_cluster_is_always_allowed(client, ok_endpoint, conn):
    cred_id = _create(client).json()["credential"]["id"]
    _select(client, conn, cred_id, accept_egress=True)

    res = _select(client, conn, None)
    assert res.json()["endpoint"] == "cluster"
    assert conn.llm_choice == {}


def test_a_credential_without_a_model_is_refused(client, ok_endpoint, conn):
    cred_id = _create(client, model="").json()["credential"]["id"]
    res = _select(client, conn, cred_id)
    assert res.status_code == 400 and "model" in res.json()["error"]


def test_selecting_another_owners_credential_is_404(client, ok_endpoint, conn):
    cred_id = _create(client).json()["credential"]["id"]
    conn.owner = "bob"
    assert _select(client, conn, cred_id).status_code == 404
