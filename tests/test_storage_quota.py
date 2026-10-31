# ruff: noqa: F401, F811
"""Account quota admission uses the same total as the Storage tab."""
import base64
import json
from types import SimpleNamespace

from test_storage_tab import gw, server  # noqa: F401, F811


def test_upload_refused_before_creating_any_partial(server, tmp_path, monkeypatch):
    from aiscientist.gateway import storage_policy
    conn = SimpleNamespace(app_user_id=server.uid, owner="yijus12", id="quota",
                           workspace=tmp_path / "work", managed_storage=False)
    monkeypatch.setattr(server.gw, "_owned_conn", lambda *args, **kwargs: conn)
    monkeypatch.setattr(storage_policy, "limit_bytes", lambda: 10)
    server.auth.record_dataset(server.uid, "existing", str(tmp_path / "existing"), 8, "h5ad")
    fields = {"connection_id": "quota", "filename": "new.csv"}
    meta = ",".join(f"{k} {base64.b64encode(v.encode()).decode()}" for k, v in fields.items())
    response = server.client.post("/api/tus", headers={"Tus-Resumable": "1.0.0",
        "Upload-Length": "3", "Upload-Metadata": meta})
    assert response.status_code == 507
    assert "left" in response.json()["error"]
    assert response.json()["storage_url"]
    assert not (conn.workspace / "uploads").exists()


def test_result_save_over_limit_keeps_run_files(server, tmp_path, monkeypatch):
    from aiscientist.gateway import storage_policy
    from aiscientist.tools.api import RESULT_DATASET_POINTER
    monkeypatch.setattr(storage_policy, "limit_bytes", lambda: 10)
    run_id = "abc123"
    name = "sample.processed.20261030-1200.abc123.h5ad"
    root = server.results / "yijus12" / run_id
    source = root / "result" / name
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x" * 11)
    pointer = root / "artifacts/data" / RESULT_DATASET_POINTER
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"name": name, "path": str(source), "size_bytes": 11,
                                   "location": "gateway"}))
    server.auth.record_run_start(server.uid, run_id, "QC", plan_mode=False)
    status, body = server.gw._save_run_result("yijus12", server.uid, run_id, "dataset", None)
    assert status == 507 and "left" in body["error"]
    assert source.read_bytes() == b"x" * 11
    assert not server.auth.list_dataset_records(server.uid)


def test_local_result_move_does_not_double_count(server, monkeypatch):
    from aiscientist.gateway import storage_policy
    monkeypatch.setattr(storage_policy, "limit_bytes", lambda: 10)
    monkeypatch.setattr(server.gw, "_server_storage", lambda *args: {"total_bytes": 10})
    assert storage_policy.quota_error(server.uid, "yijus12", 0) is None
    assert storage_policy.quota_error(server.uid, "yijus12", 1)


def test_second_upload_reserves_first_upload_space(server, tmp_path, monkeypatch):
    from aiscientist.gateway import storage_policy
    conn = SimpleNamespace(app_user_id=server.uid, owner="yijus12", id="quota",
                           workspace=tmp_path / "work", managed_storage=False)
    monkeypatch.setattr(server.gw, "_owned_conn", lambda *args, **kwargs: conn)
    monkeypatch.setattr(storage_policy, "limit_bytes", lambda: 10)
    meta = ",".join(f"{k} {base64.b64encode(v.encode()).decode()}" for k, v in
                    {"connection_id": "quota", "filename": "new.csv"}.items())
    headers = {"Tus-Resumable": "1.0.0", "Upload-Length": "6", "Upload-Metadata": meta}
    assert server.client.post("/api/tus", headers=headers).status_code == 201
    assert server.client.post("/api/tus", headers=headers).status_code == 507


def test_retention_reuses_delete_and_keeps_chats(server, tmp_path, monkeypatch):
    import asyncio
    import datetime as dt
    from uuid import uuid4
    from aiscientist.gateway import dataset_storage, storage_policy
    now = [dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(storage_policy, "_now", lambda: now[0])
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AISCIENTIST_PUBLIC_URL", "https://example.org")
    monkeypatch.setenv("AISCIENTIST_SMTP_HOST", "smtp.example.org")
    monkeypatch.setattr(storage_policy.email_send, "send_email", lambda *args: True)
    path = dataset_storage.dataset_dir(str(uuid4()), "yijus12") / "original" / "data.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"abc")
    did = server.auth.record_dataset(server.uid, "data.csv", str(path), 3, "csv")
    with server.db.session_scope() as session:
        session.get(server.models.User, server.uid).email = "owner@example.org"
        session.get(server.models.Dataset, did).uploaded_at = now[0] - dt.timedelta(days=90)
        chat = server.models.Conversation(user_id=server.uid, title="Keep this chat")
        session.add(chat)
        session.commit()
        chat_id = chat.id
    asyncio.run(storage_policy.sweep(server.gw))
    now[0] += dt.timedelta(days=14)
    asyncio.run(storage_policy.sweep(server.gw))
    assert not path.parent.parent.exists()
    with server.db.session_scope() as session:
        assert session.get(server.models.Dataset, did) is None
        assert session.get(server.models.Conversation, chat_id).title == "Keep this chat"
    assert storage_policy.summary(server.uid)["retention_removals"][0]["size_bytes"] == 3


def test_persisted_waiting_plan_protects_its_dataset(server):
    user = SimpleNamespace(id=server.uid, username="yijus12")
    path = "/data/owned/counts.csv"
    server.auth.record_run_start(server.uid, "plan123", "QC", plan_mode=True)
    with server.db.session_scope() as session:
        session.query(server.models.Run).filter_by(run_id="plan123").one().status = "awaiting_plan"
        session.commit()
    state = server.results / "yijus12/plan123/artifacts/process/run_state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"datasets": [{"path": path}]}))
    assert server.gw._dataset_in_use(user, path)
    assert not server.gw._dataset_in_use(user, "/data/other.csv")


def test_remote_result_save_reserves_copy_before_starting(server, monkeypatch):
    from aiscientist.gateway import storage_policy
    monkeypatch.setattr(storage_policy, "limit_bytes", lambda: 10)
    monkeypatch.setattr(server.gw, "_server_storage", lambda *args: {"total_bytes": 4})
    root = server.results / "yijus12" / "abc123" / "artifacts/data"
    root.mkdir(parents=True)
    (root / "result_dataset.json").write_text(json.dumps({
        "path": "/hpc/result.h5ad", "size_bytes": 5, "location": "hpc3", "saving": {"started_at": "now"}}))
    assert storage_policy.quota_error(server.uid, "yijus12", 1) is None
    assert storage_policy.quota_error(server.uid, "yijus12", 2)
