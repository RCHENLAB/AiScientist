"""Run inputs are recorded before review/execution and survive storage cleanup."""
import asyncio
import datetime as dt
import importlib
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")


@pytest.fixture
def history(tmp_path, monkeypatch):
    from aiscientist.gateway import db, models, auth_routes
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{tmp_path / 'history.db'}")
    importlib.reload(db)
    importlib.reload(models)
    importlib.reload(auth_routes)
    from aiscientist.gateway import metadata_store
    importlib.reload(metadata_store)
    db.init_db()
    monkeypatch.setattr(auth_routes, "_draft_metadata", lambda *a, **kw: None)
    with db.session_scope() as s:
        user = models.User(username="tester", password_hash="unused")
        s.add(user)
        s.commit()
        uid = user.id
    yield SimpleNamespace(db=db, models=models, routes=auth_routes, uid=uid)
    db.reset()


@pytest.fixture
def lab_history(lab_gateway, history, monkeypatch, tmp_path):
    from aiscientist.gateway import app as gw
    conn = lab_gateway.connection()
    conn.app_user_id = history.uid
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw, "_owned_conn", lambda *a, **kw: conn)
    monkeypatch.setattr(gw, "_dataset_description_input", lambda *a: None)
    monkeypatch.setattr(gw, "_triage_bound_datasets", lambda *a: [])
    # Keep the real bind ordering/identity resolution, without staging a scientific matrix.
    async def admit(conn, bound, *a):
        return bound, {}
    monkeypatch.setattr(gw, "_admit_bound_paths", admit)
    paths = [str(tmp_path / "counts.h5ad"), str(tmp_path / "panel.csv")]
    for path in paths:
        history.routes.record_dataset(history.uid, path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1], path, 0, "other")
    req = gw.LabRequest(connection_id=conn.id, question="QC cells", conversation_id="inputs",
                        plan_mode=True, mode="single", planner="linear", datasets=[
                            {"path": paths[1], "name": "panel.csv", "role": "reference"},
                            {"path": paths[0], "name": "counts.h5ad", "role": "primary"}])
    return gw, conn, req, history, lab_gateway


def _inputs(h):
    with h.db.session_scope() as s:
        return [(r.run_id, r.position, r.dataset_id, r.dataset_uuid, r.name, r.role,
                 r.shared_version_id) for r in s.query(h.models.RunDataset).order_by(
                     h.models.RunDataset.run_id, h.models.RunDataset.position)]


@pytest.mark.parametrize("outcome", ["plan_cancelled", "first_step_failed"])
def test_lab_records_both_inputs_before_cancel_or_failure(lab_history, monkeypatch, outcome):
    from aiscientist.agents import research_lab
    gw, conn, req, h, env = lab_history
    if outcome == "first_step_failed":
        original = research_lab.ResearchLab
        def failing(*a, **kw):
            lab = original(*a, **kw)
            def fail(*a, **kw):
                raise RuntimeError("first analysis step failed")
            lab.run = fail
            return lab
        monkeypatch.setattr(research_lab, "ResearchLab", failing)
    asyncio.run(gw._run_lab(conn, req))
    before = _inputs(h)
    assert [(r[1], r[4], r[5]) for r in before] == [
        (0, "counts.h5ad", "primary"), (1, "panel.csv", "reference")]
    assert all(r[2] and r[3] for r in before)
    if outcome == "plan_cancelled":
        from starlette.requests import Request
        rid = env.cards[-1]["run_id"]
        response = asyncio.run(gw.lab_plan_review(gw.PlanReviewRequest(
            connection_id=conn.id, run_id=rid, action="cancel"),
            Request({"type": "http", "headers": []})))
        assert response.status_code == 200
        assert _inputs(h) == before
    with h.db.session_scope() as s:
        assert s.query(h.models.Run).one().dataset_id is None


def test_approved_saved_plan_and_resume_do_not_insert_inputs(lab_history, monkeypatch):
    from aiscientist.agents.research_lab import ResumeState
    from starlette.requests import Request
    gw, conn, req, h, env = lab_history
    asyncio.run(gw._run_lab(conn, req))
    rid = env.cards[-1]["run_id"]
    before = _inputs(h)
    def unexpected(*a, **kw):
        pytest.fail("a resume or saved plan must not record inputs again")
    monkeypatch.setattr(h.routes, "record_run_datasets", unexpected)
    async def approve():
        response = await gw.lab_plan_review(gw.PlanReviewRequest(
            connection_id=conn.id, run_id=rid, action="approve"),
            Request({"type": "http", "headers": []}))
        assert response.status_code == 200
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks)
    asyncio.run(approve())
    assert env.calls
    assert _inputs(h) == before
    resume = ResumeState.from_run_state({"question": req.question, "agenda": ["QC"], "rounds": []}, 0)
    asyncio.run(gw._run_lab(conn, req, resume=resume, resume_run_id=rid))
    assert _inputs(h) == before
    with h.db.session_scope() as s:
        assert s.query(h.models.Run).count() == 1


def test_shared_version_server_folder_public_and_lookup(history):
    h = history
    with h.db.session_scope() as s:
        shared = h.models.SharedDataset(slug="atlas", name="Atlas")
        s.add(shared)
        s.flush()
        version = h.models.SharedDatasetVersion(dataset_id=shared.id, label="v1")
        s.add(version)
        s.commit()
        vid, vuuid = version.id, version.uuid
    h.routes.record_run_start(h.uid, "shared", "q", True, dataset_id=999)
    h.routes.record_run_datasets(h.uid, "shared", [
        {"path": "/secret/server/folder", "name": "/secret/server/folder/", "role": None},
        {"path": "/secret/shared/matrix", "name": "matrix", "role": "reference"}], [
        {"title": "Atlas", "version": "v1", "version_uuid": vuuid,
         "entries": [{"path": "/secret/shared/matrix"}]}])
    with h.db.session_scope() as s:
        run = s.query(h.models.Run).one()
        assert run.dataset_id is None
        public = run.public()
        assert public["datasets"] == [
            {"name": "folder", "role": "primary", "uuid": None},
            {"name": "Atlas v1", "role": "reference", "uuid": vuuid}]
        assert "secret" not in str(public) and "path" not in str(public)
    rows = _inputs(h)
    assert rows[0][2] is None and rows[1][6] == vid
    assert h.routes.runs_for_dataset(h.uid, shared_version_id=vid) == [public]
    assert h.routes.runs_for_dataset(h.uid + 1, shared_version_id=vid) == []
    assert h.routes.runs_for_dataset(h.uid, vuuid) == [public]


@pytest.mark.parametrize("kind", ["literature", "corpus"])
def test_non_analysis_runs_never_record_datasets(lab_history, monkeypatch, kind):
    gw, conn, req, h, _env = lab_history
    async def fail(*a, **kw):
        raise RuntimeError("stop after recording history")
    if kind == "literature":
        monkeypatch.setattr(gw, "_literature_corpus_available", lambda *a: False)
        monkeypatch.setattr(gw, "_literature_plan_review", lambda *a: {"action": "approve"})
        monkeypatch.setattr(gw, "_build_literature_mcp_route", fail)
        asyncio.run(gw._run_literature(conn, req))
    else:
        def identity(*a):
            return None
        monkeypatch.setattr(gw, "_identity_for", identity)
        monkeypatch.setattr(gw, "_corpus_scan_via_mcp", fail)
        asyncio.run(gw._run_corpus_scan(conn, req))
    with h.db.session_scope() as s:
        assert s.query(h.models.Run).count() == 1
        assert s.query(h.models.RunDataset).count() == 0


@pytest.mark.parametrize("cleanup", ["ui", "retention"])
def test_dataset_cleanup_keeps_identity_and_run_delete_cascades(history, tmp_path, monkeypatch, cleanup):
    from aiscientist.gateway import app as gw, dataset_storage, storage_policy
    h = history
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AISCIENTIST_DATASET_RETENTION_DAYS", "90")
    monkeypatch.setenv("AISCIENTIST_DATASET_NOTICE_DAYS", "7")
    monkeypatch.setattr(gw, "_active_conn_for_user", lambda *a: None)
    monkeypatch.setattr(gw, "_dataset_in_use", lambda *a: False)
    import uuid
    folder = dataset_storage.dataset_dir(str(uuid.uuid4()), "tester") / "original"
    folder.mkdir(parents=True)
    file = folder / "counts.csv"
    file.write_text("a,b")
    did = h.routes.record_dataset(h.uid, "counts.csv", str(file), 3, "csv")
    h.routes.record_run_start(h.uid, "cleanup", "q", False)
    h.routes.record_run_datasets(h.uid, "cleanup", [{"path": str(file)}])
    identity = _inputs(h)[0][3]
    with h.db.session_scope() as s:
        user = s.get(h.models.User, h.uid)
        s.get(h.models.Dataset, did).uploaded_at = dt.datetime(2020, 1, 1)
        s.commit()
    if cleanup == "ui":
        response = asyncio.run(gw._delete_owned_dataset(user, did))
        assert response.status_code == 200, response.body
    else:
        clock = [dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)]
        monkeypatch.setattr(storage_policy, "_now", lambda: clock[0])
        asyncio.run(storage_policy.sweep(gw))
        clock[0] += dt.timedelta(days=8)
        asyncio.run(storage_policy.sweep(gw))
    assert not file.exists()
    with h.db.session_scope() as s:
        assert s.get(h.models.Dataset, did) is None
        row = s.query(h.models.RunDataset).one()
        assert row.dataset_id is None
    assert h.routes.runs_for_dataset(h.uid, identity)[0]["datasets"] == [
        {"name": "counts.csv", "role": "primary", "uuid": identity}]
    assert h.routes.delete_run_record(h.uid, "cleanup")
    assert _inputs(h) == []


def test_init_without_alembic_upgrades_existing_database(history, monkeypatch):
    import builtins
    from sqlalchemy import inspect
    h = history
    with h.db.get_engine().begin() as conn:
        h.models.RunDataset.__table__.drop(conn)
    real_import = builtins.__import__
    def no_alembic(name, *a, **kw):
        if name == "alembic" or name.startswith("alembic."):
            raise ImportError(name)
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", no_alembic)
    h.db.init_db()
    assert "run_datasets" in inspect(h.db.get_engine()).get_table_names()
    h.routes.record_run_start(h.uid, "legacy", "q", True)
    h.routes.record_run_datasets(h.uid, "legacy", [{"path": "/server/counts", "name": "counts"}])
    assert _inputs(h)[0][4] == "counts"


def test_lab_records_resolved_shared_version_with_its_role(lab_history, monkeypatch):
    gw, conn, req, h, _env = lab_history
    with h.db.session_scope() as s:
        shared = h.models.SharedDataset(slug="atlas", name="Atlas")
        s.add(shared)
        s.flush()
        version = h.models.SharedDatasetVersion(dataset_id=shared.id, label="v1")
        s.add(version)
        s.commit()
        vid, identity = version.id, version.uuid
    resolved = [{"title": "Atlas", "version": "v1", "version_uuid": identity,
                 "role": "reference", "entries": [{"path": "/shared/atlas/matrix.h5ad", "name": "matrix.h5ad"}]}]
    req._shared_resolved = resolved
    async def choose(*a, **kw):
        return resolved
    async def record(*a, **kw):
        pass
    monkeypatch.setattr(gw, "_choose_shared_members", choose)
    monkeypatch.setattr(gw, "_record_shared_inputs", record)
    asyncio.run(gw._run_lab(conn, req))
    rows = _inputs(h)
    assert len(rows) == 3
    assert [r[2:] for r in rows if r[6] == vid] == [
        (None, identity, "Atlas v1", "reference", vid)]
    assert len(h.routes.runs_for_dataset(h.uid, shared_version_id=vid)) == 1
