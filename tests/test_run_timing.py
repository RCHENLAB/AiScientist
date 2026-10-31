"""Durable RunMonitor timings: overlap, interruptions, owner context and later decisions."""
from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from aiscientist.gateway import run_timing
from aiscientist.gateway.run_monitor import (
    RunMonitor, bind_monitor, current_monitor, read_timing, reopen_timing, timed_stage,
)


class Clock:
    wall = 1000.
    mono = 100.

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(run_timing.time, "time", lambda: clock.wall)
    monkeypatch.setattr(run_timing.time, "monotonic", lambda: clock.mono)
    return clock


def monitor(tmp_path, name="run-a"):
    m = RunMonitor(run_id=name, conversation_id="12")
    path = tmp_path / name / "timing.jsonl"
    m.bind_timing(path)
    return m, path


def test_plan_generation_review_model_and_tools_follow_real_monitor_events(tmp_path, clock):
    m, path = monitor(tmp_path)
    m.observe({"type": "planning"})
    m.llm_begin("plan")
    clock.advance(7)
    m.llm_end()
    m.observe_push({"type": "plan_prompt", "question": "private payload"})
    clock.advance(120)
    # A generic endpoint/UI response is not approval.
    m.observe_push({"type": "plan_done"})
    assert next(s for s in read_timing(path)["spans"] if s["stage"] == "user_review")["partial"]
    m.decision("approve")
    m.observe({"type": "plan_approved"})
    m.observe({"type": "tool_start", "tool": "run_cellqc", "args": {"password": "private payload"}})
    clock.advance(3)
    m.observe({"type": "tool_error", "tool": "run_cellqc"})
    m.end("failed")
    data = read_timing(path)
    spans = {s["stage"]: s for s in data["spans"]}
    assert spans["plan_generation"]["duration_seconds"] == 7
    assert spans["model_call"]["duration_seconds"] == 7
    assert spans["user_review"]["category"] == "user_wait"
    assert spans["user_review"]["duration_seconds"] == 120
    assert spans["user_review"]["metadata"]["action"] == "approve"
    assert spans["tool_call"]["duration_seconds"] == 3
    assert spans["tool_call"]["outcome"] == "failed"
    assert data["run_id"] == "run-a" and data["conversation_id"] == "12"
    assert not data["partial"] and data["durations_overlap"]
    assert "private payload" not in path.read_text()


def test_clock_adjustments_do_not_change_measured_duration_and_spans_overlap(tmp_path, clock):
    m, path = monitor(tmp_path)
    outer = m.begin_span("data_transfer")
    clock.advance(2)
    inner = m.begin_span("model_call")
    clock.advance(5)
    clock.wall -= 100
    m.end_span(outer)
    clock.advance(3)
    m.end_span(inner)
    spans = read_timing(path)["spans"]
    assert [s["duration_seconds"] for s in spans] == [7, 8]
    assert spans[0]["started_at"].endswith("Z")
    assert spans[0]["metadata"]["duration_basis"] == "monotonic"


def test_saved_plan_review_survives_end_restart_and_wait_is_wall_clock_labelled(tmp_path, clock):
    m, path = monitor(tmp_path)
    m.observe_push({"type": "plan_prompt", "saved": True})
    clock.advance(2)
    m.observe_push({"type": "chat_stopped", "awaiting_plan": True})
    assert m.outcome == "ended"
    assert read_timing(path)["spans"][0]["partial"]
    clock.advance(600)
    restored = reopen_timing(path, "run-a")
    restored.decision("revise")
    span = read_timing(path)["spans"][0]
    assert span["duration_seconds"] == 602 and not span["partial"]
    assert span["metadata"]["source"] == "restored_wall_clock"
    assert span["metadata"]["duration_basis"] == "wall_clock"
    assert span["metadata"]["action"] == "revise"


def test_open_processing_survives_crash_and_torn_line_does_not_destroy_later_save(tmp_path, clock):
    m, path = monitor(tmp_path)
    m.begin_span("model_call", role="literature")
    clock.advance(8)
    with path.open("a") as out:
        out.write('{"event":"span_end"')
    partial = read_timing(path)["spans"][0]
    assert partial["ended_at"] is None and partial["duration_seconds"] is None
    assert partial["outcome"] == "unknown" and partial["partial"]
    restored = reopen_timing(path, "run-a")
    with restored.span("result_save"):
        clock.advance(3)
    data = read_timing(path)
    assert data["ignored_lines"] == 1
    assert data["spans"][0]["partial"] and data["spans"][1]["duration_seconds"] == 3


@pytest.mark.parametrize("exc,outcome", [(RuntimeError("private"), "failed"), (asyncio.CancelledError(), "cancelled")])
def test_exception_and_cancelled_partial_spans_are_persisted(tmp_path, clock, exc, outcome):
    m, path = monitor(tmp_path)
    with bind_monitor(m):
        with pytest.raises(type(exc)):
            with timed_stage("data_transfer"):
                clock.advance(63)
                raise exc
    span = read_timing(path)["spans"][0]
    assert span["duration_seconds"] == 63 and span["outcome"] == outcome
    assert "private" not in path.read_text()


def test_context_isolated_for_concurrent_async_runs_to_threads_and_explicit_raw_thread(tmp_path):
    a, pa = monitor(tmp_path, "alice")
    b, pb = monitor(tmp_path, "bob")
    barrier = threading.Barrier(2)

    def operation():
        with timed_stage("model_call"):
            barrier.wait(timeout=5)
            return current_monitor().run_id

    async def run(m):
        with bind_monitor(m):
            return await asyncio.to_thread(operation)

    async def both():
        return await asyncio.gather(run(a), run(b))

    assert asyncio.run(both()) == ["alice", "bob"]
    assert current_monitor() is None
    with bind_monitor(a):
        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(ctx.run, lambda: current_monitor().run_id).result() == "alice"
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(current_monitor).result() is None
    assert read_timing(pa)["run_id"] == "alice" and len(read_timing(pa)["spans"]) == 1
    assert read_timing(pb)["run_id"] == "bob" and len(read_timing(pb)["spans"]) == 1


def test_result_ready_to_save_delay_is_user_wait_and_actual_save_is_separate(tmp_path, clock):
    m, path = monitor(tmp_path)
    with m.span("result_packaging"):
        clock.advance(4)
    m.mark_result_ready()
    m.end("finished")
    clock.advance(804)
    later = reopen_timing(path, "run-a")
    later.save_requested()
    with later.span("result_save"):
        clock.advance(2)
    spans = {s["stage"]: s for s in read_timing(path)["spans"]}
    assert spans["save_decision_wait"]["duration_seconds"] == 804
    assert spans["save_decision_wait"]["category"] == "user_wait"
    assert spans["result_save"]["duration_seconds"] == 2
    assert spans["result_packaging"]["duration_seconds"] == 4


def test_save_without_result_ready_does_not_infer_delay_from_created_at(tmp_path):
    m, path = monitor(tmp_path)
    m.save_requested()
    assert read_timing(path)["spans"] == []


def test_recorded_accounting_and_unknown_are_not_poll_durations(tmp_path):
    m, path = monitor(tmp_path)
    m.record_span("hpc_queue", started_at=1000., ended_at=1010., source="sacct", job_id="12", outcome="success")
    m.record_span("hpc_execution", started_at=None, ended_at=None, source="unavailable", job_id="12")
    queue, execution = read_timing(path)["spans"]
    assert queue["duration_seconds"] == 10 and queue["metadata"]["source"] == "sacct"
    assert execution["duration_seconds"] is None and execution["outcome"] == "unknown"


def test_late_binding_keeps_early_routing_and_only_allowlisted_metadata(tmp_path, clock, caplog, monkeypatch):
    monkeypatch.setattr(run_timing._LOG, "propagate", True)
    m = RunMonitor(run_id="run-a")
    m.llm_begin("classify")
    clock.advance(5)
    m.llm_end("failed")
    path = tmp_path / "timing.jsonl"
    with caplog.at_level("INFO", logger=run_timing.__name__):
        m.bind_timing(path)
        with m.span("data_transfer", source="sftp", password="private-password", command="private-command"):
            pass
    assert read_timing(path)["spans"][0]["duration_seconds"] == 5
    assert all(json.loads(line)["run_id"] == "run-a" for line in path.read_text().splitlines())
    assert "private" not in path.read_text() and "private" not in caplog.text
    assert "run_id=run-a" in caplog.text and "stage=data_transfer" in caplog.text


def test_storage_failure_cannot_change_application_outcome(tmp_path):
    path = tmp_path / "file"
    path.write_text("not a directory")
    m = RunMonitor(run_id="run-a")
    m.bind_timing(path / "timing.jsonl")
    with bind_monitor(m), timed_stage("model_call"):
        result = 42
    assert result == 42
    m.end("finished")


def test_unbound_buffer_is_bounded_and_reports_missing_events(tmp_path, monkeypatch):
    monkeypatch.setattr(run_timing, "_BUFFER_LIMIT", 2)
    m = RunMonitor(run_id="run-a")
    for _ in range(5):
        m.timing_event("request")
    path = tmp_path / "timing.jsonl"
    m.bind_timing(path)
    event = next(e for e in read_timing(path)["events"] if e["event"] == "buffer_truncated")
    assert event["dropped_events"] == 3


def test_background_processing_ends_on_actual_return_after_live_run_ends(tmp_path, clock):
    m, path = monitor(tmp_path)
    token = m.begin_span("corpus_search")
    m.llm_begin("literature")
    clock.advance(2)
    m.end("finished")
    assert all(s["partial"] for s in read_timing(path)["spans"])
    clock.advance(4)
    m.end_span(token, "failed")
    m.llm_end()
    spans = read_timing(path)["spans"]
    assert [s["duration_seconds"] for s in spans] == [6, 6]
    assert [s["outcome"] for s in spans] == ["failed", "success"]


@pytest.mark.parametrize("outcome,expected", [("approved", "success"), ("timeout", "timeout"),
                                             ("declined", "declined"), ("stopped", "cancelled")])
def test_confirmation_wait_uses_actual_completion_outcome(tmp_path, clock, outcome, expected):
    m, path = monitor(tmp_path)
    m.observe_push({"type": "confirm_request"})
    clock.advance(18)
    m.observe_push({"type": "confirm_done", "outcome": outcome})
    span = read_timing(path)["spans"][0]
    assert span["category"] == "user_wait" and span["duration_seconds"] == 18
    assert span["outcome"] == expected and span["metadata"]["action"] == outcome


def test_corrupt_timestamp_does_not_prevent_partial_journal_download(tmp_path):
    path = tmp_path / "timing.jsonl"
    path.write_text(json.dumps({"event": "span_start", "span_id": "a", "stage": "model_call",
                                "timestamp": 1e300}) + "\n")
    span = read_timing(path)["spans"][0]
    assert span["started_at"] is None and span["partial"]


def test_early_request_is_durable_then_links_to_existing_run_without_losing_history(tmp_path, clock):
    initial = tmp_path / "alice" / "request1" / "artifacts" / "process" / "timing.jsonl"
    resumed = tmp_path / "alice" / "existing" / "artifacts" / "process" / "timing.jsonl"
    previous = RunMonitor(run_id="existing")
    previous.bind_timing(resumed)
    with previous.span("result_packaging"):
        clock.advance(1)
    m = RunMonitor(run_id="request1", conversation_id="12")
    m.bind_timing(initial)
    m.llm_begin("classify")
    clock.advance(12)
    assert read_timing(initial)["spans"][0]["partial"]  # process loss here still leaves a start
    m.run_id = "existing"
    m.bind_timing(resumed)
    clock.advance(2)
    m.llm_end()
    m.bind_timing(resumed)  # idempotent, no duplicated routing
    data = read_timing(resumed)
    assert [s["stage"] for s in data["spans"]] == ["result_packaging", "model_call"]
    assert data["spans"][1]["duration_seconds"] == 14
    assert data["spans"][1]["request_id"] == "request1"
    assert data["spans"][1]["run_id"] == data["run_id"] == "existing"
    assert next(e for e in read_timing(initial)["events"] if e["event"] == "run_link")["target_run_id"] == "existing"
    assert initial.is_file()  # old partial evidence is never erased


def test_timing_relink_cannot_copy_between_owners(tmp_path):
    old = tmp_path / "alice" / "r1" / "artifacts" / "process" / "timing.jsonl"
    other = tmp_path / "bob" / "r2" / "artifacts" / "process" / "timing.jsonl"
    m = RunMonitor(run_id="r1")
    m.bind_timing(old)
    with m.span("model_call"):
        pass
    m.run_id = "r2"
    m.bind_timing(other)
    assert not other.exists()


def test_corpus_plan_wait_is_recorded_without_changing_its_live_graph_stage(tmp_path, clock):
    m, path = monitor(tmp_path)
    m.kind = "corpus"
    m.enter("corpus")
    m.observe_push({"type": "plan_prompt"})
    clock.advance(9)
    m.decision("approve")
    assert m.stage == "corpus"
    assert read_timing(path)["spans"][0]["duration_seconds"] == 9


def test_background_tool_result_closes_timing_after_live_run_has_ended(tmp_path, clock):
    m, path = monitor(tmp_path)
    m.observe({"type": "tool_start", "tool": "corpus_search"})
    clock.advance(2)
    m.end("finished")
    clock.advance(5)
    m.observe({"type": "tool_error", "tool": "corpus_search"})
    span = read_timing(path)["spans"][0]
    assert span["duration_seconds"] == 7 and span["outcome"] == "failed" and not span["partial"]
    assert m.stage == "done" and m.outcome == "finished"



@pytest.mark.parametrize("outcome", ["failed", "cancelled", "timeout"])
def test_tool_result_preserves_structured_failure_outcome(tmp_path, clock, outcome):
    m, path = monitor(tmp_path)
    m.observe({"type": "tool_start", "tool": "run_cellqc"})
    clock.advance(4)
    m.observe({"type": "tool_result", "tool": "run_cellqc", "timing_outcome": outcome,
               "summary": "This untrusted display text says success but is not the status."})
    span = read_timing(path)["spans"][0]
    assert span["outcome"] == outcome and span["duration_seconds"] == 4



def test_enclosing_plan_model_and_tool_waits_are_observations_not_exclusive_compute(tmp_path, clock):
    m, path = monitor(tmp_path)
    m.observe({"type": "planning"})
    m.llm_begin("plan")
    m.observe({"type": "tool_start", "tool": "hpc_shell"})
    clock.advance(2)
    m.observe_push({"type": "confirm_request"})
    clock.advance(40)
    m.observe_push({"type": "confirm_done", "outcome": "approved"})
    clock.advance(3)
    m.observe({"type": "tool_result", "tool": "hpc_shell", "timing_outcome": "success"})
    m.llm_end()
    m.observe_push({"type": "plan_prompt"})
    spans = read_timing(path)["spans"]
    enclosing = [s for s in spans if s["stage"] in {"plan_generation", "model_call", "tool_call"}]
    assert len(enclosing) == 3
    assert all(s["category"] == "observation" and s["duration_seconds"] == 45 for s in enclosing)
    confirmed = next(s for s in spans if s["stage"] == "user_review" and not s["partial"])
    assert confirmed["category"] == "user_wait" and confirmed["duration_seconds"] == 40
    # The explicit compute phase remains separately identifiable.
    with m.span("result_packaging"):
        clock.advance(1)
    assert read_timing(path)["spans"][-1]["category"] == "processing"
