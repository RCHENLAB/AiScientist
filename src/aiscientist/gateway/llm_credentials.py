"""Per-user LLM API credentials — bring-your-own-key for the research lab.

The user still supplies an HPC3 account (that is what runs the Slurm analysis jobs); this
module covers the SEPARATE question of which LLM answers, and on whose bill.

Storage deliberately mirrors :mod:`ssh_credentials` — ``<AISCIENTIST_STATE_DIR>/llm_creds/<owner>/``
with an ``index.json`` of public metadata plus one ``<id>.key`` file per credential at 0600.
One storage idiom for both secret types means one thing to audit, one thing to back up, and
one set of containment rules to get right, instead of two that drift.

**A credential id is stable; the key inside it is a rotatable field.** This is the whole point
of the shape. Everything downstream — the session's endpoint choice, a conversation's saved
preference, an in-flight run — references the ``id``, never the key. So when a user rotates a
leaked or expired key, nothing else has to be updated and no reference goes stale.

**Rotation verifies before it commits.** :func:`rotate_key` takes a ``verify`` callable and runs
it against the NEW key before a single byte is written; if it raises, the stored credential is
untouched. That ordering is what prevents the state users actually fear — old key already
discarded, new key not working, no way back. It is a parameter rather than a convention so a
caller cannot skip it by forgetting.

**Keys are encrypted at rest** (Fernet), unless ``AISCIENTIST_LLM_KEY_ENCRYPTION=0`` turns it off.
Encryption protects against FILE-level exposure — a backup, a stray ``cat``, a misplaced tarball of
the key store — but not against root on the server or a compromised gateway process, which must be
able to decrypt in order to use the key at all. The cost is a real failure mode: lose the master
key and every user re-enters their key. The ``encrypted`` flag is stored PER ROW, and a plaintext
row (written while encryption was off) is re-encrypted the next time it is read, and by
:func:`encrypt_existing` at gateway start.

The master key lives in its own 0600 file OUTSIDE the key store (``<state dir>/llm_key.master``, or
``AISCIENTIST_LLM_KEY_MASTER_FILE``), so a copy of ``llm_creds/`` alone decrypts nothing; it is never
in ``.env`` or in Git: the production ``.env`` on eyeserver is world-readable by design. A store
created by an older version kept the master at ``llm_creds/.master``; it is still used if present.

**A key can also be held without being stored** (:func:`create_session`): kept in this process's
memory for one browser session and never written to disk. **Any credential can be paused**
(:func:`set_paused`) by its owner, an admin or the spike detector; ``key_guard`` refuses a paused
key before every model call. **Every key can be revoked at once** (:func:`purge_all`, behind
``aiscientist-admin purge-llm-keys``): a marker file makes a running gateway refuse every key it
already read, then the store is emptied (RCHENLAB/AiScientist issue 16).

**The keys can live in a separate process** (``AISCIENTIST_KEY_SERVICE``, :mod:`key_client`,
:mod:`key_service`), run as its own system account: this module then keeps only the public metadata
(``index.json``), hands each new or rotated key to the service, and :func:`call_target` names the
service's proxy instead of returning the key. :func:`resolve_secret` refuses in that mode.

Nothing in this module opens a socket — verification lives in :mod:`llm_providers`, and the key
service is reached through :mod:`key_client`.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .errors import GatewayError


def _root() -> Path:
    return Path(os.environ.get("AISCIENTIST_STATE_DIR", ".")) / "llm_creds"


def _safe_owner(owner: str) -> str:
    cleaned = "".join(c for c in (owner or "") if c.isalnum() or c in "-_")
    return cleaned or "guest"


def _private_dir(d: Path) -> Path:
    """Create ``d`` and keep it 0700. A folder made with the default mode took the state dir's
    default ACL instead (group and other could list it, and read ``index.json``); the store was
    then closed only by the state dir's own permissions — one chmod away from open."""
    d.mkdir(parents=True, exist_ok=True)
    try:
        if stat.S_IMODE(d.stat().st_mode) != 0o700:
            d.chmod(0o700)
    except OSError:
        pass
    return d


def _owner_dir(owner: str) -> Path:
    _private_dir(_root())
    return _private_dir(_root() / _safe_owner(owner))


def _index_path(owner: str) -> Path:
    return _owner_dir(owner) / "index.json"


def _load_index(owner: str) -> list[dict]:
    p = _index_path(owner)
    if not p.exists():
        return []
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return rows if isinstance(rows, list) else []


def _save_index(owner: str, rows: list[dict]) -> None:
    path = _index_path(owner)
    _write_private(path, json.dumps(rows, indent=2).encode("utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically at 0600.

    Atomic because a half-written key file is indistinguishable from a wrong one, and the
    failure would surface much later as a confusing 401. The temp file is created 0600 BEFORE
    the secret goes into it, so the bytes are never briefly world-readable.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


# --- at-rest encryption --------------------------------------------------------

_warned_no_crypto = False


def _crypto_available() -> bool:
    try:
        import cryptography.fernet  # noqa: F401
    except ImportError:
        return False
    return True


def encryption_enabled() -> bool:
    """On unless ``AISCIENTIST_LLM_KEY_ENCRYPTION`` says off. Without the ``cryptography`` package
    (every gateway has it, through paramiko) keys are stored in plaintext, said once in the log and
    shown in the key dialog — never silently."""
    global _warned_no_crypto
    if os.environ.get("AISCIENTIST_LLM_KEY_ENCRYPTION", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    if not _crypto_available():
        if not _warned_no_crypto:
            print("[llm-credentials] the cryptography package is missing: API keys are stored "
                  "unencrypted (0600 files)")
            _warned_no_crypto = True
        return False
    return True


def _master_key_path() -> Path:
    raw = os.environ.get("AISCIENTIST_LLM_KEY_MASTER_FILE", "").strip()
    if raw:
        return Path(raw)
    legacy = _root() / ".master"
    if legacy.is_file():
        return legacy
    return Path(os.environ.get("AISCIENTIST_STATE_DIR", ".")) / "llm_key.master"


def private_paths() -> list[Path]:
    """The key store, its master key and the key service's token: files no tool may read
    (``tools.sdk.confine_read``)."""
    from . import key_client
    return [_root(), _master_key_path(), key_client.token_file()]


def _service():
    """The key service client when one is configured (``AISCIENTIST_KEY_SERVICE``), else None."""
    from . import key_client
    return key_client if key_client.enabled() else None


def _fernet():
    """The Fernet built from the master key, creating that key on first use.

    Import is deferred so a deployment that leaves encryption off never needs ``cryptography``
    loaded for this path (it is already a dependency via :mod:`ssh_credentials`, but the
    dependency direction should follow the feature being used).
    """
    from cryptography.fernet import Fernet

    path = _master_key_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(path, Fernet.generate_key())
    return Fernet(path.read_bytes().strip())


def _encode_secret(secret: str) -> tuple[bytes, bool]:
    """Serialize a key for disk. Returns ``(bytes, encrypted)`` so the caller records on the
    row which form was used — that per-row flag is what lets encryption be switched on later
    without rewriting or invalidating existing credentials."""
    if not encryption_enabled():
        return secret.encode("utf-8"), False
    return _fernet().encrypt(secret.encode("utf-8")), True


def _decode_secret(raw: bytes, encrypted: bool) -> str:
    if not encrypted:
        return raw.decode("utf-8").strip()
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(raw).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise GatewayError(
            "This saved API key could not be decrypted — the server's key-encryption master file "
            "is missing or was replaced. Re-enter the API key to store it again.",
            stage="llm_credential",
        ) from exc


# --- public metadata ---------------------------------------------------------


def _hint(secret: str) -> str:
    """The last 4 characters, so a user with several keys can tell them apart in the UI and match
    one to the Console's key list. Short keys degrade to a pure mask rather than leaking most of
    themselves."""
    if len(secret) <= 12:
        return "…" + secret[-2:] if len(secret) > 4 else "…"
    return f"…{secret[-4:]}"


def _shown_hint(stored: str | None) -> str | None:
    """Rows written before the hint was cut to the last 4 stored ``sk-ant…abcd``: show only the tail."""
    if not stored:
        return stored
    return "…" + stored.rsplit("…", 1)[-1][-4:] if "…" in stored else stored


def _fingerprint(secret: str) -> str:
    """Stable identity for a key WITHOUT storing it — enough to answer "is this the same key
    you already saved?" (a common rotation mistake) and to correlate a key across credentials."""
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _public(row: dict) -> dict:
    """Metadata safe to hand the UI. The key, its path, and its ciphertext never appear here —
    and no route returns anything but this."""
    return {
        "id": row["id"],
        "label": row.get("label") or row.get("provider") or "endpoint",
        "provider": row.get("provider"),
        "base_url": row.get("base_url"),
        "model": row.get("model"),
        "lab_model": row.get("lab_model"),
        "key_hint": _shown_hint(row.get("key_hint")),
        "key_fingerprint": row.get("key_fingerprint"),
        "encrypted": bool(row.get("encrypted")),
        "created_at": row.get("created_at"),
        "rotated_at": row.get("rotated_at"),
        "verified_at": row.get("verified_at"),
        "verified_model": row.get("verified_model"),
        "last_error": row.get("last_error"),
        # When the owner acknowledged that using this endpoint sends the reasoning payload
        # (dataset profile, findings, artifact digests) to a third party. Per CREDENTIAL, not
        # per user: consenting to your own self-hosted vLLM is a different decision from
        # consenting to a commercial API, and one must not stand in for the other.
        "egress_consent_at": row.get("egress_consent_at"),
        # A key held in the server's memory for one browser session, never written to disk.
        "session_only": bool(row.get("session_only")),
        "expires_at": _session_expiry(row) if row.get("session_only") else None,
        # Paused: no model call may use the key until it is resumed. ``paused_by`` is who paused it
        # ("owner", "admin" or "auto" — the spike detector), never an admin's name.
        "paused_at": row.get("paused_at"),
        "paused_by": _pauser_kind(row.get("paused_by")),
        "pause_reason": row.get("pause_reason"),
    }


def _pauser_kind(paused_by: str | None) -> str | None:
    if not paused_by:
        return None
    return "admin" if paused_by.startswith("admin") else paused_by


# --- session-only keys ----------------------------------------------------------------------------
#
# A key a user wants to use without the server keeping it: held in this process's memory only, for
# the browser session that entered it. It ends at logout, after ``AISCIENTIST_SESSION_KEY_IDLE_HOURS``
# (default 12) without use, when the gateway restarts, and when an admin revokes every key. Its id
# starts with "s", which a stored id (hex) never does, so every function below takes either kind.

_SESSION_PREFIX = "s"
_SESSION: dict[str, dict[str, dict]] = {}          # owner -> id -> row
_LOCK = threading.RLock()                          # the session map and every index.json rewrite


class _Secret:
    """A session key in memory. Its ``repr`` is a placeholder, so a row that reaches a log, a
    traceback's locals or ``json.dumps(default=str)`` by mistake does not carry the key."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<secret>"

    __str__ = __repr__


def is_session_id(cred_id: str | None) -> bool:
    return bool(cred_id) and str(cred_id).startswith(_SESSION_PREFIX)


def idle_hours() -> float:
    try:
        return max(0.1, float(os.environ.get("AISCIENTIST_SESSION_KEY_IDLE_HOURS", "12") or 12))
    except ValueError:
        return 12.0


def _session_expiry(row: dict) -> str:
    last = float(row.get("_last_used") or row.get("_entered") or time.time())
    return datetime.fromtimestamp(last + idle_hours() * 3600, timezone.utc).isoformat()


def _session_alive(row: dict, now: float) -> bool:
    if now - float(row.get("_last_used") or row.get("_entered") or 0) > idle_hours() * 3600:
        return False
    revoked = revoked_after()
    return revoked is None or float(row.get("_entered") or 0) > revoked


def _session_rows(owner: str) -> dict[str, dict]:
    """The owner's live session keys (expired and revoked ones are dropped here)."""
    now = time.time()
    with _LOCK:
        rows = _SESSION.get(_safe_owner(owner), {})
        for cred_id in [i for i, r in rows.items() if not _session_alive(r, now)]:
            rows.pop(cred_id, None)
        return rows


def create_session(owner: str, *, session_tag: str, provider: str, base_url: str, model: str,
                   api_key: str, label: str | None = None, lab_model: str | None = None,
                   verify: Callable[[str, str, str | None], Any] | None = None,
                   cred_id: str | None = None) -> dict:
    """Hold a key for one browser session without writing it anywhere. Same contract as
    :func:`create` (``verify`` runs first; nothing is kept when it raises). ``session_tag`` names
    the browser session (a hash of its login cookie) so logout can drop exactly its keys."""
    if not api_key or not api_key.strip():
        raise GatewayError("An API key is required.", stage="llm_credential")
    if not base_url or not base_url.strip():
        raise GatewayError("A base URL is required.", stage="llm_credential")
    api_key = api_key.strip()
    if verify is not None:
        verify(base_url, api_key, model)
    now = time.time()
    cred_id = cred_id if is_session_id(cred_id) else _SESSION_PREFIX + uuid4().hex[:11]
    svc = _service()
    if svc is not None:
        # The key service holds it, in its memory; this process keeps only the public row.
        svc.put(_safe_owner(owner), cred_id, api_key=api_key, base_url=base_url, provider=provider,
                session=True)
    row = {
        "id": cred_id,
        "label": label or provider or "endpoint",
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "lab_model": lab_model or None,
        "key_hint": _hint(api_key),
        "key_fingerprint": _fingerprint(api_key),
        "encrypted": False,
        "created_at": _utc_now(),
        "rotated_at": None,
        "verified_at": _utc_now() if verify is not None else None,
        "verified_model": model if verify is not None else None,
        "last_error": None,
        "session_only": True,
        "session_tag": session_tag or "",
        "_entered": now,
        "_last_used": now,
    }
    if svc is None:
        row["_secret"] = _Secret(api_key)
    with _LOCK:
        _SESSION.setdefault(_safe_owner(owner), {})[row["id"]] = row
    return _public(row)


def end_session(owner: str, session_tag: str) -> int:
    """Forget every session key the browser session ``session_tag`` entered (logout). Returns how
    many. A run that already started keeps the key it read until it ends."""
    if not session_tag:
        return 0
    with _LOCK:
        rows = _SESSION.get(_safe_owner(owner), {})
        gone = [i for i, r in rows.items() if r.get("session_tag") == session_tag]
        for cred_id in gone:
            rows.pop(cred_id, None)
    svc = _service()
    for cred_id in gone if svc is not None else []:
        try:
            svc.delete(_safe_owner(owner), cred_id)
        except GatewayError as exc:
            print(f"[llm-credentials] key service kept session key {cred_id} at logout: {exc}")
    return len(gone)


# --- store -------------------------------------------------------------------


def _find(rows: list[dict], cred_id: str) -> dict | None:
    return next((r for r in rows if r.get("id") == cred_id), None)


def _locate(owner: str, cred_id: str) -> tuple[dict | None, Callable[[], None]]:
    """``(row, save)`` for either kind of credential: a stored row and the function that rewrites
    its index, or a session row (kept in memory, so ``save`` does nothing). Callers hold ``_LOCK``."""
    if is_session_id(cred_id):
        return _session_rows(owner).get(cred_id), lambda: None
    rows = _load_index(owner)
    return _find(rows, cred_id), lambda: _save_index(owner, rows)


def _key_path(owner: str, cred_id: str) -> Path:
    return _owner_dir(owner) / f"{cred_id}.key"


def _key_file(owner: str, cred_id: str, row: dict) -> Path:
    """Where this credential's key actually is — canonical location first, recorded path second.

    A row stores the ``key_path`` it was written with, and that path does not survive the store
    moving. Rows created while ``AISCIENTIST_STATE_DIR`` was unset hold a path relative to the
    then-current working directory (``llm_creds/<owner>/<id>.key``); pointing the state dir
    somewhere else left every one of them naming a file that is no longer there, so the key sat
    intact on disk while every use of it failed — "outside the owner's store" for an LLM key, a
    missing file for an SSH one. The canonical path is derived from the CURRENT store, so it
    follows the move; the recorded path stays as a fallback for any layout that predates it, and
    is still confined to the owner's directory by the caller.
    """
    canonical = _key_path(owner, cred_id)
    if canonical.is_file():
        return canonical
    return Path(row.get("key_path") or canonical)


def create(owner: str, *, provider: str, base_url: str, model: str, api_key: str,
           label: str | None = None, lab_model: str | None = None,
           verify: Callable[[str, str, str | None], Any] | None = None,
           cred_id: str | None = None) -> dict:
    """Store a new credential. Returns PUBLIC metadata.

    ``verify`` — when given — is called as ``verify(base_url, api_key, model)`` before anything
    is written; anything it raises propagates and nothing is stored. Same contract as
    :func:`rotate_key`, so "a saved credential has been proven to work at least once" holds by
    construction rather than by the caller remembering.
    """
    if not api_key or not api_key.strip():
        raise GatewayError("An API key is required.", stage="llm_credential")
    if not base_url or not base_url.strip():
        raise GatewayError("A base URL is required.", stage="llm_credential")
    api_key = api_key.strip()

    if verify is not None:
        verify(base_url, api_key, model)

    cred_id = cred_id if (cred_id and all(c in "0123456789abcdef" for c in cred_id)) else uuid4().hex[:12]
    svc = _service()
    if svc is not None:
        svc.put(_safe_owner(owner), cred_id, api_key=api_key, base_url=base_url, provider=provider)
        key_path, encrypted = None, True          # the service encrypts it, in its own store
    else:
        blob, encrypted = _encode_secret(api_key)
        _write_private(_key_path(owner, cred_id), blob)
        key_path = str(_key_path(owner, cred_id))

    row = {
        "id": cred_id,
        "label": label or provider or "endpoint",
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "lab_model": lab_model or None,
        "key_path": key_path,
        "key_hint": _hint(api_key),
        "key_fingerprint": _fingerprint(api_key),
        "encrypted": encrypted,
        "created_at": _utc_now(),
        "rotated_at": None,
        "verified_at": _utc_now() if verify is not None else None,
        "verified_model": model if verify is not None else None,
        "last_error": None,
    }
    with _LOCK:
        rows = _load_index(owner)
        rows.insert(0, row)
        _save_index(owner, rows)
    return _public(row)


def rotate_key(owner: str, cred_id: str, api_key: str, *,
               verify: Callable[[str, str, str | None], Any] | None = None) -> dict:
    """Replace the SECRET of an existing credential, keeping its id and every reference to it.

    ``verify`` runs against the NEW key first. If it raises, this function has written nothing
    and the old key is still in place and still working — which is the entire reason rotation is
    modelled as an update to a stable id rather than delete-then-create.
    """
    if not api_key or not api_key.strip():
        raise GatewayError("An API key is required.", stage="llm_credential")
    api_key = api_key.strip()

    with _LOCK:
        row, _ = _locate(owner, cred_id)
    if row is None:
        raise GatewayError("No such API credential.", stage="llm_credential")

    if verify is not None:
        verify(row.get("base_url", ""), api_key, row.get("model"))

    with _LOCK:
        row, save = _locate(owner, cred_id)
        if row is None:
            raise GatewayError("No such API credential.", stage="llm_credential")
        svc = _service()
        if svc is not None:
            svc.put(_safe_owner(owner), cred_id, api_key=api_key, base_url=row.get("base_url") or "",
                    provider=row.get("provider"), session=bool(row.get("session_only")))
            if row.get("session_only"):
                row["_entered"] = row["_last_used"] = time.time()
        elif row.get("session_only"):
            row["_secret"] = _Secret(api_key)
            row["_entered"] = row["_last_used"] = time.time()
        else:
            blob, encrypted = _encode_secret(api_key)
            _write_private(_key_path(owner, cred_id), blob)   # atomic; the id and path are unchanged
            row["encrypted"] = encrypted
        row["key_hint"] = _hint(api_key)
        row["key_fingerprint"] = _fingerprint(api_key)
        row["rotated_at"] = _utc_now()
        row["last_error"] = None
        if verify is not None:
            row["verified_at"] = _utc_now()
            row["verified_model"] = row.get("model")
        save()
        return _public(row)


def update(owner: str, cred_id: str, *, label: str | None = None, model: str | None = None,
           lab_model: str | None = None, base_url: str | None = None,
           provider: str | None = None, key_follows: bool = False) -> dict:
    """Edit METADATA only — never the key. Changing ``model``, ``lab_model`` or ``base_url``
    invalidates the stored verification (the key was proven against the old endpoint and models,
    not these), so the row is marked unverified rather than carrying a stale green tick.

    With the key service, a key is sent only to the address (and in the protocol) it was saved
    for, so moving it to another ``base_url`` or ``provider`` needs the key again: ``key_follows``
    says the caller rotates it right after this edit (the PUT route does)."""
    with _LOCK:
        row, save = _locate(owner, cred_id)
        if row is None:
            raise GatewayError("No such API credential.", stage="llm_credential")
        moved = ((base_url is not None and base_url != row.get("base_url"))
                 or (provider is not None and provider != row.get("provider")))
        if moved and not key_follows and _service() is not None:
            raise GatewayError("To use this key at another address or with another provider, paste the "
                               "key again: a saved key is only ever sent where it was saved for.",
                               stage="llm_credential")

        endpoint_changed = False
        if label is not None:
            row["label"] = label
        if provider is not None and provider != row.get("provider"):
            row["provider"] = provider
        if model is not None and model != row.get("model"):
            row["model"] = model
            endpoint_changed = True
        if base_url is not None and base_url != row.get("base_url"):
            row["base_url"] = base_url
            endpoint_changed = True
        if lab_model is not None and (lab_model or None) != row.get("lab_model"):
            row["lab_model"] = lab_model or None
            endpoint_changed = True     # the PI/Critic model is tested at save too (see _verify_roles)

        if endpoint_changed:
            row["verified_at"] = None
            row["verified_model"] = None
        save()
        return _public(row)


def mark_verified(owner: str, cred_id: str, *, model: str | None, error: str | None = None) -> dict | None:
    """Record the outcome of a re-test so the UI can show which saved keys still work without
    re-testing them on every page load."""
    with _LOCK:
        row, save = _locate(owner, cred_id)
        if row is None:
            return None
        if error:
            row["last_error"] = error[:300]
        else:
            row["verified_at"] = _utc_now()
            row["verified_model"] = model
            row["last_error"] = None
        save()
        return _public(row)


def record_egress_consent(owner: str, cred_id: str) -> dict | None:
    """Record that the owner accepted sending the reasoning payload to THIS endpoint.

    Re-recorded rather than toggled, so the timestamp always reflects the most recent explicit
    acceptance. Deliberately not exposed as a way to REVOKE consent — a user who no longer wants
    an endpoint used deletes the credential, which also removes the key.
    """
    with _LOCK:
        row, save = _locate(owner, cred_id)
        if row is None:
            return None
        row["egress_consent_at"] = _utc_now()
        save()
        return _public(row)


def set_paused(owner: str, cred_id: str, paused: bool, *, by: str = "owner",
               reason: str = "") -> dict | None:
    """Pause or resume a credential. A paused key is refused before every model call
    (``key_guard``), including the next call of a run that is already using it.

    ``by`` is "owner", "auto" (the spike detector) or "admin:<name>". A pause an admin made is
    lifted only by an admin: an account whose key an admin stopped cannot restart it alone.
    Returns the public row, or None when there is no such credential."""
    with _LOCK:
        row, save = _locate(owner, cred_id)
        if row is None:
            return None
        if paused:
            if row.get("paused_at") and str(row.get("paused_by") or "").startswith("admin") \
                    and not by.startswith("admin"):
                return _public(row)          # already stopped by an admin: keep that record
            row["paused_at"] = _utc_now()
            row["paused_by"] = by
            row["pause_reason"] = (reason or "").strip()[:300] or None
        else:
            if str(row.get("paused_by") or "").startswith("admin") and not by.startswith("admin"):
                raise GatewayError("An administrator paused this key; only an administrator can "
                                   "resume it.", stage="llm_credential")
            row["paused_at"] = row["paused_by"] = row["pause_reason"] = None
        save()
        return _public(row)


def storage_info() -> dict:
    """How stored keys are protected, for the API endpoints dialog to state in plain words.

    Only the protection, never the location: the file path, its owner and its mode tell a user
    nothing they can act on and show everyone who opens the dialog how the server is laid out
    (RCHENLAB/AiScientist issue 37). The store's layout is in this module's docstring.
    """
    if _service() is not None:
        return {"encrypted": True, "separate_service": True}
    return {"encrypted": encryption_enabled()}


def list_credentials(owner: str, session_tag: str | None = None) -> list[dict]:
    """The owner's stored credentials, then the session keys of the browser session
    ``session_tag`` (none when it is not given)."""
    stored = [_public(r) for r in _load_index(owner)]
    if session_tag is None:
        return stored
    with _LOCK:
        mine = [_public(r) for r in _session_rows(owner).values()
                if r.get("session_tag") == session_tag]
    return mine + stored


def owner_credentials(owner: str) -> list[dict]:
    """Every credential of ``owner``, saved and session-only (whatever browser session entered it)."""
    with _LOCK:
        mine = [_public(r) for r in _session_rows(owner).values()]
    return mine + [_public(r) for r in _load_index(owner)]


def get_credential(owner: str, cred_id: str) -> dict | None:
    """The full row (base_url, model, key_path, …) for the LLM-binding path — still without the
    secret itself. Confined to ``owner``: one user's id never resolves against another's store."""
    if is_session_id(cred_id):
        with _LOCK:
            row = _session_rows(owner).get(cred_id)
            return {k: v for k, v in row.items() if not k.startswith("_")} if row else None
    return _find(_load_index(owner), cred_id)


def get_public(owner: str, cred_id: str) -> dict | None:
    """The masked view of one credential — what a route may hand back to the browser."""
    if is_session_id(cred_id):
        with _LOCK:
            row = _session_rows(owner).get(cred_id)
            return _public(row) if row else None
    row = get_credential(owner, cred_id)
    return _public(row) if row else None


def resolve_secret(owner: str, cred_id: str) -> str | None:
    """The plaintext API key. **The only function in the codebase that returns one.**

    Callers must treat the result the way they treat an SSH private key: pass it straight to the
    HTTP layer, never log it, never put it in an emitted event, never store it on the connection
    in a form that a status payload could serialize.

    With the key service this process holds no key: it refuses, and :func:`call_target` names the
    service's proxy instead.
    """
    if _service() is not None:
        raise GatewayError("API keys are held by the key service on this server; this process "
                           "cannot read them.", stage="llm_key_service")
    if is_session_id(cred_id):
        with _LOCK:
            row = _session_rows(owner).get(cred_id)
            if row is None:
                return None
            row["_last_used"] = time.time()
            return row["_secret"].get()
    row = get_credential(owner, cred_id)
    if row is None:
        return None
    path = _key_file(owner, cred_id, row)
    try:
        # Containment: a hand-edited index.json must not be able to point key_path at an
        # arbitrary file (/etc/shadow, another owner's key) and have us read it back out.
        if not path.resolve().is_relative_to(_owner_dir(owner).resolve()):
            raise GatewayError("This credential's key file is outside the owner's store.",
                               stage="llm_credential")
        raw = path.read_bytes()
    except OSError:
        return None
    secret = _decode_secret(raw, bool(row.get("encrypted")))
    if not row.get("encrypted") and secret and encryption_enabled():
        _reencrypt(owner, cred_id, secret)
    return secret


def call_target(owner: str, cred_id: str) -> tuple[str, str] | None:
    """``(base_url, api_key)`` to hand a provider client for a call with this credential, or None
    when there is no such credential or its key is gone.

    Without the key service: the credential's own address and its key. With it: the service's proxy
    for this credential and the service token, so the key never enters this process. Decisions
    about the endpoint (is it off-host, which protocol) still use the credential's own
    ``base_url`` and ``provider``, never this address."""
    row = get_credential(owner, cred_id)
    if row is None:
        return None
    svc = _service()
    if svc is not None:
        return svc.proxy_base(_safe_owner(owner), cred_id), svc.token()
    secret = resolve_secret(owner, cred_id)
    return (row.get("base_url") or "", secret) if secret else None


def touch(owner: str, cred_id: str) -> None:
    """A session key in use by a run stays alive: each of the run's calls counts as a use."""
    if is_session_id(cred_id):
        with _LOCK:
            row = _session_rows(owner).get(cred_id)
            if row is not None:
                row["_last_used"] = time.time()


def _reencrypt(owner: str, cred_id: str, secret: str) -> bool:
    """Rewrite a plaintext key file encrypted (same id, same path). Best-effort: a failure leaves
    the plaintext row working and is retried on the next read."""
    try:
        blob, encrypted = _encode_secret(secret)
        if not encrypted:
            return False
        with _LOCK:
            rows = _load_index(owner)
            row = _find(rows, cred_id)
            if row is None:
                return False
            _write_private(_key_path(owner, cred_id), blob)
            row["encrypted"] = True
            row["key_path"] = str(_key_path(owner, cred_id))
            _save_index(owner, rows)
        return True
    except Exception as exc:  # noqa: BLE001 - see the docstring
        print(f"[llm-credentials] could not encrypt credential {cred_id}: {exc}")
        return False


def encrypt_existing() -> int:
    """Encrypt every plaintext key in the store (gateway start). Returns how many were rewritten."""
    if _service() is not None or not encryption_enabled() or not _root().is_dir():
        return 0
    done = 0
    for d in sorted(p for p in _root().iterdir() if p.is_dir()):
        for row in _load_index(d.name):
            if row.get("encrypted"):
                continue
            try:
                secret = resolve_secret(d.name, row["id"])   # re-encrypts as a side effect
            except GatewayError:
                continue
            if secret and (get_credential(d.name, row["id"]) or {}).get("encrypted"):
                done += 1
    return done


def delete_owner(owner: str) -> int:
    """Remove every credential of ``owner`` — its key files and its index, and its session keys —
    when the account is deleted. Returns how many credentials there were."""
    with _LOCK:
        n = len(_SESSION.pop(_safe_owner(owner), {}))
    svc = _service()
    if svc is not None:
        try:
            svc.delete_owner(_safe_owner(owner))
        except GatewayError as exc:
            print(f"[llm-credentials] key service kept {owner}'s keys at account deletion: {exc}")
    d = _root() / _safe_owner(owner)
    if not d.is_dir():
        return n
    n += len(_load_index(owner))
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    return n


def delete_credential(owner: str, cred_id: str) -> bool:
    """Remove one credential. With the key service, the service forgets the key first; if it
    cannot be reached this raises and the credential stays listed (a deleted-looking key that is
    still held would be worse)."""
    svc = _service()
    if svc is not None and get_credential(owner, cred_id) is not None:
        svc.delete(_safe_owner(owner), cred_id)
    if is_session_id(cred_id):
        with _LOCK:
            return _SESSION.get(_safe_owner(owner), {}).pop(cred_id, None) is not None
    with _LOCK:
        rows = _load_index(owner)
        keep = [r for r in rows if r.get("id") != cred_id]
        if len(keep) == len(rows):
            return False
        gone = _find(rows, cred_id)
        try:
            kp = _key_file(owner, cred_id, gone or {}).resolve()
            if kp.is_relative_to(_owner_dir(owner).resolve()) and kp.is_file():
                kp.unlink()
        except OSError:
            pass
        _save_index(owner, keep)
    return True


# --- every key at once (admin; incident response) --------------------------------------------------


def _revocation_marker() -> Path:
    return Path(os.environ.get("AISCIENTIST_STATE_DIR", ".")) / "llm_keys_revoked_at"


def revoked_after() -> float | None:
    """When every key was last revoked (epoch seconds), or None. A key that entered the server
    before then — a session key, or a stored key a running run read — is refused (``key_guard``)."""
    try:
        return float(_revocation_marker().read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def all_credentials() -> list[tuple[str, dict]]:
    """``(owner, public row)`` for every credential on this server, stored and session-only: the
    admin's view. Never a key, a path or a ciphertext."""
    out: list[tuple[str, dict]] = []
    if _root().is_dir():
        for d in sorted(p for p in _root().iterdir() if p.is_dir()):
            out += [(d.name, _public(r)) for r in _load_index(d.name)]
    with _LOCK:
        for owner in sorted(_SESSION):
            out += [(owner, _public(r)) for r in _session_rows(owner).values()]
    return out


def store_summary() -> dict:
    """What :func:`purge_all` would remove: owners, stored credentials, key files, and whether a
    master key exists. Counts only."""
    owners, creds, files = 0, 0, 0
    if _root().is_dir():
        for d in (p for p in _root().iterdir() if p.is_dir()):
            owners += 1
            creds += len(_load_index(d.name))
            files += sum(1 for f in d.iterdir() if f.suffix == ".key")
    return {"owners": owners, "credentials": creds, "key_files": files,
            "master_key": _master_key_path().is_file()}


def purge_all(*, also_master_key: bool = False) -> dict:
    """Delete every stored API key of every user, and revoke the keys a running gateway holds.

    For an incident (a suspected server compromise, a leaked backup). In this order:

    1. the revocation marker is written, so a running gateway refuses every key it already read
       (a run in progress stops at its next model call; session-only keys end too);
    2. every owner's folder in the store is removed: key files and index;
    3. with ``also_master_key``, the master key too. Every encrypted copy of a key anywhere — a
       backup, a stray tarball — is then unreadable. A new master key is made at the next save.

    Users then re-enter their keys (after rotating them at the provider, which only they can do).
    Returns the counts removed."""
    summary = store_summary()
    svc = _service()
    if svc is not None:
        try:
            summary["service"] = svc.revoke_all(also_master_key=also_master_key)
        except GatewayError as exc:
            summary["service_error"] = exc.message
    marker = _revocation_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    _write_private(marker, f"{time.time():.3f} {_utc_now()}\n".encode())
    import shutil
    if _root().is_dir():
        for d in [p for p in _root().iterdir() if p.is_dir()]:
            shutil.rmtree(d, ignore_errors=True)
    with _LOCK:
        summary["session_keys"] = sum(len(v) for v in _SESSION.values())
        _SESSION.clear()
    if also_master_key:
        try:
            _master_key_path().unlink()
            summary["master_key_removed"] = True
        except FileNotFoundError:
            summary["master_key_removed"] = False
    left = store_summary()
    summary["left"] = {"owners": left["owners"], "key_files": left["key_files"]}
    return summary
