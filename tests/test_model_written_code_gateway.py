"""The gateway's side of RCHENLAB/AiScientist#150: the plan card and the reply say which steps are not
available, the run is recorded for the team, and a request no tested tool covers submits nothing."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from aiscientist.agents.code_policy import NotAvailable  # noqa: E402
from aiscientist.gateway import app as gw, skill_requests  # noqa: E402

QC = "Filter cells with `mcp_run_scanpy_qc` at max_pct_mt 20."
VELOCITY = ("**RNA velocity** — Infer RNA velocity trajectories from the spliced and unspliced counts "
            "and project them on the embedding.")


def _planner(monkeypatch, agenda):
    """The fixture's models, with a PI that drafts ``agenda`` and a match naming no skill."""
    base = gw._lab_llm(None)

    def complete(messages, **kw):
        system = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in system:
            return json.dumps({"agenda": list(agenda)})
        if "no tested tool performs on this deployment" in system:
            return json.dumps({"unavailable": [{"step": agenda.index(VELOCITY) + 1, "closest_skill": ""}]})
        return base.complete_fn(messages, **kw)
    monkeypatch.setattr(gw, "_lab_llm", lambda c: SimpleNamespace(**{**vars(base), "complete_fn": complete}))


def _run(conn, *, plan_mode):
    req = gw.LabRequest(connection_id=conn.id, question="QC, then RNA velocity", conversation_id="conv-A",
                        plan_mode=plan_mode, mode="single", planner="linear")
    asyncio.run(gw._run_lab(conn, req))


def _pushed(conn):
    out, publish = [], conn._publish

    def spy(payload):
        out.append(payload)
        publish(payload)
    conn._publish = spy
    return out


def test_the_plan_card_lists_the_step_that_is_not_available_and_it_is_recorded(lab_gateway, monkeypatch,
                                                                              tmp_path):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    _planner(monkeypatch, [QC, VELOCITY])
    conn = lab_gateway.connection()
    _run(conn, plan_mode=True)
    card = lab_gateway.cards[-1]
    assert card["agenda"] == [QC], "the covered step is planned"
    assert card["not_available"] == [NotAvailable(step=VELOCITY).line(recorded=True)]
    rec = skill_requests.load(card["run_id"])
    assert rec is not None and [r["step"] for r in rec["not_available"]] == [VELOCITY]
    assert not lab_gateway.calls, "nothing runs before the plan is approved"


def test_a_request_no_tested_tool_covers_runs_nothing_and_says_so(lab_gateway, monkeypatch, tmp_path):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    _planner(monkeypatch, [VELOCITY])
    conn = lab_gateway.connection()
    pushed = _pushed(conn)
    _run(conn, plan_mode=True)
    assert not lab_gateway.cards, "no plan card: there is nothing to approve"
    assert not lab_gateway.calls, "no tool ran and no job was submitted"
    reply = "".join(p.get("token", "") for p in pushed if p.get("type") == "chat_token")
    assert reply == NotAvailable(step=VELOCITY).line(recorded=True)
    assert "The request has been recorded for the team." in reply
    run_id, status = lab_gateway.records[-1][1], lab_gateway.records[-1][2]
    assert status == "done"
    rec = skill_requests.load(run_id)
    assert [r["step"] for r in rec["not_available"]] == [VELOCITY] and rec["summary"]


def test_without_a_state_directory_the_reply_does_not_claim_a_record(lab_gateway, monkeypatch):
    monkeypatch.delenv("AISCIENTIST_STATE_DIR", raising=False)
    _planner(monkeypatch, [VELOCITY])
    conn = lab_gateway.connection()
    pushed = _pushed(conn)
    _run(conn, plan_mode=False)
    reply = "".join(p.get("token", "") for p in pushed if p.get("type") == "chat_token")
    assert reply == NotAvailable(step=VELOCITY).line(recorded=False)
    assert not lab_gateway.calls



def test_a_plan_with_a_covered_and_an_uncovered_step_runs_one_and_reports_the_other(tmp_path, monkeypatch):
    """The covered step ran; the reply opens with the step that did not, and the run is recorded."""
    import test_qc_only_chat_reply as qc
    from aiscientist.agents import research_lab as rl
    from aiscientist.agents.research_lab import CriticVerdict, LabResult, LabRound
    from aiscientist.reporting import report as reporting
    from aiscientist.tools import api as tools_api

    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AISCIENTIST_SKILL_REQUESTS", raising=False)
    monkeypatch.delenv("AISCIENTIST_PLAN_YAML", raising=False)
    held = [NotAvailable(step=VELOCITY).to_dict()]

    class _Lab:
        def __init__(self, ctx, config, **_k):
            self.guidance, self.protocols, self.self_sourced = None, [], ""
            self.execution_profile, self.later, self.proposed_steps = {}, [], []
            self.request_breadth, self.induced_skills = "specific", []
            self.scientist = type("S", (), {"catalog": []})()
            self.report_requested = False
            self.unavailable_steps = held

        def run(self, question, on_event=None, plan_review=None, **_kw):
            on_event({"type": "steps_unavailable", "source": "draft", "steps": held, "all": held})
            r1 = LabRound(1, 0, qc._STEP, "Scientist", {"final_answer": "QC done", "steps": [
                {"tool": "mcp_run_cellqc", "ok": True, "args": {}, "result": qc._CELLQC}]},
                CriticVerdict("accept", 0.8, ""))
            return LabResult(question, [qc._STEP], [r1], True, 1, "QC is done.",
                             unavailable_steps=list(held))

    def _schematic(agenda, out_dir, **_k):
        out_dir.mkdir(parents=True, exist_ok=True)
        return {"status": "dependency_missing"}

    monkeypatch.setattr(rl, "ResearchLab", _Lab)
    monkeypatch.setattr(reporting, "build_pdf_report", lambda *a, **k: {"status": "markdown_only"})
    monkeypatch.setattr(tools_api, "render_workflow_schematic", _schematic)
    conn = qc._conn(tmp_path)
    payloads: list[dict] = []
    monkeypatch.setattr(conn, "_publish", lambda payload: payloads.append(payload))
    req = gw.LabRequest(connection_id=conn.id, conversation_id="conv-1",
                        question="QC this sample, then infer RNA velocity.")
    conn.begin_run("conv-1")
    try:
        asyncio.run(gw._run_lab(conn, req))
    finally:
        gw.CONNECTIONS.pop(conn.id, None)
    run_id = conn.last_run_by_conversation["conv-1"]
    reply = qc._reply(payloads)
    assert reply.startswith(NotAvailable(step=VELOCITY).line(recorded=True)), reply[:300]
    feed = [p["text"] for p in payloads if p.get("type") == "lab_progress"]
    assert "🚫 " + NotAvailable(step=VELOCITY).line(recorded=True) in feed
    rec = skill_requests.load(run_id)
    assert rec is not None and [r["step"] for r in rec["not_available"]] == [VELOCITY]
    assert rec["outcome"]["accepted_steps"] == 1 and rec["outcome"]["not_available"] == 1


_CARD = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const escapeHtml = __ESCAPE__;
const sandbox = { state: { planDoc: null, planProblems: [], planDraftYaml: "" }, escapeHtml,
  renderMarkdown: (s) => escapeHtml(s) };
const api = vm.runInNewContext(src + "\n;({ planEntryHtml })", sandbox);
const line = "**Not available:** no tested tool or skill does \"RNA <velocity>\". The closest skill is `score-x`: scores.";
const out = {
  pending: api.planEntryHtml(["**QC** — Filter cells."], null, { pending: true, notAvailable: [line] }),
  saved: api.planEntryHtml(["**QC** — Filter cells."], null, { status: "Approved", notAvailable: [line] }),
};
console.log(JSON.stringify(out));
"""


def test_the_console_card_shows_each_step_that_is_not_available_before_approval():
    from test_console_plan_card_js import ESCAPE, _node
    out = _node(_CARD.replace("__ESCAPE__", ESCAPE), "function planStepParts", "function showPlanPanel")
    pending = out["pending"]
    assert 'class="plan-note unavailable"' in pending
    assert "<strong>Not available:</strong>" in pending and "<code>score-x</code>" in pending
    assert "RNA &lt;velocity&gt;" in pending, "the step's words are escaped"
    assert pending.index("plan-note unavailable") < pending.index('data-plan="approve"')
    assert "plan-note unavailable" not in out["saved"], "only the card waiting for approval lists them"


def test_a_redraft_that_holds_no_step_takes_the_record_back(monkeypatch, tmp_path):
    """The record follows the plan's held steps: a redraft that holds none takes it back, unless the
    team has handled it already."""
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AISCIENTIST_SKILL_REQUESTS", raising=False)
    lab = SimpleNamespace(unavailable_steps=[NotAvailable(step=VELOCITY).to_dict()], protocols=[],
                          induced_skills=[], approved_plan_yaml="")
    conn, req = SimpleNamespace(owner="alice"), SimpleNamespace(question="QC, then RNA velocity",
                                                                 conversation_id="conv-1")
    assert gw._record_unavailable(conn, "run0001", req, lab, {}, {})
    assert [r["step"] for r in skill_requests.load("run0001")["not_available"]] == [VELOCITY]
    lab.unavailable_steps = []
    assert not gw._record_unavailable(conn, "run0001", req, lab, {}, {})
    assert skill_requests.load("run0001") is None

    lab.unavailable_steps = [NotAvailable(step=VELOCITY).to_dict()]
    assert gw._record_unavailable(conn, "run0002", req, lab, {}, {})
    skill_requests.set_status("run0002", "planned", by="team")
    lab.unavailable_steps = []
    gw._record_unavailable(conn, "run0002", req, lab, {}, {})
    assert skill_requests.load("run0002")["status"] == "planned", "a handled record stays"
