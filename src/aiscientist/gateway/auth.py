"""Password hashing + signed session tokens. Framework-agnostic and fully local.

- **Passwords**: bcrypt directly (``bcrypt`` package) — no passlib (avoids the well-known
  passlib↔bcrypt>=4 version wart). Hashes are salted; only the hash is ever stored.
- **Sessions**: stateless signed cookies via ``itsdangerous`` (the Flask signer). The
  cookie carries only the user id + an issue timestamp, signed with the session secret
  (:func:`secret_key`); tampering or expiry → rejected. No server-side session table needed, and
  disabling a user (``is_active=False``) blocks them immediately because every request re-checks it.
- **Session secret**: a 0600 file, ``<AISCIENTIST_STATE_DIR>/session_secret``, generated on first
  start (or the file ``AISCIENTIST_SECRET_KEY_FILE`` names). Whoever holds it can sign a cookie for
  any account, so it is kept where only the service account can read it, not in the environment.

No external API, no network — everything runs on the gateway host.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "bioagent_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days
_DEV_SECRET = "dev-insecure-secret-change-me"
SECRET_FILE_NAME = "session_secret"

# path -> secret, read once per process: every request checks its cookie, and a rotated file takes
# effect on the next restart (which signs everyone out anyway). Tests reload this module to reset it.
_file_secrets: dict[str, str] = {}
_created_here: set[str] = set()


def secret_file() -> Path | None:
    """The file that holds the session secret: ``AISCIENTIST_SECRET_KEY_FILE``, else
    ``<AISCIENTIST_STATE_DIR>/session_secret``. None when neither is configured (a laptop)."""
    explicit = os.environ.get("AISCIENTIST_SECRET_KEY_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state = os.environ.get("AISCIENTIST_STATE_DIR", "").strip()
    return Path(state) / SECRET_FILE_NAME if state else None


def secret_source() -> str:
    """Where :func:`secret_key` takes the secret from: ``"env"``, ``"file"`` or ``"dev"``."""
    if os.environ.get("AISCIENTIST_SECRET_KEY"):
        return "env"
    return "file" if secret_file() is not None else "dev"


def secret_key() -> str:
    """The secret that signs session cookies.

    ``AISCIENTIST_SECRET_KEY`` still wins when it is set (older deployments keep working), but the
    gateway warns at startup: an environment variable reaches every child process and the file it
    is set from is often readable by more accounts than the service. Otherwise the secret is the
    content of :func:`secret_file`, created on first use. The built-in dev secret is used only
    when no state dir is configured at all — never as a fallback for a file that cannot be read,
    because a known secret lets anyone sign a cookie for any account.
    """
    env = os.environ.get("AISCIENTIST_SECRET_KEY")
    if env:
        return env
    path = secret_file()
    if path is None:
        return _DEV_SECRET
    key = str(path)
    if key not in _file_secrets:
        _file_secrets[key] = _load_or_create_secret(path)
    return _file_secrets[key]


def _read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"session secret file {path} is empty; delete it to generate a new one")
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            path.chmod(0o600)   # made by hand under the default umask: close it before it is used
    except OSError:
        pass                    # not ours or read-only; startup_notes() reports the mode
    return value


def _load_or_create_secret(path: Path) -> str:
    """Read the secret file, creating it with 32 random bytes (hex) when it does not exist.

    The new file is written to a temp file created 0600 BEFORE the secret goes into it (the same
    pattern as ``llm_credentials._write_private``), then hard-linked into place: a link never
    replaces an existing file, so when two processes start at once both end up with the secret
    of whichever linked first instead of each signing with its own.
    """
    try:
        return _read_secret(path)
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(32) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)
            _created_here.add(str(path))
        except FileExistsError:
            pass                # another process won the race: use its secret
        except OSError:         # a filesystem without hard links
            if not path.exists():
                os.replace(tmp, path)
                _created_here.add(str(path))
    finally:
        tmp.unlink(missing_ok=True)
    return _read_secret(path)


def using_dev_secret() -> bool:
    return secret_key() == _DEV_SECRET


def startup_notes() -> list[str]:
    """What the gateway logs about the session secret when it starts (with accounts on).

    Resolving the secret here also creates the file on first start, so a state dir that cannot be
    written fails at startup rather than at the first login.
    """
    key = secret_key()
    source = secret_source()
    notes: list[str] = []
    if source == "env":
        notes.append(
            "[auth] AISCIENTIST_SECRET_KEY is set in the environment. Move the session secret to a "
            "0600 file: remove it from the environment and the .env file, and the gateway generates "
            f"<AISCIENTIST_STATE_DIR>/{SECRET_FILE_NAME} on its next start (or point "
            "AISCIENTIST_SECRET_KEY_FILE at a file). An environment variable reaches every child "
            "process, and an env file is often readable by more accounts than the service. "
            "Changing the secret signs every user out once.")
    elif source == "file":
        path = secret_file()
        if str(path) in _created_here:
            notes.append(f"[auth] created a new session secret at {path} (mode 0600); sessions "
                         "signed with any earlier secret are no longer valid, so users sign in again")
        else:
            notes.append(f"[auth] session secret: {path}")
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            mode = 0
        if mode & 0o077:
            notes.append(f"[auth] *** SECURITY: {path} has mode {mode:04o}: other accounts can read "
                         "the key that signs session cookies. chmod 600 it, then rotate it. ***")
    if key == _DEV_SECRET:
        if secure_cookies():
            notes.append(
                "[auth] *** SECURITY: AISCIENTIST_PUBLIC_HTTPS is set but session cookies are signed "
                "with the built-in dev secret — they are FORGEABLE. Set AISCIENTIST_STATE_DIR (the "
                f"gateway then keeps a generated secret in <state>/{SECRET_FILE_NAME}) before "
                "exposing this publicly. ***")
        else:
            notes.append("[auth] no AISCIENTIST_STATE_DIR: session cookies are signed with the built-in "
                         "dev secret (fine on a laptop, forgeable anywhere else)")
    return notes


def secure_cookies() -> bool:
    """Mark the session cookie ``Secure`` (never sent over plain HTTP) when the console
    is served behind HTTPS. Set ``AISCIENTIST_PUBLIC_HTTPS=1`` in the prod environment (the
    nginx + TLS deployment); left off for local-dev over http://localhost."""
    return os.environ.get("AISCIENTIST_PUBLIC_HTTPS", "").strip().lower() in ("1", "true", "yes")


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
