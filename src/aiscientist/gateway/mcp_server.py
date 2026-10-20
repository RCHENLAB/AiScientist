"""MCP server: an account's datasets and results, described by their Croissant metadata.

RCHENLAB/AiScientist issue 9. An MCP client — an assistant in an IDE, Claude Code, a script — reads
what a dataset contains, where it came from and how it may be reused from its ``croissant.json``,
instead of guessing from file names or opening multi-GB files.

Transport: MCP "Streamable HTTP" at ``POST /mcp``, answered with ``application/json`` (the server
never opens an SSE stream, so ``GET /mcp`` is 405). Stateless: every request carries its own
credentials and no ``Mcp-Session-Id`` is issued. Implemented here, over the gateway's own FastAPI,
because the five tools and two resource kinds need nothing the protocol's SDK would add.

Permissions are the web interface's: a request acts as ONE account, found from a personal access
token (``Authorization: Bearer <token>``, made in Files → Datasets) or the console's session
cookie, and every lookup below is filtered on that account. A disabled account or a revoked token
gets 401. A browser page of another origin is refused (403), against DNS rebinding.

Tools: ``list_datasets``, ``search_datasets``, ``get_dataset_metadata``, ``get_provenance``,
``preview_records``. Resources: ``aiscientist://datasets/<uuid>/croissant.json`` and
``aiscientist://results/<uuid>/croissant.json``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import metadata_store as store

router = APIRouter()

#: Newest first. A client asking for one of these gets it back; any other gets the newest.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "aiscientist"

_INSTRUCTIONS = (
    "AiScientist datasets and analysis results of ONE account, described by Croissant 1.1 metadata "
    "(JSON-LD on schema.org). Identify a dataset or result by its UUID. Start with list_datasets or "
    "search_datasets; read a full croissant.json with get_dataset_metadata; follow lineage (which "
    "results used a dataset, what a result was made from) with get_provenance; look at a few rows of "
    "a table with preview_records. Values whose provenance says 'drafted' were inferred "
    "automatically and not yet reviewed by the owner: check their confidence before relying on them. "
    "Cite inputs by name and UUID, and by their accession when one is given.")

# JSON-RPC error codes
_PARSE_ERROR, _INVALID_REQUEST, _METHOD_NOT_FOUND, _INVALID_PARAMS, _INTERNAL = (
    -32700, -32600, -32601, -32602, -32603)


class ToolError(Exception):
    """A tool call that cannot be answered: reported to the model as ``isError`` content, which is
    how MCP wants tool failures said (a JSON-RPC error is for a malformed request)."""


# --- the tools --------------------------------------------------------------------------------------

_ID_PROP = {"type": "string", "description": "The dataset or result UUID (a 'urn:uuid:' prefix is accepted)."}
_KIND_PROP = {"type": "string", "enum": ["dataset", "result", "all"], "default": "all",
              "description": "Uploaded/saved datasets, analysis results, or both."}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_datasets",
        "title": "List datasets and results",
        "description": "List this account's datasets and analysis results, newest first: UUID, name, "
                       "kind, formats, organism/tissue/assay where known, size and review status.",
        "inputSchema": {"type": "object", "properties": {
            "kind": _KIND_PROP,
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            "offset": {"type": "integer", "minimum": 0, "default": 0}}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "search_datasets",
        "title": "Search datasets and results",
        "description": "Find datasets and results by free text (name, description, keywords, file "
                       "formats, column names) and/or by organism, assay, tissue or accession (e.g. "
                       "GSM5676874, GSE123456, SRR…, PRJNA…). Terms match labels or ontology IDs "
                       "(NCBITaxon:10090, UBERON:0000966, EFO:0009922). All given filters must match.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "organism": {"type": "string"}, "assay": {"type": "string"},
            "tissue": {"type": "string"}, "accession": {"type": "string"}, "kind": _KIND_PROP,
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_dataset_metadata",
        "title": "Get a croissant.json",
        "description": "The full Croissant 1.1 metadata (croissant.json) of one dataset or result, plus "
                       "per-field provenance (where each value came from) and confidence, and whether "
                       "the owner has reviewed it.",
        "inputSchema": {"type": "object", "properties": {"id": _ID_PROP}, "required": ["id"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_provenance",
        "title": "Get lineage",
        "description": "The lineage of a dataset or result as a graph: 'upstream' = what it was made "
                       "from (input datasets, earlier results, the run, pipeline, version, parameters "
                       "and models), 'downstream' = which results used it, or both.",
        "inputSchema": {"type": "object", "properties": {
            "id": _ID_PROP,
            "direction": {"type": "string", "enum": ["upstream", "downstream", "both"], "default": "both"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3}},
            "required": ["id"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "preview_records",
        "title": "Preview records",
        "description": "A few rows of one record set (a table) of a dataset or result, read with "
                       "mlcroissant from the head of the file. Data kept on HPC3 needs the account's "
                       "HPC3 session to be connected.",
        "inputSchema": {"type": "object", "properties": {
            "id": _ID_PROP,
            "record_set": {"type": "string", "description": "Its name or @id; default: the first."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
            "required": ["id"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


def _limit(args: dict, key: str, default: int, top: int) -> int:
    try:
        return max(1, min(top, int(args.get(key) or default)))
    except (TypeError, ValueError):
        return default


def _kind(args: dict) -> "str | None":
    kind = str(args.get("kind") or "all")
    if kind not in ("dataset", "result", "all"):
        raise ToolError("kind must be 'dataset', 'result' or 'all'")
    return None if kind == "all" else kind


def _require_id(args: dict) -> str:
    ref = str(args.get("id") or "").strip()
    if not ref:
        raise ToolError("id is required: a dataset or result UUID")
    return ref


def _tool_list(user, args: dict) -> dict:
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    items, total = store.list_summaries(user.id, kind=_kind(args), limit=_limit(args, "limit", 50, 200),
                                        offset=offset)
    return {"items": items, "total": total, "offset": offset}


def _tool_search(user, args: dict) -> dict:
    filters = {k: str(args.get(k) or "").strip() for k in ("query", "organism", "assay", "tissue", "accession")}
    items = store.search(user.id, kind=_kind(args), limit=_limit(args, "limit", 20, 200), **filters)
    return {"items": items, "count": len(items)}


def _tool_metadata(user, args: dict) -> dict:
    view = store.metadata_view(user.id, _require_id(args))
    if view is None:
        raise ToolError("No dataset or result with that id in this account.")
    return view


def _tool_provenance(user, args: dict) -> dict:
    direction = str(args.get("direction") or "both")
    if direction not in ("upstream", "downstream", "both"):
        raise ToolError("direction must be 'upstream', 'downstream' or 'both'")
    graph = store.lineage(user.id, _require_id(args), direction=direction,
                          depth=_limit(args, "depth", 3, 10))
    if graph is None:
        raise ToolError("No dataset or result with that id in this account.")
    return graph


def _tool_preview(user, args: dict) -> dict:
    from . import metadata_preview
    try:
        return metadata_preview.preview(user, _require_id(args), str(args.get("record_set") or ""),
                                        _limit(args, "limit", 10, 50))
    except metadata_preview.PreviewError as exc:
        raise ToolError(str(exc)) from exc


_HANDLERS: dict[str, Callable[[Any, dict], dict]] = {
    "list_datasets": _tool_list,
    "search_datasets": _tool_search,
    "get_dataset_metadata": _tool_metadata,
    "get_provenance": _tool_provenance,
    "preview_records": _tool_preview,
}


def call_tool(user, name: str, args: "dict | None") -> dict:
    """One ``tools/call`` result. Also what the in-process callers use (same checks, same output)."""
    handler = _HANDLERS.get(name)
    if handler is None:
        raise KeyError(name)
    try:
        out = handler(user, dict(args or {}))
    except ToolError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}],
            "structuredContent": out, "isError": False}


# --- resources --------------------------------------------------------------------------------------

_RESOURCE_PREFIX = "aiscientist://"
_TEMPLATES = [
    {"uriTemplate": "aiscientist://datasets/{uuid}/croissant.json", "name": "dataset-croissant",
     "title": "Dataset metadata (Croissant)", "mimeType": "application/ld+json",
     "description": "The croissant.json of one dataset of this account."},
    {"uriTemplate": "aiscientist://results/{uuid}/croissant.json", "name": "result-croissant",
     "title": "Analysis result metadata (Croissant)", "mimeType": "application/ld+json",
     "description": "The croissant.json of one analysis result, with its provenance."},
]
_PAGE = 100


def resource_uri(kind: str, uuid: str) -> str:
    return f"{_RESOURCE_PREFIX}{'results' if kind == 'result' else 'datasets'}/{uuid}/croissant.json"


def _parse_resource_uri(uri: str) -> "tuple[str, str] | None":
    if not uri.startswith(_RESOURCE_PREFIX):
        return None
    parts = uri[len(_RESOURCE_PREFIX):].split("/")
    if len(parts) != 3 or parts[2] != "croissant.json" or parts[0] not in ("datasets", "results"):
        return None
    return ("result" if parts[0] == "results" else "dataset"), parts[1]


def _resources_list(user, params: dict) -> dict:
    try:
        offset = max(0, int(params.get("cursor") or 0))
    except (TypeError, ValueError):
        offset = 0
    items, total = store.list_summaries(user.id, kind=None, limit=_PAGE, offset=offset)
    out: dict[str, Any] = {"resources": [{
        "uri": resource_uri(it["kind"], it["uuid"]), "name": it["name"], "title": it["name"],
        "mimeType": "application/ld+json",
        "description": f"Croissant metadata of the {it['kind']} {it['name']}"} for it in items]}
    if offset + len(items) < total:
        out["nextCursor"] = str(offset + len(items))
    return out


def _resources_read(user, params: dict) -> dict:
    uri = str(params.get("uri") or "")
    parsed = _parse_resource_uri(uri)
    if parsed is None:
        raise _RpcError(_INVALID_PARAMS, f"Unknown resource: {uri}")
    kind, uuid = parsed
    doc = store.document_for(user.id, uuid, kind=kind)
    if doc is None:
        # -32002 is the code the MCP spec names for a resource that is not found.
        raise _RpcError(-32002, "Resource not found", {"uri": uri})
    return {"contents": [{"uri": uri, "mimeType": "application/ld+json",
                          "text": json.dumps(doc, indent=2, ensure_ascii=False)}]}


# --- JSON-RPC -----------------------------------------------------------------------------------------


class _RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _server_version() -> str:
    try:
        from importlib.metadata import version
        return version("aiscientist")
    except Exception:  # noqa: BLE001 - an uninstalled checkout
        return "0"


def _dispatch(user, method: str, params: dict) -> Any:
    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        return {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False},
                                 "resources": {"listChanged": False, "subscribe": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "AiScientist datasets",
                               "version": _server_version()},
                "instructions": _INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise _RpcError(_INVALID_PARAMS, "arguments must be an object")
        try:
            return call_tool(user, name, args)
        except KeyError:
            raise _RpcError(_INVALID_PARAMS, f"Unknown tool: {name}") from None
    if method == "resources/list":
        return _resources_list(user, params)
    if method == "resources/templates/list":
        return {"resourceTemplates": _TEMPLATES}
    if method == "resources/read":
        return _resources_read(user, params)
    raise _RpcError(_METHOD_NOT_FOUND, f"Method not found: {method}")


def handle_message(user, msg: Any) -> "dict | None":
    """Answer one JSON-RPC message; None for a notification (nothing is sent back for it)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
            return None                       # a response to a request we never send: ignore it
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": _INVALID_REQUEST, "message": "Invalid Request"}}
    has_id = "id" in msg
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    try:
        result = _dispatch(user, msg["method"], params)
    except _RpcError as exc:
        if not has_id:
            return None
        err: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            err["data"] = exc.data
        return {"jsonrpc": "2.0", "id": msg["id"], "error": err}
    except Exception as exc:  # noqa: BLE001 - one bad call must not take the endpoint down
        print(f"[mcp] {msg.get('method')} failed: {type(exc).__name__}: {exc}")
        if not has_id:
            return None
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": _INTERNAL, "message": "Internal error"}}
    if not has_id or msg["method"].startswith("notifications/"):
        return None
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


# --- HTTP ---------------------------------------------------------------------------------------------


def _foreign_origin(request: Request) -> bool:
    """True for a browser request from another site. MCP clients outside a browser send no Origin."""
    origin = request.headers.get("origin")
    if not origin:
        return False
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    try:
        return (urlparse(origin).netloc or "").lower() != host.lower()
    except ValueError:
        return True


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=401,
                        headers={"WWW-Authenticate": 'Bearer realm="aiscientist-mcp"'})


@router.post("/mcp")
async def mcp_post(request: Request) -> Response:
    if _foreign_origin(request):
        return JSONResponse({"error": "Origin not allowed"}, status_code=403)
    user = await asyncio.to_thread(store.authenticate, request)
    if user is None:
        return _unauthorized("Sign in, or send 'Authorization: Bearer <token>' with a personal access "
                             "token made in Files → Datasets.")
    try:
        payload = json.loads(await request.body() or b"null")
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": _PARSE_ERROR, "message": "Parse error"}}, status_code=400)
    if isinstance(payload, list):           # a batch (protocol 2025-03-26)
        if not payload:
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": _INVALID_REQUEST, "message": "Empty batch"}},
                                status_code=400)
        answers = [a for a in [await asyncio.to_thread(handle_message, user, m) for m in payload] if a]
        return JSONResponse(answers) if answers else Response(status_code=202)
    answer = await asyncio.to_thread(handle_message, user, payload)
    if answer is None:
        return Response(status_code=202)    # a notification or a response: accepted, nothing to say
    return JSONResponse(answer)


@router.get("/mcp")
async def mcp_get() -> Response:
    """No server-initiated stream is offered: everything is a request and its response."""
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
async def mcp_delete() -> Response:
    """Stateless: there is no session to end."""
    return Response(status_code=405, headers={"Allow": "POST"})
