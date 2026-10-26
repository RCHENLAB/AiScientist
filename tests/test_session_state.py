"""A compute session's state, shown in the console (RCHENLAB/AiScientist issue 6).

What this file pins down:

1. **The state follows what is in flight**: staging-out over staging-in over running, then failed
   (a stage-out failure no later copy resolved), else open; an ended session is cleaning-up while
   its cleanup job is queued, closed once nothing is left, failed when its cleanup or a result's
   copy home did not succeed.
2. **Every copy reports its progress and its failure**, and progress is pushed at most once a
   second while a change of state always is.
3. **A run whose result is not safely on the server is never cleaned up with the session.**
4. **The account's recent sessions are kept**, including one that ended without a disconnect, and
   a queued cleanup is settled from Slurm's record and the cleanup log at the next connect.
5. **The gateway feeds it**: copies to HPC3 at run start, copies home, the disconnect, and the
   ``/api/sessions`` list.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiscientist.gateway import session_state as ss  # noqa: E402
from aiscientist.gateway.executor import ExecResult  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

ROOT = "/dfs3b/lab/software/AiScientist"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _tracker(*, running=False, clock=None, pushes=None):
    flag = {"running": running}
    t = ss.SessionTracker(session_id="abc123def456", connection_id="conn-1", owner="tester",
                          is_running=lambda: flag["running"],
                          on_change=(pushes.append if pushes is not None else None),
                          clock=clock or _Clock())
    return t, flag


# --- the state ---------------------------------------------------------------------------------

def test_an_idle_session_is_open_and_a_run_makes_it_running():
    t, flag = _tracker()
    assert t.state() == "open"
    flag["running"] = True
    assert t.state() == "running"


def test_copies_in_flight_take_precedence_over_a_run():
    t, flag = _tracker(running=True)
    tin = t.start_transfer("stage-in", "pbmc to HPC3", bytes_total=100)
    assert t.state() == "staging-in"
    tout = t.start_transfer("stage-out", "Saving x", bytes_total=10)
    assert t.state() == "staging-out"
    t.finish_transfer(tout)
    assert t.state() == "staging-in"
    t.finish_transfer(tin)
    assert t.state() == "running"


def test_a_failed_copy_home_marks_the_session_failed_until_it_is_copied_again():
    t, _ = _tracker()
    a = t.start_transfer("stage-out", "Saving x", key="result:r1:dataset", run_id="r1")
    t.finish_transfer(a, error="OSError: link dropped")
    assert t.state() == "failed"
    snap = t.snapshot()
    assert snap["unresolved"][0]["error"] == "OSError: link dropped"
    assert snap["failures"][-1]["error"].startswith("Saving x: OSError")
    assert t.unsaved_runs() == {"r1"}
    b = t.start_transfer("stage-out", "Saving x", key="result:r1:dataset", run_id="r1")
    assert t.state() == "staging-out" and t.unsaved_runs() == {"r1"}     # being copied again
    t.finish_transfer(b)
    assert t.state() == "open" and t.unsaved_runs() == set()


def test_a_failed_copy_to_hpc3_is_shown_but_the_session_stays_usable():
    t, _ = _tracker()
    a = t.start_transfer("stage-in", "big to HPC3")
    t.finish_transfer(a, error="GatewayError: disk quota exceeded")
    assert t.state() == "open"
    snap = t.snapshot()
    assert snap["transfers"][-1]["status"] == "failed" and "quota" in snap["transfers"][-1]["error"]
    assert snap["failures"] and not snap["unresolved"]


def test_a_stopped_copy_is_cancelled_not_failed():
    t, _ = _tracker()
    a = t.start_transfer("stage-in", "x to HPC3")
    t.finish_transfer(a, cancelled=True)
    assert t.snapshot()["transfers"][-1]["status"] == "cancelled" and t.state() == "open"


def test_progress_is_pushed_at_most_once_a_second_and_a_change_always():
    clock, pushes = _Clock(), []
    t, _ = _tracker(clock=clock, pushes=pushes)
    tid = t.start_transfer("stage-in", "x to HPC3", bytes_total=1000, files_total=2)
    assert len(pushes) == 1 and pushes[-1]["state"] == "staging-in"
    t.progress(tid, bytes_done=100, files_done=0)
    assert len(pushes) == 1                                   # same second
    clock.t += 1.5
    t.progress(tid, bytes_done=600, files_done=1)
    assert len(pushes) == 2
    (active,) = pushes[-1]["active"]
    assert active["bytes_done"] == 600 and active["percent"] == 60 and active["files_done"] == 1
    t.finish_transfer(tid)                                    # within the second: still pushed
    assert len(pushes) == 3 and pushes[-1]["state"] == "open"
    done = pushes[-1]["transfers"][-1]
    assert done["status"] == "done" and done["bytes_done"] == 1000 and done["files_done"] == 2


def test_an_unknown_transfer_kind_is_refused():
    t, _ = _tracker()
    with pytest.raises(ValueError):
        t.start_transfer("sideways", "x")


def test_old_transfers_are_dropped_but_not_one_in_flight_or_an_open_failure():
    t, _ = _tracker()
    live = t.start_transfer("stage-in", "live")
    bad = t.start_transfer("stage-out", "bad", key="k", run_id="r1")
    t.finish_transfer(bad, error="x")
    for i in range(ss.KEEP_TRANSFERS + 5):
        t.finish_transfer(t.start_transfer("stage-in", f"n{i}"))
    ids = [x["id"] for x in t.record["transfers"]]
    assert len(ids) == ss.KEEP_TRANSFERS and live in ids and bad in ids


# --- the end -------------------------------------------------------------------------------------

@pytest.mark.parametrize("cleanup, state", [
    ({"status": "queued", "job_id": "8123456"}, "cleaning-up"),
    ({"status": "nothing"}, "closed"),
    ({"status": "skipped", "reason": "off"}, "closed"),
    ({"status": "error", "error": "sbatch failed"}, "failed"),
])
def test_how_a_session_ends(cleanup, state):
    t, _ = _tracker()
    t.begin_cleanup()
    assert t.state() == "cleaning-up"
    snap = t.end(cleanup)
    assert snap["state"] == state and snap["closed_at"] and snap["ended"] == "disconnect"
    assert snap["cleanup"]["status"] == cleanup["status"]


def test_a_copy_cut_off_by_the_disconnect_fails_and_fails_the_session():
    t, _ = _tracker()
    t.start_transfer("stage-out", "Saving x", key="result:r1:cellqc", run_id="r1")
    snap = t.end({"status": "queued", "job_id": "1"})
    assert snap["transfers"][0]["status"] == "failed" and "interrupted" in snap["transfers"][0]["error"]
    assert snap["state"] == "failed"


# --- the account's recent sessions --------------------------------------------------------------

def test_a_kept_record_is_listed_with_how_it_ended(tmp_path):
    t, _ = _tracker()
    t.keep(tmp_path, hpc_user="tester", host="hpc3.rcic.uci.edu")
    t.finish_transfer(t.start_transfer("stage-in", "x to HPC3", bytes_total=5))
    t.end({"status": "queued", "job_id": "8123456", "runs": 2})
    (row,) = ss.list_sessions(tmp_path)
    assert row["id"] == "abc123def456" and row["state"] == "cleaning-up"
    assert row["hpc_user"] == "tester" and row["cleanup"]["job_id"] == "8123456"
    assert row["transfers"][0]["status"] == "done"
    assert ss.store_path(tmp_path).name == ".sessions.json"


def test_a_session_that_ended_without_a_disconnect_is_closed_as_lost(tmp_path):
    t, _ = _tracker(running=True)
    t.keep(tmp_path, hpc_user="tester")
    t.start_transfer("stage-in", "x to HPC3")
    # The gateway restarted: the connection is gone, its record still says it is open.
    (row,) = ss.list_sessions(tmp_path, live={})
    assert row["ended"] == "lost" and row["closed_at"] and row["state"] == "closed"
    assert row["cleanup"] == {"status": "skipped", "reason": "sweep"}
    assert row["transfers"][0]["status"] == "failed"
    # A live one is shown as it is now, not closed.
    t2, _ = _tracker()
    t2.record["id"], t2.record["connection_id"] = "fff000aaa111", "conn-2"
    t2.keep(tmp_path, hpc_user="tester")
    rows = ss.list_sessions(tmp_path, live={"conn-2": t2.snapshot()})
    assert [r["id"] for r in rows][0] == "fff000aaa111" and rows[0]["closed_at"] is None


def test_only_the_newest_sessions_are_kept(tmp_path):
    for i in range(ss.KEEP_SESSIONS + 3):
        ss.save_record(tmp_path, {"id": f"s{i:02d}", "opened_at": f"2026-10-18T00:{i:02d}:00"})
    rows = ss.load_records(tmp_path)
    assert len(rows) == ss.KEEP_SESSIONS and rows[-1]["id"] == f"s{ss.KEEP_SESSIONS + 2:02d}"


def test_an_unreadable_record_file_reads_as_no_sessions(tmp_path):
    ss.store_path(tmp_path).write_text("{not json")
    assert ss.load_records(tmp_path) == [] and ss.list_sessions(tmp_path) == []


def test_a_queued_cleanup_is_settled_from_slurm_and_the_log(tmp_path):
    for sid, job in (("a1", "100"), ("b2", "200"), ("c3", "300")):
        t, _ = _tracker()
        t.record["id"], t.record["connection_id"] = sid, f"c-{sid}"
        t.keep(tmp_path, hpc_user="tester")
        t.end({"status": "queued", "job_id": job})
    assert sorted(ss.queued_cleanups(tmp_path, "tester")) == ["100", "200", "300"]
    assert ss.queued_cleanups(tmp_path, "someone-else") == []
    log = "removed: /x\nCLEAN_JOB 300\nCLEAN_RESULT removed=4 missing=1\n"
    last = ss.parse_clean_log(log)
    assert last == {"job": "300", "removed": 4, "missing": 1}
    changed = ss.confirm_cleanups(tmp_path, {"100": "COMPLETED", "200": "CANCELLED by 123",
                                             "300": "COMPLETED"}, last)
    assert changed == 3
    rows = {r["id"]: r for r in ss.load_records(tmp_path)}
    assert rows["a1"]["state"] == "closed" and rows["a1"]["cleanup"]["status"] == "done"
    assert "removed" not in rows["a1"]["cleanup"]                 # the log is another job's
    assert rows["b2"]["state"] == "failed" and "CANCELLED" in rows["b2"]["cleanup"]["error"]
    assert rows["c3"]["cleanup"]["removed"] == 4 and rows["c3"]["state"] == "closed"
    assert ss.confirm_cleanups(tmp_path, {"100": "COMPLETED"}) == 0     # settled once


def test_a_cleanup_still_running_or_unknown_to_slurm_stays_queued(tmp_path):
    t, _ = _tracker()
    t.keep(tmp_path, hpc_user="tester")
    t.end({"status": "queued", "job_id": "100"})
    assert ss.confirm_cleanups(tmp_path, {"100": "RUNNING"}) == 0
    assert ss.confirm_cleanups(tmp_path, {}) == 0
    assert ss.load_records(tmp_path)[0]["state"] == "cleaning-up"


def test_a_log_without_a_result_line_says_nothing():
    assert ss.parse_clean_log("") is None and ss.parse_clean_log("CLEAN_JOB 5\n") is None


# --- the gateway feeds it ------------------------------------------------------------------------

class _Remote:
    def __init__(self) -> None:
        self.username = "tester"
        self.commands: list[str] = []
        self.files: dict[str, bytes] = {}
        self.closed = False

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.commands.append(command)
        if command.startswith("sbatch"):
            return ExecResult(command=command, exit_status=0, stdout="8123456", stderr="")
        if command.startswith("sacct"):
            return ExecResult(command=command, exit_status=0, stdout="777|COMPLETED\n", stderr="")
        return ExecResult(command=command, exit_status=0, stdout="", stderr="")

    def put_file(self, local: str, remote: str) -> None:
        self.files[remote] = Path(local).read_bytes()

    def put_file_resumable(self, local, remote, progress=None, make_parent=True):
        data = Path(local).read_bytes()
        if progress:
            progress(len(data) // 2, len(data))
            progress(len(data), len(data))
        self.files[remote] = data

    def remote_size(self, path: str) -> int:
        return len(self.files.get(path, b""))

    def read_bytes(self, path: str, max_bytes=None) -> bytes:
        return self.files.get(path, b"")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def gw(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw_app
    saved = dict(gw_app.CONNECTIONS)
    gw_app.CONNECTIONS.clear()
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("AISCIENTIST_SERVER_DATA_ROOTS", "")
    yield gw_app
    gw_app.CONNECTIONS.clear()
    gw_app.CONNECTIONS.update(saved)


def _conn(gw, tmp_path, name="tester"):
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(shared_root=ROOT, analysis_on_hpc=True), mock=False, loop=loop,
                         username=name)
    loop.close()
    conn.workspace = gw.CONSOLE_RUNS_DIR / name
    conn.executor = _Remote()
    conn.shared_root_ok = True
    conn.status = "ready"
    conn.emit = lambda *a, **k: None
    conn.pushed = []
    conn._publish = conn.pushed.append
    gw.CONNECTIONS[conn.id] = conn
    return conn


def test_copying_a_bound_folder_to_hpc3_is_a_staging_in_transfer_with_progress(gw, tmp_path):
    shared = tmp_path / "shared"
    (shared / "study").mkdir(parents=True)
    (shared / "study" / "a.csv").write_bytes(b"x" * 300)
    (shared / "study" / "b.csv").write_bytes(b"y" * 100)
    conn = _conn(gw, tmp_path)
    remote, copied = gw._sync_server_path(conn, (shared / "study").resolve(), shared.resolve(),
                                          lambda *a: None)
    assert copied == {"a.csv", "b.csv"}
    states = [p["session"]["state"] for p in conn.pushed if p.get("type") == "session_state"]
    assert states[0] == "staging-in" and states[-1] == "open"
    (t,) = conn.session.snapshot()["transfers"]
    assert t["kind"] == "stage-in" and t["status"] == "done" and t["label"] == "study to HPC3"
    assert t["bytes_total"] == 400 and t["files_total"] == 2 and t["files_done"] == 2


def test_a_copy_to_hpc3_that_fails_is_recorded_and_raised(gw, tmp_path):
    shared = tmp_path / "shared"
    (shared / "d").mkdir(parents=True)
    (shared / "d" / "a.csv").write_bytes(b"x")
    conn = _conn(gw, tmp_path)

    def broken(*a, **k):
        raise OSError("quota exceeded")

    conn.executor.put_file_resumable = broken
    with pytest.raises(OSError):
        gw._sync_server_path(conn, (shared / "d").resolve(), shared.resolve(), lambda *a: None)
    t = conn.session.snapshot()["transfers"][-1]
    assert t["status"] == "failed" and "quota exceeded" in t["error"]


def test_a_run_whose_result_is_not_saved_is_kept_on_hpc3_at_disconnect(gw, tmp_path):
    conn = _conn(gw, tmp_path)
    for rid in ("saved1", "unsaved2"):
        conn.end_run(conn.begin_run("conv", rid))
    bad = conn.session.start_transfer("stage-out", "Saving x", key="result:unsaved2:dataset",
                                      run_id="unsaved2")
    conn.session.finish_transfer(bad, error="link dropped")
    targets = gw._session_cleanup_targets(conn)
    assert targets["runs"] == ["saved1"] and targets["kept_unsaved"] == ["unsaved2"]
    said: list[str] = []
    conn.emit = lambda level, stage, text, **k: said.append(text)
    gw._end_session_cleanup(conn)
    assert any("Kept on HPC3 because saving it" in s and "unsaved2" in s for s in said)
    snap = conn.session.snapshot()
    assert snap["state"] == "failed" and snap["cleanup"]["kept_unsaved"] == ["unsaved2"]
    assert snap["cleanup"]["status"] == "queued" and snap["cleanup"]["job_id"] == "8123456"


def test_disconnect_ends_the_session_and_says_how(gw, tmp_path, monkeypatch):
    conn = _conn(gw, tmp_path)
    conn.session.keep(conn.workspace, hpc_user="tester")
    conn.end_run(conn.begin_run("conv", "aaa111"))
    conn.broadcast_status = lambda: None
    monkeypatch.setattr(gw, "_owned_conn", lambda cid, request, **k: conn)
    res = asyncio.run(gw.disconnect({"connection_id": conn.id}, None))
    body = json.loads(res.body)
    assert body["session"]["state"] == "cleaning-up" and body["session"]["cleanup"]["job_id"] == "8123456"
    assert body["session"]["cleanup"]["runs"] == 1
    (row,) = ss.load_records(conn.workspace)
    assert row["state"] == "cleaning-up" and row["ended"] == "disconnect"


def test_a_session_without_cleanup_ends_closed_and_says_why(gw, tmp_path):
    conn = _conn(gw, tmp_path)
    conn.settings.session_cleanup = False
    assert gw._end_session_cleanup(conn) == {"status": "skipped"}
    snap = conn.session.snapshot()
    assert snap["state"] == "closed" and snap["cleanup"] == {"status": "skipped", "reason": "off"}


def test_the_next_connect_settles_the_last_sessions_cleanup(gw, tmp_path):
    old = _conn(gw, tmp_path)
    old.session.keep(old.workspace, hpc_user="tester")
    old.session.end({"status": "queued", "job_id": "777"})
    old.status = "disconnected"
    new = _conn(gw, tmp_path)
    log = f"{ROOT}/bin/tester/session_clean.log"
    new.executor.files[log] = b"CLEAN_JOB 777\nCLEAN_RESULT removed=3 missing=0\n"
    gw._settle_past_sessions(new)
    assert any(c.startswith("sacct -X -n -P -o JobID,State -j 777") for c in new.executor.commands)
    row = next(r for r in ss.load_records(new.workspace) if r["id"] == old.session.record["id"])
    assert row["state"] == "closed" and row["cleanup"]["removed"] == 3


def test_the_sessions_route_lists_the_live_and_the_ended(gw, tmp_path, monkeypatch):
    old = _conn(gw, tmp_path)
    old.session.keep(old.workspace, hpc_user="tester")
    old.session.end({"status": "nothing"})
    old.status = "disconnected"
    live = _conn(gw, tmp_path)
    live.session.keep(live.workspace, hpc_user="tester")
    live.session.start_transfer("stage-in", "x to HPC3", bytes_total=10)
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    monkeypatch.setattr(gw, "_owned_conn", lambda cid, request, **k: live)
    res = asyncio.run(gw.list_compute_sessions(None, connection_id=live.id))
    rows = json.loads(res.body)["sessions"]
    assert [r["state"] for r in rows] == ["staging-in", "closed"]
    assert rows[0]["active"][0]["label"] == "x to HPC3"


def test_the_status_summary_carries_the_session(gw, tmp_path):
    conn = _conn(gw, tmp_path)
    assert conn.summary()["session"]["state"] == "open"
    assert conn.summary()["session"]["id"] == gw._session_tag(conn)
