"""Dataset retention: 90 days from addition or last use in a run, a 7-day notice (by email when the
account has an address, always on the site), a removal email, and active datasets left alone."""
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
    monkeypatch.setenv("AISCIENTIST_DATASET_NOTICE_DAYS", "7")
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


def test_notice_seven_days_before_removal_and_an_email_at_removal(policy):
    p = policy
    asyncio.run(p.service.sweep(p.gateway))
    assert len(p.sent) == 1
    assert p.sent[0][1] == "AiScientist: dataset removal notice"
    assert "counts.csv" in p.sent[0][2] and "123 bytes" in p.sent[0][2]
    assert "https://example.org/api/datasets/1/download" in p.sent[0][2]
    assert "Using it in a run before then keeps it for another 90 days" in p.sent[0][2]
    notice = p.service.summary(p.user_id)["notices"][0]
    assert notice["removal_at"] == (p.clock[0] + dt.timedelta(days=7)).isoformat()
    assert p.service.removal_at(p.row) == notice["removal_at"]
    assert not p.deleted
    p.clock[0] += dt.timedelta(days=6)
    asyncio.run(p.service.sweep(p.gateway))
    assert len(p.sent) == 1 and not p.deleted
    p.clock[0] += dt.timedelta(days=1)
    asyncio.run(p.service.sweep(p.gateway))
    assert p.deleted == [p.row["id"]]
    assert len(p.sent) == 2 and p.sent[1][0] == "owner@example.org"
    assert p.sent[1][1] == "AiScientist: dataset removed" and "counts.csv" in p.sent[1][2]
    assert "https://example.org/?account=storage" in p.sent[1][2]
    summary = p.service.summary(p.user_id)
    assert not summary["notices"]
    assert summary["retention_removals"] == [{"dataset_name": "counts.csv", "size_bytes": 123,
        "owner": "owner", "removed_at": p.clock[0].isoformat()}]


@pytest.mark.parametrize("missing", ["email_failure", "smtp", "public_url"])
def test_an_account_with_an_address_is_removed_only_after_its_email(policy, monkeypatch, missing):
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


def test_notice_lead_cannot_be_shortened_below_seven_days(policy, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATASET_NOTICE_DAYS", "1")
    assert policy.service.notice_days() == 7
    monkeypatch.delenv("AISCIENTIST_DATASET_NOTICE_DAYS")
    assert policy.service.notice_days() == 7


def _set(p, **fields):
    with p.db.session_scope() as session:
        user = session.get(p.models.User, p.user_id)
        for key, value in fields.items():
            setattr(user, key, value)
        session.commit()


@pytest.mark.parametrize("missing_smtp", [False, True])
def test_an_account_without_an_address_is_told_on_the_site_and_removed(policy, monkeypatch, missing_smtp):
    p = policy
    _set(p, email=None)
    if missing_smtp:
        monkeypatch.delenv("AISCIENTIST_SMTP_HOST")
        monkeypatch.delenv("AISCIENTIST_PUBLIC_URL")
    asyncio.run(p.service.sweep(p.gateway))
    notice = p.service.summary(p.user_id)["notices"][0]
    assert notice["removal_at"] == (p.clock[0] + dt.timedelta(days=7)).isoformat()
    p.clock[0] += dt.timedelta(days=7)
    asyncio.run(p.service.sweep(p.gateway))
    assert p.deleted == [p.row["id"]] and not p.sent


def test_a_run_restarts_the_period_and_withdraws_the_notice(policy):
    p = policy
    asyncio.run(p.service.sweep(p.gateway))
    assert p.service.summary(p.user_id)["notices"]
    p.clock[0] += dt.timedelta(days=3)
    used_on = p.clock[0]
    assert p.service.mark_used(p.user_id, [p.row["path"]]) == 1
    assert not p.service.summary(p.user_id)["notices"], "the waiting notice is withdrawn"
    with p.db.session_scope() as session:
        row = session.get(p.models.Dataset, p.row["id"]).public()
    assert p.service._utc(row["last_used_at"]) == used_on       # SQLite drops the zone; Postgres keeps it
    assert p.service.removal_at(row) == (used_on + dt.timedelta(days=90)).isoformat()
    p.clock[0] += dt.timedelta(days=4)
    asyncio.run(p.service.sweep(p.gateway))
    assert not p.deleted and len(p.sent) == 1
    p.clock[0] = used_on + dt.timedelta(days=83)
    asyncio.run(p.service.sweep(p.gateway))
    assert len(p.sent) == 2 and not p.deleted, "a new notice 7 days before the new date"
    p.clock[0] += dt.timedelta(days=7)
    asyncio.run(p.service.sweep(p.gateway))
    assert p.deleted == [p.row["id"]]


def test_a_path_inside_a_dataset_folder_counts_as_using_it(policy, tmp_path):
    p = policy
    with p.db.session_scope() as session:
        session.get(p.models.Dataset, p.row["id"]).path = str(tmp_path / "library")
        session.commit()
    assert p.service.mark_used(p.user_id, [str(tmp_path / "library" / "S1" / "matrix.h5")]) == 1
    assert p.service.mark_used(p.user_id, [str(tmp_path / "library2")]) == 0
    assert p.service.mark_used(p.user_id + 1, [str(tmp_path / "library")]) == 0, "another account's"


def test_a_saved_result_has_its_own_period(policy, tmp_path):
    p = policy
    with p.db.session_scope() as session:
        result = p.models.Dataset(user_id=p.user_id, name="counts.processed.h5ad",
            path=str(tmp_path / "counts.processed.h5ad"), size_bytes=456,
            uploaded_at=p.clock[0] - dt.timedelta(days=10))
        session.add(result)
        session.commit()
        result_row = result.public()
    asyncio.run(p.service.sweep(p.gateway))
    assert [n["dataset_name"] for n in p.service.summary(p.user_id)["notices"]] == ["counts.csv"]
    assert p.service.removal_at(result_row) == (p.clock[0] + dt.timedelta(days=80)).isoformat()


def test_earlier_runs_restart_the_period_of_the_datasets_they_read(policy, tmp_path):
    pytest.importorskip("fastapi")
    import json
    p = policy
    runs = tmp_path / "runs"
    with p.db.session_scope() as session:
        uuid = session.get(p.models.Dataset, p.row["id"]).uuid
        other = p.models.User(username="someone", email=None, password_hash="unused")
        session.add(other)
        session.flush()
        for run_id, user_id, days in (("r1", p.user_id, 30), ("r2", p.user_id, 5), ("r3", other.id, 1)):
            session.add(p.models.Run(user_id=user_id, run_id=run_id, question="q", status="done",
                                     created_at=p.clock[0] - dt.timedelta(days=days)))
        session.commit()
    for owner, run_id in (("owner", "r1"), ("owner", "r2"), ("someone", "r3")):
        art = runs / owner / run_id / "artifacts"
        art.mkdir(parents=True)
        (art / "croissant.json").write_text(json.dumps({"@type": "sc:Dataset", "isBasedOn": [
            {"@id": f"urn:uuid:{uuid}"}]}), encoding="utf-8")
    assert p.service.backfill_last_used(runs) == 1
    with p.db.session_scope() as session:
        row = session.get(p.models.Dataset, p.row["id"]).public()
    assert p.service._utc(row["last_used_at"]) == p.clock[0] - dt.timedelta(days=5), \
        "the owner's latest run; another account's run naming it does not count"
    assert p.service.backfill_last_used(runs) == 0, "nothing new the second time"


def test_a_lab_run_records_the_datasets_it_binds(monkeypatch):
    pytest.importorskip("fastapi")
    import inspect
    from aiscientist.gateway import app as gw, storage_policy

    calls = []
    monkeypatch.setattr(storage_policy, "mark_used", lambda user_id, paths: calls.append((user_id, paths)))
    conn = SimpleNamespace(app_user_id=7)
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    gw._mark_bound_datasets_used(conn, ["/data/a"])
    assert calls == [], "no accounts, no retention"
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    gw._mark_bound_datasets_used(conn, ["/data/a", "/data/b"], ["/data/a-moved"])
    assert calls == [(7, ["/data/a", "/data/b", "/data/a-moved"])]
    monkeypatch.setattr(storage_policy, "mark_used", lambda *a: 1 / 0)
    gw._mark_bound_datasets_used(conn, ["/data/a"])          # a failure never stops the run
    assert "_mark_bound_datasets_used, conn" in inspect.getsource(gw._run_lab), "called when a lab run starts"
