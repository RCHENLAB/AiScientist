"""Uploads and results kept on the gateway host (RCHENLAB/AiScientist issue 6).

With a data root, the gateway host is the one persistent home of a user's data: an upload is checked
and published into ``users/<account>/datasets/<uuid>/original`` and nothing goes to HPC3 at upload time;
a run on HPC3 reads a copy made in the session's own area when it starts; a result kept on HPC3 is
copied back when it is saved; a dataset that lived only on HPC3 is brought here when its owner connects.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import types
import zipfile
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from test_tus_upload import env, _create, _patch  # noqa: F401 - env is a fixture used here

LAB = "/dfs3b/ruic20_lab"
SHARED = f"{LAB}/software/AiScientist"


@pytest.fixture()
def kept(env, tmp_path, monkeypatch):  # noqa: F811 - the env fixture imported above
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    return env


def _job(client, gw, conn, url):
    result = client.get(url).json()
    assert result["job_id"], result
    job = gw._staging_queue(conn).wait(result["job_id"], 5)
    assert job is not None, "the check did not settle"
    return job


class FakeHpc:
    """An HPC3 session over a dict of ``path -> bytes``: answers the probes, the listings and the
    copies the gateway makes (both ways)."""

    def __init__(self, files: "dict[str, bytes] | None" = None, username: str = "tester"):
        self.files = dict(files or {})
        self.username = username
        self.calls: list[str] = []

    def _dirs(self) -> set:
        return {p.rsplit("/", i)[0] for p in self.files for i in range(1, p.count("/"))}

    def exec(self, cmd: str, timeout: float = 60.0):
        self.calls.append(cmd)
        out, ok = "", True
        if cmd.startswith("if test -d"):
            path = cmd.split("test -d ", 1)[1].split(";", 1)[0].strip("'")
            out = "dir" if path in self._dirs() else "file" if path in self.files else ""
        elif cmd.startswith("find "):
            root = cmd.split("find ", 1)[1].split(" -type", 1)[0].strip("'")
            out = "".join(f"{p[len(root) + 1:]}\t{len(b)}\n" for p, b in self.files.items()
                          if p.startswith(root + "/"))
        return types.SimpleNamespace(ok=ok, stdout=out, stderr="", out=out, exit_status=0)

    def remote_size(self, path: str) -> int:
        return len(self.files.get(path, b""))

    def get_file(self, remote: str, local: str) -> None:
        Path(local).write_bytes(self.files[remote])

    def put_file(self, local: str, remote: str) -> None:
        self.files[remote] = Path(local).read_bytes()


# --- uploads ---------------------------------------------------------------------------------------

def test_a_file_that_does_not_match_the_browsers_checksum_is_never_published(kept):
    client, conn, gw = kept
    from aiscientist.gateway import dataset_storage
    url = _create(client, conn, 3, client_sha256=hashlib.sha256(b"xyz").hexdigest()).headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 204          # the request answers at once
    job = _job(client, gw, conn, url)
    assert job.state == "failed" and "SHA-256" in job.error
    assert not dataset_storage.datasets_root("tester").exists()


def test_a_matching_checksum_is_recorded_as_how_the_file_was_checked(kept):
    client, conn, gw = kept
    url = _create(client, conn, 3, client_sha256=hashlib.sha256(b"abc").hexdigest()).headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 204
    job = _job(client, gw, conn, url)
    assert job.state == "done" and job.verified == "sha256" and job.sha256 == hashlib.sha256(b"abc").hexdigest()


def test_an_archive_is_unpacked_on_the_server_into_its_dataset_folder(kept):
    client, conn, gw = kept
    from aiscientist.gateway import dataset_storage
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("run1/a.csv", "g,c\nRHO,1\n")
        z.writestr("run1/sub/b.txt", "hello")
    data = buf.getvalue()
    created = client.post("/api/tus", headers={
        "Tus-Resumable": "1.0.0", "Upload-Length": str(len(data)),
        "Upload-Metadata": ",".join(f"{k} {__import__('base64').b64encode(v.encode()).decode()}" for k, v in
                                    {"connection_id": conn.id, "filename": "run1.zip", "unpack": "1"}.items())})
    url = created.headers["location"]
    assert _patch(client, url, 0, data).status_code == 204
    job = gw._staging_queue(conn).wait(client.get(url).json()["job_id"], 20)
    assert job is not None and job.state == "done", job and job.error
    out = Path(job.unpacked["path"])
    tree = dataset_storage.managed_tree(out)
    assert tree is not None and dataset_storage.managed_owner(out) == "tester"
    assert out.parent == tree / "processed"
    found = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert found == ["a.csv", "sub/b.txt"]
    assert conn.executor.state.staged_files == []                     # nothing went to HPC3


def test_a_check_interrupted_by_a_restart_continues_without_a_session(kept, tmp_path):
    client, conn, gw = kept
    from aiscientist.gateway import dataset_storage, staging, upload_storage
    identity = str(uuid4())
    local = upload_storage.uploads(conn) / identity / "late.csv"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"late")
    final = dataset_storage.dataset_dir(identity, "tester") / "original" / "late.csv"
    store = staging.StagingStore(upload_storage.uploads(conn) / ".staging")
    job = staging.new_job(owner="tester", local_path=str(local), remote_path=str(final), name="late.csv",
                          size=4, dest="server")
    job.state = "pushing"                       # what a process that died mid-check left
    store.save(job)
    gw._STAGING.pop("tester", None)
    gw.CONNECTIONS.pop(conn.id, None)           # nobody is connected
    try:
        assert gw._resume_kept_uploads() == 1
        settled = gw._STAGING["tester"].wait(job.id, 5)
        assert settled is not None and settled.state == "done", settled and settled.error
        assert final.read_bytes() == b"late" and not local.exists()
    finally:
        gw.CONNECTIONS[conn.id] = conn


# --- a run on HPC3 reads a copy made for its session -----------------------------------------------

def _live(gw, tmp_path, monkeypatch, files=None):
    """A real (not mock) session whose analysis runs on HPC3, over :class:`FakeHpc`."""
    from aiscientist.gateway.settings import HPCSettings
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(), mock=False, loop=loop, username="tester")
    conn.workspace = tmp_path / "workspace"
    conn.settings.analysis_on_hpc = True
    conn.executor = FakeHpc(files)
    gw.CONNECTIONS[conn.id] = conn
    return conn


def test_a_dataset_kept_here_is_copied_to_the_sessions_area_when_a_run_needs_it(kept, tmp_path, monkeypatch):
    _client, _conn, gw = kept
    from aiscientist.gateway import dataset_storage
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    conn = _live(gw, tmp_path, monkeypatch)
    try:
        identity = str(uuid4())
        mine = dataset_storage.dataset_dir(identity, "tester") / "original"
        (mine / "outs").mkdir(parents=True)
        (mine / "outs" / "matrix.h5").write_bytes(b"H5")
        theirs = dataset_storage.dataset_dir(str(uuid4()), "someone") / "original" / "x.csv"
        theirs.parent.mkdir(parents=True)
        theirs.write_text("a,b\n")
        said: list[str] = []
        bound = [{"path": str(mine), "name": "study", "role": None},
                 {"path": str(theirs), "name": "x.csv", "role": None}]
        admitted, mirrors = asyncio.run(gw._admit_bound_paths(
            conn, bound, lambda msg, *a: said.append(msg), lambda *a: None))
        assert [e["path"] for e in admitted] == [str(mine)]          # another account's is refused
        remote, copied = mirrors[str(mine)]
        base = gw._session_mirror_base(conn)
        assert remote.startswith(base + "/") and remote.endswith(f"/{identity}/original")
        assert copied == {"outs/matrix.h5"}
        assert conn.executor.files[f"{remote}/outs/matrix.h5"] == b"H5"
        assert conn.server_data_synced is True                       # so a disconnect removes it
        assert any("Your dataset" in m for m in said)
    finally:
        gw.CONNECTIONS.pop(conn.id, None)


# --- the Datasets tab ------------------------------------------------------------------------------

def test_a_dataset_kept_here_previews_without_an_hpc3_session(kept, tmp_path, monkeypatch):
    client, _conn, gw = kept
    from aiscientist.gateway import dataset_storage
    rows = {}
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw, "_optional_user", lambda request: types.SimpleNamespace(username="tester", id=5))
    monkeypatch.setattr(gw.auth_routes, "get_dataset_record",
                        lambda uid, did: dict(rows[did], id=did) if did in rows else None)
    monkeypatch.setattr(gw, "_active_conn_for_user", lambda user: None)
    mine = dataset_storage.dataset_dir(str(uuid4()), "tester") / "original" / "de.csv"
    mine.parent.mkdir(parents=True)
    mine.write_bytes(b"gene,padj\nRHO,0.001\n")
    rows[1] = {"path": str(mine), "kind": "other", "name": "de.csv", "size_bytes": mine.stat().st_size}
    body = client.get("/api/datasets/1/preview").json()
    assert body["dataset"]["location"] == "server" and body["preview"]["rows"] == [["RHO", "0.001"]]
    assert client.get("/api/datasets/1/download").content == mine.read_bytes()
    # a row of this account that names another account's dataset folder reads nothing here
    theirs = dataset_storage.dataset_dir(str(uuid4()), "someone") / "original" / "x.csv"
    theirs.parent.mkdir(parents=True)
    theirs.write_text("secret,1\n")
    rows[2] = {"path": str(theirs), "kind": "other", "name": "x.csv", "size_bytes": 9}
    assert client.get("/api/datasets/2/download").status_code == 409     # not read here, no session


def test_deleting_a_kept_dataset_removes_its_folder_and_never_another_accounts(kept, tmp_path, monkeypatch):
    client, conn, gw = kept
    pytest.importorskip("sqlalchemy")
    from aiscientist.gateway import auth_routes, dataset_storage, db, models
    db.reset(f"sqlite:///{(tmp_path / 'kept.db').as_posix()}")
    db.init_db()
    try:
        with db.session_scope() as session:
            user = models.User(username="tester", password_hash="x")
            session.add(user)
            session.commit()
            user_id = user.id
        monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
        monkeypatch.setattr(gw, "_optional_user", lambda request: types.SimpleNamespace(id=user_id, username="tester"))
        monkeypatch.setattr(gw, "_active_conn_for_user", lambda user: None)
        mine = dataset_storage.dataset_dir(str(uuid4()), "tester") / "original"
        mine.mkdir(parents=True)
        (mine / "a.csv").write_text("x")
        theirs = dataset_storage.dataset_dir(str(uuid4()), "someone") / "original"
        theirs.mkdir(parents=True)
        (theirs / "b.csv").write_text("y")
        own = auth_routes.record_folder_dataset(user_id, "mine", str(mine), 1)
        forged = auth_routes.record_folder_dataset(user_id, "forged", str(theirs), 1)
        assert client.post("/api/datasets/delete", json={"id": forged}).status_code == 409
        assert theirs.exists()
        assert client.post("/api/datasets/delete", json={"id": own}).status_code == 200
        assert not mine.parent.exists()
    finally:
        db.reset()


# --- results ---------------------------------------------------------------------------------------

def _pointer(gw, owner, run_id, rec):
    from aiscientist.tools.api import RESULT_DATASET_POINTER
    p = gw.CONSOLE_RUNS_DIR / owner / run_id / "artifacts" / "data" / RESULT_DATASET_POINTER
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def test_saving_a_local_runs_result_makes_it_a_dataset_in_the_owners_folder(kept, tmp_path, monkeypatch):
    _client, conn, gw = kept
    from aiscientist.gateway import dataset_storage
    monkeypatch.setattr(gw, "CONSOLE_RUNS_DIR", tmp_path / "runs")
    run_id, name = "abc123def456", "pbmc.processed.20261018-1200.abc123def456.h5ad"
    src = gw.CONSOLE_RUNS_DIR / "tester" / run_id / "result" / name
    src.parent.mkdir(parents=True)
    src.write_bytes(b"H5AD")
    pointer = _pointer(gw, "tester", run_id, {"name": name, "path": str(src), "location": "gateway",
                                              "size_bytes": 4})
    status, body = gw._save_run_result("tester", None, run_id, "dataset", None)
    assert status == 200, body
    dest = Path(body["path"])
    assert dataset_storage.is_owned(dest, "tester") and dest.name == name and dest.parent.name == "original"
    assert dest.read_bytes() == b"H5AD" and not src.exists()
    assert json.loads(pointer.read_text())["saved_to"] == str(dest)


def test_saving_a_result_kept_on_hpc3_copies_it_back_in_the_background(kept, tmp_path, monkeypatch):
    _client, _conn, gw = kept
    from aiscientist.gateway import dataset_storage
    monkeypatch.setattr(gw, "CONSOLE_RUNS_DIR", tmp_path / "runs")
    conn = _live(gw, tmp_path, monkeypatch)
    try:
        run_id, name = "fedcba987654", "s2.cellqc.20261018-1200.fedcba987654"
        src = f"{gw._temp_base(conn)}/analysis/{run_id}/result/cellqc"
        conn.executor.files.update({f"{src}/report.html": b"<html>", f"{src}/lib1/obs.csv": b"cell,qc\n"})
        from aiscientist.tools.api import CELLQC_RESULT_POINTER
        pointer = gw.CONSOLE_RUNS_DIR / "tester" / run_id / "artifacts" / "data" / CELLQC_RESULT_POINTER
        pointer.parent.mkdir(parents=True)
        pointer.write_text(json.dumps({"name": name, "path": src, "location": "hpc3", "files": [
            {"name": "report.html", "size": 6}], "size_bytes": 14}), encoding="utf-8")
        status, body = gw._save_run_result("tester", None, run_id, "cellqc", conn)
        assert status == 202 and body["saving"] is True
        assert gw._save_run_result("tester", None, run_id, "cellqc", conn)[0] == 202   # not twice
        for _ in range(100):
            rec = json.loads(pointer.read_text())
            if rec.get("saved_to") or rec.get("save_error"):
                break
            __import__("time").sleep(0.05)
        assert rec.get("saved_to"), rec
        dest = Path(rec["saved_to"])
        assert dataset_storage.is_owned(dest, "tester") and rec["location"] == "gateway"
        assert (dest / "report.html").read_bytes() == b"<html>"
        assert (dest / "lib1" / "obs.csv").read_bytes() == b"cell,qc\n"
        assert "saving" not in rec
        # the CellQC zip is now read from the server
        assert gw._cellqc_result_root("tester", run_id, rec, None) == str(dest)
    finally:
        gw.CONNECTIONS.pop(conn.id, None)


# --- datasets kept only on HPC3 are brought here ---------------------------------------------------

def test_an_hpc3_only_dataset_is_copied_here_and_its_row_repointed(kept, tmp_path, monkeypatch):
    _client, _conn, gw = kept
    pytest.importorskip("sqlalchemy")
    from aiscientist.gateway import auth_routes, dataset_storage, db, models
    db.reset(f"sqlite:///{(tmp_path / 'homing.db').as_posix()}")
    db.init_db()
    conn = _live(gw, tmp_path, monkeypatch)
    try:
        with db.session_scope() as session:
            user = models.User(username="tester", password_hash="x")
            session.add(user)
            session.commit()
            conn.app_user_id = user.id
        monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
        uploads = f"{SHARED}/uploads/tester"
        conn.executor.files.update({
            f"{uploads}/pbmc.h5ad": b"MATRIX",
            f"{uploads}/Sample2/outs/a.h5": b"A",
            f"{uploads}/Sample2/outs/b/c.tsv": b"BC",
            f"{LAB}/someone-else/uploads/x.h5ad": b"NOT MINE",
        })
        single = auth_routes.record_dataset(conn.app_user_id, "pbmc.h5ad", f"{uploads}/pbmc.h5ad", 6, "h5ad")
        folder = auth_routes.record_folder_dataset(conn.app_user_id, "Sample2", f"{uploads}/Sample2", 3)
        gone = auth_routes.record_dataset(conn.app_user_id, "gone.h5ad", f"{uploads}/gone.h5ad", 9, "h5ad")
        other = auth_routes.record_dataset(conn.app_user_id, "x.h5ad", f"{LAB}/someone-else/uploads/x.h5ad", 8, "h5ad")
        server = auth_routes.record_dataset(conn.app_user_id, "lab", "/data/Users/shared/lab", 1, "server-folder")
        out = gw._home_datasets(conn)
        assert sorted(out["moved"]) == ["Sample2", "pbmc.h5ad"] and out["missing"] == ["gone.h5ad"]
        rows = {r["id"]: r for r in auth_routes.list_dataset_records(conn.app_user_id)}
        p1, p2 = Path(rows[single]["path"]), Path(rows[folder]["path"])
        assert dataset_storage.is_owned(p1, "tester") and p1.read_bytes() == b"MATRIX"
        assert dataset_storage.managed_uuid(p1) == rows[single]["uuid"]
        assert (p2 / "outs" / "b" / "c.tsv").read_bytes() == b"BC" and rows[folder]["size_bytes"] == 3
        source = json.loads((dataset_storage.managed_tree(p1) / "source.json").read_text())
        assert source["hpc3_path"] == f"{uploads}/pbmc.h5ad"         # the original, for its cleanup
        assert rows[gone]["path"] == f"{uploads}/gone.h5ad"           # nothing to move: left as it was
        assert rows[other]["path"].startswith(LAB)                    # outside this session's storage
        assert rows[server]["path"] == "/data/Users/shared/lab"       # the lab's folder: never moved
        assert f"{uploads}/pbmc.h5ad" in conn.executor.files          # the HPC3 original is kept
        assert gw._home_datasets(conn)["moved"] == []                  # done once
    finally:
        gw.CONNECTIONS.pop(conn.id, None)
        db.reset()


def test_moving_hpc3_datasets_can_be_switched_off(kept, monkeypatch):
    _client, _conn, gw = kept
    assert gw._homing_enabled() is True
    monkeypatch.setenv("AISCIENTIST_MOVE_HPC_DATASETS", "0")
    assert gw._homing_enabled() is False
    monkeypatch.delenv("AISCIENTIST_DATA_ROOT")
    monkeypatch.delenv("AISCIENTIST_MOVE_HPC_DATASETS")
    assert gw._homing_enabled() is False                               # no data root: nowhere to move to
