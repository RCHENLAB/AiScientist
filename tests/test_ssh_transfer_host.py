"""RCIC compliance: bulk file staging must NOT run on an HPC3 login node.

The 2026-08-06 RCIC notice reserves login-i15/16/17 for logging in and submitting Slurm jobs,
and sends rsync/SFTP/rclone/wget to access-hpc3.rcic.uci.edu instead. ``SSHExecutor`` therefore
splits the two planes: ``exec``/Slurm/tunnels keep the login session, ``put_file``/``get_file``
open their own connection to the transfer host.

These tests pin the split without any network: the executor is built bare (``object.__new__``)
and handed fake clients, so what is asserted is WHICH connection each byte goes over.
"""

from __future__ import annotations

import types

import pytest

pytest.importorskip("paramiko")

from bioagent.gateway.ssh_gateway import SSHExecutor  # noqa: E402


class _FakeRemoteFile:
    """Enough of a paramiko SFTPFile to see how much was prefetched and read."""

    def __init__(self, data: bytes, owner: "_FakeClient") -> None:
        self.data = data
        self.owner = owner
        self.pos = 0

    def prefetch(self, size: int | None = None) -> None:
        self.owner.prefetched.append(size)

    def read(self, size: int | None = None) -> bytes:
        chunk = self.data[self.pos:] if size is None else self.data[self.pos:self.pos + size]
        self.pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class _FakeSFTP:
    def __init__(self, owner: "_FakeClient") -> None:
        self.owner = owner
        self.closed = False

    def put(self, local: str, remote: str) -> None:
        self.owner.puts.append((local, remote))

    def get(self, remote: str, local: str) -> None:
        self.owner.gets.append((remote, local))

    def open(self, remote: str, mode: str = "r") -> _FakeRemoteFile:
        self.owner.reads.append(remote)
        if remote not in self.owner.files:
            raise OSError(f"No such file: {remote}")
        return _FakeRemoteFile(self.owner.files[remote], self.owner)

    def stat(self, remote: str):
        if remote not in self.owner.files:
            raise OSError(f"No such file: {remote}")
        return types.SimpleNamespace(st_size=len(self.owner.files[remote]))

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    """Stands in for a paramiko SSHClient; records every transfer made over it."""

    def __init__(self, label: str, *, active: bool = True) -> None:
        self.label = label
        self.puts: list[tuple[str, str]] = []
        self.gets: list[tuple[str, str]] = []
        self.reads: list[str] = []
        self.prefetched: list[int | None] = []
        self.files: dict[str, bytes] = {}
        self.sftps: list[_FakeSFTP] = []
        self.closed = False
        self.active = active

    def open_sftp(self) -> _FakeSFTP:
        sftp = _FakeSFTP(self)
        self.sftps.append(sftp)
        return sftp

    def get_transport(self):
        return types.SimpleNamespace(is_active=lambda: self.active)

    def set_missing_host_key_policy(self, _policy) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _executor(*, transfer_host: str | None = "access-hpc3.rcic.uci.edu", pkey: object | None = "KEY"):
    """A bare SSHExecutor with no connection — only the data-plane state the tests exercise."""
    ex = object.__new__(SSHExecutor)
    ex.host = "hpc3.rcic.uci.edu"
    ex.username = "testuser"
    ex.port = 22
    ex.group = None
    ex.events: list[tuple[str, str, str]] = []
    ex._emit = lambda level, stage, message: ex.events.append((level, stage, message))
    ex._tunnels = []
    ex.transfer_host = (transfer_host or "").strip() or None
    ex._connect_timeout = 30.0
    ex._transfer_client = None
    ex._transfer_pkey = pkey
    ex._transfer_disabled = False
    ex._client = _FakeClient("login")
    ex.execs: list[str] = []
    ex.exec = lambda cmd, timeout=60.0: ex.execs.append(cmd)  # control plane recorder
    return ex


@pytest.fixture()
def dtn(monkeypatch):
    """Make the transfer-host connection succeed, and hand back the client it produces."""
    import paramiko

    from bioagent.gateway import ssh_gateway

    made: list[_FakeClient] = []
    dialed: list[tuple[str, int]] = []

    class _FakeTransport:
        def __init__(self, _sock) -> None:
            self.authed = False

        def start_client(self, timeout=None) -> None:
            return None

        def auth_publickey(self, _user, _key) -> None:
            self.authed = True

        def is_authenticated(self) -> bool:
            return self.authed

        def set_keepalive(self, _n) -> None:
            return None

    def _fake_client():
        client = _FakeClient("transfer")
        made.append(client)
        return client

    def _fake_connect(addr, timeout=None):
        dialed.append(addr)
        return object()

    monkeypatch.setattr(ssh_gateway.socket, "create_connection", _fake_connect)
    monkeypatch.setattr(paramiko, "Transport", _FakeTransport)
    monkeypatch.setattr(paramiko, "SSHClient", _fake_client)
    return types.SimpleNamespace(clients=made, dialed=dialed)


# -- the split ------------------------------------------------------------------------


def test_put_and_get_go_over_the_transfer_host_not_the_login_node(dtn):
    ex = _executor()
    ex.put_file("/local/big.vcf.gz", "/dfs3b/ruic20_lab/u/uploads/big.vcf.gz")
    ex.get_file("/dfs3b/ruic20_lab/u/out/result.csv", "/local/result.csv")

    assert dtn.dialed == [("access-hpc3.rcic.uci.edu", 22)]
    transfer = dtn.clients[0]
    assert transfer.puts == [("/local/big.vcf.gz", "/dfs3b/ruic20_lab/u/uploads/big.vcf.gz")]
    assert transfer.gets == [("/dfs3b/ruic20_lab/u/out/result.csv", "/local/result.csv")]
    # The login node carried NO bytes — that is the whole point of the rule.
    assert ex._client.puts == [] and ex._client.gets == [] and ex._client.sftps == []


def test_one_connection_is_reused_across_transfers(dtn):
    ex = _executor()
    for i in range(3):
        ex.put_file(f"/local/{i}", f"/dfs3b/{i}")
    assert len(dtn.dialed) == 1, "each transfer must not re-authenticate"
    assert len(dtn.clients[0].puts) == 3


def test_every_sftp_channel_is_closed(dtn):
    ex = _executor()
    ex.put_file("/local/a", "/dfs3b/a")
    ex.get_file("/dfs3b/b", "/local/b")
    assert [s.closed for s in dtn.clients[0].sftps] == [True, True]


def test_mkdir_stays_on_the_login_session(dtn):
    """The parent mkdir is control plane, not transfer — and the DTN's restricted shell
    would refuse to run it anyway."""
    ex = _executor()
    ex.put_file("/local/big.h5ad", "/dfs3b/ruic20_lab/u/uploads/nested/big.h5ad")
    assert ex.execs == ["mkdir -p /dfs3b/ruic20_lab/u/uploads/nested"]


def test_a_dead_transfer_transport_is_reopened(dtn):
    ex = _executor()
    ex.put_file("/local/a", "/dfs3b/a")
    dtn.clients[0].active = False  # reaped while the run sat idle
    ex.put_file("/local/b", "/dfs3b/b")
    assert len(dtn.clients) == 2 and dtn.clients[1].puts == [("/local/b", "/dfs3b/b")]


# -- reading file CONTENT is a transfer too --------------------------------------------


def test_read_bytes_uses_the_transfer_host_not_cat_on_the_login_node(dtn):
    ex = _executor()
    ex._transfer_session().files["/dfs3b/run/result.json"] = b'{"status": "ok"}'

    assert ex.read_bytes("/dfs3b/run/result.json") == b'{"status": "ok"}'
    assert ex.execs == [], "content must not be fetched by exec'ing cat/head on a login node"
    assert dtn.clients[0].reads == ["/dfs3b/run/result.json"]


def test_a_bounded_peek_never_prefetches_the_whole_file(dtn):
    """The ingest peek reads 256 KB of files that can be 1.1 GB — an unbounded prefetch here
    would drag the entire VCF back for a preview."""
    ex = _executor()
    ex._transfer_session().files["/dfs3b/u/huge.vcf"] = b"x" * 4096

    assert ex.read_bytes("/dfs3b/u/huge.vcf", 512) == b"x" * 512
    assert dtn.clients[0].prefetched == [512]


def test_unbounded_read_prefetches_whole_file(dtn):
    ex = _executor()
    ex._transfer_session().files["/dfs3b/run/small.log"] = b"boom"
    ex.read_bytes("/dfs3b/run/small.log")
    assert dtn.clients[0].prefetched == [None]


def test_missing_file_reads_empty_like_cat_dev_null_did(dtn):
    """Callers treat "absent" as "nothing written yet"; that must survive the move to SFTP."""
    ex = _executor()
    assert ex.read_bytes("/dfs3b/run/not-there.json") == b""
    assert ex.remote_size("/dfs3b/run/not-there.json") == 0


def test_remote_size_reports_the_true_size(dtn):
    ex = _executor()
    ex._transfer_session().files["/dfs3b/u/data.bin"] = b"0123456789"
    assert ex.remote_size("/dfs3b/u/data.bin") == 10


# -- degrading instead of breaking ----------------------------------------------------


def test_password_session_falls_back_and_warns_once(dtn):
    """No key means a second Duo push, which mid-transfer is indistinguishable from a hang."""
    ex = _executor(pkey=None)
    ex.put_file("/local/a", "/dfs3b/a")
    ex.get_file("/dfs3b/b", "/local/b")

    assert ex._client.puts == [("/local/a", "/dfs3b/a")], "staging must still work"
    assert dtn.dialed == [], "must not attempt the transfer host without a key"
    warnings = [e for e in ex.events if e[0] == "warning"]
    assert len(warnings) == 1 and "not an SSH key" in warnings[0][2]


def test_unreachable_transfer_host_falls_back_and_stops_retrying(monkeypatch):
    from bioagent.gateway import ssh_gateway

    attempts: list[tuple[str, int]] = []

    def _refuse(addr, timeout=None):
        attempts.append(addr)
        raise OSError("connection refused")

    monkeypatch.setattr(ssh_gateway.socket, "create_connection", _refuse)

    ex = _executor()
    ex.put_file("/local/a", "/dfs3b/a")
    ex.put_file("/local/b", "/dfs3b/b")

    assert len(attempts) == 1, "a dead transfer host must not cost a timeout per file"
    assert ex._client.puts == [("/local/a", "/dfs3b/a"), ("/local/b", "/dfs3b/b")]
    warnings = [e for e in ex.events if e[0] == "warning"]
    assert len(warnings) == 1 and "not usable" in warnings[0][2]


def test_opting_out_uses_the_login_node_silently(dtn):
    """BIOAGENT_HPC_TRANSFER_HOST="" is a deliberate choice, so it gets no warning."""
    ex = _executor(transfer_host="")
    ex.put_file("/local/a", "/dfs3b/a")
    assert ex._client.puts == [("/local/a", "/dfs3b/a")]
    assert dtn.dialed == []
    assert [e for e in ex.events if e[0] == "warning"] == []


def test_close_tears_down_both_connections(dtn):
    ex = _executor()
    ex.put_file("/local/a", "/dfs3b/a")
    ex.close()
    assert dtn.clients[0].closed and ex._client.closed


# -- settings -------------------------------------------------------------------------


def test_transfer_host_defaults_to_the_rcic_data_transfer_node(monkeypatch):
    from bioagent.gateway.settings import HPCSettings

    monkeypatch.delenv("BIOAGENT_HPC_TRANSFER_HOST", raising=False)
    assert HPCSettings.from_env().transfer_host == "access-hpc3.rcic.uci.edu"


def test_transfer_host_is_overridable_and_can_be_disabled(monkeypatch):
    from bioagent.gateway.settings import HPCSettings

    monkeypatch.setenv("BIOAGENT_HPC_TRANSFER_HOST", "dtn.example.edu")
    assert HPCSettings.from_env().transfer_host == "dtn.example.edu"

    monkeypatch.setenv("BIOAGENT_HPC_TRANSFER_HOST", "")
    assert HPCSettings.from_env().transfer_host == ""
