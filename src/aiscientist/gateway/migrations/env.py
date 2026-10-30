"""Alembic environment for the accounts database. ``db.migrate()`` runs it with the connection it
opened (``config.attributes["connection"]``), so the database URL never passes through a config file."""

from __future__ import annotations

from alembic import context

from aiscientist.gateway import models  # noqa: F401 - registers the tables on Base.metadata
from aiscientist.gateway.db import Base, get_engine

target_metadata = Base.metadata


def _run(connection) -> None:
    # render_as_batch: SQLite (development, CI) can only change a column by copying the table.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:  # the alembic command line, for writing a new revision: python -m alembic -c <ini> revision ...
    with get_engine().begin() as conn:
        _run(conn)
