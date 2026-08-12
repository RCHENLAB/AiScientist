"""Password hashing + signed session tokens. Framework-agnostic and fully local.

- **Passwords**: bcrypt directly (``bcrypt`` package) — no passlib (avoids the well-known
  passlib↔bcrypt>=4 version wart). Hashes are salted; only the hash is ever stored.
- **Sessions**: stateless signed cookies via ``itsdangerous`` (the Flask signer). The
  cookie carries only the user id + an issue timestamp, signed with ``BIOAGENT_SECRET_KEY``;
  tampering or expiry → rejected. No server-side session table needed, and disabling a
  user (``is_active=False``) blocks them immediately because every request re-checks it.

No external API, no network — everything runs on the gateway host.
"""

from __future__ import annotations

import os

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "bioagent_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days
_DEV_SECRET = "dev-insecure-secret-change-me"


def secret_key() -> str:
    return os.environ.get("BIOAGENT_SECRET_KEY") or _DEV_SECRET


def using_dev_secret() -> bool:
    return secret_key() == _DEV_SECRET


def secure_cookies() -> bool:
    """Mark the session cookie ``Secure`` (never sent over plain HTTP) when the console
    is served behind HTTPS. Set ``BIOAGENT_PUBLIC_HTTPS=1`` in the prod environment (the
    nginx + TLS deployment); left off for local-dev over http://localhost."""
    return os.environ.get("BIOAGENT_PUBLIC_HTTPS", "").strip().lower() in ("1", "true", "yes")


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key(), salt="bioagent-session")


# --- passwords ---------------------------------------------------------------


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --- session tokens ----------------------------------------------------------


def make_session_token(user_id: int) -> str:
    return _serializer().dumps({"uid": int(user_id)})


def read_session_token(token: str, max_age: int = SESSION_MAX_AGE) -> int | None:
    """Return the user id from a valid token, or None if missing/forged/expired."""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=max_age)
        return int(data["uid"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None
