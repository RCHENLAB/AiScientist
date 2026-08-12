"""Per-user SSH credential lifecycle: generate a keypair, deploy the public key to the
user's HPC3 ``~/.ssh/authorized_keys`` over an already-authenticated session, and remember
the private key locally so the NEXT login can use the key (no password, no Duo).

Flow:
  1. First login with password + Duo succeeds → we have a live, authenticated executor.
  2. :func:`create_and_deploy` generates an Ed25519 keypair (optionally passphrase-
     encrypted), appends the public key to the remote ``authorized_keys`` (idempotent),
     and records the credential under the caller's own store.
  3. Next time, the UI offers that credential; login uses ``auth_publickey`` — no Duo.

Storage: ``<BIOAGENT_STATE_DIR>/ssh_creds/<owner>/`` — one ``<id>.key`` private-key file
(0600) per credential plus an ``index.json`` of public metadata. Private keys never leave
the gateway host; only the PUBLIC key is sent to HPC3.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .errors import GatewayError


def _root() -> Path:
    return Path(os.environ.get("BIOAGENT_STATE_DIR", ".")) / "ssh_creds"


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
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []


def _save_index(owner: str, rows: list[dict]) -> None:
    _index_path(owner).write_text(json.dumps(rows, indent=2), encoding="utf-8")


def _public(row: dict) -> dict:
    """Metadata safe to hand the UI — never the private-key path/content."""
    return {
        "id": row["id"], "host": row["host"], "hpc_user": row["hpc_user"],
        "label": row.get("label") or f"{row['hpc_user']}@{row['host']}",
        "encrypted": bool(row.get("encrypted")), "created_at": row.get("created_at"),
    }


# --- keygen + deploy ---------------------------------------------------------


def generate_keypair(passphrase: str | None = None) -> tuple[bytes, str]:
    """A fresh Ed25519 keypair. Returns ``(private_openssh_pem_bytes, public_openssh_str)``.
    When ``passphrase`` is given, the private key is encrypted at rest (BestAvailable)."""
    key = Ed25519PrivateKey.generate()
    enc: serialization.KeySerializationEncryption
    enc = serialization.BestAvailableEncryption(passphrase.encode("utf-8")) if passphrase else serialization.NoEncryption()
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=enc,
    )
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    return priv, pub


def deploy_public_key(executor, public_key: str, comment: str) -> None:
    """Append ``public_key`` to the remote ``~/.ssh/authorized_keys`` (idempotent) over the
    already-authenticated ``executor``. Only the PUBLIC key is sent. Raises on failure."""
    line = f"{public_key.strip()} {comment}".strip()
    q = shlex.quote(line)
    cmd = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys && "
        f"(grep -qxF {q} ~/.ssh/authorized_keys || echo {q} >> ~/.ssh/authorized_keys)"
    )
    res = executor.exec(cmd)
    if getattr(res, "exit_status", 0) != 0:
        raise GatewayError(
            "Could not install the SSH key on HPC3 (writing ~/.ssh/authorized_keys failed).",
            stage="ssh_keydeploy",
        )


# --- store -------------------------------------------------------------------


def create_and_deploy(owner: str, executor, *, host: str, hpc_user: str,
                      passphrase: str | None = None, label: str | None = None) -> dict:
    """Generate a keypair, deploy its public key to HPC3 via ``executor``, persist the
    private key locally (0600), and record the credential. Returns the PUBLIC metadata."""
    priv, pub = generate_keypair(passphrase or None)
    deploy_public_key(executor, pub, comment=f"bioagent-{_safe_owner(owner)}@{host}")

    cred_id = uuid4().hex[:12]
    key_path = _owner_dir(owner) / f"{cred_id}.key"
    key_path.write_bytes(priv)
    try:
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)   # 0600
    except OSError:
        pass

    row = {
        "id": cred_id, "host": host, "hpc_user": hpc_user, "key_path": str(key_path),
        "encrypted": bool(passphrase), "label": label or f"{hpc_user}@{host}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    rows = _load_index(owner)
    rows.insert(0, row)
    _save_index(owner, rows)
    return _public(row)


def list_credentials(owner: str) -> list[dict]:
    return [_public(r) for r in _load_index(owner)]


def get_credential(owner: str, cred_id: str) -> dict | None:
    """Full row (incl. ``key_path``) for the login path, or None. Confined to ``owner``."""
    for r in _load_index(owner):
        if r["id"] == cred_id:
            return r
    return None


def delete_credential(owner: str, cred_id: str) -> bool:
    rows = _load_index(owner)
    keep = [r for r in rows if r["id"] != cred_id]
    if len(keep) == len(rows):
        return False
    gone = next(r for r in rows if r["id"] == cred_id)
    try:
        kp = Path(gone["key_path"]).resolve()
        if kp.is_relative_to(_owner_dir(owner).resolve()) and kp.is_file():
            kp.unlink()
    except OSError:
        pass
    _save_index(owner, keep)
    return True
