"""Offline tests for the SSH-credential lifecycle: generate a keypair, deploy the public
key over a (fake) authenticated session, persist the private key locally, and list/get/
delete. No network, no real SSH."""

from __future__ import annotations

import os
import types
from pathlib import Path

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
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path))
    import importlib

    from aiscientist.gateway import ssh_credentials
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
    from aiscientist.gateway.errors import GatewayError
    ex = _FakeExec(exit_status=1)
    with pytest.raises(GatewayError):
        sc.create_and_deploy("yijun", ex, host="h", hpc_user="u")


def test_a_saved_ssh_key_survives_the_store_moving(tmp_path, monkeypatch):
    """Same trap as the LLM store, with a worse blast radius: this key_path goes to paramiko, so a
    stale one costs every user their key login and drops them back to password + Duo."""
    import importlib
    import shutil
    from aiscientist.gateway import ssh_credentials as sc

    old_root = tmp_path / "old"
    old_root.mkdir()
    monkeypatch.chdir(old_root)
    monkeypatch.delenv("AISCIENTIST_STATE_DIR", raising=False)
    importlib.reload(sc)
    cred = sc.create_and_deploy("alice", _FakeExec(), host="hpc3.rcic.uci.edu", hpc_user="alice")

    new_root = tmp_path / "new"
    shutil.copytree(old_root / "ssh_creds", new_root / "ssh_creds")
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(new_root))
    monkeypatch.chdir(tmp_path)
    importlib.reload(sc)

    row = sc.get_credential("alice", cred["id"])
    assert row is not None
    assert Path(row["key_path"]).is_file(), "the login path opens this exact path"
    importlib.reload(sc)


def test_a_new_key_and_its_index_are_private_from_the_first_byte(sc, tmp_path):
    import stat
    (tmp_path / "ssh_creds" / "yijun").mkdir(parents=True)
    os.chmod(tmp_path / "ssh_creds" / "yijun", 0o775)          # as production had them
    sc.create_and_deploy("yijun", _FakeExec(), host="hpc3.rcic.uci.edu", hpc_user="testuser")
    for d in (tmp_path / "ssh_creds", tmp_path / "ssh_creds" / "yijun"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    files = list((tmp_path / "ssh_creds" / "yijun").iterdir())
    assert {f.name.split(".")[-1] for f in files} == {"key", "json"}     # no .tmp left behind
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
