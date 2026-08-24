"""Mid-run durability: the run's state is persisted after EVERY round, not only at the end.

Before this, ``run_state.json`` was written once — after ``ResearchLab.run`` returned. Prod is a
single stateful replica, so ONE gateway restart hits every live run: a multi-hour analysis lost its
orchestration state entirely, and although the analysis checkpoints (``work/adata_*.h5ad``) were
still on disk, nothing could re-enter the loop to use them. The run was simply gone.

What is pinned here is the contract the recovery path depends on: after each round there is a
persisted state that ``ResumeState.from_run_state`` can load, carrying the agenda and every
accepted round so far.
"""

from __future__ import annotations

import json
from pathlib import Path

from bioagent.agents.research_harness import HarnessContext
from bioagent.agents.research_lab import LabConfig, LabResult, ResearchLab, ResumeState


def _lab(tmp_path: Path, steps: int = 3) -> tuple[ResearchLab, list]:
    """A lab whose PI plans `steps` steps and whose Critic accepts everything, with no real tools."""
    agenda = [f"Step {i + 1}: do the thing" for i in range(steps)]

    def complete_fn(messages):
        system = messages[0]["content"]
        if "Principal Investigator" in system and "final research report" in system:
            return "The report."
        if "Critic" in system:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "fine"})
        return json.dumps({"agenda": agenda})

    class _Scientist:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

        def run(self, *_a, **_k):
            from bioagent.agents.research_harness import HarnessResult
            # A real successful tool step: the Critic's deterministic floor refuses to accept a
            # step that produced nothing, so an empty result would never reach the checkpoint.
            return HarnessResult(
                status="ok", stop_reason=None, final_answer="did it",
                steps=[{"tool": "run_scanpy_qc", "args": {}, "ok": True, "summary": "qc done",
                        "result": {"status": "ok", "cells_after": 100,
                                   "figures": ["figures/violin_qc_violin.png"]}}],
                errors=[])

    ctx = HarnessContext(decisions={}, workspace=tmp_path)
    lab = ResearchLab(ctx, LabConfig(max_steps=steps, auto_select_skill=False),
                      complete_fn=complete_fn, scientist=_Scientist())
    return lab, agenda


def test_state_is_persisted_after_every_round(tmp_path):
    seen: list[LabResult] = []
    lab, agenda = _lab(tmp_path, steps=3)

    lab.run("q", checkpoint=lambda partial: seen.append(partial))

    assert len(seen) == 3, "one checkpoint per round, not one per run"
    # Each snapshot carries the plan and every round completed so far — what a resume needs.
    for i, snap in enumerate(seen, start=1):
        assert snap.agenda == agenda
        assert len(snap.rounds) == i
        assert snap.converged is False, "an in-flight run must not look completed"
        assert snap.final_answer == ""


def test_a_snapshot_reloads_into_a_resumable_state(tmp_path):
    """The recovery path: the LAST snapshot before a crash must round-trip through the same
    ResumeState machinery /api/lab/continue uses."""
    seen: list[LabResult] = []
    lab, agenda = _lab(tmp_path, steps=3)
    lab.run("q", checkpoint=lambda partial: seen.append(partial))

    crashed_after_step_2 = seen[1]                       # pretend the process died here
    state = crashed_after_step_2.to_dict()
    state["guidance"] = None

    resume = ResumeState.from_run_state(state, from_step_index=2)

    assert resume.agenda == agenda
    assert len(resume.prior_rounds) == 2, "both completed steps are reusable, not re-run"
    assert resume.from_step_index == 2


def test_a_failed_checkpoint_never_breaks_the_run(tmp_path):
    """Durability metadata is best-effort by construction: recording a run must not be able to
    kill the run it is recording."""
    lab, _ = _lab(tmp_path, steps=2)

    def exploding(_partial):
        raise OSError("disk full")

    result = lab.run("q", checkpoint=exploding)

    assert result.accepted_steps == 2
    assert result.final_answer


def test_no_checkpoint_callback_is_the_old_behaviour(tmp_path):
    lab, _ = _lab(tmp_path, steps=2)
    result = lab.run("q")                                # no checkpoint= -> nothing persisted
    assert result.accepted_steps == 2
