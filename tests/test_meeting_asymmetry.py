"""Make the team's disagreement mean something.

Before this, every member of a "team" received the IDENTICAL prompt and differed only by a persona
string, held no private evidence, and could not call a single tool. Their disagreement was drawn
from one posterior — self-consistency sampling wearing name tags. And the machinery around it was
tuned to erase what little there was: the meeting ends early at a high Critic score, the high-score
feedback said "consolidate and tighten", and the synthesis asked for "the decisions the team
converges on".

Three coordinated changes are pinned here:
  1. each expert is dealt a DIFFERENT slice of the accepted findings, and is told its view is partial;
  2. experts may call READ-ONLY tools, so one can return with evidence the others do not have;
  3. the synthesis reports AGREED **and** UNRESOLVED — a disagreement is a result, and an
     unresolved question with the check that would settle it beats an unearned consensus.
"""

from __future__ import annotations


from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.research_lab import (
    _MEETING_SYNTH_SYSTEM,
    _MEETING_TOOLS,
    CriticVerdict,
    LabConfig,
    LabRound,
    ResearchLab,
    Specialist,
    _assign_evidence,
    _round_feedback,
)

EXPERTS = (Specialist("Retina biologist", "retina"),
           Specialist("Statistician", "stats"),
           Specialist("Clinical geneticist", "clinical"))


def _round(i: int, step: str, answer: str, *, accept: bool = True, figures=()) -> LabRound:
    return LabRound(i, i, step, "X",
                    {"final_answer": answer,
                     "steps": [{"tool": "run_de", "ok": True,
                                "result": {"status": "ok", "figures": list(figures)}}]},
                    CriticVerdict("accept" if accept else "revise", 0.9, ""))


ROUNDS = [_round(i, f"Step {i}", f"finding {i}", figures=[f"figures/f{i}.png"]) for i in range(1, 7)]


# --- 1. information asymmetry ---------------------------------------------------


def test_every_expert_gets_a_different_slice():
    blocks = _assign_evidence(EXPERTS, ROUNDS)

    assert len(blocks) == 3
    owned = [{ln for ln in b.splitlines() if ln.startswith("- Step")} for b in blocks]
    assert all(owned), "each expert must own something"
    assert owned[0] != owned[1] != owned[2]
    assert not (owned[0] & owned[1]) and not (owned[1] & owned[2]), "slices must not overlap"
    # every finding is owned by someone — asymmetry must not lose evidence
    assert len(owned[0] | owned[1] | owned[2]) == 6


def test_an_expert_is_told_its_view_is_partial():
    """An expert that does not know it holds part of the picture reports it as the whole one."""
    block = _assign_evidence(EXPERTS, ROUNDS)[0]
    assert "PARTIAL" in block
    assert "OTHER members" in block
    assert "Do NOT claim anything about findings you were not assigned" in block


def test_the_block_carries_the_artifact_paths_so_a_claim_can_be_checked():
    block = _assign_evidence(EXPERTS, ROUNDS)[0]
    assert "figures/f1.png" in block
    assert "evidence on disk" in block


def test_it_asks_for_the_discriminating_check_not_just_the_disagreement():
    block = _assign_evidence(EXPERTS, ROUNDS)[0]
    assert "CONTRADICT" in block and "what would settle it" in block


def test_rejected_rounds_are_not_dealt_out():
    rounds = [_round(1, "a", "ok"), _round(2, "b", "bad", accept=False)]
    blocks = _assign_evidence((EXPERTS[0],), rounds)
    assert "Step 1" in blocks[0] and "Step 2" not in blocks[0]


def test_a_design_meeting_has_no_findings_and_says_nothing():
    """Held before any analysis — the asymmetry there comes from the tools, not from findings."""
    assert _assign_evidence(EXPERTS, []) == ["", "", ""]


def test_more_experts_than_findings_still_assigns_everyone_a_role():
    blocks = _assign_evidence(EXPERTS, [_round(1, "a", "only finding")])
    assert "Step 1" in blocks[0]
    for b in blocks[1:]:
        assert "none" in b and "do not invent results" in b


# --- 2. read-only tools ---------------------------------------------------------


def test_the_meeting_toolset_cannot_mutate_the_run():
    """A whitelist, not a blacklist. The analysis catalog is full of tools that rewrite the
    checkpoint chain, and a meeting must never be able to reach one."""
    assert _MEETING_TOOLS == {"inspect_dataset", "literature_search", "deep_literature"}
    for banned in ("run_de", "run_code", "run_scanpy_qc", "run_clustering", "annotate_variants"):
        assert banned not in _MEETING_TOOLS


def test_experts_fall_back_to_plain_completions_without_tools(tmp_path):
    """Offline / no tool-capable Scientist: the meeting must still happen."""
    class _Stub:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

    lab = ResearchLab(HarnessContext(decisions={}, workspace=tmp_path),
                      LabConfig(auto_select_skill=False),
                      complete_fn=lambda _m: "said something", scientist=_Stub())

    assert lab._expert_turns([[{"role": "system", "content": "s"},
                               {"role": "user", "content": "u"}]], lambda _e: None) == \
        ["said something"]


def test_meeting_tools_can_be_switched_off(tmp_path):
    assert LabConfig().meeting_tools is True
    assert LabConfig(meeting_tools=False).meeting_tools is False
    assert LabConfig().meeting_tool_calls > 0, "a meeting turn is a lookup, and must stay bounded"


# --- 3. disagreement survives ----------------------------------------------------


def test_the_synthesis_demands_an_unresolved_section():
    assert "UNRESOLVED" in _MEETING_SYNTH_SYSTEM
    assert "AGREED" in _MEETING_SYNTH_SYSTEM
    assert "WHAT WOULD SETTLE IT" in _MEETING_SYNTH_SYSTEM
    assert "never invent agreement" in _MEETING_SYNTH_SYSTEM.lower()


def test_a_high_score_no_longer_asks_the_team_to_drop_its_disagreement():
    """The old high-score branch said only "consolidate and tighten", which told an expert holding
    a live objection to let it go."""
    msg = _round_feedback(0.92, "minor wording")
    assert "not a reason to drop a disagreement" in msg.lower()
    assert "settle it" in msg.lower()


def test_low_scores_still_push_back_hard():
    assert "push back HARD" in _round_feedback(0.3, "weak")


# --- the tool path itself (the branch the fallback test does NOT cover) -----------


def _tool_lab(tmp_path, chat_fn, *, catalog_names=("inspect_dataset", "literature_search",
                                                   "run_de", "run_code")):
    """A lab whose Scientist exposes a catalog, so `_expert_turns` takes its TOOL branch."""
    from aiscientist.agents.research_harness import HarnessTool

    tools = [HarnessTool(n, f"{n} desc", {"type": "object", "properties": {}},
                         lambda a, c, _n=n: {"status": "ok", "tool": _n})
             for n in catalog_names]

    class _Sci:
        catalog = tools
        _chat_fn = staticmethod(chat_fn)

        def add_tools(self, *_a, **_k):
            return None

    return _lab_with(tmp_path, _Sci())


def _lab_with(tmp_path, scientist):
    return ResearchLab(HarnessContext(decisions={}, workspace=tmp_path),
                       LabConfig(auto_select_skill=False),
                       complete_fn=lambda _m: "fallback text", scientist=scientist)


def test_an_expert_actually_calls_a_tool_and_reports_what_it_found(tmp_path):
    """The whole point of part 2: an expert can go and LOOK, not just talk."""
    seen_tools: list[list[str]] = []

    def chat_fn(messages, tools, **_k):
        seen_tools.append(sorted(t["function"]["name"] for t in tools))
        if not any(m.get("role") == "tool" for m in messages):
            return {"tool_calls": [{"id": "1", "type": "function",
                                    "function": {"name": "literature_search",
                                                 "arguments": "{}"}}]}
        return {"content": "I checked the literature and my slice does not support the claim."}

    out = _tool_lab(tmp_path, chat_fn)._expert_turns(
        [[{"role": "system", "content": "you are X"}, {"role": "user", "content": "topic"}]],
        lambda _e: None)

    assert "does not support" in out[0], "the expert's tool-grounded answer must come back"
    assert seen_tools, "the tool branch must actually have run"


def test_the_expert_is_offered_ONLY_the_read_only_whitelist(tmp_path):
    """A meeting must not be able to reach a tool that rewrites the checkpoint chain."""
    offered: list[list[str]] = []

    def chat_fn(messages, tools, **_k):
        offered.append(sorted(t["function"]["name"] for t in tools))
        return {"content": "spoke"}

    _tool_lab(tmp_path, chat_fn)._expert_turns(
        [[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]], lambda _e: None)

    names = set(offered[0])
    assert names <= _MEETING_TOOLS | {"finish"}, f"non-whitelisted tools offered: {names}"
    assert "run_de" not in names and "run_code" not in names


def test_a_broken_tool_loop_still_yields_an_opinion(tmp_path):
    """An expert that cannot use its tools must still speak — a meeting is not allowed to fail."""
    def chat_fn(*_a, **_k):
        raise RuntimeError("tool backend down")

    events: list[dict] = []
    out = _tool_lab(tmp_path, chat_fn)._expert_turns(
        [[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]], events.append)

    assert out == ["fallback text"]
    assert any(e.get("type") == "expert_tools_failed" for e in events), "the degradation is reported"


def test_an_empty_tool_answer_falls_back_rather_than_contributing_nothing(tmp_path):
    def chat_fn(*_a, **_k):
        return {"content": "   "}

    out = _tool_lab(tmp_path, chat_fn)._expert_turns(
        [[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]], lambda _e: None)
    assert out == ["fallback text"]


def test_a_budget_exhausted_expert_speaks_instead_of_reciting_its_tool_log(tmp_path):
    """The digest that closes a STEP must never be spoken as an expert's contribution.

    ``ResearchHarness`` was changed so a step that runs out of tool turns returns a deterministic
    tool digest instead of an empty string — right for the Critic and the report writer, wrong
    here: this meeting path used ``final_answer or self._complete(...)``, so the newly non-empty
    digest silently disabled the fall-back and meetings started printing "(auto-summary: ...)"
    where an opinion belonged. The expert now speaks, with its lookups handed back as notes.
    """
    calls: list[str] = []

    def chat_fn(messages, tools, **_k):     # never calls `finish` → burns the tool budget
        calls.append("tool")
        return {"tool_calls": [{"id": "1", "type": "function",
                                "function": {"name": "inspect_dataset", "arguments": "{}"}}]}

    lab = _tool_lab(tmp_path, chat_fn)
    seen: list[list[dict]] = []
    lab._complete = lambda messages: (seen.append(messages), "AC nuclei look under-sequenced.")[1]

    out = lab._expert_turns([[{"role": "system", "content": "You are the statistician."},
                              {"role": "user", "content": "Topic: the analysis plan."}]],
                            lambda _e: None)

    assert out == ["AC nuclei look under-sequenced."]
    assert not any("auto-summary" in o for o in out)
    # and the expert was told what its own lookups returned, rather than losing them
    assert "auto-summary" in seen[0][-1]["content"]
    assert seen[0][-1]["role"] == "user"
