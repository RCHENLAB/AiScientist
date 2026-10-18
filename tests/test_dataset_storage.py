"""Storage lifecycle checks: all filesystem mutations stay inside tmp_path."""
import importlib
import uuid

import pytest


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    pytest.importorskip("sqlalchemy")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AISCIENTIST_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("AISCIENTIST_LEGACY_RESULTS_DIR", str(tmp_path / "old"))
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{tmp_path / 'storage.db'}")
    from aiscientist.gateway import db, models, auth_routes, dataset_storage
    importlib.reload(db)
    importlib.reload(models)
    importlib.reload(auth_routes)
    db.init_db()
    with db.session_scope() as session:
        user = models.User(username="owner", password_hash="x")
        session.add(user)
        session.commit()
        uid = user.id
    # the publication grace period has its own test; the others reconcile at once
    monkeypatch.setattr(dataset_storage, "_PUBLISH_GRACE_S", 0)
    yield dataset_storage, auth_routes, db, models, uid
    db.reset()


def test_publish_uuid_layout_and_retry(storage):
    store, *_ = storage
    identity = str(uuid.uuid4())
    stage = store.temp_dir(identity)
    stage.mkdir(parents=True)
    (stage / "sample.txt").write_text("complete")
    target = store.publish_dataset(stage, identity)
    assert target == store.dataset_dir(identity) / "original"
    assert (target / "sample.txt").read_text() == "complete"
    assert (target.parent / "processed").is_dir()
    assert store.publish_dataset(stage, identity) == target
    with pytest.raises(ValueError):
        store.dataset_dir("../escape")
    with pytest.raises(ValueError):
        store.publish_dataset(target, str(uuid.uuid4()))


def test_delete_failure_preserves_row_and_overlap_guard(storage, tmp_path):
    store, routes, db, models, uid = storage
    folder = tmp_path / "legacy"
    folder.mkdir()
    dataset = routes.record_folder_dataset(uid, "name", str(folder), 0)
    def fail(path):
        raise PermissionError("busy")
    with pytest.raises(PermissionError):
        routes.delete_dataset_record(uid, dataset, delete_files=fail)
    with db.session_scope() as session:
        assert session.get(models.Dataset, dataset) is not None
    child = routes.record_dataset(uid, "nested", str(folder / "child"), 0, "other")
    with pytest.raises(ValueError, match="overlaps"):
        routes.delete_dataset_record(uid, dataset, delete_files=fail)
    assert routes.delete_dataset_record(uid, child)


def test_delete_only_uuid_tree(storage):
    store, routes, db, models, uid = storage
    identity, sibling = str(uuid.uuid4()), str(uuid.uuid4())
    for value in (identity, sibling):
        folder = store.dataset_dir(value) / "original"
        folder.mkdir(parents=True)
        (folder / "same.txt").write_text("contents")
    dataset = routes.record_folder_dataset(uid, "same", str(store.dataset_dir(identity) / "original"), 8)
    routes.delete_dataset_record(uid, dataset, delete_files=store.remove_managed_dataset)
    assert not store.dataset_dir(identity).exists()
    assert store.dataset_dir(sibling).exists()
    with db.session_scope() as session:
        assert session.get(models.Dataset, dataset) is None


def test_reconcile_dry_run_owner_scope_and_cleanup(storage):
    store, routes, db, models, uid = storage
    orphan = store.dataset_dir(str(uuid.uuid4()))
    orphan.mkdir(parents=True)
    missing = routes.record_dataset(uid, "missing", str(orphan.parent / str(uuid.uuid4())), 0, "other")
    assert store.reconcile(user_id=uid)["orphans"] == []
    report = store.reconcile()
    assert report["orphans"] == [str(orphan)]
    assert report["missing"][0]["id"] == missing
    assert orphan.exists()
    store.reconcile(cleanup=True)
    assert not orphan.exists()
    with db.session_scope() as session:
        assert session.get(models.Dataset, missing) is None


def test_legacy_migration_is_copy_and_preserves_uuid(storage, tmp_path):
    store, routes, db, models, uid = storage
    source = tmp_path / "legacy.txt"
    source.write_text("data")
    dataset = routes.record_dataset(uid, "original.txt", str(source), 4, "other")
    preview = store.migrate_dataset(uid, dataset)
    assert not store.data_root().exists()
    result = store.migrate_dataset(uid, dataset, apply=True)
    assert preview["destination"] == result["destination"]
    assert source.read_text() == "data"
    with db.session_scope() as session:
        row = session.get(models.Dataset, dataset)
        assert row.uuid == store.managed_uuid(row.path)
        assert row.public()["original_name"] == "original.txt"
        assert row.public()["status"] == "complete"


def test_managed_delete_rejects_outside(storage, tmp_path):
    store, *_ = storage
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError):
        store.remove_managed_dataset(outside)
    assert outside.exists()

def test_additive_legacy_schema_backfill(storage):
    store, routes, db, models, uid = storage
    from sqlalchemy import text
    engine = db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE datasets"))
        conn.execute(text("CREATE TABLE datasets (id INTEGER PRIMARY KEY, user_id INTEGER, "
                          "name VARCHAR(255), path VARCHAR(1024), size_bytes BIGINT, "
                          "kind VARCHAR(32), sha256 VARCHAR(64), uploaded_at TIMESTAMP)"))
        conn.execute(text("INSERT INTO datasets (id, user_id, name, path, size_bytes, kind) "
                          "VALUES (1, :uid, 'legacy', '/legacy/path', 12, 'folder')"), {"uid": uid})
    db.init_db()
    with db.session_scope() as session:
        row = session.get(models.Dataset, 1)
        first_uuid = row.uuid
        assert str(uuid.UUID(first_uuid)) == first_uuid
        assert row.path == "/legacy/path"
        assert row.status == "complete"
    db.init_db()
    with db.session_scope() as session:
        assert session.get(models.Dataset, 1).uuid == first_uuid


def test_reconcile_preserves_remote_and_bound_rows(storage, tmp_path):
    store, routes, db, models, uid = storage
    remote = routes.record_dataset(uid, "remote", "/dfs3b/lab/remote/file.h5ad", 1, "h5ad")
    bound = routes.record_dataset(uid, "bound", str(tmp_path / "external"), 1, "folder")
    assert store.reconcile(cleanup=True)["missing"] == []
    with db.session_scope() as session:
        assert session.get(models.Dataset, remote)
        assert session.get(models.Dataset, bound)


def test_reconcile_legacy_orphans_and_registered_overlap(storage, tmp_path):
    store, routes, db, models, uid = storage
    root = tmp_path / "results" / "owner" / "uploads"
    orphan = root / "old dataset (1)"
    orphan.mkdir(parents=True)
    (orphan / "data").write_text("old")
    kept = root / "bound"
    kept.mkdir()
    routes.record_dataset(uid, "bound", str(kept / "data"), 0, "other")
    report = store.reconcile()
    assert str(orphan) in report["orphans"]
    assert str(kept) not in report["orphans"]
    assert orphan.exists()
    store.reconcile(cleanup=True)
    assert not orphan.exists()
    assert kept.exists()


def test_migration_rejects_symlink(storage, tmp_path):
    store, routes, db, models, uid = storage
    source = tmp_path / "legacy"
    source.mkdir()
    outside = tmp_path / "external.txt"
    outside.write_text("private")
    try:
        (source / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks are unavailable for this Windows account")
    dataset = routes.record_folder_dataset(uid, "legacy", str(source), 7)
    with pytest.raises(ValueError, match="symlink"):
        store.migrate_dataset(uid, dataset, apply=True)
    assert outside.read_text() == "private"


def test_reconcile_leaves_a_tree_published_a_moment_ago(storage, monkeypatch):
    """A tree moved into datasets/ gets its registry row right after: a cleanup in between must not
    remove an upload that is being recorded (the review of RCHENLAB/AiScientist PR 12)."""
    import os
    import time
    store, *_ = storage
    monkeypatch.setattr(store, "_PUBLISH_GRACE_S", 3600)
    fresh = store.dataset_dir(str(uuid.uuid4()))
    fresh.mkdir(parents=True)
    old = store.dataset_dir(str(uuid.uuid4()))
    old.mkdir(parents=True)
    hours_ago = time.time() - 2 * 3600
    os.utime(old, (hours_ago, hours_ago))
    report = store.reconcile(cleanup=True)
    assert str(old) in report["orphans"] and not old.exists()
    assert str(fresh) not in report["orphans"] and fresh.is_dir()


def test_reconcile_leaves_what_the_upload_queue_still_holds(storage, tmp_path):
    """A legacy folder being uploaded (registered only at its end) and a file whose HPC3 copy failed
    (kept for "Retry copy") have no registry row yet; an admin cleanup must not remove them."""
    import json
    store, *_ = storage
    root = tmp_path / "results" / "owner" / "uploads"
    (root / ".staging").mkdir(parents=True)
    (root / "study").mkdir()
    (root / "big.bam").write_text("bytes")
    (root / "stray").mkdir()
    (root / ".staging" / "folder-study.json").write_text(json.dumps({"folder": "study"}))
    (root / ".staging" / "j1.json").write_text(json.dumps({"folder": "", "local_path": str(root / "big.bam")}))
    report = store.reconcile(cleanup=True)
    assert report["orphans"] == [str(root / "stray")]
    assert (root / "study").is_dir() and (root / "big.bam").exists() and not (root / "stray").exists()
