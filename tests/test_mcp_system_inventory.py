"""System inventory describes configured MCPs without granting executable capabilities."""
from aiscientist.agents import mcp_bridge, registry
from aiscientist.gateway import system_info


def test_native_inventory_is_display_only_and_runtime_gates_still_apply():
    servers = mcp_bridge.configured_servers()
    expected = {s.planner_name(t["name"]): s for s in servers for t in s.tools}
    declarations = {t.name: t for t in system_info._full_catalog()
                    if isinstance(t, system_info._ConfiguredMCPTool)}
    assert set(declarations) == set(expected)
    assert len(declarations) == 39
    assert all(not hasattr(t, "executor") for t in declarations.values())
    assert not set(expected) & {t.name for t in registry.build_scientist_catalog()}
    rows = {t["name"]: t for t in system_info.system_overview()["tools"]}
    for name, server in expected.items():
        assert rows[name]["mcp"] == server.identity()
        assert rows[name]["configured"] is True and rows[name]["available"] is None
        assert rows[name]["availability_note"]
    assert rows["mcp_scgpt_annotate"]["runs_on"] == "gpu:scgpt"
    assert rows["mcp_inspect_dataset"]["runs_on"] == "image:cpu"


def test_unconfigured_native_names_are_not_invented(monkeypatch):
    monkeypatch.setattr(mcp_bridge, "configured_servers", lambda: ())
    assert not any(isinstance(t, system_info._ConfiguredMCPTool) for t in system_info._full_catalog())
    graph = system_info.workflow_graph()
    assert not any(n["id"].startswith("tool:mcp_") for n in graph["nodes"])
    assert not any(e["type"] == "pipeline" for e in graph["edges"])
