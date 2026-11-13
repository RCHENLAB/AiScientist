"""Platform test doubles at the external MCP boundary, never in-process science."""
from aiscientist.agents.mcp_model import ModelBinding
from aiscientist.agents.research_harness import HarnessContext
from aiscientist.gateway import app, literature_mcp
from types import SimpleNamespace


def bind_route(monkeypatch, build):
    monkeypatch.setattr(literature_mcp, "registered_server", lambda:
        SimpleNamespace(name="literature", settings_env=(), image="docker://fixture@sha256:" + "a" * 64,
                        version="0.0.4", tools_sha256="b" * 64))
    async def native(conn, run, run_id, emit):
        route = build(conn, run_id=run_id)
        if route is not None and not hasattr(route, "stage_scan_tables"):
            route.stage_scan_tables = lambda result, subdir: None
        run.literature_mcp = route
        run.literature_mcp_context = HarnessContext(decisions={}, workspace=conn.workspace / run_id,
                                                   model=conn.selected_model)
        return route
    monkeypatch.setattr(app, "_build_literature_mcp_route", native)
    monkeypatch.setattr(app, "_mcp_session_model", lambda conn:
        ModelBinding("http://model-fixture.invalid/v1", conn.selected_model))
