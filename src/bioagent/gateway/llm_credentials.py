"""Per-user LLM API credentials — bring-your-own-key for the research lab.

The user still supplies an HPC3 account (that is what runs the Slurm analysis jobs); this
module covers the SEPARATE question of which LLM answers, and on whose bill.

Storage deliberately mirrors :mod:`ssh_credentials` — ``<BIOAGENT_STATE_DIR>/llm_creds/<owner>/``
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

**At-rest encryption is optional** (``BIOAGENT_LLM_KEY_ENCRYPTION=1``), and off by default, which
matches how SSH private keys are already stored here. The reasoning: encryption protects against
FILE-level exposure (a backup, a stray ``cat``, a misplaced tarball) but not against a compromised
gateway process, which must be able to decrypt in order to use the key at all. Against that
partial benefit it adds a real failure mode — lose the master key and every user re-enters their
key. So it is a deployment choice. The ``encrypted`` flag is stored PER ROW, so switching it on
later affects only newly written rows and existing plaintext rows keep working.

The master key lives in its own 0600 file, never in ``.env``: the production ``.env`` on eyeserver
is world-readable by design, so a secret placed there would be readable by every account on the box.

Nothing in this module opens a socket — verification lives in :mod:`llm_providers`.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .errors import GatewayError


def _root() -> Path:
    return Path(os.environ.get("BIOAGENT_STATE_DIR", ".")) / "llm_creds"


def _safe_owner(owner: str) -> str:
    cleaned = "".join(c for c in (owner or "") if c.isalnum() or c in "-_")
    return cleaned or "guest"


def _owner_dir(owner: str) -> Path:
    d = _root() / _safe_owner(owner)
    d.mkdir(parents=True, exist_ok=True)
    return d


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


# --- optional at-rest encryption ---------------------------------------------


def encryption_enabled() -> bool:
    return os.environ.get("BIOAGENT_LLM_KEY_ENCRYPTION", "").strip().lower() in {"1", "true", "yes", "on"}


def _master_key_path() -> Path:
    return _root() / ".master"


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
    """A recognizable but useless fragment, so a user with several keys can tell them apart
    in the UI. Short keys degrade to a pure mask rather than leaking most of themselves."""
    if len(secret) <= 12:
        return "…" + secret[-2:] if len(secret) > 4 else "…"
    return f"{secret[:6]}…{secret[-4:]}"


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
        "key_hint": row.get("key_hint"),
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
    }


# --- store -------------------------------------------------------------------


def _find(rows: list[dict], cred_id: str) -> dict | None:
    return next((r for r in rows if r.get("id") == cred_id), None)


def _key_path(owner: str, cred_id: str) -> Path:
    return _owner_dir(owner) / f"{cred_id}.key"


def _key_file(owner: str, cred_id: str, row: dict) -> Path:
    """Where this credential's key actually is — canonical location first, recorded path second.

    A row stores the ``key_path`` it was written with, and that path does not survive the store
    moving. Rows created while ``BIOAGENT_STATE_DIR`` was unset hold a path relative to the
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
           verify: Callable[[str, str, str | None], Any] | None = None) -> dict:
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

    cred_id = uuid4().hex[:12]
    blob, encrypted = _encode_secret(api_key)
    _write_private(_key_path(owner, cred_id), blob)

    row = {
        "id": cred_id,
        "label": label or provider or "endpoint",
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "lab_model": lab_model or None,
        "key_path": str(_key_path(owner, cred_id)),
        "key_hint": _hint(api_key),
        "key_fingerprint": _fingerprint(api_key),
        "encrypted": encrypted,
        "created_at": _utc_now(),
        "rotated_at": None,
        "verified_at": _utc_now() if verify is not None else None,
        "verified_model": model if verify is not None else None,
        "last_error": None,
    }
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

    rows = _load_index(owner)
    row = _find(rows, cred_id)
    if row is None:
        raise GatewayError("No such API credential.", stage="llm_credential")

    if verify is not None:
        verify(row.get("base_url", ""), api_key, row.get("model"))

    blob, encrypted = _encode_secret(api_key)
    _write_private(_key_path(owner, cred_id), blob)   # atomic; the id and path are unchanged

    row["key_hint"] = _hint(api_key)
    row["key_fingerprint"] = _fingerprint(api_key)
    row["encrypted"] = encrypted
    row["rotated_at"] = _utc_now()
    row["last_error"] = None
    if verify is not None:
        row["verified_at"] = _utc_now()
        row["verified_model"] = row.get("model")
    _save_index(owner, rows)
    return _public(row)


def update(owner: str, cred_id: str, *, label: str | None = None, model: str | None = None,
           lab_model: str | None = None, base_url: str | None = None,
           provider: str | None = None) -> dict:
    """Edit METADATA only — never the key. Changing ``model`` or ``base_url`` invalidates the
    stored verification (the key was proven against the old pair, not this one), so the row is
    marked unverified rather than carrying a stale green tick."""
    rows = _load_index(owner)
    row = _find(rows, cred_id)
    if row is None:
        raise GatewayError("No such API credential.", stage="llm_credential")

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
    if lab_model is not None:
        row["lab_model"] = lab_model or None

    if endpoint_changed:
        row["verified_at"] = None
        row["verified_model"] = None
    _save_index(owner, rows)
    return _public(row)


def mark_verified(owner: str, cred_id: str, *, model: str | None, error: str | None = None) -> dict | None:
    """Record the outcome of a re-test so the UI can show which saved keys still work without
    re-testing them on every page load."""
    rows = _load_index(owner)
    row = _find(rows, cred_id)
    if row is None:
        return None
    if error:
        row["last_error"] = error[:300]
    else:
        row["verified_at"] = _utc_now()
        row["verified_model"] = model
        row["last_error"] = None
    _save_index(owner, rows)
    return _public(row)


def record_egress_consent(owner: str, cred_id: str) -> dict | None:
    """Record that the owner accepted sending the reasoning payload to THIS endpoint.

    Re-recorded rather than toggled, so the timestamp always reflects the most recent explicit
    acceptance. Deliberately not exposed as a way to REVOKE consent — a user who no longer wants
    an endpoint used deletes the credential, which also removes the key.
    """
    rows = _load_index(owner)
    row = _find(rows, cred_id)
    if row is None:
        return None
    row["egress_consent_at"] = _utc_now()
    _save_index(owner, rows)
    return _public(row)


def storage_info(owner: str) -> dict:
    """Where this owner's keys actually live, in plain terms, for the UI to show.

    "Where is my key?" is a fair question and the honest answer is short: a 0600 file on THIS
    server, under a directory named after the account, never in the browser and never back over
    the wire. Users who cannot see that answer assume the worst (or paste the key somewhere
    else "to be safe"), so the dialog states it rather than leaving it to the docs.
    """
    # Absolute, always. ``BIOAGENT_STATE_DIR`` is unset in production, so the store resolves
    # relative to the service's working directory and this read "llm_creds/<user>" — which
    # answers "where is my key?" with a path the reader cannot locate, and so does not answer it.
    return {
        "owner": _safe_owner(owner),
        "dir": str(_owner_dir(owner).resolve()),
        "mode": "0600",
        "encrypted": encryption_enabled(),
    }


def list_credentials(owner: str) -> list[dict]:
    return [_public(r) for r in _load_index(owner)]


def get_credential(owner: str, cred_id: str) -> dict | None:
    """The full row (base_url, model, key_path, …) for the LLM-binding path — still without the
    secret itself. Confined to ``owner``: one user's id never resolves against another's store."""
    return _find(_load_index(owner), cred_id)


def get_public(owner: str, cred_id: str) -> dict | None:
    """The masked view of one credential — what a route may hand back to the browser."""
    row = get_credential(owner, cred_id)
    return _public(row) if row else None


def resolve_secret(owner: str, cred_id: str) -> str | None:
    """The plaintext API key. **The only function in the codebase that returns one.**

    Callers must treat the result the way they treat an SSH private key: pass it straight to the
    HTTP layer, never log it, never put it in an emitted event, never store it on the connection
    in a form that a status payload could serialize.
    """
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
    return _decode_secret(raw, bool(row.get("encrypted")))


def delete_credential(owner: str, cred_id: str) -> bool:
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
