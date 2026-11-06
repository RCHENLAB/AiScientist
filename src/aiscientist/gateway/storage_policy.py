"""Per-account server quota, and dataset retention after a notice on the site and by email.

A dataset is kept ``retention_days()`` (90) from when it was added or, if later, from when a run last
used it: using a dataset in a run restarts its period, and a result saved to Datasets has its own
period from when it was saved (Yijun, 2026-11-05). Its owner is told ``notice_days()`` (7) before
removal, on the site and, when the account has an email address, by email; an account with no
address is told on the site only. A second email says what was removed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Iterable

from . import db, email_send
from .dataset_storage import location_of

_log = logging.getLogger(__name__)


def limit_bytes() -> int:
    return int(max(0, float(os.environ.get("AISCIENTIST_ACCOUNT_STORAGE_GB", "150"))) * 1024 ** 3)


def retention_days() -> int:
    return max(0, int(os.environ.get("AISCIENTIST_DATASET_RETENTION_DAYS", "90")))


def notice_days() -> int:
    return max(7, int(os.environ.get("AISCIENTIST_DATASET_NOTICE_DAYS", "7")))


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


def _period_start(dataset: dict) -> dt.datetime:
    """When the dataset's retention period started: when it was added, or the last run that used it."""
    start = _utc(dataset["uploaded_at"])
    if dataset.get("last_used_at"):
        start = max(start, _utc(dataset["last_used_at"]))
    return start


def removal_at(dataset: dict) -> str | None:
    if not retention_days() or not _eligible(dataset) or not dataset.get("uploaded_at"):
        return None
    from .models import Dataset, DatasetRetention
    date = _period_start(dataset) + dt.timedelta(days=retention_days())
    with db.session_scope() as session:
        uuid = dataset.get("uuid")
        if not uuid and dataset.get("id"):
            row = session.get(Dataset, dataset["id"])
            uuid = row.uuid if row else None
        record = session.get(DatasetRetention, uuid) if uuid else None
        if record:
            date = max(date, _utc(record.removal_at), _utc(record.notice_at) + dt.timedelta(days=notice_days()))
    return date.isoformat()


def mark_used(user_id: int, paths: Iterable[str], when: "dt.datetime | None" = None) -> int:
    """A run is using ``paths``: restart the retention period of each of the account's datasets they
    are (or lie inside), and withdraw a removal notice that was waiting for one. Returns how many."""
    from .models import Dataset, DatasetRetention
    wanted = [str(p).rstrip("/") for p in paths if p]
    if not wanted:
        return 0
    when = when or _now()
    count = 0
    with db.session_scope() as session:
        for dataset in session.query(Dataset).filter_by(user_id=user_id):
            base = (dataset.path or "").rstrip("/")
            if not base or not any(p == base or p.startswith(base + "/") for p in wanted):
                continue
            if dataset.last_used_at is None or _utc(dataset.last_used_at) < when:
                dataset.last_used_at = when
            record = session.get(DatasetRetention, dataset.uuid)
            if record is not None and record.removed_at is None:
                session.delete(record)
            count += 1
        session.commit()
    return count


def _based_on(value) -> "set[str]":
    """The dataset uuids a run's Croissant record names as its inputs (``isBasedOn`` /
    ``prov:wasDerivedFrom``, ``urn:uuid:<uuid>``), at any depth."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("isBasedOn", "prov:wasDerivedFrom"):
                for ref in item if isinstance(item, list) else [item]:
                    ref = ref.get("@id") if isinstance(ref, dict) else ref
                    if isinstance(ref, str) and ref.startswith("urn:uuid:"):
                        found.add(ref[len("urn:uuid:"):])
            else:
                found |= _based_on(item)
    elif isinstance(value, list):
        for item in value:
            found |= _based_on(item)
    return found


def backfill_last_used(runs_dir: Path) -> int:
    """Raise ``last_used_at`` from the runs made before it was recorded: each run's
    ``artifacts/croissant.json`` names the datasets it read, and the run's start (its history row,
    else the file's time) is when. Only the owner's own runs count. Returns how many changed."""
    from .models import Dataset, Run, User
    from .app import safe_name
    with db.session_scope() as session:
        started = {r.run_id: r.created_at for r in session.query(Run)}
        owners = {u.id: safe_name(u.username) for u in session.query(User)}
    used: dict[str, dt.datetime] = {}
    for path in Path(runs_dir).glob("*/*/artifacts/croissant.json"):
        owner, run_id = path.parts[-4], path.parts[-3]
        try:
            uuids = _based_on(json.loads(path.read_text(encoding="utf-8")))
            when = started.get(run_id) or dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
        except (OSError, ValueError):
            continue
        for uuid in uuids:
            key = f"{owner}/{uuid}"
            if key not in used or _utc(when) > used[key]:
                used[key] = _utc(when)
    changed = 0
    with db.session_scope() as session:
        for dataset in session.query(Dataset):
            when = used.get(f"{owners.get(dataset.user_id)}/{dataset.uuid}")
            if when is None or (dataset.last_used_at is not None and _utc(dataset.last_used_at) >= when):
                continue
            dataset.last_used_at = when
            changed += 1
        session.commit()
    return changed


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
    cutoff = _now() + dt.timedelta(days=notice_days())
    due = []
    for dataset in datasets:
        date = removal_at(dataset)
        if date and _utc(date) <= cutoff:
            due.append({"dataset_id": dataset["id"], "dataset_name": dataset["name"],
                        "size_bytes": dataset["size_bytes"], "removal_at": date,
                        "download_url": f"/api/datasets/{dataset['id']}/download"})
    return due


def _base_url() -> str:
    return (os.environ.get("AISCIENTIST_PUBLIC_URL") or "").strip().rstrip("/")


def _can_email() -> bool:
    return email_send.smtp_configured() and bool(_base_url())


def _size(n: int) -> str:
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} bytes"


def _notice_body(dataset: dict, date: dt.datetime) -> str:
    return (f"Your dataset {dataset['name']} ({_size(int(dataset['size_bytes'] or 0))}) will be removed "
            f"from AiScientist on {date:%Y-%m-%d} (UTC): by then it will not have been used in a run "
            f"for at least {retention_days()} days.\n"
            f"Download: {_base_url()}/api/datasets/{dataset['id']}/download\n"
            f"Using it in a run before then keeps it for another {retention_days()} days. "
            "Your chats remain either way.")


def _removal_body(dataset: dict, removed: dt.datetime) -> str:
    return (f"Your dataset {dataset['name']} ({_size(int(dataset['size_bytes'] or 0))}) was removed "
            f"from AiScientist on {removed:%Y-%m-%d} (UTC): it had not been used in a run for at least "
            f"{retention_days()} days. Your chats remain. Account → Storage lists what was removed: "
            f"{_base_url()}/?account=storage")


async def sweep(gateway) -> None:
    """Notice first; remove only after both the retention and the notice periods have elapsed.

    An account with an email address is told by email and on the site, and is not removed until the
    email was delivered (no SMTP or no ``AISCIENTIST_PUBLIC_URL`` means it waits). An account with no
    address is told on the site only. After a removal its owner gets a second email, when it has one."""
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
                date = max(_utc(date), now + lead)
                if user.email:
                    if not _can_email():
                        continue
                    sent = await asyncio.to_thread(email_send.send_email, user.email,
                                                   "AiScientist: dataset removal notice",
                                                   _notice_body(dataset, date))
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
            removed = _now()
            with db.session_scope() as session:
                record = session.get(DatasetRetention, dataset["uuid"])
                record.size_bytes = dataset["size_bytes"]
                if record.removed_at is None:
                    record.removed_at = removed
                session.commit()
            if user.email and _can_email():
                sent = await asyncio.to_thread(email_send.send_email, user.email,
                                               "AiScientist: dataset removed",
                                               _removal_body(dataset, removed))
                if not sent:
                    _log.warning("Removal email for dataset %s was not delivered", dataset["id"])
        except Exception:
            _log.exception("Dataset retention failed for dataset %s", dataset["id"])
