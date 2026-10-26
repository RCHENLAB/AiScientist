"""The compute-backend interface (RCHENLAB/AiScientist issue 6).

A session's data movement and housekeeping on HPC3 go through one small interface (stage-in,
submit, status, stage-out, clean-up), so another place to compute can be added without touching how
data is stored. This file pins down:

1. **HPC3 implements the interface** and each operation does what the gateway did before it
   (``server_data.sync_to_hpc``, ``slurm_job._submit``, ``sacct``, the SFTP copy home,
   ``hpc_gc.clean_session``): same commands, same results.
2. **The gateway's HPC3 paths sit behind it**: copying a run's inputs, saving a result home, moving a
   dataset home and the disconnect cleanup all call the session's backend, so a different backend
   receives them.
3. **What reaches a shell is checked**: a job id that is not a number never reaches ``sacct``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiscientist.gateway import compute, hpc_gc, server_data as sd  # noqa: E402
from aiscientist.gateway.executor import ExecResult  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402
from aiscientist.gateway.slurm_job import SlurmJobSpec  # noqa: E402

ROOT = "/dfs3b/lab/software/AiScientist"


class _Remote:
    """An HPC3 session over a dict of files, recording every command."""

    def __init__(self, files: "dict[str, bytes] | None" = None, *, sacct: str = "") -> None:
        self.username = "tester"
        self.files = dict(files or {})
        self.commands: list[str] = []
        self.sacct = sacct
        self.closed = False

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.commands.append(command)
        out = ""
        if command.startswith("sbatch"):
            out = "Submitted batch job 4242" if "--wrap" not in command else "4243"
        elif command.startswith("sacct"):
            out = self.sacct
        elif command.startswith("find "):
            root = command.split("find ", 1)[1].split(" -type", 1)[0].strip("'")
            size_first = "'%s\\t%P" in command          # server_data._remote_sizes
            out = "".join((f"{len(b)}\t{p[len(root) + 1:]}\n" if size_first else
                           f"{p[len(root) + 1:]}\t{len(b)}\n") for p, b in self.files.items()
                          if p.startswith(root + "/"))
        return ExecResult(command=command, exit_status=0, stdout=out, stderr="")

    def put_file(self, local: str, remote: str) -> None:
        self.files[remote] = Path(local).read_bytes()

    def put_file_resumable(self, local, remote, progress=None, make_parent=True):
        data = Path(local).read_bytes()
        if progress:
            progress(len(data), len(data))
        self.files[remote] = data

    def get_file_resumable(self, remote: str, local: str, progress=None) -> None:
        data = self.files[remote]
        if progress:
            progress(len(data) // 2, len(data))
            progress(len(data), len(data))
        Path(local).write_bytes(data)

    def remote_size(self, path: str) -> int:
        return len(self.files.get(path, b""))

    def close(self) -> None:
        self.closed = True


def _backend(remote=None, **kw):
    return compute.HPC3Backend(remote or _Remote(), shared_root=ROOT, user="tester",
                               partition="free", account="lab_account", **kw)


# --- HPC3 implements the interface ---------------------------------------------------------------

def test_hpc3_is_a_compute_backend():
    assert isinstance(_backend(), compute.ComputeBackend)
    assert _backend().name == "hpc3"


def test_stage_in_is_the_incremental_copy_with_its_byte_counts(tmp_path):
    folder = tmp_path / "study"
    folder.mkdir()
    (folder / "a.csv").write_bytes(b"x" * 30)
    (folder / "b.csv").write_bytes(b"y" * 10)
    found = sd.scan(folder, tmp_path, (tmp_path,))
    remote = _Remote()
    counts: list[tuple] = []
    res = _backend(remote).stage_in(found, "/dfs/m", on_bytes=lambda *c: counts.append(c))
    assert res["copied"] == 2 and res["copied_bytes"] == 40
    assert remote.files["/dfs/m/a.csv"] == b"x" * 30
    assert counts[0] == (0, 40, 0, 2) and counts[-1] == (40, 40, 2, 2)
    again = _backend(remote).stage_in(found, "/dfs/m")
    assert again["copied"] == 0 and again["kept"] == 2                 # nothing moves twice


def test_submit_writes_the_script_where_the_spec_says_and_returns_the_job_id():
    remote = _Remote()
    spec = SlurmJobSpec(script="#!/bin/bash\necho hi", job_name="aiscientist-x-tester",
                        submit_dir=f"{ROOT}/bin/tester")
    assert _backend(remote).submit(spec) == "4242"
    write, submit = remote.commands
    assert write.startswith(f"mkdir -p {ROOT}/bin/tester && cat > {ROOT}/bin/tester/aiscientist-x-tester.sbatch")
    assert submit == f"sbatch {ROOT}/bin/tester/aiscientist-x-tester.sbatch"


def test_status_is_one_sacct_and_reads_each_jobs_state():
    remote = _Remote(sacct="100|COMPLETED\n200|CANCELLED by 5\n999|RUNNING\n")
    states = _backend(remote).status(["100", "200", "300"])
    assert remote.commands == ["sacct -X -n -P -o JobID,State -j 100,200,300"]
    assert states == {"100": "COMPLETED", "200": "CANCELLED by 5"}     # 300: no record; 999: not asked


def test_a_job_id_that_is_not_a_number_never_reaches_sacct():
    remote = _Remote()
    assert _backend(remote).status(["1; rm -rf ~", "", "$(id)"]) == {}
    assert remote.commands == []


def test_stage_out_copies_a_folder_home_file_by_file_with_progress(tmp_path):
    remote = _Remote({"/dfs/r/cellqc/report.html": b"<html>", "/dfs/r/cellqc/lib/obs.csv": b"c,q\n"})
    counts: list[tuple] = []
    size = _backend(remote).stage_out("/dfs/r/cellqc", tmp_path / "out", directory=True,
                                      on_bytes=lambda *c: counts.append(c))
    assert size == 10
    assert (tmp_path / "out" / "lib" / "obs.csv").read_bytes() == b"c,q\n"
    assert counts[0] == (0, 10, 0, 2) and counts[-1] == (10, 10, 2, 2)


def test_stage_out_refuses_a_file_that_arrives_short(tmp_path):
    remote = _Remote({"/dfs/x.h5ad": b"12345"})
    remote.get_file_resumable = lambda r, local, progress=None: Path(local).write_bytes(b"12")
    with pytest.raises(OSError, match="expected 5"):
        _backend(remote).stage_out("/dfs/x.h5ad", tmp_path / "x.h5ad", directory=False)


def test_clean_up_is_the_session_cleanup_job_unchanged():
    remote = _Remote()
    script = f"{ROOT}/bin/tester/session_clean.sh"
    res = _backend(remote, clean_script=script).clean_up(runs=["aaa111"], session="sess01")
    direct = _Remote()
    expected = hpc_gc.clean_session(direct, ROOT, "tester", runs=["aaa111"], session="sess01",
                                    partition="free", account="lab_account", script_path=script)
    assert res == expected and remote.commands == direct.commands
    assert remote.commands[0].startswith("sbatch --job-name=aiscientist-sessionclean-tester")
    assert _backend(_Remote()).clean_up() == {"status": "nothing", "runs": 0}


# --- the gateway's HPC3 paths sit behind it -------------------------------------------------------

class _Recording:
    """A backend that records what the gateway asks of it and forwards to HPC3."""

    name = "recording"

    def __init__(self, inner: compute.HPC3Backend) -> None:
        self.inner = inner
        self.calls: list[str] = []

    def stage_in(self, scan, dest, **kw):
        self.calls.append("stage_in")
        return self.inner.stage_in(scan, dest, **kw)

    def submit(self, spec):
        self.calls.append("submit")
        return self.inner.submit(spec)

    def status(self, job_ids):
        self.calls.append("status")
        return self.inner.status(job_ids)

    def stage_out(self, src, dest, **kw):
        self.calls.append("stage_out")
        return self.inner.stage_out(src, dest, **kw)

    def clean_up(self, **kw):
        self.calls.append("clean_up")
        return self.inner.clean_up(**kw)


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


@pytest.fixture
def conn(gw, tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    c = gw.Connection(HPCSettings(shared_root=ROOT, analysis_on_hpc=True), mock=False, loop=loop,
                      username="tester")
    loop.close()
    c.workspace = gw.CONSOLE_RUNS_DIR / "tester"
    c.executor = _Remote()
    c.shared_root_ok = True
    c.status = "ready"
    c.emit = lambda *a, **k: None
    c._publish = lambda payload: None
    gw.CONNECTIONS[c.id] = c
    rec = _Recording(compute.HPC3Backend(c.executor, shared_root=ROOT, user="tester"))
    monkeypatch.setattr(gw, "_compute", lambda _c: rec)
    c.recording = rec
    return c


def test_the_gateway_builds_an_hpc3_backend_for_the_session(gw, tmp_path):
    loop = asyncio.new_event_loop()
    c = gw.Connection(HPCSettings(shared_root=ROOT, cpu_partition="free", cpu_account="lab"),
                      mock=False, loop=loop, username="tester")
    loop.close()
    c.executor = _Remote()
    c.session_clean_script = "/x/clean.sh"
    b = gw._compute(c)
    assert isinstance(b, compute.HPC3Backend) and isinstance(b, compute.ComputeBackend)
    assert (b.shared_root, b.user, b.partition, b.account, b.clean_script) == (
        ROOT, "tester", "free", "lab", "/x/clean.sh")


def test_a_runs_inputs_reach_hpc3_through_the_backend(gw, conn, tmp_path):
    shared = tmp_path / "shared"
    (shared / "d").mkdir(parents=True)
    (shared / "d" / "a.csv").write_bytes(b"abc")
    remote, copied = gw._sync_server_path(conn, (shared / "d").resolve(), shared.resolve(), lambda *a: None)
    assert conn.recording.calls == ["stage_in"] and copied == {"a.csv"}
    assert conn.executor.files[f"{remote}/a.csv"] == b"abc"


def test_the_disconnect_cleanup_goes_through_the_backend(gw, conn):
    conn.end_run(conn.begin_run("conv", "aaa111"))
    assert gw._end_session_cleanup(conn)["status"] == "submitted"
    assert conn.recording.calls == ["clean_up"]
    assert "--run aaa111" in conn.executor.commands[-1]


def test_saving_a_result_home_goes_through_the_backend(gw, conn, tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    run_id, name = "fedcba987654", "s2.cellqc.20261018-1200.fedcba987654"
    src = f"{ROOT}/Temp/tester/analysis/{run_id}/result/cellqc"
    conn.executor.files.update({f"{src}/report.html": b"<html>"})
    from aiscientist.tools.api import CELLQC_RESULT_POINTER
    pointer = gw.CONSOLE_RUNS_DIR / "tester" / run_id / "artifacts" / "data" / CELLQC_RESULT_POINTER
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"name": name, "path": src, "location": "hpc3", "size_bytes": 6}))
    gw._bring_result_home("tester", None, run_id, "cellqc", conn, src, pointer)
    assert conn.recording.calls == ["stage_out"]
    assert json.loads(pointer.read_text())["saved_to"]
    (t,) = conn.session.snapshot()["transfers"]
    assert t["kind"] == "stage-out" and t["status"] == "done" and t["run_id"] == run_id


def test_a_failed_save_home_is_recorded_for_its_run(gw, conn, tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    run_id = "abcabc123123"
    from aiscientist.tools.api import RESULT_DATASET_POINTER
    pointer = gw.CONSOLE_RUNS_DIR / "tester" / run_id / "artifacts" / "data" / RESULT_DATASET_POINTER
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"name": "x.h5ad", "path": "/dfs/x.h5ad", "size_bytes": 5}))
    gw._bring_result_home("tester", None, run_id, "dataset", conn, "/dfs/missing.h5ad", pointer)
    assert json.loads(pointer.read_text())["save_error"]
    assert conn.session.unsaved_runs() == {run_id} and conn.session.state() == "failed"


def test_moving_a_dataset_home_goes_through_the_backend(gw, conn, tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    from aiscientist.gateway import auth_routes
    path = f"{ROOT}/uploads/tester/pbmc.h5ad"
    conn.executor.files[path] = b"MATRIX"
    conn.executor.exec = _probe(conn.executor)
    row = {"id": 7, "uuid": "8b0c3a4e-1f2d-4c5b-9a6e-7d8f9a0b1c2d", "name": "pbmc.h5ad", "path": path,
           "size_bytes": 6}
    conn.app_user_id = 1
    monkeypatch.setattr(auth_routes, "get_dataset_record", lambda uid, did: row)
    monkeypatch.setattr(auth_routes, "repoint_dataset", lambda *a: None)
    assert gw._home_dataset(conn, row) == 6
    assert conn.recording.calls == ["stage_out"]
    from aiscientist.gateway import dataset_storage
    source = json.loads((dataset_storage.dataset_dir(row["uuid"], "tester") / "source.json").read_text())
    assert source["kind"] == "file" and source["hpc3_path"] == path and source["bytes"] == 6


def _probe(remote: _Remote):
    """Answer the gateway's ``if test -d …`` probe from the fake's files."""
    plain = remote.exec

    def exec_(command: str, timeout: float = 60.0) -> ExecResult:
        if command.startswith("if test -d"):
            path = command.split("test -d ", 1)[1].split(";", 1)[0].strip("'")
            what = "file" if path in remote.files else (
                "dir" if any(p.startswith(path + "/") for p in remote.files) else "")
            return ExecResult(command=command, exit_status=0, stdout=what, stderr="")
        return plain(command, timeout)

    return exec_
