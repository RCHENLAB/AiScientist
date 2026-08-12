"""Database layer for user accounts / datasets / runs.

SQLAlchemy 2.0 over a single ``BIOAGENT_DATABASE_URL``:

* **Local / CI** — defaults to a SQLite file (stdlib driver, zero setup), so the
  models + auth + admin flow are fully testable on a laptop with no PostgreSQL.
* **Server** — set ``BIOAGENT_DATABASE_URL=postgresql+psycopg://bioagent:<pw>@localhost/bioagent``
  and the SAME code runs against PostgreSQL (only the connection string changes).

The engine is created lazily + cached so importing this module costs nothing; tests
call :func:`reset` to point at a fresh temp database. No external API, no network.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def database_url() -> str:
    """The active DB URL — env override, else a local SQLite file under the state dir."""
    url = os.environ.get("BIOAGENT_DATABASE_URL")
    if url:
        return url
    state_dir = Path(os.environ.get("BIOAGENT_STATE_DIR", "."))
    state_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(state_dir / 'bioagent.db').as_posix()}"


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _ensure() -> tuple[Engine, sessionmaker[Session]]:
    global _engine, _SessionLocal
    if _engine is None:
        url = database_url()
        # SQLite needs check_same_thread=False because the gateway touches the DB from
        # worker threads (lab runs, provisioning) as well as the event loop.
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, class_=Session)
    return _engine, _SessionLocal


def get_engine() -> Engine:
    return _ensure()[0]


def session_scope() -> Session:
    """A new ORM session. Caller is responsible for closing (use ``with``)."""
    return _ensure()[1]()


def init_db() -> None:
    """Create all tables if missing (dev/first-run convenience; prod uses Alembic)."""
    from . import models  # noqa: F401 - register the ORM models on Base.metadata

    engine = get_engine()
    Base.metadata.create_all(engine)
    # Lightweight additive migrations for columns added after a table was first created.
    # ``create_all`` never ALTERs an existing table, so a column added to a model would be
    # absent from an already-deployed DB and every INSERT would fail. Each entry is an
    # idempotent ADD COLUMN (skipped when the column already exists), which SQLite and
    # PostgreSQL both support for a nullable column with no default. Keep these forward-only.
    _ensure_columns(engine, "runs", {"conversation_id": "VARCHAR(64)"})


def _ensure_columns(engine: Engine, table: str, columns: dict[str, str]) -> None:
    """Best-effort ``ALTER TABLE <table> ADD COLUMN`` for any of ``columns`` (name -> SQL
    type) missing from an existing table. No-op when the table doesn't exist yet (create_all
    just made it with the columns). Never raises past a warning — history is best-effort."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if table not in insp.get_table_names():
            return
        existing = {c["name"] for c in insp.get_columns(table)}
        missing = {name: sqltype for name, sqltype in columns.items() if name not in existing}
        if not missing:
            return
        with engine.begin() as conn:
            for name, sqltype in missing.items():
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {sqltype}'))
    except Exception as exc:  # noqa: BLE001 - a migration failure must not block startup
        print(f"[db] additive migration for {table} skipped: {exc}")


def reset(url: str | None = None) -> None:
    """Dispose + drop the cached engine so the next use rebuilds it (tests/reconfig)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
    if url is not None:
        os.environ["BIOAGENT_DATABASE_URL"] = url
