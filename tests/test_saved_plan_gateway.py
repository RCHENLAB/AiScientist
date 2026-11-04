"""Saved reviews cross the gateway worker, HTTP reply and reconnect boundaries offline."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from starlette.requests import Request

from aiscientist.agents.plan_doc import parse_plan_yaml
from aiscientist.agents.research_lab import ResearchLab as _ResearchLab
from aiscientist.gateway import app as gw, saved_plans


def _show(conn, conversation="conv-A"):
    req = gw.LabRequest(connection_id=conn.id, question="QC cells", conversation_id=conversation,
                        plan_mode=True, mode="single", planner="linear")
    asyncio.run(gw._run_lab(conn, req))
    found = gw._find_saved_plan(conn, None, conversation)
    assert found is not None, conn.log
    return found


def _reply(conn, action, **kwargs):
    async def run():
        req = gw.PlanReviewRequest(connection_id=conn.id, action=action, **kwargs)
        response = await gw.lab_plan_review(req, Request({"type": "http", "headers": []}))
        # Await the task created by the real endpoint rather than ending its event loop early.
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks)
        return response
    response = asyncio.run(run())
    assert response.status_code == 200, response.body
    return json.loads(response.body)


def test_plan_is_durable_before_card_and_three_waiting_chats_release_all_slots(lab_gateway):
    env = lab_gateway
    conn = env.connection()
    for cid in ("conv-A", "conv-B", "conv-C"):
        rid, art, current, state = _show(conn, cid)
        assert current["source"] == "draft"
        assert state["conversation_id"] == cid and state["run_options"]["planner"] == "linear"
        assert "guidance" in state and "plan_context" in state and "lab_config" in state
        assert not conn.live_runs and not conn.chat_running
        assert any(row[1:3] == (rid, "awaiting_plan") for row in env.records)
    assert len(env.cards) == 3
    assert gw._run_slot_refusal(conn, "conv-D") is None
    _show(conn, "conv-D")
    assert len(env.cards) == 4 and not env.calls


@pytest.mark.parametrize("action", ["revise", "edit"])
def test_reply_and_edit_use_changed_disk_plan_after_restart(lab_gateway, action):
    env = lab_gateway
    first = env.connection()
    rid, art, _, _ = _show(first)
    path = art / "plan" / "v01.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("max_pct_mt: 20", "max_pct_mt: 25"),
                    encoding="utf-8")
    gw.CONNECTIONS.pop(first.id)
    restored = env.connection()
    with TestClient(gw.app).websocket_connect(f"/ws/{restored.id}") as ws:
        assert ws.receive_json()["type"] == "status"
        card = ws.receive_json()
        assert card["type"] == "plan_prompt" and card["run_id"] == rid
        assert card["conversation_id"] == "conv-A" and "max_pct_mt: 25" in card["plan"]["yaml"]
    options = {"conversation_id": "conv-A", "feedback": "Set min_genes to 300"}
    if action == "edit":
        # The editor's text is the document the card showed; the edit adds a setting to it.
        doc = parse_plan_yaml(saved_plans.load(art)["yaml"])
        step = doc.steps[0]
        options["plan_yaml"] = replace(doc, steps=(
            replace(step, settings={**step.settings, "min_genes": 300}),)).to_yaml()
    assert _reply(restored, action, **options)["run_id"] == rid
    latest = saved_plans.load(art)
    assert latest["version"] == 2
    assert latest["source"] == ("edit" if action == "edit" else "reply")
    settings = parse_plan_yaml(latest["yaml"]).steps[0].settings
    assert settings == {"max_pct_mt": 25, "min_genes": 300}
    assert not restored.live_runs
    assert env.cards[-1]["plan"]["yaml"] == latest["yaml"]


def test_run_after_restart_executes_current_settings_without_a_planner_call(lab_gateway):
    env = lab_gateway
    first = env.connection()
    rid, art, _, _ = _show(first)
    path = art / "plan" / "v01.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("max_pct_mt: 20", "max_pct_mt: 25"),
                    encoding="utf-8")
    gw.CONNECTIONS.pop(first.id)
    restored = env.connection()
    models_before = len(env.models)
    _reply(restored, "approve", run_id=rid)
    assert env.calls and env.calls[0]["max_pct_mt"] == 25
    assert all("Principal Investigator of a bioinformatics lab" not in m for m in env.models[models_before:])
    approved = saved_plans.load(art)
    assert approved["state"] == "approved"
    assert (art / "report" / "plan.yaml").read_text(encoding="utf-8") == approved["yaml"]
    assert not gw._saved_plan_replays(restored)


def test_reply_without_saved_plan_reports_not_delivered(lab_gateway):
    conn = lab_gateway.connection()
    response = _reply(conn, "revise", conversation_id="missing", feedback="raise the limit")
    assert response["status"] == "not_delivered" and "Not delivered" in response["message"]


def test_cancel_keeps_versions_and_never_calls_the_model(lab_gateway):
    env = lab_gateway
    conn = env.connection()
    rid, art, _, _ = _show(conn)
    before = len(env.models)
    assert _reply(conn, "cancel", run_id=rid)["action"] == "cancel"
    assert len(env.models) == before
    assert saved_plans.load(art)["state"] == "cancelled"
    assert len(list((art / "plan").glob("v*.yaml"))) == 2
    assert not gw._saved_plan_replays(conn) and not conn.live_runs


def test_dispatch_cleanup_only_closes_its_own_scope(lab_gateway, monkeypatch):
    conn = lab_gateway.connection()
    other = None

    async def dispatch(c, req):
        nonlocal other
        # The original scope ended; another chat now owns the session's newest live run.
        c.end_run(c.active_run)
        other = c.begin_run("other-chat", "other-run")
        other.running = True

    monkeypatch.setattr(gw, "_dispatch_lab_run", dispatch)
    req = gw.LabRequest(connection_id=conn.id, question="QC", conversation_id="first-chat")
    asyncio.run(gw._dispatch_lab(conn, req))
    assert conn.live_runs == [other] and other.running


def test_the_card_shows_the_lab_document_and_the_report_holds_no_record(lab_gateway):
    env = lab_gateway
    conn = env.connection()
    rid, art, current, _ = _show(conn)
    card = env.cards[-1]
    # The plan document as the lab wrote it, header comments included; its record stays on disk.
    assert card["plan"]["yaml"].startswith("# AiScientist plan.")
    assert "_review" not in card["plan"]["yaml"]
    assert "_review:" in (art / "plan" / current["filename"]).read_text(encoding="utf-8")
    _reply(conn, "approve", run_id=rid)
    report = (art / "report" / "plan.yaml").read_text(encoding="utf-8")
    assert "_review" not in report and report == saved_plans.load(art)["yaml"]


@pytest.mark.parametrize("broken", ["steps: [unclosed\n", "plan: 1\nsteps: []\n"])
def test_a_saved_plan_that_cannot_be_read_does_not_break_the_socket_or_a_reply(lab_gateway, broken):
    env = lab_gateway
    conn = env.connection()
    rid_a, art_a, current_a, _ = _show(conn, "conv-A")
    _show(conn, "conv-B")
    # Chat A's version edited by hand into YAML that does not parse, or into a plan with no steps.
    path = art_a / "plan" / current_a["filename"]
    text = path.read_text(encoding="utf-8")
    path.write_text(broken + text[text.index("# --- Saved by AiScientist"):], encoding="utf-8")
    gw.CONNECTIONS.pop(conn.id)
    restored = env.connection()
    replays = gw._saved_plan_replays(restored)
    assert [p["conversation_id"] for p in replays] == ["conv-B"]
    with TestClient(gw.app).websocket_connect(f"/ws/{restored.id}") as ws:
        assert ws.receive_json()["type"] == "status"
        assert ws.receive_json()["conversation_id"] == "conv-B"
    assert _reply(restored, "approve", conversation_id="conv-B")["status"] == "ok"


def test_the_scan_reads_only_the_plans_that_wait(lab_gateway, monkeypatch):
    env = lab_gateway
    conn = env.connection()
    rid, art, _, _ = _show(conn, "conv-A")
    _show(conn, "conv-B")
    _reply(conn, "cancel", run_id=rid)
    loaded = []
    real_load = saved_plans.load
    monkeypatch.setattr(saved_plans, "load", lambda a: loaded.append(a) or real_load(a))
    assert [p["conversation_id"] for p in gw._saved_plan_replays(conn)] == ["conv-B"]
    assert art not in loaded


def test_the_saved_card_keeps_each_steps_expected_outputs(lab_gateway, monkeypatch):
    env = lab_gateway
    outputs = [{"path": "figures/qc_violin.png", "kind": "figure", "required": True,
                "description": "QC metrics per cell"}]
    real = _ResearchLab._public_plan_steps     # the class, not the fixture's factory

    def with_outputs(self, doc):
        steps = real(self, doc)
        for step in steps:
            step["outputs"] = outputs          # what a skill declaring this output would add
        return steps

    monkeypatch.setattr(_ResearchLab, "_public_plan_steps", with_outputs)
    first = env.connection()
    rid, art, current, _ = _show(first)
    assert env.cards[-1]["plan"]["steps"][0]["outputs"] == outputs
    # After a restart the card is drawn from the file, with the same expected outputs.
    gw.CONNECTIONS.pop(first.id)
    restored = env.connection()
    card = gw._saved_plan_replays(restored)[0]
    assert card["plan"]["steps"][0]["outputs"] == outputs and "step_outputs" not in card
    # A step whose tool was changed by hand no longer claims the old tool's outputs.
    path = art / "plan" / current["filename"]
    document, mark, record = path.read_text(encoding="utf-8").partition("# --- Saved by AiScientist")
    path.write_text(document.replace("tool: run_scanpy_qc", "tool: run_code") + mark + record,
                    encoding="utf-8")
    assert "outputs" not in gw._saved_plan_replays(restored)[0]["plan"]["steps"][0]


def test_a_reply_that_names_no_chat_does_not_reach_a_saved_plan(lab_gateway):
    env = lab_gateway
    conn = env.connection()
    rid, art, _, _ = _show(conn, "conv-A")
    # No run id and no chat (an older client): it must not pick the newest waiting plan of any chat.
    assert _reply(conn, "approve")["status"] == "not_delivered"
    assert saved_plans.load(art)["state"] == "shown" and not env.calls


def test_approving_a_saved_plan_keeps_the_budget_answer_on_its_run(lab_gateway, monkeypatch):
    env = lab_gateway
    conn = env.connection()
    rid, _, _, _ = _show(conn)
    seen = []

    async def restage(c, decisions, say, emit):
        run = c.active_run
        seen.append((run.budget_ack, run.budget_tz))

    monkeypatch.setattr(gw, "_restage_server_data", restage)
    _reply(conn, "approve", run_id=rid, budget_ack=True, tz="America/Los_Angeles")
    # The in-run budget check (key_guard) reads these: the user already said yes once.
    assert seen == [(True, "America/Los_Angeles")]


@pytest.mark.parametrize("action", ["approve", "revise"])
def test_a_reply_or_run_that_fails_before_a_new_version_leaves_the_plan_waiting(
        lab_gateway, monkeypatch, action):
    env = lab_gateway
    conn = env.connection()
    rid, art, current, _ = _show(conn)

    async def unreachable(*args):
        raise gw.GatewayError("The dataset could not be copied to HPC3 again.", stage="data")

    if action == "approve":
        monkeypatch.setattr(gw, "_restage_server_data", unreachable)
    else:
        def broken(self, *args, **kwargs):
            raise RuntimeError("the model endpoint went away")
        monkeypatch.setattr(_ResearchLab, "_revise_plan_document", broken)
    cards = len(env.cards)
    _reply(conn, action, run_id=rid, feedback="Set min_genes to 300")
    # The version the reply started from is still the plan: the row, the file and the chat agree.
    latest = saved_plans.load(art)
    assert latest["state"] == "shown" and latest["version"] == current["version"]
    assert env.records[-1][1:3] == (rid, "awaiting_plan")
    assert len(env.cards) == cards + 1 and env.cards[-1]["run_id"] == rid
    assert env.cards[-1]["conversation_id"] == "conv-A" and env.cards[-1]["saved"] is True
    assert not conn.live_runs and not gw._SAVED_PLAN_TASKS
    assert [p["run_id"] for p in gw._saved_plan_replays(conn)] == [rid]


def test_stop_while_the_pi_redrafts_leaves_the_saved_plan_waiting(lab_gateway, monkeypatch):
    env = lab_gateway
    conn = env.connection()
    rid, art, current, _ = _show(conn)

    def stopped_midway(self, *args, **kwargs):
        conn.active_run.chat_stop.set()          # the user pressed Stop during the PI's call
        return None                              # no patch: the lab redrafts, and sees the Stop

    monkeypatch.setattr(_ResearchLab, "_revise_plan_document", stopped_midway)
    _reply(conn, "revise", run_id=rid, feedback="Set min_genes to 300")
    latest = saved_plans.load(art)
    assert latest["state"] == "shown" and latest["version"] == current["version"]
    assert env.records[-1][1:3] == (rid, "awaiting_plan")
    assert env.cards[-1]["run_id"] == rid and not env.calls


def test_a_saved_plan_that_continues_an_earlier_run_keeps_it_when_approved(lab_gateway, monkeypatch):
    env = lab_gateway
    conn = env.connection()
    prior = {"run_id": "earlier00001", "question": "QC the cells",
             "steps": [{"title": "QC", "done": True, "finding": "kept 900 cells"}],
             "dataset": None, "plan_yaml": "", "session": [], "bound_primary": ""}
    monkeypatch.setattr(gw, "_handed_on_dataset", lambda c, p: None)
    req = gw.LabRequest(connection_id=conn.id, question="Cluster them", conversation_id="conv-A",
                        plan_mode=True, mode="single", planner="linear")
    asyncio.run(gw._run_lab(conn, req, prior=prior))
    rid, art, _, state = gw._find_saved_plan(conn, None, "conv-A")
    assert state["prior"]["run_id"] == "earlier00001"
    # The dataset this run starts from is the one the earlier run handed on (its profile says so).
    path = art / "process" / "run_state.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["decisions"]["dataset_result"] = {"handed_on": True}
    path.write_text(json.dumps(saved), encoding="utf-8")
    adopted = []

    async def adopt(c, run_id, decisions, executor):
        adopted.append((run_id, c.active_run.continues))
        return {"checkpoints": ["adata_qc"]}

    monkeypatch.setattr(gw, "_adopt_handed_on_dataset", adopt)
    _reply(conn, "revise", run_id=rid, feedback="Set min_genes to 300")
    assert adopted == []                 # a reply only plans
    # Run, possibly in a later session whose HPC3 folder never held the adopted checkpoints: they
    # are written again, and the earlier run is known as the one this run continues.
    _reply(conn, "approve", run_id=rid)
    assert adopted == [(rid, "earlier00001")]


def test_a_plan_saved_before_a_deploy_that_drops_a_setting_still_runs(lab_gateway):
    env = lab_gateway
    conn = env.connection()
    rid, art, _, _ = _show(conn)
    path = art / "process" / "run_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["lab_config"]["retired_setting"] = 1
    state["lab_config"]["scientist"]["retired_setting"] = 1
    path.write_text(json.dumps(state), encoding="utf-8")
    _reply(conn, "approve", run_id=rid)
    assert env.calls and saved_plans.load(art)["state"] == "approved"
