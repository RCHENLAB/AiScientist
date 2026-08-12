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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from . import auth, email_send
from .db import session_scope
from .models import Conversation, Dataset, Message, PendingRegistration, Run, User

router = APIRouter(prefix="/api")


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
def logout(response: Response) -> dict:
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
# ``BIOAGENT_ALLOW_SELF_REGISTER=0``. Allowed email domains: ``BIOAGENT_ALLOWED_EMAIL_DOMAINS``
# (comma-separated; default ``uci.edu``) — a base domain also matches its subdomains.

CODE_TTL_MINUTES = 15
MAX_CODE_ATTEMPTS = 5
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def self_register_enabled() -> bool:
    return os.environ.get("BIOAGENT_ALLOW_SELF_REGISTER", "1").strip().lower() not in ("0", "false", "no", "off")


def allowed_email_domains() -> list[str]:
    raw = os.environ.get("BIOAGENT_ALLOWED_EMAIL_DOMAINS", "uci.edu")
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
    with session_scope() as s:
        rows = s.scalars(select(Dataset).where(Dataset.user_id == user.id).order_by(Dataset.id.desc())).all()
        return {"datasets": [d.public() for d in rows]}


@router.get("/runs")
def my_runs(request: Request) -> dict:
    user = require_user(request)
    with session_scope() as s:
        rows = s.scalars(select(Run).where(Run.user_id == user.id).order_by(Run.id.desc())).all()
        return {"runs": [r.public() for r in rows]}


# --- recording helpers (called by the gateway when a user is logged in) -------


def record_dataset(user_id: int, name: str, path: str, size_bytes: int, kind: str) -> int | None:
    with session_scope() as s:
        d = Dataset(user_id=user_id, name=name, path=path, size_bytes=size_bytes, kind=kind)
        s.add(d)
        s.commit()
        return d.id


def delete_dataset_record(user_id: int, dataset_id: int) -> str | None:
    """Remove ONE dataset-history row, but only if it belongs to ``user_id`` (so a user
    can never delete another account's dataset by guessing an id). Returns the stored
    server-side ``path`` so the caller can unlink the physical file, or ``None`` when the
    row is missing or owned by someone else."""
    with session_scope() as s:
        row = s.get(Dataset, dataset_id)
        if row is None or row.user_id != user_id:
            return None
        path = row.path
        s.delete(row)
        s.commit()
        return path


def record_run_start(user_id: int, run_id: str, question: str, plan_mode: bool,
                     dataset_id: int | None = None, conversation_id: str | None = None) -> None:
    with session_scope() as s:
        s.add(Run(user_id=user_id, run_id=run_id, question=question, plan_mode=plan_mode,
                  dataset_id=dataset_id, conversation_id=conversation_id, status="running"))
        s.commit()


def latest_run_id_for_conversation(user_id: int, conversation_id: str) -> str | None:
    """The most recent COMPLETED run this user produced in a conversation (``done``/``incomplete`` —
    i.e. one that reached the report stage, not a cancelled/errored/running one). Used by the
    follow-up router to recognise a prior run across a gateway restart, when the in-memory last-run
    map is empty. None when the conversation has no such run."""
    if not conversation_id:
        return None
    with session_scope() as s:
        return s.scalar(
            select(Run.run_id)
            .where(Run.user_id == user_id,
                   Run.conversation_id == conversation_id,
                   Run.status.in_(("done", "incomplete")))
            .order_by(Run.created_at.desc(), Run.id.desc()))


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
    user = require_user(request)
    with session_scope() as s:
        conv = _owned_conversation(s, cid, user.id)
        s.delete(conv)   # cascade removes the conversation's messages
        s.commit()
        return {"status": "ok"}


@router.post("/conversations/{cid}/messages")
def add_message(cid: int, req: MessageCreate, request: Request) -> dict:
    user = require_user(request)
    if req.role not in ("user", "assistant", "system"):
        raise HTTPException(status_code=400, detail="role must be user|assistant|system")
    with session_scope() as s:
        conv = _owned_conversation(s, cid, user.id)
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


@router.delete("/admin/users/{user_id}")
def delete_user(user_id: int, admin: User = Depends(require_admin)) -> dict:
    """Hard-delete a user entry: the account row + all its history (datasets / runs /
    conversations / messages cascade), plus a best-effort ``rm -rf`` of the user's own
    results/uploads directory on disk. Guards: you can't delete your own account, and you
    can't remove the last remaining admin."""
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
        s.delete(user)   # cascades datasets/runs/conversations/messages
        s.commit()
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

    Prefers ``BIOAGENT_ADMIN_PASSWORD_HASH`` (a bcrypt hash — keeps plaintext off disk).
    Falls back to ``BIOAGENT_ADMIN_PASSWORD`` (plaintext) but then prints a warning to
    delete that line, since the hash now lives in the DB. Returns the username, else None.
    """
    import os

    username = os.environ.get("BIOAGENT_ADMIN_USER")
    pw_hash = os.environ.get("BIOAGENT_ADMIN_PASSWORD_HASH")
    pw_plain = os.environ.get("BIOAGENT_ADMIN_PASSWORD")
    if not username or not (pw_hash or pw_plain):
        return None
    with session_scope() as s:
        if s.scalar(select(User).where(User.username == username)):
            return None
        s.add(User(username=username, role="admin", is_active=True,
                   password_hash=pw_hash or auth.hash_password(pw_plain)))
        s.commit()
    if pw_plain and not pw_hash:
        print(f"[auth] SECURITY: admin '{username}' was seeded from BIOAGENT_ADMIN_PASSWORD "
              "(plaintext in .env). The bcrypt hash is now in the DB — DELETE that line from "
              ".env. Prefer `bioagent-admin create-admin` (interactive) or BIOAGENT_ADMIN_PASSWORD_HASH.")
    return username
