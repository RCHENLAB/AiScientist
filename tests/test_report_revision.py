"""A report correction reviews existing evidence and cannot dispatch scientific tools."""
import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from aiscientist.gateway import app as gw, report_revision as rr

RID = "a1608e696d7b"


@pytest.fixture(autouse=True)
def registered_operations(monkeypatch):
    def operation(server, name):
        wire = {"mcp_diagnose_disease": "diagnose_disease", "mcp_cellqc_job_status": "cellqc_job_status"}[name]
        return wire, {"diagnose_disease", "cellqc_job_status", "run_cellqc"}
    monkeypatch.setattr(rr, "_registered_operation", operation)


def seed(tmp_path, server="phenotype-diagnosis", payload=None):
    art = tmp_path / RID / "artifacts"
    wire = "cellqc_job_status" if server == "cellqc" else "diagnose_disease"
    full = f"mcp/{server}/synthetic/{wire}.json"
    path = art / full
    path.parent.mkdir(parents=True)
    payload = payload or {"status": "ok", "n_candidates": 8621, "literature": [{
        "gene": "PRPF31", "association": False, "evidence_status": "unsupported", "evidence": []}]}
    path.write_text(json.dumps(payload))
    (art / "croissant.json").write_text(json.dumps({"distribution": [{"name": full,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}))
    state = {"question": "synthetic test", "conversation_id": "chat1", "agenda": ["diagnose"],
             "accepted_steps": 1, "converged": True, "final_answer": "Unreliable prior prose",
             "claim_audit": {"claims": [{"step": 1, "status": "robust", "claim": "PRPF31 supported"}]},
             "rounds": [{"step_index": 1, "verdict": {"verdict": "accept"}, "scientist_result": {
                 "steps": [{"tool": "mcp_" + wire, "ok": True, "args": {"analysis_id": "synthetic"}, "result": {
                     "status": "ok", "analysis_id": "synthetic", "task_id": "task-one",
                     "response_omitted": True, "full_result": full,
                     "mcp": {"name": server, "version": "0.0.4"}}}]}}]}
    (art / "process").mkdir()
    (art / "process/run_state.json").write_text(json.dumps(state))
    (art / "report").mkdir()
    (art / "report/plan.yaml").write_text("original scientific plan")
    (art / "report/report.md").write_text("original report")
    (art / "report/summary.md").write_text("historical unsupported claim")
    return art, state


def test_durable_review_does_not_replace_original_scientific_state_or_plan(tmp_path):
    art, state = seed(tmp_path)
    original = (art / "process/run_state.json").read_bytes()
    folder = rr.create(art, RID, "chat1", "Revise only the report")
    assert rr.prompt(folder)["run_id"] == RID
    assert list(rr.pending(tmp_path))[0][0] == folder
    rr.decide(folder, "approve")
    assert not list(rr.pending(tmp_path))
    assert (art / "process/run_state.json").read_bytes() == original
    assert (art / "report/plan.yaml").read_text() == "original scientific plan"
    rr.archive_report(art, folder)
    assert (folder / "prior_report/report.md").read_text() == "original report"


def test_hydration_preserves_false_evidence_and_downgrades_stale_claim_only_in_copy(tmp_path):
    art, state = seed(tmp_path)
    before = copy.deepcopy(state)
    view, evidence = rr.hydrate(art, state, "Correct unsupported PRPF31 claim")
    result = view["rounds"][0]["scientist_result"]["steps"][0]["result"]
    assert result["literature"][0]["association"] is False
    assert '"evidence_status": "unsupported"' in view["final_answer"]
    assert "Unreliable prior prose" not in view["final_answer"]
    assert view["claim_audit"]["claims"][0]["status"] == "unclear"
    assert state == before and evidence["scientific_calls"] == 0
    assert len(evidence["full_results"][0]["sha256"]) == 64


@pytest.mark.parametrize("relative", ["../outside.json", "/tmp/outside.json", "mcp/link/data.json"])
def test_hydration_rejects_path_escape_and_symlinks(tmp_path, relative):
    art, _ = seed(tmp_path)
    if "link" in relative:
        (art / "mcp/link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        rr.confined(art, relative)


def test_cross_conversation_or_empty_source_refused(tmp_path):
    art, state = seed(tmp_path)
    with pytest.raises(ValueError, match="different conversation"):
        rr.load_target(tmp_path, RID, "chat2")
    state["rounds"] = []
    (art / "process/run_state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="no accepted"):
        rr.load_target(tmp_path, RID, "chat1")


def test_review_can_change_instructions_but_cannot_add_scientific_execution(tmp_path):
    art, _ = seed(tmp_path)
    folder = rr.create(art, RID, "chat1", "Correct report")
    record = rr.decide(folder, "revise", feedback="Keep the negative evidence")
    assert "negative evidence" in record["instructions"]
    bad = rr.prompt(folder)["plan"]["yaml"].replace("tool: write_report", "tool: mcp_run_cellqc")
    with pytest.raises(ValueError, match="cannot change"):
        rr.decide(folder, "edit", plan_yaml=bad)
    assert rr.prompt(folder)


def connection(tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    messages = []
    conn = SimpleNamespace(workspace=tmp_path, id="conn", last_run_id=RID, app_user_id=None,
                           last_run_by_conversation={"chat1": RID}, push=messages.append)
    return conn, messages


def test_report_only_routes_to_durable_review_without_lab_or_scientific_calls(tmp_path, monkeypatch):
    seed(tmp_path)
    conn, messages = connection(tmp_path, monkeypatch)
    monkeypatch.setattr(gw, "_model_ready", lambda _: True)
    monkeypatch.setattr(gw, "_lab_llm", lambda _: (lambda _: json.dumps({
        "intent": "report_only", "confidence": 1, "reason": "correct existing report"}),))
    monkeypatch.setattr(gw, "_run_lab", lambda *a, **k: pytest.fail("a new analysis must not start"))
    req = gw.LabRequest(connection_id="conn", conversation_id="chat1", question=f"Revise only report for run {RID}")
    assert asyncio.run(gw._maybe_report_revision(conn, req))
    assert messages[0]["type"] == "plan_prompt" and messages[0]["run_id"] == RID
    assert len(messages[0]["plan"]["steps"]) == 1


def test_explicit_foreign_owner_target_is_not_fallback_to_another_run(tmp_path, monkeypatch):
    seed(tmp_path)
    conn, _ = connection(tmp_path, monkeypatch)
    conn.app_user_id = 1
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw.auth_routes, "get_run_record", lambda *a: None)
    req = gw.LabRequest(connection_id="conn", conversation_id="chat1", question=f"Revise report for run {RID}")
    with pytest.raises(ValueError, match="not a run in this account"):
        gw._report_target(conn, req)


def test_report_only_without_accepted_source_never_creates_empty_new_study(tmp_path, monkeypatch):
    conn, messages = connection(tmp_path, monkeypatch)
    req = gw.LabRequest(connection_id="conn", conversation_id="chat1", question="Revise only the report")
    assert asyncio.run(gw._maybe_report_revision(conn, req))
    assert messages[-1]["type"] == "chat_error"


def test_classifier_keeps_new_science_outside_report_only():
    assert '"report_only"' in gw._FOLLOWUP_ROUTER_SYS
    assert gw._parse_followup_intent('{"intent":"report_only","confidence":1}', ["QC"])["intent"] == "report_only"
    assert gw._parse_followup_intent('{"intent":"continue","confidence":1}', ["QC"])["intent"] == "continue"


def test_manifest_resolves_only_exact_nested_resumed_artifact_with_verified_hash(tmp_path):
    import hashlib
    art, state = seed(tmp_path, "cellqc", {"status": "ok", "cells_after": 26504})
    direct = art / "mcp/cellqc/synthetic/cellqc_job_status.json"
    nested = art / "extra/mcp/mcp/cellqc/synthetic/cellqc_job_status.json"
    nested.parent.mkdir(parents=True)
    direct.rename(nested)
    manifest = {"distribution": [{"name": str(nested.relative_to(art)),
                                  "sha256": hashlib.sha256(nested.read_bytes()).hexdigest()}]}
    (art / "croissant.json").write_text(json.dumps(manifest))
    view, evidence = rr.hydrate(art, state, "Correct QC report")
    assert "26504" in view["final_answer"]
    assert evidence["full_results"][0]["path"] == str(nested.relative_to(art))
    manifest["distribution"][0]["sha256"] = "0" * 64
    (art / "croissant.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="checksum"):
        rr.hydrate(art, state, "Correct QC report")


def test_after_restart_approval_reuses_report_worker_and_unchanged_source(tmp_path, monkeypatch):
    art, _ = seed(tmp_path)
    folder = rr.create(art, RID, "chat1", "Correct report")
    original = (art / "process/run_state.json").read_bytes()
    conn, _ = connection(tmp_path, monkeypatch)
    conn.status, conn.executor = "ready", object()
    conn.begin_run = lambda *a, **k: SimpleNamespace(kind="", running=False)
    monkeypatch.setattr(gw, "_model_ready", lambda _: True)
    monkeypatch.setattr(gw, "_run_slot_refusal", lambda *a: None)
    monkeypatch.setattr(gw, "_budget_refusal", lambda *a: None)
    monkeypatch.setattr(gw, "_note_budget_choice", lambda *a: None)
    calls = []
    async def writer(*args):
        calls.append(args)
    monkeypatch.setattr(gw, "_run_report_revision", writer)
    async def approve():
        req = gw.PlanReviewRequest(connection_id="conn", run_id=RID, conversation_id="chat1", action="approve")
        response = await gw._report_revision_review(conn, req, "approve")
        await asyncio.sleep(0)
        return response
    assert asyncio.run(approve()).status_code == 200
    assert calls[0][2] == art and calls[0][3] == folder
    assert (art / "process/run_state.json").read_bytes() == original
    assert json.loads((folder / "revision.json").read_text())["state"] == "approved"


def test_cancel_or_budget_refusal_never_starts_writer(tmp_path, monkeypatch):
    art, _ = seed(tmp_path)
    folder = rr.create(art, RID, "chat1", "Correct report")
    conn, _ = connection(tmp_path, monkeypatch)
    conn.status, conn.executor = "ready", object()
    monkeypatch.setattr(gw, "_model_ready", lambda _: True)
    monkeypatch.setattr(gw, "_run_slot_refusal", lambda *a: None)
    monkeypatch.setattr(gw, "_budget_refusal", lambda *a: gw.JSONResponse({"error": "budget"}, status_code=409))
    monkeypatch.setattr(gw, "_run_report_revision", lambda *a: pytest.fail("writer must not run"))
    req = gw.PlanReviewRequest(connection_id="conn", run_id=RID, conversation_id="chat1")
    assert asyncio.run(gw._report_revision_review(conn, req, "approve")).status_code == 409
    assert rr.prompt(folder)
    assert asyncio.run(gw._report_revision_review(conn, req, "cancel")).status_code == 200
    assert json.loads((folder / "revision.json").read_text())["state"] == "cancelled"


def test_source_changed_since_review_requires_new_review(tmp_path, monkeypatch):
    art, _ = seed(tmp_path)
    rr.create(art, RID, "chat1", "Correct report")
    conn, _ = connection(tmp_path, monkeypatch)
    state_path = art / "process/run_state.json"
    state = json.loads(state_path.read_text())
    state["final_answer"] = "Updated by later accepted step"
    state_path.write_text(json.dumps(state))
    req = gw.PlanReviewRequest(connection_id="conn", run_id=RID, conversation_id="chat1")
    response = asyncio.run(gw._report_revision_review(conn, req, "approve"))
    assert response.status_code == 400 and b"source run changed" in response.body


def test_existing_report_worker_receives_accepted_artifact_view_without_rewriting_state(tmp_path, monkeypatch):
    import threading
    art, state = seed(tmp_path)
    folder = rr.create(art, RID, "chat1", "Correct negative evidence")
    record = rr.decide(folder, "approve")
    original = (art / "process/run_state.json").read_bytes()
    received = []
    async def write(conn, target, result, question, **kwargs):
        received.append((target, result, question, kwargs))
        return {"status": "ok", "rendered": []}
    monkeypatch.setattr(gw, "_write_report", write)
    monkeypatch.setattr(gw, "_check_figure_shapes", lambda *a: "")
    monkeypatch.setattr(gw, "_lab_llm", lambda _: SimpleNamespace(complete_fn=lambda *a: None))
    monkeypatch.setattr(gw, "_llm_provenance_note", lambda _: "selected model")
    monkeypatch.setattr(gw, "_build_report_render_fn", lambda *a: None)
    conn = SimpleNamespace(chat_stop=threading.Event())
    asyncio.run(gw._report_job(conn, "tester", RID, art, revision=(folder, record)))
    assert len(received) == 1 and received[0][0] == art
    assert '"association": false' in received[0][1].final_answer
    assert received[0][1].accepted_steps == 1 and len(received[0][1].rounds) == 1
    assert received[0][3]["qc_report"] is False
    assert (art / "process/run_state.json").read_bytes() == original
    assert json.loads((folder / "grounding.json").read_text())["scientific_calls"] == 0
    assert json.loads((art / "process/report_job.json").read_text())["state"] == "done"
    assert "[report.md](report.md)" in (art / "report/summary.md").read_text()
    assert (folder / "prior_report/summary.md").read_text() == "historical unsupported claim"


def test_stopped_revision_never_calls_model_or_writer(tmp_path, monkeypatch):
    import threading
    art, _ = seed(tmp_path)
    folder = rr.create(art, RID, "chat1", "Correct report")
    record = rr.decide(folder, "approve")
    stopped = threading.Event()
    stopped.set()
    run = SimpleNamespace(chat_stop=stopped, running=True)
    conn = SimpleNamespace(owner="tester", push=lambda *a, **k: None, end_run=lambda _: None)
    monkeypatch.setattr(gw, "_report_job", lambda *a, **k: pytest.fail("a stopped revision cannot write"))
    asyncio.run(gw._run_report_revision(conn, run, art, folder, record))
    assert json.loads((folder / "revision.json").read_text())["state"] == "cancelled"
    assert run.running is False
    assert json.loads((art / "process/report_job.json").read_text())["state"] == "cancelled"


@pytest.mark.parametrize("ready,response", [(False, None), (True, "not JSON"), (True, '{"intent":"report_only","confidence":0.2}')])
def test_explicit_report_only_uncertainty_cannot_fall_through_to_new_study(tmp_path, monkeypatch, ready, response):
    seed(tmp_path)
    conn, messages = connection(tmp_path, monkeypatch)
    monkeypatch.setattr(gw, "_model_ready", lambda _: ready)
    monkeypatch.setattr(gw, "_lab_llm", lambda _: (lambda _: response,))
    request = gw.LabRequest(connection_id="conn", conversation_id="chat1", question="Revise only the report")
    assert asyncio.run(gw._maybe_report_revision(conn, request))
    assert messages[-1]["type"] == "chat_error"
    assert "no new analysis" in messages[-1]["message"]


def test_direct_full_result_tampering_is_rejected(tmp_path):
    art, state = seed(tmp_path)
    path = art / state["rounds"][0]["scientist_result"]["steps"][0]["result"]["full_result"]
    path.write_text('{"status":"ok","literature":[{"association":true}]}')
    with pytest.raises(ValueError, match="checksum"):
        rr.hydrate(art, state, "Correct report")


def test_direct_full_result_replacement_task_is_rejected_even_with_new_manifest(tmp_path):
    art, state = seed(tmp_path, payload={"status": "ok", "task_id": "later-task"})
    with pytest.raises(ValueError, match="task identity"):
        rr.hydrate(art, state, "Correct report")


@pytest.mark.parametrize("state_name", ["FAILED", "CANCELLED", "TIMEOUT", "OOM", "OUT_OF_MEMORY"])
def test_failed_state_cannot_become_successful_report_evidence(tmp_path, state_name):
    art, state = seed(tmp_path, payload={"status": "ok", "state": state_name})
    with pytest.raises(ValueError, match="contradicts"):
        rr.hydrate(art, state, "Correct report")


def task_snapshot(art, state):
    step = state["rounds"][0]["scientist_result"]["steps"][0]
    old = art / step["result"]["full_result"]
    name = "mcp/phenotype-diagnosis/synthetic/tasks/task-one/diagnose_disease.json"
    new = art / name
    new.parent.mkdir(parents=True)
    payload = json.loads(old.read_text())
    payload.update(analysis_id="synthetic", task_id="task-one")
    new.write_text(json.dumps(payload))
    step["result"]["full_result"] = name
    owner = {"server": "phenotype-diagnosis", "analysis_id": "synthetic", "task_id": "task-one",
             "operation": "diagnose_disease", "sha256": hashlib.sha256(new.read_bytes()).hexdigest()}
    (art / "mcp/.compatibility.json").write_text(json.dumps({"evidence": {name: owner}}))
    return new, name, owner


def test_task_snapshot_requires_matching_owner_and_embedded_identity(tmp_path):
    art, state = seed(tmp_path)
    path, name, owner = task_snapshot(art, state)
    view, evidence = rr.hydrate(art, state, "Correct report")
    assert evidence["full_results"][0]["ownership"] == "task"
    assert '"evidence_operation": "diagnose_disease"' in view["final_answer"]
    payload = json.loads(path.read_text())
    del payload["task_id"]
    path.write_text(json.dumps(payload))
    owner["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (art / "mcp/.compatibility.json").write_text(json.dumps({"evidence": {name: owner}}))
    with pytest.raises(ValueError, match="task identity"):
        rr.hydrate(art, state, "Correct report")


def test_task_owner_replacement_is_not_hidden_by_manifest_fallback(tmp_path):
    art, state = seed(tmp_path)
    path, name, owner = task_snapshot(art, state)
    owner["task_id"] = "later-task"
    (art / "mcp/.compatibility.json").write_text(json.dumps({"evidence": {name: owner}}))
    with pytest.raises(ValueError, match="owner differs"):
        rr.hydrate(art, state, "Correct report")


def test_legacy_alias_shared_by_different_accepted_tasks_requires_recollection(tmp_path):
    art, state = seed(tmp_path)
    later = copy.deepcopy(state["rounds"][0])
    later["scientist_result"]["steps"][0]["result"]["task_id"] = "later-task"
    state["rounds"].append(later)
    with pytest.raises(ValueError, match="Multiple accepted tasks"):
        rr.hydrate(art, state, "Correct report")


def test_main_guard_runs_after_report_route_definitions():
    import inspect
    source = inspect.getsource(gw)
    assert source.index('if __name__ == "__main__":') > source.index("async def _run_report_revision(")


def test_failed_revision_preserves_current_summary_and_archived_history(tmp_path, monkeypatch):
    import threading
    art, _ = seed(tmp_path)
    folder = rr.create(art, RID, "chat1", "Correct report")
    record = rr.decide(folder, "approve")
    def unavailable(_):
        raise ValueError("selected model unavailable")
    monkeypatch.setattr(gw, "_lab_llm", unavailable)
    asyncio.run(gw._report_job(SimpleNamespace(chat_stop=threading.Event()), "tester", RID, art,
                             revision=(folder, record)))
    assert json.loads((art / "process/report_job.json").read_text())["state"] == "error"
    assert (art / "report/summary.md").read_text() == "historical unsupported claim"
    assert (folder / "prior_report/summary.md").read_text() == "historical unsupported claim"


def test_owned_nested_resumed_snapshot_uses_same_bounded_packaging_paths(tmp_path):
    art, state = seed(tmp_path)
    source, name, owner = task_snapshot(art, state)
    nested = art / "extra/mcp" / name
    nested.parent.mkdir(parents=True)
    source.rename(nested)
    compat = art / "extra/mcp/mcp/.compatibility.json"
    (art / "mcp/.compatibility.json").rename(compat)
    view, evidence = rr.hydrate(art, state, "Correct report")
    assert evidence["full_results"][0]["ownership"] == "task"
    assert evidence["full_results"][0]["path"] == str(nested.relative_to(art))
    assert '"association": false' in view["final_answer"]
    source.write_bytes(nested.read_bytes())
    with pytest.raises(ValueError, match="ambiguous"):
        rr.hydrate(art, state, "Correct report")


def test_stop_during_writer_keeps_summary_and_reports_cancelled_consistently(tmp_path, monkeypatch):
    import threading
    art, _ = seed(tmp_path)
    folder = rr.create(art, RID, "chat1", "Correct report")
    record = rr.decide(folder, "approve")
    stop = threading.Event()
    async def write(*args, **kwargs):
        stop.set()
        return {"status": "markdown_only", "rendered": []}
    monkeypatch.setattr(gw, "_write_report", write)
    monkeypatch.setattr(gw, "_check_figure_shapes", lambda *a: "")
    monkeypatch.setattr(gw, "_lab_llm", lambda _: SimpleNamespace(complete_fn=lambda *a: None))
    monkeypatch.setattr(gw, "_llm_provenance_note", lambda _: "selected model")
    monkeypatch.setattr(gw, "_build_report_render_fn", lambda *a: None)
    monkeypatch.setattr(gw, "report_job_status", lambda *a: gw._read_report_job(art))
    run = SimpleNamespace(chat_stop=stop, running=True)
    conn = SimpleNamespace(owner="tester", chat_stop=stop, push=lambda *a, **k: None, end_run=lambda _: None)
    asyncio.run(gw._run_report_revision(conn, run, art, folder, record))
    assert json.loads((folder / "revision.json").read_text())["state"] == "cancelled"
    assert gw._read_report_job(art)["state"] == "cancelled"
    assert (art / "report/summary.md").read_text() == "historical unsupported claim"
