"""Offline test for the dataset-delete DB helper — no cluster, no SSH, no network.

Covers ``delete_dataset_record``, the owner-scoped helper behind
``POST /api/datasets/delete``: a user can remove their OWN uploaded dataset (manual,
explicit) but never another account's, and a missing id is a no-op. There is no
automatic deletion of research data anywhere — removal is always user-initiated.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def db_ctx(tmp_path, monkeypatch):
    pytest.importorskip("sqlalchemy")
    monkeypatch.setenv("BIOAGENT_DATABASE_URL", f"sqlite:///{(tmp_path / 'd.db').as_posix()}")
    from bioagent.gateway import auth_routes, db, models  # noqa: F401

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


def test_delete_dataset_record_enforces_ownership(db_ctx):
    auth_routes, db, models = db_ctx
    alice = _mk_user(db, models, "alice")
    bob = _mk_user(db, models, "bob")
    ds_id = auth_routes.record_dataset(alice, "pbmc.h5ad", "/data/alice/uploads/pbmc.h5ad", 10, "h5ad")

    # Bob cannot delete Alice's dataset — returns None and leaves the row intact.
    assert auth_routes.delete_dataset_record(bob, ds_id) is None
    assert auth_routes.delete_dataset_record(alice, 999999) is None  # missing id

    # The owner gets the stored path back and the row is gone afterwards.
    path = auth_routes.delete_dataset_record(alice, ds_id)
    assert path == "/data/alice/uploads/pbmc.h5ad"
    assert auth_routes.delete_dataset_record(alice, ds_id) is None   # already deleted
