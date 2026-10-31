"""Run timing through gateway routing, review endpoints, literature and result-saving seams.

All transports are local fakes. The real gateway/model wrappers, plan waits, files and save
operations run, so a future missing call-site binding cannot pass by only testing the timer.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from aiscientist.gateway import app as gw, run_monitor, run_timing, server_data, vllm_client
from aiscientist.gateway.errors import GatewayError
from aiscientist.gateway.settings import HPCSettings
from test_literature_route import _Model
from test_saved_plan_gateway import _reply, _show

pytest_plugins = ("test_saved_plan_gateway",)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    monkeypatch.setattr(gw, "CONNECTIONS", {})
    monkeypatch.setattr(gw, "_record_lab_usage", lambda *a, **kw: None)
    monkeypatch.setattr(gw, "_bound_credential", lambda conn: None)
    monkeypatch.setenv("AISCIENTIST_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.delenv("AISCIENTIST_LAB_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AISCIENTIST_DATA_ROOT", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    current = gw._CURRENT_RUN.set(None)
    with run_monitor.bind_monitor(None):
        yield
    gw._CURRENT_RUN.reset(current)


@pytest.fixture
def clock(monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(run_timing.time, "time", lambda: now[0])
    monkeypatch.setattr(run_timing.time, "monotonic", lambda: now[0] - 1_700_000_000)
    return now


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "CONSOLE_RUNS_DIR", tmp_path / "runs")
    conn = gw.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace = gw.CONSOLE_RUNS_DIR / conn.owner
    conn.status, conn.executor, conn.alloc = "ready", object(), object()
    conn.selected_model = "offline"
    gw.CONNECTIONS[conn.id] = conn
    yield conn
    conn.loop.close()


def _watch(conn, run_id=None):
    run = conn.begin_run("conversation", run_id)
    req = gw.LabRequest(connection_id=conn.id, conversation_id="conversation", question="A research question")
    gw._watch_run(conn, run, req)
    return run, req


def _timeline(conn, run_id):
    return run_monitor.read_timing(gw._timing_path(conn.workspace, run_id))


def test_routing_model_span_is_bound_with_final_run_before_server_staging(connection, clock, monkeypatch, tmp_path):
    conn = connection
    run, req = _watch(conn)
    monitor = run.monitor

    def complete(*a, **kw):
        clock[0] += 7
        return '{"route":"analysis","confidence":0.99,"reason":"QC"}', {}

    monkeypatch.setattr(vllm_client, "complete_ex", complete)
    assert not gw._route_request(conn, req).literature
    conn.bind_run_id(run, "boundrun")
    gw._watch_run(conn, run, req)
    assert run.monitor is monitor
    assert gw._timing_path(conn.workspace, "boundrun").is_file()
    src = tmp_path / "input.h5ad"
    src.write_bytes(b"data")

    def put(*a, **kw):
        clock[0] += 12

    scan = server_data.Scan(src, tmp_path, False, copy=[(src.name, 4)])
    server_data.sync_to_hpc(SimpleNamespace(put_file=put), scan, "/placeholder/result")
    conn.end_run(run)
    timeline = _timeline(conn, "boundrun")
    assert timeline["run_id"] == "boundrun"
    assert {ev["run_id"] for ev in timeline["events"]} == {"boundrun"}
    assert [(s["stage"], s["duration_seconds"]) for s in timeline["spans"]] == [
        ("model_call", 7), ("data_staging", 12)]
    assert run_monitor.current_monitor() is None


@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
@pytest.mark.parametrize("entrypoint", ["complete", "scientist"])
def test_real_lab_llm_is_measured_once_with_transport_outcome(connection, clock, monkeypatch, outcome, entrypoint):
    run, _ = _watch(connection, "modelrun")

    def complete(*a, **kw):
        clock[0] += 9
        if outcome != "success":
            raise GatewayError("private response details", stage="shared_llm_stopped" if outcome == "cancelled" else "llm")
        return ("answer", {}) if entrypoint == "complete" else {"content": "answer"}

    monkeypatch.setattr(vllm_client, "complete_ex" if entrypoint == "complete" else "chat_tools", complete)
    llm = gw._lab_llm(connection)
    complete_fn, scientist = gw._watch_llm(run, llm.complete_fn, llm.scientist_chat)
    assert complete_fn is llm.complete_fn and scientist is llm.scientist_chat
    def call():
        messages = [{"role": "user", "content": "private prompt"}]
        return complete_fn(messages, role="classify") if entrypoint == "complete" else scientist(messages, [])

    if outcome == "success":
        assert call() == ("answer" if entrypoint == "complete" else {"content": "answer"})
    else:
        with pytest.raises(GatewayError):
            call()
    spans = _timeline(connection, "modelrun")["spans"]
    assert len(spans) == 1
    assert (spans[0]["stage"], spans[0]["duration_seconds"], spans[0]["outcome"]) == ("model_call", 9, outcome)
    assert "private" not in gw._timing_path(connection.workspace, "modelrun").read_text()
    connection.end_run(run)


def test_real_literature_review_search_packaging_and_late_save_are_distinct(connection, clock, monkeypatch):
    conn = connection
    model = _Model(queries='["DDX41 retina"]')
    pushed = []

    def complete(port, name, messages, **kwargs):
        clock[0] += 5
        return model(messages), {}

    monkeypatch.setattr(vllm_client, "complete_ex", complete)
    monkeypatch.setattr(gw, "_literature_corpus_available", lambda conn: False)

    def publish(payload):
        pushed.append(payload)
        if payload.get("type") == "plan_prompt":
            # The plan worker really waits on its event; the scripted user replies through the
            # actual decision endpoint after two minutes, with the same correlated run id.
            clock[0] += 120
            req = gw.PlanReviewRequest(connection_id=conn.id, run_id=payload["run_id"], action="approve")
            reply = asyncio.run(gw.lab_plan_review(req, Request({"type": "http", "headers": []})))
            assert reply.status_code == 200

    monkeypatch.setattr(conn, "_publish", publish)

    def search(query, limit, **kwargs):
        clock[0] += 30
        return {"status": "ok", "results": [{"title": "DDX41", "pmid": "42", "abstract": "Public abstract",
                                               "citation": "A paper", "year": "2025"}]}

    monkeypatch.setattr(gw._tools_api, "search_literature", search)
    from aiscientist.agents import literature_answer
    original_csv = literature_answer.papers_csv

    def papers_csv(*args, **kwargs):
        clock[0] += 4
        return original_csv(*args, **kwargs)

    monkeypatch.setattr(literature_answer, "papers_csv", papers_csv)
    req = gw.LabRequest(connection_id=conn.id, conversation_id="literature-chat",
                        question="What is known about DDX41 in the retina?")
    asyncio.run(gw._dispatch_lab(conn, req))
    assert not any(p.get("type") == "chat_error" for p in pushed)
    run_id = next(p["run_id"] for p in pushed if p.get("type") == "artifacts")
    before = _timeline(conn, run_id)
    review = [s for s in before["spans"] if s["stage"] == "user_review"]
    assert [(s["duration_seconds"], s["category"], s["metadata"]["action"]) for s in review] == [
        (120, "user_wait", "approve")]
    model_spans = [s for s in before["spans"] if s["stage"] == "model_call"]
    assert len(model_spans) >= 3 and {s["duration_seconds"] for s in model_spans} == {5}
    search_spans = [s for s in before["spans"] if s["stage"] == "tool_call"]
    assert len(search_spans) == 1 and search_spans[0]["duration_seconds"] == 30
    packaging = [s for s in before["spans"] if s["stage"] == "result_packaging"]
    assert any(s["duration_seconds"] == 4 for s in packaging)
    ready = next(ev for ev in before["events"] if ev["event"] == "result_ready")
    assert all(s["ended"] <= ready["timestamp"] for s in packaging)

    # Saving after a finished run is a new request. Another chat's monitor must not receive it.
    other, _ = _watch(conn, "otherchat")
    clock[0] += 804
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw, "_optional_user", lambda request: SimpleNamespace(username=conn.owner, id=3))
    monkeypatch.setattr(gw, "_active_conn_for_user", lambda user: None)
    monkeypatch.setattr(gw.auth_routes, "dataset_path_recorded", lambda *a: True)
    original_move = gw.shutil.move

    def move(*args, **kwargs):
        clock[0] += 6
        return original_move(*args, **kwargs)

    monkeypatch.setattr(gw.shutil, "move", move)
    response = asyncio.run(gw.save_result_dataset(run_id, Request({"type": "http", "headers": []}), "literature"))
    assert response.status_code == 200, response.body
    saved = _timeline(conn, run_id)
    waits = [s for s in saved["spans"] if s["stage"] == "save_decision_wait"]
    assert [(s["duration_seconds"], s["category"]) for s in waits] == [(804, "user_wait")]
    saves = [s for s in saved["spans"] if s["stage"] == "result_save"]
    assert [(s["duration_seconds"], s["outcome"]) for s in saves] == [(6, "success")]
    assert [s for s in saved["spans"] if s["stage"] == "model_call"] == model_spans
    assert not _timeline(conn, "otherchat")["spans"]
    assert run_monitor.current_monitor() is other.monitor
    conn.end_run(other)


@pytest.mark.parametrize("action", ["approve", "revise", "cancel"])
def test_saved_plan_decision_restores_review_timing_after_scope_ended(lab_gateway, clock, action):
    env = lab_gateway
    first = env.connection()
    run_id, art, _, _ = _show(first)
    path = art / "process" / "timing.jsonl"
    before = run_monitor.read_timing(path)
    reviews = [s for s in before["spans"] if s["stage"] == "user_review"]
    assert len(reviews) == 1 and reviews[0]["partial"]
    assert not first.live_runs
    gw.CONNECTIONS.pop(first.id)
    restored = env.connection()
    clock[0] += 600
    _reply(restored, action, run_id=run_id, feedback="Set min_genes to 300" if action == "revise" else "")
    after = run_monitor.read_timing(path)
    closed = next(s for s in after["spans"] if s["span_id"] == reviews[0]["span_id"])
    assert closed["duration_seconds"] == 600 and closed["category"] == "user_wait"
    assert closed["outcome"] == ("cancelled" if action == "cancel" else "success")
    assert closed["metadata"]["action"] == action
    assert closed["metadata"]["duration_basis"] == "wall_clock"
    assert not closed["partial"]
    assert any(e["event"] == "plan_decision" and e["metadata"]["action"] == action for e in after["events"])


@pytest.mark.parametrize("failed", [False, True])
def test_actual_background_result_save_targets_original_run_after_end(connection, clock, monkeypatch, tmp_path, failed):
    conn = connection
    old, _ = _watch(conn, "oldrun")
    old.monitor.mark_result_ready()
    conn.end_run(old)
    other, _ = _watch(conn, "newrun")
    run_id = "oldrun"
    name = f"sample.processed.20261007-2215.{run_id}.h5ad"
    pointer = gw._result_pointer_path(conn.owner, run_id, "dataset")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"name": name, "path": "/placeholder/result.h5ad", "location": "hpc3", "size_bytes": 4}))
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "managed"))
    monkeypatch.setattr(gw, "_is_run_result_path", lambda *args: True)
    seen = []

    def stage_out(src, dest, **kwargs):
        seen.append(run_monitor.current_monitor().run_id)
        clock[0] += 8
        if failed:
            raise OSError("offline transfer failure")
        Path(dest).write_bytes(b"data")
        return 4

    monkeypatch.setattr(gw, "_compute", lambda conn: SimpleNamespace(stage_out=stage_out))
    conn.executor = SimpleNamespace(remote_size=lambda path: 0)
    conn.session = SimpleNamespace(start_transfer=lambda *a, **kw: "transfer", progress=lambda *a, **kw: None,
                                   finish_transfer=lambda *a, **kw: None)
    threads = []
    original_thread = threading.Thread

    def thread(*args, **kwargs):
        worker = original_thread(*args, **kwargs)
        threads.append(worker)
        return worker

    monkeypatch.setattr(gw.threading, "Thread", thread)
    status, body = gw._save_run_result(conn.owner, None, run_id, "dataset", conn)
    assert status == 202 and body["saving"]
    for worker in threads:
        worker.join(5)
        assert not worker.is_alive()
    assert seen == ["oldrun"]
    spans = _timeline(conn, "oldrun")["spans"]
    background = [s for s in spans if s["stage"] == "result_save" and s["metadata"]["source"] == "background"]
    assert len(background) == 1 and background[0]["duration_seconds"] == 8
    assert background[0]["outcome"] == ("failed" if failed else "success")
    assert any(s["outcome"] == "queued" for s in spans)
    assert not _timeline(conn, "newrun")["spans"]
    assert run_monitor.current_monitor() is other.monitor
    conn.end_run(other)


@pytest.mark.parametrize("reason,expected", [("stop", "cancelled"), ("abandon", "unknown"),
                                            ("shared_stop", "cancelled"), ("failure", "failed")])
def test_quickchat_stream_close_and_shared_stop_keep_true_outcome(connection, clock, monkeypatch, reason, expected):
    run, _ = _watch(connection, "streamrun")
    monkeypatch.setattr(gw, "_record_chat_usage", lambda *a, **kw: None)

    def transport(*args, **kwargs):
        clock[0] += 3
        yield "content", "A token"
        clock[0] += 2
        if reason in {"shared_stop", "failure"}:
            raise GatewayError("private transport detail", stage="shared_llm_stopped" if reason == "shared_stop" else "llm")
        yield "content", "A second token"

    monkeypatch.setattr(vllm_client, "chat_tools_stream", transport)
    stream = gw._quickchat_stream_fn(connection, "offline", "http://127.0.0.1:1234/v1", None)([], [])
    assert next(stream) == ("content", "A token")
    if reason in {"stop", "abandon"}:
        if reason == "stop":
            run.chat_stop.set()
        stream.close()
    else:
        with pytest.raises(GatewayError):
            next(stream)
    spans = _timeline(connection, "streamrun")["spans"]
    assert len(spans) == 1 and spans[0]["stage"] == "model_call"
    assert spans[0]["outcome"] == expected and not spans[0]["partial"]
    assert spans[0]["duration_seconds"] == (3 if reason in {"stop", "abandon"} else 5)
    assert "private" not in gw._timing_path(connection.workspace, "streamrun").read_text()
    connection.end_run(run)


def _seed_recursive_result(conn, run_id):
    name = f"sample.processed.20261007-2215.{run_id}.h5ad"
    result = conn.workspace / run_id / "result" / name
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_bytes(b"offline data")
    rec = {"name": name, "path": str(result), "location": "gateway", "size_bytes": 12,
           "what": "dataset:sample"}
    pointer = gw._result_pointer_path(conn.owner, run_id, "dataset:sample")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps(rec))
    gw._result_pointer_path(conn.owner, run_id).write_text(json.dumps({**rec, "datasets": [rec]}))
    return result


@pytest.mark.parametrize("timing_unavailable", [False, True])
def test_recursive_dataset_save_is_one_span_and_telemetry_failure_does_not_block_move(
        connection, monkeypatch, timing_unavailable):
    run, _ = _watch(connection, "recursiverun")
    source = _seed_recursive_result(connection, "recursiverun")
    if timing_unavailable:
        def unavailable(*args, **kwargs):
            raise OSError("timing storage unavailable")
        monkeypatch.setattr(run_monitor, "reopen_timing", unavailable)
    status, body = gw._save_run_result(connection.owner, None, "recursiverun", "dataset", None)
    assert status == 200 and Path(body["path"]).read_bytes() == b"offline data"
    assert not source.exists()
    spans = _timeline(connection, "recursiverun")["spans"]
    assert len(spans) == (0 if timing_unavailable else 1)
    if spans:
        assert spans[0]["stage"] == "result_save" and spans[0]["outcome"] == "success"
    assert gw._SAVE_TIMING.get() is None
    connection.end_run(run)


def test_automatic_save_records_action_without_manual_wait(connection, clock, monkeypatch):
    run, _ = _watch(connection, "automaticrun")
    source = _seed_recursive_result(connection, "automaticrun")
    run.monitor.mark_result_ready()
    clock[0] += 100
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    connection.app_user_id = 3
    monkeypatch.setattr(gw.auth_routes, "dataset_path_recorded", lambda *args: True)
    asyncio.run(gw._autosave_results(connection, "automaticrun", lambda *a, **kw: None))
    timeline = _timeline(connection, "automaticrun")
    assert not source.exists()
    assert not any(s["stage"] == "save_decision_wait" for s in timeline["spans"])
    assert [s["stage"] for s in timeline["spans"]] == ["result_save"]
    assert any(e["event"] == "save_requested" and e["metadata"]["action"] == "automatic"
               for e in timeline["events"])
    connection.end_run(run)
