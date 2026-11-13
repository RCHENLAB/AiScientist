"""Offline MCP registry/transport contracts, including a second, non-Atlas server."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest
from jsonschema.exceptions import SchemaError

from aiscientist.agents import mcp_bridge as bridge, skills_registry
from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.research_lab import make_run_in_environment_tool

IMAGE = "docker://ghcr.io/example/mcp-table-check@sha256:" + "a" * 64
SCHEMA = {
    "type": "object", "properties": {
        "text": {"type": "string"}, "count": {"type": "integer", "minimum": 1},
        "options": {"$ref": "#/$defs/options"}},
    "required": ["text"], "additionalProperties": False,
    "$defs": {"options": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                            "additionalProperties": False}},
}


def write_config(root: Path, *, entries=None, overrides=None):
    entries = entries or [{"name": "check", "description": "Check a table without changing it.",
                           "inputSchema": copy.deepcopy(SCHEMA)}]
    snapshot = root / "tools.json"
    snapshot.write_text(json.dumps(entries))
    row = {
        "name": "table-check", "version": "1.2.3", "release_tag": "table-check-v1.2.3",
        "image": IMAGE, "entrypoint": "table-check-mcp", "skill": "check-table",
        "runs_on": "hpc:utilities", "tools_file": snapshot.name,
        "tools_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "prefix": "tables_", "timeout": 90, "resources": {"check": [2, 8, "00:10:00"]},
    }
    row.update(overrides or {})
    config = root / "mcp_servers.json"
    config.write_text(json.dumps({"schema_version": 1, "servers": [row]}))
    return config


def write_skill(root: Path, image: str = IMAGE):
    folder = root / "check-table"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        "---\nname: check-table\ndescription: Check a table.\nmetadata:\n"
        "  category: data\n  version: '1.0.0'\n  status: verified\n"
        f"  image: '{image}'\n  cpus: '4'\n  mem_gb: '16'\n  time_limit: '00:20:00'\n"
        "  requires-network: none\n  requires-gpu: 'false'\n  requires-secrets: none\n"
        "  scheduler: optional\n---\nCheck the input table.\n")


class Runner:
    supports_skill_policy = True

    def __init__(self):
        self.calls = []
        self.response = {"status": "ok", "stdout": json.dumps({"result": {"rows": 7}}),
                         "returncode": 0, "artifacts": ["mcp/table-check/check.json"]}

    def run_in_image(self, command, image, **kwargs):
        self.calls.append((command, image, kwargs, skills_registry.POLICY.get()))
        return copy.deepcopy(self.response)


@pytest.fixture
def registry(tmp_path, monkeypatch, skill_release):
    config = write_config(tmp_path)
    monkeypatch.setenv("AISCIENTIST_MCP_CONFIG", str(config))
    source = tmp_path / "skills"
    write_skill(source)
    skill_release(source)
    return config, source


def build(runner):
    return bridge.build_mcp_tools(runner, make_run_in_environment_tool(runner))


def test_second_server_registers_schema_and_runs_through_skill_policy(registry, tmp_path):
    runner = Runner()
    tool, = build(runner)
    assert tool.name == "tables_check"
    assert "Check a table without changing it." in tool.description
    assert tool.parameters["$defs"] == SCHEMA["$defs"]
    assert tool.parameters["additionalProperties"] is False
    assert bridge.load_servers()[0].tools[0]["inputSchema"]["additionalProperties"] is False
    ctx = HarnessContext(workspace=tmp_path / "run")
    # Quotes, substitutions and a newline must stay in the single JSON argument.
    args = {"text": "a'; $(touch nope) `touch nope`\nline", "count": 3,
            "options": {"enabled": True}}
    result = tool.executor(args, ctx)
    assert result["status"] == "ok" and result["result"] == {"rows": 7}
    assert "stdout" not in result
    command, image, kw, policy = runner.calls[0]
    assert image == IMAGE
    cli = shlex.split(next(line for line in command.splitlines() if line.startswith("aiscientist-mcp ")))
    assert cli[:7] == ["aiscientist-mcp", "--server", "table-check-mcp", "--timeout", "90", "call", "check"]
    assert json.loads(cli[7]) == args
    assert 'AISCI_EXECUTOR=inline' in command and '|| exit $?' in command
    assert (kw["cpus"], kw["mem_gb"], kw["time_limit"]) == (2, 8, "00:10:00")
    assert kw["workdir"] == "env/check-table"
    assert policy["network"] == "none" and not policy["gpu"]
    manifest = json.loads((ctx.workspace / "manifest.json").read_text())
    assert manifest["mcp_servers"] == [bridge.load_servers()[0].identity()]
    assert manifest["skills"][0]["name"] == "check-table"


@pytest.mark.parametrize("args", [[], {}, {"text": 3}, {"text": "x", "count": True},
                                  {"text": "x", "count": 0}, {"text": "x", "image": "other"},
                                  {"text": "x", "options": {"enabled": "true"}}])
def test_invalid_arguments_are_rejected_before_a_job(registry, args):
    runner = Runner()
    tool, = build(runner)
    assert tool.executor(args, None)["status"] == "error"
    assert not runner.calls


@pytest.mark.parametrize("change", [
    {"version": "latest"}, {"release_tag": "table-check-v9.0.0"},
    {"image": "docker://ghcr.io/example/mcp-table-check:1.2.3"},
    {"tools_sha256": "b" * 64}, {"tools_file": "../tools.json"},
    {"tools_file": "/tmp/tools.json"}, {"entrypoint": "server; touch nope"},
    {"runs_on": "gpu:arbitrary"}, {"runs_on": "inprocess:arbitrary"},
    {"resources": {"other": [1, 1, "00:01:00"]}},
    {"resources": {"check": [True, 1, "00:01:00"]}},
    {"aliases": []}, {"aliases": {"check": "bad;name"}}, {"aliases": {"other": "unused"}},
    {"prefix": "", "aliases": {"check": "write_report"}},
])
def test_invalid_pins_paths_and_execution_policy_are_refused(tmp_path, change):
    config = write_config(tmp_path, overrides=change)
    with pytest.raises(ValueError):
        bridge.load_servers(config)


def test_alias_changes_only_the_planner_name(registry):
    config, _ = registry
    write_config(config.parent, overrides={"aliases": {"check": "inspect"}})
    runner = Runner()
    tool, = build(runner)
    assert tool.name == "tables_inspect"
    assert bridge.configured_tool_names() == {tool.name}
    assert bridge.protected_skill_tools("check-table") == [tool.name]
    assert bridge.configured_tool_skills() == {tool.name: "check-table"}
    assert tool.executor({"text": "fixture"}, None)["status"] == "ok"
    command, _image, kwargs, _policy = runner.calls[0]
    cli = shlex.split(next(line for line in command.splitlines() if line.startswith("aiscientist-mcp ")))
    assert cli[6] == "check" and kwargs["cpus"] == 2


def test_snapshot_checksum_covers_descriptions_and_schemas(tmp_path):
    config = write_config(tmp_path)
    snapshot = tmp_path / "tools.json"
    entries = json.loads(snapshot.read_text())
    entries[0]["description"] = "Changed description"
    snapshot.write_text(json.dumps(entries))
    with pytest.raises(ValueError, match="checksum"):
        bridge.load_servers(config)


@pytest.mark.parametrize("schema", [
    {"type": "array"}, {"type": "object", "properties": {"x": {"type": "not-a-type"}}},
    {"type": "object", "properties": {"x": {"$ref": "https://example.org/schema"}}},
    {"type": "object", "properties": {"x": {"$ref": "file:///tmp/schema"}}},
])
def test_invalid_or_external_schemas_are_refused(tmp_path, schema):
    config = write_config(tmp_path, entries=[{"name": "check", "description": "Test", "inputSchema": schema}])
    with pytest.raises((ValueError, SchemaError)):
        bridge.load_servers(config)


def test_unresolved_local_schema_ref_is_rejected_at_registration(registry):
    config, _ = registry
    write_config(config.parent, entries=[{
        "name": "check", "description": "Test", "inputSchema": {
            "type": "object", "properties": {"text": {"$ref": "#/$defs/missing"}}}}])
    with pytest.raises(ValueError, match="Unresolved local MCP schema reference"):
        bridge.load_servers(config)


def test_nonfinite_numbers_never_reach_the_cli(registry):
    config, _ = registry
    write_config(config.parent, entries=[{
        "name": "check", "description": "Test", "inputSchema": {
            "type": "object", "properties": {"number": {"type": "number"}}}}])
    runner = Runner()
    tool, = build(runner)
    for value in (float("nan"), float("inf"), -float("inf")):
        assert tool.executor({"number": value}, None)["status"] == "error"
    assert not runner.calls


@pytest.mark.parametrize("composition", ["$ref", "allOf", "anyOf", "oneOf"])
def test_composed_schemas_keep_their_argument_semantics(registry, composition):
    config, _ = registry
    args_schema = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}
    schema = {"type": "object", "$defs": {"Args": args_schema}}
    reference = {"$ref": "#/$defs/Args"}
    schema.update(reference if composition == "$ref" else {composition: [reference]})
    write_config(config.parent, entries=[{"name": "check", "description": "Test", "inputSchema": schema}])
    runner = Runner()
    tool, = build(runner)
    assert tool.parameters == schema
    assert tool.executor({"value": "ok"}, None)["status"] == "ok"
    assert tool.executor({"value": 3}, None)["status"] == "error"
    assert len(runner.calls) == 1


def test_duplicate_servers_tools_and_builtin_names_are_refused(registry):
    config, _ = registry
    cfg = json.loads(config.read_text())
    cfg["servers"].append(copy.deepcopy(cfg["servers"][0]))
    config.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="Duplicate MCP server"):
        bridge.load_servers(config)
    cfg["servers"][1].update(name="other", skill="other-skill", release_tag="other-v1.2.3")
    config.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="Duplicate MCP planner"):
        bridge.load_servers(config)
    cfg["servers"].pop()
    config.write_text(json.dumps(cfg))
    runner = Runner()
    # A name another tool already has leaves that server out; it does not stop the catalog.
    assert bridge.build_mcp_tools(runner, make_run_in_environment_tool(runner), existing_names={"tables_check"}) == []


def test_exposure_and_call_time_skill_guard(registry, skill_release, monkeypatch):
    _, source = registry
    assert not build(object())
    runner = Runner()
    tool, = build(runner)
    direct = make_run_in_environment_tool(runner)
    out = direct.executor({"skill": "CHECK-TABLE", "command": "true", "_mcp_server": {},
                           "_resources": [1, 1, "00:01:00"]}, None)
    assert out["status"] == "error" and "tables_check" in out["error"]
    assert not {"_mcp_server", "_resources"} & direct.parameters["properties"].keys()
    write_skill(source, IMAGE[:-1] + "b")
    skill_release(source)
    assert not build(runner)
    assert tool.executor({"text": "x"}, None)["status"] == "error"
    assert not runner.calls
    write_skill(source)
    skill_release(source)
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_ALLOWED_TIERS", "core")
    assert tool.executor({"text": "x"}, None)["status"] == "error"


def test_generic_tools_reach_the_catalog_and_plan_classifier(registry):
    from aiscientist.agents.registry import build_scientist_catalog
    from aiscientist.agents.research_lab import _known_tool_names, _primary_tool

    assert "tables_check" in {t.name for t in build_scientist_catalog(code_executor=Runner())}
    _known_tool_names.cache_clear()
    try:
        assert _primary_tool("Check the table with `tables_check`.") == "tables_check"
    finally:
        _known_tool_names.cache_clear()


@pytest.mark.parametrize("response", [
    {"status": "error", "returncode": 1, "stderr": "tool error: invalid table"},
    {"status": "cancelled", "returncode": None},
    {"status": "ok", "stdout": "truncated"},
    {"status": "ok", "stdout": json.dumps({"result": []})},
])
def test_transport_errors_and_failed_attempts_keep_server_identity(registry, tmp_path, response):
    runner = Runner()
    runner.response = response
    tool, = build(runner)
    ctx = HarnessContext(workspace=tmp_path / "run")
    result = tool.executor({"text": "x"}, ctx)
    assert result["status"] == ("cancelled" if response["status"] == "cancelled" else "error")
    assert result["mcp"]["version"] == "1.2.3"
    if "stderr" in response:
        assert result["stderr"] == response["stderr"]
    # A failed attempt is not a use: the result names the server, the manifest does not.
    manifest = ctx.workspace / "manifest.json"
    assert not manifest.exists() or "mcp_servers" not in json.loads(manifest.read_text())


def test_server_result_cannot_replace_execution_metadata(registry):
    runner = Runner()
    data = {"status": "error", "artifacts": ["/other"], "provenance": {"image": "other"}}
    runner.response["stdout"] = json.dumps({"result": data})
    tool, = build(runner)
    result = tool.executor({"text": "x"}, None)
    assert result["status"] == "ok" and result["result"] == data
    assert result["artifacts"] == ["mcp/table-check/check.json"]
    assert result["provenance"]["image"] == IMAGE


@pytest.mark.parametrize("raw, expected", [
    ({}, {}), (["{}"], {}), (["{\"rows\":", "7}"], {"rows": 7}),
    (["ordinary text"], {"content": [{"type": "text", "text": "ordinary text"}]}),
])
def test_collector_accepts_structured_and_text_results(tmp_path, monkeypatch, capsys, raw, expected):
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps(raw))
    for key, value in {"AISCI_MCP_RESULT": result_file, "AISCIENTIST_ARTIFACTS": tmp_path / "artifacts",
                       "AISCI_MCP_SERVER": "table-check", "AISCI_MCP_TOOL": "check"}.items():
        monkeypatch.setenv(key, str(value))
    exec(bridge.JSON_COLLECT, {})
    assert json.loads(capsys.readouterr().out) == {"result": expected}
    assert json.loads((tmp_path / "artifacts/mcp/table-check/check.json").read_text()) == expected


def test_collector_bounds_transport_and_retains_complete_evidence(tmp_path, monkeypatch, capsys):
    result_file = tmp_path / "result.json"
    payload = {"rows": ["large response" * 1000] * 3}
    result_file.write_text(json.dumps(payload))
    for key, value in {"AISCI_MCP_RESULT": result_file, "AISCIENTIST_ARTIFACTS": tmp_path / "artifacts",
                       "AISCI_MCP_SERVER": "table-check", "AISCI_MCP_TOOL": "check"}.items():
        monkeypatch.setenv(key, str(value))
    exec(bridge.JSON_COLLECT, {})
    stdout = capsys.readouterr().out
    assert len(stdout) < 20000
    result = json.loads(stdout)["result"]
    assert result == {"response_omitted": True, "full_result": "mcp/table-check/check.json"}
    assert json.loads((tmp_path / "artifacts" / result["full_result"]).read_text()) == payload
    result_file.write_text(json.dumps([{"text": "wrong CLI envelope"}]))
    with pytest.raises(ValueError, match="text blocks"):
        exec(bridge.JSON_COLLECT, {})


def test_exporter_paginates_and_refuses_duplicate_names_or_cursor_loops():
    path = Path(__file__).resolve().parents[1] / "scripts/export_mcp_tools.py"
    spec = importlib.util.spec_from_file_location("export_mcp_tools", path)
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)

    def item(name):
        return SimpleNamespace(model_dump=lambda **kw: {"name": name, "inputSchema": {"type": "object"}})

    class Client:
        def __init__(self, pages):
            self.pages, self.cursors = iter(pages), []

        async def list_tools(self, cursor=None):
            self.cursors.append(cursor)
            return next(self.pages)

    first = SimpleNamespace(tools=[item("one")], next_cursor="next")
    last = SimpleNamespace(tools=[item("two")], next_cursor=None)
    client = Client([first, last])
    assert [t["name"] for t in asyncio.run(exporter.list_tool_schemas(client))] == ["one", "two"]
    assert client.cursors == [None, "next"]
    with pytest.raises(ValueError, match="Duplicate"):
        asyncio.run(exporter.list_tool_schemas(Client([first, first])))
    with pytest.raises(ValueError, match="cursor"):
        asyncio.run(exporter.list_tool_schemas(Client([first, SimpleNamespace(tools=[], next_cursor="next")])))


@pytest.mark.parametrize("broken", ["invalid", "unreadable", "no_jsonschema"])
def test_an_unusable_registry_leaves_out_only_the_mcp_tools(registry, tmp_path, monkeypatch, caplog, broken):
    import sys
    from aiscientist.agents import atlas_expression as atlas
    from aiscientist.agents.registry import build_scientist_catalog
    from aiscientist.agents.research_lab import _known_tool_names

    config, _ = registry
    if broken == "invalid":
        config.write_text(json.dumps({"schema_version": 1, "servers": [{"name": "x"}]}))
    elif broken == "unreadable":
        monkeypatch.setenv("AISCIENTIST_MCP_CONFIG", str(tmp_path / "missing.json"))
    else:
        monkeypatch.setattr(bridge, "_LOADED", {})
        monkeypatch.setitem(sys.modules, "jsonschema", None)
    assert bridge.configured_servers() == ()
    assert "MCP tools disabled" in caplog.text
    names = {t.name for t in build_scientist_catalog(code_executor=Runner())}
    assert "tables_check" not in names and "run_in_environment" in names
    _known_tool_names.cache_clear()
    try:
        assert "run_in_environment" in _known_tool_names()
    finally:
        _known_tool_names.cache_clear()
    # Atlas's shell stays closed while its registration cannot be read (run_in_environment
    # refuses a skill whose protected tools are listed here).
    assert bridge.protected_skill_tools(atlas.SKILL) == ["Atlas MCP tools (the Atlas bridge is not configured)"]


def test_the_registry_is_loaded_once_per_version(registry, monkeypatch):
    config, _ = registry
    calls = []
    real = bridge.load_servers

    def counted(path=None):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(bridge, "load_servers", counted)
    monkeypatch.setattr(bridge, "_LOADED", {})
    first = bridge.configured_servers()
    assert bridge.configured_servers() is first and bridge.configured_tool_names() == {"tables_check"}
    assert len(calls) == 1
    cfg = json.loads(config.read_text())
    cfg["servers"][0]["prefix"] = "other_"
    config.write_text(json.dumps(cfg))
    assert bridge.configured_tool_names() == {"other_check"}
    assert len(calls) == 2
