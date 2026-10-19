"""Resumable browser-to-gateway uploads (tus 1.0 core + creation/termination/checksum).

Only this module owns bytes before a file is complete. The existing staging queue owns a
completed file afterwards. Metadata is kept beside the part file so HEAD survives a gateway
restart; every operation checks the connection's account ownership.

No request ever waits for another one on the same upload. A browser whose network path vanishes
without a FIN leaves its PATCH open here; a HEAD queued behind it outlived the proxy's 15-second
request timeout (504), and tus-js-client takes any answer to its resume HEAD other than 2xx or 423
for "cannot resume" and sends the file again from byte 0 (prod, 2026-10-13). So a request for an
upload another request holds is answered 423 Locked at once, which tus-js-client retries, and a
PATCH body that delivers nothing for ``_IDLE_SECONDS`` is given up. Its bytes are staged in
``.incoming`` and dropped, so the persisted offset never moves.

A browser finds its own unfinished uploads in its storage. One that has none for a file (another
computer, a private window, cleared site data) asks ``POST /api/tus/find``: the account's unfinished
upload of the same path, size and modification time is offered when its first bytes are the file's
first bytes. ``.parts/by-file/<key>`` lists the uploads of one such file, so the lookup reads one
small file, not every sidecar. ``GET /api/tus/unfinished`` lists every upload of the account that
another browser can continue this way, so such a browser shows them before any file is chosen.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import importlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.requests import ClientDisconnect

from . import upload_log, upload_storage

router = APIRouter(prefix="/api/tus")
_VERSION = "1.0.0"
_MAX_SIZE = 100 * 1024**3
_HEADERS = {"Tus-Resumable": _VERSION, "Cache-Control": "no-store"}
# A live PATCH delivers a block every fraction of a second, even at 0.6 MiB/s. Well under the
# proxy's 300 s stream idle timeout, which is what used to end a PATCH whose browser was gone.
_IDLE_SECONDS = 30.0
# An unfinished upload nothing has written to for this long is removed (and never offered).
# The browser's chunk (UPLOAD_CHUNK in frontend/console/app.js). Every unfinished upload holds at
# least this much, since the offset moves one whole chunk at a time; a file of one chunk or less is
# sent in one request and is never left part-way. A browser continuing another browser's upload
# sends the SHA-256 of its file's first chunk, compared with the first chunk of the ``.part``.
_HEAD_BYTES = 8 * 1024**2
_locks_guard = threading.Lock()
_locks: dict[str, asyncio.Lock] = {}
_last_cleanup: dict[str, float] = {}


def _gw():
    # app includes this router after defining its upload/staging helpers.
    return importlib.import_module("aiscientist.gateway.app")


def _answer(status: int, *, headers: dict | None = None, body: dict | None = None) -> Response:
    merged = {**_HEADERS, **(headers or {})}
    if body is None:
        return Response(status_code=status, headers=merged)
    return Response(json.dumps(body), status_code=status, headers=merged, media_type="application/json")


def _require_version(request: Request) -> Response | None:
    if request.headers.get("Tus-Resumable") != _VERSION:
        return _answer(412, headers={"Tus-Version": _VERSION}, body={"error": "Tus-Resumable: 1.0.0 required"})
    return None


# The sidecar of an upload whose bytes all arrived (handed to the HPC3 copy, or complete) is only a
# record; it goes after a week, while a stopped upload goes after _expire_seconds().
_FINISHED_KEEP_S = 7 * 86400


def _expire_seconds() -> float:
    """How long an upload nobody continues is kept (``AISCIENTIST_UPLOAD_TTL_SECONDS``, 24 h): its
    sidecar, its partial file, and its offer to another browser all go together."""
    return upload_storage.ttl_seconds()


def _metadata(raw: str) -> dict[str, str]:
    result = {}
    if len(raw) > 8192:
        raise ValueError("Upload-Metadata is too large")
    for item in filter(None, (x.strip() for x in raw.split(","))):
        key, sep, encoded = item.partition(" ")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not sep or key in result:
            raise ValueError("invalid Upload-Metadata")
        try:
            result[key] = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError) as exc:
            raise ValueError("invalid Upload-Metadata encoding") from exc
    return result


def _paths(conn, upload_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise ValueError("invalid upload id")
    root = upload_storage.uploads(conn) / ".parts"
    return root / f"{upload_id}.json", root / f"{upload_id}.part"


def _lock(upload_id: str) -> asyncio.Lock:
    with _locks_guard:
        return _locks.setdefault(upload_id, asyncio.Lock())


def _busy(conn, upload_id: str, meta: dict, method: str) -> Response | None:
    """423 Locked when another request holds this upload, else None. Called right before
    ``async with _lock(upload_id)`` with no ``await`` in between, so nothing waits on the lock.

    The answer carries the persisted offset: a PATCH in progress moves it only when it completes."""
    if not _lock(upload_id).locked():
        return None
    upload_log.record(conn.workspace, "busy", upload_id=upload_id, method=method,
                      name=meta.get("rel_path") or meta.get("name"), offset=meta.get("offset"),
                      error="an earlier request for this upload is still open; answered 423 Locked")
    return _answer(423, headers={"Upload-Offset": str(meta.get("offset", 0)),
                                 "Upload-Length": str(meta.get("length", 0))},
                   body={"error": "the server is still closing an earlier request for this upload"})


class _Stalled(Exception):
    """A PATCH body that delivered nothing for ``_IDLE_SECONDS``."""


async def _blocks(request: Request):
    """The request body block by block, or :class:`_Stalled` once ``_IDLE_SECONDS`` pass without one.
    A path that vanished silently gives neither more bytes nor a disconnect."""
    stream = request.stream().__aiter__()
    while True:
        try:
            block = await asyncio.wait_for(stream.__anext__(), _IDLE_SECONDS)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise _Stalled from None
        yield block


def _save(path: Path, meta: dict) -> None:
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(temp, path)


def _cleanup_parts(root: Path) -> None:
    """Expire inactive resumable uploads without touching a live PATCH."""
    now = time.time()
    key = str(root)
    with _locks_guard:
        if now - _last_cleanup.get(key, 0) < min(60, upload_storage.ttl_seconds()):
            return
        _last_cleanup[key] = now
    if not root.is_dir():
        return
    for path in root.glob("*.json"):
        if now - path.stat().st_mtime < _expire_seconds():
            continue
        upload_id = path.stem
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id) or _lock(upload_id).locked():
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("status") in ("staging", "complete") and now - path.stat().st_mtime < _FINISHED_KEEP_S:
            continue        # a finished upload's record (its bytes are elsewhere): kept a week, as before
        for suffix in (".json", ".part", ".incoming"):
            (root / (upload_id + suffix)).unlink(missing_ok=True)
    # An index entry is written when an upload is created; the upload itself can live on past that,
    # as long as its bytes keep coming. So an old entry goes only once none of its uploads is left.
    for entry in (root / "by-file").glob("*"):
        try:
            if now - entry.stat().st_mtime < _expire_seconds():
                continue
            ids = entry.read_text(encoding="utf-8").split()
        except OSError:
            continue
        if not any(re.fullmatch(r"[0-9a-f]{32}", i) and (root / f"{i}.json").exists() for i in ids):
            entry.unlink(missing_ok=True)


def _file_key(name: str, rel: str, length: int, last_modified: int) -> str:
    """The index name of one file as the browser knows it: its path in a folder upload (else its
    name), its size and its modification time. The cleaned names, as :func:`create` stores them."""
    return hashlib.sha256(json.dumps([rel or name, length, last_modified]).encode()).hexdigest()


def _index(root: Path, key: str, upload_id: str) -> None:
    index = root / "by-file"
    index.mkdir(exist_ok=True)
    with (index / key).open("a", encoding="utf-8") as out:
        out.write(upload_id + "\n")


def _first_chunk_sha256(part: Path) -> str:
    """SHA-256 of the first ``_HEAD_BYTES`` of an unfinished upload, "" when it holds fewer."""
    with part.open("rb") as stream:
        head = stream.read(_HEAD_BYTES)
    return hashlib.sha256(head).hexdigest() if len(head) == _HEAD_BYTES else ""


def _unfinished(conn, path: Path, meta: dict, now: float) -> bool:
    """An upload of this connection's account that holds bytes, is still taking more, and has not
    expired: the kind another browser may continue."""
    try:
        fresh = now - path.stat().st_mtime < _expire_seconds()
        offset, length = int(meta.get("offset") or 0), int(meta.get("length") or 0)
    except (OSError, TypeError, ValueError):
        return False
    return (fresh and meta.get("status") == "uploading" and 0 < offset < length
            and meta.get("owner") == conn.owner
            and (meta.get("app_user_id") is None or meta["app_user_id"] == conn.app_user_id))


def find_partial(conn, name: str, rel: str, length: int, last_modified: int,
                 head_sha256: str) -> "tuple[str, dict] | None":
    """``(upload id, sidecar)`` of the account's unfinished upload of this file furthest along, or
    None. A candidate whose first chunk is not the file's first chunk (``head_sha256``) is another
    file that shares its name, size and modification time: it is passed over and logged. Reads up to
    ``_HEAD_BYTES`` of each candidate: call via a thread."""
    if length <= _HEAD_BYTES:
        return None
    root = upload_storage.uploads(conn) / ".parts"
    try:
        listed = (root / "by-file" / _file_key(name, rel, length, last_modified)).read_text(encoding="utf-8")
    except OSError:
        return None
    now = time.time()
    best = None
    for upload_id in dict.fromkeys(reversed(listed.split())):          # newest first, once each
        try:
            path, part = _paths(conn, upload_id)
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        same_file = (meta.get("length") == length and meta.get("last_modified") == last_modified
                     and (meta.get("rel_path") or "") == rel and (rel or meta.get("name") == name))
        if not same_file or not _unfinished(conn, path, meta, now):
            continue
        if best is not None and best[1]["offset"] >= meta["offset"]:
            continue
        try:
            head = _first_chunk_sha256(part)
        except OSError:
            continue
        if head != head_sha256:
            upload_log.record(conn.workspace, "resume_mismatch", upload_id=upload_id, name=rel or name,
                              offset=meta["offset"], size=length,
                              error="the first 8 MiB differ: another file of the same name, size and "
                                    "modification time; it gets an upload of its own")
            continue
        best = (upload_id, meta)
    return best


def partial_files(conn, folder: str) -> dict[str, int]:
    """Path below ``folder`` -> size, for the account's unfinished uploads into that upload folder
    (see :func:`find_partial` for which count). Reads every sidecar: call via a thread."""
    root = upload_storage.uploads(conn) / ".parts"
    now = time.time()
    found: dict[str, int] = {}
    for path in root.glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rel = str(meta.get("rel_path") or "")
        if meta.get("folder") == folder and rel.startswith(folder + "/") and _unfinished(conn, path, meta, now):
            found[rel[len(folder) + 1:]] = int(meta["length"])
    return found


def unfinished_uploads(conn) -> dict:
    """``{"files": [...], "folders": [...]}``: the account's uploads that :func:`find_partial` offers
    once the same file is chosen again, a folder upload's part-way files grouped under their folder.
    ``name`` is the browser's name for the file, ``rel_path`` and ``folder`` are the server's.
    Reads every sidecar: call via a thread."""
    root = upload_storage.uploads(conn) / ".parts"
    now = time.time()
    files: list[dict] = []
    folders: dict[str, list[dict]] = {}
    for path in root.glob("*.json"):
        if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # find_partial reaches only what create() indexed: a modification time, more than a chunk
        if not _unfinished(conn, path, meta, now) or meta.get("last_modified") is None \
                or int(meta["length"]) <= _HEAD_BYTES:
            continue
        folder, rel = str(meta.get("folder") or ""), str(meta.get("rel_path") or "")
        if folder and not rel.startswith(folder + "/"):
            continue
        item = {"id": path.stem, "url": f"/api/tus/{path.stem}",
                "name": str(meta.get("browser_name") or meta.get("name") or ""),
                "rel_path": rel, "folder": folder, "size": int(meta["length"]), "offset": int(meta["offset"]),
                "created_at": meta.get("created_at"), "conversation_id": str(meta.get("conversation_id") or "")}
        (folders.setdefault(folder, []) if folder else files).append(item)

    def made(item: dict) -> float:
        return item["created_at"] or 0

    return {"files": sorted(files, key=made),
            "folders": [{"folder": name, "size": sum(i["size"] for i in items),
                         "offset": sum(i["offset"] for i in items), "created_at": min(map(made, items)),
                         "files": sorted(items, key=lambda i: i["rel_path"])}
                        for name, items in sorted(folders.items())]}


def _load(request: Request, upload_id: str):
    gw = _gw()
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        return None, None, None, _answer(404)
    # The connection id changes after a gateway restart. The upload belongs to the account's
    # workspace; a currently owned connection to that workspace can resume it.
    preferred = request.headers.get("X-Connection-ID", "")
    connections = list(gw.CONNECTIONS.values())[::-1]
    connections.sort(key=lambda conn: conn.id != preferred)
    for conn in connections:
        path, part = _paths(conn, upload_id)
        if not path.is_file():
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, None, None, _answer(500, body={"error": "upload metadata is damaged"})
        if conn.owner != meta.get("owner") or (meta.get("app_user_id") is not None
                                               and conn.app_user_id != meta["app_user_id"]):
            continue
        gw._owned_conn(conn.id, request)
        if meta.get("status") == "uploading" and time.time() - path.stat().st_mtime >= upload_storage.ttl_seconds() and not _lock(upload_id).locked():
            for item in (part, part.with_suffix(".incoming"), path):
                item.unlink(missing_ok=True)
            return None, None, None, _answer(410, body={"error": "upload expired"})
        return conn, meta, (path, part), None
    return None, None, None, _unattached(request, upload_id)


def _unattached(request: Request, upload_id: str) -> Response:
    """The answer for an upload no live connection reaches: 423 while its sidecar is in the caller's
    workspace, 404 when there is none.

    After a gateway restart, or a disconnect, the account has no connection until the page connects
    again. A 404 then made tus-js-client forget the upload and send the file again from byte 0; on a
    423 it keeps the upload and asks again."""
    gw = _gw()
    from . import dataset_storage
    # Only where this deployment keeps uploads now: a sidecar left in the other layout (from before
    # a data root was configured) is not continued, so it must answer 404 (send again), not a 423
    # the browser would wait on for ever.
    roots = (dataset_storage.data_root() / "temp",) if dataset_storage.configured() else (gw.CONSOLE_RUNS_DIR,)
    paths = [p for root in roots for p in root.glob(f"*/uploads/.parts/{upload_id}.json")]
    user = gw._optional_user(request) if gw._AUTH_ENABLED else None
    if gw._AUTH_ENABLED and user is None:
        return _answer(401, body={"error": "Not authenticated"})
    for path in paths:
        owner = path.parents[2].name
        if user is not None and owner != gw.safe_name(user.username):
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("status") == "uploading" and time.time() - path.stat().st_mtime > upload_storage.ttl_seconds():
            return _answer(410, body={"error": "upload expired"})
        if meta.get("owner") != owner or (gw._AUTH_ENABLED and meta.get("app_user_id") not in (None, user.id)):
            continue
        return _answer(423, headers={"Upload-Offset": str(meta.get("offset", 0)),
                                     "Upload-Length": str(meta.get("length", 0))},
                       body={"error": "this session is not connected: connect to HPC3 again to continue "
                                      "this upload, the server keeps what it received"})
    return _answer(404, body={"error": "unknown upload"})


@router.options("")
@router.options("/")
async def options() -> Response:
    return _answer(204, headers={"Tus-Version": _VERSION,
                                 "Tus-Extension": "creation,termination,checksum",
                                 "Tus-Max-Size": str(_MAX_SIZE),
                                 "Tus-Checksum-Algorithm": "sha256",
                                 "Access-Control-Allow-Methods": "OPTIONS,POST,HEAD,PATCH,DELETE",
                                 "Access-Control-Allow-Headers": "Tus-Resumable,Upload-Length,Upload-Metadata,Upload-Offset,Upload-Checksum,Upload-Final-SHA256,X-Connection-ID,Content-Type"})


@router.post("")
@router.post("/")
async def create(request: Request) -> Response:
    if wrong := _require_version(request):
        return wrong
    gw = _gw()
    try:
        fields = _metadata(request.headers.get("Upload-Metadata", ""))
        length = int(request.headers.get("Upload-Length", ""))
    except (ValueError, TypeError):
        return _answer(400, body={"error": "valid Upload-Length and Upload-Metadata required"})
    if length < 0 or length > _MAX_SIZE:
        return _answer(413, body={"error": "upload exceeds Tus-Max-Size"})
    conn = gw._owned_conn(fields.get("connection_id"), request)
    upload_storage.uploads(conn).mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(upload_storage.uploads(conn)).free < length + 64 * 1024**2:
        upload_log.record(conn.workspace, "refused", name=fields.get("filename"), size=length,
                          error="not enough eyeserver disk space")
        return _answer(507, body={"error": "not enough eyeserver disk space"})
    name = gw.safe_filename(fields.get("filename") or "dataset")
    rel = gw.safe_relpath(fields.get("rel_path") or "")
    folder = gw.safe_relpath(fields.get("folder") or "").split("/")[0]
    if rel and (not folder or rel.split("/")[0] != folder or len(rel.split("/")) < 2):
        return _answer(400, body={"error": "rel_path must be inside the reserved folder"})
    if folder and not (upload_storage.uploads(conn) / folder).is_dir():
        upload_log.record(conn.workspace, "refused", name=rel or name, folder=folder, size=length,
                          error="the folder is not reserved on the server")
        return _answer(404, body={"error": "reserve the folder before uploading its files"})
    unpack = fields.get("unpack", "0") == "1"
    client_sha256 = fields.get("client_sha256", "").lower()
    if client_sha256 and not re.fullmatch(r"[0-9a-f]{64}", client_sha256):
        return _answer(400, body={"error": "client_sha256 must be a SHA-256 hex digest"})
    if unpack and not name.lower().endswith((".zip", ".tar", ".tar.gz", ".tgz")):
        return _answer(400, body={"error": "unpack requires a supported archive"})
    # The browser's File.lastModified (ms). Optional: without it the upload is not offered to
    # another browser (find), and nothing else changes.
    raw_modified = fields.get("last_modified", "")
    last_modified = int(raw_modified) if re.fullmatch(r"-?\d{1,16}", raw_modified) else None
    upload_id = uuid4().hex
    path, part = _paths(conn, upload_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_cleanup_parts, path.parent)
    part.touch(exist_ok=False)
    # browser_name / original_name: the file's name as the browser has it ("my data.bam" is stored as
    # "mydata.bam"), for the card another browser shows (unfinished_uploads) and the dataset's display
    # name; never used as a path. dataset_uuid: the managed-storage folder of a single file.
    dataset_uuid = str(uuid4()) if upload_storage.enabled(conn) and not folder else ""
    meta = {"original_name": os.path.basename(str(fields.get("filename") or ""))[:255] or name,
            "dataset_uuid": dataset_uuid,
            "connection_id": conn.id, "owner": conn.owner, "app_user_id": conn.app_user_id,
            "length": length,
                 "name": name, "rel_path": rel, "folder": folder, "unpack": unpack,
                 "browser_name": os.path.basename(str(fields.get("filename") or ""))[:255] or name,
                 "client_sha256": client_sha256, "last_modified": last_modified,
                 # the chat the upload was started from: its copy and its dataset row keep it
                 "conversation_id": gw.chat_ref(fields.get("conversation_id")),
                 "offset": 0, "job_id": "", "status": "uploading", "created_at": time.time()}
    _save(path, meta)
    if last_modified is not None and length > _HEAD_BYTES:
        _index(path.parent, _file_key(name, rel, length, last_modified), upload_id)
    upload_log.record(conn.workspace, "created", upload_id=upload_id, name=rel or name, folder=folder,
                      size=length, unpack=unpack)
    if length == 0:
        await _finish(conn, meta, path, part)
    return _answer(201, headers={"Location": f"/api/tus/{upload_id}", "Upload-Offset": "0"})


@router.post("/find")
async def find(payload: dict, request: Request) -> Response:
    """The account's unfinished upload of one file, for a browser that has none of its own stored
    (outside the tus protocol). ``{name, rel_path, size, last_modified, head_sha256}`` describe the
    file as :func:`create` received it; ``head_sha256`` is the SHA-256 of its first ``_HEAD_BYTES``.
    Answers ``{"upload": {"url", "offset", "length"}}``, or ``{"upload": null}`` when there is none
    to continue. A POST, so file paths stay out of URLs and access logs."""
    gw = _gw()
    conn = gw._owned_conn(payload.get("connection_id"), request)
    try:
        length, last_modified = int(payload["size"]), int(payload["last_modified"])
    except (KeyError, TypeError, ValueError):
        return _answer(400, body={"error": "size and last_modified are required"})
    head = str(payload.get("head_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", head):
        return _answer(400, body={"error": "head_sha256 must be a SHA-256 hex digest"})
    name = gw.safe_filename(str(payload.get("name") or "") or "dataset")
    rel = gw.safe_relpath(str(payload.get("rel_path") or ""))
    found = await asyncio.to_thread(find_partial, conn, name, rel, length, last_modified, head)
    if found is None:
        return _answer(200, body={"upload": None})
    upload_id, meta = found
    upload_log.record(conn.workspace, "resume_found", upload_id=upload_id, name=rel or name,
                      offset=meta["offset"], size=length,
                      note="a browser without this upload stored continues it")
    return _answer(200, body={"upload": {"url": f"/api/tus/{upload_id}", "offset": meta["offset"],
                                         "length": length}})


# Before GET /{upload_id}, which would take "unfinished" for an upload id.
@router.get("/unfinished")
async def unfinished(request: Request, connection_id: str = "") -> Response:
    """The account's unfinished uploads (:func:`unfinished_uploads`), for a page that has none of them
    stored: it shows each as an "Interrupted" card, and choosing the file or folder again continues
    it through :func:`find` (outside the tus protocol)."""
    conn = _gw()._owned_conn(connection_id, request)
    return _answer(200, body=await asyncio.to_thread(unfinished_uploads, conn))


@router.get("/{upload_id}")
async def result(upload_id: str, request: Request) -> Response:
    """Authenticated completion details for the console (outside the tus protocol)."""
    conn, meta, _paths_, error = _load(request, upload_id)
    if error:
        return error
    gw = _gw()
    rel = meta.get("rel_path", "")
    path = meta.get("storage_path") or (f"{gw._hpc_uploads_dir(conn)}/{rel}" if gw._uploads_on_hpc(conn) else str(upload_storage.uploads(conn) / rel))
    if meta.get("job_id"):
        job = gw._staging_queue(conn).get(meta["job_id"])
        if job is not None:
            path = job.remote_path
    return _answer(200, body={"status": meta["status"], "name": meta.get("original_name") or meta["name"],
                              "path": path, "size": meta["length"], "job_id": meta.get("job_id", ""),
                              "peek": meta.get("peek"), "error": meta.get("error", "")})


@router.head("/{upload_id}")
async def head(upload_id: str, request: Request) -> Response:
    if wrong := _require_version(request):
        return wrong
    conn, meta, paths, error = _load(request, upload_id)
    if error:
        return error
    if busy := _busy(conn, upload_id, meta, "HEAD"):
        return busy
    async with _lock(upload_id):
        if not paths[0].is_file():
            return _answer(404)
        meta = json.loads(paths[0].read_text(encoding="utf-8"))
        if meta["status"] == "failed":
            return _answer(410, body={"error": meta.get("error", "upload failed")})
        if meta["offset"] == meta["length"] and meta["status"] in ("uploading", "finalizing"):
            try:
                await _finish(conn, meta, *paths)
            except (OSError, ValueError) as exc:
                _log_finish_error(conn, upload_id, meta, exc)
                if isinstance(exc, ValueError):
                    _fail_finalization(paths[0], paths[1], meta, exc)
                return _answer(422 if isinstance(exc, ValueError) else 500,
                               body={"error": f"upload finalization failed: {exc}"})
        if 0 < meta["offset"] < meta["length"]:
            upload_log.record(conn.workspace, "resume_asked", upload_id=upload_id,
                              name=meta.get("rel_path") or meta.get("name"), offset=meta["offset"],
                              size=meta["length"])
        headers = {"Upload-Offset": str(meta["offset"]), "Upload-Length": str(meta["length"])}
        if meta.get("job_id"):
            headers["Upload-Staging-Job"] = meta["job_id"]
        return _answer(200, headers=headers)


def _fail_finalization(path: Path, part: Path, meta: dict, exc: ValueError) -> None:
    meta["status"] = "failed"
    meta["error"] = str(exc)
    _save(path, meta)
    part.unlink(missing_ok=True)


def _log_finish_error(conn, upload_id: str, meta: dict, exc: Exception) -> None:
    upload_log.record(conn.workspace, "finalize_failed", upload_id=upload_id,
                      name=meta.get("rel_path") or meta.get("name"), size=meta.get("length"),
                      error=f"{type(exc).__name__}: {exc}",
                      kept=not isinstance(exc, ValueError))   # an OSError keeps the bytes for a retry


async def _finish(conn, meta: dict, path: Path, part: Path) -> None:
    gw = _gw()
    uploads = upload_storage.uploads(conn)
    if meta["status"] == "finalizing" and gw._uploads_queued(conn):
        # Finished before, and the request that did it died before it said so: the queue already
        # holds the file (and may have published it into the dataset folder), so continue that job.
        held = gw._staging_queue(conn).find_upload(path.stem)
        if held is not None and held.state != "cancelled":
            meta["job_id"] = held.id
            meta["status"] = "staging"
            _save(path, meta)
            return
    if meta["status"] == "finalizing":
        rel = meta["rel_path"]
        dest = uploads / rel
    else:
        if meta["rel_path"]:
            rel = meta["rel_path"]
            dest = uploads / rel
            if not dest.resolve().is_relative_to(uploads.resolve()):
                raise ValueError("unsafe upload destination")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise ValueError(f"folder entry already exists: {rel}")
        else:
            if meta.get("dataset_uuid"):
                rel = f"{meta['dataset_uuid']}/{meta['name']}"
                dest = uploads / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
            else:
                rel = await asyncio.to_thread(gw._unique_upload_name, conn, uploads, meta["name"])
                dest = uploads / rel
        meta["rel_path"] = rel
        meta["status"] = "finalizing"
        _save(path, meta)
    if not dest.resolve().is_relative_to(uploads.resolve()):
        raise ValueError("unsafe upload destination")
    source = part if part.exists() else dest
    if not source.is_file():
        raise ValueError("completed upload bytes are missing")
    if source.stat().st_size != meta["length"]:
        raise ValueError("completed upload size mismatch")
    # The staging worker checks this digest in the background (before a copy to HPC3, or before an
    # upload kept here is published). Re-reading a 25 GiB file here would hold the final PATCH past
    # the gateway proxy's 15-second response window.
    if meta["client_sha256"] and not gw._uploads_queued(conn):
        def full_digest() -> str:
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(block)
            return digest.hexdigest()
        if await asyncio.to_thread(full_digest) != meta["client_sha256"]:
            raise ValueError("whole-file SHA-256 mismatch")
    if part.exists():
        os.replace(part, dest)
    meta["status"] = "complete"
    meta["name"] = dest.name if not meta["folder"] else rel
    # Skim single files, matching /api/upload, and a folder's tables (their columns go into the
    # folder's Croissant metadata). Staging itself never blocks this request.
    skim = not meta["folder"] or gw._is_table_name(rel)
    peek = await asyncio.to_thread(gw._peek_local_upload, dest) if skim else None
    meta["peek"] = peek
    if gw._uploads_queued(conn):
        job = gw._submit_staging(conn, dest, rel, name=meta["name"],
                                 kind=gw._file_kind(meta["name"]), folder=meta["folder"],
                                 peek=peek, upload_id=path.stem,
                                 client_sha256=meta["client_sha256"], unpack=meta["unpack"],
                                 conversation_id=meta.get("conversation_id", ""))
        meta["job_id"] = job.id
        meta["status"] = "staging"
    elif not meta["folder"]:
        if gw._AUTH_ENABLED and conn.app_user_id:
            await asyncio.to_thread(gw.auth_routes.record_dataset, conn.app_user_id,
                                    meta.get("original_name") or dest.name, str(dest),
                                    dest.stat().st_size, gw._file_kind(dest.name),
                                    conversation_id=meta.get("conversation_id") or None,
                                    sha256=meta["client_sha256"] or None,
                                    dataset_uuid=meta.get("dataset_uuid") or None, peek=peek)
        conn.emit("success", "upload", f"Uploaded {dest.name} to the server.")
    _save(path, meta)
    seconds = max(0.0, time.time() - float(meta.get("created_at") or time.time()))
    upload_log.record(conn.workspace, "received", upload_id=path.stem, name=rel, folder=meta["folder"],
                      size=meta["length"], seconds=seconds,
                      mb_s=meta["length"] / 1e6 / seconds if seconds >= 1 else None,
                      sha256="browser" if meta["client_sha256"] else "",
                      next=("kept on the server" if not meta["job_id"] or gw._uploads_kept_here(conn)
                            else "copy to HPC3"),
                      job_id=meta["job_id"])


@router.patch("/{upload_id}")
async def patch(upload_id: str, request: Request) -> Response:
    if wrong := _require_version(request):
        return wrong
    if request.headers.get("Content-Type", "").split(";")[0].strip() != "application/offset+octet-stream":
        return _answer(415, body={"error": "Content-Type must be application/offset+octet-stream"})
    conn, meta, paths, error = _load(request, upload_id)
    if error:
        return error
    path, part = paths
    try:
        wanted = int(request.headers.get("Upload-Offset", ""))
    except ValueError:
        return _answer(400, body={"error": "Upload-Offset required"})
    if busy := _busy(conn, upload_id, meta, "PATCH"):
        return busy
    async with _lock(upload_id):
        if not path.is_file():
            return _answer(404)
        meta = json.loads(path.read_text(encoding="utf-8"))
        offset = int(meta["offset"])
        if wanted != offset or meta["status"] != "uploading":
            upload_log.record(conn.workspace, "chunk_refused", upload_id=upload_id,
                              name=meta.get("rel_path") or meta.get("name"), offset=offset,
                              error=(f"the browser sent offset {wanted}, the server has {offset}"
                                     if wanted != offset else f"the upload is {meta['status']}"))
            return _answer(409, headers={"Upload-Offset": str(offset)})
        checksum = request.headers.get("Upload-Checksum", "")
        if checksum:
            algorithm, _, encoded = checksum.partition(" ")
            if algorithm != "sha256" or not encoded:
                return _answer(400, body={"error": "only sha256 Upload-Checksum is supported"})
            try:
                expected = base64.b64decode(encoded, validate=True)
            except binascii.Error:
                return _answer(400, body={"error": "invalid Upload-Checksum"})
            if len(expected) != 32:
                return _answer(400, body={"error": "invalid sha256 digest"})
        else:
            expected = None
        # Stage one PATCH in a sidecar. A bad checksum or interrupted request never advances
        # the persisted offset or modifies the assembled file.
        incoming = part.with_suffix(".incoming")
        digest = hashlib.sha256()
        count = 0
        try:
            with incoming.open("wb") as out:
                async for block in _blocks(request):
                    count += len(block)
                    if offset + count > meta["length"]:
                        return _answer(413, headers={"Upload-Offset": str(offset)})
                    await asyncio.to_thread(_write_chunk, out, digest, block)
            if expected is not None and digest.digest() != expected:
                upload_log.record(conn.workspace, "chunk_refused", upload_id=upload_id,
                                  name=meta.get("rel_path") or meta.get("name"), offset=offset,
                                  bytes=count, error="chunk checksum mismatch (SHA-256)")
                return _answer(460, headers={"Upload-Offset": str(offset)},
                               body={"error": "chunk checksum mismatch"})
            final_sha = request.headers.get("Upload-Final-SHA256", "").lower()
            if final_sha:
                if not re.fullmatch(r"[0-9a-f]{64}", final_sha) or offset + count != meta["length"]:
                    return _answer(400, body={"error": "Upload-Final-SHA256 requires the final PATCH and a hex digest"})
                if meta["client_sha256"] and meta["client_sha256"] != final_sha:
                    return _answer(409, body={"error": "whole-file digest changed"})
                meta["client_sha256"] = final_sha
            await asyncio.to_thread(_append_chunk, part, incoming)
            meta["offset"] = offset + count
            if meta.get("folder") and upload_storage.enabled(conn):
                upload_storage.session_path(conn, meta["folder"]).touch()
            _save(path, meta)
            if meta["offset"] == meta["length"]:
                try:
                    await _finish(conn, meta, path, part)
                except (OSError, ValueError) as exc:
                    _log_finish_error(conn, upload_id, meta, exc)
                    if isinstance(exc, ValueError):
                        _fail_finalization(path, part, meta, exc)
                    return _answer(422 if isinstance(exc, ValueError) else 500,
                                   headers={"Upload-Offset": str(meta["offset"])},
                                   body={"error": f"upload received but staging failed: {exc}"})
            headers = {"Upload-Offset": str(meta["offset"])}
            if meta.get("job_id"):
                headers["Upload-Staging-Job"] = meta["job_id"]
            return _answer(204, headers=headers)
        except (_Stalled, ClientDisconnect) as exc:       # only the body read raises these
            stalled = isinstance(exc, _Stalled)
            upload_log.record(conn.workspace, "chunk_dropped", upload_id=upload_id,
                              name=meta.get("rel_path") or meta.get("name"), offset=offset, bytes=count,
                              error=(f"no bytes for {_IDLE_SECONDS:g} s: the browser's network path is gone"
                                     if stalled else "the browser closed the connection"))
            return _answer(408 if stalled else 400, headers={"Upload-Offset": str(offset), "Connection": "close"})
        finally:
            incoming.unlink(missing_ok=True)


def _write_chunk(out, digest, block: bytes) -> None:
    out.write(block)
    digest.update(block)


def _append_chunk(part: Path, incoming: Path) -> None:
    with part.open("ab") as out, incoming.open("rb") as source:
        shutil.copyfileobj(source, out, 1024 * 1024)


@router.delete("/{upload_id}")
async def delete(upload_id: str, request: Request) -> Response:
    if wrong := _require_version(request):
        return wrong
    conn, meta, paths, error = _load(request, upload_id)
    if error:
        return error
    path, part = paths
    if busy := _busy(conn, upload_id, meta, "DELETE"):
        return busy
    async with _lock(upload_id):
        if not path.is_file():
            return _answer(404)
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("job_id"):
            _gw()._staging_queue(conn).cancel(meta["job_id"])
        elif meta.get("status") != "complete":
            part.unlink(missing_ok=True)
            if meta.get("rel_path"):
                target = upload_storage.uploads(conn) / meta["rel_path"]
                if target.resolve().is_relative_to(upload_storage.uploads(conn).resolve()):
                    target.unlink(missing_ok=True)
        part.with_suffix(".incoming").unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        upload_log.record(conn.workspace, "discarded", upload_id=upload_id,
                          name=meta.get("rel_path") or meta.get("name"), offset=meta.get("offset"),
                          size=meta.get("length"), job_id=meta.get("job_id"))
    with _locks_guard:
        _locks.pop(upload_id, None)
    return _answer(204)
