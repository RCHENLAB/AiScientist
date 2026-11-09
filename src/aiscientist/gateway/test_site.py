"""A test site removes what its testers leave behind (``AISCIENTIST_TEST_SITE_DAYS``).

The staging site sets it (``deploy/setup_staging.sh``: 7 days). Each hour, with ``N`` days set:

- a chat with no activity for ``N`` days is deleted with its messages, its runs and their result
  folders on this host;
- a run that belongs to no chat (any more) is deleted the same way ``N`` days after it started or
  ended;
- a dataset added or last used by a run ``N`` days ago is deleted as its own Delete would.

Each removal goes through the same functions as the site's Delete buttons, so the database and the
disk stay in step. Nothing is removed while a run uses it; a run whose folder holds a dataset is kept
until that dataset goes. A dataset kept on HPC3 is deleted once its owner is connected. Testers get no
notice: the site is for trying a version, not for keeping results.

Unset or 0 (the production site) does nothing; there, datasets follow ``storage_policy``.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
from collections import defaultdict
from pathlib import Path

from . import db

_log = logging.getLogger(__name__)


def days() -> int:
    try:
        return max(0, int(os.environ.get("AISCIENTIST_TEST_SITE_DAYS", "0") or 0))
    except ValueError:
        return 0


def _utc(value) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _remove_run(gateway, user, run_id: str) -> bool:
    """One run, as its Delete does: its folder, then its row and metadata. False when kept."""
    from . import auth_routes

    owner = gateway.safe_name(user.username)
    folder = gateway._run_folder(owner, run_id)
    home = (Path(gateway.CONSOLE_RUNS_DIR) / owner).resolve()

    def remove(target: Path) -> None:
        if folder is None or folder.is_symlink() or target.parent != home:
            raise ValueError("Refusing to delete outside the account's run folders")
        if target.is_dir():
            shutil.rmtree(target)

    try:
        auth_routes.delete_run_record(user.id, run_id, run_dir=folder, delete_files=remove)
    except (OSError, ValueError) as exc:
        _log.info("Test site: run %s kept: %s", run_id, exc)
        return False
    return True


def _remove_chat(user_id: int, cid: int) -> None:
    from .models import Conversation
    with db.session_scope() as session:
        chat = session.get(Conversation, cid)
        if chat is not None and chat.user_id == user_id:
            session.delete(chat)      # its messages and dataset attachments go with it
            session.commit()


async def sweep(gateway, *, now: dt.datetime | None = None) -> dict[str, int]:
    """Remove what has been left ``days()`` days; returns how many chats, runs and datasets went."""
    keep_days = days()
    if not keep_days:
        return {}
    from .models import Conversation, Dataset, Run, User

    cutoff = (now or _now()) - dt.timedelta(days=keep_days)
    with db.session_scope() as session:
        users = {u.id: u for u in session.query(User)}
        chats = [(c.id, c.user_id, _utc(c.updated_at) or _utc(c.created_at)) for c in session.query(Conversation)]
        runs = [(r.run_id, r.user_id, r.conversation_id, _utc(r.finished_at) or _utc(r.created_at), r.status)
                for r in session.query(Run)]
        datasets = [d.public() for d in session.query(Dataset)]
    removed = {"chats": 0, "runs": 0, "datasets": 0}

    # Datasets first: a dataset kept inside a run's folder holds that run (and its chat) until it goes.
    for dataset in datasets:
        user = users.get(dataset["owner"])
        last = max(filter(None, (_utc(dataset["uploaded_at"]), _utc(dataset["last_used_at"]))), default=None)
        if user is None or last is None or last > cutoff or dataset.get("status", "complete") != "complete":
            continue
        try:
            if gateway._dataset_in_use(user, dataset["path"]):
                continue
            result = await gateway._delete_owned_dataset(user, dataset["id"])
        except Exception:  # noqa: BLE001 - one dataset must not stop the sweep
            _log.exception("Test site: dataset %s could not be removed", dataset["id"])
            continue
        if getattr(result, "status_code", 200) < 400:
            removed["datasets"] += 1

    def idle(run_id: str, status: str) -> bool:
        return status not in ("running", "awaiting_plan") and not gateway._run_in_flight(run_id)

    chat_ids = {str(cid) for cid, _, _ in chats}
    by_chat: dict[str, list] = defaultdict(list)
    for run in runs:
        by_chat[str(run[2])].append(run)

    for cid, uid, updated in chats:
        user = users.get(uid)
        chat_runs = by_chat.get(str(cid), [])
        last = max(filter(None, [updated, *(r[3] for r in chat_runs)]), default=None)
        if user is None or last is None or last > cutoff:
            continue
        if not all(idle(run_id, status) for run_id, *_, status in chat_runs):
            continue
        kept = 0
        for run_id, *_ in chat_runs:
            if await asyncio.to_thread(_remove_run, gateway, user, run_id):
                removed["runs"] += 1
            else:
                kept += 1
        if kept:
            continue                  # the chat stays with the run a dataset still holds
        await asyncio.to_thread(_remove_chat, uid, cid)
        removed["chats"] += 1

    for run_id, uid, conversation_id, last, status in runs:
        user = users.get(uid)
        if (str(conversation_id) in chat_ids or user is None or last is None or last > cutoff
                or not idle(run_id, status)):
            continue
        if await asyncio.to_thread(_remove_run, gateway, user, run_id):
            removed["runs"] += 1

    if any(removed.values()):
        _log.info("Test site: removed %(chats)d chat(s), %(runs)d run(s), %(datasets)d dataset(s)", removed)
    return removed
