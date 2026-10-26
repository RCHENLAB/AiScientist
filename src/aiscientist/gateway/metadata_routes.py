"""HTTP API for dataset and result metadata (RCHENLAB/AiScientist issue 9).

Mounted beside :mod:`.auth_routes` when accounts are on. Every endpoint acts for the logged-in
account only (the same cookie and the same filter as ``/api/datasets``):

* ``GET  /api/datasets/{ref}/metadata``       the review form, the croissant.json, provenance,
                                              confidence and validation of one dataset
* ``PUT  /api/datasets/{ref}/metadata``       save the review form; the croissant.json is rebuilt
* ``GET  /api/datasets/{ref}/croissant.json`` the document itself
* ``GET  /api/results/{ref}/metadata``        a run's result (``ref`` = run id or result UUID)
* ``GET  /api/results/{ref}/croissant.json``
* ``GET  /api/metadata/{ref}/lineage``        upstream / downstream graph
* ``GET  /api/metadata/vocabulary``           the form's ontology choices and licenses
* ``GET/POST/DELETE /api/account/tokens``     personal access tokens for MCP clients

``ref`` is a dataset's integer id or its UUID (``urn:uuid:`` accepted).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .. import metadata as md
from . import metadata_store as store
from .auth_routes import require_user

router = APIRouter(prefix="/api")


def _ld_json(doc: dict, filename: str) -> Response:
    return Response(json.dumps(doc, indent=2, ensure_ascii=False), media_type="application/ld+json",
                    headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/metadata/vocabulary")
def vocabulary() -> dict:
    return md.vocabulary()


@router.get("/datasets/{ref}/metadata")
def dataset_metadata(ref: str, request: Request) -> dict:
    user = require_user(request)
    view = store.metadata_view(user.id, ref, kind="dataset", with_form=True)
    if view is None:
        store.ensure_dataset_records(user.id)        # a row older than this feature: draft it now
        view = store.metadata_view(user.id, ref, kind="dataset", with_form=True)
    if view is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return view


@router.put("/datasets/{ref}/metadata")
async def save_dataset_metadata(ref: str, request: Request) -> dict:
    user = require_user(request)
    try:
        fields = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Send the form as a JSON object.") from None
    if not isinstance(fields, dict):
        raise HTTPException(status_code=400, detail="Send the form as a JSON object.")
    try:
        view = store.save_form(user.id, ref, fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if view is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return view


@router.get("/datasets/{ref}/croissant.json")
def dataset_croissant(ref: str, request: Request) -> Response:
    user = require_user(request)
    doc = store.document_for(user.id, ref, kind="dataset")
    if doc is None:
        store.ensure_dataset_records(user.id)
        doc = store.document_for(user.id, ref, kind="dataset")
    if doc is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return _ld_json(doc, "croissant.json")


@router.get("/results/{ref}/metadata")
def result_metadata(ref: str, request: Request) -> dict:
    user = require_user(request)
    view = store.metadata_view(user.id, ref, kind="result")
    if view is None:
        raise HTTPException(status_code=404, detail="No metadata for that result.")
    return view


@router.get("/results/{ref}/croissant.json")
def result_croissant(ref: str, request: Request) -> Response:
    user = require_user(request)
    doc = store.document_for(user.id, ref, kind="result")
    if doc is None:
        raise HTTPException(status_code=404, detail="No metadata for that result.")
    return _ld_json(doc, "croissant.json")


@router.get("/metadata/{ref}/lineage")
def metadata_lineage(ref: str, request: Request, direction: str = "both", depth: int = 3) -> dict:
    user = require_user(request)
    if direction not in ("upstream", "downstream", "both"):
        raise HTTPException(status_code=400, detail="direction: upstream, downstream or both")
    graph = store.lineage(user.id, ref, direction=direction, depth=max(1, min(10, depth)))
    if graph is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return graph


# --- personal access tokens -------------------------------------------------------------------------


class TokenCreate(BaseModel):
    name: str = "MCP client"


@router.get("/account/tokens")
def my_tokens(request: Request) -> dict:
    user = require_user(request)
    return {"tokens": store.list_tokens(user.id)}


@router.post("/account/tokens")
def new_token(req: TokenCreate, request: Request) -> JSONResponse:
    user = require_user(request)
    if len(store.list_tokens(user.id)) >= 20:
        raise HTTPException(status_code=400, detail="At most 20 tokens: revoke one first.")
    token, row = store.create_token(user.id, req.name)
    # The token is in this answer only: it is not stored, so it cannot be shown again.
    return JSONResponse({"token": token, **row}, headers={"Cache-Control": "no-store"})


@router.delete("/account/tokens/{token_id}")
def drop_token(token_id: int, request: Request) -> dict:
    user = require_user(request)
    if not store.revoke_token(user.id, token_id):
        raise HTTPException(status_code=404, detail="Token not found.")
    return {"ok": True}
