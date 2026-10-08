"""A dropped SSH transport is re-established in place instead of failing the run.

Run 78a707cd79e9 (local gateway, 2026-10-03) had 7 steps accepted when the laptop's network
blipped. The vLLM call failed, ``_heal_vllm_session`` ran ``squeue`` to find the serve job, and
``SSHExecutor.exec`` raised "SSH session is no longer active." on that first command — the run ended
with chat_error although the GPU job, the Slurm jobs and HPC3 itself were all fine.

``SSHExecutor`` now redials with the session's SSH key and swaps the new transport in under the SAME
executor object, because the Slurm executors, job stores and tunnels a run built all hold that object.
These tests drive the real class over a fake network: no sockets reach HPC3.
"""

from __future__ import annotations

import socket
import threading
import types

import pytest

paramiko = pytest.importorskip("paramiko")

from aiscientist.gateway import ssh_gateway  # noqa: E402
from aiscientist.gateway.errors import GatewayError  # noqa: E402
from aiscientist.gateway.ssh_gateway import SSHExecutor  # noqa: E402


class _Channel:
    """One finished command: prints ``ok`` and exits 0, so exec's read loop ends without select()."""

    def __init__(self) -> None:
        self.command: str | None = None
        self._out = [b"ok\n"]

    def settimeout(self, _t) -> None:
        return None

    def exec_command(self, command: str) -> None:
        self.command = command

    def recv_ready(self) -> bool:
        return bool(self._out)

    def recv(self, _n: int) -> bytes:
        return self._out.pop(0)

    def recv_stderr_ready(self) -> bool:
        return False

    def recv_stderr(self, _n: int) -> bytes:
        return b""

    def exit_status_ready(self) -> bool:
        return True

    def recv_exit_status(self) -> int:
        return 0

    def close(self) -> None:
        return None


class _Net:
    """The fake network: every dial makes a ``_Transport``; tests decide how dials and auth go."""

    def __init__(self) -> None:
        self.transports: list[_Transport] = []
        self.dial_errors: list[Exception] = []     # consumed one per dial; empty = the dial succeeds
        self.refuse_key = False

    def dial(self, addr, timeout=None):
        if self.dial_errors:
            raise self.dial_errors.pop(0)
        return object()


class _Transport:
    net: _Net

    def __init__(self, _sock) -> None:
        self.authed = False
        self.active = True
        self.closed = False
        self.channels: list[_Channel] = []
        self.fail_next_open = False
        self.net.transports.append(self)

    def start_client(self, timeout=None) -> None:
        return None

    def auth_publickey(self, _user, _key) -> None:
        if self.net.refuse_key:
            raise paramiko.AuthenticationException("key refused")
        self.authed = True

    def auth_interactive(self, _user, _handler) -> None:
        self.authed = True

    def is_authenticated(self) -> bool:
        return self.authed

    def is_active(self) -> bool:
        return self.active and not self.closed

    def set_keepalive(self, _n) -> None:
        return None

    def open_session(self, timeout=None) -> _Channel:
        if self.fail_next_open:            # the link died between the liveness check and the open
            self.fail_next_open = False
            self.active = False
        if not self.is_active():
            raise paramiko.SSHException("SSH session not active")
        ch = _Channel()
        self.channels.append(ch)
        return ch

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def net(monkeypatch) -> _Net:
    n = _Net()
    _Transport.net = n
    monkeypatch.setattr(ssh_gateway.socket, "create_connection", n.dial)
    monkeypatch.setattr(paramiko, "Transport", _Transport)
    monkeypatch.setattr(SSHExecutor, "_load_key", staticmethod(lambda path, passphrase: f"KEY:{path}"))
    return n


def _session(*, key: bool = True) -> SSHExecutor:
    events: list[tuple[str, str, str]] = []
    ex = SSHExecutor("hpc3.example.edu", "tester",
                     key_path="/keys/id" if key else None, password=None if key else "pw",
                     emit=lambda *e: events.append(e), transfer_host="")
    ex._reconnect_delays = (0.0, 0.0, 0.0)   # the schedule is the class default; tests don't sleep
    ex.events = events
    return ex


# -- the fix ------------------------------------------------------------------------------


def test_a_dropped_key_session_reconnects_and_runs_the_command(net):
    ex = _session()
    net.transports[0].active = False                 # the network blipped

    res = ex.exec("squeue --me")

    assert res.ok and res.stdout == "ok\n"
    assert len(net.transports) == 2, "exactly one redial"
    assert net.transports[1].channels[0].command == "squeue --me", "ran on the NEW transport"
    assert net.transports[0].closed, "the dead transport is closed, not leaked"
    assert ex._client.get_transport() is net.transports[1]
    assert any(e[1] == "ssh_reconnect" and e[0] == "success" for e in ex.events)


def test_a_live_session_never_redials(net):
    ex = _session()
    ex.exec("true")
    ex.exec("true")
    assert len(net.transports) == 1


def test_network_failures_are_retried_until_one_dial_works(net):
    ex = _session()
    net.transports[0].active = False
    net.dial_errors = [OSError("Can't assign requested address"), socket.timeout("timed out")]

    assert ex.exec("squeue").ok
    attempts = [e for e in ex.events if e[1] == "ssh_reconnect" and e[0] == "warning"]
    assert len(attempts) == 3, "two failed dials, then the one that worked"


def test_a_network_that_stays_down_fails_with_a_clear_error(net):
    ex = _session()
    net.transports[0].active = False
    net.dial_errors = [OSError("network is down")] * 3

    with pytest.raises(GatewayError) as err:
        ex.exec("squeue")

    assert "3 attempts to reconnect" in err.value.message
    assert "network is down" in str(err.value.detail)


def test_hpc3_refusing_the_key_is_final_not_retried(net):
    ex = _session()
    net.transports[0].active = False
    net.refuse_key = True

    with pytest.raises(GatewayError) as err:
        ex.exec("squeue")

    assert err.value.stage == "ssh_auth"
    assert len(net.transports) == 2, "an auth refusal does not burn the remaining attempts"


def test_a_password_session_without_a_key_does_not_fire_a_duo_push(net):
    """Re-authenticating a password session means another Duo push nobody is watching for."""
    ex = _session(key=False)
    net.transports[0].active = False

    with pytest.raises(GatewayError) as err:
        ex.exec("squeue")

    assert "no saved SSH key" in err.value.message
    assert len(net.transports) == 1, "nothing was dialled"


def test_a_password_session_reconnects_with_an_armed_saved_key(net):
    ex = _session(key=False)
    assert ex.arm_reconnect_key("/keys/saved") is True
    net.transports[0].active = False

    assert ex.exec("squeue").ok
    assert len(net.transports) == 2


def test_an_unloadable_key_is_not_armed(net, monkeypatch):
    def _bad(path, passphrase):
        raise GatewayError("Could not load SSH key", stage="ssh_auth")

    ex = _session(key=False)
    monkeypatch.setattr(SSHExecutor, "_load_key", staticmethod(_bad))
    assert ex.arm_reconnect_key("/keys/broken") is False
    assert ex._reconnect_pkey is None


def test_a_drop_between_the_check_and_the_channel_open_is_retried(net):
    """Nothing was sent yet, so sending the command once more is safe."""
    ex = _session()
    net.transports[0].fail_next_open = True

    assert ex.exec("sbatch job.sh").ok
    assert net.transports[1].channels[0].command == "sbatch job.sh"


def test_a_failed_open_on_a_live_transport_is_not_retried(net):
    """A refusal on a healthy transport (MaxSessions, say) is not a drop: no reconnect, no resend."""
    ex = _session()

    def _refuse(timeout=None):
        raise paramiko.ChannelException(1, "Administratively prohibited")

    net.transports[0].open_session = _refuse
    with pytest.raises(GatewayError) as err:
        ex.exec("sbatch job.sh")
    assert "Failed to run remote command" in err.value.message
    assert len(net.transports) == 1


def test_a_thread_that_waited_out_a_failed_round_does_not_start_another(net, monkeypatch):
    """The run thread and the GPU watchdog can hit a dead transport together. The second must
    report the first one's failure, not make the run wait through a second full round."""
    ex = _session()
    net.transports[0].active = False

    class _SignallingLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.someone_waits = threading.Event()

        def __enter__(self):
            if not self._lock.acquire(blocking=False):
                self.someone_waits.set()
                self._lock.acquire()
            return self

        def __exit__(self, *_exc) -> None:
            self._lock.release()

    lock = _SignallingLock()
    ex._reconnect_lock = lock
    inside = threading.Event()
    rounds: list[int] = []

    def _slow_failing_round() -> None:
        rounds.append(1)
        inside.set()
        assert lock.someone_waits.wait(5), "the second thread never reached the lock"
        raise GatewayError("SSH session is no longer active, and 3 attempts failed.",
                           stage="ssh_exec")

    monkeypatch.setattr(ex, "_reconnect", _slow_failing_round)
    errors: list[GatewayError] = []

    def _call() -> None:
        try:
            ex.exec("squeue")
        except GatewayError as exc:
            errors.append(exc)

    first = threading.Thread(target=_call)
    first.start()
    assert inside.wait(5)
    second = threading.Thread(target=_call)
    second.start()
    first.join(5)
    second.join(5)

    assert len(rounds) == 1, "only one round of reconnect attempts"
    assert len(errors) == 2 and all("3 attempts failed" in e.message for e in errors)


# -- tunnels ride the reconnected session ------------------------------------------------


def test_an_open_tunnel_forwards_over_the_new_transport_after_a_reconnect(net):
    """Whatever still holds the old local port (a run's tool context, a literature job's
    base_url) must keep working once the session is back."""
    ex = _session()
    opened: list[tuple[str, object]] = []
    remotes: list[socket.socket] = []

    def _channel_factory(label: str):
        def _open_channel(kind, dest, src):
            near, far = socket.socketpair()
            opened.append((label, dest))
            remotes.append(far)
            return near
        return _open_channel

    net.transports[0].open_channel = _channel_factory("old")
    port = ex.open_tunnel("gpu-node", 8000)
    try:
        def _round_trip(payload: bytes) -> bytes:
            # A raw socket: the fixture fakes socket.create_connection for the SSH dials.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(("127.0.0.1", port))
                client.sendall(payload)
                far = _wait_for(lambda: remotes[-1] if len(remotes) > len(seen) else None)
                seen.append(far)
                far.settimeout(5)
                return far.recv(64)

        seen: list[socket.socket] = []
        assert _round_trip(b"before") == b"before"

        net.transports[0].active = False
        ex.exec("true")                                  # reconnects
        net.transports[1].open_channel = _channel_factory("new")

        assert _round_trip(b"after") == b"after"
        assert [label for label, _ in opened] == ["old", "new"]
        assert opened[1][1] == ("gpu-node", 8000)
    finally:
        for far in remotes:
            far.close()
        ex.close()


def _wait_for(probe, timeout: float = 5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = probe()
        if got is not None:
            return got
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the tunnel to open a channel")


# -- Slurm polls survive a drop mid-command --------------------------------------------------


def test_a_slurm_poll_that_fails_under_a_drop_is_sent_once_more():
    """The step in run 78a707cd79e9 died on one failed ``squeue -j`` while its job was healthy."""
    from aiscientist.gateway import slurm_job
    from aiscientist.gateway.executor import ExecResult

    sent: list[str] = []

    class _Flaky:
        def exec(self, command, timeout=60.0):
            sent.append(command)
            if len(sent) == 1:
                raise GatewayError(f"Failed to run remote command: {command}", stage="ssh_exec")
            return ExecResult(command=command, exit_status=0, stdout="R|hpc3-14-01\n", stderr="",
                              duration_ms=1)

    assert slurm_job._queue_state(_Flaky(), "57747904") == ("R", "hpc3-14-01")
    assert len(sent) == 2 and sent[0] == sent[1]


def test_a_slurm_poll_that_fails_twice_still_raises():
    from aiscientist.gateway import slurm_job

    class _Down:
        def exec(self, command, timeout=60.0):
            raise GatewayError("SSH session is no longer active.", stage="ssh_exec")

    with pytest.raises(GatewayError):
        slurm_job._terminal_state(_Down(), "57747904")


# -- the gateway side ------------------------------------------------------------------------


def test_a_password_login_arms_the_users_saved_key_for_this_host(monkeypatch):
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw
    from aiscientist.gateway import ssh_credentials

    rows = [{"id": "old", "host": "hpc3.example.edu", "hpc_user": "tester", "label": "a"},
            {"id": "other-host", "host": "elsewhere", "hpc_user": "tester", "label": "b"},
            {"id": "new", "host": "hpc3.example.edu", "hpc_user": "tester", "label": "c"}]
    monkeypatch.setattr(ssh_credentials, "list_credentials", lambda owner: rows)
    monkeypatch.setattr(ssh_credentials, "get_credential",
                        lambda owner, cid: {"id": cid, "key_path": f"/store/{cid}.key"})
    armed: list[tuple[str, str | None]] = []
    executor = types.SimpleNamespace(
        arm_reconnect_key=lambda path, passphrase: armed.append((path, passphrase)) or True)
    conn = types.SimpleNamespace(owner="tester", settings=types.SimpleNamespace(host="hpc3.example.edu"),
                                 executor=executor)

    gw._arm_reconnect_key(conn, "tester", None)

    assert armed == [("/store/new.key", None)], "the newest key for THIS host, and only one"


def test_heal_keeps_the_existing_tunnel_when_the_reconnected_session_carries_it(monkeypatch):
    """Same serve job + the old local port answers again → no second forward (which a fixed
    AISCIENTIST_LOCAL_TUNNEL_PORT could not even bind)."""
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw

    tunnels: list[tuple] = []
    alloc = types.SimpleNamespace(node="gpu-node", port=8000)
    conn = types.SimpleNamespace(
        mock=False, tunnel_port=5001, settings=types.SimpleNamespace(local_tunnel_port=0),
        gpu_lock=threading.Lock(), alloc=alloc,
        executor=types.SimpleNamespace(open_tunnel=lambda *a, **k: tunnels.append(a) or 5999),
        emit_fn=lambda: (lambda *a, **k: None))
    probes = iter([False, True])                         # dead before the heal, alive after
    monkeypatch.setattr(gw, "_vllm_reachable", lambda c: next(probes))
    monkeypatch.setattr(gw.gpu, "ensure_serve_job",
                        lambda executor, settings, emit, **kw: types.SimpleNamespace(node="gpu-node", port=8000))
    monkeypatch.setattr(gw, "_wait_for_server", lambda *a, **k: None)

    gw._heal_vllm_session(conn)

    assert tunnels == [] and conn.tunnel_port == 5001
