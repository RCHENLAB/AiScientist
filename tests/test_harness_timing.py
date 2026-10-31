"""Real agent loops close tool spans from returned statuses, including cooperative cancellation."""
from __future__ import annotations

import pytest

from aiscientist.agents import quick_chat, research_harness
from aiscientist.agents.research_harness import HarnessConfig, HarnessContext, HarnessTool, ResearchHarness
from aiscientist.agents.research_lab import LabConfig, ResearchLab, Specialist
from aiscientist.gateway import run_timing
from aiscientist.gateway.run_monitor import RunMonitor


def _call(name):
    return {"id": "call-1", "type": "function", "function": {"name": name, "arguments": {}}}


def _observer(tmp_path, monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(run_timing.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(run_timing.time, "time", lambda: 1800000000 + elapsed[0])
    path = tmp_path / "timing.jsonl"
    monitor = RunMonitor(run_id="run1")
    monitor.bind_timing(path)
    events = []

    def emit(event):
        events.append(event)
        monitor.observe(event)

    return elapsed, path, emit, events


@pytest.mark.parametrize("status,outcome", [
    ("error", "failed"), ("failed", "failed"), ("timeout", "timeout"),
    ("blocked_network", "blocked"), ("dependency_missing", "failed"),
    ("cancelled", "cancelled"), ("completed_lightweight_qc", "success"),
])
def test_research_harness_returned_status_closes_real_tool_span(tmp_path, monkeypatch, status, outcome):
    elapsed, path, emit, events = _observer(tmp_path, monkeypatch)
    # The text can be misleading; instrumentation must use the structured output, not parse prose.
    monkeypatch.setattr(research_harness, "_summarize", lambda output: "completed successfully")

    def execute(args, context):
        elapsed[0] += 7
        return {"status": status, "error": "structured failure detail"}

    tool = HarnessTool("run_code", "offline tool", {"type": "object", "properties": {}}, execute)
    harness = ResearchHarness(catalog=[tool],
                              config=HarnessConfig(max_steps=1, closing_turn_on_budget=False),
                              chat_fn=lambda messages, tools: {"content": "", "tool_calls": [_call(tool.name)]})
    result = harness.run("Run the test tool", HarnessContext(), on_event=emit)
    spans = [span for span in run_timing.read_timing(path)["spans"] if span["stage"] == "tool_call"]
    assert len(spans) == 1 and spans[0]["outcome"] == outcome
    assert spans[0]["duration_seconds"] == 7 and not spans[0]["partial"]
    completions = [event for event in events if event["type"] == "tool_result"]
    assert len(completions) == 1 and completions[0]["timing_outcome"] == outcome
    assert completions[0]["summary"] == "completed successfully"
    if status == "cancelled":
        assert result.stop_reason == "cancelled" and result.steps[0]["ok"] is False
    else:
        # Keep existing harness success/failure adjudication and retry semantics unchanged.
        assert result.steps[0]["ok"] is True
        assert result.steps[0]["result"]["status"] == status


@pytest.mark.parametrize("forced", [False, True])
@pytest.mark.parametrize("status,outcome", [("error", "failed"), ("cancelled", "cancelled"),
                                            ("blocked_policy", "blocked"), ("ok", "success")])
def test_quick_chat_tool_status_closes_span_for_forced_and_requested_tools(
        tmp_path, monkeypatch, forced, status, outcome):
    elapsed, path, emit, events = _observer(tmp_path, monkeypatch)

    def execute(args, context):
        elapsed[0] += 4
        return {"status": status, "contexts": []}

    tool = HarnessTool("search_corpus" if forced else "lookup", "offline lookup",
                       {"type": "object", "properties": {}}, execute)
    turns = iter([[('content', 'No evidence found.')]] if forced else
                 [[('tool_calls', [_call(tool.name)])], [('content', 'Tool finished.')]])

    def stream(messages, schemas):
        yield from next(turns)

    result = quick_chat.run_quick_chat(stream_fn=stream, tools=[tool],
                                       question="Find literature evidence for RPE65" if forced else "Lookup an item",
                                       on_event=emit)
    assert len(result.tool_calls) == 1
    spans = [span for span in run_timing.read_timing(path)["spans"] if span["stage"] == "tool_call"]
    assert len(spans) == 1 and spans[0]["outcome"] == outcome
    assert spans[0]["duration_seconds"] == 4 and not spans[0]["partial"]
    assert next(event for event in events if event["type"] == "tool_result")["timing_outcome"] == outcome


@pytest.mark.parametrize("name,method", [("literature_search", "_run_literature_step"),
                                         ("deep_literature", "_run_deep_literature_step"),
                                         ("search_corpus", "_run_search_corpus_step")])
@pytest.mark.parametrize("status,outcome", [("error", "failed"), ("cancelled", "cancelled")])
def test_lab_direct_literature_producers_preserve_failure_outcome(
        tmp_path, monkeypatch, name, method, status, outcome):
    elapsed, path, emit, events = _observer(tmp_path, monkeypatch)

    def execute(args, context):
        elapsed[0] += 3
        return {"status": status, "error": "No result", "contexts": [], "results": []}

    tool = HarnessTool(name, "offline literature", {"type": "object", "properties": {}}, execute)
    lab = ResearchLab(HarnessContext(), LabConfig(auto_select_skill=False), complete_fn=lambda messages: "{}",
                      scientist=ResearchHarness(catalog=[tool], chat_fn=lambda messages, tools: {}))
    monkeypatch.setattr(lab, "_plan_literature_queries", lambda *args: ["retina"])
    monkeypatch.setattr(lab, "_deep_literature_question", lambda *args: "retina")
    kwargs = {"pinned": {"queries": ["retina"]}} if name == "search_corpus" else {}
    getattr(lab, method)("question", "literature step", [], tool, Specialist("reader", "read"), emit, **kwargs)
    spans = [span for span in run_timing.read_timing(path)["spans"] if span["stage"] == "tool_call"]
    assert len(spans) == 1 and spans[0]["outcome"] == outcome
    assert spans[0]["duration_seconds"] == 3 and not spans[0]["partial"]
    assert next(event for event in events if event["type"] == "tool_result")["timing_outcome"] == outcome


def test_expert_tool_relay_retains_structured_failure_outcome(tmp_path, monkeypatch):
    elapsed, path, emit, events = _observer(tmp_path, monkeypatch)

    def execute(args, context):
        elapsed[0] += 2
        return {"status": "blocked_network"}

    tool = HarnessTool("literature_search", "offline lookup", {"type": "object", "properties": {}}, execute)

    def chat(messages, tools):
        if any(message.get("role") == "tool" for message in messages):
            return {"content": "The lookup was blocked."}
        return {"content": "", "tool_calls": [_call(tool.name)]}

    lab = ResearchLab(HarnessContext(), LabConfig(auto_select_skill=False), complete_fn=lambda messages: "fallback",
                      scientist=ResearchHarness(catalog=[tool], chat_fn=chat))
    lab._expert_turns([[{"role": "system", "content": "Read literature"},
                        {"role": "user", "content": "Retina"}]], emit, names=["reader"])
    done = [event for event in events if event["type"] == "expert_tool" and event["state"] == "done"]
    assert len(done) == 1 and done[0]["timing_outcome"] == "blocked"
    spans = [span for span in run_timing.read_timing(path)["spans"] if span["stage"] == "tool_call"]
    assert len(spans) == 1 and spans[0]["outcome"] == "blocked" and not spans[0]["partial"]
