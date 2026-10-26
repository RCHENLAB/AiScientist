"""Issue 5's storage, as merged onto the deployed layout: what happens with no data root configured,
and the guards the review of RCHENLAB/AiScientist PR 12 added (delete, register, the per-file copy).
Every filesystem change stays inside tmp_path; HPC3 is a recording fake."""
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from test_tus_upload import env, _create, _patch  # noqa: F401 - env is a fixture used here


@pytest.fixture()
def unconfigured(env, monkeypatch):  # noqa: F811 - the env fixture imported above
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.delenv("AISCIENTIST_DATA_ROOT", raising=False)
    return env


@pytest.fixture()
def managed(env, tmp_path, monkeypatch):  # noqa: F811 - the env fixture imported above
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    return env


def test_without_a_data_root_uploads_stay_in_the_account_folder(unconfigured):
    """A deploy that sets no data root changes nothing: no UUID folders, nothing under a home
    directory, the file lands in the account's uploads/ as before."""
    client, conn, gw = unconfigured
    from aiscientist.gateway import dataset_storage, upload_storage
    assert not dataset_storage.configured() and not upload_storage.enabled(conn)
    url = _create(client, conn, 3).headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 204
    path = Path(client.get(url).json()["path"])
    assert path == conn.workspace / "uploads" / "data.csv"
    assert path.read_bytes() == b"abc"


def test_a_folder_this_browser_started_continues_as_before(unconfigured):
    """Without managed storage the browser's own record of finished files stands: naming its
    reserved folder answers "resume" and the folder is not reserved a second time."""
    client, conn, gw = unconfigured
    body = {"connection_id": conn.id, "name": "study", "files": [["a.csv", 3], ["b.csv", 2]]}
    first = client.post("/api/upload/reserve-folder", json=body).json()
    assert first["folder"] == "study" and "resume" not in first
    again = client.post("/api/upload/reserve-folder", json={**body, "folder": "study"}).json()
    assert again["folder"] == "study" and again["resume"] is True and again["have"] == []
    # a name that is not a reserved folder here is not trusted: the folder is reserved as usual
    other = client.post("/api/upload/reserve-folder", json={**body, "folder": "../elsewhere"}).json()
    assert "resume" not in other


def test_a_managed_folder_resume_names_a_stale_or_older_folder(managed):
    """With managed storage a saved folder that is not a live session (expired, or a name from before
    the data root) gets a new session instead of an error."""
    client, conn, gw = managed
    body = {"connection_id": conn.id, "name": "study", "files": [["a.csv", 3]]}
    got = client.post("/api/upload/reserve-folder", json={**body, "folder": "study"})
    assert got.status_code == 200, got.text
    UUID(got.json()["folder"])


def test_a_managed_hpc_folder_with_nothing_handed_over_is_not_recorded(managed):
    client, conn, gw = managed
    conn.settings.uploads_on_hpc = True
    folder = client.post("/api/upload/reserve-folder", json={
        "connection_id": conn.id, "name": "study", "files": [["a.csv", 3]]}).json()["folder"]
    got = client.post("/api/upload/register-folder", json={"connection_id": conn.id, "folder": folder, "count": 1})
    assert got.status_code == 409
    assert gw._staging_queue(conn).store.folder(folder).get("recorded") is not True


def test_a_failed_copy_names_its_file_in_the_folder(unconfigured):
    client, conn, gw = unconfigured
    assert gw._path_in_folder("/hpc/uploads/u/temp/1b4e28ba-2fa1-11d2-883f-0016d3cca427/original/x/a.h5", "f") == "x/a.h5"
    assert gw._path_in_folder("/hpc/uploads/u/study/x/a.h5", "study") == "x/a.h5"


@pytest.fixture()
def accounts(env, tmp_path, monkeypatch):  # noqa: F811 - the env fixture imported above
    """The delete route with accounts on, one user, and an HPC3 session whose commands are recorded."""
    pytest.importorskip("sqlalchemy")
    client, conn, gw = env
    from aiscientist.gateway import auth_routes, db, models
    db.reset(f"sqlite:///{(tmp_path / 'guards.db').as_posix()}")
    db.init_db()
    with db.session_scope() as session:
        user = models.User(username="tester", password_hash="x")
        session.add(user)
        session.commit()
        user_id = user.id
    ran: list[str] = []
    conn.executor = SimpleNamespace(exec=lambda cmd, *a, **k: ran.append(cmd) or SimpleNamespace(
        ok=True, stdout="", stderr="", out=""))
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw, "_optional_user", lambda request: SimpleNamespace(id=user_id, username="tester"))
    monkeypatch.setattr(gw, "_active_conn_for_user", lambda user: conn)
    monkeypatch.setattr(gw, "_is_remote_dataset", lambda c, p: str(p).startswith("/dfs3b/"))
    monkeypatch.setattr(gw, "_hpc_uploads_dirs", lambda c: ("/dfs3b/lab/sw/uploads/tester",
                                                            "/dfs3b/lab/tester/uploads"))
    monkeypatch.setattr(gw, "_storage_base", lambda c: "/dfs3b/lab/tester")
    yield client, conn, gw, auth_routes, db, models, user_id, ran
    db.reset()


def test_a_saved_result_can_be_deleted_but_not_the_rest_of_the_lab_folder(accounts):
    """"Save to my data" puts results in <lab folder>/<user>/AiScientist_results/: deleting one
    removes it. Anything else in the personal lab folder is the user's own and is refused."""
    client, conn, gw, auth_routes, db, models, user_id, ran = accounts
    saved = auth_routes.record_dataset(user_id, "r.h5ad", "/dfs3b/lab/tester/AiScientist_results/r.h5ad", 1, "h5ad")
    got = client.post("/api/datasets/delete", json={"id": saved})
    assert got.status_code == 200, got.text
    assert ran == ["rm -rf -- /dfs3b/lab/tester/AiScientist_results/r.h5ad"]
    own = auth_routes.record_dataset(user_id, "notes", "/dfs3b/lab/tester/thesis/notes.txt", 1, "txt")
    assert client.post("/api/datasets/delete", json={"id": own}).status_code == 409
    assert len(ran) == 1
    with db.session_scope() as session:          # (SQLite may give the second row the first one's id)
        assert [r.path for r in session.query(models.Dataset).all()] == ["/dfs3b/lab/tester/thesis/notes.txt"]


def test_a_bound_server_folder_is_a_reference_even_when_shared(accounts, tmp_path, monkeypatch):
    """A bound lab folder is never deleted, and its row goes even when another row names the same
    folder (the overlap guard is for files that are deleted)."""
    client, conn, gw, auth_routes, db, models, user_id, ran = accounts
    from aiscientist.gateway import server_data as sd
    shared = tmp_path / "shared" / "jin" / "Sample1"
    shared.mkdir(parents=True)
    (shared / "x.h5").write_bytes(b"x")
    monkeypatch.setattr(sd, "server_data_roots", lambda environ=None: ((tmp_path / "shared").resolve(),))
    monkeypatch.setattr(gw, "_session_mirror_base", lambda c: "/dfs3b/lab/sw/Temp/tester/server-data/s1")
    first = auth_routes.record_dataset(user_id, "Sample1", str(shared), 1, "folder")
    auth_routes.record_dataset(user_id, "Sample1 again", str(shared), 1, "folder")
    got = client.post("/api/datasets/delete", json={"id": first})
    assert got.status_code == 200, got.text
    assert (shared / "x.h5").exists()
    assert all(cmd.startswith("rm -rf -- /dfs3b/lab/sw/Temp/tester/server-data/s1/") for cmd in ran)


def test_a_row_from_before_the_data_root_is_still_deleted(accounts, tmp_path, monkeypatch):
    """Configuring a data root moves the results dir; a dataset recorded under the old one
    (runs/console/<owner>/uploads) is still the owner's to delete."""
    client, conn, gw, auth_routes, db, models, user_id, ran = accounts
    old_root = tmp_path / "old-results"
    legacy = old_root / "tester" / "uploads" / "old.csv"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("x")
    monkeypatch.setenv("AISCIENTIST_LEGACY_RESULTS_DIR", str(old_root))
    row = auth_routes.record_dataset(user_id, "old.csv", str(legacy), 1, "csv")
    got = client.post("/api/datasets/delete", json={"id": row})
    assert got.status_code == 200, got.text
    assert not legacy.exists()


def test_finished_upload_records_go_after_a_week_stopped_uploads_after_a_day(tmp_path):
    """A stopped upload's sidecar and partial go after the upload TTL (24 h); the record of an upload
    whose bytes all arrived goes after a week, as before the TTL: never kept for ever."""
    import json
    import os
    import time
    from aiscientist.gateway import tus_upload
    root = tmp_path / "uploads" / ".parts"
    root.mkdir(parents=True)
    now = time.time()

    def sidecar(i, status, age_h):
        uid = f"{i:032x}"
        path = root / f"{uid}.json"
        path.write_text(json.dumps({"status": status}))
        (root / f"{uid}.part").write_bytes(b"x" if status == "uploading" else b"")
        os.utime(path, (now - age_h * 3600,) * 2)
        return path

    stopped = sidecar(1, "uploading", 30)
    fresh = sidecar(2, "uploading", 2)
    finished_recent = sidecar(3, "staging", 30)
    finished_old = sidecar(4, "complete", 8 * 24)
    tus_upload._cleanup_parts(root)
    assert not stopped.exists() and not stopped.with_suffix(".part").exists()
    assert fresh.exists() and finished_recent.exists()
    assert not finished_old.exists()
