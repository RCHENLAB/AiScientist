# ruff: noqa: F401, F811
"""A test site (the staging site) removes chats, runs and datasets its testers left behind."""
import asyncio
import datetime as dt
from uuid import uuid4

import pytest

from test_storage_tab import gw, server  # noqa: F401, F811

NOW = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
OLD = NOW - dt.timedelta(days=10)
RECENT = NOW - dt.timedelta(days=1)


@pytest.fixture()
def site(server, tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_TEST_SITE_DAYS", "7")
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    return server


def _sweep(server):
    from aiscientist.gateway import test_site
    return asyncio.run(test_site.sweep(server.gw, now=NOW))


def _dataset(server, when, uid=None):
    from aiscientist.gateway import dataset_storage
    path = dataset_storage.dataset_dir(str(uuid4()), "yijus12") / "original" / "data.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"abc")
    did = server.auth.record_dataset(uid or server.uid, "data.csv", str(path), 3, "csv")
    with server.db.session_scope() as s:
        s.get(server.models.Dataset, did).uploaded_at = when
        s.commit()
    return did, path


def _chat(server, when, *run_ids, status="done"):
    m = server.models
    with server.db.session_scope() as s:
        chat = m.Conversation(user_id=server.uid, title="chat")
        s.add(chat)
        s.flush()
        s.add(m.Message(conversation_id=chat.id, role="user", content="QC please", seq=0))
        chat.updated_at = when
        s.commit()
        cid = chat.id
    for run_id in run_ids:
        _run(server, run_id, when, conversation_id=str(cid), status=status)
    return cid


def _run(server, run_id, when, conversation_id=None, status="done"):
    server.auth.record_run_start(server.uid, run_id, "QC", plan_mode=False, conversation_id=conversation_id)
    with server.db.session_scope() as s:
        row = s.query(server.models.Run).filter_by(run_id=run_id).one()
        row.created_at, row.finished_at, row.status = when, when, status
        s.commit()
    folder = server.results / "yijus12" / run_id / "artifacts"
    folder.mkdir(parents=True)
    (folder / "report.md").write_text("report")


def _exists(server, model, key):
    with server.db.session_scope() as s:
        return s.get(model, key) is not None


def _run_exists(server, run_id):
    with server.db.session_scope() as s:
        return s.query(server.models.Run).filter_by(run_id=run_id).first() is not None


def test_a_production_site_removes_nothing(server, monkeypatch):
    monkeypatch.delenv("AISCIENTIST_TEST_SITE_DAYS", raising=False)
    cid = _chat(server, OLD, "old1")
    assert _sweep(server) == {}
    assert _exists(server, server.models.Conversation, cid) and _run_exists(server, "old1")


def test_what_was_left_goes_and_what_is_in_use_stays(site):
    m = site.models
    old_chat = _chat(site, OLD, "old1")
    new_chat = _chat(site, RECENT, "new1")
    _run(site, "orphan", OLD)
    old_data, old_path = _dataset(site, OLD)
    new_data, new_path = _dataset(site, RECENT)

    assert _sweep(site) == {"chats": 1, "runs": 2, "datasets": 1}

    assert not _exists(site, m.Conversation, old_chat) and not _run_exists(site, "old1")
    assert not (site.results / "yijus12/old1").exists() and not _run_exists(site, "orphan")
    with site.db.session_scope() as s:
        assert not s.query(m.Message).filter_by(conversation_id=old_chat).count()
    assert not _exists(site, m.Dataset, old_data) and not old_path.exists()
    assert _exists(site, m.Conversation, new_chat) and _run_exists(site, "new1")
    assert (site.results / "yijus12/new1/artifacts/report.md").exists()
    assert _exists(site, m.Dataset, new_data) and new_path.exists()
    assert _sweep(site) == {"chats": 0, "runs": 0, "datasets": 0}


def test_a_recent_run_keeps_an_old_chat(site):
    cid = _chat(site, OLD, "old1")
    _run(site, "new1", RECENT, conversation_id=str(cid))
    assert _sweep(site)["chats"] == 0
    assert _run_exists(site, "old1") and _run_exists(site, "new1")


@pytest.mark.parametrize("status", ["running", "awaiting_plan"])
def test_unfinished_runs_keep_their_chat(site, status):
    cid = _chat(site, OLD, "old1", status=status)
    assert _sweep(site)["chats"] == 0
    assert _exists(site, site.models.Conversation, cid) and _run_exists(site, "old1")


def test_a_run_being_executed_keeps_its_chat(site, monkeypatch):
    cid = _chat(site, OLD, "old1")
    monkeypatch.setattr(site.gw, "_run_in_flight", lambda run_id: run_id == "old1")
    assert _sweep(site)["chats"] == 0
    assert _exists(site, site.models.Conversation, cid)


def test_a_dataset_in_use_stays(site, monkeypatch):
    did, path = _dataset(site, OLD)
    monkeypatch.setattr(site.gw, "_dataset_in_use", lambda user, p: p == str(path))
    assert _sweep(site)["datasets"] == 0
    assert _exists(site, site.models.Dataset, did) and path.exists()


def test_a_run_holding_a_recent_dataset_keeps_its_chat(site):
    cid = _chat(site, OLD, "old1")
    kept = site.results / "yijus12/old1/result/sample.h5ad"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"h5")
    did = site.auth.record_dataset(site.uid, "sample.h5ad", str(kept), 2, "h5ad")
    with site.db.session_scope() as s:
        s.get(site.models.Dataset, did).uploaded_at = RECENT
        s.commit()
    assert _sweep(site) == {"chats": 0, "runs": 0, "datasets": 0}
    assert _exists(site, site.models.Conversation, cid) and kept.exists()


def test_the_lifetime_setting(monkeypatch):
    from aiscientist.gateway import test_site
    for value, expected in [("", 0), ("0", 0), ("7", 7), ("-3", 0), ("seven", 0)]:
        monkeypatch.setenv("AISCIENTIST_TEST_SITE_DAYS", value)
        assert test_site.days() == expected
