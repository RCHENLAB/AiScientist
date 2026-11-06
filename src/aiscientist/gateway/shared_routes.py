"""HTTP API for shared datasets (RCHENLAB/AiScientist issue 92): read-only, for every logged-in account.

Mounted beside :mod:`.auth_routes` when accounts are on.

* ``GET /api/shared-datasets``                                   every shared dataset and its versions
* ``GET /api/shared-datasets/{slug}?version=latest``            one version: files, facets, changelog
* ``GET /api/shared-datasets/{slug}/versions/{label}/manifest.json``  the version's manifest

No answer carries a server path: files are named relative to their version. Publishing and
withdrawing are done with ``aiscientist-admin shared-dataset`` (see ``gateway/shared_datasets.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from . import shared_datasets as sd
from .auth_routes import require_user

router = APIRouter(prefix="/api/shared-datasets")


@router.get("")
def list_shared_datasets(request: Request) -> dict:
    user = require_user(request)
    if sd.store_root() is None:
        return {"enabled": False, "datasets": []}
    return {"enabled": True, "datasets": sd.dataset_views(user)}


@router.get("/{slug}")
def shared_dataset(slug: str, request: Request, version: str = sd.LATEST) -> dict:
    require_user(request)
    if sd.store_root() is None:
        raise HTTPException(status_code=404, detail="Shared datasets are not enabled on this server.")
    try:
        return sd.version_view(slug, version)
    except sd.SharedDatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


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
