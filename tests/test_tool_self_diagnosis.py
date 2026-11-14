import json

def test_the_critic_sees_tool_warnings_first(tmp_path):
    from aiscientist.agents.research_harness import HarnessContext, HarnessResult
    from aiscientist.agents.research_lab import LabConfig, ResearchLab

    seen: list = []

    class _Stub:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

    lab = ResearchLab(
        HarnessContext(decisions={}, workspace=tmp_path), LabConfig(auto_select_skill=False),
        complete_fn=lambda m: (seen.append(m), json.dumps(
            {"verdict": "accept", "score": 0.9, "critique": "ok"}))[1],
        scientist=_Stub())
    events: list = []

    lab._critic("q", "QC the cells", HarnessResult(
        status="ok", stop_reason=None, final_answer="filtered",
        steps=[{"tool": "run_scanpy_qc", "ok": True,
                "result": {"status": "ok", "cells_after": 100,
                           "warnings": ["NO mitochondrial genes matched the 'MT-' name prefix"]}}],
        errors=[]), events.append)

    payload = json.loads(seen[0][1]["content"])
    keys = list(payload)
    assert "TOOL_SELF_REPORTED_PROBLEMS" in payload
    assert keys.index("TOOL_SELF_REPORTED_PROBLEMS") < keys.index("tool_results"), \
        "a warning read after a wall of successful numbers is a warning nobody acts on"
    assert payload["TOOL_SELF_REPORTED_PROBLEMS"] == [
        "run_scanpy_qc: NO mitochondrial genes matched the 'MT-' name prefix"]
    assert "unsupported" in payload["tool_warning_note"]
    assert any(e.get("type") == "tool_warnings" for e in events), "and a human must be told too"

def test_a_clean_step_adds_no_warning_block(tmp_path):
    from aiscientist.agents.research_harness import HarnessContext, HarnessResult
    from aiscientist.agents.research_lab import LabConfig, ResearchLab

    seen: list = []

    class _Stub:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

    lab = ResearchLab(
        HarnessContext(decisions={}, workspace=tmp_path), LabConfig(auto_select_skill=False),
        complete_fn=lambda m: (seen.append(m), json.dumps(
            {"verdict": "accept", "score": 0.9, "critique": "ok"}))[1],
        scientist=_Stub())

    lab._critic("q", "s", HarnessResult(
        status="ok", stop_reason=None, final_answer="done",
        steps=[{"tool": "run_scanpy_qc", "ok": True,
                "result": {"status": "ok", "warnings": []}}], errors=[]), lambda _e: None)

    assert "TOOL_SELF_REPORTED_PROBLEMS" not in json.loads(seen[0][1]["content"])
