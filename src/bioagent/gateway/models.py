"""ORM models: users, datasets, runs.

Design (locked with the user):
- **Admin-created accounts only** — there is no self-registration; an ``admin`` creates
  users and resets forgotten passwords. ``role`` ∈ {``user``, ``admin``}; ``is_active``
  lets an admin disable an account without deleting its history.
- **Two-layer identity** — this is the AiScientist app account (who you are, your data +
  history). HPC3 cluster credentials are NEVER stored here; the SSH/Duo login stays
  per-session. Only a bcrypt hash of the *app* password lives in ``users.password_hash``.
- **Big files stay on disk** — ``datasets``/``runs`` store METADATA + server-side paths
  (the per-user workspace), not the matrices/artifacts themselves.
- **Chat history is server-side** — a ``Conversation`` is a UI chat (many turns); a
  ``Message`` is one turn. Text lives in the DB; images/downloads stay on disk and are
  referenced by URL/path inside ``Message.meta`` (the same metadata-in-DB / blobs-on-disk
  split as ``runs``/``datasets``). This is what makes history survive a browser/device
  change instead of living only in the browser's localStorage.
"""

from __future__ import annotations

import datetime as _dt
import json as _json

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")   # 'user' | 'admin'
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    last_login_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    datasets: Mapped[list["Dataset"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", foreign_keys="Dataset.user_id")
    runs: Mapped[list["Run"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", foreign_keys="Run.user_id")
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", foreign_keys="Conversation.user_id")

    def public(self) -> dict:
        """Safe dict for API responses — NEVER includes the password hash."""
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


class PendingRegistration(Base):
    """A self-service signup awaiting email-code verification.

    Self-registration is gated: a prospective user submits a username + a UCI email +
    password; we hold the (already-bcrypt-hashed) password here with a hashed 6-digit
    code emailed to them, and only mint a real :class:`User` once they enter the code
    (before it expires). No plaintext password or code is ever stored. One row per email
    — a restart of the flow replaces the previous pending row for that email/username.
    """

    __tablename__ = "pending_registrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    code_hash: Mapped[str] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, default=0)   # wrong-code tries so far
    expires_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(1024))       # server path under the user workspace
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    kind: Mapped[str] = mapped_column(String(32), default="other")
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner: Mapped[User] = relationship(back_populates="datasets", foreign_keys=[user_id])

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "path": self.path, "size_bytes": self.size_bytes,
            "kind": self.kind, "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
        }


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # The chat/conversation (UI thread) that launched this run. A run belongs to ONE
    # conversation, so a fresh window/thread never inherits another's "last run" as a
    # replan target. Nullable + string (the client's conversation id, == the server
    # Conversation id when logged in) so it works for anonymous sessions too. Older rows
    # predate this column → NULL.
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    question: Mapped[str] = mapped_column(Text)
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("datasets.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|cancelled|error
    plan_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    artifacts_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped[User] = relationship(back_populates="runs", foreign_keys=[user_id])

    def public(self) -> dict:
        return {
            "id": self.id, "run_id": self.run_id, "question": self.question, "status": self.status,
            "plan_mode": self.plan_mode, "dataset_id": self.dataset_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class Conversation(Base):
    """A chat thread in the UI sidebar — many turns, owned by one user.

    Replaces the browser-localStorage session store with server-side history, so a
    user's chats survive a browser/device change and are scoped to their account.
    """

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="New chat")
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # The pre-selected research path + its (user-edited) guidance prompt for this chat.
    # This is conversation-level context the PI's planning is steered by — persisted so
    # it survives a reload/device change, like the messages themselves.
    preset_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    owner: Mapped[User] = relationship(back_populates="conversations", foreign_keys=[user_id])
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan",
        order_by="Message.seq", foreign_keys="Message.conversation_id")

    def public(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "preset_key": self.preset_key, "context_prompt": self.context_prompt,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Message(Base):
    """One turn in a conversation. ``content`` is the text; ``meta`` is a JSON blob of
    references to on-disk artifacts (e.g. ``{"items": [...], "bundleUrl": "..."}``) — the
    files themselves stay on disk under the run workspace, never in the DB."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))            # 'user' | 'assistant' | 'system'
    content: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(32), default="text")   # 'text' | 'artifacts' | ...
    meta: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON string, or None
    seq: Mapped[int] = mapped_column(Integer, default=0)    # ordering within the conversation
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[Conversation] = relationship(
        back_populates="messages", foreign_keys=[conversation_id])

    def public(self) -> dict:
        meta = None
        if self.meta:
            try:
                meta = _json.loads(self.meta)
            except (ValueError, TypeError):
                meta = None
        return {
            "id": self.id, "role": self.role, "content": self.content, "kind": self.kind,
            "meta": meta, "seq": self.seq,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
