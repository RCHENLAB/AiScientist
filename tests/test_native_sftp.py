"""The OpenSSH fast path of SSHExecutor.put_file_resumable, offline.

Measured 2026-10-08 from the eyeserver: paramiko 101 MB/s, the OpenSSH client 327 MB/s (438 with
aes128-gcm), four OpenSSH copies 763 MB/s together. So bulk copies run ``sftp`` when it can
authenticate unattended, and paramiko otherwise. Here a fake ``sftp`` process and a fake SFTP channel
stand in for both, so what is pinned is the choice, the batch command, resume, and the fallback.
"""

from __future__ import annotations

import time
import types

import pytest

pytest.importorskip("paramiko")

from aiscientist.gateway import ssh_gateway  # noqa: E402
from aiscientist.gateway.ssh_gateway import SSHExecutor  # noqa: E402


class _File:
    def __init__(self, store: dict, path: str, mode: str) -> None:
        self.store, self.path, self.pos = store, path, 0
        if "w" in mode:
            store[path] = [bytearray(), time.time()]

    def set_pipelined(self, _on) -> None:
        pass

    def seek(self, n: int) -> None:
        self.pos = n

    def write(self, b: bytes) -> None:
        buf = self.store[self.path][0]
        buf[self.pos:self.pos + len(b)] = b
        self.pos += len(b)
        self.store[self.path][1] = time.time()

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


class _SFTP:
    """A paramiko SFTPClient over a dict: path -> [bytes, mtime]."""

    def __init__(self, store: dict) -> None:
        self.store = store

    def stat(self, path: str):
        if path not in self.store:
            raise OSError("no such file")
        data, mtime = self.store[path]
        return types.SimpleNamespace(st_size=len(data), st_mtime=int(mtime))

    def open(self, path: str, mode: str = "r") -> _File:
        return _File(self.store, path, mode)

    def posix_rename(self, a: str, b: str) -> None:
        self.store[b] = self.store.pop(a)

    def remove(self, path: str) -> None:
        self.store.pop(path, None)

    def rename(self, a: str, b: str) -> None:
        self.posix_rename(a, b)

    def close(self) -> None:
        pass


def _executor(store: dict, key_path: str | None, *, encrypted: bool = False, passphrase: str | None = None,
              transfer_host: str | None = "access-hpc3.rcic.uci.edu") -> SSHExecutor:
    ex = object.__new__(SSHExecutor)
    ex.host, ex.username, ex.port, ex.group = "hpc3.rcic.uci.edu", "tester", 22, None
    ex.events = []
    ex._emit = lambda level, stage, msg: ex.events.append((level, stage, msg))
    ex.transfer_host = transfer_host
    ex._connect_timeout = 30.0
    ex._transfer_disabled = False
    ex._transfer_key_path = key_path
    ex._transfer_key_encrypted = encrypted
    ex._transfer_key_passphrase = passphrase
    ex._agent, ex._agent_dir, ex._agent_failed = None, None, False
    ex._agent_lock = ssh_gateway.threading.Lock()
    ex._tunnels, ex._transfer_client = [], None
    ex._client = types.SimpleNamespace(close=lambda: None)
    ex.execs = []
    ex.exec = lambda cmd, timeout=60.0: ex.execs.append(cmd)
    ex._open_sftp = lambda: _SFTP(store)
    return ex


@pytest.fixture()
def key(tmp_path, monkeypatch):
    k = tmp_path / "id_ed25519"
    k.write_text("PRIVATE")
    monkeypatch.setattr(ssh_gateway.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AISCIENTIST_NATIVE_SFTP", raising=False)
    return str(k)


class _FakeSftpProcess:
    """Plays the OpenSSH client: reads the batch line and copies (all, or the first ``stop_at``
    bytes) from the local file into the fake remote store."""

    calls: list[dict] = []
    stop_at: int | None = None

    def __init__(self, store: dict):
        self.store = store

    def __call__(self, argv, stdin=None, stdout=None, stderr=None):
        proc = types.SimpleNamespace(argv=argv, returncode=None, killed=False)
        outer = self

        class _Stdin:
            def write(self, b: bytes) -> None:
                proc.batch = b.decode()

            def close(self) -> None:
                verb_append = proc.batch.startswith("put -a ")
                rest = proc.batch[len("put -a "):] if verb_append else proc.batch[len("put "):]
                local, remote = [x.strip('"') for x in rest.strip().split('" "')]
                data = open(local, "rb").read()
                if outer.stop_at is not None:
                    data = data[:outer.stop_at]
                if verb_append and remote in outer.store:
                    buf = outer.store[remote][0]
                    buf[len(buf):] = data[len(buf):]
                else:
                    outer.store[remote] = [bytearray(data), time.time()]
                proc.returncode = 1 if outer.stop_at is not None else 0
                outer.calls.append({"argv": argv, "batch": proc.batch})

        proc.stdin = _Stdin()
        proc.wait = lambda timeout=None: proc.returncode
        proc.kill = lambda: setattr(proc, "killed", True)
        return proc


@pytest.fixture()
def fake_sftp(monkeypatch):
    store: dict = {}
    fake = _FakeSftpProcess(store)
    _FakeSftpProcess.calls = []
    _FakeSftpProcess.stop_at = None
    monkeypatch.setattr(ssh_gateway.subprocess, "Popen", fake)
    return store, fake


def _local(tmp_path, data: bytes = b"0123456789" * 1000, name: str = "big.h5ad") -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# -- when the fast path is used ----------------------------------------------------------------

def test_argv_runs_sftp_unattended_against_the_transfer_host(key):
    argv = _executor({}, key)._native_sftp_argv()
    assert argv[0] == "/usr/bin/sftp" and argv[-1] == "tester@access-hpc3.rcic.uci.edu"
    joined = " ".join(argv)
    for opt in ("-b -", f"-i {key}", "BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none",
                "StrictHostKeyChecking=accept-new", "aes128-gcm@openssh.com"):
        assert opt in joined
    assert "UserKnownHostsFile=" in joined and "state/ssh_known_hosts" in joined


@pytest.mark.parametrize("why", ["no_transfer_host", "password_session", "encrypted_key", "disabled_by_env", "no_binary"])
def test_argv_is_none_when_the_client_cannot_run_unattended(key, monkeypatch, why):
    ex = _executor({}, None if why == "password_session" else key, encrypted=(why == "encrypted_key"),
                   transfer_host=None if why == "no_transfer_host" else "access-hpc3.rcic.uci.edu")
    if why == "disabled_by_env":
        monkeypatch.setenv("AISCIENTIST_NATIVE_SFTP", "0")
    if why == "no_binary":
        monkeypatch.setattr(ssh_gateway.shutil, "which", lambda name: None)
    assert ex._native_sftp_argv() is None
    assert ex.native_transfer_available() is False


# -- a passphrase-protected key: loaded once into an agent that is this session's alone ---------
# These run the real ssh-keygen / ssh-agent / ssh-add; only the server is absent.

@pytest.fixture()
def locked_key(tmp_path, monkeypatch):
    import shutil
    import subprocess
    if not all(shutil.which(b) for b in ("ssh-keygen", "ssh-agent", "ssh-add", "sftp")):
        pytest.skip("the OpenSSH client tools are not installed")
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AISCIENTIST_NATIVE_SFTP", raising=False)
    path = tmp_path / "id_locked"
    made = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "open sesame 'q'", "-C", "test", "-f", str(path)],
                          capture_output=True)
    if made.returncode != 0:
        pytest.skip("ssh-keygen could not make a test key")
    return str(path), "open sesame 'q'"


def _agent_keys(sock: str) -> str:
    import os
    import subprocess
    return subprocess.run(["ssh-add", "-L"], env={**os.environ, "SSH_AUTH_SOCK": sock},
                          capture_output=True, text=True).stdout


def test_a_passphrase_protected_key_is_loaded_into_an_agent_of_its_own(locked_key):
    import os
    key, passphrase = locked_key
    ex = _executor({}, key, encrypted=True, passphrase=passphrase)
    try:
        argv = ex._native_sftp_argv()
        assert argv is not None and "-i" not in argv
        sock = next(a.split("=", 1)[1] for a in argv if a.startswith("IdentityAgent="))
        public = open(key + ".pub").read().split()[1]
        assert public in _agent_keys(sock)                                # the agent holds exactly this key
        home = os.path.dirname(sock)
        assert os.stat(home).st_mode & 0o777 == 0o700                     # nobody else can reach the socket
        assert os.listdir(home) == ["agent.sock"]                         # the askpass helper is gone
        first = ex._agent.pid
        assert ex._native_sftp_argv() == argv and ex._agent.pid == first  # one agent for the session
        assert ex.native_transfer_available() is True
    finally:
        ex.close()
    assert ex._agent is None and not os.path.exists(home)                 # it ends with the session


def test_a_wrong_passphrase_falls_back_to_the_built_in_client_once(locked_key):
    key, _passphrase = locked_key
    ex = _executor({}, key, encrypted=True, passphrase="not it")
    try:
        assert ex._native_sftp_argv() is None
        assert ex._agent is None and ex._agent_dir is None                # nothing left running
        assert [e for e in ex.events if e[0] == "warning" and "could not be loaded" in e[2]]
        assert ex._native_sftp_argv() is None
        assert len([e for e in ex.events if e[0] == "warning"]) == 1      # told once, not per file
    finally:
        ex.close()


def test_an_agent_that_died_is_started_again(locked_key):
    key, passphrase = locked_key
    ex = _executor({}, key, encrypted=True, passphrase=passphrase)
    try:
        first = ex._native_sftp_argv()
        ex._agent.kill()
        ex._agent.wait()
        again = ex._native_sftp_argv()
        assert again is not None and again != first                       # a new socket, the key loaded again
        sock = next(a.split("=", 1)[1] for a in again if a.startswith("IdentityAgent="))
        assert open(key + ".pub").read().split()[1] in _agent_keys(sock)
    finally:
        ex.close()


# -- what it does ------------------------------------------------------------------------------

def test_a_fresh_copy_puts_to_part_then_renames(tmp_path, key, fake_sftp):
    store, fake = fake_sftp
    src = _local(tmp_path)
    _executor(store, key).put_file_resumable(src, "/dfs/u/big.h5ad")
    assert fake.calls[0]["batch"] == f'put "{src}" "/dfs/u/big.h5ad.part"\n'
    assert bytes(store["/dfs/u/big.h5ad"][0]) == open(src, "rb").read()
    assert "/dfs/u/big.h5ad.part" not in store


def test_a_partial_copy_resumes_with_put_a(tmp_path, key, fake_sftp):
    store, fake = fake_sftp
    src = _local(tmp_path)
    store["/dfs/u/big.h5ad.part"] = [bytearray(open(src, "rb").read()[:3000]), time.time() + 5]
    _executor(store, key).put_file_resumable(src, "/dfs/u/big.h5ad")
    assert fake.calls[0]["batch"].startswith("put -a ")
    assert bytes(store["/dfs/u/big.h5ad"][0]) == open(src, "rb").read()


def test_a_part_older_than_the_source_is_restarted_not_appended_to(tmp_path, key, fake_sftp):
    store, fake = fake_sftp
    store["/dfs/u/big.h5ad.part"] = [bytearray(b"OLD-VERSION"), time.time() - 3600]
    src = _local(tmp_path)
    _executor(store, key).put_file_resumable(src, "/dfs/u/big.h5ad")
    assert fake.calls[0]["batch"].startswith("put \"")
    assert bytes(store["/dfs/u/big.h5ad"][0]) == open(src, "rb").read()


def test_when_the_client_fails_paramiko_finishes_from_where_it_stopped(tmp_path, key, fake_sftp):
    store, fake = fake_sftp
    fake.stop_at = 4000                       # the OpenSSH client dies after 4000 bytes
    src = _local(tmp_path)
    ex = _executor(store, key)
    ex.put_file_resumable(src, "/dfs/u/big.h5ad")
    assert bytes(store["/dfs/u/big.h5ad"][0]) == open(src, "rb").read()
    assert any("continuing with paramiko" in m for _l, _s, m in ex.events)


def test_a_glob_character_in_the_path_goes_through_paramiko(tmp_path, key, fake_sftp):
    store, fake = fake_sftp
    src = _local(tmp_path, name="run[1].h5ad")
    _executor(store, key).put_file_resumable(src, "/dfs/u/run[1].h5ad")
    assert fake.calls == []
    assert bytes(store["/dfs/u/run[1].h5ad"][0]) == open(src, "rb").read()


def test_without_a_key_file_paramiko_does_the_copy(tmp_path, fake_sftp):
    store, fake = fake_sftp
    src = _local(tmp_path)
    _executor(store, None).put_file_resumable(src, "/dfs/u/big.h5ad")
    assert fake.calls == []
    assert bytes(store["/dfs/u/big.h5ad"][0]) == open(src, "rb").read()


def test_progress_reaches_the_total(tmp_path, key, fake_sftp):
    store, _fake = fake_sftp
    src = _local(tmp_path)
    seen = []
    _executor(store, key).put_file_resumable(src, "/dfs/u/big.h5ad", progress=lambda s, t: seen.append((s, t)))
    assert seen[-1] == (10000, 10000)


def test_sftp_quote_escapes_quotes_and_backslashes():
    assert ssh_gateway._sftp_quote('a "b"\\c') == '"a \\"b\\"\\\\c"'


def test_cancelled_server_copy_persists_native_transfer_timing(tmp_path, key, monkeypatch):
    """The real staging -> resumable put -> native polling path retains cancellation, not timeout."""
    import json
    import subprocess
    from aiscientist.gateway import run_monitor, run_timing, server_data

    clock = [100.0]
    monkeypatch.setattr(run_timing.time, "monotonic", lambda: clock[0])
    source = tmp_path / "private-input.bam"
    source.write_bytes(b"x" * 100)
    store = {}
    executor = _executor(store, key)
    proc = types.SimpleNamespace(returncode=None, killed=False)
    proc.stdin = types.SimpleNamespace(write=lambda _: None, close=lambda: None)

    def wait(timeout=None):
        if timeout is None:
            return -9
        clock[0] += 2
        store["/private/dataset.bam.part"] = [bytearray(b"x" * 20), time.time()]
        raise subprocess.TimeoutExpired("command-not-for-the-timeline", timeout)

    proc.wait = wait
    proc.kill = lambda: setattr(proc, "killed", True)
    monkeypatch.setattr(ssh_gateway.subprocess, "Popen", lambda *a, **k: proc)
    monitor = run_monitor.RunMonitor(run_id="transfer-run", conversation_id="101")
    path = tmp_path / "timing.jsonl"
    monitor.bind_timing(path)
    scan = server_data.Scan(source, tmp_path, False, copy=[(source.name, 100)])
    with run_monitor.bind_monitor(monitor), pytest.raises(server_data.TransferCancelled):
        server_data.sync_to_hpc(executor, scan, "/private/dataset.bam",
                                should_cancel=lambda: clock[0] >= 106)
    timeline = run_monitor.read_timing(path)
    spans = timeline["spans"]
    assert len(spans) == 3                 # staging, full upload, native attempt overlap
    assert {s["stage"] for s in spans} == {"data_staging", "data_transfer"}
    assert {s["duration_seconds"] for s in spans} == {6.0}
    assert {s["outcome"] for s in spans} == {"cancelled"}
    assert all(s["ended_at"] and not s["partial"] for s in spans)
    assert proc.killed and "/private/dataset.bam.part" in store
    serialized = json.dumps(timeline)
    assert "private-input" not in serialized and "/private/" not in serialized
    assert key not in serialized and "command-not-for-the-timeline" not in serialized
    assert timeline["run_id"] == "transfer-run"


def test_native_fallback_timing_is_distinct_from_successful_upload(tmp_path, key, fake_sftp):
    from aiscientist.gateway import run_monitor

    store, fake = fake_sftp
    fake.stop_at = 4000
    monitor = run_monitor.RunMonitor(run_id="fallback-run")
    path = tmp_path / "timing.jsonl"
    monitor.bind_timing(path)
    with run_monitor.bind_monitor(monitor):
        _executor(store, key).put_file_resumable(_local(tmp_path), "/dfs/u/big.h5ad")
    spans = run_monitor.read_timing(path)["spans"]
    assert [(s["metadata"]["source"], s["outcome"]) for s in spans] == [
        ("sftp_upload", "success"), ("native_sftp", "fallback")]
    assert len(store["/dfs/u/big.h5ad"][0]) == 10000
