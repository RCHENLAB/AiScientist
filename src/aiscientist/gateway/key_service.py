"""The key service: the one process that holds users' API keys and makes calls with them.

RCHENLAB/AiScientist issue 16, task "keep keys away from agents"; design in
``docs/key_service_design.md``. Run it as its OWN system account (``aiscientist-keys``), so the web
server's account — which also runs the agents' tool calls — cannot read the keys, whatever code runs
in it:

    AISCIENTIST_STATE_DIR=/data/aiscientist/keys \\
    AISCIENTIST_KEY_SERVICE_TOKEN_FILE=/etc/aiscientist-keys/token \\
    python -m aiscientist.gateway.key_service --host 127.0.0.1 --port 8899

The store is :mod:`llm_credentials` in its ordinary file mode, under this process's own state dir
(the web server keeps only the metadata). Every request carries the shared token (``Authorization:
Bearer`` or ``x-api-key``); without it nothing answers but ``/health``.

* ``PUT /v1/keys/{owner}/{id}`` — hold a key (new or replacing), with the address and provider it is
  for; ``session: true`` keeps it in memory only. ``DELETE`` one, or all of an owner's.
* ``/proxy/{owner}/{id}/{path}`` — the model call: the token is replaced by the key (``x-api-key`` for
  Anthropic, ``Authorization: Bearer`` otherwise) and the request forwarded to the key's own address
  plus ``path``; the reply streams back. An error reply is scrubbed of the key first.
* ``POST /v1/revoke-all`` — delete every key (``aiscientist-admin purge-llm-keys``).
* ``GET /v1/status`` — counts only.

It never returns a key, never logs one, and never forwards one to an address the caller names.
"""

from __future__ import annotations

import argparse
import hmac
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import llm_credentials
from .errors import GatewayError, scrub_secrets

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Request headers never forwarded: the token (replaced by the key), hop-by-hop ones, and those the
# HTTP client sets itself.
_DROP_REQ = {"host", "content-length", "authorization", "x-api-key", "connection", "keep-alive",
             "accept-encoding", "transfer-encoding", "te", "upgrade", "proxy-authorization",
             "proxy-connection", "cookie"}
_DROP_RESP = {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive",
              "set-cookie", "server", "date"}

app = FastAPI(title="AiScientist key service", docs_url=None, redoc_url=None, openapi_url=None)
_client: Any = None


def _token() -> str:
    raw = os.environ.get("AISCIENTIST_KEY_SERVICE_TOKEN_FILE", "").strip()
    path = Path(raw) if raw else Path(os.environ.get("AISCIENTIST_STATE_DIR", ".")) / "key_service.token"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _authorized(request: Request) -> bool:
    expected = _token()
    if not expected:
        return False
    got = request.headers.get("x-api-key") or ""
    auth = request.headers.get("authorization") or ""
    if not got and auth.lower().startswith("bearer "):
        got = auth[7:].strip()
    return bool(got) and hmac.compare_digest(got.encode(), expected.encode())


def _check(request: Request, *names: str) -> None:
    if not _authorized(request):
        raise HTTPException(status_code=401, detail="key service: not authorized")
    for n in names:
        if not _NAME.match(n or ""):
            raise HTTPException(status_code=400, detail="key service: bad name")


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "key_service"}}, status_code=status)


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    return _err(exc.status_code, str(exc.detail))


@app.get("/health")
def health() -> dict:
    return {"ok": True}


class KeyBody(BaseModel):
    api_key: str
    base_url: str
    provider: str = ""
    session: bool = False


@app.put("/v1/keys/{owner}/{cred_id}")
def put_key(owner: str, cred_id: str, body: KeyBody, request: Request) -> JSONResponse:
    _check(request, owner, cred_id)
    try:
        if body.session or llm_credentials.is_session_id(cred_id):
            if llm_credentials.get_credential(owner, cred_id) is not None:
                llm_credentials.delete_credential(owner, cred_id)
            row = llm_credentials.create_session(owner, session_tag="", provider=body.provider,
                                                 base_url=body.base_url, model="", api_key=body.api_key,
                                                 cred_id=cred_id)
        elif llm_credentials.get_credential(owner, cred_id) is not None:
            llm_credentials.update(owner, cred_id, base_url=body.base_url, provider=body.provider,
                                   key_follows=True)
            row = llm_credentials.rotate_key(owner, cred_id, body.api_key)
        else:
            row = llm_credentials.create(owner, provider=body.provider, base_url=body.base_url, model="",
                                         api_key=body.api_key, cred_id=cred_id)
    except GatewayError as exc:
        return _err(400, exc.message)
    return JSONResponse({"key": {k: row.get(k) for k in ("id", "key_hint", "key_fingerprint",
                                                         "session_only", "encrypted")}})


@app.get("/v1/keys/{owner}/{cred_id}")
def get_key(owner: str, cred_id: str, request: Request) -> JSONResponse:
    """Whether this service holds the key and can decrypt it, with its fingerprint (never the key)."""
    _check(request, owner, cred_id)
    row = llm_credentials.get_credential(owner, cred_id)
    if row is None:
        return _err(404, "Not held.")
    try:
        readable = bool(llm_credentials.resolve_secret(owner, cred_id))
    except GatewayError:
        readable = False
    return JSONResponse({"held": True, "readable": readable, "key_fingerprint": row.get("key_fingerprint")})


@app.delete("/v1/keys/{owner}/{cred_id}")
def delete_key(owner: str, cred_id: str, request: Request) -> dict:
    _check(request, owner, cred_id)
    return {"deleted": llm_credentials.delete_credential(owner, cred_id)}


@app.delete("/v1/keys/{owner}")
def delete_owner(owner: str, request: Request) -> dict:
    _check(request, owner)
    return {"deleted": llm_credentials.delete_owner(owner)}


@app.post("/v1/sessions/drop")
def drop_sessions(request: Request) -> dict:
    _check(request)
    with llm_credentials._LOCK:
        n = sum(len(v) for v in llm_credentials._SESSION.values())
        llm_credentials._SESSION.clear()
    return {"dropped": n}


class RevokeBody(BaseModel):
    also_master_key: bool = False


@app.post("/v1/revoke-all")
def revoke_all(body: RevokeBody, request: Request) -> dict:
    _check(request)
    done = llm_credentials.purge_all(also_master_key=body.also_master_key)
    print(f"[key-service] every key revoked: {done.get('credentials')} saved, "
          f"{done.get('session_keys')} session-only")
    return done


@app.get("/v1/status")
def status(request: Request) -> dict:
    """How many keys this service holds and how many it can decrypt (the install script compares
    this with the web server's list). Counts only."""
    _check(request)
    held = readable = 0
    for owner, row in llm_credentials.all_credentials():
        held += 1
        try:
            if llm_credentials.resolve_secret(owner, row["id"]):
                readable += 1
        except GatewayError:
            pass
    with llm_credentials._LOCK:
        sessions = sum(len(v) for v in llm_credentials._SESSION.values())
    return {"held": held, "readable": readable, "session_keys": sessions,
            "encrypted": llm_credentials.encryption_enabled()}


# --- the proxy ------------------------------------------------------------------------------------


def _target(owner: str, cred_id: str) -> tuple[str, str, bool]:
    """``(base URL, key, anthropic?)`` for the credential, or a 404."""
    from . import anthropic_client, llm_providers
    row = llm_credentials.get_credential(owner, cred_id)
    key = llm_credentials.resolve_secret(owner, cred_id) if row is not None else None
    if row is None or not key:
        raise HTTPException(status_code=404, detail="This API key is not held by the key service "
                                                    "(deleted, revoked, or a session key that ended).")
    base = row.get("base_url") or ""
    native = llm_providers.transport_for(row.get("provider"), base) == "anthropic"
    if native:
        base = anthropic_client.normalize_base_url(base)
    if not base.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="This key has no usable address.")
    return base.rstrip("/"), key, native


def _http():
    global _client
    if _client is None:
        import httpx
        _client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=1800.0, write=120.0, pool=60.0),
                                    follow_redirects=False,
                                    limits=httpx.Limits(max_connections=64, max_keepalive_connections=16))
    return _client


@app.api_route("/proxy/{owner}/{cred_id}/{path:path}", methods=["GET", "POST"])
async def proxy(owner: str, cred_id: str, path: str, request: Request) -> Response:
    _check(request, owner, cred_id)
    if ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="key service: bad path")
    base, key, native = _target(owner, cred_id)
    url = f"{base}/{path.lstrip('/')}"
    if request.url.query:
        url += "?" + request.url.query
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQ}
    if native:
        headers["x-api-key"] = key
    else:
        headers["authorization"] = f"Bearer {key}"
    body = await request.body()
    import httpx
    client = _http()
    try:
        upstream = await client.send(client.build_request(request.method, url, headers=headers, content=body),
                                     stream=True)
    except httpx.HTTPError as exc:
        return _err(502, f"The key service could not reach the provider ({type(exc).__name__}).")
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESP}
    if upstream.status_code >= 400:
        # A provider's error body can copy the key it was given: scrub it before it leaves.
        data = await upstream.aread()
        await upstream.aclose()
        clean = scrub_secrets(data.decode("utf-8", "replace"), key)
        return Response(clean.encode("utf-8"), status_code=upstream.status_code, headers=out_headers)
    secret = key.encode("utf-8")

    async def stream():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk.replace(secret, b"<key>") if secret in chunk else chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(stream(), status_code=upstream.status_code, headers=out_headers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiscientist-key-service",
                                     description="Hold users' API keys and make model calls with them.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args(argv)
    if not os.environ.get("AISCIENTIST_STATE_DIR", "").strip():
        print("AISCIENTIST_STATE_DIR (the key service's own store) is not set.")
        return 2
    if os.environ.get("AISCIENTIST_KEY_SERVICE", "").strip().lower() not in ("", "inproc"):
        print("AISCIENTIST_KEY_SERVICE is set: the key service itself keeps the keys, unset it here.")
        return 2
    if not _token():
        print("The token file (AISCIENTIST_KEY_SERVICE_TOKEN_FILE) is missing or empty.")
        return 2
    print(f"[key-service] {llm_credentials.encrypt_existing()} plaintext key(s) encrypted at start")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
