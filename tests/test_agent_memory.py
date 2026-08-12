"""Offline tests for per-agent evolving memory (Axis C) — disk store, isolation, reflection.

No network/LLM: reflection is driven by an injected ``complete_fn``. See docs/agent_memory_design.md.
"""

from __future__ import annotations

from bioagent.agents.agent_memory import AgentMemory, slug_agent_id


def test_slug_is_filesystem_safe():
    assert slug_agent_id("QC & preprocessing specialist") == "qc_preprocessing_specialist"
    assert slug_agent_id("") == "agent"


def test_cold_start_read_is_empty(tmp_path):
    m = AgentMemory(tmp_path)
    assert m.read("QC specialist", "run scanpy qc") == ""      # no memory yet → graceful


def test_write_then_read_surfaces_relevant_episode(tmp_path):
    m = AgentMemory(tmp_path)
    m.write_episode("QC specialist", {"node": "Run scanpy QC", "action": "set mito<10%",
                                      "outcome": "revised/advanced", "note": "reviewer: too permissive"})
    block = m.read("QC specialist", "run scanpy QC filtering")
    assert "PRIVATE memory" in block
    assert "set mito<10%" in block and "Run scanpy QC" in block


def test_memory_is_isolated_per_agent(tmp_path):
    m = AgentMemory(tmp_path)
    m.write_episode("QC specialist", {"node": "QC", "action": "mito threshold work", "outcome": "accepted"})
    # a DIFFERENT agent never sees the QC agent's memory
    assert m.read("Pathway specialist", "QC mito threshold") == ""


def test_reflect_distils_episodes_into_lessons(tmp_path):
    m = AgentMemory(tmp_path)
    for i in range(3):
        m.write_episode("QC specialist", {"node": "QC", "action": f"attempt {i} mito<10%",
                                          "outcome": "revised/advanced", "note": "too permissive"})
    seen = {}

    def complete(messages):
        seen["sys"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        return "- For retinal-type data, start mito < 5%; the reviewer rejects 10% as too permissive."

    assert m.reflect("QC specialist", complete, role_hint="QC specialist") is True
    # the reflection prompt enforces methodology-only (no dataset numbers rule present)
    assert "METHODOLOGICAL" in seen["sys"] and "attempt 0 mito<10%" in seen["user"]
    # the distilled lesson is now surfaced on the next read
    block = m.read("QC specialist", "run qc mito filtering")
    assert "start mito < 5%" in block and "Lessons you've learned" in block


def test_reflect_without_episodes_is_a_noop(tmp_path):
    m = AgentMemory(tmp_path)
    assert m.reflect("QC specialist", lambda msgs: "x") is False


def test_memory_never_raises_on_bad_root(tmp_path):
    # a file where the dir should be → writes/reads degrade silently, never crash a run
    bad = tmp_path / "afile"
    bad.write_text("not a dir")
    m = AgentMemory(bad)
    m.write_episode("a", {"node": "n", "action": "x"})   # no raise
    assert m.read("a", "q") == ""
    assert m.reflect("a", lambda msgs: "x") is False
