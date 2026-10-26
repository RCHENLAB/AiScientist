"""My runs: deleting a run, and Files → Outputs: what a run wrote, previewed like a folder dataset.

The DB helper first (only the owner's row goes, with its result metadata; a dataset kept inside the
run's folder blocks it; the model calls stay), then the routes: a run's folder is removed only under
the caller's own results folder, never while the run executes, and its outputs are read with the
dataset viewer's bounds and its inline rule.
"""

from __future__ import annotations

import importlib
import io
import threading
import types
import zipfile

import pytest


@pytest.fixture()
def db_ctx(tmp_path, monkeypatch):
    pytest.importorskip("sqlalchemy")
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'd.db').as_posix()}")
    from aiscientist.gateway import auth_routes, db, models  # noqa: F401

    importlib.reload(db)
    importlib.reload(models)
    importlib.reload(auth_routes)
    db.reset(f"sqlite:///{(tmp_path / 'd.db').as_posix()}")
    db.init_db()
    return auth_routes, db, models


def _mk_user(db, models, username):
    with db.session_scope() as s:
        u = models.User(username=username, password_hash="x", role="user", is_active=True)
        s.add(u)
        s.commit()
        return u.id


def _result_metadata(db, models, user_id, run_id, input_uuid):
    from aiscientist.gateway import metadata_store
    uid = metadata_store.result_uuid(run_id)
    with db.session_scope() as s:
        s.add(models.MetadataRecord(uuid=uid, kind="result", user_id=user_id, run_id=run_id, name="r"))
        s.add(models.MetadataLink(user_id=user_id, parent_uuid=input_uuid, child_uuid=uid, relation="input"))
        s.add(models.MetadataLink(user_id=user_id, parent_uuid=uid, child_uuid="saved-ds", relation="saved_from"))
        s.commit()
    return uid


def test_only_the_owner_deletes_a_run_and_its_metadata_goes_with_it(db_ctx, tmp_path):
    auth_routes, db, models = db_ctx
    alice = _mk_user(db, models, "alice")
    bob = _mk_user(db, models, "bob")
    auth_routes.record_run_start(alice, "abc123", "Cluster the retina", plan_mode=True)
    uid = _result_metadata(db, models, alice, "abc123", "input-ds")
    with db.session_scope() as s:
        s.add(models.LlmCall(run_id="abc123", user_id=alice, role="pi", model="m"))
        s.commit()

    removed: list = []
    folder = tmp_path / "alice" / "abc123"
    assert auth_routes.delete_run_record(bob, "abc123", folder, removed.append) is None   # not his
    assert auth_routes.delete_run_record(alice, "nope", folder, removed.append) is None   # missing
    assert removed == []
    assert auth_routes.get_run_record(bob, "abc123") is None
    assert auth_routes.get_run_record(alice, "abc123")["question"] == "Cluster the retina"

    gone = auth_routes.delete_run_record(alice, "abc123", folder, removed.append)
    assert gone["run_id"] == "abc123" and removed == [folder.resolve()]
    assert auth_routes.get_run_record(alice, "abc123") is None
    with db.session_scope() as s:
        assert s.query(models.MetadataRecord).filter_by(uuid=uid).count() == 0
        links = [(e.parent_uuid, e.child_uuid) for e in s.query(models.MetadataLink).all()]
        # the edge INTO the result goes; a dataset saved from it keeps its own (its lineage says deleted)
        assert links == [(uid, "saved-ds")]
        assert s.query(models.LlmCall).filter_by(run_id="abc123").count() == 1   # usage stays counted


def test_a_failed_removal_keeps_the_row_for_a_retry(db_ctx, tmp_path):
    auth_routes, db, models = db_ctx
    alice = _mk_user(db, models, "alice")
    auth_routes.record_run_start(alice, "abc123", "q", plan_mode=False)

    def fail(_target):
        raise OSError("permission denied")

    with pytest.raises(OSError):
        auth_routes.delete_run_record(alice, "abc123", tmp_path / "alice" / "abc123", fail)
    assert auth_routes.get_run_record(alice, "abc123") is not None


def test_a_dataset_kept_inside_the_run_folder_blocks_the_delete(db_ctx, tmp_path):
    auth_routes, db, models = db_ctx
    alice = _mk_user(db, models, "alice")
    auth_routes.record_run_start(alice, "abc123", "q", plan_mode=False)
    folder = tmp_path / "alice" / "abc123"
    auth_routes.record_dataset(alice, "x.h5ad", str(folder / "result" / "x.h5ad"), 1, "h5ad")
    with pytest.raises(ValueError, match="x.h5ad"):
        auth_routes.delete_run_record(alice, "abc123", folder, lambda t: None)
    assert auth_routes.get_run_record(alice, "abc123") is not None


# --- the routes ---------------------------------------------------------------------------------------


@pytest.fixture
def gw(tmp_path, monkeypatch):
    gw_app = pytest.importorskip("aiscientist.gateway.app")
    pytest.importorskip("httpx")
    runs = {"run1": {"id": 1, "user": 5, "question": "Cluster the retina"},
            "run2": {"id": 2, "user": 6, "question": "Someone else's"}}
    deleted: list = []

    def get_run(uid, run_id):
        r = runs.get(run_id)
        return dict(r, run_id=run_id) if r and r["user"] == uid else None

    def delete_run(uid, run_id, run_dir=None, delete_files=None):
        if get_run(uid, run_id) is None:
            return None
        if delete_files is not None:
            delete_files(run_dir.resolve())
        deleted.append(run_id)
        return get_run(uid, run_id)

    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_optional_user",
                        lambda request: types.SimpleNamespace(username="yijus12", id=5, role="user"))
    monkeypatch.setattr(gw_app.auth_routes, "get_run_record", get_run)
    monkeypatch.setattr(gw_app.auth_routes, "delete_run_record", delete_run)
    monkeypatch.setattr(gw_app, "CONNECTIONS", {})
    gw_app._test_deleted = deleted
    return gw_app


def _client(gw_app):
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app)


def _run_folder(tmp_path, owner="yijus12", run_id="run1"):
    art = tmp_path / owner / run_id / "artifacts"
    (art / "figures").mkdir(parents=True)
    (art / "report").mkdir()
    (art / "tables").mkdir()
    (art / "figures" / "umap.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (art / "figures" / "graph.svg").write_bytes(b"<svg onload='alert(1)'/>")
    (art / "report" / "report.pdf").write_bytes(b"%PDF-1.4\n")
    (art / "tables" / "markers.csv").write_bytes(b"gene,logfc\nRHO,3.2\n")
    return art


def test_a_runs_outputs_list_preview_and_download(tmp_path, gw):
    _run_folder(tmp_path)
    c = _client(gw)
    folder = c.get("/api/runs/run1/preview").json()["folder"]
    assert [e["path"] for e in folder["entries"]] == [
        "figures/graph.svg", "figures/umap.png", "report/report.pdf", "tables/markers.csv"]
    table = c.get("/api/runs/run1/preview", params={"file": "tables/markers.csv"}).json()
    assert table["preview"]["rows"] == [["RHO", "3.2"]]
    assert c.get("/api/runs/run1/preview", params={"file": "../../x"}).status_code == 400
    png = c.get("/api/runs/run1/download", params={"file": "figures/umap.png", "inline": 1})
    assert png.headers["content-type"] == "image/png" and png.headers["content-disposition"].startswith("inline")
    svg = c.get("/api/runs/run1/download", params={"file": "figures/graph.svg", "inline": 1})
    assert svg.headers["content-type"] == "application/octet-stream"          # never run in the page
    z = c.get("/api/runs/run1/download")
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert names[0].startswith("aiscientist_results_run1/") and len(names) == 4


def test_another_accounts_run_and_a_missing_folder(tmp_path, gw):
    _run_folder(tmp_path, owner="someone", run_id="run2")
    c = _client(gw)
    assert c.get("/api/runs/run2/preview").status_code == 404                # not the caller's run
    assert c.get("/api/runs/uploads/preview").status_code == 404             # not a run at all
    assert c.get("/api/runs/run1/preview").status_code == 410                # the caller's, but gone


def test_delete_removes_the_run_folder_and_nothing_beside_it(tmp_path, gw):
    _run_folder(tmp_path)
    uploads = tmp_path / "yijus12" / "uploads"
    uploads.mkdir()
    (uploads / "keep.h5ad").write_bytes(b"x")
    c = _client(gw)
    assert c.post("/api/runs/delete", json={"run_id": "run2"}).status_code == 404
    assert c.post("/api/runs/delete", json={"run_id": "uploads"}).status_code == 400
    assert c.post("/api/runs/delete", json={"run_id": "../yijus12"}).status_code == 400
    r = c.post("/api/runs/delete", json={"run_id": "run1"})
    assert r.status_code == 200 and r.json()["deleted"] == "run1"
    assert not (tmp_path / "yijus12" / "run1").exists()
    assert (uploads / "keep.h5ad").exists()
    assert gw._test_deleted == ["run1"]


def test_a_run_that_is_executing_or_continued_is_not_deleted(tmp_path, gw, monkeypatch):
    _run_folder(tmp_path)
    live = types.SimpleNamespace(run_id="run9", continues="run1")
    monkeypatch.setattr(gw, "CONNECTIONS", {"c": types.SimpleNamespace(running_runs=lambda: [live])})
    c = _client(gw)
    r = c.post("/api/runs/delete", json={"run_id": "run1"})
    assert r.status_code == 409 and "still running" in r.json()["error"]
    assert (tmp_path / "yijus12" / "run1").exists() and gw._test_deleted == []


def test_a_run_whose_result_is_being_saved_is_not_deleted(tmp_path, gw):
    _run_folder(tmp_path)
    release = threading.Event()
    t = threading.Thread(target=release.wait, name="save-result-run1", daemon=True)
    t.start()
    try:
        r = _client(gw).post("/api/runs/delete", json={"run_id": "run1"})
        assert r.status_code == 409 and "copied to your data" in r.json()["error"]
    finally:
        release.set()
        t.join()
    assert (tmp_path / "yijus12" / "run1").exists()
