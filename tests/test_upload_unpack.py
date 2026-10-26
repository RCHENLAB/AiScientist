"""An uploaded archive is unpacked on an HPC3 compute node once its copy is verified.

The unpacker itself is tested against real archives in test_unpack_archive.py. Here it is the gateway
side: the Slurm command it builds, what it makes of the script's answer, and what the user ends up
with — a folder dataset, or (for an archive that is refused) the file that was uploaded plus the reason.
No SSH: a fake executor answers the one ``srun`` the gateway sends.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from aiscientist.gateway.executor import ExecResult


class FakeHPC:
    """An executor that records commands and answers the unpack step with ``self.unpack_answer``."""

    username = "tester"

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.sizes: dict[str, int] = {}
        self.unpack_answer: "dict | str | None" = {"status": "ok", "files": 7, "bytes": 1234, "skipped_links": 0}
        self.existing: set[str] = set()
        self.die_first = 0                 # this many unpack steps die before one answers

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.commands.append(command)
        if "unpack_archive.py" in command:
            ans = self.unpack_answer
            if self.die_first > 0:
                self.die_first -= 1
                ans = None
            if ans is None:
                return ExecResult(command=command, exit_status=1, stdout="", stderr="srun: error: node failure")
            if isinstance(ans, str):
                return ExecResult(command=command, exit_status=0, stdout=ans, stderr="")
            dest = command.split("--dest ")[1].split(" ")[0].strip("'")
            out = {**ans, **({"path": dest} if ans.get("status") == "ok" else {})}
            return ExecResult(command=command, exit_status=0 if ans.get("status") == "ok" else 3,
                              stdout=f"some module noise\nAISCIENTIST_UNPACK_JSON {json.dumps(out)}\n", stderr="")
        if command.startswith("test -e "):
            path = command.split("test -e ")[1].split(" && ")[0].strip("'")
            return ExecResult(command=command, exit_status=0, stdout="taken" if path in self.existing else "", stderr="")
        return ExecResult(command=command, exit_status=0, stdout="", stderr="")

    def put_file(self, local: str, remote: str) -> None:
        import os
        self.sizes[remote] = os.path.getsize(local)

    def remote_size(self, remote: str) -> int:
        return self.sizes.get(remote, 0)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'u.db').as_posix()}")
    from fastapi.testclient import TestClient

    from aiscientist.gateway import app as gw
    from aiscientist.gateway import staging
    from aiscientist.gateway.settings import HPCSettings

    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    monkeypatch.setattr(staging, "RETRY_DELAYS", (0.01, 0.01))
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(), mock=False, loop=loop, username="tester")
    conn.workspace = tmp_path / "ws"
    conn.settings.uploads_on_hpc = True
    conn.executor = FakeHPC()
    conn.hpc_pysrc = "/dfs/shared/pysrc/tester"          # the source tree is "already synced"
    conn.events = []
    conn.emit = lambda level, stage, msg, detail=None: conn.events.append((level, msg))
    gw.CONNECTIONS[conn.id] = conn
    gw._STAGING.pop(conn.owner, None)
    try:
        yield TestClient(gw.app), conn, gw
    finally:
        q = gw._STAGING.pop(conn.owner, None)
        if q is not None:
            # A worker still running outlives shutdown(), and it finds its connection by owner: every
            # test's owner is "tester", so it would copy through the NEXT test's executor (seen as a
            # fourth copy in a test that starts three).
            for job in q.jobs():
                q.wait(job.id, 5.0)
            q.shutdown()
        gw.CONNECTIONS.pop(conn.id, None)
        loop.close()


def _upload(client, conn, name, data=b"PK-not-really-a-zip", unpack=True, uid="up1"):
    return client.post("/api/upload/chunk", data={
        "connection_id": conn.id, "upload_id": uid, "name": name, "offset": "0", "done": "true",
        "unpack": "true" if unpack else "false"},
        files={"chunk": ("chunk", data, "application/octet-stream")}).json()


def _settled(client, conn, job_id, timeout=5.0):
    """The job once it has settled and its worker has returned: the state is set before the events
    are emitted and the dataset recorded, so reading those on the state alone is a race."""
    from aiscientist.gateway import app as gw
    end = time.time() + timeout
    while time.time() < end:
        job = client.get("/api/upload/staging", params={"connection_id": conn.id, "job_id": job_id}).json()["job"]
        if job["state"] in ("done", "failed", "cancelled"):
            queue = gw._STAGING.get(conn.owner)
            if queue is not None:
                queue.wait(job_id, timeout)
            return client.get("/api/upload/staging", params={"connection_id": conn.id, "job_id": job_id}).json()["job"]
        time.sleep(0.02)
    raise AssertionError("job did not settle")


# -- names -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,stem", [
    ("run1.zip", "run1"), ("run1.tar.gz", "run1"), ("run1.TGZ", "run1"), ("a.b.tar.xz", "a.b"),
    ("cellranger_out.tar", "cellranger_out"), ("matrix.h5ad", None), ("notes.gz", None), (".zip", None),
])
def test_archive_stem(env, name, stem):
    _client, _conn, gw = env
    assert gw._archive_stem(name) == stem


# -- the Slurm step ----------------------------------------------------------------------------------

def test_the_unpack_runs_on_a_compute_node_inside_the_analysis_image(env, monkeypatch):
    _client, conn, gw = env
    monkeypatch.delenv("AISCIENTIST_UNPACK_KEEP_ARCHIVE", raising=False)
    up = gw._hpc_uploads_dir(conn)
    cmd = gw._unpack_command(conn, "/dfs/pysrc/tester", f"{up}/run 1.zip", f"{up}/run 1")
    assert cmd.startswith(f"srun -p {conn.settings.cpu_partition} ")
    assert f"-A {conn.settings.cpu_account} " in cmd and "-J aiscientist-unpack-tester" in cmd
    assert " bash -lc " in cmd
    for piece in (f"{conn.settings.container_bin} exec", f"-B {up}:{up}", "-B /dfs/pysrc/tester:/dfs/pysrc/tester:ro",
                  conn.settings.analysis_image, "python3 /dfs/pysrc/tester/aiscientist/hpc/unpack_archive.py",
                  "--max-bytes 536870912000", "--max-files 200000", "--max-ratio 200"):
        assert piece in cmd, piece
    assert "run 1.zip" in cmd and "'\"'\"'" in cmd            # a space in the name is quoted twice over
    assert "--keep-archive" not in cmd


def test_limits_and_keep_archive_come_from_the_environment(env, monkeypatch):
    _client, conn, gw = env
    monkeypatch.setenv("AISCIENTIST_UNPACK_MAX_BYTES", "1000")
    monkeypatch.setenv("AISCIENTIST_UNPACK_MAX_FILES", "5")
    monkeypatch.setenv("AISCIENTIST_UNPACK_MAX_RATIO", "10")
    monkeypatch.setenv("AISCIENTIST_UNPACK_KEEP_ARCHIVE", "1")
    cmd = gw._unpack_command(conn, "/p", "/u/a.zip", "/u/a")
    assert "--max-bytes 1000 --max-files 5 --max-ratio 10 --keep-archive" in cmd


def test_the_result_line_is_found_among_other_output(env):
    _client, _conn, gw = env
    out = 'Lmod is loading\nAISCIENTIST_UNPACK_JSON {"status": "ok", "files": 2}\ntrailing\n'
    assert gw._unpack_result(out) == {"status": "ok", "files": 2}
    assert gw._unpack_result("nothing useful") is None
    assert gw._unpack_result("AISCIENTIST_UNPACK_JSON {broken") is None


# -- what the user ends up with ------------------------------------------------------------------------

def test_an_uploaded_archive_becomes_a_folder_dataset(env):
    client, conn, gw = env
    body = _upload(client, conn, "run1.zip")
    job = _settled(client, conn, body["job_id"])
    up = gw._hpc_uploads_dir(conn)
    assert job["state"] == "done" and job["unpack"] is True
    assert job["unpack_dest"] == f"{up}/run1" and job["unpacked"]["path"] == f"{up}/run1"
    assert job["unpacked"]["files"] == 7 and job["unpacked"]["bytes"] == 1234
    sent = [c for c in conn.executor.commands if "unpack_archive.py" in c]
    assert len(sent) == 1 and f"--archive {up}/run1.zip --dest {up}/run1 " in sent[0]
    assert any(level == "success" and "unpacked it on your HPC3 storage to run1/ (7 files" in msg
               for level, msg in conn.events)
    # asking again (the browser re-sends, or polls the upload) now reports the FOLDER
    again = _upload(client, conn, "run1.zip")
    assert again["status"] == "uploaded" and again["kind"] == "folder"
    assert again["name"] == "run1" and again["path"] == f"{up}/run1" and again["count"] == 7


def test_the_folder_name_avoids_one_that_already_exists_on_hpc3(env):
    client, conn, gw = env
    up = gw._hpc_uploads_dir(conn)
    conn.executor.existing.add(f"{up}/run1")
    job = _settled(client, conn, _upload(client, conn, "run1.tar.gz")["job_id"])
    assert job["unpack_dest"] == f"{up}/run1 (1)"


def test_a_refused_archive_is_kept_as_the_file_with_the_reason(env):
    client, conn, gw = env
    conn.executor.unpack_answer = {"status": "refused", "reason": "an entry climbs out of the archive with '..'"}
    job = _settled(client, conn, _upload(client, conn, "evil.zip")["job_id"])
    assert job["state"] == "done" and job["unpacked"] is None
    assert "climbs out" in job["unpack_error"]
    assert len([c for c in conn.executor.commands if "unpack_archive.py" in c]) == 1     # not retried
    assert any(level == "warning" and "was not unpacked" in msg and "kept as a file" in msg
               for level, msg in conn.events)
    again = _upload(client, conn, "evil.zip")
    assert again["status"] == "uploaded" and again.get("kind") != "folder" and "climbs out" in again["unpack_error"]


def test_a_step_that_dies_is_retried_into_the_same_folder_without_copying_again(env):
    client, conn, gw = env
    conn.executor.die_first = 1                                    # the first srun step dies
    conn.executor.unpack_answer = {"status": "ok", "files": 1, "bytes": 5}
    body = _upload(client, conn, "run2.zip")
    job = _settled(client, conn, body["job_id"])
    assert job["state"] == "done" and job["unpacked"]["files"] == 1
    sent = [c for c in conn.executor.commands if "unpack_archive.py" in c]
    assert len(sent) == 2 and len({c.split("--dest ")[1].split(" ")[0] for c in sent}) == 1   # same --dest
    assert len(conn.executor.sizes) == 1                           # the archive was copied once


def test_a_step_that_keeps_dying_fails_the_job_and_can_be_retried(env):
    client, conn, gw = env
    conn.executor.unpack_answer = "module: command not found"      # output, but no result line
    body = _upload(client, conn, "run3.zip")
    job = _settled(client, conn, body["job_id"])
    assert job["state"] == "failed" and "gave no result" in job["error"]
    conn.executor.unpack_answer = {"status": "ok", "files": 3, "bytes": 9}
    assert client.post("/api/upload/staging/retry",
                       json={"connection_id": conn.id, "job_id": body["job_id"]}).json()["status"] == "retrying"
    assert _settled(client, conn, body["job_id"])["unpacked"]["files"] == 3


def test_dismissing_an_archive_whose_unpack_kept_failing_keeps_it_as_a_file(env):
    """Its copy is complete and verified: dismissing the card must not delete 20 GB that arrived."""
    client, conn, gw = env
    conn.executor.unpack_answer = "module: command not found"
    body = _upload(client, conn, "run4.zip")
    assert _settled(client, conn, body["job_id"])["state"] == "failed"
    local = conn.workspace / "uploads" / "run4.zip"
    assert local.exists()
    del conn.executor.commands[:]
    client.post("/api/upload/staging/dismiss", json={"connection_id": conn.id, "job_id": body["job_id"]})
    end = time.time() + 5
    while time.time() < end and not any("rm -rf -- " in c for c in conn.executor.commands):
        time.sleep(0.02)                                           # the work folder goes in a thread
    sent = conn.executor.commands
    assert not local.exists()
    assert not any(c.startswith("rm -f") for c in sent)            # neither the archive nor a .part
    clean = [c for c in sent if "rm -rf -- " in c]
    assert len(clean) == 1 and clean[0].startswith("srun -p ") and clean[0].endswith("run4.unpacking")
    assert any(level == "warning" and "run4.zip was not unpacked" in msg and "kept as a file" in msg
               for level, msg in conn.events)


def test_a_refused_archive_has_its_work_folder_checked_not_blindly_removed(env):
    client, conn, gw = env
    conn.executor.unpack_answer = {"status": "refused", "reason": "an entry climbs out of the archive"}
    real_exec = conn.executor.exec
    conn.executor.exec = lambda command, timeout=60.0: (
        ExecResult(command=command, exit_status=1, stdout="", stderr="") if command.startswith("test -d ")
        else real_exec(command, timeout))                          # the script cleaned up after itself
    body = _upload(client, conn, "evil.zip")
    assert _settled(client, conn, body["job_id"])["unpack_error"]
    time.sleep(0.1)
    assert not any("rm -rf" in c for c in conn.executor.commands)


def test_without_the_flag_an_archive_stays_a_file(env):
    client, conn, gw = env
    job = _settled(client, conn, _upload(client, conn, "plain.zip", unpack=False)["job_id"])
    assert job["state"] == "done" and job["unpack"] is False and job["unpacked"] is None
    assert not [c for c in conn.executor.commands if "unpack_archive.py" in c]


def test_an_archive_inside_a_folder_upload_is_just_a_file_of_that_folder(env):
    client, conn, gw = env
    local = conn.workspace / "uploads" / "data" / "inner.zip"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"zip")
    job = gw._submit_staging(conn, local, "data/inner.zip", name="data/inner.zip", folder="data", unpack=True)
    assert job.unpack is False


# -- parallel copies ------------------------------------------------------------------------------------

class _Counting:
    """An executor whose copies stay open until the test releases them. They used to sleep 50 ms,
    so "three at once" needed all three threads to start within 50 ms; on a busy machine one did
    not (2 failures in 80 runs, eight at a time). ``reached`` is set once ``expect`` are inside."""

    username = "tester"

    def __init__(self, native: bool, expect: int) -> None:
        self.native, self.now, self.peak, self.lock = native, 0, 0, threading.Lock()
        self.expect, self.reached, self.release = expect, threading.Event(), threading.Event()

    def native_transfer_available(self) -> bool:
        return self.native

    def put_file_resumable(self, local, remote, progress=None) -> None:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
            if self.now >= self.expect:
                self.reached.set()
        self.release.wait(10)
        with self.lock:
            self.now -= 1


def _peak_of(gw, conn, n_jobs: int) -> int:
    """Start ``n_jobs`` copies; once as many as expected are inside, give any further one time to
    get in too (it must not), then let them all finish. Returns the most that ran at once."""
    from aiscientist.gateway import staging
    jobs = [staging.new_job(owner=conn.owner, local_path="/l", remote_path=f"/r{i}", name=f"f{i}", size=1,
                            hpc_user="tester") for i in range(n_jobs)]
    threads = [threading.Thread(target=gw._push_staged, args=(conn.owner, j, lambda s, t: None)) for j in jobs]
    for t in threads:
        t.start()
    assert conn.executor.reached.wait(10), "the expected number of copies never ran together"
    time.sleep(0.2)
    held = conn.executor.peak
    conn.executor.release.set()
    for t in threads:
        t.join()
    assert conn.executor.peak == held          # and none slipped in once the first ones left
    return held


@pytest.mark.parametrize("native,peak", [(True, 3), (False, 1)])
def test_openssh_copies_run_together_and_paramiko_copies_one_at_a_time(env, native, peak):
    _client, conn, gw = env
    conn.executor = _Counting(native, expect=peak)
    assert _peak_of(gw, conn, 3) == peak


def test_the_gateway_wide_cap_holds_across_users(env, monkeypatch):
    _client, conn, gw = env
    monkeypatch.setattr(gw, "_TRANSFER_SLOTS", threading.BoundedSemaphore(2))
    conn.executor = _Counting(native=True, expect=2)
    assert _peak_of(gw, conn, 5) == 2
