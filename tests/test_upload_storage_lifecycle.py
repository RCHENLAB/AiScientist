"""Exercise managed upload publication and cleanup via the real HTTP routes."""
import os
import time
from pathlib import Path
from uuid import UUID

import pytest

from test_tus_upload import env, _create, _patch  # noqa: F401 - env is a fixture used here


@pytest.fixture()
def managed(env, tmp_path, monkeypatch):  # noqa: F811 - the env fixture imported above
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    return env


def _settled(client, gw, conn, url):
    """The upload's background check, once it has settled."""
    result = client.get(url).json()
    assert result["job_id"], result
    job = gw._staging_queue(conn).wait(result["job_id"], 5)
    assert job is not None and job.state == "done", job and job.error
    return job


def test_single_file_uuid_layout_and_same_name(managed):
    client, conn, gw = managed
    from aiscientist.gateway import dataset_storage, upload_storage
    destinations = []
    for _ in range(2):
        url = _create(client, conn, 3).headers["location"]
        assert not (dataset_storage.data_root() / "users").exists() or destinations
        assert _patch(client, url, 0, b"abc").status_code == 204
        job = _settled(client, gw, conn, url)
        assert job.dest == "server"
        target = Path(job.remote_path)
        assert target.name == "data.csv"
        UUID(target.parents[1].name)
        assert target.parent.name == "original"
        # kept on this host, in the account's own dataset folder
        assert target.parents[2] == dataset_storage.datasets_root("tester")
        assert (target.parents[1] / "processed").is_dir()
        assert target.read_bytes() == b"abc"
        assert client.get(url).json()["path"] == str(target)
        destinations.append(target)
    assert destinations[0] != destinations[1]
    assert not list(upload_storage.uploads(conn).rglob("*.part"))
    assert conn.executor.state.staged_files == []    # nothing was copied to HPC3


def test_folder_manifest_prevents_partial_publication_and_cancel(managed):
    client, conn, gw = managed
    from aiscientist.gateway import dataset_storage, upload_storage
    reserve = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id,
        "name": "study", "files": [["a.csv", 3], ["nested/b.csv", 2]]}).json()
    folder = reserve["folder"]
    UUID(folder)
    url = _create(client, conn, 3, folder=folder, rel_path=f"{folder}/a.csv").headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 204
    _settled(client, gw, conn, url)
    body = {"connection_id": conn.id, "folder": folder, "count": 2}
    early = client.post("/api/upload/register-folder", json=body)
    # registered before its last file arrived: it waits, and is not published half-way
    assert early.status_code == 200 and early.json()["status"] == "staging", early.text
    assert not dataset_storage.dataset_dir(folder, "tester").exists()
    url = _create(client, conn, 2, folder=folder, rel_path=f"{folder}/nested/b.csv").headers["location"]
    assert _patch(client, url, 0, b"xy").status_code == 204
    _settled(client, gw, conn, url)
    response = client.post("/api/upload/register-folder", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "registered" and response.json()["name"] == "study"
    published = dataset_storage.dataset_dir(folder, "tester") / "original"
    assert Path(response.json()["path"]) == published
    assert (published / "nested/b.csv").read_bytes() == b"xy"
    assert not (upload_storage.uploads(conn) / folder).exists()
    # Cancellation of another session never touches the completed dataset.
    other = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "study"}).json()["folder"]
    url = _create(client, conn, 5, folder=other, rel_path=f"{other}/partial.csv").headers["location"]
    assert _patch(client, url, 0, b"ab").status_code == 204
    assert client.delete(url, headers={"Tus-Resumable": "1.0.0"}).status_code == 204
    response = client.post("/api/upload/staging/cancel", json={"connection_id": conn.id, "folder": other,
                                                             "discard_upload": True})
    assert response.status_code == 200
    assert not (upload_storage.uploads(conn) / other).exists()
    assert (published / "a.csv").read_bytes() == b"abc"


def test_interrupted_upload_resumes_until_configured_expiry(managed, monkeypatch):
    client, conn, gw = managed
    from aiscientist.gateway import tus_upload
    monkeypatch.setenv("AISCIENTIST_UPLOAD_TTL_SECONDS", "10")
    url = _create(client, conn, 6).headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 204
    meta, part = tus_upload._paths(conn, url.rsplit("/", 1)[-1])
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).headers["upload-offset"] == "3"
    assert part.read_bytes() == b"abc"
    os.utime(meta, (time.time() - 11,) * 2)
    assert client.head(url, headers={"Tus-Resumable": "1.0.0"}).status_code == 410
    assert not meta.exists() and not part.exists()


def test_a_data_root_keeps_uploads_here_even_with_the_hpc3_flag(managed):
    """Issue 6: with a data root nothing is written to HPC3 at upload time, whatever
    AISCIENTIST_UPLOADS_ON_HPC says."""
    client, conn, gw = managed
    from aiscientist.gateway import dataset_storage
    conn.settings.uploads_on_hpc = True
    assert gw._uploads_on_hpc(conn) is False and gw._uploads_kept_here(conn) is True
    url = _create(client, conn, 3).headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 204
    job = _settled(client, gw, conn, url)
    assert job.dest == "server" and job.hpc_user == ""
    assert Path(job.remote_path).is_relative_to(dataset_storage.datasets_root("tester"))
    assert Path(job.remote_path).read_bytes() == b"abc"


def test_folder_resume_checks_server_completed_files(managed):
    client, conn, gw = managed
    body = {"connection_id": conn.id, "name": "study", "files": [["a.csv", 3], ["b.csv", 2]]}
    folder = client.post("/api/upload/reserve-folder", json=body).json()["folder"]
    url = _create(client, conn, 3, folder=folder, rel_path=f"{folder}/a.csv").headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 204
    resumed = client.post("/api/upload/reserve-folder", json={**body, "folder": folder}).json()
    assert resumed["folder"] == folder and resumed["have"] == [0]


def test_expire_disconnected_sessions_removes_partials(managed, monkeypatch):
    client, conn, gw = managed
    from aiscientist.gateway import tus_upload, upload_storage
    monkeypatch.setenv("AISCIENTIST_UPLOAD_TTL_SECONDS", "5")
    url = _create(client, conn, 6).headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 204
    meta, part = tus_upload._paths(conn, url.rsplit("/", 1)[-1])
    os.utime(meta, (time.time() - 10,) * 2)
    tus_upload._last_cleanup.clear()
    upload_storage.expire_all()
    assert not part.exists() and not meta.exists()


def test_delete_endpoint_sync_failure_and_owner_isolation(managed, tmp_path, monkeypatch):
    client, conn, gw = managed
    pytest.importorskip("sqlalchemy")
    from aiscientist.gateway import auth_routes, db, models, dataset_storage
    from types import SimpleNamespace
    from uuid import uuid4
    db.reset(f"sqlite:///{(tmp_path / 'delete.db').as_posix()}")
    db.init_db()
    with db.session_scope() as session:
        user = models.User(username="tester", password_hash="x")
        session.add(user)
        session.commit()
        user_id = user.id
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw, "_optional_user", lambda request: SimpleNamespace(id=user_id, username="tester"))
    monkeypatch.setattr(gw, "_active_conn_for_user", lambda user: conn)
    targets = []
    for _ in range(2):
        target = dataset_storage.dataset_dir(str(uuid4()), "tester") / "original"
        target.mkdir(parents=True)
        (target / "x").write_bytes(b"x")
        targets.append(target)
    row = auth_routes.record_folder_dataset(user_id, "study", str(targets[0]), 1)
    real_delete = dataset_storage.remove_managed_dataset
    monkeypatch.setattr(dataset_storage, "remove_managed_dataset", lambda path: (_ for _ in ()).throw(PermissionError("busy")))
    assert client.post("/api/datasets/delete", json={"id": row}).status_code == 409
    with db.session_scope() as session:
        assert session.get(models.Dataset, row) is not None
    monkeypatch.setattr(dataset_storage, "remove_managed_dataset", real_delete)
    assert client.post("/api/datasets/delete", json={"id": row}).status_code == 200
    assert not targets[0].parent.exists() and targets[1].exists()
    db.reset()


def test_expired_incomplete_hpc_folder_cleans_completed_temp_files(managed, monkeypatch):
    import json
    from dataclasses import asdict
    from aiscientist.gateway import upload_storage
    client, conn, gw = managed
    conn.settings.uploads_on_hpc = True
    monkeypatch.setenv("AISCIENTIST_UPLOAD_TTL_SECONDS", "5")
    body = {"connection_id": conn.id, "name": "study", "files": [["a.csv", 3], ["b.csv", 2]]}
    folder = client.post("/api/upload/reserve-folder", json=body).json()["folder"]
    url = _create(client, conn, 3, folder=folder, rel_path=f"{folder}/a.csv").headers["location"]
    assert _patch(client, url, 0, b"abc").status_code == 204
    job_id = client.get(url).json()["job_id"]
    queue = gw._staging_queue(conn)
    job = queue.wait(job_id, 5)
    assert job.state == "done"
    job.updated_at = time.time() - 10
    queue.store._path(job.id).write_text(json.dumps(asdict(job)), encoding="utf-8")
    marker = upload_storage.session_path(conn, folder)
    os.utime(marker, (time.time() - 10,) * 2)
    upload_storage.expire_sessions(conn)
    assert not marker.exists()
    assert queue.jobs(folder) == []
