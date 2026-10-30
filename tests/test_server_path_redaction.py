"""No server path reaches a user (RCHENLAB/AiScientist issue 37).

Error messages and tracebacks carry the paths of the files they failed on — the key stores, the
state dir, the code checkout. ``errors.redact_server_paths`` takes this host's paths out of
anything a user sees, keeping a traceback frame's module path and any other path's file name.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiscientist.gateway import errors
from aiscientist.gateway.errors import GatewayError, error_detail, public_error, redact_server_paths

STATE = "/srv/aisci-test/state"


@pytest.fixture(autouse=True)
def _state_dir(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", STATE)


def test_a_server_path_keeps_only_its_file_name():
    text = f"No such file or directory: '{STATE}/llm_creds/alice/abc.key'"
    assert redact_server_paths(text) == "No such file or directory: '…/abc.key'"


def test_paths_under_the_checkout_home_and_cwd_are_hidden():
    for root in (Path(errors.__file__).resolve().parents[3], Path.home(), Path.cwd()):
        out = redact_server_paths(f"Could not create {root}/runs/console/alice/r1/artifacts: denied")
        assert str(root) not in out
        assert out == "Could not create …/artifacts: denied"


def test_a_root_matches_only_whole_directory_names():
    for other in (f"{STATE}2/x.key", f"{STATE}.bak/x.key"):
        assert redact_server_paths(other) == other
    assert redact_server_paths(f"in {STATE}.") == "in …."


def test_hpc3_paths_are_the_users_own_and_stay():
    for text in ("/dfs3b/ruic20_lab/alice/data.h5ad", "/data/homezvol0/alice/run.sh: not found",
                 "/tmp/aiscientist_job/run.sh"):
        assert redact_server_paths(text) == text


def test_public_error_never_shows_the_path_and_is_never_empty():
    with pytest.raises(OSError) as info:
        open(f"{STATE}/ssh_creds/alice/k1.key", encoding="utf-8")
    assert STATE not in public_error(info.value)
    assert public_error(info.value).endswith("'…/k1.key'")
    assert public_error(RuntimeError()) == "RuntimeError"


def test_error_detail_tracebacks_keep_module_paths_only():
    try:
        open(f"{STATE}/llm_key.master", encoding="utf-8")
    except OSError as exc:
        try:
            raise GatewayError("could not read the key", stage="llm") from exc
        except GatewayError as outer:
            detail = error_detail(outer)
    dumped = repr(detail)
    assert STATE not in dumped
    assert str(Path(errors.__file__).resolve().parents[2]) not in dumped, "no checkout path"
    assert 'File "' in detail["traceback"]
    assert os.path.basename(__file__) in detail["traceback"], "the frame's file is still named"
    assert detail["cause"]["message"].endswith("'…/llm_key.master'")


def test_a_saved_ssh_key_s_path_is_not_in_its_errors(tmp_path):
    from aiscientist.gateway.ssh_gateway import SSHExecutor

    missing = tmp_path / "ssh_creds" / "alice" / "k1.key"
    with pytest.raises(GatewayError) as info:
        SSHExecutor._load_key(str(missing), None)
    assert str(tmp_path) not in info.value.message

    broken = tmp_path / "k2.key"
    broken.write_text("not a key", encoding="utf-8")
    with pytest.raises(GatewayError) as info:
        SSHExecutor._load_key(str(broken), None)
    assert str(tmp_path) not in info.value.message
    assert str(tmp_path) not in repr(info.value.detail)


# --- the chat: a failed run's message (run 82fab7a4a437) -----------------------------------------
#
# A GatewayError's message went to the chat as it was, so an upload that failed showed the user's
# dataset folder on the gateway host: "Failed to upload /data/aiscientist/data/users/lijinbio/
# datasets/<uuid>/original/analysis/…/clusters.csv -> /dfs3b/…".

HPC3_DEST = "/dfs3b/ruic20_lab/lijinbio/aiscientist/82fab7a4a437/clusters.csv"


def _upload_failure(data_root: str) -> str:
    return (f"Failed to upload {data_root}/users/lijinbio/datasets/3f2c9a1e-5b7d-4c8e-9f0a-1b2c3d4e5f60"
            f"/original/analysis/leiden/clusters.csv -> {HPC3_DEST}.")


def _connection():
    import asyncio

    pytest.importorskip("fastapi")
    from aiscientist.gateway.app import Connection
    from aiscientist.gateway.settings import HPCSettings

    conn = Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    published: list[dict] = []
    conn._publish = lambda payload: published.append(dict(payload))
    return conn, published


def test_a_chat_error_loses_the_server_path_live_and_on_replay(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", "/data/aiscientist/data")
    conn, published = _connection()
    conn.push({"type": "chat_start"})
    conn.push({"type": "chat_error", "message": _upload_failure("/data/aiscientist/data")})

    shown = next(p for p in published if p["type"] == "chat_error")["message"]
    assert shown == f"Failed to upload …/clusters.csv -> {HPC3_DEST}."
    replayed = conn.stream_replay_payloads()[-1]
    assert replayed["type"] == "chat_error" and replayed["message"] == shown


def test_a_lab_run_that_fails_on_an_upload_shows_no_server_path(tmp_path, monkeypatch):
    import asyncio

    pytest.importorskip("fastapi")
    from aiscientist.agents import research_lab as rl
    from aiscientist.gateway import app as gw_app

    data_root = tmp_path / "data"
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(data_root))
    message = _upload_failure(str(data_root))

    class _FailingLab:
        def __init__(self, *a, **k):
            self.guidance = None
            self.protocols = []
            self.self_sourced = ""
            self.execution_profile = {}

        def run(self, *a, **k):
            raise GatewayError(message, stage="ssh_put")

    monkeypatch.setattr(rl, "ResearchLab", _FailingLab)
    conn, published = _connection()
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = object()
    gw_app.CONNECTIONS[conn.id] = conn
    try:
        req = gw_app.LabRequest(connection_id=conn.id, question="cluster this", conversation_id="c1")
        conn.begin_run("c1")
        asyncio.run(gw_app._run_lab(conn, req))
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)

    error = next(p for p in published if p["type"] == "chat_error")["message"]
    assert str(data_root) not in error and "lijinbio/datasets" not in error
    assert error == f"Failed to upload …/clusters.csv -> {HPC3_DEST}."
    logged = [e for e in conn.log if e["level"] == "error" and e["stage"] == "ssh_put"]
    assert logged and str(data_root) not in logged[-1]["message"]


def test_transfer_failures_name_the_file_not_its_server_folder(tmp_path, monkeypatch):
    from aiscientist.gateway.ssh_gateway import SSHExecutor

    folder = tmp_path / "users" / "lijinbio" / "datasets" / "u1" / "original"
    folder.mkdir(parents=True)
    local = folder / "clusters.csv"
    local.write_text("cell,cluster\n", encoding="utf-8")

    class _BrokenSFTP:
        def put(self, *a, **k):
            raise OSError("Permission denied")

        get = put

        def close(self):
            pass

    ex = SSHExecutor.__new__(SSHExecutor)
    monkeypatch.setattr(ex, "exec", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ex, "_open_sftp", lambda: _BrokenSFTP(), raising=False)

    with pytest.raises(GatewayError) as up:
        ex.put_file(str(local), HPC3_DEST)
    assert up.value.message == f"Failed to upload clusters.csv to {HPC3_DEST}."

    with pytest.raises(GatewayError) as down:
        ex.get_file(HPC3_DEST, str(local))
    assert down.value.message == f"Failed to download {HPC3_DEST}."
    assert str(tmp_path) not in up.value.message + down.value.message


# --- the dialogs and the progress feed (RCHENLAB/AiScientist issue 71) ------------------------------
#
# A route that failed answered {"error": str(exc)} and the console showed it in a dialog; a warning
# or an error event put f"…: {exc}" into the progress feed. An exception about a file on this host
# names its folder there. RedactErrorReplies filters every error reply and Connection.emit every
# warning and error; the journal keeps the unfiltered text.

UUID = "3f2c9a1e-5b7d-4c8e-9f0a-1b2c3d4e5f60"


def test_every_string_in_a_reply_or_a_detail_loses_the_server_path():
    value = {"error": f"Could not read {STATE}/llm_key.master", "stage": "llm",
             "paths": [f"{STATE}/ssh_creds/alice/k1.key", HPC3_DEST], "n": 2, "ok": None,
             "pair": (f"in {STATE}", 1)}
    assert errors.redact_server_paths_in(value) == {
        "error": "Could not read …/llm_key.master", "stage": "llm", "paths": ["…/k1.key", HPC3_DEST],
        "n": 2, "ok": None, "pair": ("in …", 1)}
    assert errors.redact_server_paths_in(None) is None


def test_the_config_folder_the_settings_file_links_into_is_a_server_folder(tmp_path, monkeypatch):
    config = tmp_path / "srv" / "config"
    release = tmp_path / "srv" / "app" / "releases" / "v0.4.0"
    config.mkdir(parents=True)
    release.mkdir(parents=True)
    (config / ".env").write_text("AISCIENTIST_RESULTS_DIR=/x\n", encoding="utf-8")
    (release / ".env").symlink_to(config / ".env")
    monkeypatch.chdir(release)
    out = redact_server_paths(f"Could not read {config}/backups/llm_creds.tgz.enc")
    assert out == "Could not read …/llm_creds.tgz.enc"


def test_the_reply_filter_takes_every_error_reply_and_leaves_the_others():
    pytest.importorskip("httpx")
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    key = f"{STATE}/ssh_creds/alice/k1.key"
    app = FastAPI()
    app.add_middleware(errors.RedactErrorReplies)

    @app.get("/raised")
    def raised():
        raise HTTPException(status_code=409, detail=f"Could not read {key}")

    @app.get("/saved")
    def saved():
        return {"ok": True, "path": key}

    @app.get("/hpc3")
    def hpc3():
        return JSONResponse({"error": f"Could not read {HPC3_DEST}"}, status_code=502)

    client = TestClient(app)
    assert client.get("/raised").json() == {"detail": "Could not read …/k1.key"}
    assert client.get("/saved").json() == {"ok": True, "path": key}, "a reply that is not an error is left alone"
    r = client.get("/hpc3")
    assert r.json() == {"error": f"Could not read {HPC3_DEST}"}
    assert int(r.headers["content-length"]) == len(r.content)


@pytest.fixture
def gw(tmp_path, monkeypatch):
    """The gateway app with this test's data root (the users' datasets) and results dir."""
    pytest.importorskip("httpx")
    gw_app = pytest.importorskip("aiscientist.gateway.app")
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path / "runs")
    saved = dict(gw_app.CONNECTIONS)
    yield gw_app
    gw_app.CONNECTIONS.clear()
    gw_app.CONNECTIONS.update(saved)


def _client(gw_app):
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app)


def _signed_in(gw_app, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_optional_user",
                        lambda request: SimpleNamespace(username="alice", id=5, role="user"))


def test_a_failed_dataset_preview_names_the_file_not_its_folder(gw, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    _signed_in(gw, monkeypatch)
    rows: dict[int, dict] = {}
    monkeypatch.setattr(gw.auth_routes, "get_dataset_record", lambda uid, did: rows.get(did))
    client = _client(gw)

    # Kept on this host, in the user's dataset folder, and unreadable.
    table = tmp_path / "data" / "users" / "alice" / "datasets" / UUID / "original" / "de.csv"
    table.parent.mkdir(parents=True)
    table.write_text("gene,padj\nRHO,0.001\n", encoding="utf-8")
    rows[1] = {"id": 1, "path": str(table), "kind": "file", "name": "de.csv", "size_bytes": 20}
    table.chmod(0)
    try:
        if os.access(table, os.R_OK):
            pytest.skip("running as root: a file without permissions is still readable")
        r = client.get("/api/datasets/1/preview")
    finally:
        table.chmod(0o644)
    assert r.status_code == 500
    assert r.json() == {"error": "The file could not be read: [Errno 13] Permission denied: '…/de.csv'"}
    assert str(tmp_path) not in r.text
    assert str(table) in capsys.readouterr().out, "the journal keeps the full error"

    # Kept on HPC3: the HPC3 path is the user's own and stays; the key's path on this host goes.
    calls = "/dfs3b/ruic20_lab/alice/AiScientist_results/calls.vcf"

    class _Session:
        def exec(self, command, timeout=60.0):
            return SimpleNamespace(ok=True, stdout="file", stderr="")

        def remote_size(self, path):
            return 10

        def read_bytes(self, path, max_bytes=None):
            raise GatewayError(f"Could not read {path}: the key {STATE}/ssh_creds/alice/k1.key was refused")

    monkeypatch.setattr(gw, "_active_conn_for_user", lambda user: SimpleNamespace(executor=_Session()))
    monkeypatch.setattr(gw, "_hpc_uploads_dirs", lambda c: ("/dfs3b/proj/AiScientist/uploads/alice",))
    monkeypatch.setattr(gw, "_storage_base", lambda c: "/dfs3b/ruic20_lab/alice")
    rows[2] = {"id": 2, "path": calls, "kind": "file", "name": "calls.vcf", "size_bytes": 10}
    r = client.get("/api/datasets/2/preview")
    assert r.status_code == 502
    assert r.json()["error"] == f"The file could not be read: Could not read {calls}: the key …/k1.key was refused"


def test_a_storage_deletion_that_fails_names_the_script_not_the_code_folder(gw, monkeypatch, capsys):
    import asyncio
    from types import SimpleNamespace

    from aiscientist.gateway import storage_delete
    from aiscientist.gateway.settings import HPCSettings

    root = "/dfs3b/proj/AiScientist"

    class _Session:
        username = "alice"

        def exec(self, command, timeout=60.0):
            return SimpleNamespace(ok=True, exit_status=0, stdout="", stderr="")

        def put_file(self, local, remote):          # a deploy that did not ship the script
            raise FileNotFoundError(2, "No such file or directory", local)

    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(shared_root=root), mock=False, loop=loop, username="alice")
    loop.close()
    conn.executor = _Session()
    gw.CONNECTIONS[conn.id] = conn
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    monkeypatch.setattr(gw, "_storage_areas", lambda c: [("Your earlier sessions", f"{root}/sessions")])

    r = _client(gw).post("/api/storage/delete",
                         json={"connection_id": conn.id, "path": f"{root}/sessions/s0/runs/r1"})
    assert r.status_code == 500
    assert r.json() == {"error": "The deletion could not be submitted on HPC3: [Errno 2] No such file or "
                                 f"directory: '…/{storage_delete.SCRIPT_SRC.name}'"}
    assert int(r.headers["content-length"]) == len(r.content)
    assert str(storage_delete.SCRIPT_SRC.parent) not in r.text
    assert str(storage_delete.SCRIPT_SRC) in capsys.readouterr().out, "the journal keeps the full error"


def test_a_table_save_that_fails_names_the_file_not_the_dataset_folder(gw, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from aiscientist.gateway import corpus_scan

    _signed_in(gw, monkeypatch)
    folder = tmp_path / "runs" / "alice" / "cur123" / "artifacts" / "curation"
    folder.mkdir(parents=True)
    (folder / corpus_scan.CURATION_GENES).write_text("gene,claim\nPRPF31,haploinsufficient\n")
    gw._point_at_curation_tables(SimpleNamespace(workspace=tmp_path / "runs" / "alice"), "cur123",
                                 folder.parent, {"files": [corpus_scan.CURATION_GENES],
                                                 "counts": {"haploinsufficient": 1, "contested": 0}})
    target = tmp_path / "data" / "users" / "alice" / "datasets" / UUID / "original"

    def full_disk(staged, identity, owner):
        raise OSError(28, "No space left on device", str(target / corpus_scan.CURATION_GENES))

    monkeypatch.setattr(gw.dataset_storage, "publish_dataset", full_disk)
    r = _client(gw).post("/api/result-datasets/cur123/save?what=tables")
    assert r.status_code == 500
    assert r.json() == {"error": "Could not save the tables: [Errno 28] No space left on device: "
                                 f"'…/{corpus_scan.CURATION_GENES}'"}
    assert str(tmp_path / "data") not in r.text
    assert str(target) in capsys.readouterr().out, "the journal keeps the full error"


def test_a_feed_warning_or_error_loses_the_server_path_and_the_journal_keeps_it(monkeypatch, capsys):
    data = "/data/aiscientist/data"
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", data)
    folder = f"{data}/users/alice/datasets/{UUID}/original"
    conn, published = _connection()

    conn.emit("warning", "lab", f"Could not copy {folder}/pbmc.h5ad to {HPC3_DEST} again: denied",
              detail={"paths": [f"{folder}/pbmc.h5ad", HPC3_DEST], "n": 1})
    conn.emit("error", "storage", f"Could not read {STATE}/llm_key.master")
    conn.emit("warning", "upload", f"Could not checksum {HPC3_DEST} on HPC3 (timeout); its size matched.")

    warning, error, hpc3 = published
    assert warning["message"] == f"Could not copy …/pbmc.h5ad to {HPC3_DEST} again: denied"
    assert warning["detail"] == {"paths": ["…/pbmc.h5ad", HPC3_DEST], "n": 1}
    assert error["message"] == "Could not read …/llm_key.master"
    assert hpc3["message"] == f"Could not checksum {HPC3_DEST} on HPC3 (timeout); its size matched."
    assert conn.log == published, "a late subscriber's replay and the bundle's event log are the same"
    journal = capsys.readouterr().out
    assert f"{folder}/pbmc.h5ad" in journal and f"{STATE}/llm_key.master" in journal
    assert "checksum" not in journal, "a line with nothing to hide is not repeated in the journal"


def test_a_processed_dataset_not_exported_names_the_file_not_its_folder(tmp_path, monkeypatch, capsys):
    import asyncio

    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw_app
    from aiscientist.tools._lib import result_dataset

    data_root = tmp_path / "data"
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(data_root))
    source = data_root / "users" / "alice" / "datasets" / UUID / "original" / "pbmc.h5ad"

    def unreadable(workspace, name, meta):
        raise OSError(5, "Input/output error", str(source))

    monkeypatch.setattr(result_dataset, "export", unreadable)
    conn, published = _connection()
    conn.workspace = tmp_path / "runs" / "alice"
    out = asyncio.run(gw_app._export_result_dataset(
        conn, "r1", {"dataset_path": str(source), "input_name": "pbmc.h5ad"}, None, "cluster it",
        conn.emit_fn()))

    assert out is None
    (warning,) = [p for p in published if p.get("level") == "warning"]
    assert warning["message"] == ("Processed dataset not exported (non-fatal): [Errno 5] Input/output "
                                  "error: '…/pbmc.h5ad'")
    assert str(source) in capsys.readouterr().out


def test_could_not_resume_unfinished_uploads_names_the_file_not_its_folder(tmp_path, monkeypatch, capsys):
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw_app

    data_root = tmp_path / "data"
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(data_root))
    record = data_root / "temp" / "alice" / "uploads" / ".staging" / "0a1b2c3d.json"

    class _Queue:
        def resume(self):
            raise PermissionError(13, "Permission denied", str(record))

    monkeypatch.setattr(gw_app, "_uploads_queued", lambda c: True)
    monkeypatch.setattr(gw_app, "_staging_queue", lambda c: _Queue())
    conn, published = _connection()
    gw_app._resume_staging(conn)

    (warning,) = published
    assert warning["level"] == "warning" and warning["stage"] == "upload"
    assert warning["message"] == ("Could not resume unfinished uploads: [Errno 13] Permission denied: "
                                  "'…/0a1b2c3d.json'")
    assert str(data_root) not in repr(conn.log)
    assert str(record) in capsys.readouterr().out


def test_a_saved_result_is_named_as_datasets_lists_it_never_by_its_server_folder(tmp_path, monkeypatch):
    """The success line after a run's results are saved said "saved to your data: /data/…/original/…"."""
    import asyncio
    from types import SimpleNamespace

    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw_app

    folder = f"/data/aiscientist/data/users/alice/datasets/{UUID}/original"
    replies = {
        "dataset": (200, {"ok": True, "path": f"{folder}/pbmc.processed.r7.h5ad", "dataset_id": 3,
                          "name": "pbmc.processed.r7.h5ad", "kind": "file"}),
        "cellqc": (200, {"ok": True, "path": folder, "name": "pbmc.cellqc.r7", "already": True}),
        "literature": (200, {"ok": True, "path": folder, "dataset_id": 4, "name": "pbmc.literature.r7",
                             "kind": "folder"}),
    }
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_result_dataset_pointers",
                        lambda art: [{"what": "dataset", "name": "pbmc.processed.r7.h5ad"}])
    monkeypatch.setattr(gw_app, "_read_pointer", lambda path: {})
    monkeypatch.setattr(gw_app, "_save_run_result", lambda owner, uid, run_id, what, conn: replies[what])
    conn = SimpleNamespace(owner="alice", app_user_id=5, executor=None, workspace=tmp_path / "alice")
    said: list = []
    asyncio.run(gw_app._autosave_results(conn, "r7", lambda level, stage, msg: said.append((level, msg))))

    assert [level for level, _ in said] == ["success"] * 3
    assert all("/data/" not in msg for _, msg in said)
    assert said[0][1] == ("💾 The processed dataset pbmc.processed.r7.h5ad is saved to your data: it is in "
                          "Datasets, ready for the next analysis.")
    assert "CellQC's result directory is saved to your data as “pbmc.cellqc.r7”" in said[1][1]
    assert "The literature tables is saved to your data as “pbmc.literature.r7”" in said[2][1]


def test_saving_again_answers_with_the_name_too(gw, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from aiscientist.gateway import corpus_scan

    _signed_in(gw, monkeypatch)
    monkeypatch.setattr(gw.auth_routes, "dataset_path_recorded", lambda uid, path: False)
    monkeypatch.setattr(gw.auth_routes, "record_dataset", lambda *a, **kw: 46)
    folder = tmp_path / "runs" / "alice" / "cur123" / "artifacts" / "curation"
    folder.mkdir(parents=True)
    (folder / corpus_scan.CURATION_GENES).write_text("gene,claim\nPRPF31,haploinsufficient\n")
    gw._point_at_curation_tables(SimpleNamespace(workspace=tmp_path / "runs" / "alice"), "cur123",
                                 folder.parent, {"files": [corpus_scan.CURATION_GENES],
                                                 "counts": {"haploinsufficient": 1, "contested": 0}})
    first = _client(gw).post("/api/result-datasets/cur123/save?what=tables").json()
    again = _client(gw).post("/api/result-datasets/cur123/save?what=tables").json()
    assert again["already"] is True and again["name"] == first["name"] and first["name"]
    assert again["path"] == first["path"], "the reply keeps the path for the console's own use"
