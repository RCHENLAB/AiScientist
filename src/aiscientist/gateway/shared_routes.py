"""HTTP API for shared datasets (RCHENLAB/AiScientist issue 92): read-only, for every logged-in account.

Mounted beside :mod:`.auth_routes` when accounts are on.

* ``GET /api/shared-datasets``                                   every shared dataset and its versions
* ``GET /api/shared-datasets/{slug}?version=latest``            one version: files, facets, changelog
* ``GET /api/shared-datasets/{slug}/versions/{label}/manifest.json``  the version's manifest

No answer carries a server path: files are named relative to their version. Publishing and
withdrawing are done with ``aiscientist-admin shared-dataset`` (see ``gateway/shared_datasets.py``).
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
import uuid as _uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import shared_datasets as sd
from .auth_routes import _owned_conversation, require_user
from .dataset_limits import MAX_BOUND_DATASETS
from .db import session_scope

router = APIRouter(prefix="/api/shared-datasets")
chat_router = APIRouter(prefix="/api/conversations")


@router.get("")
def list_shared_datasets(request: Request) -> dict:
    user = require_user(request)
    if sd.store_root() is None:
        return {"enabled": False, "datasets": []}
    views = sd.dataset_views(user)
    out: dict = {"enabled": True, "datasets": views}
    if any(v["can_publish"] for v in views):
        # For the maintainers' and admins' publish form only: where a version's source folder may be.
        from . import server_data
        out["source_roots"] = server_data.display_roots()
    return out


@router.get("/{slug}")
def shared_dataset(slug: str, request: Request, version: str = sd.LATEST) -> dict:
    require_user(request)
    if sd.store_root() is None:
        raise HTTPException(status_code=404, detail="Shared datasets are not enabled on this server.")
    try:
        return sd.version_view(slug, version)
    except sd.SharedDatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.get("/{slug}/versions/{label}/croissant.json")
def shared_dataset_croissant(slug: str, label: str, request: Request) -> JSONResponse:
    """The version's Croissant metadata (written into the version folder at publish)."""
    require_user(request)
    if sd.store_root() is None:
        raise HTTPException(status_code=404, detail="Shared datasets are not enabled on this server.")
    try:
        doc = sd.version_croissant(slug, label)
    except sd.SharedDatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return JSONResponse(doc, media_type="application/ld+json")


@router.get("/{slug}/versions/{label}/manifest.json")
def shared_dataset_manifest(slug: str, label: str, request: Request) -> FileResponse:
    require_user(request)
    if sd.store_root() is None:
        raise HTTPException(status_code=404, detail="Shared datasets are not enabled on this server.")
    try:
        path = sd.manifest_file(slug, label)
    except sd.SharedDatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return FileResponse(path, media_type="application/json",
                        filename=f"{slug}-{path.parent.name}-manifest.json",
                        content_disposition_type="inline")


class SharedAttachment(BaseModel):
    shared: str
    version: str = sd.LATEST
    files: list[str] | None = None
    role: str | None = "reference"


class SharedAttachmentsPut(BaseModel):
    shared_datasets: list[SharedAttachment]


@chat_router.put("/{cid}/shared-datasets")
def replace_conversation_shared_datasets(cid: int, req: SharedAttachmentsPut, request: Request) -> dict:
    """Replace the chat's shared-dataset attachments, saved apart from the account's own datasets. The
    browser sends references, never paths; a whole version is one attachment toward the 12-per-chat limit."""
    user = require_user(request)
    if sd.store_root() is None:
        raise HTTPException(status_code=404, detail="Shared datasets are not enabled on this server.")
    with session_scope() as s:
        conv = _owned_conversation(s, cid, user.id)
        s.refresh(conv, with_for_update=True)
        try:
            views = sd.replace_attachments(
                s, cid, [a.model_dump() for a in req.shared_datasets],
                other_attached=len(conv.datasets), limit=MAX_BOUND_DATASETS)
        except sd.SharedDatasetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        conv.updated_at = _dt.datetime.now(_dt.timezone.utc)
        s.commit()
        return {"shared_datasets": views, "max_bound_datasets": MAX_BOUND_DATASETS}


# --- publishing and withdrawing from the console (maintainers and admins) ----------------------------


class _Job:
    """One background publish: its progress lines, then its result or the error."""

    def __init__(self, slug: str, label: str, user: str) -> None:
        self.id, self.slug, self.label, self.user = _uuid.uuid4().hex[:12], slug, label, user
        self.state, self.lines, self.result, self.error = "running", [], None, None
        self.started = time.time()

    def view(self) -> dict:
        return {"job": self.id, "slug": self.slug, "label": self.label, "state": self.state,
                "lines": self.lines[-200:], "result": self.result, "error": self.error}


_JOBS: dict[str, _Job] = {}
_JOBS_LOCK = threading.Lock()


class PublishRequest(BaseModel):
    source: str
    label: str
    changelog: str


def _run_publish(job: _Job, source: str, changelog: str) -> None:
    try:
        job.lines.append(f"Publishing {job.slug} {job.label}…")
        job.result = sd.publish(job.slug, job.label, source, by=job.user, changelog=changelog,
                                log=lambda line: job.lines.append(line.strip()))
        job.state = "done"
        job.lines.append("Published.")
    except sd.SharedDatasetError as exc:
        job.state, job.error = "error", str(exc)
    except Exception as exc:  # noqa: BLE001 - the job reports it; the version stays 'publishing' and can be published again
        job.state, job.error = "error", f"{type(exc).__name__}: {exc}"


def _require_publisher(user, slug: str) -> None:
    if sd.store_root() is None:
        raise HTTPException(status_code=404, detail="Shared datasets are not enabled on this server.")
    if not sd.can_publish(user, slug):
        raise HTTPException(status_code=403, detail="Only this dataset's maintainers and admins can publish or withdraw its versions.")


@router.post("/{slug}/publish", status_code=202)
def publish_shared_version(slug: str, req: PublishRequest, request: Request) -> dict:
    """Start publishing a version as a background job (``sd.publish``); poll the job for its progress."""
    user = require_user(request)
    _require_publisher(user, slug)
    try:
        label = sd.check_label(req.label.strip())
        sd.check_slug(slug)
    except sd.SharedDatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not req.changelog.strip():
        raise HTTPException(status_code=400, detail="A version needs a changelog: what changed and why.")
    if not req.source.strip():
        raise HTTPException(status_code=400, detail="Name the folder that holds the version's files.")
    with _JOBS_LOCK:
        if any(j.slug == slug and j.state == "running" for j in _JOBS.values()):
            raise HTTPException(status_code=409, detail=f"A version of {slug} is already being published.")
        for old in [k for k, j in _JOBS.items() if j.state != "running" and time.time() - j.started > 86400]:
            del _JOBS[old]
        job = _Job(slug, label, user.username)
        _JOBS[job.id] = job
    threading.Thread(target=_run_publish, args=(job, req.source.strip(), req.changelog), daemon=True,
                     name=f"publish-{slug}-{label}").start()
    return {"job": job.id, **job.view()}


@router.get("/{slug}/publish-jobs/{job_id}")
def publish_job(slug: str, job_id: str, request: Request) -> dict:
    user = require_user(request)
    _require_publisher(user, slug)
    job = _JOBS.get(job_id)
    if job is None or job.slug != slug:
        raise HTTPException(status_code=404, detail="No such publish job (the server may have restarted: "
                                                    "publish again, the label is reused until it is published).")
    return job.view()


@router.post("/{slug}/versions/{label}/withdraw")
def withdraw_shared_version(slug: str, label: str, request: Request) -> dict:
    """Stop offering a version. Its files stay, so chats pinned to it still run."""
    user = require_user(request)
    _require_publisher(user, slug)
    try:
        return sd.withdraw(slug, label, by=user.username)
    except sd.SharedDatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
