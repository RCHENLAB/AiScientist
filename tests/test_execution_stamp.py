"""Every result must state HOW it was run — which mode, which team, which switches.

Of 26 production run bundles inspected on 2026-08-11, NONE recorded the execution mode. The only
way to tell a Virtual-Lab run from a single-scientist one was to infer it from the specialist NAMES
in the rounds, and that inference is one-directional: PI-invented titles prove a team, but the fixed
roster does NOT prove single, because a team run whose roster fails to parse falls back to exactly
that roster. (16 of the 26 turned out to be team runs — nobody had ever selected it; `auto` chose
it.) So the stamp is recorded machine-readably AND rendered at the top of the transcript.
"""

from __future__ import annotations

from pathlib import Path

from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.research_lab import LabConfig, ResearchLab, Specialist
from aiscientist.reporting.research_bundle import _render_transcript, write_process_artifacts


def _lab(tmp_path: Path, **cfg) -> ResearchLab:
    class _Stub:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

    return ResearchLab(HarnessContext(decisions={}, workspace=tmp_path),
                       LabConfig(auto_select_skill=False, **cfg),
                       complete_fn=lambda _m: "", scientist=_Stub())


# --- the profile ---------------------------------------------------------------


def test_resolved_mode_is_recorded_not_the_requested_one(tmp_path):
    """"auto" is what the user picks and says nothing about what ran — both are kept."""
    lab = _lab(tmp_path, mode="auto")
    lab._mode = "team"                                  # what run() resolved it to

    prof = lab.execution_profile

    assert prof["mode"] == "team"
    assert prof["mode_requested"] == "auto"


def test_a_pi_formed_team_is_recorded_and_marked_as_such(tmp_path):
    lab = _lab(tmp_path, mode="team")
    lab._mode = "team"
    lab._team = (Specialist("Retina Transcriptomics Expert", "retina"),
                 Specialist("Computational Biostatistician", "stats"))

    prof = lab.execution_profile

    assert prof["team"] == ["Retina Transcriptomics Expert", "Computational Biostatistician"]
    assert prof["team_is_pi_formed"] is True


def test_a_fallback_to_the_fixed_roster_is_not_claimed_as_a_formed_team(tmp_path):
    """The exact ambiguity that made the historical bundles unreadable: a team run whose roster
    failed to parse falls back to the fixed specialists. That must not look PI-formed."""
    lab = _lab(tmp_path, mode="team")
    lab._mode = "team"
    lab._team = lab.config.specialists                  # what _form_team falls back to

    assert lab.execution_profile["team_is_pi_formed"] is False


def test_single_mode_records_no_team(tmp_path):
    lab = _lab(tmp_path, mode="single")
    lab._mode = "single"
    prof = lab.execution_profile
    assert prof["mode"] == "single" and prof["team"] is None


def test_the_switches_that_change_behaviour_are_recorded(tmp_path):
    lab = _lab(tmp_path, planner="dag", multi_agent=True, max_concurrency=3,
               hypothesis_driven=True, max_cycles=2)
    prof = lab.execution_profile
    assert prof["planner"] == "dag"
    assert prof["multi_agent"] is True
    assert prof["max_concurrency"] == 3
    assert prof["hypothesis_driven"] is True
    assert prof["max_cycles"] == 2


def test_agent_memory_counts_as_off_without_a_directory(tmp_path):
    """The flag alone does nothing — no directory means no memory was actually read or written."""
    assert _lab(tmp_path, agent_memory=True, agent_memory_dir=None).execution_profile[
        "agent_memory"] is False
    assert _lab(tmp_path, agent_memory=True,
                agent_memory_dir=str(tmp_path)).execution_profile["agent_memory"] is True


# --- where a human actually reads it ------------------------------------------


def test_the_transcript_states_the_mode_and_the_team_up_front(tmp_path):
    md = _render_transcript({
        "question": "compare KO vs WT",
        "agenda": ["QC"],
        "rounds": [],
        "execution": {"mode": "team", "mode_requested": "auto",
                      "team": ["Retina Expert", "Biostatistician"], "team_is_pi_formed": True,
                      "planner": "dag", "max_concurrency": 1, "max_cycles": 1,
                      "multi_agent": True, "agent_memory": True},
    })

    head = md.split("## Agenda")[0]
    assert "Virtual Lab" in head, "the mode must be above the agenda, not buried"
    assert "auto" in head, "a resolved mode must say what it was resolved from"
    assert "Retina Expert" in head and "PI-formed" in head
    assert "multi_agent" in head and "agent_memory" in head


def test_a_single_scientist_run_says_so(tmp_path):
    md = _render_transcript({"question": "q", "rounds": [],
                             "execution": {"mode": "single", "mode_requested": "single",
                                           "planner": "dag", "max_concurrency": 1,
                                           "max_cycles": 1}})
    assert "Single scientist" in md


def test_a_bundle_without_the_stamp_still_renders(tmp_path):
    """Historical bundles have no execution block — they must not crash the renderer."""
    md = _render_transcript({"question": "q", "agenda": ["a"], "rounds": []})
    assert "Research transcript" in md and "Execution mode" not in md


def test_the_stamp_reaches_the_written_bundle(tmp_path):
    written = write_process_artifacts(
        {"question": "q", "agenda": ["a"], "rounds": [],
         "execution": {"mode": "team", "mode_requested": "auto", "team": ["X"],
                       "team_is_pi_formed": True, "planner": "dag", "max_concurrency": 1,
                       "max_cycles": 1}},
        tmp_path / "process")

    transcript = next(p for p in written if p.name == "transcript.md")
    assert "Virtual Lab" in transcript.read_text(encoding="utf-8")
    import json
    result = json.loads((tmp_path / "process" / "lab_result.json").read_text(encoding="utf-8"))
    assert result["execution"]["mode"] == "team"
