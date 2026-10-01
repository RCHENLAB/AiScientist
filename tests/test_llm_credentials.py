"""Offline tests for per-user LLM API credentials: create, rotate, encrypt, confine, delete.

No network — :mod:`llm_credentials` never opens a socket, and the ``verify`` callable is
supplied by the caller, so every path here is exercised with a plain function.

The load-bearing property under test is **rotation is atomic in the user-visible sense**: a
rotation whose verification fails must leave the previous key working. That is what stops a
user from ending up with a discarded old key and a new one that doesn't work.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
from pathlib import Path

import pytest

from bioagent.gateway.errors import GatewayError


@pytest.fixture()
def lc(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("BIOAGENT_LLM_KEY_ENCRYPTION", raising=False)
    from bioagent.gateway import llm_credentials
    importlib.reload(llm_credentials)
    return llm_credentials


def _make(lc, owner="alice", key="sk-test-abcdefghijklmnop", **kw):
    return lc.create(owner, provider="deepseek", base_url="https://api.deepseek.com/v1",
                     model="deepseek-chat", api_key=key, **kw)


# --- create / public shape ---------------------------------------------------


def test_public_metadata_never_carries_the_key(lc):
    pub = _make(lc)
    blob = json.dumps(pub)
    assert "sk-test-abcdefghijklmnop" not in blob
    assert "key_path" not in pub
    assert pub["key_hint"] == "sk-tes…mnop"
    assert pub["key_fingerprint"].startswith("sha256:")


def test_key_file_is_0600_and_secret_round_trips(lc, tmp_path):
    pub = _make(lc)
    key_file = tmp_path / "llm_creds" / "alice" / f"{pub['id']}.key"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert lc.resolve_secret("alice", pub["id"]) == "sk-test-abcdefghijklmnop"


def test_index_json_holds_no_secret(lc, tmp_path):
    _make(lc)
    index = (tmp_path / "llm_creds" / "alice" / "index.json").read_text(encoding="utf-8")
    assert "sk-test-abcdefghijklmnop" not in index


def test_create_requires_key_and_base_url(lc):
    with pytest.raises(GatewayError):
        lc.create("alice", provider="x", base_url="https://a/v1", model="m", api_key="  ")
    with pytest.raises(GatewayError):
        lc.create("alice", provider="x", base_url="", model="m", api_key="sk-1")


def test_failed_verification_stores_nothing(lc):
    def boom(base_url, api_key, model):
        raise GatewayError("nope", stage="llm_credential")

    with pytest.raises(GatewayError):
        _make(lc, verify=boom)
    assert lc.list_credentials("alice") == []


# --- rotation ----------------------------------------------------------------


def test_rotation_keeps_the_id_and_swaps_the_secret(lc):
    pub = _make(lc)
    rotated = lc.rotate_key("alice", pub["id"], "sk-brand-new-qrstuvwxyz")

    assert rotated["id"] == pub["id"], "the id must survive so every reference to it stays valid"
    assert rotated["key_fingerprint"] != pub["key_fingerprint"]
    assert rotated["rotated_at"] is not None
    assert lc.resolve_secret("alice", pub["id"]) == "sk-brand-new-qrstuvwxyz"
    assert len(lc.list_credentials("alice")) == 1, "rotation updates in place, it does not add a row"


def test_failed_rotation_leaves_the_old_key_working(lc):
    """The property the whole design exists for."""
    pub = _make(lc)
    seen = {}

    def reject(base_url, api_key, model):
        seen["key"] = api_key
        raise GatewayError("401", stage="llm_credential")

    with pytest.raises(GatewayError):
        lc.rotate_key("alice", pub["id"], "sk-bad-key-000000000", verify=reject)

    assert seen["key"] == "sk-bad-key-000000000", "verification must see the NEW key, not the stored one"
    assert lc.resolve_secret("alice", pub["id"]) == "sk-test-abcdefghijklmnop"
    after = lc.list_credentials("alice")[0]
    assert after["key_fingerprint"] == pub["key_fingerprint"]
    assert after["rotated_at"] is None


def test_rotation_verifies_against_the_stored_endpoint(lc):
    pub = _make(lc)
    calls = []
    lc.rotate_key("alice", pub["id"], "sk-new-000000000000",
                  verify=lambda b, k, m: calls.append((b, m)))
    assert calls == [("https://api.deepseek.com/v1", "deepseek-chat")]


def test_rotating_an_unknown_id_raises(lc):
    with pytest.raises(GatewayError):
        lc.rotate_key("alice", "deadbeef", "sk-whatever-0000000")


# --- metadata edits ----------------------------------------------------------


def test_changing_the_model_invalidates_the_verification(lc):
    pub = _make(lc, verify=lambda b, k, m: None)
    assert pub["verified_at"] is not None

    edited = lc.update("alice", pub["id"], model="deepseek-reasoner")
    assert edited["verified_at"] is None, "the key was proven against the OLD model, not this one"
    assert edited["verified_model"] is None


def test_relabelling_keeps_the_verification(lc):
    pub = _make(lc, verify=lambda b, k, m: None)
    edited = lc.update("alice", pub["id"], label="my cheap endpoint")
    assert edited["label"] == "my cheap endpoint"
    assert edited["verified_at"] == pub["verified_at"]


def test_lab_model_round_trips(lc):
    pub = _make(lc, lab_model="deepseek-reasoner")
    assert pub["lab_model"] == "deepseek-reasoner"
    assert lc.update("alice", pub["id"], lab_model="")["lab_model"] is None


# --- optional encryption -----------------------------------------------------


def test_encrypted_at_rest_when_enabled(lc, tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.setenv("BIOAGENT_LLM_KEY_ENCRYPTION", "1")

    pub = _make(lc, key="sk-secret-value-1234567")
    raw = (tmp_path / "llm_creds" / "alice" / f"{pub['id']}.key").read_bytes()
    assert b"sk-secret-value-1234567" not in raw
    assert pub["encrypted"] is True
    assert lc.resolve_secret("alice", pub["id"]) == "sk-secret-value-1234567"

    master = tmp_path / "llm_creds" / ".master"
    assert stat.S_IMODE(master.stat().st_mode) == 0o600


def test_enabling_encryption_later_keeps_old_plaintext_rows_readable(lc, monkeypatch):
    """The per-row ``encrypted`` flag is what makes this a deployment choice rather than a
    migration."""
    pytest.importorskip("cryptography")
    old = _make(lc, key="sk-plaintext-000000000")

    monkeypatch.setenv("BIOAGENT_LLM_KEY_ENCRYPTION", "1")
    new = _make(lc, key="sk-encrypted-11111111")

    assert lc.resolve_secret("alice", old["id"]) == "sk-plaintext-000000000"
    assert lc.resolve_secret("alice", new["id"]) == "sk-encrypted-11111111"
    assert lc.list_credentials("alice")[0]["encrypted"] is True


def test_lost_master_key_reports_a_recoverable_error(lc, tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.setenv("BIOAGENT_LLM_KEY_ENCRYPTION", "1")
    pub = _make(lc, key="sk-gone-00000000000000")

    from cryptography.fernet import Fernet
    (tmp_path / "llm_creds" / ".master").write_bytes(Fernet.generate_key())

    with pytest.raises(GatewayError) as exc:
        lc.resolve_secret("alice", pub["id"])
    assert "Re-enter the API key" in str(exc.value)


# --- confinement -------------------------------------------------------------


def test_one_owner_cannot_resolve_anothers_credential(lc):
    mine = _make(lc, owner="alice")
    _make(lc, owner="bob", key="sk-bob-key-0000000000")

    assert lc.resolve_secret("bob", mine["id"]) is None
    assert lc.get_credential("bob", mine["id"]) is None
    assert [c["id"] for c in lc.list_credentials("bob")] != [mine["id"]]


def _point_key_path_at(tmp_path, target) -> None:
    index = tmp_path / "llm_creds" / "alice" / "index.json"
    rows = json.loads(index.read_text(encoding="utf-8"))
    rows[0]["key_path"] = str(target)
    index.write_text(json.dumps(rows), encoding="utf-8")


def test_key_path_outside_the_owner_store_is_ignored(lc, tmp_path):
    """A hand-edited or tampered index.json must not turn resolve_secret into an arbitrary
    file read. The recorded path is not consulted at all while the real key is in its canonical
    place, so the tampering achieves nothing — not an error, just no effect."""
    pub = _make(lc)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("root:x:0:0", encoding="utf-8")
    _point_key_path_at(tmp_path, outside)

    assert lc.resolve_secret("alice", pub["id"]) != "root:x:0:0"
    assert lc.resolve_secret("alice", pub["id"]) == "sk-test-abcdefghijklmnop"


def test_a_tampered_key_path_is_refused_when_there_is_no_canonical_file(lc, tmp_path):
    """The one case where the recorded path IS consulted — a store whose layout predates the
    canonical scheme — still may not escape the owner's directory."""
    pub = _make(lc)
    (tmp_path / "llm_creds" / "alice" / f"{pub['id']}.key").unlink()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("root:x:0:0", encoding="utf-8")
    _point_key_path_at(tmp_path, outside)

    with pytest.raises(GatewayError):
        lc.resolve_secret("alice", pub["id"])


def test_owner_names_are_sanitised_into_one_directory(lc, tmp_path):
    lc.create("../../etc", provider="p", base_url="https://a/v1", model="m", api_key="sk-000000000000")
    assert not (tmp_path / "llm_creds" / ".." / ".." / "etc").exists()
    assert (tmp_path / "llm_creds" / "etc").is_dir()


# --- delete ------------------------------------------------------------------


def test_delete_removes_the_row_and_the_key_file(lc, tmp_path):
    pub = _make(lc)
    key_file = tmp_path / "llm_creds" / "alice" / f"{pub['id']}.key"
    assert key_file.exists()

    assert lc.delete_credential("alice", pub["id"]) is True
    assert not key_file.exists()
    assert lc.list_credentials("alice") == []
    assert lc.delete_credential("alice", pub["id"]) is False


def test_mark_verified_records_success_and_failure(lc):
    pub = _make(lc)
    ok = lc.mark_verified("alice", pub["id"], model="deepseek-chat")
    assert ok["verified_at"] is not None and ok["last_error"] is None

    bad = lc.mark_verified("alice", pub["id"], model=None, error="401 unauthorized")
    assert bad["last_error"] == "401 unauthorized"
    assert lc.mark_verified("alice", "nope", model=None) is None


def test_the_key_survives_the_store_moving(tmp_path, monkeypatch):
    """A stored row's key_path does not survive the state dir moving — the key must anyway.

    Rows written while BIOAGENT_STATE_DIR was unset hold a path relative to the then-current
    working directory. Setting the state dir (which is what takes the users' secrets OUT of the
    directory the deploy rsyncs into) left every row naming a file that was no longer there: the
    key sat intact on disk while every use of it failed with "outside the owner's store".
    """
    import importlib, json, os, shutil
    from bioagent.gateway import llm_credentials as lc

    old_root = tmp_path / "old"
    old_root.mkdir()
    monkeypatch.chdir(old_root)
    monkeypatch.delenv("BIOAGENT_STATE_DIR", raising=False)   # the unset-in-prod case
    importlib.reload(lc)
    cred = lc.create("alice", provider="custom", base_url="https://a/v1", model="m1",
                     api_key="sk-lives-through-a-move")
    assert not os.path.isabs(json.loads((old_root / "llm_creds/alice/index.json").read_text())[0]["key_path"])

    new_root = tmp_path / "new"
    shutil.copytree(old_root / "llm_creds", new_root / "llm_creds")
    monkeypatch.setenv("BIOAGENT_STATE_DIR", str(new_root))
    monkeypatch.chdir(tmp_path)                               # the old relative path now resolves nowhere
    importlib.reload(lc)

    assert lc.resolve_secret("alice", cred["id"]) == "sk-lives-through-a-move"
    importlib.reload(lc)
