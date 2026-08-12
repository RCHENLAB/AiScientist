"""Offline tests for the DRAFT multi-agent lab kernel (src/bioagent/lab).

No GPU / no real LLM: the LLM is a scripted fake. Proves the kernel runs end-to-end, the
PI->dispatcher handoff works, each agent keeps its OWN memory, and the Lab Archive is
durable + resumable.
"""

from __future__ import annotations

import json

from bioagent.lab.archive import LabArchive
from bioagent.lab.kernel import Agent, Lab, LabConfig, Registry, Tool


def _echo_tool() -> Tool:
    return Tool("echo", "echo back args", lambda args, _ctx: {"status": "ok", "echo": args})


def _events(arch: LabArchive) -> list[dict]:
    p = arch.root / "events.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


# --------------------------------------------------------------------------- archive
def test_archive_is_durable_and_resumable(tmp_path):
    arch = LabArchive(tmp_path, "lab1")
    arch.init_manifest("why are these cells weird?")
    arch.append_memory("immunologist", "user", "look at the markers")
    arch.append_memory("immunologist", "assistant", "looks like T cells")
    arch.write_checkpoint(0, {"step": 0, "history": ["a"]})
    arch.write_checkpoint(1, {"step": 1, "history": ["a", "b"]})

    # per-agent memory rehydrates as chat messages
    mem = arch.read_memory("immunologist")
    assert [m["role"] for m in mem] == ["user", "assistant"]

    # a FRESH archive object on the same root resumes from the latest checkpoint
    reopened = LabArchive(tmp_path, "lab1")
    seq, state = reopened.latest_checkpoint()
    assert seq == 1 and state["history"] == ["a", "b"]
    assert reopened.load_manifest()["question"].startswith("why are these")


# --------------------------------------------------------------------------- agent turn
def test_agent_turn_keeps_own_memory_and_calls_scoped_tool(tmp_path):
    calls = {"n": 0}

    def fake(messages):
        calls["n"] += 1
        return '{"tool":"echo","args":{"q":1}}' if calls["n"] == 1 else '{"final":"T cells, high confidence"}'

    tools = Registry()
    tools.register(_echo_tool())
    lab = Lab(fake, LabArchive(tmp_path, "lab2"), tools=tools)
    agent = Agent("immuno", "Immunologist", "T-cell biology", "name the cluster", tools=("echo",))

    out = lab.run_agent_turn(agent, "Name cluster 3 from its markers.")
    assert out == "T cells, high confidence"

    mem = lab.archive.read_memory("immuno")
    assert any("TOOL_RESULT echo" in m["content"] for m in mem)   # the tool observation was recorded
    assert any(e["type"] == "tool_call" and e["by"] == "immuno" for e in _events(lab.archive))


def test_agent_cannot_call_unscoped_tool(tmp_path):
    def fake(_messages):
        return '{"tool":"secret","args":{}}'   # not in the agent's scoped tools

    tools = Registry()
    tools.register(_echo_tool())
    lab = Lab(fake, LabArchive(tmp_path, "lab3"), tools=tools, config=LabConfig(max_tool_turns=1))
    agent = Agent("a", "Analyst", "stats", "help", tools=("echo",))
    lab.run_agent_turn(agent, "do it")
    assert any("TOOL_DENIED secret" in m["content"] for m in lab.archive.read_memory("a"))


# --------------------------------------------------------------------------- full run
def test_full_run_pi_routes_then_finishes(tmp_path):
    route = {"n": 0}

    def fake(messages):
        sys = messages[0]["content"]
        if "Plan 2-6 ordered" in sys:                 # PI_PLAN
            return '["Run echo", "Summarize"]'
        if "choose the NEXT action" in sys:           # PI_ROUTE (handoff)
            route["n"] += 1
            return '{"action":"tool","tool":"echo","args":{"x":1}}' if route["n"] == 1 else '{"action":"finish","answer":""}'
        if "writing the final answer" in sys:         # PI_SYNTH
            return "Final synthesized report."
        return "ok."

    tools = Registry()
    tools.register(_echo_tool())
    arch = LabArchive(tmp_path, "lab4")
    lab = Lab(fake, arch, tools=tools)

    res = lab.run("Annotate this dataset.")
    assert res.status == "done"
    assert res.agenda == ["Run echo", "Summarize"]
    assert res.steps_taken == 1                         # one tool action, then finish
    assert res.final_answer == "Final synthesized report."   # empty answer -> PI synthesized

    # durable trail: a checkpoint exists and the tool call was logged
    assert arch.latest_checkpoint() is not None
    assert any(e["type"] == "tool_call" and e["tool"] == "echo" for e in _events(arch))
    assert arch.load_manifest()["status"] == "done"


def test_run_resumes_from_existing_checkpoint(tmp_path):
    """A second run() on the same archive continues past the saved step (no re-plan)."""
    arch = LabArchive(tmp_path, "lab5")
    arch.init_manifest("Q")
    arch.write_agenda(["only step"])
    arch.write_checkpoint(0, {"question": "Q", "agenda": ["only step"], "history": [], "final": None})

    def fake(messages):
        if "choose the NEXT action" in messages[0]["content"]:
            return '{"action":"finish","answer":"resumed-and-done"}'
        return "ok."

    res = Lab(fake, arch).run("Q")
    assert res.final_answer == "resumed-and-done"
    assert any(e["type"] == "resumed" for e in _events(arch))
