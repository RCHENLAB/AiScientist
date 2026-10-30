"""A session's results come home before its HPC3 folder goes (RCHENLAB/AiScientist issue 6).

What this file pins down:

1. **What counts as a result.** Every file under a run's ``result/`` on HPC3 comes home, hidden or
   not, named by a record or not; its job logs come too, within a size limit; nothing else does.
2. **Where it goes and how it is checked.** Into the run's own folder on the server, at the size
   HPC3 listed, with the SHA-256 of each server copy in ``stage_out.json``; the run's records then
   point at the server copy. A result already saved to the user's data is not copied twice.
3. **A failed copy keeps the folder.** Nothing is pointed home, no manifest is written, and the
   session ends ``failed`` with the run kept.
4. **The removal on HPC3 checks again.** The cleanup script removes a run folder with results only
   when every file under its ``result/`` matches the manifest by size and checksum.
5. **The sweep never removes results.** A cold run folder loses everything but its ``result/``.
6. **Abandoned sessions.** At the next connect the account's ended runs left on HPC3 come home and
   are then removed; interrupted runs, other accounts' runs and runs in use are left alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiscientist.gateway import stage_out  # noqa: E402
from aiscientist.gateway.executor import ExecResult  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CLEAN_SCRIPT = REPO / "deploy" / "hpc3" / "aiscientist_session_clean.sh"
GC_SCRIPT = REPO / "deploy" / "hpc3" / "aiscientist_temp_gc.sh"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _HPC:
    """HPC3 as a local folder: remote paths ARE local paths here. Answers the listing ``find``,
    ``ls``, ``mkdir``, ``squeue`` and ``sbatch`` the gateway sends."""

    def __init__(self, *, squeue: str = "", fail_pull: str = "") -> None:
        self.username = "tester"
        self.commands: list[str] = []
        self.squeue = squeue
        self.fail_pull = fail_pull
        self.put: dict[str, bytes] = {}
        self.closed = False

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.commands.append(command)
        ok = lambda out="": ExecResult(command=command, exit_status=0, stdout=out, stderr="")  # noqa: E731
        if "-printf '%s\\t%P\\n'" in command:
            root = Path(shlex.split(command.split(" then ", 1)[1])[1])
            if not root.is_dir():
                return ok()
            lines = [f"{p.stat().st_size}\t{p.relative_to(root).as_posix()}"
                     for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()]
            return ok("\n".join(lines) + ("\n" if lines else ""))
        if command.startswith("for d in "):
            # A listing by shell glob: HPC3 is a local folder here, so run it.
            out = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=30)
            return ok(out.stdout)
        if command.startswith("squeue"):
            return ok(self.squeue)
        if command.startswith("sbatch"):
            return ok("8123456")
        if "mkdir -p" in command:
            for word in shlex.split(command.split("mkdir -p", 1)[1].split("&&")[0]):
                Path(word).mkdir(parents=True, exist_ok=True)
        return ok()

    def put_file(self, local: str, remote: str) -> None:
        self.put[remote] = Path(local).read_bytes()

    def remote_size(self, path: str) -> int:
        p = Path(path)
        return p.stat().st_size if p.is_file() else 0

    def close(self) -> None:
        self.closed = True

    def pull(self, src: str, dest: Path, *, directory: bool = False, on_bytes=None) -> int:
        if self.fail_pull and self.fail_pull in src:
            raise OSError("the link dropped")
        assert not directory
        size = Path(src).stat().st_size
        if on_bytes:
            on_bytes(size // 2, size, 0, 1)
        shutil.copyfile(src, dest)
        if on_bytes:
            on_bytes(size, size, 1, 1)
        return size


def _hpc_run(tmp_path: Path, run_id: str = "run1", *, at: "str | Path | None" = None) -> Path:
    """A run's analysis folder on HPC3 with a processed dataset, its record, a CellQC folder, a
    hidden file and a checkpoint. In the retired layout unless ``at`` names the folder."""
    run = Path(at) if at else tmp_path / "hpc" / "Temp" / "tester" / "analysis" / run_id
    (run / "result" / "cellqc" / "lib1").mkdir(parents=True)
    (run / "work").mkdir()
    (run / "result" / "in.processed.r.h5ad").write_bytes(b"H5AD" * 1000)
    (run / "result" / "in.processed.r.h5ad.json").write_text('{"n_cells": 3}')
    (run / "result" / "cellqc" / "summary.csv").write_text("a,b\n1,2\n")
    (run / "result" / "cellqc" / "lib1" / "lib1.h5ad").write_bytes(b"L" * 500)
    (run / "result" / ".done").write_text("ok")
    (run / "work" / "adata_3.h5ad").write_bytes(b"C" * 2000)
    return run


def _server_run(tmp_path: Path, hpc_run: Path, run_id: str = "run1", *, saved: "dict | None" = None) -> Path:
    """The run's folder on the server with its records of what it handed on (on HPC3)."""
    run_dir = tmp_path / "runs" / "tester" / run_id
    data = run_dir / "artifacts" / "data"
    data.mkdir(parents=True)
    rec = {"name": "in.processed.r.h5ad", "path": str(hpc_run / "result" / "in.processed.r.h5ad"),
           "location": "hpc3", "kept_days": 3, "until_disconnect": True, "size_bytes": 4000}
    rec.update((saved or {}).get("dataset") or {})
    (data / "result_dataset.json").write_text(json.dumps(rec))
    q = {"name": "in.cellqc.r", "kind": "cellqc", "path": str(hpc_run / "result" / "cellqc"),
         "location": "hpc3", "kept_days": 3, "until_disconnect": True}
    q.update((saved or {}).get("cellqc") or {})
    (data / "cellqc_result.json").write_text(json.dumps(q))
    return run_dir


# --- 1-2: what comes home, where, and how it is recorded -----------------------------------------

def test_every_result_file_comes_home_with_its_checksum_and_the_records_point_at_it(tmp_path):
    hpc_run = _hpc_run(tmp_path)
    run_dir = _server_run(tmp_path, hpc_run)
    hpc = _HPC()
    seen: list[tuple] = []
    out = stage_out.stage_out_run(hpc, hpc.pull, run_id="run1", run_dir=run_dir,
                                  remote_run=str(hpc_run), session="s1", hpc_user="tester",
                                  on_bytes=lambda *a: seen.append(a))
    assert out["status"] == "done" and out["files"] == 5 and out["copied"] == 5
    for rel in ("in.processed.r.h5ad", "in.processed.r.h5ad.json", "cellqc/summary.csv",
                "cellqc/lib1/lib1.h5ad", ".done"):
        assert (run_dir / "result" / rel).read_bytes() == (hpc_run / "result" / rel).read_bytes()
    assert not (run_dir / "work").exists() and not (run_dir / "result" / "adata_3.h5ad").exists()
    assert seen[0][1] == out["copied_bytes"] and seen[-1][0] == out["copied_bytes"]

    manifest = json.loads((run_dir / "stage_out.json").read_text())
    by_path = {f["path"]: f for f in manifest["files"]}
    assert by_path["cellqc/lib1/lib1.h5ad"]["sha256"] == _sha(b"L" * 500)
    assert manifest["session"] == "s1" and manifest["hpc_user"] == "tester"
    assert manifest["remote"] == f"{hpc_run}/result"

    rd = json.loads((run_dir / "artifacts" / "data" / "result_dataset.json").read_text())
    assert rd["location"] == "gateway" and rd["until_disconnect"] is False and not rd["kept_days"]
    assert rd["path"] == str(run_dir / "result" / "in.processed.r.h5ad")
    assert rd["sha256"] == _sha(b"H5AD" * 1000)
    q = json.loads((run_dir / "artifacts" / "data" / "cellqc_result.json").read_text())
    assert q["location"] == "gateway" and q["path"] == str(run_dir / "result" / "cellqc")

    tsv = stage_out.verify_tsv(out["entries"])
    assert f"{_sha(b'ok')}\t2\t.done\n" in tsv and tsv.count("\n") == 5


def test_a_result_already_saved_is_not_copied_again(tmp_path):
    hpc_run = _hpc_run(tmp_path)
    saved = tmp_path / "data" / "users" / "tester" / "datasets" / "u1" / "original"
    saved.mkdir(parents=True)
    shutil.copyfile(hpc_run / "result" / "in.processed.r.h5ad", saved / "in.processed.r.h5ad")
    shutil.copyfile(hpc_run / "result" / "in.processed.r.h5ad.json", saved / "in.processed.r.h5ad.json")
    shutil.copytree(hpc_run / "result" / "cellqc", tmp_path / "qc")
    (tmp_path / "qc" / "summary.csv").write_text("changed on the server")     # no longer the same
    run_dir = _server_run(tmp_path, hpc_run, saved={
        "dataset": {"saved_to": str(saved / "in.processed.r.h5ad")},
        "cellqc": {"saved_to": str(tmp_path / "qc")}})
    hpc = _HPC()
    out = stage_out.stage_out_run(hpc, hpc.pull, run_id="run1", run_dir=run_dir, remote_run=str(hpc_run))
    by_path = {e["path"]: e for e in out["entries"]}
    assert by_path["in.processed.r.h5ad"]["saved"] and by_path["in.processed.r.h5ad.json"]["saved"]
    assert by_path["cellqc/lib1/lib1.h5ad"]["saved"]
    # The saved copy that differs in size cannot stand for the HPC3 file: that one comes home.
    assert not by_path["cellqc/summary.csv"]["saved"]
    assert (run_dir / "result" / "cellqc" / "summary.csv").read_text() == "a,b\n1,2\n"
    assert not (run_dir / "result" / "in.processed.r.h5ad").exists()
    assert by_path["in.processed.r.h5ad"]["sha256"] == _sha(b"H5AD" * 1000)
    rd = json.loads((run_dir / "artifacts" / "data" / "result_dataset.json").read_text())
    assert rd["location"] == "hpc3" and rd["saved_to"]          # a saved record keeps saved_to


def test_a_run_without_results_on_hpc3_has_nothing_to_bring(tmp_path):
    run = tmp_path / "hpc" / "Temp" / "tester" / "analysis" / "run2"
    (run / "work").mkdir(parents=True)
    hpc = _HPC()
    out = stage_out.stage_out_run(hpc, hpc.pull, run_id="run2", run_dir=tmp_path / "runs" / "run2",
                                  remote_run=str(run))
    assert out["status"] == "nothing" and out["entries"] == []
    assert not (tmp_path / "runs" / "run2" / "stage_out.json").exists()


def test_the_job_logs_come_home_within_their_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(stage_out, "LOG_MAX_BYTES", 100)
    hpc_run = _hpc_run(tmp_path)
    logs = tmp_path / "hpc" / "Temp" / "tester" / "scratch" / "analysis" / "run1"
    logs.mkdir(parents=True)
    (logs / "job_1.out").write_text("ran")
    (logs / "job_1.sh").write_text("#!/bin/bash")          # a job script is not a log
    (logs / "huge.log").write_text("x" * 500)
    run_dir = _server_run(tmp_path, hpc_run)
    hpc = _HPC()
    out = stage_out.stage_out_run(hpc, hpc.pull, run_id="run1", run_dir=run_dir, remote_run=str(hpc_run),
                                  log_dirs=[("analysis", str(logs)), ("report", str(tmp_path / "none"))])
    assert out["logs"] == 1
    assert (run_dir / "logs" / "hpc3" / "analysis" / "job_1.out").read_text() == "ran"
    assert not (run_dir / "logs" / "hpc3" / "analysis" / "job_1.sh").exists()
    assert any("huge.log" in p for p in out["log_errors"])


# --- 3: a failed copy keeps everything ----------------------------------------------------------

def test_a_failed_copy_points_nothing_home_and_writes_no_manifest(tmp_path):
    hpc_run = _hpc_run(tmp_path)
    run_dir = _server_run(tmp_path, hpc_run)
    hpc = _HPC(fail_pull="lib1.h5ad")
    out = stage_out.stage_out_run(hpc, hpc.pull, run_id="run1", run_dir=run_dir, remote_run=str(hpc_run))
    assert out["status"] == "failed" and "link dropped" in out["error"]
    assert not (run_dir / "stage_out.json").exists()
    assert json.loads((run_dir / "artifacts" / "data" / "cellqc_result.json").read_text())["location"] == "hpc3"


def test_a_file_that_arrives_short_is_a_failure(tmp_path):
    hpc_run = _hpc_run(tmp_path)
    run_dir = _server_run(tmp_path, hpc_run)
    hpc = _HPC()

    def short(src, dest, *, directory=False, on_bytes=None):
        Path(dest).write_bytes(Path(src).read_bytes()[:3])
        return 3

    out = stage_out.stage_out_run(hpc, short, run_id="run1", run_dir=run_dir, remote_run=str(hpc_run))
    assert out["status"] == "failed" and "expected" in out["error"]


def test_a_name_that_could_leave_the_folder_is_refused():
    assert stage_out.safe_rel("../etc/passwd") is None
    assert stage_out.safe_rel("/abs") is None
    assert stage_out.safe_rel("a\tb") is None
    assert stage_out.safe_rel("cellqc/./x.csv") == "cellqc/x.csv"


# --- 4: the cleanup script checks the results again ----------------------------------------------

def _clean(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(CLEAN_SCRIPT), "--root", str(root), "--user", "tester", *args],
                          capture_output=True, text=True, timeout=120)


def _verify_dir(root: Path, manifests: dict) -> Path:
    d = root / "bin" / "tester" / "stageout" / "sess1"
    d.mkdir(parents=True)
    for run_id, text in manifests.items():
        (d / f"{run_id}.tsv").write_text(text)
    return d


def _clean_tree(tmp_path: Path) -> "tuple[Path, Path, str]":
    root = tmp_path / "AiScientist"
    run = root / "Temp" / "tester" / "analysis" / "run1"
    (run / "result" / "cellqc").mkdir(parents=True)
    (run / "work").mkdir()
    (run / "result" / "a b.h5ad").write_bytes(b"A" * 10)            # a space in the name
    (run / "result" / "cellqc" / "s.csv").write_text("x,y\n")
    (run / "work" / "adata.h5ad").write_text("ckpt")
    tsv = (f"{_sha(b'A' * 10)}\t10\ta b.h5ad\n"
           f"{_sha(b'x,y' + bytes([10]))}\t4\tcellqc/s.csv\n")
    return root, run, tsv


def test_the_cleanup_removes_a_run_whose_results_match_the_manifest(tmp_path):
    root, run, tsv = _clean_tree(tmp_path)
    verify = _verify_dir(root, {"run1": tsv})
    out = _clean(root, "--run", "run1", "--verify-dir", str(verify), "--quiet")
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip().splitlines()[-1] == "CLEAN_RESULT removed=1 missing=0 kept=0"
    assert not run.exists()
    assert not verify.exists()               # its manifests did their job


@pytest.mark.parametrize("change, reason", [
    (lambda run: (run / "result" / "cellqc" / "s.csv").write_text("x,z\n"), "checksum-differs"),
    (lambda run: (run / "result" / "a b.h5ad").write_bytes(b"A" * 11), "size-differs"),
    (lambda run: (run / "result" / "late.csv").write_text("written after the copy"), "not-on-the-server"),
])
def test_the_cleanup_keeps_a_run_whose_results_differ(tmp_path, change, reason):
    root, run, tsv = _clean_tree(tmp_path)
    change(run)
    verify = _verify_dir(root, {"run1": tsv})
    out = _clean(root, "--run", "run1", "--verify-dir", str(verify), "--quiet")
    assert out.returncode == 3
    assert f"KEPT run=run1 reason={reason}" in out.stdout
    assert out.stdout.strip().splitlines()[-1] == "CLEAN_RESULT removed=0 missing=0 kept=1"
    assert (run / "work" / "adata.h5ad").exists()            # kept whole
    assert verify.exists()                                   # left for whoever looks at why


def test_the_cleanup_keeps_a_run_with_results_and_no_manifest_and_removes_one_without_results(tmp_path):
    root, run, _tsv = _clean_tree(tmp_path)
    bare = root / "Temp" / "tester" / "variant" / "run2"
    bare.mkdir(parents=True)
    (bare / "x.vcf").write_text("v")
    out = _clean(root, "--run", "run1", "--run", "run2", "--quiet")
    assert out.returncode == 3 and "KEPT run=run1 reason=no-stage-out-manifest" in out.stdout
    assert run.exists() and not bare.exists()


def test_a_relative_verify_dir_is_refused(tmp_path):
    root, _run, _tsv = _clean_tree(tmp_path)
    out = _clean(root, "--run", "run1", "--verify-dir", "stageout/x")
    assert out.returncode == 2 and "absolute" in out.stderr


# --- 5: the sweep keeps results ------------------------------------------------------------------

def _gnu_tools() -> bool:
    try:
        ok_find = subprocess.run(["find", "/dev/null", "-newer", "/dev/null", "-print", "-quit"],
                                 capture_output=True, timeout=10).returncode == 0
        ok_touch = subprocess.run(["touch", "-d", "-1 days", "/dev/null"],
                                  capture_output=True, timeout=10).returncode == 0
        return ok_find and ok_touch
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _gnu_tools(), reason="the sweeper needs GNU find/touch (as on HPC3)")
def test_the_sweep_strips_a_cold_run_to_its_results(tmp_path):
    root = tmp_path / "AiScientist"
    temp = root / "Temp" / "tester"
    for rel in ("analysis/withresult/result/cellqc", "analysis/withresult/work",
                "analysis/withresult/artifacts", "analysis/noresult/work"):
        (temp / rel).mkdir(parents=True)
    (temp / "analysis/withresult/result/cellqc/s.csv").write_text("keep me")
    (temp / "analysis/withresult/work/adata.h5ad").write_text("ckpt")
    (temp / "analysis/withresult/artifacts/report.md").write_text("copy")
    (temp / "analysis/withresult/.hidden").write_text("x")
    (temp / "analysis/noresult/work/adata.h5ad").write_text("ckpt")
    old = time.time() - 10 * 86400
    for p in root.rglob("*"):
        os.utime(p, (old, old))
    out = subprocess.run(["bash", str(GC_SCRIPT), "--root", str(root), "--ttl-days", "3", "--user",
                          "tester", "--quiet"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "GC_RESULT removed=1 kept=0 held=1 retired=0"
    assert (temp / "analysis/withresult/result/cellqc/s.csv").read_text() == "keep me"
    assert sorted(p.name for p in (temp / "analysis/withresult").iterdir()) == ["result"]
    assert not (temp / "analysis/noresult").exists()


# --- the gateway: the end of a session, and the next connect -------------------------------------

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


def _conn(gw, tmp_path, hpc: _HPC, name: str = "tester"):
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(shared_root=str(tmp_path / "hpc"), analysis_on_hpc=True),
                         mock=False, loop=loop, username=name)
    loop.close()
    conn.workspace = gw.CONSOLE_RUNS_DIR / name
    conn.executor = hpc
    conn.shared_root_ok = True
    conn.status = "ready"
    conn.said = []
    conn.emit = lambda level, stage, text, **k: conn.said.append((level, text))
    conn._publish = lambda payload: None
    gw.CONNECTIONS[conn.id] = conn
    return conn


class _Backend:
    """The compute backend with HPC3 as a local folder; records what the cleanup was given."""

    def __init__(self, hpc: _HPC) -> None:
        self.hpc = hpc
        self.cleanups: list[dict] = []

    def stage_out(self, src, dest, *, directory, on_bytes=None):
        return self.hpc.pull(src, dest, directory=directory, on_bytes=on_bytes)

    def clean_up(self, **kw):
        self.cleanups.append(kw)
        named = kw.get("runs") or kw.get("run_dirs") or kw.get("session_dirs") or kw.get("session")
        return {"status": "submitted", "job_id": "8123456"} if named else {"status": "nothing"}


def test_a_session_end_brings_results_home_before_it_queues_the_removal(gw, tmp_path, monkeypatch):
    hpc = _HPC()
    conn = _conn(gw, tmp_path, hpc)
    hpc_run = _hpc_run(tmp_path, "run1", at=gw._run_home(conn, "run1").dir("analysis"))
    _server_run(tmp_path, hpc_run, "run1")
    conn.end_run(conn.begin_run("conv", "run1"))
    backend = _Backend(hpc)
    monkeypatch.setattr(gw, "_compute", lambda c: backend)
    gw._end_session_cleanup(conn)
    (call,) = backend.cleanups
    assert call["run_dirs"] == {"run1": gw._run_home(conn, "run1").root} and call["runs"] == []
    assert call["session_dirs"] == [gw._temp_base(conn)] and set(call["verify"]) == {"run1"}
    assert f"{_sha(b'L' * 500)}\t500\tcellqc/lib1/lib1.h5ad" in call["verify"]["run1"]
    assert (conn.workspace / "run1" / "result" / "cellqc" / "lib1" / "lib1.h5ad").is_file()
    snap = conn.session.snapshot()
    assert snap["cleanup"]["staged_out"] == ["run1"] and snap["state"] == "cleaning-up"
    stage = [t for t in snap["transfers"] if t["kind"] == "stage-out"]
    assert stage and stage[0]["status"] == "done" and stage[0]["run_id"] == "run1"


def test_a_run_whose_copy_failed_is_kept_and_the_session_fails(gw, tmp_path, monkeypatch):
    hpc = _HPC(fail_pull="/run1/")
    conn = _conn(gw, tmp_path, hpc)
    for rid in ("run1", "run2"):
        _server_run(tmp_path, _hpc_run(tmp_path, rid, at=gw._run_home(conn, rid).dir("analysis")), rid)
        conn.end_run(conn.begin_run("conv", rid))
    backend = _Backend(hpc)
    monkeypatch.setattr(gw, "_compute", lambda c: backend)
    gw._end_session_cleanup(conn)
    (call,) = backend.cleanups
    assert list(call["run_dirs"]) == ["run2"] and set(call["verify"]) == {"run2"}
    snap = conn.session.snapshot()
    assert snap["state"] == "failed" and snap["cleanup"]["kept_unsaved"] == ["run1"]
    assert any("run1" in text and "kept" in text for _lvl, text in conn.said)


def test_the_disconnect_reply_does_not_wait_for_a_long_copy(gw, tmp_path, monkeypatch):
    hpc = _HPC()
    conn = _conn(gw, tmp_path, hpc)
    monkeypatch.setattr(gw, "_owned_conn", lambda cid, request: conn)
    monkeypatch.setattr(gw, "_DISCONNECT_WAIT_S", 0.05)
    release = __import__("threading").Event()
    order: list[str] = []

    def slow_end(c):
        release.wait(5)
        order.append("cleanup")
        return {"status": "nothing"}

    monkeypatch.setattr(gw, "_end_session_cleanup", slow_end)
    conn.broadcast_status = lambda: order.append("broadcast")

    async def go():
        reply = await gw.disconnect({"connection_id": conn.id}, request=None)
        body = json.loads(reply.body)
        assert body["ending"] is True and conn.ending
        again = json.loads((await gw.disconnect({"connection_id": conn.id}, request=None)).body)
        assert again["ending"] is True                  # a second click does not start a second end
        release.set()
        for _ in range(100):
            if not conn.ending:
                break
            await asyncio.sleep(0.02)

    asyncio.run(go())
    assert order == ["cleanup", "broadcast"] and hpc.closed and not conn.ending


def _ended(gw, monkeypatch, statuses: dict) -> None:
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw.auth_routes, "run_statuses", lambda uid, ids: {r: statuses[r] for r in ids
                                                                           if r in statuses})


def test_the_next_connect_brings_an_earlier_sessions_ended_runs_home(gw, tmp_path, monkeypatch):
    from aiscientist.gateway import hpc_layout, session_state
    hpc = _HPC()
    conn = _conn(gw, tmp_path, hpc)
    conn.app_user_id = 7
    old = "0ld5e5510n-0000"                               # an ended session of this account
    root = str(tmp_path / "hpc")
    for rid in ("done1", "stopped2", "broken3", "paused4", "theirs5", "held6"):
        # Two runs in the retired Temp/<user>, the rest in the earlier session's folder.
        at = None if rid in ("done1", "paused4") else hpc_layout.session_run(root, old, rid).dir("analysis")
        hpc_run = _hpc_run(tmp_path, rid, at=at)
        if rid != "theirs5":                              # another account's run: not in my results
            _server_run(tmp_path, hpc_run, rid)
    (Path(hpc_layout.session_dir(root, old)) / "app").mkdir()
    gone = "9one5e5510n-0000"                             # a folder this account has no record of
    Path(hpc_layout.session_dir(root, gone), "app").mkdir(parents=True)
    session_state.save_record(conn.workspace, {"id": "x", "connection_id": old, "owner": "tester",
                                               "hpc_user": "tester", "closed_at": "2026-10-28T00:00:00+00:00"})
    other = _conn(gw, tmp_path, _HPC(), name="someone")
    other.hpc_run_ids["held6"] = None                     # a live session of this process holds it
    _ended(gw, monkeypatch, {"done1": "done", "stopped2": "cancelled", "broken3": "error",
                             "paused4": "interrupted", "held6": "done"})
    backend = _Backend(hpc)
    monkeypatch.setattr(gw, "_compute", lambda c: backend)
    out = gw._recover_earlier_runs(conn)
    assert out["status"] == "submitted" and out["runs"] == ["broken3", "done1", "stopped2"]
    (call,) = backend.cleanups
    assert call["runs"] == ["done1"]                       # the retired layout, by run id
    assert call["run_dirs"] == {r: hpc_layout.session_run(root, old, r).root for r in ("broken3", "stopped2")}
    assert set(call["verify"]) == {"broken3", "done1", "stopped2"}
    assert call["session_dirs"] == [hpc_layout.session_dir(root, old)]     # not the unknown one
    assert call["log_name"] == "clean-earlier.log"
    assert (conn.workspace / "done1" / "stage_out.json").is_file()
    assert (conn.workspace / "broken3" / "result" / "cellqc" / "summary.csv").is_file()
    assert not (conn.workspace / "paused4" / "stage_out.json").exists()
    labels = [t["label"] for t in conn.session.snapshot()["transfers"]]
    assert any("an earlier session" in label for label in labels)


def test_a_run_whose_result_is_being_saved_is_left_for_later(gw, tmp_path, monkeypatch):
    hpc = _HPC()
    conn = _conn(gw, tmp_path, hpc)
    conn.app_user_id = 7
    run_dir = _server_run(tmp_path, _hpc_run(tmp_path, "done1"), "done1")
    pointer = run_dir / "artifacts" / "data" / "cellqc_result.json"
    pointer.write_text(json.dumps({**json.loads(pointer.read_text()), "saving": {"started_at": "now"}}))
    _ended(gw, monkeypatch, {"done1": "done"})
    backend = _Backend(hpc)
    monkeypatch.setattr(gw, "_compute", lambda c: backend)
    assert gw._recover_earlier_runs(conn)["status"] == "nothing" and backend.cleanups == []


def test_the_next_connect_waits_while_an_analysis_job_is_on_hpc3(gw, tmp_path, monkeypatch):
    hpc = _HPC(squeue="bioagent-vllm-tester\nbioagent_analysis_17\n")
    conn = _conn(gw, tmp_path, hpc)
    conn.app_user_id = 7
    _server_run(tmp_path, _hpc_run(tmp_path, "done1"), "done1")
    _ended(gw, monkeypatch, {"done1": "done"})
    backend = _Backend(hpc)
    monkeypatch.setattr(gw, "_compute", lambda c: backend)
    assert gw._recover_earlier_runs(conn) == {"status": "busy", "runs": ["done1"], "sessions": []}
    assert backend.cleanups == [] and not (conn.workspace / "done1" / "stage_out.json").exists()


def test_the_models_own_job_does_not_hold_the_recovery_back(gw, tmp_path, monkeypatch):
    hpc = _HPC(squeue="bioagent-vllm-tester\naiscientist-worker-tester\n")
    conn = _conn(gw, tmp_path, hpc)
    conn.app_user_id = 7
    _server_run(tmp_path, _hpc_run(tmp_path, "done1"), "done1")
    _ended(gw, monkeypatch, {"done1": "done"})
    backend = _Backend(hpc)
    monkeypatch.setattr(gw, "_compute", lambda c: backend)
    assert gw._recover_earlier_runs(conn)["status"] == "submitted"


def test_a_result_copied_home_does_not_expire_on_the_server(gw, tmp_path):
    runs = tmp_path / "runs"
    for rid, staged in (("kept1", True), ("local2", False)):
        (runs / "tester" / rid / "result").mkdir(parents=True)
        (runs / "tester" / rid / "result" / "x.h5ad").write_text("data")
        if staged:
            (runs / "tester" / rid / "stage_out.json").write_text("{}")
    old = time.time() - 30 * 86400
    for p in runs.rglob("*"):
        os.utime(p, (old, old))
    gw._expire_old_checkpoints(runs, 7)
    assert (runs / "tester" / "kept1" / "result" / "x.h5ad").exists()
    assert not (runs / "tester" / "local2" / "result").exists()


def test_a_cleanup_that_kept_a_run_ends_the_session_failed_with_the_reason(tmp_path):
    from aiscientist.gateway import session_state as ss
    ss.save_record(tmp_path, {"id": "s1", "connection_id": "c1", "owner": "tester", "hpc_user": "tester",
                              "closed_at": "2026-10-29T00:00:00+00:00", "transfers": [], "failures": [],
                              "cleanup": {"status": "queued", "job_id": "300"}})
    last = ss.parse_clean_log("KEPT run=r1 reason=checksum-differs file=x\nCLEAN_JOB 300\n"
                              "CLEAN_RESULT removed=2 missing=0 kept=1\n")
    assert last == {"job": "300", "removed": 2, "missing": 0, "kept": 1}
    assert ss.confirm_cleanups(tmp_path, {"300": "FAILED"}, last) == 1
    (rec,) = ss.load_records(tmp_path)
    assert rec["state"] == "failed" and rec["cleanup"]["kept"] == 1
    assert "did not match its copy on the server" in rec["cleanup"]["error"]


def test_the_result_metadata_says_where_the_outputs_are_kept():
    from aiscientist import metadata as md
    facts = {"run_id": "r1", "owner": "t", "status": "done",
             "handed_on": [{"what": "processed dataset", "name": "x.h5ad", "size_bytes": 10,
                            "location": "server"}]}
    doc = md.build_result(md.result_uuid("r1"), facts).document
    assert "Kept with the run on the server (not in this bundle)" in doc["description"]
    facts["handed_on"][0].pop("location")
    assert "Kept beside the run (not in this bundle)" in md.build_result(md.result_uuid("r1"), facts).document["description"]
