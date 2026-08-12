"""Offline tests for the SSH-credential lifecycle: generate a keypair, deploy the public
key over a (fake) authenticated session, persist the private key locally, and list/get/
delete. No network, no real SSH."""

from __future__ import annotations

import os
import types

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("paramiko")


class _FakeExec:
    """Records the deploy command; mimics ExecResult (exit_status)."""

    def __init__(self, exit_status: int = 0) -> None:
        self.cmds: list[str] = []
        self._status = exit_status

    def exec(self, cmd: str, timeout: float = 60.0):
        self.cmds.append(cmd)
        return types.SimpleNamespace(exit_status=self._status, stdout="", stderr="")


@pytest.fixture()
def sc(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOAGENT_STATE_DIR", str(tmp_path))
    import importlib

    from bioagent.gateway import ssh_credentials
    importlib.reload(ssh_credentials)
    return ssh_credentials


def test_generate_keypair_shapes(sc):
    priv, pub = sc.generate_keypair()
    assert pub.startswith("ssh-ed25519 ")
    assert b"OPENSSH PRIVATE KEY" in priv


def test_encrypted_key_loads_only_with_passphrase(sc, tmp_path):
    import paramiko
    priv, _ = sc.generate_keypair("hunter2")
    p = tmp_path / "enc.key"
    p.write_bytes(priv)
    assert paramiko.Ed25519Key.from_private_key_file(str(p), password="hunter2")
    with pytest.raises(paramiko.SSHException):
        paramiko.Ed25519Key.from_private_key_file(str(p), password="wrong")


def test_create_and_deploy_then_list_get_delete(sc):
    ex = _FakeExec()
    cred = sc.create_and_deploy("yijun", ex, host="hpc3.rcic.uci.edu", hpc_user="testuser")
    # deploy appended to authorized_keys, idempotently
    assert "authorized_keys" in ex.cmds[0] and "grep -qxF" in ex.cmds[0]
    # public metadata only — no key_path leaked
    assert cred["host"] == "hpc3.rcic.uci.edu" and cred["hpc_user"] == "testuser"
    assert "key_path" not in cred
    # listed, and the private key file is 0600
    assert len(sc.list_credentials("yijun")) == 1
    full = sc.get_credential("yijun", cred["id"])
    assert os.path.exists(full["key_path"])
    assert oct(os.stat(full["key_path"]).st_mode)[-3:] == "600"
    # owner isolation
    assert sc.get_credential("someone_else", cred["id"]) is None
    assert sc.list_credentials("someone_else") == []
    # delete removes row + file
    assert sc.delete_credential("yijun", cred["id"])
    assert not os.path.exists(full["key_path"])
    assert sc.list_credentials("yijun") == []


def test_deploy_failure_raises(sc):
    from bioagent.gateway.errors import GatewayError
    ex = _FakeExec(exit_status=1)
    with pytest.raises(GatewayError):
        sc.create_and_deploy("yijun", ex, host="h", hpc_user="u")
