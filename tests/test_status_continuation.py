"""Same-run status recovery is reviewed, task-bound and never an H5AD-expiry bypass."""
import asyncio
import copy
import json
from dataclasses import replace

import pytest

from aiscientist.agents import status_continuation as recovery
from aiscientist.agents.mcp_bridge import configured_servers
from aiscientist.agents.plan_doc import PlanDoc, PlanStep
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness
from aiscientist.agents.research_lab import LabConfig, ResearchLab, ResumeState
from aiscientist.gateway import app as gw, hpc_layout
from aiscientist.gateway.settings import HPCSettings

RUN = "0532008b38de"
STATUS = "mcp_phenotype_job_status"


@pytest.fixture
def original():
    names = ["mcp_map_phenotype_to_hpo", "mcp_run_lirical", "mcp_diagnose_disease"]
    plan = PlanDoc("Original synthetic research question", tuple(
        PlanStep(f"s{i}", f"**{i}. Original** — Use `{name}`.", name, {"analysis_id": "prior"})
        for i, name in enumerate(names, 1)))
    rounds = [{"round_no": i, "step_index": i, "step": step.text,
               "scientist_result": {"final_answer": f"Original finding {i}", "steps": [{
                   "tool": step.tool, "args": {"analysis_id": "prior"}, "ok": True,
                   "result": {"status": "ok", "state": "COMPLETED", "analysis_id": "prior", "task_id": f"task-{i}",
                              "mcp": {"name": "phenotype-diagnosis"}}}]},
               "verdict": {"verdict": "accept", "score": 1.0, "critique": "recorded"}}
              for i, step in enumerate(plan.steps, 1)]
    return {"question": plan.request, "agenda": plan.agenda(), "approved_plan_yaml": plan.to_yaml(),
            "rounds": rounds, "conversation_id": "conv1", "accepted_steps": 3,
            "plan_context": {}, "converged": True}


def request():
    return (f"Modify the existing run {RUN} in place. Preserve s1. Show the typed plan for approval.\n"
            f's2: {STATUS} {{"analysis_id":"prior","task_id":"task-2"}}\n'
            f's3: {STATUS} {{"analysis_id":"prior","task_id":"task-3"}}')


def connection(tmp_path, monkeypatch, state):
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    conn = gw.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace = tmp_path
    conn.executor = object()
    art = tmp_path / RUN / "artifacts"
    (art / "process").mkdir(parents=True)
    (art / "process/run_state.json").write_text(json.dumps(state))
    home = hpc_layout.session_run(conn.settings.shared_root, "123456abcdef", RUN)
    (art.parent / "hpc3_home.json").write_text(json.dumps(home.to_json()))
    return conn, art, home


def test_exact_draft_preserves_question_step1_evidence_and_original_input(original):
    before = copy.deepcopy(original)
    updated, indices = recovery.revised_state(original, request())
    assert original == before and indices == frozenset({1, 2})
    assert updated["rounds"] == original["rounds"]
    resume = ResumeState.from_run_state(updated, 1, redo_indices=indices)
    assert resume.approved_plan.steps[0].tool == "mcp_map_phenotype_to_hpo"
    assert resume.approved_plan.request == original["question"]
    assert resume.approved_plan.steps[0].text == original["agenda"][0]
    assert [s.settings["task_id"] for s in resume.approved_plan.steps[1:]] == ["task-2", "task-3"]


@pytest.mark.parametrize("change", [
    lambda s: s.replace("s2:", "s0:"),
    lambda s: s.replace("s3:", "s2:"),
    lambda s: s.replace("s3:", "s4:"),
    lambda s: s.replace(STATUS, "mcp_foreign_phenotype_job_status"),
    lambda s: s.replace('"task-2"', '"another-task"'),
    lambda s: s.replace('"prior"', '"another-analysis"'),
    lambda s: s.replace('"task-2"', 'null'),
    lambda s: s.replace('"task-2"', '"wrong","task_id":"task-2"'),
    lambda s: s.replace('"task-2"', '"../escape"'),
    lambda s: s.replace('"task-2"', '"task-2","retry":true'),
    lambda s: s.replace(f"s3: {STATUS}", "s3: mcp_run_de"),
    lambda s: s.replace(RUN, "not-a-run"),
])
def test_invalid_or_mixed_recovery_is_refused(original, change):
    with pytest.raises(ValueError):
        recovery.revised_state(original, change(request()))


def test_task_must_belong_to_that_original_step_and_server(original):
    with pytest.raises(ValueError, match="matching completed original task"):
        recovery.revised_state(original, request().replace('"task-2"', '"task-3"'))
    original["rounds"][1]["scientist_result"]["steps"][0]["result"]["mcp"]["name"] = "foreign"
    with pytest.raises(ValueError, match="matching completed original task"):
        recovery.revised_state(original, request())


def test_status_route_preserves_runhome_kept_round_and_review_no_fallback(tmp_path, monkeypatch, original):
    conn, art, home = connection(tmp_path, monkeypatch, original)
    seen = []
    async def run(c, req, **kwargs):
        seen.append((req, kwargs))
    monkeypatch.setattr(gw, "_run_lab", run)
    monkeypatch.setattr(gw, "_analysis_checkpoints_exist", lambda *_a, **_kw: pytest.fail("status needs no H5AD"))
    req = gw.LabRequest(connection_id=conn.id, question=request(), conversation_id="conv1")
    assert asyncio.run(gw._maybe_status_recovery(conn, req))
    req, out = seen[0]
    assert out["resume_run_id"] == RUN and conn.run_homes[RUN] == home
    resume = out["resume"]
    assert resume.from_step_index == 1 and resume.redo_indices == {1, 2}
    assert resume.status_recovery and resume.needs_plan_review
    assert resume.prior_rounds[0].scientist_result == original["rounds"][0]["scientist_result"]
    assert req.question == original["question"] and req.conversation_id == "conv1"
    assert json.loads((art / "process/run_state.json").read_text()) == original
    archives = list((art / "process/status_recovery").glob("*/before/process/run_state.json"))
    assert len(archives) == 1 and json.loads(archives[0].read_text()) == original
    restored = ResumeState.from_review_state(resume.to_review_state())
    assert restored.status_recovery and restored.redo_indices == {1, 2} and restored.needs_plan_review
    assert restored.reviewed_note == request()


@pytest.mark.parametrize("problem", ["conversation", "owner", "home", "task", "symlink"])
def test_wrong_target_stops_without_new_run(tmp_path, monkeypatch, original, problem):
    conn, art, _ = connection(tmp_path, monkeypatch, original)
    messages = []
    monkeypatch.setattr(conn, "push", messages.append)
    monkeypatch.setattr(gw, "_run_lab", lambda *_a, **_kw: pytest.fail("must not mint or execute a run"))
    question, conv = request(), "conv1"
    if problem == "conversation":
        conv = "other"
    elif problem == "owner":
        monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
        conn.app_user_id = "current-account"
        monkeypatch.setattr(gw.auth_routes, "get_run_record", lambda *_: None)
    elif problem == "home":
        (art.parent / "hpc3_home.json").unlink()
    elif problem == "task":
        question = question.replace('"task-2"', '"foreign"')
    else:
        p = art / "process/run_state.json"
        p.rename(tmp_path / "outside.json")
        p.symlink_to(tmp_path / "outside.json")
    req = gw.LabRequest(connection_id=conn.id, question=question, conversation_id=conv)
    assert asyncio.run(gw._maybe_status_recovery(conn, req))
    assert any(m["type"] == "chat_error" for m in messages)
    assert messages[-1]["type"] == "chat_stopped"


def test_ordinary_scrna_expiry_guard_is_unchanged(tmp_path, monkeypatch, original):
    conn, art, _ = connection(tmp_path, monkeypatch, original)
    monkeypatch.setattr(gw, "_analysis_checkpoints_exist", lambda *_a, **_kw: False)
    with pytest.raises(ValueError, match="checkpoints have expired"):
        gw._prepare_continue(conn.id, conn, RUN, art, original, 1, modify_note="run clustering")
    with pytest.raises(ValueError, match="non-status"):
        gw._prepare_continue(conn.id, conn, RUN, art, original, 1, status_steps=frozenset({1, 2}))


def status_lab(resume, calls):
    server = next(s for s in configured_servers() if s.name == "phenotype-diagnosis")
    desc = next(t for t in server.tools if t["name"] == "phenotype_job_status")
    tool = HarnessTool(STATUS, "Read original task", desc["inputSchema"],
                      lambda args, _: calls.append(dict(args)) or {"status": "ok", "state": "COMPLETED"})
    def chat(_messages, _tools):
        return {"tool_calls": [{"id": "status", "function": {"name": STATUS,
                         "arguments": '{"analysis_id":"WRONG","task_id":"WRONG"}'}}]}
    def complete(messages):
        if "Critic" in messages[0]["content"]:
            return '{"verdict":"accept","score":1,"critique":"actual task"}'
        return "Accepted evidence report."
    return ResearchLab(HarnessContext(decisions={}),
        LabConfig(mode="single", planner="linear", plan_yaml=True, plan_review=False, auto_select_skill=False),
        scientist=ResearchHarness(catalog=[tool], chat_fn=chat), complete_fn=complete)


@pytest.mark.parametrize("approve", [True, False])
def test_normal_typed_review_precedes_exact_two_status_calls(original, approve):
    updated, indices = recovery.revised_state(original, request())
    resume = replace(ResumeState.from_run_state(updated, 1, redo_indices=indices),
                     status_recovery=True, review_requested=True)
    calls, shown, events = [], [], []
    lab = status_lab(resume, calls)
    def review(kind, agenda):
        assert not calls and kind == "agenda"
        shown.append(lab._plan_doc)
        assert lab._plan_doc.steps[0].text == original["agenda"][0]
        assert [s.settings["task_id"] for s in lab._plan_doc.steps[1:]] == ["task-2", "task-3"]
        return {"action": "approve" if approve else "cancel"}
    result = lab.run(original["question"], on_event=events.append, plan_review=review, resume=resume)
    assert len(shown) == 1
    if approve:
        assert calls == [{"analysis_id": "prior", "task_id": "task-2"},
                         {"analysis_id": "prior", "task_id": "task-3"}]
        assert result.accepted_steps == 3
        assert any(e.get("type") == "run_resumed" and e["kept"] == [1] for e in events)
    else:
        assert not calls


def test_review_cannot_turn_status_draft_into_science(original):
    state, indices = recovery.revised_state(original, request())
    plan = ResumeState.from_run_state(state, 1).approved_plan
    science = replace(plan, steps=(plan.steps[0], replace(plan.steps[1], tool="mcp_run_de"), plan.steps[2]))
    with pytest.raises(ValueError, match="verified task"):
        recovery.validate_review(plan, science, indices)
    changed_s1 = replace(plan, steps=(replace(plan.steps[0], text="Changed"), *plan.steps[1:]))
    with pytest.raises(ValueError, match="retained step"):
        recovery.validate_review(plan, changed_s1, indices)


@pytest.mark.parametrize("state", ["PENDING", "PREEMPTED", "FAILED", "RUNNING", None])
def test_only_recorded_completed_tasks_qualify_for_recollection(original, state):
    original["rounds"][1]["scientist_result"]["steps"][0]["result"]["state"] = state
    with pytest.raises(ValueError, match="completed original task"):
        recovery.revised_state(original, request())
