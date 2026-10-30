"""The session's CPU worker is held only while the agent uses it (RCHENLAB/AiScientist issue 23).

The holder job used to run ``sleep infinity`` until the session disconnected or its 8-hour wall
limit, charging 4 CPUs and 16 GB to the lab's account whether or not anything ran. Pinned here: the
gateway releases it after the idle timeout but never under a running command, and on logout and
server stop; a new session cancels the user's leftover holders except those a live session holds.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app          # noqa: E402
from aiscientist.gateway import worker as worker_mod    # noqa: E402
from aiscientist.gateway.executor import ExecResult     # noqa: E402
from aiscientist.gateway.settings import HPCSettings    # noqa: E402


class FakeExec:
    username = "alice"
    host = "hpc3"

    def __init__(self) -> None:
        self.cmds: list[str] = []

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.cmds.append(command)
        return ExecResult(command, 0, "", "")

    def scancels(self) -> list[str]:
        return [c for c in self.cmds if c.startswith("scancel")]


@pytest.fixture
def conns():
    made: list[gw_app.Connection] = []

    def make(*, user_id=1, idle_s=900, mock=False) -> gw_app.Connection:
        c = gw_app.Connection(HPCSettings(worker_enabled=True, worker_idle_timeout_s=idle_s),
                              mock=mock, loop=asyncio.new_event_loop(), username="alice")
        c.executor = FakeExec()
        c.app_user_id = user_id
        gw_app.CONNECTIONS[c.id] = c
        made.append(c)
        return c

    yield make
    for c in made:
        gw_app.CONNECTIONS.pop(c.id, None)


def _hold(conn, job_id="4242", idle_for=0.0):
    conn.worker_alloc = worker_mod.WorkerAllocation(job_id, "hpc3-20-01")
    conn.worker_last_used = time.monotonic() - idle_for


# --- the idle release --------------------------------------------------------


def test_an_idle_worker_is_released_after_the_timeout(conns):
    c = conns()
    _hold(c, idle_for=16 * 60)
    assert gw_app._release_idle_worker(c, 900) is True
    assert c.worker_alloc is None and c.executor.scancels() == ["scancel 4242"]


def test_a_recently_used_worker_is_kept(conns):
    c = conns()
    _hold(c, idle_for=60)
    assert gw_app._release_idle_worker(c, 900) is False
    assert c.worker_alloc is not None and c.executor.scancels() == []


def test_a_worker_is_never_released_under_a_running_command(conns):
    """A 30-minute pip install stamps its use only when it STARTS and ENDS; the count is what
    stops the release in between."""
    c = conns()
    _hold(c, idle_for=3600)
    c.worker_busy = 1
    assert gw_app._release_idle_worker(c, 900) is False
    assert gw_app._release_idle_worker(c, 0.0) is False, "not even on logout"
    assert c.executor.scancels() == []


def test_a_worker_being_acquired_is_left_to_the_next_pass(conns):
    c = conns()
    _hold(c, idle_for=3600)
    with c.worker_lock:
        assert gw_app._release_idle_worker(c, 900) is False
    assert c.executor.scancels() == []


def test_the_release_message_says_why(conns):
    c = conns()
    said = []
    c.emit = lambda level, stage, msg, **kw: said.append((stage, msg))
    _hold(c, idle_for=20 * 60)
    gw_app._release_idle_worker(c, 900)
    assert any("Released worker node hpc3-20-01 (job 4242) after 20 min without a command" in m
               for _s, m in said)


def test_a_command_counts_as_running_and_stamps_its_end(conns):
    c = conns()
    _hold(c, idle_for=3600)
    seen = {}

    def run_on_worker(executor, alloc, command, *, timeout_s):
        seen["busy"] = c.worker_busy
        return ExecResult(command, 0, "ok", "")

    orig = worker_mod.run_on_worker
    worker_mod.run_on_worker = run_on_worker
    try:
        gw_app._run_on_session_worker(c.worker_alloc, "ls", 60)
    finally:
        worker_mod.run_on_worker = orig
    assert seen["busy"] == 1 and c.worker_busy == 0
    assert time.monotonic() - c.worker_last_used < 5
    assert gw_app._release_idle_worker(c, 900) is False


def test_handing_out_the_worker_stamps_its_use(conns, monkeypatch):
    """Stamped under the lock, so the idle release cannot cancel the holder between the provider's
    answer and the command it was asked for."""
    c = conns()
    _hold(c, idle_for=3600)
    monkeypatch.setattr(worker_mod, "worker_is_alive", lambda ex, alloc: True)
    assert gw_app._ensure_session_worker(c) is c.worker_alloc
    assert gw_app._release_idle_worker(c, 900) is False


def test_an_ended_worker_is_replaced_on_the_next_command(conns, monkeypatch):
    c = conns()
    _hold(c, job_id="1", idle_for=10)
    monkeypatch.setattr(worker_mod, "worker_is_alive", lambda ex, alloc: False)
    monkeypatch.setattr(worker_mod, "ensure_worker",
                        lambda ex, st, emit, scratch_dir: worker_mod.WorkerAllocation("2", "n2"))
    assert gw_app._ensure_session_worker(c).job_id == "2"


def test_the_idle_timeout_defaults_to_fifteen_minutes(monkeypatch):
    monkeypatch.delenv("AISCIENTIST_WORKER_IDLE_TIMEOUT_S", raising=False)
    assert HPCSettings.from_env().worker_idle_timeout_s == 900
    monkeypatch.setenv("AISCIENTIST_WORKER_IDLE_TIMEOUT_S", "5")
    assert HPCSettings.from_env().worker_idle_timeout_s == 60, "never below a minute"


# --- logout, server stop, a new session ---------------------------------------


def test_logout_releases_only_that_accounts_worker(conns):
    mine, theirs = conns(user_id=1), conns(user_id=2)
    _hold(mine, job_id="11", idle_for=5)
    _hold(theirs, job_id="22", idle_for=5)

    gw_app._release_workers_of(type("U", (), {"id": 1})())
    assert mine.worker_alloc is None and mine.executor.scancels() == ["scancel 11"]
    assert theirs.worker_alloc is not None and theirs.executor.scancels() == []


def test_the_logout_route_runs_its_hooks_and_still_logs_out(monkeypatch):
    auth_routes = pytest.importorskip("aiscientist.gateway.auth_routes")
    user = type("U", (), {"id": 7})()
    monkeypatch.setattr(auth_routes, "current_user", lambda request: user)
    got = []

    def boom(u):
        raise RuntimeError("ssh gone")

    monkeypatch.setattr(auth_routes, "LOGOUT_HOOKS", [got.append, boom])

    class Resp:
        def delete_cookie(self, *a, **k):
            got.append("cookie cleared")

    assert auth_routes.logout(object(), Resp()) == {"status": "ok"}
    assert got == [user, "cookie cleared"], "a failing hook never blocks a logout"


def test_server_stop_releases_every_sessions_worker(conns):
    a, b, m = conns(user_id=1), conns(user_id=2), conns(user_id=3, mock=True)
    _hold(a, job_id="11")
    _hold(b, job_id="22")
    _hold(m, job_id="33")
    gw_app._release_session_workers()
    assert a.executor.scancels() == ["scancel 11"] and b.executor.scancels() == ["scancel 22"]
    assert a.worker_alloc is None and b.worker_alloc is None
    assert m.executor.scancels() == [], "a mock session holds nothing on HPC3"


def test_a_new_session_sweeps_leftovers_but_keeps_live_sessions_holders(conns, monkeypatch):
    live = conns(user_id=1)
    _hold(live, job_id="11")
    new = conns(user_id=2)
    seen = {}

    def sweep(executor, *, log_dir, keep, stale_after_s, emit=None, username=""):
        seen.update(log_dir=log_dir, keep=set(keep), stale_after_s=stale_after_s)
        return []

    monkeypatch.setattr(worker_mod, "sweep_stale_workers", sweep)
    gw_app._sweep_leftover_workers(new)
    assert seen["keep"] == {"11"} and seen["stale_after_s"] == 900
    # Each session keeps its worker files in its own folder: the check looks in all of the user's.
    assert seen["log_dir"] == f"{new.settings.shared_root}/sessions/*/jobs/worker"


def test_the_sweep_never_breaks_a_login(conns, monkeypatch):
    c = conns()

    def broken(*a, **k):
        raise RuntimeError("squeue timed out")

    monkeypatch.setattr(worker_mod, "sweep_stale_workers", broken)
    gw_app._sweep_leftover_workers(c)          # must not raise


def test_concurrent_commands_never_submit_two_holders(conns, monkeypatch):
    c = conns()
    submitted = []

    def ensure(ex, st, emit, scratch_dir):
        time.sleep(0.05)
        submitted.append(1)
        return worker_mod.WorkerAllocation(str(len(submitted)), "n")

    monkeypatch.setattr(worker_mod, "ensure_worker", ensure)
    monkeypatch.setattr(worker_mod, "worker_is_alive", lambda ex, alloc: True)
    threads = [threading.Thread(target=gw_app._ensure_session_worker, args=(c,)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert submitted == [1]


def test_a_heartbeat_in_another_sessions_folder_keeps_its_worker():
    """The holder of an earlier session (another process, say) is still in use when its heartbeat,
    in that session's own folder, is fresh: the new session must not cancel it."""
    from aiscientist.gateway import worker as wm

    class _Ex:
        username = "alice"

        def __init__(self):
            self.commands = []

        def exec(self, command, timeout=60.0):
            self.commands.append(command)
            if command.startswith("squeue"):
                out = "11|1:00:00\n12|2:00:00\n"
            elif command.startswith("date"):
                out = ("1000000\n/r/sessions/s-old/jobs/worker/aiscientist-worker-alice-11.alive|999990\n"
                       "/r/sessions/s-older/jobs/worker/aiscientist-worker-alice-12.alive|100\n")
            else:
                out = ""
            return type("R", (), {"out": out, "ok": True, "stdout": out})()

    ex = _Ex()
    cancelled = wm.sweep_stale_workers(ex, log_dir="/r/sessions/*/jobs/worker", stale_after_s=900)
    assert cancelled == ["12"]
    stat = next(c for c in ex.commands if c.startswith("date"))
    assert "/r/sessions/*/jobs/worker/aiscientist-worker-alice-11.alive" in stat
