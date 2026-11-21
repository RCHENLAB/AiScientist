"""A skill whose MCP server writes its own report gets that report, in its fixed format (Jin Li,
RCHENLAB/AiScientist#102).

The server row names the tool (``report`` in mcp_servers.json). When a written report is wanted, the
plan's report step is that tool, run like any other step, and the gateway makes the run's report from
what it wrote, by code. The manuscript a model writes (``write_report``) is planned only when some
analysis step uses no such skill. Scripted models and fake executors throughout.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import test_atlas_expression
import test_mcp_bridge
from aiscientist.agents import mcp_bridge as bridge
from aiscientist.agents import research_lab as rl
from aiscientist.agents.atlas_expression import SKILL as ATLAS_SKILL
from aiscientist.agents.research_harness import HarnessContext, step_succeeded
from aiscientist.agents.research_lab import LabConfig, ResearchLab
from aiscientist.agents.retigene import SKILL as RETIGENE_SKILL
from aiscientist.agents.skills import Skill
from aiscientist.reporting import fixed_report

release = test_atlas_expression.release
ATLAS_REPORT = "atlas_write_report"
QUERY = "**Atlas query** — Run `submit_query(genes=\"RPE65\")` on the attached atlas."
PLOTS = "**Plots** — Draw `plot_dotplot` for the query."
QC = "**QC** — Run `mcp_run_scanpy_qc` on the cells."


@pytest.fixture(autouse=True)
def _known_names():
    rl._known_tool_names.cache_clear()
    yield
    rl._known_tool_names.cache_clear()


# --- the registry ------------------------------------------------------------------------------------

def test_the_atlas_and_retigene_servers_name_their_report_tools():
    assert bridge.report_tools() == {ATLAS_SKILL: ATLAS_REPORT, RETIGENE_SKILL: "retigene_write_report"}


def test_a_report_tool_must_be_one_of_the_servers_tools(tmp_path):
    config = test_mcp_bridge.write_config(tmp_path, overrides={"report": "check"})
    assert bridge.load_servers(config)[0].report == "check"
    config = test_mcp_bridge.write_config(tmp_path, overrides={"report": "absent"})
    with pytest.raises(ValueError, match="Unknown MCP report tool"):
        bridge.load_servers(config)


def test_a_step_is_a_report_step_only_when_the_report_tool_is_its_only_tool():
    assert rl._is_fixed_report_step(rl.fixed_report_step(ATLAS_REPORT))
    assert rl._is_fixed_report_step('Write the query report with `atlas_write_report(query_id="q1")`.')
    assert not rl._is_fixed_report_step(QUERY)
    # A step that plots and then writes the report is an analysis step: it is never moved or dropped.
    assert not rl._is_fixed_report_step("Draw `plot_dotplot`, then `atlas_write_report`.")
    assert not rl._is_report_step(rl.fixed_report_step(ATLAS_REPORT))
    assert not rl._is_report_busywork(rl.fixed_report_step(ATLAS_REPORT))
    assert not rl._is_readback_step(rl.fixed_report_step(ATLAS_REPORT))


def test_the_plan_check_takes_the_generated_report_step_as_it_is(release):
    from aiscientist.agents.research_lab.tooling import check_plan_tooling
    catalog = list(test_atlas_expression.tools(test_atlas_expression.Runner()).values())
    step = rl.fixed_report_step(ATLAS_REPORT)
    assert check_plan_tooling([step], catalog) == ([step], [])


# --- the plan ----------------------------------------------------------------------------------------

def _lab(monkeypatch, offered=(ATLAS_REPORT,)) -> ResearchLab:
    atlas, other = Skill(name=ATLAS_SKILL), Skill(name="analyze-scrna-with-mcp")
    monkeypatch.setattr(ResearchLab, "_step_skills",
                        lambda self, step: [atlas] if ({"submit_query", "plot_dotplot", ATLAS_REPORT}
                                                       & rl._declared_tools(step)) else [other])
    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"), LabConfig())
    lab.scientist = SimpleNamespace(catalog=[SimpleNamespace(name=n) for n in offered])
    return lab


def _events():
    seen: list[dict] = []
    return seen, seen.append


def test_a_report_on_an_atlas_plan_is_the_atlas_report_tool(monkeypatch):
    seen, emit = _events()
    plan = _lab(monkeypatch)._ensure_report_step(
        [QUERY, PLOTS], "Check RPE65 in the atlas and write a report", emit=emit)
    assert plan == [QUERY, PLOTS, rl.fixed_report_step(ATLAS_REPORT)]
    assert seen[-1] == {"type": "report_step_added", "skills": [], "tools": [ATLAS_REPORT],
                        "manuscript": False}


def test_the_models_write_report_step_gives_way_to_the_tool(monkeypatch):
    seen, emit = _events()
    drafted = 'Write the query report with `atlas_write_report(query_id="q1")`.'
    plan = _lab(monkeypatch)._ensure_report_step(
        [drafted, QUERY, rl.REPORT_STEP], "Check RPE65 and write a PDF report", emit=emit)
    assert plan == [QUERY, drafted]     # its own wording kept, moved last, never doubled
    assert {"type": "steps_pruned", "reason": "fixed_format_report", "dropped": [rl.REPORT_STEP],
            "tools": [ATLAS_REPORT]} in seen
    assert not [e for e in seen if e["type"] == "report_step_added"]


def test_a_mixed_plan_gets_the_tool_report_and_the_manuscript(monkeypatch):
    seen, emit = _events()
    plan = _lab(monkeypatch)._ensure_report_step(
        [QC, QUERY], "QC the cells, check RPE65 in the atlas and write a report", emit=emit)
    assert plan == [QC, QUERY, rl.fixed_report_step(ATLAS_REPORT), rl.REPORT_STEP]
    assert seen[-1]["manuscript"] is True and seen[-1]["tools"] == [ATLAS_REPORT]


def test_a_server_this_deployment_does_not_offer_gives_the_manuscript(monkeypatch):
    _seen, emit = _events()
    plan = _lab(monkeypatch, offered=())._ensure_report_step(
        [QUERY], "Check RPE65 and write a report", emit=emit)
    assert plan == [QUERY, rl.REPORT_STEP]


def test_no_report_asked_for_has_no_report_step_of_either_kind(monkeypatch):
    seen, emit = _events()
    tool_step = rl.fixed_report_step(ATLAS_REPORT)
    plan = _lab(monkeypatch)._ensure_report_step([QUERY, tool_step], "Check RPE65 in the atlas", emit=emit)
    assert plan == [QUERY]
    assert {"type": "steps_pruned", "reason": "report_not_asked", "dropped": [tool_step]} in seen


def test_a_note_on_the_plan_keeps_or_drops_the_tool_report(monkeypatch):
    _seen, emit = _events()
    lab = _lab(monkeypatch)
    tool_step = rl.fixed_report_step(ATLAS_REPORT)
    kept = lab._ensure_report_step([QUERY], "Check RPE65 and write a report", feedback="use RHO too",
                                   note="use RHO too", prior_agenda=[QUERY, tool_step], emit=emit)
    assert kept == [QUERY, tool_step]
    dropped = lab._ensure_report_step([QUERY, tool_step], "Check RPE65 and write a report",
                                      feedback="drop the report step", note="drop the report step",
                                      prior_agenda=[QUERY, tool_step], emit=emit)
    assert dropped == [QUERY]


def test_plan_review_keeps_the_report_steps_last():
    tool_step = rl.fixed_report_step(ATLAS_REPORT)
    reviewed = ResearchLab._keep_report_step([QC, QUERY, tool_step, rl.REPORT_STEP],
                                             [QUERY, rl.REPORT_STEP, QC])
    assert reviewed == [QUERY, QC, tool_step, rl.REPORT_STEP]


# --- the report from what the tools wrote ------------------------------------------------------------

REPORT_MD = ("# Expression of RPE65\n\n## RPE65\n\nHighest in RPE.\n\n## Figures\n\n"
             "![dotplot_majorclass](dotplot_majorclass.png)\n\n![missing](absent.png)\n\n"
             "[atlas](https://example.org/atlas)\n")


def _art(tmp_path):
    art = tmp_path / "artifacts"
    query = art / "gene_expression" / "q1"
    query.mkdir(parents=True)
    (query / "report.md").write_text(REPORT_MD, encoding="utf-8")
    (query / "dotplot_majorclass.png").write_bytes(b"\x89PNG")
    return art


def _round(step, calls, verdict="accept"):
    return SimpleNamespace(step=step, verdict=SimpleNamespace(verdict=verdict),
                           scientist_result={"steps": calls})


def _call(result, args=None, ok=True):
    return {"tool": ATLAS_REPORT, "ok": ok, "args": args or {"query_id": "q1"}, "result": result}


def _files(art, rounds):
    return fixed_report.report_files(SimpleNamespace(rounds=rounds), art, is_report_step=rl._is_fixed_report_step,
                                     tools={ATLAS_REPORT}, succeeded=step_succeeded)


def test_only_the_accepted_report_steps_give_the_report(tmp_path):
    art = _art(tmp_path)
    step = rl.fixed_report_step(ATLAS_REPORT)
    named = _call({"file": "gene_expression/q1/report.md", "status": "ok"})
    want = [(art / "gene_expression/q1/report.md").resolve()]
    assert _files(art, [_round(step, [named, named])]) == want
    # Atlas 0.0.4 names its report twice in one result, as its file and among its artifacts (prod
    # run 94c9081f8dea printed the report twice): one file
    both = _call({"file": "gene_expression/q1/report.md", "status": "ok",
                  "artifacts": ["gene_expression/q1/report.md", "gene_expression/mcp_result.json"]})
    assert _files(art, [_round(step, [both])]) == want
    # cut short by the transport bound: found by its query id
    assert _files(art, [_round(step, [_call({"state": "COMPLETED", "response_omitted": True})])]) == want
    assert _files(art, [_round(step, [named], verdict="reject")]) == []
    assert _files(art, [_round(step, [_call({"status": "error", "error": "no query"})])]) == []
    # the tool called inside an analysis step is that step's output, not the run's report
    assert _files(art, [_round("Plot with `plot_dotplot`, then `atlas_write_report`.", [named])]) == []
    outside = _call({"file": "../elsewhere/report.md"}, args={"query_id": "../x"})
    assert _files(art, [_round(step, [outside])]) == []


def test_the_report_is_the_tools_markdown_with_its_figures_found(tmp_path):
    art = _art(tmp_path)
    md = fixed_report.assemble(art, [art / "gene_expression/q1/report.md"])
    assert "Highest in RPE." in md
    assert "![dotplot_majorclass](gene_expression/q1/dotplot_majorclass.png)" in md
    assert "![missing](absent.png)" in md and "(https://example.org/atlas)" in md


# --- the run's tail in the gateway -------------------------------------------------------------------

def test_the_gateway_renders_the_tools_report_and_no_model_writes_one(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import test_qc_only_chat_reply as harness
    from aiscientist.agents.research_lab import CriticVerdict, LabResult, LabRound
    from aiscientist.gateway import app as gw_app
    from aiscientist.reporting import report as reporting
    from aiscientist.tools import api as tools_api

    conn = harness._conn(tmp_path)
    step = rl.fixed_report_step(ATLAS_REPORT)

    class _FakeLab:
        def __init__(self, ctx, config, **_k):
            self.ctx = ctx
            self.guidance, self.protocols, self.self_sourced = None, [], ""
            self.execution_profile, self.later, self.proposed_steps = {}, [], []
            self.request_breadth = "specific"
            self.report_requested = False

        def run(self, question, on_event=None, plan_review=None, **_kw):
            _art(self.ctx.workspace)
            query = LabRound(1, 0, QUERY, "Scientist", {"final_answer": "RPE65 is highest in RPE.", "steps": [
                {"tool": "submit_query", "ok": True, "args": {"genes": "RPE65"},
                 "result": {"status": "ok", "query_id": "q1", "state": "COMPLETED"}}]},
                CriticVerdict("accept", 0.9, ""))
            report = LabRound(2, 0, step, "Scientist", {"final_answer": "Report written.", "steps": [
                _call({"status": "ok", "file": "gene_expression/q1/report.md",
                       "artifacts": ["gene_expression/q1/report.md"]})]},
                CriticVerdict("accept", 0.9, ""))
            return LabResult(question, [QUERY, step], [query, report], True, 2, "RPE65 is highest in RPE.")

    def _not_this(*_a, **_k):
        raise AssertionError("no model may write, review or extend a fixed-format report")

    rendered: list[dict] = []

    def _render(md, out_dir, **kw):
        rendered.append({"md": md, **kw})
        out_dir.mkdir(parents=True, exist_ok=True)
        for ext in ("md", "pdf", "docx"):
            (out_dir / f"report.{ext}").write_text(md, encoding="utf-8")
        return {"status": "ok", "md_path": str(out_dir / "report.md"),
                "pdf_path": str(out_dir / "report.pdf"), "docx_path": str(out_dir / "report.docx")}

    monkeypatch.setattr(rl, "ResearchLab", _FakeLab)
    monkeypatch.delenv("AISCIENTIST_PLAN_YAML", raising=False)
    for name in ("_build_report", "_review_and_finalize_report", "_build_technical_report",
                 "_build_brief_report", "_write_report_narrative", "_verification_statement"):
        monkeypatch.setattr(gw_app, name, _not_this)
    monkeypatch.setattr(reporting, "build_pdf_report", _render)
    monkeypatch.setattr(tools_api, "render_workflow_schematic", lambda agenda, out_dir, **_k: {"status": "dependency_missing"})
    payloads: list[dict] = []
    monkeypatch.setattr(conn, "_publish", lambda payload: payloads.append(payload))
    req = gw_app.LabRequest(connection_id=conn.id, conversation_id="conv-1",
                            question="Check RPE65 in the atlas and write a report")
    conn.begin_run("conv-1")
    asyncio.run(gw_app._run_lab(conn, req))
    art = conn.workspace / conn.last_run_by_conversation["conv-1"] / "artifacts"

    assert len(rendered) == 1
    md = rendered[0]["md"]
    assert md.count("Highest in RPE.") == 1 and "gene_expression/q1/dotplot_majorclass.png" in md
    assert rendered[0]["title"] == "Expression of RPE65" and rendered[0]["assets_root"] == art
    assert (art / "report" / "report.pdf").exists()
    # the cleanup moved the server's folder to extra/ after the render: the report says where it is
    assert json.loads((art / "process" / "fixed_report.json").read_text()) == {
        "sources": ["extra/gene_expression/q1/report.md"]}
    assert "](extra/gene_expression/q1/dotplot_majorclass.png)" in (art / "report" / "report.md").read_text()
    assert (art / "extra/gene_expression/q1/dotplot_majorclass.png").is_file()
    said = [str(p.get("text") or p.get("message") or "") for p in payloads]
    assert any("fixed format, no model-written text" in s for s in said)
    assert any("tools' own report (1 file(s)" in s for s in said)
    assert any("Report ready" in s for s in said)
    assert not any("No written report was asked for" in s for s in said)
    assert "RPE65 is highest in RPE." in harness._reply(payloads)


# --- after the run: the quarantine, and a report asked for later -----------------------------------

def _quarantined(tmp_path):
    """A finished run whose fixed-format report was made from Atlas's, after the bundle cleanup."""
    from aiscientist.gateway import app as gw_app
    art = _art(tmp_path)
    (art / "figures").mkdir()
    (art / "figures" / "workflow.png").write_bytes(b"\x89PNG")
    (art / "report").mkdir()
    md = fixed_report.assemble(art, [art / "gene_expression/q1/report.md"])
    (art / "report" / "report.md").write_text(md + "\n![workflow](figures/workflow.png)\n", encoding="utf-8")
    (art / "process").mkdir()
    (art / fixed_report.SOURCES_RECORD).write_text(json.dumps({"sources": ["gene_expression/q1/report.md"]}))
    moved = gw_app._quarantine_strays(art)
    assert moved == ["gene_expression"]
    return art, moved


def test_the_report_still_finds_its_figures_after_the_quarantine(tmp_path):
    # prod run 94c9081f8dea: the cleanup moved gene_expression/ to extra/ after the render, and
    # report/report.md still linked gene_expression/...
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw_app
    art, moved = _quarantined(tmp_path)
    assert gw_app._follow_quarantine(art, moved) == 2
    text = (art / "report" / "report.md").read_text()
    assert "![dotplot_majorclass](extra/gene_expression/q1/dotplot_majorclass.png)" in text
    assert (art / "extra/gene_expression/q1/dotplot_majorclass.png").is_file()
    # a link into a folder that stayed, or to nothing, is left as it is
    assert "](figures/workflow.png)" in text and "![missing](absent.png)" in text
    assert fixed_report.recorded_sources(art) == [(art / "extra/gene_expression/q1/report.md").resolve()]
    assert gw_app._follow_quarantine(art, moved) == 0
    assert gw_app._follow_quarantine(art, []) == 0


def test_a_recorded_source_outside_the_run_is_not_read(tmp_path):
    art = _art(tmp_path)
    (art / "process").mkdir()
    (tmp_path / "elsewhere.md").write_text("# not this run's")
    (art / fixed_report.SOURCES_RECORD).write_text(json.dumps(
        {"sources": ["../elsewhere.md", "/etc/hosts", "gene_expression/q1/report.md", "gone/report.md", 3]}))
    assert fixed_report.recorded_sources(art) == [(art / "gene_expression/q1/report.md").resolve()]
    # recorded before the cleanup moved the server's folder, and twice (v0.4.24, prod run 94c9081f8dea)
    moved = art / "extra" / "gene_expression"
    moved.parent.mkdir()
    (art / "gene_expression").rename(moved)
    (art / fixed_report.SOURCES_RECORD).write_text(json.dumps({"sources": ["gene_expression/q1/report.md"] * 2}))
    assert fixed_report.recorded_sources(art) == [(moved / "q1/report.md").resolve()]
    (art / fixed_report.SOURCES_RECORD).write_text("[]")
    assert fixed_report.recorded_sources(art) == []
    assert fixed_report.recorded_sources(tmp_path / "no-run") == []


def test_a_report_asked_for_after_the_run_is_the_tools_report_again(tmp_path, monkeypatch):
    # write_report (the MCP tool) on a run whose report was Atlas's used to replace it with a
    # model-written manuscript
    pytest.importorskip("fastapi")
    import threading
    from aiscientist.gateway import app as gw_app
    from aiscientist.reporting import report as reporting
    art, moved = _quarantined(tmp_path)
    gw_app._follow_quarantine(art, moved)
    (art / "process" / "run_state.json").write_text(json.dumps({
        "question": "Check RPE65 in the atlas and write a report", "agenda": [QUERY],
        "accepted_steps": 1, "converged": True, "final_answer": "RPE65 is highest in RPE.", "rounds": []}))

    def _not_this(*_a, **_k):
        raise AssertionError("no model may write the report of a fixed-format run")

    rendered: list[dict] = []

    def _render(md, out_dir, **kw):
        rendered.append({"md": md, **kw})
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.pdf").write_bytes(b"%PDF")
        return {"status": "ok", "md_path": str(out_dir / "report.md"),
                "pdf_path": str(out_dir / "report.pdf"), "docx_path": None}

    monkeypatch.setattr(gw_app, "_write_report", _not_this)
    monkeypatch.setattr(gw_app, "_lab_llm", _not_this)
    monkeypatch.setattr(gw_app, "_build_report_render_fn", lambda *a: None)
    monkeypatch.setattr(reporting, "build_pdf_report", _render)
    asyncio.run(gw_app._report_job(SimpleNamespace(chat_stop=threading.Event()), "tester", "r1", art))
    job = json.loads((art / "process" / "report_job.json").read_text())
    assert job["state"] == "done", job
    assert len(rendered) == 1 and rendered[0]["md"].count("Highest in RPE.") == 1
    assert "extra/gene_expression/q1/dotplot_majorclass.png" in rendered[0]["md"]
    assert rendered[0]["title"] == "Expression of RPE65" and rendered[0]["assets_root"] == art
    assert any("no model wrote it" in line for line in job["log"])
