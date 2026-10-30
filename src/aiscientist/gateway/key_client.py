"""The web server's side of the key service (``key_service.py``, RCHENLAB/AiScientist issue 16).

With ``AISCIENTIST_KEY_SERVICE`` unset (or ``inproc``) nothing here is used: the web server keeps
and decrypts the API keys itself, as it always did (``llm_credentials``).

With ``AISCIENTIST_KEY_SERVICE=http://127.0.0.1:<port>`` the keys live in a separate process, run as
its own system account, which alone can read them. The web server then:

* hands a new or rotated key to the service once, as the user submits it (:func:`put`), and keeps
  only the key's public metadata (label, hint, fingerprint);
* makes every model call through the service's proxy (:func:`proxy_base`): the provider client is
  pointed at ``<service>/proxy/<owner>/<credential id>`` with the service TOKEN as its "API key".
  The service checks the token, puts the real key in the request and forwards it to the address the
  key was saved for, never to one the web server names. No client library changes: an OpenAI-
  compatible client and the Anthropic SDK each see an ordinary base URL and key.

The token (``AISCIENTIST_KEY_SERVICE_TOKEN_FILE``) lets its holder SPEND keys through the service,
never read one: whoever runs as the web server's account could spend them anyway.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .errors import GatewayError

_OFF = {"", "inproc", "0", "off", "no", "false", "none"}
_token_cache: tuple[str, float, str] | None = None     # (path, mtime, token)


def service_url() -> str | None:
    raw = os.environ.get("AISCIENTIST_KEY_SERVICE", "").strip()
    return None if raw.lower() in _OFF else raw.rstrip("/")


def enabled() -> bool:
    return service_url() is not None


def token_file() -> Path:
    raw = os.environ.get("AISCIENTIST_KEY_SERVICE_TOKEN_FILE", "").strip()
    if raw:
        return Path(raw)
    return Path(os.environ.get("AISCIENTIST_STATE_DIR", ".")) / "key_service.token"


def token() -> str:
    """The shared token, re-read when the file changes (the install script may rotate it)."""
    global _token_cache
    path = token_file()
    try:
        mtime = path.stat().st_mtime
        if _token_cache and _token_cache[0] == str(path) and _token_cache[1] == mtime:
            return _token_cache[2]
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise GatewayError("The key service's token cannot be read on this server, so no saved API key "
                           "can be used. An administrator needs to check the key service.",
                           stage="llm_key_service") from exc
    if not value:
        raise GatewayError("The key service's token is empty.", stage="llm_key_service")
    _token_cache = (str(path), mtime, value)
    return value


def proxy_base(owner: str, cred_id: str) -> str:
    """The base URL a provider client uses for this credential: the service forwards whatever path
    the client appends (``/chat/completions``, ``/v1/messages``, ``/models``) to the key's own
    address, with the key."""
    return f"{service_url()}/proxy/{owner}/{cred_id}"


def _call(method: str, path: str, body: dict | None = None, timeout: float = 20.0) -> dict[str, Any]:
    url = service_url()
    if url is None:
        raise GatewayError("No key service is configured.", stage="llm_key_service")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token()}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback service
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            msg = (json.loads(exc.read() or b"{}").get("error") or {})
        except ValueError:
            msg = {}
        text = msg.get("message") if isinstance(msg, dict) else str(msg)
        raise GatewayError(text or f"The key service answered HTTP {exc.code}.",
                           stage="llm_key_service", detail={"status": exc.code}) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GatewayError("The key service is not answering, so saved API keys cannot be used or "
                           "changed right now. An administrator needs to check it.",
                           stage="llm_key_service") from exc
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {}


def put(owner: str, cred_id: str, *, api_key: str, base_url: str, provider: str | None,
        session: bool = False) -> dict:
    """Give the service a key to hold (new, or replacing the one it holds for this id). ``session``:
    in its memory only, never on disk. Returns the service's public row (hint, fingerprint)."""
    return _call("PUT", f"/v1/keys/{owner}/{cred_id}",
                 {"api_key": api_key, "base_url": base_url, "provider": provider or "",
                  "session": bool(session)}).get("key") or {}


def get(owner: str, cred_id: str) -> dict | None:
    """``{"held", "readable", "key_fingerprint"}`` for a key the service holds, else None."""
    try:
        return _call("GET", f"/v1/keys/{owner}/{cred_id}")
    except GatewayError as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("status") == 404:
            return None
        raise


def delete(owner: str, cred_id: str) -> bool:
    return bool(_call("DELETE", f"/v1/keys/{owner}/{cred_id}").get("deleted"))


def delete_owner(owner: str) -> int:
    return int(_call("DELETE", f"/v1/keys/{owner}").get("deleted") or 0)


def drop_sessions() -> int:
    """Forget every session-only key the service holds (the web server restarted, so the browser
    sessions that entered them are gone)."""
    return int(_call("POST", "/v1/sessions/drop").get("dropped") or 0)


def revoke_all(*, also_master_key: bool = False) -> dict:
    return _call("POST", "/v1/revoke-all", {"also_master_key": bool(also_master_key)}, timeout=60.0)


def status() -> dict:
    """Counts only: keys held, keys it can decrypt, session keys."""
    return _call("GET", "/v1/status")
