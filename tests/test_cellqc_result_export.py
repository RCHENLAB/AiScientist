"""CellQC's complete result directory: downloaded as one zip, saved to the user's data, or saved by
itself when the run completes (RCHENLAB/AiScientist issue 3).

The tool side (what run_cellqc keeps, and where) is in test_run_cellqc.py; this is the gateway.
"""
from __future__ import annotations

import asyncio
import io
import json
import types
import zipfile
from pathlib import Path

import pytest

FILES = {"manifest.tsv": b"sample\tincluded\nS3\tTrue\n", "metrics.csv": b"sampleid\nS3\n",
         "qc_status.csv": b"sample,step,status\n", "report.html": b"<html>CellQC</html>",
         "report_slides.pdf": b"%PDF-1.5 slides", "report_slides.tex": b"\\documentclass{beamer}",
         "S3_doublet_concordance.txt": b"kappa 0.8\n", "S3_doublet_summary.txt": b"doublets 12\n",
         "S3.h5ad": b"\x89HDF\r\n\x1a\n" + bytes(range(256)) * 400,
         "S3_obs.txt.gz": b"\x1f\x8b obs", "S3_var.txt.gz": b"\x1f\x8b var"}
NAME = "Sample3_GSM5676874.cellqc.20261012-1630.run7"
HPC_TEMP = "/dfs3b/proj/AiScientist/Temp/yijus12"
HPC_LAB = "/dfs3b/ruic20_lab/yijus12"


@pytest.fixture
def gw(tmp_path, monkeypatch):
    gw_app = pytest.importorskip("aiscientist.gateway.app")
    pytest.importorskip("httpx")
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_optional_user",
                        lambda request: types.SimpleNamespace(username="yijus12", id=5, role="user"))
    monkeypatch.setattr(gw_app, "_is_admin", lambda user: False)
    return gw_app


def _client(gw_app):
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app)


def _pointer(root: Path, owner: str, run_id: str, **fields) -> Path:
    data = root / owner / run_id / "artifacts" / "data"
    data.mkdir(parents=True, exist_ok=True)
    rec = {"name": NAME.replace("run7", run_id), "files": [{"name": n, "size": len(b)} for n, b in FILES.items()],
           "size_bytes": sum(map(len, FILES.values())), "libraries": ["S3"], "matrices": ["S3.h5ad"],
           "created_at": "2026-10-12T23:30:00+00:00", "kept_days": 3, "saved_to": None, **fields}
    (data / "cellqc_result.json").write_text(json.dumps(rec))
    return data / "cellqc_result.json"


def _seed_local(root: Path, owner: str, run_id: str) -> Path:
    full = root / owner / run_id / "result" / "cellqc"
    full.mkdir(parents=True)
    for name, body in FILES.items():
        (full / name).write_bytes(body)
    _pointer(root, owner, run_id, path=str(full), location="gateway")
    return full


class _Hpc:
    """An HPC3 session that holds the directory: answers the gateway's commands and reads."""

    def __init__(self, root: str, devices: str = "49 49"):
        self.root, self.devices, self.calls, self.read = root, devices, [], []

    def exec(self, cmd, timeout: float = 60.0):
        self.calls.append(cmd)
        if cmd.startswith("test -d "):
            return types.SimpleNamespace(ok=self.root in cmd, stdout="", stderr="")
        out = self.devices if cmd.startswith("mkdir -p") else ""
        return types.SimpleNamespace(ok=True, stdout=out, stderr="")

    def read_stream(self, path: str):
        self.read.append(path)
        body = FILES[path.rsplit("/", 1)[1]]
        for i in range(0, len(body), 30_000):              # several blocks for the matrix
            yield body[i:i + 30_000]


def _hpc_conn(gw_app, monkeypatch, executor):
    conn = types.SimpleNamespace(
        owner="yijus12", app_user_id=5, executor=executor,
        settings=types.SimpleNamespace(temp_ttl_days=3, checkpoint_ttl_days=7, session_cleanup=True,
                                       cpu_partition="standard",
                                       cpu_account="ruic20_lab"))
    monkeypatch.setattr(gw_app, "_owner_connections", lambda owner: [conn] if owner == "yijus12" else [])
    monkeypatch.setattr(gw_app, "_active_conn_for_user", lambda user: conn)
    monkeypatch.setattr(gw_app, "_temp_base", lambda c: HPC_TEMP)
    monkeypatch.setattr(gw_app, "_storage_base", lambda c: HPC_LAB)
    return conn


def _unzip(response) -> dict[str, bytes]:
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    assert zf.testzip() is None
    return {info.filename: zf.read(info) for info in zf.infolist()}


# --- the download -----------------------------------------------------------------------------------

def test_the_download_is_cellqcs_result_directory_complete_and_nothing_else(tmp_path, gw):
    _seed_local(tmp_path, "yijus12", "run7")
    r = _client(gw).get("/api/cellqc-result/yijus12/run7")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert 'filename="cellqc_result_run7.zip"' in r.headers["content-disposition"]
    assert _unzip(r) == {f"result/{name}": body for name, body in FILES.items()}


def test_an_hpc3_runs_directory_is_streamed_from_hpc3_not_stored_here(tmp_path, gw, monkeypatch):
    root = f"{HPC_TEMP}/analysis/run7/result/cellqc"
    hpc = _Hpc(root)
    _hpc_conn(gw, monkeypatch, hpc)
    _pointer(tmp_path, "yijus12", "run7", path=root, location="hpc3")
    r = _client(gw).get("/api/cellqc-result/yijus12/run7")
    assert r.status_code == 200
    assert _unzip(r) == {f"result/{name}": body for name, body in FILES.items()}
    assert hpc.read == [f"{root}/{name}" for name in FILES]          # read where it is kept
    # only the pointer is on this host: no matrix, no zip
    assert [p.name for p in (tmp_path / "yijus12" / "run7").rglob("*") if p.is_file()] == ["cellqc_result.json"]


def test_a_library_named_after_a_renamed_upload_folder_is_not_dropped(tmp_path, gw):
    # an upload of a folder that already exists becomes "Sample3_GSM5676874 (1)", and CellQC names
    # the library's files after the folder
    full = _seed_local(tmp_path, "yijus12", "run7")
    odd = "Sample3_GSM5676874 (1).h5ad"
    (full / odd).write_bytes(b"matrix")
    p = tmp_path / "yijus12" / "run7" / "artifacts" / "data" / "cellqc_result.json"
    rec = json.loads(p.read_text())
    rec["files"] += [{"name": odd, "size": 6}, {"name": ".hidden", "size": 1}, {"name": "a\\b", "size": 1}]
    p.write_text(json.dumps(rec))
    got = _unzip(_client(gw).get("/api/cellqc-result/yijus12/run7"))
    assert got[f"result/{odd}"] == b"matrix" and len(got) == len(FILES) + 1


def test_matrices_are_stored_as_they_are_and_text_is_deflated(tmp_path, gw):
    _seed_local(tmp_path, "yijus12", "run7")
    zf = zipfile.ZipFile(io.BytesIO(_client(gw).get("/api/cellqc-result/yijus12/run7").content))
    kinds = {i.filename: i.compress_type for i in zf.infolist()}
    assert kinds["result/S3.h5ad"] == zipfile.ZIP_STORED and kinds["result/S3_obs.txt.gz"] == zipfile.ZIP_STORED
    assert kinds["result/metrics.csv"] == zipfile.ZIP_DEFLATED


def test_a_swept_directory_and_a_closed_session_are_said_plainly(tmp_path, gw, monkeypatch):
    root = f"{HPC_TEMP}/analysis/run7/result/cellqc"
    _pointer(tmp_path, "yijus12", "run7", path=root, location="hpc3")
    monkeypatch.setattr(gw, "_owner_connections", lambda owner: [])
    r = _client(gw).get("/api/cellqc-result/yijus12/run7")
    assert r.status_code == 409 and "Connect to HPC3" in r.json()["error"]
    _hpc_conn(gw, monkeypatch, _Hpc("/somewhere/else"))               # the Temp sweeper took it
    r = _client(gw).get("/api/cellqc-result/yijus12/run7")
    assert r.status_code == 410 and "session disconnects, or after 3 days" in r.json()["error"]
    assert _client(gw).get("/api/cellqc-result/yijus12/none").status_code == 404


def test_a_pointer_cannot_send_the_download_anywhere_else(tmp_path, gw, monkeypatch):
    hpc = _Hpc("/dfs3b")
    _hpc_conn(gw, monkeypatch, hpc)
    _pointer(tmp_path, "yijus12", "run7", path="/dfs3b/ruic20_lab/someone_else/data", location="hpc3")
    assert _client(gw).get("/api/cellqc-result/yijus12/run7").status_code == 400
    # nor a file name out of the directory
    root = f"{HPC_TEMP}/analysis/run8/result/cellqc"
    p = _pointer(tmp_path, "yijus12", "run8", path=root, location="hpc3")
    rec = json.loads(p.read_text())
    rec["files"] = [{"name": "../../../../etc/passwd", "size": 1}, {"name": "metrics.csv", "size": 12}]
    p.write_text(json.dumps(rec))
    assert list(_unzip(_client(gw).get("/api/cellqc-result/yijus12/run8"))) == ["result/metrics.csv"]
    assert hpc.read == [f"{root}/metrics.csv"]
    # and another account's run is refused before anything is read
    _seed_local(tmp_path, "victim", "run9")
    assert _client(gw).get("/api/cellqc-result/victim/run9").status_code == 403


# --- save to my data --------------------------------------------------------------------------------

def test_the_directory_is_saved_as_a_folder_dataset_and_still_downloads(tmp_path, gw, monkeypatch):
    recorded: list = []
    monkeypatch.setattr(gw.auth_routes, "dataset_path_recorded", lambda uid, path: False)
    monkeypatch.setattr(gw.auth_routes, "record_dataset", lambda *a, **kw: recorded.append((a, kw)) or 77)
    full = _seed_local(tmp_path, "yijus12", "run7")

    (row,) = _client(gw).get("/api/result-datasets").json()["result_datasets"]
    assert row["run_id"] == "run7" and row["cellqc"]["n_files"] == 11 and row["cellqc"]["n_matrices"] == 1
    assert row["cellqc"]["download_url"] == "/api/cellqc-result/yijus12/run7"

    r = _client(gw).post("/api/result-datasets/run7/save?what=cellqc")
    assert r.status_code == 200 and r.json()["dataset_id"] == 77 and r.json()["kind"] == "folder"
    dest = tmp_path / "yijus12" / "uploads" / "processed" / NAME
    assert {p.name for p in dest.iterdir()} == set(FILES) and not full.exists()   # moved, not copied
    ((args, kw),) = recorded
    assert args == (5, NAME, str(dest), sum(map(len, FILES.values())), "folder")
    # its metadata names the run's result it came from, and every file with its size (issue 9)
    assert kw["source"] == "result" and kw["derived_from"]["run_id"] == "run7"
    assert sorted(f["path"] for f in kw["files"]) == sorted(FILES)
    (row,) = _client(gw).get("/api/result-datasets").json()["result_datasets"]
    assert row["cellqc"]["saved_to"] == str(dest)
    assert _client(gw).post("/api/result-datasets/run7/save?what=cellqc").json()["already"] is True
    # the download follows it to where it was saved
    assert set(_unzip(_client(gw).get("/api/cellqc-result/yijus12/run7"))) == {f"result/{n}" for n in FILES}
    assert _client(gw).post("/api/result-datasets/run7/save?what=everything").status_code == 400


def test_on_hpc3_the_directory_is_moved_into_the_personal_lab_folder(tmp_path, gw, monkeypatch):
    monkeypatch.setattr(gw.auth_routes, "dataset_path_recorded", lambda uid, path: False)
    monkeypatch.setattr(gw.auth_routes, "record_dataset", lambda *a, **kw: 78)
    root = f"{HPC_TEMP}/analysis/run7/result/cellqc"
    hpc = _Hpc(root)
    _hpc_conn(gw, monkeypatch, hpc)
    _pointer(tmp_path, "yijus12", "run7", path=root, location="hpc3")
    r = _client(gw).post("/api/result-datasets/run7/save?what=cellqc")
    dest = f"{HPC_LAB}/AiScientist_results/{NAME}"
    assert r.status_code == 200 and r.json()["path"] == dest
    check, move = hpc.calls
    assert f"test -d {root}" in check and f"test ! -e {dest}" in check
    assert move == f"mv {root} {dest}"                    # one filesystem: a rename, on the login node
    assert ".json" not in move                            # a directory has no sidecar record


def test_a_run_that_handed_on_both_lists_both(tmp_path, gw):
    _seed_local(tmp_path, "yijus12", "run7")
    data = tmp_path / "yijus12" / "run7" / "artifacts" / "data"
    (data / "result_dataset.json").write_text(json.dumps({
        "name": "Sample3_GSM5676874.processed.20261012-1630.run7.h5ad", "size_bytes": 10,
        "n_cells": 100, "n_genes": 50, "created_at": "2026-10-12T23:31:00+00:00",
        "location": "gateway", "kept_days": 7, "saved_to": None}))
    (row,) = _client(gw).get("/api/result-datasets").json()["result_datasets"]
    assert row["name"].endswith(".h5ad") and row["n_cells"] == 100 and row["cellqc"]["name"] == NAME


# --- at the end of a run ----------------------------------------------------------------------------

def _run_conn(tmp_path, executor=None, user_id=5):
    return types.SimpleNamespace(owner="yijus12", app_user_id=user_id, executor=executor,
                                 workspace=tmp_path / "yijus12",
                                 settings=types.SimpleNamespace(temp_ttl_days=3, checkpoint_ttl_days=7,
                                                                session_cleanup=True))


def test_the_gateway_names_the_directory_and_says_how_long_it_is_kept(tmp_path, gw):
    root = f"{HPC_TEMP}/analysis/run7/result/cellqc"
    p = _pointer(tmp_path, "yijus12", "run7", path=root)
    rec = json.loads(p.read_text())
    del rec["name"], rec["kept_days"]                     # as run_cellqc leaves it
    p.write_text(json.dumps(rec))
    said: list = []
    out = gw._stamp_cellqc_result(_run_conn(tmp_path), "run7",
                                  {"dataset_root": "/dfs3b/uploads/yijus12/Sample3_GSM5676874"}, True,
                                  lambda level, stage, msg: said.append((level, msg)))
    assert gw._CELLQC_NAME_RE.fullmatch(out["name"]) and out["name"].startswith("Sample3_GSM5676874.cellqc.")
    assert out["name"].endswith(".run7") and out["location"] == "hpc3" and out["kept_days"] == 3
    assert out["until_disconnect"] is True                # on HPC3 it goes with the session
    assert json.loads(p.read_text())["name"] == out["name"]
    (level, msg), = said
    assert level == "success" and "11 files" in msg and "1 per-library .h5ad" in msg
    assert "until you disconnect (at most 3 days)" in msg
    # a run without CellQC says nothing
    assert gw._stamp_cellqc_result(_run_conn(tmp_path), "other", {}, True, lambda *a: said.append(a)) is None
    assert len(said) == 1


def test_a_run_asked_to_save_its_results_registers_them_itself(tmp_path, gw, monkeypatch):
    recorded: list = []
    monkeypatch.setattr(gw.auth_routes, "dataset_path_recorded", lambda uid, path: False)
    monkeypatch.setattr(gw.auth_routes, "record_dataset", lambda *a, **kw: recorded.append(a) or len(recorded))
    _seed_local(tmp_path, "yijus12", "run7")
    name = "Sample3_GSM5676874.processed.20261012-1630.run7.h5ad"
    res = tmp_path / "yijus12" / "run7" / "result"
    (res / name).write_bytes(b"h5ad bytes")
    (tmp_path / "yijus12" / "run7" / "artifacts" / "data" / "result_dataset.json").write_text(json.dumps({
        "name": name, "path": str(res / name), "size_bytes": 10, "location": "gateway", "saved_to": None}))
    said: list = []
    asyncio.run(gw._autosave_results(_run_conn(tmp_path), "run7", lambda level, stage, msg: said.append((level, msg))))
    saved = tmp_path / "yijus12" / "uploads" / "processed"
    assert (saved / name).is_file() and (saved / NAME / "S3.h5ad").is_file()
    assert [(r[1], r[4]) for r in recorded] == [(name, "h5ad"), (NAME, "folder")]
    assert [level for level, _ in said] == ["success", "success"] and all("Datasets" in m for _, m in said)
    # the report's dataset line then says where it is, not that it expires
    line = gw._result_dataset_line(tmp_path / "yijus12" / "run7" / "artifacts")
    assert "saved in your data" in line and "Save to my data" not in line


def test_a_save_that_cannot_happen_is_a_warning_and_the_run_goes_on(tmp_path, gw):
    root = f"{HPC_TEMP}/analysis/run7/result/cellqc"
    _pointer(tmp_path, "yijus12", "run7", path=root, location="hpc3")
    said: list = []
    asyncio.run(gw._autosave_results(_run_conn(tmp_path), "run7", lambda level, stage, msg: said.append((level, msg))))
    (level, msg), = said
    assert level == "warning" and "Connect to HPC3" in msg and "still can" in msg
    said.clear()
    # Without an account there is no "my data": every run would say so, so nothing is said.
    asyncio.run(gw._autosave_results(_run_conn(tmp_path, user_id=None), "run7",
                                     lambda level, stage, msg: said.append((level, msg))))
    assert said == []


def test_cellqcs_directory_is_a_bundle_folder_not_a_stray(tmp_path, gw):
    art = tmp_path / "art"
    for d in ("report", "result", "qc_report_by_the_model"):
        (art / d).mkdir(parents=True)
    assert gw._quarantine_strays(art) == ["qc_report_by_the_model"]
    assert (art / "result").is_dir() and (art / "extra" / "qc_report_by_the_model").is_dir()


def test_the_lab_request_carries_the_choice(gw):
    assert gw.LabRequest(connection_id="c", question="q").save_results is False
    assert gw.LabRequest(connection_id="c", question="q", save_results=True).save_results is True


# --- reading a remote file while it is sent on ---------------------------------------------------------

class _Handle:
    def __init__(self, body: bytes, log: list, short_at: "int | None" = None):
        self.body, self.log, self.short_at = body, log, short_at

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.log.append("closed")

    def stat(self):
        return types.SimpleNamespace(st_size=len(self.body))

    def prefetch(self, *a):                               # the whole file at once: must not be used
        raise AssertionError("read_stream must not prefetch the whole file")

    def readv(self, chunks):
        for offset, length in chunks:
            self.log.append((offset, length))
            end = offset + length if self.short_at is None else min(offset + length, self.short_at)
            yield self.body[offset:end]


def _executor(handle):
    from aiscientist.gateway.ssh_gateway import SSHExecutor
    ex = object.__new__(SSHExecutor)
    closed: list = []
    ex._open_sftp = lambda: types.SimpleNamespace(open=lambda path, mode: handle,
                                                  close=lambda: closed.append(True))
    return ex, closed


def test_a_remote_file_is_read_one_window_at_a_time():
    body = bytes(range(256)) * 1000                       # 256 kB
    log: list = []
    ex, closed = _executor(_Handle(body, log))
    stream = ex.read_stream("/dfs3b/x/S3.h5ad", block=100_000)
    first = next(stream)
    assert log == [(0, 100_000)]                          # the next window waits for this one to be taken
    assert first + b"".join(stream) == body
    assert log == [(0, 100_000), (100_000, 100_000), (200_000, 56_000), "closed"] and closed == [True]


def test_a_remote_file_that_ends_early_is_an_error_not_a_short_download():
    from aiscientist.gateway.errors import GatewayError
    log: list = []
    ex, closed = _executor(_Handle(b"x" * 1000, log, short_at=600))
    stream = ex.read_stream("/dfs3b/x/S3.h5ad", block=400)
    assert next(stream) == b"x" * 400 and next(stream) == b"x" * 200
    with pytest.raises(GatewayError, match="ended at 600 of 1000"):
        next(stream)
    assert closed == [True]
