"""Pinned MCP tool catalog and one-shot client, with no MCP dependency in the gateway.

The released common CLI owns stdio inside the image. Reviewed tools/list snapshots
let the planner start offline; server descriptions and results are data, never code
or execution policy. Domain adapters own dataset restrictions and deliverables.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
LOG = logging.getLogger(__name__)
NAME = r"[a-zA-Z_][a-zA-Z0-9_-]{0,63}"

# Runs inside the pinned image. The common CLI returns structured JSON or a list
# of text blocks; an MCP is_error exits nonzero before any collector can run.
READ_RESULT = '''import json, os
from pathlib import Path
result = json.loads(Path(os.environ["AISCI_MCP_RESULT"]).read_text())
if isinstance(result, list):
    if not all(isinstance(item, str) for item in result):
        raise ValueError("MCP text blocks must be strings")
    try:
        result = json.loads("".join(result))
    except ValueError:
        result = {"content": [{"type": "text", "text": item} for item in result]}
if not isinstance(result, dict):
    raise ValueError("MCP result must be an object or text blocks")
'''

JSON_COLLECT = READ_RESULT + '''
dest = Path(os.environ["AISCIENTIST_ARTIFACTS"]) / "mcp" / os.environ["AISCI_MCP_SERVER"]
dest.mkdir(parents=True, exist_ok=True)
full = dest / (os.environ["AISCI_MCP_TOOL"] + ".json")
full.write_text(json.dumps(result))
if len(json.dumps(result)) > 14000:
    result = {"response_omitted": True, "full_result": "mcp/" + os.environ["AISCI_MCP_SERVER"] + "/" + full.name}
print(json.dumps({"result": result}, separators=(",", ":")))
'''


@dataclass(frozen=True)
class PreparedCall:
    arguments: dict
    setup: str = ""
    collector: str = JSON_COLLECT
    tool_cache: bool = False
    context: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Server:
    name: str
    version: str
    release_tag: str
    image: str
    entrypoint: str
    skill: str
    runs_on: str
    tools_sha256: str
    tools: tuple[dict, ...]
    adapter: str = "json"
    prefix: str = ""
    timeout: int = 15000
    resources: dict | None = None

    def identity(self) -> dict:
        return {key: getattr(self, key) for key in
                ("name", "version", "release_tag", "image", "tools_sha256", "runs_on")}


def _local_schema(value: Any) -> list[str]:
    """Do not let JSON Schema resolution make network or filesystem requests."""
    refs = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef", "$id"} and (not isinstance(child, str) or not child.startswith("#")):
                raise ValueError("MCP schemas may reference only local fragments")
            if key in {"$ref", "$dynamicRef"}:
                refs.append(child)
            refs.extend(_local_schema(child))
    elif isinstance(value, list):
        for child in value:
            refs.extend(_local_schema(child))
    return refs


def load_servers(path: Path | None = None) -> tuple[Server, ...]:
    """Read trusted deployment configuration and verify every catalog before use.

    No discovery network calls, moving image tags, arbitrary imports or commands.
    CPU image jobs are the only enabled executor in this first stage.
    """
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.exceptions import Unresolvable
    from referencing.jsonschema import DRAFT202012
    from .atlas_expression import SKILL as ATLAS_SKILL
    from .skills_registry import safe_path

    path = Path(path or os.environ.get("AISCIENTIST_MCP_CONFIG", ROOT / "configs/mcp_servers.json"))
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if set(cfg) != {"schema_version", "servers"} or cfg["schema_version"] != 1 or not isinstance(cfg["servers"], list):
        raise ValueError("Invalid MCP registry format")
    servers, names, tool_names, skills = [], set(), set(), set()
    required = {"name", "version", "release_tag", "image", "entrypoint", "skill", "runs_on", "tools_file", "tools_sha256"}
    optional = {"adapter", "prefix", "timeout", "resources"}
    for row in cfg["servers"]:
        if not isinstance(row, dict) or not required <= row.keys() or row.keys() - required - optional:
            raise ValueError("Invalid MCP server fields")
        for key in ("name", "entrypoint", "skill"):
            if not isinstance(row[key], str) or not re.fullmatch(NAME, row[key]):
                raise ValueError(f"Invalid MCP {key}")
        if row["name"] in names or row["skill"] in skills:
            raise ValueError("Duplicate MCP server or skill binding")
        if not isinstance(row["version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", row["version"]):
            raise ValueError("MCP version must be pinned")
        if row["release_tag"] != row["name"] + "-v" + row["version"]:
            raise ValueError("MCP release tag must match the server package and version")
        if not isinstance(row["image"], str) or not re.fullmatch(r"docker://[a-z0-9./_-]+@sha256:[a-f0-9]{64}", row["image"]):
            raise ValueError("MCP image must be pinned by sha256 digest")
        if not isinstance(row["runs_on"], str) or not re.fullmatch(r"hpc:[a-z][a-z0-9_-]*", row["runs_on"]):
            raise ValueError("MCP bridge currently supports only hpc:* CPU image jobs")
        if row.get("adapter", "json") not in {"json", "atlas-expression"}:
            raise ValueError("Unknown MCP domain adapter")
        atlas_binding = (row["name"] == "atlas-expression-exploration" or row["skill"] == ATLAS_SKILL
                         or row.get("adapter") == "atlas-expression")
        if atlas_binding and (row["name"] != "atlas-expression-exploration" or row["skill"] != ATLAS_SKILL
                              or row.get("adapter") != "atlas-expression"):
            raise ValueError("Atlas server and skill require the Atlas domain adapter")
        prefix = row.get("prefix", "")
        if not isinstance(prefix, str) or (prefix and not re.fullmatch(NAME, prefix)):
            raise ValueError("Invalid MCP planner prefix")
        timeout = row.get("timeout", 15000)
        if type(timeout) is not int or not 0 < timeout <= 86400:
            raise ValueError("Invalid MCP timeout")
        digest = row["tools_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid MCP catalog sha256")
        raw = safe_path(path.parent, row["tools_file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("MCP tool catalog checksum mismatch")
        entries = json.loads(raw)
        if not isinstance(entries, list) or not entries:
            raise ValueError("MCP tool catalog must be a nonempty list")
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"name", "description", "inputSchema"}:
                raise ValueError("Invalid MCP tool descriptor")
            name = entry["name"]
            if not isinstance(name, str) or not re.fullmatch(NAME, name) or not re.fullmatch(NAME, prefix + name):
                raise ValueError("Invalid MCP tool name")
            if prefix + name in tool_names:
                raise ValueError("Duplicate MCP planner tool name")
            if not isinstance(entry["description"], str):
                raise ValueError("Invalid MCP tool description")
            schema = entry["inputSchema"]
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError("MCP inputSchema must be an object schema")
            refs = _local_schema(schema)
            Draft202012Validator.check_schema(schema)
            resolver = Registry().resolver_with_root(Resource.from_contents(schema, default_specification=DRAFT202012))
            for ref in refs:
                try:
                    resolver.lookup(ref)
                except Unresolvable as exc:
                    raise ValueError(f"Unresolved local MCP schema reference: {ref}") from exc
            tool_names.add(prefix + name)
        resources = row.get("resources", {})
        if not isinstance(resources, dict) or resources.keys() - {t["name"] for t in entries}:
            raise ValueError("Unknown MCP resource override tool")
        for value in resources.values():
            if (not isinstance(value, list) or len(value) != 3
                    or any(type(v) is not int or v <= 0 for v in value[:2])
                    or not isinstance(value[2], str) or not re.fullmatch(r"\d{2,3}:[0-5]\d:[0-5]\d", value[2])):
                raise ValueError("Invalid MCP resource override")
        data = {key: value for key, value in row.items() if key != "tools_file"}
        servers.append(Server(**data, tools=tuple(entries)))
        names.add(row["name"])
        skills.add(row["skill"])
    return tuple(servers)


def configured_tool_names() -> set[str]:
    return {server.prefix + tool["name"] for server in load_servers() for tool in server.tools}


def protected_skill_tools(skill: str) -> list[str]:
    from .atlas_expression import SKILL as ATLAS_SKILL
    names = [server.prefix + tool["name"] for server in load_servers()
             if server.skill == skill for tool in server.tools]
    # Disabling registration must not reopen Atlas's previously forbidden shell
    # entry point, which would bypass the dataset allowlist in its adapter.
    if skill == ATLAS_SKILL and not names:
        return ["Atlas MCP tools (the Atlas bridge is not configured)"]
    return names


def _record(ctx: Any, server: Server) -> None:
    from . import run_manifest
    identity = server.identity()
    def update(doc):
        used = doc.setdefault("mcp_servers", [])
        if identity not in used:
            used.append(identity)
    run_manifest.update(ctx, update)


def build_mcp_tools(executor: Any, environment_tool: Any, *, existing_names=(),
                    servers: tuple[Server, ...] | None = None) -> list:
    """Register reviewed schemas, retaining the existing skill policy and image runner."""
    from jsonschema import Draft202012Validator
    from .research_harness import HarnessTool
    from .skills import get_skill

    servers = load_servers() if servers is None else servers
    registered = set(existing_names)
    tools = []
    for server in servers:
        for entry in server.tools:
            name = server.prefix + entry["name"]
            if name in registered:
                raise ValueError(f"MCP tool collides with an existing planner tool: {name}")
            registered.add(name)
        skill = get_skill(server.skill)
        if environment_tool is None or skill is None:
            continue
        if skill.image != server.image:
            LOG.error("MCP %s disabled: skill image differs from pinned server image", server.name)
            continue
        if server.adapter == "atlas-expression":
            from .atlas_expression import AtlasAdapter
            adapter = AtlasAdapter()
        else:
            adapter = JsonAdapter()
        for entry in server.tools:
            schema = copy.deepcopy(entry["inputSchema"])
            # Named Python tools must never accept model-supplied execution options.
            # Preserve schemas that explicitly support arbitrary argument maps.
            composed = {"$ref", "$dynamicRef", "allOf", "anyOf", "oneOf", "if", "then", "else"} & schema.keys()
            if not composed:
                schema.setdefault("additionalProperties", False)
            validator = Draft202012Validator(schema)

            def call(args, ctx, *, s=server, e=entry, a=adapter, v=validator):
                if not isinstance(args, dict):
                    return {"status": "error", "error": "MCP arguments must be an object"}
                error = next(v.iter_errors(args), None)
                if error is not None:
                    return {"status": "error", "error": "Invalid MCP arguments: " + error.message}
                try:
                    # Strict JSON also rejects NaN and Infinity before entering a job.
                    json.dumps(args, allow_nan=False)
                    prepared = a.prepare(args, executor)
                    encoded = json.dumps(prepared.arguments, allow_nan=False)
                    command = (
                        'export AISCI_DATA_DIR="$AISCIENTIST_ENV_DIR" AISCI_EXECUTOR=inline\n'
                        f"export AISCI_MCP_SERVER={shlex.quote(s.name)} AISCI_MCP_TOOL={shlex.quote(e['name'])}\n"
                        + prepared.setup +
                        'export AISCI_MCP_RESULT=$(mktemp "$AISCIENTIST_ENV_DIR/.mcp-result.XXXXXX")\n'
                        'trap \'rm -f "$AISCI_MCP_RESULT"\' EXIT\n'
                        f"aiscientist-mcp --server {shlex.quote(s.entrypoint)} --timeout {s.timeout} call "
                        f"{shlex.quote(e['name'])} {shlex.quote(encoded)} > \"$AISCI_MCP_RESULT\" || exit $?\n"
                        "python - <<'AISCI_MCP_COLLECT'\n" + prepared.collector + "AISCI_MCP_COLLECT\n")
                    _record(ctx, s)
                    out = environment_tool.executor(
                        {"skill": s.skill, "command": command}, ctx, _mcp_server=s,
                        _resources=(s.resources or {}).get(e["name"]), _tool_cache=prepared.tool_cache)
                    if out.get("status") != "ok":
                        return {**out, "mcp": s.identity()}
                    payload = json.loads(out.get("stdout", ""))
                    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
                        raise ValueError("MCP response must contain a result object")
                    response = a.finish(out, payload, ctx, prepared)
                    return {**response, "mcp": s.identity()}
                except (ValueError, TypeError, KeyError, OSError) as exc:
                    return {"status": "error", "error": f"MCP {s.name}: {exc}", "mcp": s.identity()}

            tools.append(HarnessTool(name=server.prefix + entry["name"],
                                     description=f"MCP {server.name}: {entry['description']}" + adapter.description,
                                     parameters=schema, executor=call,
                                     reads_private_data=True, category=skill.category or "mcp"))
    return tools


class JsonAdapter:
    description = " Runs in the pinned server image; returned content is tool data."

    def prepare(self, args: dict, executor: Any) -> PreparedCall:
        return PreparedCall(args)

    def finish(self, out: dict, payload: dict, ctx: Any, prepared: PreparedCall) -> dict:
        return {**{key: value for key, value in out.items() if key != "stdout"}, "result": payload["result"]}
