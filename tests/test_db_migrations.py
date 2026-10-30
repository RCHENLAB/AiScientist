"""The database schema is versioned by Alembic (gateway/migrations/), and every deploy migrates.

- The migrations build the same schema as the models, so a model change without a revision fails here.
- An empty database, a database created before migrations existed, and one already at the newest
  revision each end at the newest revision; running again changes nothing.
- ``aiscientist-admin migrate`` and ``active-runs``, which deploy/deploy_release.sh calls.
"""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")

from sqlalchemy import create_engine, inspect, text  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", url)
    from aiscientist.gateway import db, models  # noqa: F401
    importlib.reload(db)
    importlib.reload(models)
    db.reset(url)
    yield db, url
    db.reset()


def _columns(url: str, table: str) -> set[str]:
    return {c["name"] for c in inspect(create_engine(url)).get_columns(table)}


def test_migrations_build_the_schema_the_models_describe(fresh_db):
    """Upgrading an empty database through every revision gives exactly the models' tables,
    columns, types and indexes. When this fails, a model changed without a migration: add one in
    src/aiscientist/gateway/migrations/versions/ (see deploy/README.md)."""
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    db, _ = fresh_db
    cfg = db._alembic_config()
    with db.get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}),
                                db.Base.metadata)
    assert diff == [], f"models and migrations disagree: {diff}"


def test_an_empty_database_is_created_at_the_newest_revision(fresh_db):
    db, url = fresh_db
    db.init_db()
    assert db.current_revision() == db.head_revision()
    assert {"users", "runs", "datasets", "llm_calls", "alembic_version"} <= set(
        inspect(create_engine(url)).get_table_names())
    assert db.migrate() == (db.head_revision(), db.head_revision())     # nothing left to do


def test_a_database_from_before_migrations_is_brought_to_the_newest_revision(fresh_db):
    """The production database was made by create_all plus additive columns, and has no
    alembic_version: the same steps bring it to the baseline, then it is stamped and upgraded, and
    its rows are kept."""
    db, url = fresh_db
    eng = create_engine(url)
    with eng.begin() as c:     # the shape of an early deployment: no conversation_id, no dataset uuid
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64) NOT NULL, "
                       "email VARCHAR(255), password_hash VARCHAR(255) NOT NULL, role VARCHAR(16) NOT NULL, "
                       "is_active BOOLEAN NOT NULL, created_at TIMESTAMP, created_by INTEGER, "
                       "last_login_at TIMESTAMP)"))
        c.execute(text("CREATE TABLE datasets (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                       "name VARCHAR(255) NOT NULL, path VARCHAR(1024) NOT NULL, size_bytes BIGINT NOT NULL, "
                       "kind VARCHAR(32) NOT NULL, sha256 VARCHAR(64), uploaded_at TIMESTAMP)"))
        c.execute(text("CREATE TABLE runs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                       "run_id VARCHAR(32) NOT NULL, question TEXT NOT NULL, dataset_id INTEGER, "
                       "status VARCHAR(16) NOT NULL, plan_mode BOOLEAN NOT NULL, artifacts_path VARCHAR(1024), "
                       "summary TEXT, created_at TIMESTAMP, finished_at TIMESTAMP)"))
        c.execute(text("INSERT INTO users (id, username, password_hash, role, is_active) "
                       "VALUES (1, 'jin', 'x', 'admin', 1)"))
        c.execute(text("INSERT INTO datasets (id, user_id, name, path, size_bytes, kind, uploaded_at) "
                       "VALUES (7, 1, 'a.h5ad', '/data/a.h5ad', 6, 'h5ad', '2026-10-01 00:00:00')"))
        c.execute(text("INSERT INTO runs (user_id, run_id, question, status, plan_mode) "
                       "VALUES (1, 'abc', 'q', 'done', 0)"))

    before, after = db.migrate()
    assert (before, after) == (None, db.head_revision())
    assert {"conversation_id"} <= _columns(url, "runs")
    assert {"uuid", "status", "updated_at", "conversation_id"} <= _columns(url, "datasets")
    assert "llm_calls" in inspect(eng).get_table_names()               # a table added later
    with eng.connect() as c:
        uuid, status, updated = c.execute(text("SELECT uuid, status, updated_at FROM datasets WHERE id=7")).one()
        assert len(uuid) == 36 and status == "complete" and updated == "2026-10-01 00:00:00"
        assert c.execute(text("SELECT run_id FROM runs")).scalar() == "abc"
    assert db.migrate() == (db.head_revision(), db.head_revision())


def test_init_db_without_alembic_creates_the_tables_as_before(fresh_db, monkeypatch):
    """A host without the auth extra has no Alembic: the gateway still starts on create_all."""
    import builtins

    db, url = fresh_db
    real_import = builtins.__import__

    def no_alembic(name, *a, **kw):
        if name == "alembic" or name.startswith("alembic."):
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_alembic)
    db.init_db()
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"users", "runs"} <= tables and "alembic_version" not in tables


def test_admin_cli_migrate_and_active_runs(fresh_db, capsys, monkeypatch):
    pytest.importorskip("bcrypt")
    pytest.importorskip("itsdangerous")
    db, url = fresh_db
    from aiscientist.gateway import admin_cli, auth_routes, models
    importlib.reload(auth_routes)
    # The CLI loads the checkout's .env; this test's database URL must win over anything there.
    monkeypatch.setattr("aiscientist.core.config.load_project_env", lambda *a, **k: None)

    assert admin_cli.main(["migrate"]) == 0
    assert f"-> {db.head_revision()}" in capsys.readouterr().out
    assert admin_cli.main(["migrate"]) == 0
    assert "nothing to do" in capsys.readouterr().out

    with db.session_scope() as s:
        s.add(models.User(username="u", password_hash="x", role="user", is_active=True))
        s.flush()
        uid = s.query(models.User).one().id
        for rid, status in (("r1", "running"), ("r2", "running"), ("r3", "done"), ("r4", "interrupted")):
            s.add(models.Run(user_id=uid, run_id=rid, question="q", status=status, plan_mode=False))
        s.commit()
    assert admin_cli.main(["active-runs"]) == 0
    assert capsys.readouterr().out.strip() == "2"


def test_0002_adds_key_fp_and_its_index_once(fresh_db):
    """A database at the baseline gets llm_calls.key_fp and its index from 0002; one that already has
    them (main's start-up step added them before migrations) passes through it unchanged."""
    from alembic import command

    db, url = fresh_db
    cfg = db._alembic_config()
    with db.get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001")
    assert "key_fp" not in _columns(url, "llm_calls")
    assert db.migrate() == ("0001", db.head_revision())
    assert "key_fp" in _columns(url, "llm_calls")
    indexes = {i["name"] for i in inspect(create_engine(url)).get_indexes("llm_calls")}
    assert "ix_llm_calls_key_fp_created" in indexes

    with create_engine(url).begin() as c:        # back to "0001" with the column and index still there
        c.execute(text("UPDATE alembic_version SET version_num = '0001'"))
    assert db.migrate() == ("0001", db.head_revision())
