"""A small plan revision must not disturb the steps the user did not mention.

In plan mode the user types natural-language feedback and the PI used to RE-DRAFT the whole agenda,
with one sentence of prompt protecting everything else. Measured on the real served Qwen3.6
(`experiments/plan_revision_ab`, 19 trials): that damaged unmentioned steps in 100% of trials — a
mean of 3.89 other steps rewritten, plan length changed 89% of the time. Asking only for
"resolution 1.0" once replaced the differential-expression step with a descriptive summary and
dropped the final step, with no diff shown to the researcher.

The revision now asks for a PATCH — the model names one step and rewrites it, and this code applies
the edit — so untouched steps are the same objects they were. What is pinned here is exactly that
property, plus the fallback: a request that a single-step edit cannot express must still reach the
whole-plan redraft rather than being silently dropped.
"""

from __future__ import annotations

import json

from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.research_lab import LabConfig, ResearchLab, _parse_plan_patch

AGENDA = [
    "QC the cells: filter, normalize and log1p, report counts",
    "Cluster the cells with Leiden at resolution 0.5 and produce a UMAP",
    "Assign cell-type labels from canonical markers",
    "Run DDX41 vs WT differential expression within each major cell class",
    "Run pathway enrichment on the changed genes per cell class",
    "Summarise shared versus class-specific changes",
]


def _lab(tmp_path, reply):
    """A lab whose model returns `reply` (str or callable(messages) -> str)."""
    fn = reply if callable(reply) else (lambda _m: reply)

    class _Stub:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

    return ResearchLab(HarnessContext(decisions={}, workspace=tmp_path),
                       LabConfig(max_steps=20, auto_select_skill=False),
                       complete_fn=fn, scientist=_Stub())


# --- parsing ------------------------------------------------------------------


def test_parses_a_well_formed_patch():
    assert _parse_plan_patch('{"step": 2, "new_text": "Cluster at 1.0"}', 6) == (1, "Cluster at 1.0")


def test_parses_a_patch_wrapped_in_prose_and_fences():
    raw = 'Sure!\n```json\n{"step": 3, "new_text": "Reuse majorclass"}\n```\n'
    assert _parse_plan_patch(raw, 6) == (2, "Reuse majorclass")


def test_step_zero_means_the_model_declined():
    """Its escape hatch for "this needs more than one step changed" — must route to a redraft."""
    assert _parse_plan_patch('{"step": 0, "new_text": ""}', 6) is None


def test_out_of_range_and_empty_and_garbage_are_all_unusable():
    assert _parse_plan_patch('{"step": 99, "new_text": "x"}', 6) is None
    assert _parse_plan_patch('{"step": 2, "new_text": "   "}', 6) is None
    assert _parse_plan_patch("I think you should reconsider the plan.", 6) is None


# --- applying -----------------------------------------------------------------


def test_only_the_named_step_changes(tmp_path):
    lab = _lab(tmp_path, '{"step": 2, "new_text": "Cluster the cells with Leiden at resolution 1.0"}')

    out = lab._patch_plan(list(AGENDA), "use resolution 1.0", lambda _e: None)

    assert out is not None
    assert len(out) == len(AGENDA)
    assert out[1] == "Cluster the cells with Leiden at resolution 1.0"
    for i in (0, 2, 3, 4, 5):
        assert out[i] == AGENDA[i], f"step {i + 1} was disturbed"
    # The step that used to disappear in the redraft path is specifically still there.
    assert "differential expression" in out[3]


def test_the_edit_is_emitted_as_a_before_after_diff(tmp_path):
    lab = _lab(tmp_path, '{"step": 2, "new_text": "Cluster at resolution 1.0"}')
    seen: list[dict] = []

    lab._patch_plan(list(AGENDA), "use resolution 1.0", seen.append)

    diff = [e for e in seen if e.get("type") == "plan_patched"]
    assert len(diff) == 1
    assert diff[0]["step"] == 2
    assert diff[0]["before"] == AGENDA[1]
    assert diff[0]["after"] == "Cluster at resolution 1.0"


def test_a_declined_patch_returns_none_so_the_caller_redrafts(tmp_path):
    lab = _lab(tmp_path, '{"step": 0, "new_text": ""}')
    assert lab._patch_plan(list(AGENDA), "add a trajectory analysis step", lambda _e: None) is None


def test_a_no_op_rewrite_is_not_accepted_as_a_revision(tmp_path):
    """Echoing the step back unchanged is not an answer — redraft instead of pretending it worked."""
    lab = _lab(tmp_path, json.dumps({"step": 2, "new_text": AGENDA[1]}))
    assert lab._patch_plan(list(AGENDA), "use resolution 1.0", lambda _e: None) is None


def test_a_model_error_falls_back_rather_than_failing_the_revision(tmp_path):
    def boom(_messages):
        raise RuntimeError("vLLM died")

    lab = _lab(tmp_path, boom)
    assert lab._patch_plan(list(AGENDA), "use resolution 1.0", lambda _e: None) is None


# --- the wiring in _pi_plan ---------------------------------------------------


def test_pi_plan_uses_the_patch_and_never_calls_the_redraft(tmp_path):
    calls: list[str] = []

    def fn(messages):
        system = messages[0]["content"]
        calls.append("patch" if "revising ONE step" in system else "redraft")
        return '{"step": 2, "new_text": "Cluster the cells with Leiden at resolution 1.0"}'

    lab = _lab(tmp_path, fn)

    kind, agenda = lab._pi_plan("q", lambda _e: None, feedback="use resolution 1.0",
                                prior_agenda=list(AGENDA), latest_feedback="use resolution 1.0")

    assert kind == "agenda"
    assert calls == ["patch"], "a successful patch must not also redraft"
    assert agenda[3] == AGENDA[3]


def test_pi_plan_falls_back_to_the_redraft_when_the_patch_declines(tmp_path):
    calls: list[str] = []

    def fn(messages):
        system = messages[0]["content"]
        if "revising ONE step" in system:
            calls.append("patch")
            return '{"step": 0, "new_text": ""}'
        calls.append("redraft")
        return json.dumps({"agenda": ["A", "B", "C"]})

    lab = _lab(tmp_path, fn)

    kind, agenda = lab._pi_plan("q", lambda _e: None, feedback="add a trajectory step",
                                prior_agenda=list(AGENDA), latest_feedback="add a trajectory step")

    assert calls == ["patch", "redraft"]
    assert kind == "agenda" and agenda[:3] == ["A", "B", "C"]


def test_a_first_draft_is_unaffected(tmp_path):
    """No prior agenda, no feedback -> the ordinary planning path, untouched."""
    calls: list[str] = []

    def fn(messages):
        calls.append("patch" if "revising ONE step" in messages[0]["content"] else "redraft")
        return json.dumps({"agenda": ["A", "B"]})

    lab = _lab(tmp_path, fn)
    kind, agenda = lab._pi_plan("q", lambda _e: None)

    assert calls == ["redraft"] and kind == "agenda" and agenda[:2] == ["A", "B"]
