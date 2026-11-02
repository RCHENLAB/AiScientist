"""Per-account server quota and dataset retention after an emailed, persistent notice."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os

from . import db, email_send
from .dataset_storage import location_of

_log = logging.getLogger(__name__)


def limit_bytes() -> int:
    return int(max(0, float(os.environ.get("AISCIENTIST_ACCOUNT_STORAGE_GB", "150"))) * 1024 ** 3)


def retention_days() -> int:
    return max(0, int(os.environ.get("AISCIENTIST_DATASET_RETENTION_DAYS", "90")))


def notice_days() -> int:
    return max(14, int(os.environ.get("AISCIENTIST_DATASET_NOTICE_DAYS", "14")))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc(value) -> dt.datetime:
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _eligible(dataset: dict) -> bool:
    return (not str(dataset.get("kind") or "").startswith("server-")
            and location_of(dataset.get("path") or "") != "hpc3"
            and dataset.get("status", "complete") == "complete")


def removal_at(dataset: dict) -> str | None:
    if not retention_days() or not _eligible(dataset) or not dataset.get("uploaded_at"):
        return None
    from .models import Dataset, DatasetRetention
    date = _utc(dataset["uploaded_at"]) + dt.timedelta(days=retention_days())
    with db.session_scope() as session:
        uuid = dataset.get("uuid")
        if not uuid and dataset.get("id"):
            row = session.get(Dataset, dataset["id"])
            uuid = row.uuid if row else None
        record = session.get(DatasetRetention, uuid) if uuid else None
        if record:
            date = max(date, _utc(record.removal_at), _utc(record.notice_at) + dt.timedelta(days=notice_days()))
    return date.isoformat()


def quota_error(user_id: int, username: str, incoming_bytes: int) -> str | None:
    limit = limit_bytes()
    if not limit:
        return None
    from .app import _server_storage, CONSOLE_RUNS_DIR, safe_name
    import json
    pending = {}
    for pointer in (CONSOLE_RUNS_DIR / safe_name(username)).glob("*/artifacts/data/*.json"):
        try:
            record = json.loads(pointer.read_text(encoding="utf-8"))
            for item in record.get("datasets") or [record]:
                if item.get("saving") and item.get("location") == "hpc3" and not item.get("saved_to"):
                    pending[item.get("path") or str(pointer)] = int(item.get("size_bytes") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    used = _server_storage(user_id, username)["total_bytes"] + sum(pending.values())
    if used + max(0, incoming_bytes) <= limit:
        return None
    left = max(0, limit - used) / 1024 ** 3
    return (f"Account storage limit reached: {left:.2f} GB of space left. "
            "Free space in [Account → Storage](/?account=storage) before saving more datasets.")


def _notice(record, date: str | None = None) -> dict:
    return {"dataset_id": record.dataset_id, "dataset_name": record.dataset_name,
            "size_bytes": record.size_bytes, "removal_at": date or _utc(record.removal_at).isoformat(),
            "download_url": f"/api/datasets/{record.dataset_id}/download"}


def summary(user_id: int) -> dict:
    from .models import Dataset, DatasetRetention
    with db.session_scope() as session:
        records = session.query(DatasetRetention).filter_by(user_id=user_id).all()
        datasets = {d.uuid: d.public() for d in session.query(Dataset).filter_by(user_id=user_id)}
    notices, removed = [], []
    for record in records:
        if record.removed_at:
            removed.append({"dataset_name": record.dataset_name, "size_bytes": record.size_bytes,
                            "owner": record.owner, "removed_at": _utc(record.removed_at).isoformat()})
        elif record.dataset_uuid in datasets:
            date = removal_at(datasets[record.dataset_uuid])
            if date:
                notices.append(_notice(record, date))
    removed.sort(key=lambda row: row["removed_at"], reverse=True)
    return {"notices": notices, "retention_removals": removed}


def due_datasets(user_id: int) -> list[dict]:
    from .models import Dataset
    with db.session_scope() as session:
        datasets = [d.public() for d in session.query(Dataset).filter_by(user_id=user_id)]
    cutoff = _now() + dt.timedelta(days=14)
    due = []
    for dataset in datasets:
        date = removal_at(dataset)
        if date and _utc(date) <= cutoff:
            due.append({"dataset_id": dataset["id"], "dataset_name": dataset["name"],
                        "size_bytes": dataset["size_bytes"], "removal_at": date,
                        "download_url": f"/api/datasets/{dataset['id']}/download"})
    return due


async def sweep(gateway) -> None:
    """Notice first; remove only after both the age and notice periods have elapsed."""
    if not retention_days():
        return
    from .models import Dataset, DatasetRetention, User
    now, lead = _now(), dt.timedelta(days=notice_days())
    with db.session_scope() as session:
        datasets = [d.public() for d in session.query(Dataset)]
        users = {u.id: u for u in session.query(User)}
    for dataset in datasets:
        user = users.get(dataset["owner"])
        date = removal_at(dataset)
        if user is None or date is None or _utc(date) > now + lead:
            continue
        try:
            with db.session_scope() as session:
                record = session.get(DatasetRetention, dataset["uuid"])
            if record is None:
                base = (os.environ.get("AISCIENTIST_PUBLIC_URL") or "").strip().rstrip("/")
                if not user.email or not email_send.smtp_configured() or not base:
                    continue
                date = max(_utc(date), now + lead)
                body = (f"Your dataset {dataset['name']} ({dataset['size_bytes']} bytes) is scheduled "
                        f"for removal on {date.isoformat()}.\n"
                        f"Download: {base}/api/datasets/{dataset['id']}/download\n"
                        "The files and dataset entry will be removed; your chats will remain.")
                sent = await asyncio.to_thread(email_send.send_email, user.email,
                                               "AiScientist: dataset removal notice", body)
                if not sent:
                    continue
                # The site's notice becomes visible when this transaction commits. Count the
                # full lead time from then, including time spent sending the email.
                noticed = _now()
                with db.session_scope() as session:
                    session.add(DatasetRetention(dataset_uuid=dataset["uuid"], dataset_id=dataset["id"],
                        user_id=user.id, owner=user.username, dataset_name=dataset["name"],
                        size_bytes=dataset["size_bytes"], notice_at=noticed,
                        removal_at=max(date, noticed + lead)))
                    session.commit()
                continue
            if record.removed_at or _utc(date) > now:
                continue
            if gateway._dataset_in_use(user, dataset["path"]):
                continue
            result = await gateway._delete_owned_dataset(user, dataset["id"])
            if result is False or getattr(result, "status_code", 200) >= 400:
                continue
            with db.session_scope() as session:
                record = session.get(DatasetRetention, dataset["uuid"])
                record.size_bytes = dataset["size_bytes"]
                if record.removed_at is None:
                    record.removed_at = _now()
                session.commit()
        except Exception:
            _log.exception("Dataset retention failed for dataset %s", dataset["id"])
