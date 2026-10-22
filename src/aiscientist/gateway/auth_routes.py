"""Auth + admin HTTP surface: login / logout / me, and admin user management.

Mounted on the gateway app via ``app.include_router(router)``. All endpoints are
local; there is no email or external identity provider. Admin-only routes are guarded
by :func:`require_admin`; a forgotten password is reset by an admin (no self-service).
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import os
import re
import secrets
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select

from . import auth, email_send
from .db import session_scope
from .models import (Conversation, Dataset, LlmCall, Message, PendingRegistration, Run,
                     UsageBudget, User)

router = APIRouter(prefix="/api")

# Called with the user who is logging out, before the cookie is cleared. The gateway registers
# what a logout releases on HPC3 (the account's CPU worker); a failing hook never blocks a logout.
LOGOUT_HOOKS: list = []


# --- dependencies ------------------------------------------------------------


def current_user(request: Request) -> User | None:
    """Resolve the signed session cookie to an ACTIVE user, or None."""
    uid = auth.read_session_token(request.cookies.get(auth.COOKIE_NAME, ""))
    if uid is None:
        return None
    with session_scope() as s:
        user = s.get(User, uid)
        if user is None or not user.is_active:
            return None
        s.expunge(user)   # detach so attributes are usable after the session closes
        return user


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(request: Request) -> User:
    user = require_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


# --- auth: login / logout / me ----------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(req: LoginRequest, response: Response) -> dict:
    with session_scope() as s:
        user = s.scalar(select(User).where(User.username == req.username))
        if user is None or not user.is_active or not auth.verify_password(req.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        user.last_login_at = _dt.datetime.now(_dt.timezone.utc)
        s.commit()
        token = auth.make_session_token(user.id)
        info = user.public()
    response.set_cookie(
        auth.COOKIE_NAME, token, max_age=auth.SESSION_MAX_AGE,
        httponly=True, samesite="lax", secure=auth.secure_cookies(),
    )
    return {"status": "ok", "user": info}


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    user = current_user(request) if LOGOUT_HOOKS else None
    if user is not None:
        for hook in list(LOGOUT_HOOKS):
            try:
                hook(user)
            except Exception as exc:  # noqa: BLE001 - housekeeping never blocks a logout
                print(f"[auth] logout hook {getattr(hook, '__name__', hook)} failed: "
                      f"{type(exc).__name__}: {exc}")
    response.delete_cookie(auth.COOKIE_NAME, samesite="lax", secure=auth.secure_cookies())
    return {"status": "ok"}


@router.get("/auth/me")
def me(request: Request) -> dict:
    user = current_user(request)
    return {"authenticated": user is not None, "user": user.public() if user else None}


# --- self-registration (UCI email + emailed verification code) --------------
# A prospective user registers themselves, gated by (a) a UCI email address and (b) a
# 6-digit code emailed to that address. Nothing is minted until the code is verified —
# the pending signup (with a bcrypt-hashed password + hashed code) lives in
# ``pending_registrations`` until then. Enabled by default; disable with
# ``AISCIENTIST_ALLOW_SELF_REGISTER=0``. Allowed email domains: ``AISCIENTIST_ALLOWED_EMAIL_DOMAINS``
# (comma-separated; default ``uci.edu``) — a base domain also matches its subdomains.

CODE_TTL_MINUTES = 15
MAX_CODE_ATTEMPTS = 5
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def self_register_enabled() -> bool:
    return os.environ.get("AISCIENTIST_ALLOW_SELF_REGISTER", "1").strip().lower() not in ("0", "false", "no", "off")


def allowed_email_domains() -> list[str]:
    raw = os.environ.get("AISCIENTIST_ALLOWED_EMAIL_DOMAINS", "uci.edu")
    return [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]


def _email_domain_ok(email: str) -> bool:
    """True if the address is under an allowed domain — the base domain OR any subdomain
    of it (so ``uci.edu`` also admits ``ics.uci.edu``/``hs.uci.edu``)."""
    domain = email.rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith("." + d) for d in allowed_email_domains())


def _gen_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


class RegisterStartRequest(BaseModel):
    username: str
    email: str
    password: str


class RegisterVerifyRequest(BaseModel):
    email: str
    code: str


@router.get("/auth/config")
def auth_config() -> dict:
    """Public: whether self-registration is on, the allowed email domain(s), and whether
    email actually sends (``smtp``) or is in dev/log mode. The login UI reads this to show
    or hide the 'Create account' path and to explain dev mode."""
    return {
        "self_register": self_register_enabled(),
        "email_domains": allowed_email_domains(),
        "email_mode": email_send.email_mode(),
    }


@router.post("/auth/register/start")
def register_start(req: RegisterStartRequest) -> dict:
    # Convert any *unexpected* failure (e.g. a schema-drift ``OperationalError`` when the
    # ``pending_registrations`` table is missing) into a clean JSON 500 with a ``detail``.
    # A bare 500 returns plain text, which the UI can't parse — the user then sees only the
    # opaque "Could not start registration." fallback with no clue what went wrong.
    try:
        return _register_start(req)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a diagnosable error, never a bare 500
        print(f"[auth] register_start failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=(
            "Registration is temporarily unavailable (server error). Please try again "
            "shortly, or contact an admin if it persists.")) from exc


def _register_start(req: RegisterStartRequest) -> dict:
    if not self_register_enabled():
        raise HTTPException(status_code=403, detail="Self-registration is disabled on this server.")
    username = (req.username or "").strip()
    email = (req.email or "").strip().lower()
    if not _USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="Username must be 3–64 chars: letters, digits, . _ -")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if not _email_domain_ok(email):
        allowed = ", ".join("@" + d for d in allowed_email_domains())
        raise HTTPException(status_code=400, detail=f"Registration is limited to {allowed} email addresses.")

    code = _gen_code()
    expires = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=CODE_TTL_MINUTES)
    with session_scope() as s:
        if s.scalar(select(User).where(func.lower(User.username) == username.lower())):
            raise HTTPException(status_code=409, detail="That username is taken.")
        # Email is intentionally NOT unique: one person may hold several accounts (e.g. an
        # admin + a regular account) on the same UCI address. Login is by username only and
        # there is no email-based password reset, so a shared email carries no auth risk.
        # Upsert: drop any prior pending row for this email OR username, then insert fresh.
        # (Keying the delete on email keeps ONE in-flight signup per address, so two
        # same-email registrations must be done sequentially — verify one before the next.)
        for old in s.scalars(select(PendingRegistration).where(
                or_(PendingRegistration.email == email,
                    func.lower(PendingRegistration.username) == username.lower()))).all():
            s.delete(old)
        s.add(PendingRegistration(
            email=email, username=username, password_hash=auth.hash_password(req.password),
            code_hash=auth.hash_password(code), attempts=0, expires_at=expires))
        s.commit()

    sent = email_send.send_email(
        email, "Your AiScientist verification code",
        f"Welcome to AiScientist.\n\nYour verification code is: {code}\n\n"
        f"It expires in {CODE_TTL_MINUTES} minutes. If you didn't request this, ignore this email.")
    if not sent:
        # A configured relay failed — drop the pending row so a retry starts clean.
        with session_scope() as s:
            row = s.scalar(select(PendingRegistration).where(PendingRegistration.email == email))
            if row is not None:
                s.delete(row)
                s.commit()
        raise HTTPException(status_code=502, detail="Could not send the verification email. Try again later.")

    out = {"status": "code_sent", "email": email, "expires_in_minutes": CODE_TTL_MINUTES,
           "dev_mode": email_send.email_mode() == "dev"}
    # In dev mode (no SMTP) there is no inbox to check, so surface the code to the UI to
    # keep local testing end-to-end. Never exposed once a real relay is configured.
    if email_send.email_mode() == "dev":
        out["dev_code"] = code
    return out


@router.post("/auth/register/verify")
def register_verify(req: RegisterVerifyRequest, response: Response) -> dict:
    try:
        return _register_verify(req, response)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a diagnosable error, never a bare 500
        print(f"[auth] register_verify failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=(
            "Verification is temporarily unavailable (server error). Please try again "
            "shortly, or contact an admin if it persists.")) from exc


def _register_verify(req: RegisterVerifyRequest, response: Response) -> dict:
    if not self_register_enabled():
        raise HTTPException(status_code=403, detail="Self-registration is disabled on this server.")
    email = (req.email or "").strip().lower()
    code = (req.code or "").strip()
    now = _dt.datetime.now(_dt.timezone.utc)
    with session_scope() as s:
        pending = s.scalar(select(PendingRegistration).where(PendingRegistration.email == email)
                           .order_by(PendingRegistration.id.desc()))
        if pending is None:
            raise HTTPException(status_code=404, detail="No pending registration — start again.")
        expires = pending.expires_at
        if expires.tzinfo is None:   # SQLite may return naive datetimes
            expires = expires.replace(tzinfo=_dt.timezone.utc)
        if now > expires:
            s.delete(pending)
            s.commit()
            raise HTTPException(status_code=400, detail="Code expired — start again.")
        if pending.attempts >= MAX_CODE_ATTEMPTS:
            s.delete(pending)
            s.commit()
            raise HTTPException(status_code=429, detail="Too many attempts — start again.")
        if not auth.verify_password(code, pending.code_hash):
            pending.attempts += 1
            s.commit()
            left = MAX_CODE_ATTEMPTS - pending.attempts
            raise HTTPException(status_code=400, detail=f"Incorrect code. {max(left, 0)} attempt(s) left.")
        # Success — re-check the username (it could have been claimed meanwhile). Email is
        # deliberately not unique (see register/start), so no email re-check here.
        if s.scalar(select(User).where(func.lower(User.username) == pending.username.lower())):
            s.delete(pending)
            s.commit()
            raise HTTPException(status_code=409, detail="That username was just taken — start again.")
        user = User(username=pending.username, email=email, password_hash=pending.password_hash,
                    role="user", is_active=True)
        s.add(user)
        s.delete(pending)
        user.last_login_at = now
        s.commit()
        token = auth.make_session_token(user.id)
        info = user.public()
    response.set_cookie(
        auth.COOKIE_NAME, token, max_age=auth.SESSION_MAX_AGE,
        httponly=True, samesite="lax", secure=auth.secure_cookies())
    return {"status": "ok", "user": info}


# --- account: my history + change my password -------------------------------


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.post("/account/password")
def change_my_password(req: ChangePasswordRequest, request: Request) -> dict:
    user = require_user(request)
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="new password must be >= 6 chars")
    with session_scope() as s:
        u = s.get(User, user.id)
        if u is None or not auth.verify_password(req.old_password, u.password_hash):
            raise HTTPException(status_code=401, detail="current password is incorrect")
        u.password_hash = auth.hash_password(req.new_password)
        s.commit()
    return {"status": "ok"}


@router.get("/datasets")
def my_datasets(request: Request) -> dict:
    user = require_user(request)
    from .models import MetadataRecord
    with session_scope() as s:
        rows = s.scalars(select(Dataset).where(Dataset.user_id == user.id).order_by(Dataset.id.desc())).all()
        # Each dataset's Croissant metadata: reviewed or still a draft, and whether it validated.
        meta = {r.dataset_id: {"status": r.status, "valid": r.loads("validation").get("ok"),
                               "needs_review": r.status != "reviewed"}
                for r in s.scalars(select(MetadataRecord).where(MetadataRecord.user_id == user.id,
                                                                MetadataRecord.kind == "dataset")).all()}
        from .dataset_storage import location_of
        return {"datasets": [{**d.public(), "metadata": meta.get(d.id), "location": location_of(d.path)}
                             for d in rows]}


@router.get("/runs")
def my_runs(request: Request) -> dict:
    user = require_user(request)
    with session_scope() as s:
        rows = s.scalars(select(Run).where(Run.user_id == user.id).order_by(Run.id.desc())).all()
        # Each run's plan estimate and measured compute (issue 25), two queries for the whole list.
        from .estimates import runs_usage
        usage = runs_usage([r.run_id for r in rows])
        return {"runs": [{**r.public(), "compute_usage": usage[r.run_id]} for r in rows]}


# --- recording helpers (called by the gateway when a user is logged in) -------


def record_dataset(user_id: int, name: str, path: str, size_bytes: int, kind: str,
                   sha256: str | None = None, conversation_id: str | None = None,
                   dataset_uuid: str | None = None, *, peek: dict | None = None,
                   files: list[dict] | None = None, source: str = "upload",
                   derived_from: dict | None = None) -> int | None:
    """One dataset-history row. ``conversation_id`` is the chat the upload was started from;
    ``dataset_uuid`` the dataset's managed-storage id (read off the path when it lies in a UUID
    folder). A second record of the same UUID and path returns the first row.

    The row's Croissant metadata is drafted in the same call (RCHENLAB/AiScientist issue 9) from what
    the upload measured: ``sha256``, the content ``peek`` of a single file, the ``files`` of a folder
    (``[{"path" relative to the folder, "size", "sha256", "peek"}]``). ``source`` says where the row
    came from (``upload``, ``server`` for a bound server path, ``result`` for a saved result, with
    ``derived_from`` = ``{"uuid", "run_id"}`` of that result). Drafting never fails the recording."""
    with session_scope() as s:
        from .dataset_storage import managed_uuid
        metadata = {"uuid": dataset_uuid or managed_uuid(path)}
        metadata = {k: v for k, v in metadata.items() if v is not None}
        if metadata:
            existing = s.scalars(select(Dataset).where(Dataset.uuid == metadata["uuid"])).first()
            if existing is not None:
                if existing.user_id != user_id or existing.path != path:
                    raise ValueError("Dataset UUID already belongs to another storage reference")
                return existing.id
        d = Dataset(user_id=user_id, name=name, path=path, size_bytes=size_bytes, kind=kind, sha256=sha256,
                    conversation_id=conversation_id or None, **metadata)
        s.add(d)
        s.commit()
        dataset_id = d.id
    _draft_metadata(dataset_id, peek=peek, files=files, source=source, derived_from=derived_from)
    return dataset_id


def _draft_metadata(dataset_id: int, **kw) -> None:
    try:
        from . import metadata_store
        metadata_store.draft_dataset_record(dataset_id, **kw)
    except Exception as exc:  # noqa: BLE001 - metadata is drafted again by the scheduled re-check
        print(f"[metadata] draft for dataset {dataset_id} skipped: {type(exc).__name__}: {exc}")


def record_folder_dataset(user_id: int, name: str, path: str, size_bytes: int,
                          conversation_id: str | None = None,
                          dataset_uuid: str | None = None, *, files: list[dict] | None = None,
                          source: str = "upload") -> int | None:
    """One row per uploaded folder: files added to a folder that is already a dataset update its
    row's size instead of listing the folder a second time. The row keeps the chat that first
    uploaded the folder. Its metadata is (re-)drafted from ``files`` (see :func:`record_dataset`);
    files sent earlier keep the checksums recorded for them then."""
    with session_scope() as s:
        row = s.scalars(select(Dataset).where(Dataset.user_id == user_id, Dataset.path == path)
                        .order_by(Dataset.id.desc()).limit(1)).first()
        if row is None:
            from .dataset_storage import managed_uuid
            metadata = {"uuid": dataset_uuid or managed_uuid(path)}
            metadata = {k: v for k, v in metadata.items() if v is not None}
            row = Dataset(user_id=user_id, name=name, path=path, size_bytes=size_bytes, kind="folder",
                          conversation_id=conversation_id or None, **metadata)
            s.add(row)
        else:
            row.size_bytes = size_bytes
            if conversation_id and not row.conversation_id:
                row.conversation_id = conversation_id
        s.commit()
        dataset_id = row.id
    try:
        from . import metadata_store
        files = metadata_store.merge_folder_files(dataset_id, files)
    except Exception as exc:  # noqa: BLE001
        print(f"[metadata] folder file list for dataset {dataset_id} not merged: {exc}")
    _draft_metadata(dataset_id, files=files, source=source)
    return dataset_id


def dataset_path_recorded(user_id: int, path: str) -> bool:
    """True when this user already has a dataset row for ``path`` (binding a server folder twice
    must not list it twice)."""
    with session_scope() as s:
        return s.scalars(select(Dataset.id).where(Dataset.user_id == user_id,
                                                  Dataset.path == path).limit(1)).first() is not None


def get_dataset_record(user_id: int, dataset_id: int) -> dict | None:
    """One of this user's dataset rows as ``{id, name, path, kind, size_bytes}``; None when the row is
    missing or belongs to another account (an id guessed from the URL reads nothing)."""
    with session_scope() as s:
        row = s.get(Dataset, dataset_id)
        if row is None or row.user_id != user_id:
            return None
        return {"id": row.id, "name": row.name, "path": row.path, "kind": row.kind,
                "size_bytes": row.size_bytes, "uuid": row.uuid, "status": row.status}


def list_dataset_records(user_id: int) -> list[dict]:
    """This user's dataset rows, oldest first, as :func:`get_dataset_record` gives one."""
    with session_scope() as s:
        rows = s.scalars(select(Dataset).where(Dataset.user_id == user_id).order_by(Dataset.id)).all()
        return [{"id": r.id, "name": r.name, "path": r.path, "kind": r.kind, "size_bytes": r.size_bytes,
                 "uuid": r.uuid, "status": r.status} for r in rows]


def repoint_dataset(user_id: int, dataset_id: int, old_path: str, new_path: str,
                    size_bytes: int | None = None) -> bool:
    """Point one of this user's rows at the copy of its data in ``new_path`` (a dataset moved from
    HPC3 to the gateway host), only while it still names ``old_path``. True when it was changed."""
    with session_scope() as s:
        row = s.get(Dataset, dataset_id)
        if row is None or row.user_id != user_id or row.path != old_path:
            return False
        row.path = new_path
        if size_bytes is not None:
            row.size_bytes = size_bytes
        s.commit()
        return True


def delete_dataset_record(user_id: int, dataset_id: int, delete_files=None) -> str | None:
    """Remove ONE dataset-history row, but only if it belongs to ``user_id`` (so a user
    can never delete another account's dataset by guessing an id). Returns the stored
    server-side ``path`` so the caller can unlink the physical file, or ``None`` when the
    row is missing or owned by someone else."""
    with session_scope() as s:
        row = s.get(Dataset, dataset_id)
        if row is None or row.user_id != user_id:
            return None
        path = row.path
        from . import server_data as sd
        if delete_files is not None and sd.root_of(path, sd.server_data_roots()) is not None:
            delete_files(path)       # a bound server folder: only its session copy goes, see the caller
        elif delete_files is not None:
            from pathlib import Path
            from .dataset_storage import managed_tree
            target = managed_tree(path) or Path(path).resolve()
            for other in s.scalars(select(Dataset).where(Dataset.id != dataset_id)):
                if not other.path:            # "" resolves to the working directory: no overlap
                    continue
                other_path = Path(other.path).resolve()
                if (target == other_path or target.is_relative_to(other_path)
                        or other_path.is_relative_to(target)):
                    raise ValueError("Dataset storage overlaps another registered dataset")
            # A failed physical deletion leaves the registry entry available for retry.
            delete_files(path)
        # Its Croissant metadata goes in the same commit: no metadata outlives its dataset.
        from . import metadata_store
        metadata_store.delete_for_dataset(s, dataset_id)
        s.flush()
        s.delete(row)
        s.commit()
        return path


def get_run_record(user_id: int, run_id: str) -> dict | None:
    """One of this user's runs (:meth:`Run.public`); None when it is missing or another account's."""
    with session_scope() as s:
        row = s.scalars(select(Run).where(Run.run_id == run_id)).first()
        if row is None or row.user_id != user_id:
            return None
        return row.public()


def delete_run_record(user_id: int, run_id: str, run_dir=None, delete_files=None) -> dict | None:
    """Remove one of this user's runs from their history: ``delete_files(run_dir)`` first (a failure
    raises and keeps the row, so the delete can be retried), then the run's row and its result's
    Croissant metadata, in one commit. Returns the run as it was, or ``None`` when it is missing or
    another account's.

    Refused (``ValueError``) while a dataset is kept inside ``run_dir``: deleting the run would take
    that dataset's files with it. The run's model-call rows stay, so what the account spent is still
    counted."""
    with session_scope() as s:
        row = s.scalars(select(Run).where(Run.run_id == run_id)).first()
        if row is None or row.user_id != user_id:
            return None
        info = row.public()
        if run_dir is not None:
            from pathlib import Path
            target = Path(run_dir).resolve()
            for d in s.scalars(select(Dataset)):
                if d.path and Path(d.path).resolve().is_relative_to(target):
                    raise ValueError(f'The dataset "{d.name}" is kept inside this run\'s folder; '
                                     "delete it in Files first.")
            if delete_files is not None:
                delete_files(target)
        from . import metadata_store
        metadata_store.delete_for_run(s, user_id, run_id)
        s.flush()
        s.delete(row)
        s.commit()
        return info


def record_run_start(user_id: int, run_id: str, question: str, plan_mode: bool,
                     dataset_id: int | None = None, conversation_id: str | None = None) -> None:
    with session_scope() as s:
        s.add(Run(user_id=user_id, run_id=run_id, question=question, plan_mode=plan_mode,
                  dataset_id=dataset_id, conversation_id=conversation_id, status="running"))
        s.commit()


def _usage_split(usage: dict) -> tuple[int, int, int, int, int]:
    """(uncached input, 5-minute cache writes, 1-hour cache writes, cache reads, output) from either
    usage shape: Claude's (``input_tokens`` + ``cache_creation_input_tokens`` +
    ``cache_read_input_tokens``, mapped by :func:`anthropic_client._usage`) or OpenAI's
    (``prompt_tokens`` with ``prompt_tokens_details.cached_tokens`` inside it)."""
    details = usage.get("prompt_tokens_details") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    reads = int(usage.get("cache_read_input_tokens") or details.get("cached_tokens") or 0)
    writes = int(usage.get("cache_creation_input_tokens") or details.get("cache_write_tokens") or 0)
    writes_1h = min(writes, int(usage.get("cache_creation_1h_input_tokens") or 0))
    if usage.get("input_tokens") is not None:
        uncached = int(usage.get("input_tokens") or 0)
    else:
        uncached = max(0, prompt - reads - writes)
    return uncached, writes - writes_1h, writes_1h, reads, int(usage.get("completion_tokens") or 0)


def record_llm_call(*, run_id: str | None, user_id: int | None, role: str, model: str,
                    endpoint: str, remote: bool, usage: dict | None,
                    max_tokens: int | None, duration_ms: int,
                    conversation_id: str | None = None, step: str | None = None,
                    provider: str | None = None, error_type: str | None = None) -> None:
    """Persist what one model call cost, or why it failed. Best-effort, and deliberately so.

    Accounting must never be able to fail an analysis: a dropped row costs a line in a cost report,
    while a raised exception would kill a run mid-step. So every failure here is swallowed with a
    log line.

    Two money figures are kept apart: ``cost_usd`` is what the provider reported (OpenRouter returns
    it in ``usage``); ``est_cost_usd`` is priced from the configurable table in
    :mod:`llm_pricing` at the time of the call, and only for calls billed to an API key (``remote``).
    A call on the cluster's own GPU costs the user nothing and is recorded at 0.

    ``error_type`` marks a failed request (``status = "error"``): the cause the transport classified
    (auth, credit, rate, …), so a user can see why a run stopped. No message text is stored.
    """
    try:
        usage = usage or {}
        uncached, writes_5m, writes_1h, reads, completion = _usage_split(usage)
        prompt = int(usage.get("prompt_tokens") or (uncached + writes_5m + writes_1h + reads))
        total = int(usage.get("total_tokens") or (prompt + completion))
        cost = usage.get("cost")
        if cost is None:
            cost = (usage.get("cost_details") or {}).get("upstream_inference_cost")
        served = str(usage.get("model") or "") or model
        if not remote:
            estimate: float | None = 0.0
        else:
            from . import llm_pricing
            estimate = llm_pricing.estimate(served, input_tokens=uncached, output_tokens=completion,
                                            cache_write_tokens=writes_5m,
                                            cache_write_1h_tokens=writes_1h, cache_read_tokens=reads)
        with session_scope() as s:
            s.add(LlmCall(run_id=run_id, conversation_id=(conversation_id or None) and str(conversation_id)[:64],
                          step=(step or None) and str(step)[:160], user_id=user_id,
                          role=(role or "reason")[:24], model=(model or "")[:128],
                          endpoint=(endpoint or "")[:128], remote=bool(remote),
                          provider=((provider or ("vllm" if not remote else "openai")))[:16],
                          prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
                          input_tokens=uncached, cache_write_tokens=writes_5m + writes_1h,
                          cache_read_tokens=reads,
                          cost_usd=float(cost) if cost is not None else None,
                          est_cost_usd=estimate,
                          status="error" if error_type else "ok",
                          error_type=(error_type or None) and str(error_type)[:32],
                          max_tokens=max_tokens, duration_ms=max(0, int(duration_ms))))
            s.commit()
    except Exception as exc:  # noqa: BLE001 - accounting never breaks a run
        print(f"[llm-usage] call not recorded: {exc}")


def run_llm_cost(run_id: str) -> dict:
    """Totals for one run: calls, tokens, and USD where the provider reported it.

    ``cost_usd`` sums only the rows that carry a provider number, so ``priced_calls`` is what makes
    the total readable — 40 calls with 12 priced means the figure is a floor, not the bill.
    """
    with session_scope() as s:
        rows = s.scalars(select(LlmCall).where(LlmCall.run_id == run_id)).all()
    priced = [r for r in rows if r.cost_usd is not None]
    by_role: dict[str, dict] = {}
    for r in rows:
        slot = by_role.setdefault(r.role, {"calls": 0, "total_tokens": 0, "cost_usd": 0.0})
        slot["calls"] += 1
        slot["total_tokens"] += r.total_tokens
        slot["cost_usd"] += r.cost_usd or 0.0
    return {
        "run_id": run_id,
        "calls": len(rows),
        "priced_calls": len(priced),
        "prompt_tokens": sum(r.prompt_tokens for r in rows),
        "completion_tokens": sum(r.completion_tokens for r in rows),
        "total_tokens": sum(r.total_tokens for r in rows),
        "cost_usd": round(sum(r.cost_usd or 0.0 for r in priced), 6) if priced else None,
        "by_role": by_role,
    }


def latest_run_id_for_conversation(user_id: int, conversation_id: str,
                                   accept: Callable[[str], bool] | None = None) -> str | None:
    """The most recent COMPLETED run this user produced in a conversation (``done``/``incomplete`` —
    i.e. one that reached the report stage, not a cancelled/errored/running one). Used by the
    follow-up router to recognise a prior run across a gateway restart, when the in-memory last-run
    map is empty. ``accept`` keeps only the runs it returns True for (the gateway passes "has a run
    state": a literature answer is finished but is no study to continue); the latest of the last 20
    completed runs it accepts. None when the conversation has no such run."""
    if not conversation_id:
        return None
    with session_scope() as s:
        rows = s.scalars(
            select(Run.run_id)
            .where(Run.user_id == user_id,
                   Run.conversation_id == conversation_id,
                   Run.status.in_(("done", "incomplete")))
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(1 if accept is None else 20)).all()
    for run_id in rows:
        if accept is None or accept(run_id):
            return run_id
    return None


def record_run_finish(run_id: str, status: str, artifacts_path: str | None = None,
                      summary: str | None = None) -> None:
    import datetime as _dt2

    with session_scope() as s:
        run = s.scalar(select(Run).where(Run.run_id == run_id))
        if run is not None:
            run.status = status
            run.artifacts_path = artifacts_path
            run.summary = (summary or "")[:4000]
            run.finished_at = _dt2.datetime.now(_dt2.timezone.utc)
            s.commit()


def mark_interrupted_runs() -> int:
    """Close out runs left ``running`` by a process that is no longer alive. Returns the count.

    A run row is opened as ``running`` and closed by ``record_run_finish``. Nothing closes it when
    the gateway dies mid-run — a deploy restart, a crash, an OOM — so the row stays ``running``
    for ever. Twelve had accumulated since July, and they are not merely untidy: every "how many
    runs succeeded" number computed from this table counts them as still in flight, which is how a
    success rate gets quoted with a denominator nobody can reconcile.

    Safe to call ONLY from the server's own startup, and only because prod is a single stateful
    replica: a process that has just started cannot own a live run, so anything still marked
    running belongs to a process that is gone. Deliberately NOT in ``init_db`` — the admin CLI
    calls that too, and it can run while the gateway is up.

    ``interrupted`` rather than ``error``: nothing went wrong with the analysis, it was cut off,
    and the two deserve different rows when the question is "does this pipeline work".
    """
    import datetime as _dt3

    with session_scope() as s:
        stale = list(s.scalars(select(Run).where(Run.status == "running")))
        for run in stale:
            run.status = "interrupted"
            if run.finished_at is None:
                run.finished_at = _dt3.datetime.now(_dt3.timezone.utc)
            if not (run.summary or "").strip():
                run.summary = ("The gateway stopped while this run was executing (deploy, restart "
                               "or crash). Its artifacts on disk are whatever it had produced.")
        if stale:
            s.commit()
        return len(stale)


# --- chat history: conversations + messages ---------------------------------
# Server-side replacement for the old browser-localStorage chat store, so a user's
# chats survive a browser/device change. Text lives in the DB; figures/downloads stay
# on disk and are referenced by URL inside Message.meta (metadata-in-DB, blobs-on-disk).


class ConversationCreate(BaseModel):
    title: str | None = None


class ConversationPatch(BaseModel):
    title: str | None = None
    preset_key: str | None = None        # the selected research path (or "" to clear)
    context_prompt: str | None = None    # the user-edited methodology guidance (or "" to clear)


class MessageCreate(BaseModel):
    role: str
    content: str = ""
    kind: str = "text"
    meta: dict | None = None


def _owned_conversation(s, cid: int, user_id: int) -> Conversation:
    """Fetch a conversation, 404-ing if it is missing OR owned by someone else (so a
    user can never read/modify another user's chats by guessing an id)."""
    conv = s.get(Conversation, cid)
    if conv is None or conv.user_id != user_id:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


@router.get("/conversations")
def list_conversations(request: Request) -> dict:
    user = require_user(request)
    with session_scope() as s:
        rows = s.scalars(
            select(Conversation).where(Conversation.user_id == user.id)
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        ).all()
        return {"conversations": [c.public() for c in rows]}


@router.post("/conversations")
def create_conversation(req: ConversationCreate, request: Request) -> dict:
    user = require_user(request)
    title = ((req.title or "").strip() or "New chat")[:255]
    with session_scope() as s:
        conv = Conversation(user_id=user.id, title=title)
        s.add(conv)
        s.commit()
        return {"conversation": conv.public()}


@router.get("/conversations/{cid}")
def get_conversation(cid: int, request: Request) -> dict:
    user = require_user(request)
    with session_scope() as s:
        conv = _owned_conversation(s, cid, user.id)
        msgs = s.scalars(
            select(Message).where(Message.conversation_id == cid).order_by(Message.seq, Message.id)
        ).all()
        return {"conversation": conv.public(), "messages": [m.public() for m in msgs]}


@router.patch("/conversations/{cid}")
def update_conversation(cid: int, req: ConversationPatch, request: Request) -> dict:
    """Update a conversation's title and/or its research-path context (preset + the
    editable methodology prompt). Only the provided fields change; "" clears a field."""
    user = require_user(request)
    with session_scope() as s:
        conv = _owned_conversation(s, cid, user.id)
        if req.title is not None:
            conv.title = (req.title.strip() or "New chat")[:255]
        if req.preset_key is not None:
            conv.preset_key = (req.preset_key.strip()[:64] or None)
        if req.context_prompt is not None:
            conv.context_prompt = (req.context_prompt or None)
        s.commit()
        return {"conversation": conv.public()}


@router.delete("/conversations/{cid}")
def delete_conversation(cid: int, request: Request) -> dict:
    """Delete a chat AND everything it produced: its messages, its run records, and those runs'
    files on disk.

    The browser already asked the server to delete each run's artifacts before calling this, but it
    derived the run list from the chat's own MESSAGE METADATA — so a chat whose messages failed to
    load, or a run whose reference never made it into a message, left its bundle on disk forever
    with no owner and no way to reach it from the UI. The authoritative list is ``Run.conversation_id``,
    which only the server has. Deleting a chat now means the disk is clean too.
    """
    user = require_user(request)
    removed_dirs = 0
    with session_scope() as s:
        conv = _owned_conversation(s, cid, user.id)
        runs = list(s.scalars(
            select(Run).where(Run.user_id == user.id, Run.conversation_id == str(cid))).all())
        run_ids = [r.run_id for r in runs]
        for run in runs:
            s.delete(run)
        s.delete(conv)   # cascade removes the conversation's messages
        s.commit()
    for run_id in run_ids:
        if _delete_run_dir(user.username, run_id):
            removed_dirs += 1
    return {"status": "ok", "runs_deleted": len(run_ids), "dirs_removed": removed_dirs}


def _delete_run_dir(owner: str, run_id: str) -> bool:
    """Remove ``<results>/<owner>/<run_id>/`` — the report bundle a download would have zipped.

    Guarded twice: the components must be plain identifiers (no traversal), and the resolved path
    must stay under the results root.
    """
    import shutil
    from .app import CONSOLE_RUNS_DIR       # deferred: auth_routes is imported BY app

    if not all(part and part.replace("-", "").replace("_", "").isalnum() for part in (owner, run_id)):
        return False
    try:
        root = CONSOLE_RUNS_DIR.resolve()
        target = (root / owner / run_id).resolve()
        if not str(target).startswith(str(root)) or not target.is_dir():
            return False
        shutil.rmtree(target)
        return True
    except (OSError, ValueError):
        return False


@router.post("/conversations/{cid}/messages")
def add_message(cid: int, req: MessageCreate, request: Request) -> dict:
    user = require_user(request)
    if req.role not in ("user", "assistant", "system"):
        raise HTTPException(status_code=400, detail="role must be user|assistant|system")
    with session_scope() as s:
        conv = _owned_conversation(s, cid, user.id)
        # An answer is saved by every window that showed it: two windows of one account share a
        # session, and a reconnect replays a finished answer to a window that may not have seen it.
        # The answer's stream id makes the second save return the first instead of a duplicate.
        stream_id = (req.meta or {}).get("stream_id") if isinstance(req.meta, dict) else None
        if stream_id:
            recent = s.scalars(select(Message).where(Message.conversation_id == cid)
                               .order_by(Message.seq.desc()).limit(50)).all()
            for m in recent:
                try:
                    if (m.kind == (req.kind or "text") and m.meta
                            and _json.loads(m.meta).get("stream_id") == stream_id):
                        return {"message": m.public(), "duplicate": True}
                except (ValueError, AttributeError):
                    continue
        next_seq = (s.scalar(select(func.max(Message.seq)).where(Message.conversation_id == cid)) or 0) + 1
        msg = Message(
            conversation_id=cid, role=req.role, content=req.content or "",
            kind=(req.kind or "text"),
            meta=_json.dumps(req.meta) if req.meta is not None else None,
            seq=next_seq,
        )
        s.add(msg)
        conv.updated_at = _dt.datetime.now(_dt.timezone.utc)   # bump so the sidebar re-sorts to top
        s.commit()
        return {"message": msg.public()}


# --- admin: user management --------------------------------------------------


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"          # 'user' | 'admin'
    email: str | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str


@router.get("/admin/users")
def list_users(q: str | None = None, _admin: User = Depends(require_admin)) -> dict:
    """List users, optionally FUZZY-filtered by ``q`` — matched against email and username
    (case-insensitive substring) and, when ``q`` is all digits, an exact user id."""
    with session_scope() as s:
        stmt = select(User)
        term = (q or "").strip()
        if term:
            like = f"%{term.lower()}%"
            conds = [func.lower(User.email).like(like), func.lower(User.username).like(like)]
            if term.isdigit():
                conds.append(User.id == int(term))
            stmt = stmt.where(or_(*conds))
        users = s.scalars(stmt.order_by(User.id)).all()
        return {"users": [u.public() for u in users]}


def _store_owner(username: str) -> str:
    """The folder name the credential stores file an account under (``app.safe_name``)."""
    return "".join(c for c in (username or "") if c.isalnum() or c in "-_") or "guest"


@router.delete("/admin/users/{user_id}")
def delete_user(user_id: int, admin: User = Depends(require_admin)) -> dict:
    """Hard-delete a user entry: the account row + all its history (datasets / runs /
    conversations / messages cascade; LLM usage rows and the budget explicitly), the account's
    saved API keys and SSH keys, plus a best-effort ``rm -rf`` of the user's own results/uploads
    directory on disk. Guards: you can't delete your own account, and you can't remove the last
    remaining admin."""
    with session_scope() as s:
        user = s.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        if user.id == admin.id:
            raise HTTPException(status_code=400, detail="You can't delete your own account.")
        if user.role == "admin":
            admins = s.scalar(select(func.count()).select_from(User).where(User.role == "admin"))
            if (admins or 0) <= 1:
                raise HTTPException(status_code=400, detail="Can't delete the last admin.")
        username = user.username
        # The key stores are keyed by the sanitized name, so "a.b" and "ab" share one. Another
        # account on the same folder keeps it.
        owner = _store_owner(username)
        shared_store = any(_store_owner(u) == owner for u in
                           s.scalars(select(User.username).where(User.id != user_id)).all())
        # Not cascaded by the ORM (no relationship): without these the FK makes PostgreSQL refuse
        # the delete for any account that ever made a model call.
        s.execute(sa_delete(LlmCall).where(LlmCall.user_id == user_id))
        s.execute(sa_delete(UsageBudget).where(UsageBudget.user_id == user_id))
        s.delete(user)   # cascades datasets/runs/conversations/messages
        s.commit()
    # The account's saved keys go with it, at once: an API key left on disk after its owner is gone
    # could still be read by anyone who later signs up under the same name.
    from . import llm_credentials, ssh_credentials
    for store in (llm_credentials, ssh_credentials):
        if shared_store:
            print(f"[admin] user {user_id} deleted; {store.__name__} folder {owner!r} is shared "
                  "with another account and was kept")
            continue
        try:
            store.delete_owner(owner)
        except Exception as exc:  # noqa: BLE001 - reported; the account is already gone
            print(f"[admin] user {user_id} deleted; {store.__name__} cleanup failed: {exc}")
    # Best-effort disk cleanup, strictly confined to this user's OWN results subtree.
    # Deferred import (app imports auth_routes at load; the reverse only at call time).
    try:
        import shutil

        from .app import CONSOLE_RUNS_DIR, safe_name
        target = (CONSOLE_RUNS_DIR / safe_name(username)).resolve()
        if str(target).startswith(str(CONSOLE_RUNS_DIR)) and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 - the account is already gone; disk is best-effort
        print(f"[admin] user {user_id} deleted; disk cleanup skipped: {exc}")
    return {"status": "ok", "deleted": user_id, "username": username}


@router.post("/admin/users")
def create_user(req: CreateUserRequest, admin: User = Depends(require_admin)) -> dict:
    if req.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'admin'")
    if not req.username.strip() or len(req.password) < 6:
        raise HTTPException(status_code=400, detail="username required; password must be >= 6 chars")
    with session_scope() as s:
        if s.scalar(select(User).where(User.username == req.username)):
            raise HTTPException(status_code=409, detail="username already exists")
        user = User(
            username=req.username.strip(), email=req.email,
            password_hash=auth.hash_password(req.password), role=req.role,
            is_active=True, created_by=admin.id,
        )
        s.add(user)
        s.commit()
        return {"status": "ok", "user": user.public()}


@router.post("/admin/users/{user_id}/active")
def set_active(user_id: int, active: bool, _admin: User = Depends(require_admin)) -> dict:
    with session_scope() as s:
        user = s.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        user.is_active = active
        s.commit()
        return {"status": "ok", "user": user.public()}


@router.post("/admin/users/{user_id}/reset-password")
def reset_password(user_id: int, req: ResetPasswordRequest, _admin: User = Depends(require_admin)) -> dict:
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="password must be >= 6 chars")
    with session_scope() as s:
        user = s.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        user.password_hash = auth.hash_password(req.new_password)
        s.commit()
        return {"status": "ok"}


class SetEmailRequest(BaseModel):
    email: str = ""   # a valid address, or "" to clear it


@router.post("/admin/users/{user_id}/email")
def set_email(user_id: int, req: SetEmailRequest, _admin: User = Depends(require_admin)) -> dict:
    """Admin: set or clear a user's email address (manual injection / correction). Blank
    clears it; a non-blank value must be a valid address. Email is NOT unique — several
    accounts may share one address (see register/start) — so no cross-account collision check."""
    email = (req.email or "").strip().lower()
    if email and not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    with session_scope() as s:
        user = s.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        user.email = email or None
        s.commit()
        return {"status": "ok", "user": user.public()}


class SetRoleRequest(BaseModel):
    role: str          # 'user' | 'admin'


@router.post("/admin/users/{user_id}/role")
def set_role(user_id: int, req: SetRoleRequest, admin: User = Depends(require_admin)) -> dict:
    """Admin: promote a user to admin or demote an admin to user. Guards mirror delete_user:
    you can't change your OWN role (prevents accidental self-lockout — another admin can do
    it), and you can't demote the LAST remaining admin (the console would have no admin)."""
    role = (req.role or "").strip().lower()
    if role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'admin'")
    with session_scope() as s:
        user = s.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        if user.id == admin.id and role != user.role:
            raise HTTPException(status_code=400, detail="You can't change your own role.")
        if user.role == "admin" and role == "user":
            admins = s.scalar(select(func.count()).select_from(User).where(User.role == "admin"))
            if (admins or 0) <= 1:
                raise HTTPException(status_code=400, detail="Can't demote the last admin.")
        user.role = role
        s.commit()
        return {"status": "ok", "user": user.public()}


# --- admin: induced-skill review ----------------------------------------------
# An induced skill reaches the PI and the Scientist only once an admin approves it here
# (agents/skills.py, "review gate"). Curated skills are not listed: they are always on.


class SkillReviewRequest(BaseModel):
    status: str                 # 'approved' | 'retired' | 'pending'
    note: str = ""


@router.get("/admin/skills")
def list_skill_reviews(_admin: User = Depends(require_admin)) -> dict:
    """Admin: every induced skill with its review status and provenance, newest first."""
    from ..agents import skills as skills_mod
    rows = skills_mod.review_listing()
    counts = {st: sum(1 for r in rows if r["status"] == st) for st in skills_mod.REVIEW_STATUSES}
    return {"skills": rows, "counts": counts,
            "curated": sum(1 for s in list(skills_mod.ALL_SKILLS.values()) if not s.induced),
            "induced_dir_configured": skills_mod._induced_dir() is not None}


@router.get("/admin/skills/{name}")
def skill_review_detail(name: str, _admin: User = Depends(require_admin)) -> dict:
    """Admin: one induced skill in full — its SKILL.md and bundled files — to judge it by."""
    from ..agents.skills import review_detail
    detail = review_detail(name)
    if detail is None:
        raise HTTPException(status_code=404, detail="no induced skill by that name")
    return detail


@router.post("/admin/skills/{name}/review")
def review_skill(name: str, req: SkillReviewRequest, admin: User = Depends(require_admin)) -> dict:
    """Admin: approve (the models may use it), retire (kept on disk, never offered) or reset to
    pending. Applies to the next step of every run — no restart."""
    from ..agents.skills import set_review
    try:
        rec = set_review(name, (req.status or "").strip().lower(), by=admin.username, note=req.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="no induced skill by that name") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not record the review: {exc}") from exc
    return {"status": "ok", "name": name, "review": rec}


# --- account helpers (shared by the admin CLI; no plaintext ever on disk) -----


def create_admin_account(username: str, password: str, email: str | None = None) -> tuple[str, bool]:
    """Create an admin, or promote+repassword an existing user. Returns (username, created).
    The password is hashed immediately; only the bcrypt hash is persisted."""
    with session_scope() as s:
        user = s.scalar(select(User).where(User.username == username))
        created = user is None
        if user is None:
            user = User(username=username, email=email, role="admin", is_active=True,
                        password_hash=auth.hash_password(password))
            s.add(user)
        else:
            user.role = "admin"
            user.is_active = True
            if password:
                user.password_hash = auth.hash_password(password)
        s.commit()
    return username, created


def set_user_password(username: str, password: str) -> bool:
    """Reset a user's password by username (admin CLI). Returns False if not found."""
    with session_scope() as s:
        user = s.scalar(select(User).where(User.username == username))
        if user is None:
            return False
        user.password_hash = auth.hash_password(password)
        s.commit()
    return True


def all_users() -> list[dict]:
    with session_scope() as s:
        return [u.public() for u in s.scalars(select(User).order_by(User.id)).all()]


# --- bootstrap ---------------------------------------------------------------


def ensure_bootstrap_admin() -> str | None:
    """Seed the first admin from env if that account doesn't exist yet. Idempotent.

    Prefers ``AISCIENTIST_ADMIN_PASSWORD_HASH`` (a bcrypt hash — keeps plaintext off disk).
    Falls back to ``AISCIENTIST_ADMIN_PASSWORD`` (plaintext) but then prints a warning to
    delete that line, since the hash now lives in the DB. Returns the username, else None.
    """
    import os

    username = os.environ.get("AISCIENTIST_ADMIN_USER")
    pw_hash = os.environ.get("AISCIENTIST_ADMIN_PASSWORD_HASH")
    pw_plain = os.environ.get("AISCIENTIST_ADMIN_PASSWORD")
    if not username or not (pw_hash or pw_plain):
        return None
    with session_scope() as s:
        if s.scalar(select(User).where(User.username == username)):
            return None
        s.add(User(username=username, role="admin", is_active=True,
                   password_hash=pw_hash or auth.hash_password(pw_plain)))
        s.commit()
    if pw_plain and not pw_hash:
        print(f"[auth] SECURITY: admin '{username}' was seeded from AISCIENTIST_ADMIN_PASSWORD "
              "(plaintext in .env). The bcrypt hash is now in the DB — DELETE that line from "
              ".env. Prefer `aiscientist-admin create-admin` (interactive) or AISCIENTIST_ADMIN_PASSWORD_HASH.")
    return username


class ReconcileDatasetsRequest(BaseModel):
    cleanup: bool = False
    all_owners: bool = False


@router.post("/datasets/reconcile")
def reconcile_datasets(req: ReconcileDatasetsRequest, request: Request) -> dict:
    user = require_user(request)
    if req.all_owners and user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    from .dataset_storage import reconcile
    return reconcile(user_id=None if req.all_owners else user.id, cleanup=req.cleanup)


class MigrateDatasetRequest(BaseModel):
    dataset_id: int
    apply: bool = False


@router.post("/datasets/migrate")
def migrate_legacy_dataset(req: MigrateDatasetRequest, request: Request) -> dict:
    user = require_user(request)
    from .dataset_storage import migrate_dataset
    from .errors import public_error
    try:
        return migrate_dataset(user.id, req.dataset_id, apply=req.apply)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=public_error(exc)) from exc
