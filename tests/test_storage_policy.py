"""Storage retention requires delivered notice and leaves active datasets alone."""
import asyncio
import datetime as dt
import importlib
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")


@pytest.fixture()
def policy(tmp_path, monkeypatch):
    from aiscientist.gateway import db, models, storage_policy
    importlib.reload(db)
    importlib.reload(models)
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{tmp_path / 'retention.db'}")
    monkeypatch.setenv("AISCIENTIST_DATASET_RETENTION_DAYS", "90")
    monkeypatch.setenv("AISCIENTIST_DATASET_NOTICE_DAYS", "14")
    monkeypatch.setenv("AISCIENTIST_PUBLIC_URL", "https://example.org")
    monkeypatch.setenv("AISCIENTIST_SMTP_HOST", "smtp.example.org")
    db.init_db()
    clock = [dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(storage_policy, "_now", lambda: clock[0])
    sent = []
    monkeypatch.setattr(storage_policy.email_send, "send_email",
                        lambda *args: sent.append(args) or True)
    with db.session_scope() as session:
        user = models.User(username="owner", email="owner@example.org", password_hash="unused")
        session.add(user)
        session.flush()
        dataset = models.Dataset(user_id=user.id, name="counts.csv", path=str(tmp_path / "counts.csv"),
            size_bytes=123, uploaded_at=clock[0] - dt.timedelta(days=90))
        session.add(dataset)
        session.commit()
        row, user_id = dataset.public(), user.id
    deleted = []

    async def delete(user, dataset_id):
        deleted.append(dataset_id)
        with db.session_scope() as session:
            session.delete(session.get(models.Dataset, dataset_id))
            session.commit()

    gateway = SimpleNamespace(_dataset_in_use=lambda user, path: False, _delete_owned_dataset=delete)
    yield SimpleNamespace(service=storage_policy, db=db, models=models, row=row, user_id=user_id,
                          clock=clock, sent=sent, deleted=deleted, gateway=gateway)
    db.reset()


def test_notice_persists_on_site_and_email_before_removal(policy):
    p = policy
    asyncio.run(p.service.sweep(p.gateway))
    assert len(p.sent) == 1
    assert "counts.csv" in p.sent[0][2] and "123 bytes" in p.sent[0][2]
    assert "https://example.org/api/datasets/1/download" in p.sent[0][2]
    notice = p.service.summary(p.user_id)["notices"][0]
    assert notice["removal_at"] == (p.clock[0] + dt.timedelta(days=14)).isoformat()
    assert p.service.removal_at(p.row) == notice["removal_at"]
    assert not p.deleted
    p.clock[0] += dt.timedelta(days=13)
    asyncio.run(p.service.sweep(p.gateway))
    assert len(p.sent) == 1 and not p.deleted
    p.clock[0] += dt.timedelta(days=1)
    asyncio.run(p.service.sweep(p.gateway))
    assert p.deleted == [p.row["id"]]
    summary = p.service.summary(p.user_id)
    assert not summary["notices"]
    assert summary["retention_removals"] == [{"dataset_name": "counts.csv", "size_bytes": 123,
        "owner": "owner", "removed_at": p.clock[0].isoformat()}]


@pytest.mark.parametrize("missing", ["email_failure", "smtp", "public_url"])
def test_missing_email_delivery_never_allows_removal(policy, monkeypatch, missing):
    p = policy
    if missing == "email_failure":
        monkeypatch.setattr(p.service.email_send, "send_email", lambda *args: False)
    elif missing == "smtp":
        monkeypatch.delenv("AISCIENTIST_SMTP_HOST")
    else:
        monkeypatch.delenv("AISCIENTIST_PUBLIC_URL")
    asyncio.run(p.service.sweep(p.gateway))
    p.clock[0] += dt.timedelta(days=100)
    asyncio.run(p.service.sweep(p.gateway))
    assert not p.deleted and not p.service.summary(p.user_id)["notices"]


@pytest.mark.parametrize("active", ["running", "awaiting_plan"])
def test_in_use_dataset_is_kept_until_analysis_ends(policy, active):
    p = policy
    asyncio.run(p.service.sweep(p.gateway))
    p.clock[0] += dt.timedelta(days=14)
    p.gateway._dataset_in_use = lambda user, path: active in {"running", "awaiting_plan"}
    asyncio.run(p.service.sweep(p.gateway))
    assert not p.deleted
    p.gateway._dataset_in_use = lambda user, path: False
    asyncio.run(p.service.sweep(p.gateway))
    assert p.deleted == [p.row["id"]]


def test_disabled_retention_and_shared_pointers_are_kept(policy, monkeypatch):
    p = policy
    monkeypatch.setenv("AISCIENTIST_DATASET_RETENTION_DAYS", "0")
    asyncio.run(p.service.sweep(p.gateway))
    assert p.service.removal_at(p.row) is None
    assert not p.sent and not p.deleted
    monkeypatch.setenv("AISCIENTIST_DATASET_RETENTION_DAYS", "90")
    with p.db.session_scope() as session:
        session.get(p.models.Dataset, p.row["id"]).kind = "server-folder"
        session.commit()
    asyncio.run(p.service.sweep(p.gateway))
    assert not p.sent and not p.deleted


def test_notice_lead_cannot_be_shortened_below_fourteen_days(policy, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATASET_NOTICE_DAYS", "1")
    assert policy.service.notice_days() == 14
