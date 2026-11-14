"""Readable exports preserve planned identities independently of recorded execution."""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")
from aiscientist.gateway import app as gw


def _mcp(name="get_summary", version="0.0.3"):
    return {"tool": name, "provider": "mcp", "server": "atlas-expression-exploration", "version": version}


def test_readable_two_atlas_plan_keeps_every_tool_and_skill():
    steps = [
        {"id": "s1", "tools": [_mcp("resolve_genes"), _mcp()], "skills": [
            {"name": "atlas-query", "version": "1.2"}, {"name": "gene-symbols", "version": "2"}]},
        {"id": "s2", "tools": [{"tool": "run_code", "provider": "local", "version": "unknown"}],
         "skills": []},
    ]
    md = gw._readable_plan_steps(steps, ["Query attached atlas", "Analyze second atlas"])
    first, second = md.split("2. Analyze second atlas")
    assert "resolve_genes (MCP atlas-expression-exploration @ 0.0.3)" in first
    assert "get_summary (MCP atlas-expression-exploration @ 0.0.3)" in first
    assert "atlas-query @ 1.2; gene-symbols @ 2" in first
    assert "run_code (local @ unknown)" in second
    assert "No skill assigned" in second
    assert "atlas-query" not in second


def test_readable_legacy_plan_never_guesses_identity_from_prose():
    md = gw._readable_plan_steps(
        [{"tool": "get_summary", "skill": "atlas-query"}, {}],
        ["Use atlas-expression-exploration version 0.0.3", "No tools"])
    assert "get_summary (unknown provider @ unknown)" in md
    assert "atlas-query @ unknown" in md
    assert "No skill assigned" in md
    assert "No tool assigned (provider/version unknown)" in md


def test_readable_execution_distinguishes_used_failed_and_skipped():
    rows = [
        {"id": "s1", "step": "Query atlas", "status": "used",
         "planned_tools": [_mcp()], "planned_skills": [{"name": "query", "version": "1"}],
         "tools": [{**_mcp(version="0.0.4"), "ok": True}],
         "skills": [{"name": "query", "version": "1", "status": "used"}],
         "provider_changes": [{"tool": "get_summary", "planned": [_mcp()], "used": _mcp(version="0.0.4")}]},
        {"id": "s2", "step": "Failed analysis", "status": "failed",
         "planned_skills": [{"name": "analysis", "version": "2"}],
         "tools": [{"tool": "run_code", "provider": "local", "ok": False}],
         "skills": [{"name": "analysis", "version": "2", "status": "failed"}]},
        {"id": "s3", "step": "Skipped analysis", "status": "skipped",
         "planned_skills": [{"name": "analysis", "version": "2"}]},
    ]
    md = gw._readable_step_usage(rows)
    assert "Planned skills: query @ 1" in md and "Recorded skills: query @ 1 [used]" in md
    assert ("Provider changed from approved plan: get_summary (MCP atlas-expression-exploration @ 0.0.3)"
            " -> get_summary (MCP atlas-expression-exploration @ 0.0.4)") in md
    failed, skipped = md.split("## s2:")[1].split("## s3:")
    assert "analysis @ 2 [failed]" in failed and "run_code (local @ unknown) [failed]" in failed
    assert "[used]" not in failed + skipped
    assert "Step status: skipped" in skipped and "No successful skill use recorded" in skipped


def test_gateway_saves_readable_assignment_before_review_and_final_usage(lab_gateway):
    env = lab_gateway
    conn = env.connection()
    req = gw.LabRequest(connection_id=conn.id, question="QC cells", conversation_id="provenance",
                        plan_mode=True, mode="single", planner="linear")
    asyncio.run(gw._run_lab(conn, req))
    rid, art, _, _ = gw._find_saved_plan(conn, None, "provenance")
    draft = (art / "report" / "plan.md").read_text(encoding="utf-8")
    assert "Planned tools: run_scanpy_qc (local @ unknown)" in draft
    assert "No skill assigned" in draft

    async def approve():
        from starlette.requests import Request
        reply = await gw.lab_plan_review(
            gw.PlanReviewRequest(connection_id=conn.id, action="approve", run_id=rid),
            Request({"type": "http", "headers": []}))
        pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
        assert reply.status_code == 200

    asyncio.run(approve())
    record = (art / "process" / "step_provenance.md").read_text(encoding="utf-8")
    assert not (art / "report" / "run_record.md").exists()
    assert "run_scanpy_qc" in record and "Recorded tools:" in record
    state = json.loads((art / "process" / "run_state.json").read_text(encoding="utf-8"))
    assert state["step_provenance"] == state["session_meta"]["step_provenance"]
    assert state["step_provenance"][0]["tools"][0]["ok"] is True


def test_step_provenance_reaches_live_console_without_losing_identity():
    event = {"type": "step_provenance", "id": "s1", "step": "Query atlas", "status": "failed",
             "planned_tools": [_mcp()], "planned_skills": [{"name": "query", "version": "1"}],
             "tools": [{**_mcp(), "ok": False}], "skills": [], "provider_changes": []}
    assert gw._lab_event_to_chat(event) == [event]
