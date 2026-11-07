"""A step is explored only when a skill it uses declares ``exploration`` (RCHENLAB/AiScientist#102).

Run 3974f4bd26b1 (2026-11-04) planned one `run_cellqc` step; with exploration on for every run, the
PI then proposed a SoupX/DecontX comparison and a doublet-caller audit, and the run waited about five
minutes for the user to decline both. Most skills need no exploration, CellQC's and the RAG skills
among them, so the skill decides: ``exploration: "true"`` in its SKILL.md ``metadata``. Scripted
models throughout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiscientist.agents import research_lab as rl
from aiscientist.agents import skills as skills_mod
from aiscientist.agents.plan_doc import PlanDoc, PlanStep
from aiscientist.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
from aiscientist.agents.research_lab import LabConfig, ResearchLab
from aiscientist.agents.skills import Skill, declares, refresh_skills

_EXPLORE_MARK = "opened a research path the CURRENT PLAN DOES NOT COVER"
_CELLQC = "qc-snrna-with-cellqc-standalone"
_FIXTURES = Path(__file__).parent / "fixtures" / "skills" / "single-cell" / _CELLQC


def _skill_md(name: str, *, exploration: "str | None") -> str:
    extra = f'  exploration: "{exploration}"\n' if exploration is not None else ""
    return (f"---\nname: {name}\ndescription: Test skill {name}.\nlicense: MIT\nmetadata:\n"
            f'  category: single-cell\n  version: "1.0.0"\n  status: core\n{extra}---\n\n'
            f"# {name}\n\nGuidance.\n")


@pytest.fixture
def library(tmp_path, monkeypatch, skill_release):
    """``library(cellqc_explores=False)``: a release with a skill that declares exploration, one that
    does not, and the CellQC skill run_cellqc's TOOL.md names (declaring it or not)."""
    def install(*, cellqc_explores: bool = False) -> dict:
        root = tmp_path / "skills"
        for name, flag in (("explore-demo", "true"), ("quiet-demo", None)):
            folder = root / "single-cell" / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "SKILL.md").write_text(_skill_md(name, exploration=flag), encoding="utf-8")
        cellqc = root / "single-cell" / _CELLQC
        (cellqc / "assets").mkdir(parents=True, exist_ok=True)
        md = (_FIXTURES / "SKILL.md").read_text(encoding="utf-8")
        if cellqc_explores:
            md = md.replace("  status: core\n", '  status: core\n  exploration: "true"\n', 1)
        (cellqc / "SKILL.md").write_text(md, encoding="utf-8")
        for f in (_FIXTURES / "assets").iterdir():
            (cellqc / "assets" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.delenv("AISCIENTIST_INDUCED_SKILLS_DIR", raising=False)
        skill_release(root)
        lib: dict = {}
        monkeypatch.setattr(skills_mod, "SKILLS", lib)
        monkeypatch.setattr(rl, "ATOMIC_SKILLS", lib)        # research_lab binds the dict at import
        monkeypatch.setattr(skills_mod, "ALL_SKILLS", {})
        monkeypatch.setattr(skills_mod, "_SIGNATURE", ())
        monkeypatch.setattr(skills_mod, "_REGISTERED", {})
        refresh_skills()
        assert {"explore-demo", "quiet-demo", _CELLQC} <= set(lib)
        return lib
    return install


def _ctx() -> HarnessContext:
    return HarnessContext(decisions={}, tunnel_port=1, model="m")


def _scientist():
    """Call run_qc once, then finish."""
    def chat(messages, _tools):
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"id": "f1", "type": "function", "function": {
                "name": "finish", "arguments": json.dumps({"answer": "done this step"})}}]}
        return {"content": "", "tool_calls": [{"id": "t1", "type": "function", "function": {
            "name": "run_qc", "arguments": "{}"}}]}
    return chat


def _router(agenda, seen):
    def complete(messages):
        sys = messages[0]["content"]
        seen.append(sys)
        if _EXPLORE_MARK in sys:
            return json.dumps({"surprise": "nothing", "hypotheses": [], "new_steps": []})
        if "reviewing a step that JUST completed" in sys:
            return json.dumps({"contribution": "new", "prune": []})
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "grounded"})
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(agenda)
        return "FINAL REPORT: synthesized from accepted steps."
    return complete


def _explore_calls(step: str) -> int:
    seen: list[str] = []
    lab = ResearchLab(_ctx(), LabConfig(hypothesis_driven=True, new_steps_need_approval=False,
                                        planner="linear"),
                      complete_fn=_router([step], seen),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    lab.run("Run the analysis.")
    return sum(_EXPLORE_MARK in s for s in seen)


@pytest.mark.parametrize("value,on", [("true", True), ("yes", True), ("1", True), ('"true"', True),
                                      ("TRUE", True), ("false", False), ("no", False), ("", False)])
def test_declares_reads_the_metadata_switch(value, on):
    assert declares(Skill(name="s", metadata={"exploration": value}), "exploration") is on


def test_a_skill_without_the_key_or_no_skill_declares_nothing():
    assert not declares(Skill(name="s", metadata={"category": "single-cell"}), "exploration")
    assert not declares(None, "exploration")


def test_the_declaration_survives_the_registry_loader(library):
    lib = library()
    assert declares(lib["explore-demo"], "exploration")
    assert not declares(lib["quiet-demo"], "exploration")


def test_a_step_using_a_skill_that_declares_exploration_is_explored(library):
    library()
    assert _explore_calls("**Score** — Apply skill `explore-demo` with `run_qc` to the cells.") == 1


def test_a_step_whose_skill_declares_nothing_is_not_explored(library):
    library()
    assert _explore_calls("**Score** — Apply skill `quiet-demo` with `run_qc` to the cells.") == 0


def test_a_step_with_no_skill_is_not_explored(library):
    library()
    assert _explore_calls("**QC** — Run `run_qc` on the cells.") == 0


def test_the_operator_switch_still_turns_it_off(library):
    library()
    seen: list[str] = []
    step = "**Score** — Apply skill `explore-demo` with `run_qc` to the cells."
    lab = ResearchLab(_ctx(), LabConfig(hypothesis_driven=False, planner="linear"),
                      complete_fn=_router([step], seen),
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=_scientist()))
    lab.run("Run the analysis.")
    assert not any(_EXPLORE_MARK in s for s in seen)


@pytest.mark.parametrize("explores", [False, True])
def test_a_tool_step_takes_its_skill_from_the_tools_manifest(library, explores):
    """run_cellqc's TOOL.md names the CellQC skill: that skill decides, as the 3974f4bd26b1 step."""
    library(cellqc_explores=explores)
    lab = ResearchLab(_ctx(), LabConfig(hypothesis_driven=True))
    step = "**Cell QC over both libraries** — Run `run_cellqc` once over both libraries."
    assert _CELLQC in [s.name for s in lab._step_skills(step)]
    assert lab._explores_after(step) is explores


def test_the_skill_the_plan_assigned_counts(library):
    library()
    lab = ResearchLab(_ctx(), LabConfig(hypothesis_driven=True))
    step = "**Score** — Score the cells with `run_code`."
    lab._plan_doc = PlanDoc(request="r", steps=(PlanStep(id="s1", text=step, tool="run_code",
                                                         skill="explore-demo"),))
    assert lab._explores_after(step)
    assert not lab._explores_after("**Other** — A step the plan does not have.")
