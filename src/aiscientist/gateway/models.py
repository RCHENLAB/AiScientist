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
import uuid as _uuid

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String,
                        Text, func)
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
    # The dataset's stable identifier: its managed-storage folder (issue 5), its Croissant ``@id``
    # (``urn:uuid:<uuid>``) and the name agents and results use for it (issue 9). The integer ``id``
    # and the ``path`` are internal. Rows older than the column get one at start (``db.init_db``).
    uuid: Mapped[str] = mapped_column(String(36), default=lambda: str(_uuid.uuid4()), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="complete")
    # default as well as server_default: a database that gained this column by ALTER TABLE has no
    # server default for it
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now())
    name: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(1024))       # server path under the user workspace
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    kind: Mapped[str] = mapped_column(String(32), default="other")
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The chat the upload was started from (the console's conversation id), so the Datasets page can
    # say where each upload came from. NULL for data no chat started and for rows older than this.
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    owner: Mapped[User] = relationship(back_populates="datasets", foreign_keys=[user_id])

    chat_attachments: Mapped[list["ConversationDataset"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan")

    def public(self) -> dict:
        return {
            "id": self.id, "uuid": self.uuid, "owner": self.user_id,
            "name": self.name, "original_name": self.name, "path": self.path,
            "storage_path": self.path, "size_bytes": self.size_bytes, "status": self.status,
            "created_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "kind": self.kind, "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
            "conversation_id": self.conversation_id,
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

    datasets: Mapped[list["ConversationDataset"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan",
        order_by="ConversationDataset.position")

    def public(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "preset_key": self.preset_key, "context_prompt": self.context_prompt,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ConversationDataset(Base):
    """A saved dataset attached to a chat, with its order and user-selected role."""

    __tablename__ = "conversation_datasets"

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True, index=True)
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="datasets")
    dataset: Mapped[Dataset] = relationship(back_populates="chat_attachments")

    def public(self) -> dict:
        return {**self.dataset.public(), "role": self.role}


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


class LlmCall(Base):
    """One LLM completion, with what the provider said it cost.

    Every endpoint returns a ``usage`` block — prompt/completion/total tokens, and on OpenRouter a
    ``cost`` in USD. ``vllm_client.complete`` read the response body for its ``content`` and threw
    the rest away, so a run's spend existed NOWHERE: not in a log, not on the ``Run`` row, not in
    this database. The only instrument was the monthly bill, and a bill cannot tell you WHICH call
    was expensive — which is the only question worth asking when a run costs more than it should.

    One row per call, failed calls included (``status`` / ``error_type``), so a user can see why a
    run stopped. Written best-effort: accounting must never be able to fail a run, so the recorder
    swallows its own errors and a lost row is preferable to a broken analysis.
    """

    __tablename__ = "llm_calls"
    # The spike check reads one key's last hour (key_guard); revision 0002 adds it to a migrated database.
    __table_args__ = (Index("ix_llm_calls_key_fp_created", "key_fp", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    #: The chat the call was made for (the console's conversation id), research run or fast chat.
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: The pipeline step the call belongs to ("Planning", "Step 3: …", "Write-up"); NULL outside a run.
    step: Mapped[str | None] = mapped_column(String(160), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    #: Which half of the lab this was — the cost profile differs by an order of magnitude.
    role: Mapped[str] = mapped_column(String(24), default="reason")   # reason | writer | scientist
    model: Mapped[str] = mapped_column(String(128), default="")
    endpoint: Mapped[str] = mapped_column(String(128), default="")
    #: True when the prompt left the cluster — i.e. when the call was BILLED to the user's key.
    remote: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Which wire protocol answered: "anthropic" (a user's Claude key), "openai" (any other API
    #: key), "vllm" (the cluster GPU or the lab's shared server — not billed to the user).
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: All input the request carried (uncached + cache reads + cache writes), and the output.
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    #: The split of ``prompt_tokens`` the provider bills at different rates (Claude: ``input_tokens``,
    #: ``cache_creation_input_tokens``, ``cache_read_input_tokens``). NULL on rows older than it.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Provider-reported USD, when the endpoint returns one (OpenRouter does). NULL otherwise.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: USD from the configurable price table (``llm_pricing``), priced when the row was written.
    #: An ESTIMATE: the provider's console is the bill. NULL for a model the table does not list.
    est_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: "ok", or "error" with the cause the transport classified (auth | credit | rate | model |
    #: endpoint | network | context | refusal …). Never the provider's message, which can echo input.
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The ``max_tokens`` this call actually SENT (NULL = none sent, i.e. uncapped). Recorded so a
    #: bill can be read against the ceiling that was in force, not the one currently configured.
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    #: The fingerprint of the API key that paid (``llm_credentials._fingerprint``: a hash prefix,
    #: never the key), so a spike can be seen per KEY (``key_guard``). NULL off a user's key.
    key_fp: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def spend_usd(self) -> float | None:
        """What the call cost as best we know: the provider's figure, else the table estimate."""
        return self.cost_usd if self.cost_usd is not None else self.est_cost_usd

    def public(self) -> dict:
        return {
            "id": self.id, "run_id": self.run_id, "conversation_id": self.conversation_id,
            "step": self.step, "role": self.role, "model": self.model,
            "endpoint": self.endpoint, "remote": self.remote, "provider": self.provider,
            "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens, "input_tokens": self.input_tokens,
            "cache_write_tokens": self.cache_write_tokens, "cache_read_tokens": self.cache_read_tokens,
            "cost_usd": self.cost_usd, "est_cost_usd": self.est_cost_usd,
            "status": self.status or "ok", "error_type": self.error_type,
            "max_tokens": self.max_tokens, "duration_ms": self.duration_ms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class MetadataRecord(Base):
    """The Croissant metadata of one dataset or one analysis result (RCHENLAB/AiScientist issue 9).

    ``document`` is the ``croissant.json`` itself. It is never edited by hand: it is rebuilt by
    :mod:`aiscientist.metadata` from ``facts`` (what was measured: files, sizes, checksums, table
    columns, the run's record) and ``user_fields`` (what the owner entered in the review form), so
    the two sources stay apart and a review never loses a checksum. ``provenance`` and ``confidence``
    say, per drafted field, where its value came from and how sure the drafting was (the
    provenance.json / confidence.json pair of the Boulangerie catalog).

    ``uuid`` is the subject's identifier: ``Dataset.uuid`` for a dataset, the run's result UUID for a
    result. Lineage edges live in :class:`MetadataLink`. Every query filters on ``user_id``.
    """

    __tablename__ = "metadata_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="dataset")     # 'dataset' | 'result'
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("datasets.id"), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    document: Mapped[str] = mapped_column(Text, default="{}")
    facts: Mapped[str] = mapped_column(Text, default="{}")
    user_fields: Mapped[str] = mapped_column(Text, default="{}")
    provenance: Mapped[str] = mapped_column(Text, default="{}")
    confidence: Mapped[str] = mapped_column(Text, default="{}")
    #: Lower-cased text the search tools match against (name, description, organism, assay, ...).
    search_text: Mapped[str] = mapped_column(Text, default="")
    #: {"organism": [...], "tissue": [...], "assay": [...], "accession": [...], "format": [...]}:
    #: labels and ontology IDs, for the search filters and the dataset list.
    facets: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="draft")    # 'draft' | 'reviewed'
    #: The dataset's DATASET.md (issue 10), as the record it is built from: the run-start profile the
    #: PI read, its statements, who wrote them and when, and the owner's edit of the Markdown, which
    #: a later run never overwrites. NULL until a run has read the dataset. Both DATASET.md and the
    #: croissant.json are built from it (:mod:`aiscientist.metadata.description`).
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The last validation: {"ok", "errors", "warnings", "validator", "checked_at"}.
    validation: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def loads(self, column: str) -> "dict | list":
        """A JSON column, parsed: a dict (``provenance`` is a list), or ``{}`` when unreadable."""
        try:
            value = _json.loads(getattr(self, column) or "{}")
        except (TypeError, ValueError):
            return [] if column == "provenance" else {}
        return value if isinstance(value, (dict, list)) else {}


class MetadataLink(Base):
    """One lineage edge: ``parent_uuid`` was used to make ``child_uuid``. ``relation`` is ``input``
    (a dataset or earlier result a run read) or ``saved_from`` (a dataset saved from a run's result).
    Both directions are one indexed lookup: "which results used this dataset?" and "what was this
    result made from?"."""

    __tablename__ = "metadata_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    parent_uuid: Mapped[str] = mapped_column(String(36), index=True)
    child_uuid: Mapped[str] = mapped_column(String(36), index=True)
    relation: Mapped[str] = mapped_column(String(16), default="input")
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccessToken(Base):
    """A personal token an account holder creates so an MCP client they choose (Claude Code, an IDE,
    a script) can read their datasets' metadata. Only a SHA-256 of the token is stored; the token is
    shown once. A token acts as its owner with the owner's permissions and nothing more, and stops
    working when revoked or when the account is disabled."""

    __tablename__ = "access_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16), default="")     # shown in the list to tell tokens apart
    scope: Mapped[str] = mapped_column(String(32), default="mcp:read")
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "prefix": self.prefix, "scope": self.scope,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "revoked": self.revoked_at is not None,
        }


class UsageBudget(Base):
    """A user's monthly budget for their own API key, as they entered it on the usage page.

    ``monthly_credit_usd`` is the credit their plan includes (for example $100 for Max 5x) and
    ``reset_day`` the day of the month the billing cycle restarts. ``thresholds`` are the percentages
    at which the console shows a notice; ``notified`` records which of them were already shown in
    the cycle that started on ``notified_cycle``. ``hard_limit_usd`` with ``hard_limit_mode``
    ("confirm" | "block") gates new runs on a metered endpoint once the cycle's estimated spend
    reaches it. Every figure is compared with AiScientist's ESTIMATE, never with the provider's bill.
    """

    __tablename__ = "usage_budgets"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    monthly_credit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    reset_day: Mapped[int] = mapped_column(Integer, default=1)
    thresholds: Mapped[str] = mapped_column(String(64), default="50,80,100")
    hard_limit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    hard_limit_mode: Mapped[str] = mapped_column(String(16), default="confirm")
    notified_cycle: Mapped[str | None] = mapped_column(String(10), nullable=True)
    notified: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def threshold_list(self) -> list[int]:
        out = []
        for part in (self.thresholds or "").split(","):
            try:
                v = int(float(part))
            except ValueError:
                continue
            if 0 < v <= 1000 and v not in out:
                out.append(v)
        return sorted(out)

    def public(self) -> dict:
        return {
            "monthly_credit_usd": self.monthly_credit_usd, "reset_day": self.reset_day,
            "thresholds": self.threshold_list(), "hard_limit_usd": self.hard_limit_usd,
            "hard_limit_mode": self.hard_limit_mode or "confirm",
        }


class ComputeUsage(Base):
    """Measured tool calls, including failed/cancelled calls; no dataset contents."""

    __tablename__ = "compute_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    skill: Mapped[str] = mapped_column(String(128), index=True)
    units: Mapped[float] = mapped_column(Float)
    seconds: Mapped[float] = mapped_column(Float)
    cpu_hours: Mapped[float] = mapped_column(Float)
    gpu_hours: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))
    success: Mapped[bool] = mapped_column(Boolean)


class ComputeEstimate(Base):
    __tablename__ = "compute_estimates"
    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
