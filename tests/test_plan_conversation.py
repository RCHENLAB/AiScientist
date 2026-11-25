"""The plan phase as one append-only PI conversation (RCHENLAB/AiScientist#122, item 5 of #151).

From the request to the shown plan, the plan phase was 13-15 separate model calls, each with a system
prompt of its own, so no call could reuse the prompt prefix of another and the dataset profile, the
guidance and the tools were processed again every time. Pinned here with scripted models:

* the draft starts one conversation with a fixed prefix; the review's PI round and every reply's
  reading, answer and revision are turns appended after it, and a reply to a saved plan (a lab built
  anew) continues the saved conversation;
* the draft returns structured steps (tool, settings, skill), checked by code with the binder's own
  rule; the binding and skill calls are made only for a step the check does not settle, and the plan
  document for the existing fixtures is the same either way;
* the review is one pass: the Critic once, the PI only when the Critic reports a problem;
* the experts' prompts share their context and end with the persona.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("yaml")

from aiscientist.agents.plan_conversation import MAX_TURNS, PlanConversation, shared_prefix  # noqa: E402
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness  # noqa: E402
from aiscientist.agents.research_lab import LabConfig, ResearchLab, Specialist  # noqa: E402
from plan_turns import instruction  # noqa: E402

QC = ("**QC & normalization** — Measure each cell's quality with `mcp_run_scanpy_qc` and remove cells "
      "detecting fewer than 200 genes and cells whose reads are more than 10% mitochondrial.")
CLUSTER = ("**Clustering & UMAP** — Group the cells with `mcp_run_clustering` at resolution 0.5 and "
           "lay them out on a 2-D map.")
AGENDA = [QC, CLUSTER]
STEPS = [{"tool": "mcp_run_scanpy_qc", "settings": {"min_genes": 200, "max_pct_mt": 10}, "skill": None},
         {"tool": "mcp_run_clustering", "settings": {"resolution": 0.5}, "skill": None}]
# The binder's reply for the same steps (as tests/test_plan_yaml_flow.py scripts it): n_pcs is the
# binder being helpful, and the rule drops it because the sentence never states 30.
BOUND = [{"step": 1, "settings": {"min_genes": 200, "max_pct_mt": 10}},
         {"step": 2, "settings": {"resolution": 0.5, "n_pcs": 30}}]
PI_SYSTEM = "Principal Investigator of a bioinformatics lab"


def _catalog() -> list[HarnessTool]:
    def tool(name: str, props: dict) -> HarnessTool:
        return HarnessTool(name, f"{name} test double", {"type": "object", "properties": props},
                           lambda args, _ctx: {"status": "ok"})

    return [tool("mcp_run_scanpy_qc", {"min_genes": {"type": "integer", "default": 200},
                                       "max_pct_mt": {"type": "number", "default": 20}}),
            tool("mcp_run_clustering", {"resolution": {"type": "number", "default": 1.0},
                                        "n_pcs": {"type": "integer", "default": 30}})]


def _finish(messages, _tools):
    return {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
        "name": "finish", "arguments": json.dumps({"answer": "done"})}}]}


def _models(calls: list, *, steps=STEPS, critic=None, final=None, reading=None, answer="Because 10% is the norm."):
    """Scripted models, each call kept as (kind, messages). A conversation turn is told by its LAST
    message (its instruction); a call of its own by its system prompt."""
    def complete(messages):
        system = last = instruction(messages)

        def kind(name, reply):
            calls.append((name, [dict(m) for m in messages]))
            return reply

        if "read a researcher's reply" in last:
            return kind("read", json.dumps({"parts": reading or [{"kind": "question", "text": "why?"}]}))
        if "answering a researcher's question" in last:
            return kind("answer", answer)
        if "finalizing the analysis plan" in last:
            return kind("finalize", json.dumps(final or {"final_agenda": AGENDA, "reason": "keep"}))
        if "changing an analysis plan" in last:
            return kind("revise", json.dumps({"ops": [{"op": "change", "step": "s2",
                                                       "settings": {"resolution": 0.8}}]}))
        if "reviewing a DRAFT analysis plan" in system:
            return kind("critic", json.dumps(critic or {"issues": [], "revised_agenda": []}))
        if "filling in a form" in system:
            return kind("bind", json.dumps(BOUND))
        if "match the steps of an analysis plan" in system:
            return kind("skills", "[]")
        if PI_SYSTEM in system:
            body = {"breadth": "specific", "agenda": AGENDA, "later": [], "self_sourced": "none"}
            return kind("draft", json.dumps({**body, **({"plan_steps": steps} if steps is not None else {})}))
        if "rigorous scientific Critic" in system:
            return kind("step_critic", json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"}))
        return kind("other", "FINAL REPORT")
    return complete


def _lab(calls: list, *, conversation: bool = True, plan_review: bool = False, **models) -> ResearchLab:
    return ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"),
                       LabConfig(plan_yaml=True, plan_review=plan_review, auto_select_skill=False,
                                 mode="single", planner="linear", plan_conversation=conversation),
                       complete_fn=_models(calls, **models),
                       scientist=ResearchHarness(catalog=_catalog(), chat_fn=_finish))


class Card(Exception):
    pass


def _to_card(lab: ResearchLab, events: list):
    seen = {}

    def review(kind, payload):
        seen.update(kind=kind, payload=list(payload))
        raise Card()

    with pytest.raises(Card):
        lab.run("Cluster the cells", events.append, review)
    return seen


def _doc(events):
    return [{k: s.get(k) for k in ("tool", "settings", "skill")}
            for s in [e for e in events if e["type"] == "plan_doc"][-1]["steps"]]


# --- the conversation ------------------------------------------------------------------------------

def test_the_conversation_keeps_its_prefix_and_only_ever_grows():
    convo = PlanConversation.start("SYS", "context")
    convo.answer("draft")
    first = convo.ask("turn 1")
    convo.add("turn 1", "reply 1")
    second = convo.ask("turn 2")
    assert second[:len(first)] == first[:-1] + [first[-1]], "every turn is sent after the whole conversation"
    back = PlanConversation.from_dict(json.loads(json.dumps(convo.to_dict())))
    assert back.messages == convo.messages and back.answered
    assert PlanConversation.from_dict(None) is None and PlanConversation.from_dict({"messages": []}) is None
    for n in range(MAX_TURNS * 2):
        convo.add(f"turn {n}", "reply")
    assert convo.messages[:3] == back.messages[:3], "the fixed prefix is never dropped"
    assert len(convo.messages) <= 3 + 2 * MAX_TURNS
    assert shared_prefix([[{"role": "user", "content": "abc"}], [{"role": "user", "content": "abd"}]]) == [0, 7]


def test_the_review_and_a_reply_are_turns_after_the_drafts_fixed_prefix():
    calls: list = []
    critic = {"issues": ["step 2 never says how many PCs"], "revised_agenda": AGENDA}
    lab = _lab(calls, plan_review=True, critic=critic)
    events: list = []
    _to_card(lab, events)
    kinds = [k for k, _ in calls]
    assert kinds == ["draft", "critic", "finalize"], "the plan is shown after three calls; no binder or skill call"
    draft, finalize = calls[0][1], calls[2][1]
    assert finalize[:len(draft)] == draft[:2] + finalize[2:len(draft)]
    assert finalize[0]["content"] == draft[0]["content"] and finalize[1] == draft[1], \
        "the review's PI round is a turn after the draft's fixed prefix"
    assert finalize[2]["role"] == "assistant" and "plan_steps" in finalize[2]["content"]
    saved = lab.plan_context
    assert len(saved["conversation"]["messages"]) == 5

    # A reply to the saved plan: a lab built anew continues the saved conversation.
    again = _lab(calls := [])
    doc = lab._plan_doc
    with pytest.raises(Card):
        again.run("Cluster the cells", [].append, lambda *_a: (_ for _ in ()).throw(Card()),
                  saved_plan=doc, plan_context=saved,
                  saved_plan_decision={"action": "revise", "feedback": "why 10% mitochondrial?"})
    assert [k for k, _ in calls] == ["read", "answer"], "a reply costs two calls"
    prefix = saved["conversation"]["messages"]
    for _kind, messages in calls:
        assert messages[:len(prefix)] == prefix, "each turn is sent after the saved conversation"
    reading, answer = calls[0][1], calls[1][1]
    assert answer[:len(reading)] == reading[:-1] + [reading[-1]] and answer[len(reading)]["role"] == "assistant"
    assert len(again.plan_context["conversation"]["messages"]) == len(prefix) + 4


def test_a_change_is_a_turn_of_the_conversation_too():
    calls: list = []
    lab = _lab(calls, reading=[{"kind": "change", "text": "cluster at resolution 0.8"}])
    events: list = []
    replies = iter([{"action": "revise", "feedback": "cluster at resolution 0.8"}, {"action": "approve"}])
    lab.run("Cluster the cells", events.append, lambda *_a: next(replies))
    kinds = [k for k, _ in calls if k not in ("step_critic", "other")]
    assert kinds == ["draft", "read", "revise"]
    draft, revise = calls[0][1], next(m for k, m in calls if k == "revise")
    assert revise[:2] == draft[:2] and len(revise) == 6
    assert _doc(events)[1]["settings"] == {"resolution": 0.8}


def test_with_the_conversation_off_each_call_stands_alone_as_before():
    calls: list = []
    lab = _lab(calls, conversation=False, plan_review=True,
               critic={"issues": ["x"], "revised_agenda": AGENDA})
    _to_card(lab, [])
    finalize = next(m for k, m in calls if k == "finalize")
    assert "finalizing the analysis plan" in finalize[0]["content"] and len(finalize) == 2
    assert lab.plan_context["conversation"] is None


# --- structured steps ---------------------------------------------------------------------------------

def test_the_drafts_structured_steps_make_the_same_document_without_the_binder():
    with_steps, without = [], []
    events_a, events_b = [], []
    _to_card(_lab(with_steps), events_a)
    _to_card(_lab(without, steps=None), events_b)
    assert _doc(events_a) == _doc(events_b) == [
        {"tool": "mcp_run_scanpy_qc", "settings": {"min_genes": 200, "max_pct_mt": 10}, "skill": None},
        {"tool": "mcp_run_clustering", "settings": {"resolution": 0.5}, "skill": None}]
    assert "bind" not in [k for k, _ in with_steps], "the PI's steps settled every setting"
    assert "bind" in [k for k, _ in without], "with no structured steps the binder is the fallback"
    settled = [e for e in events_a if e["type"] == "plan_drafted_steps"][0]
    assert settled["settled"] == 2 and settled["to_bind"] == 0


@pytest.mark.parametrize("bad", [
    {"tool": "mcp_run_clustering", "settings": {"resolution": 1.7}, "skill": None},      # not in the sentence
    {"tool": "mcp_run_clustering", "settings": {"n_neighbors": 15}, "skill": None},      # not a setting
    {"tool": "mcp_run_scanpy_qc", "settings": {"resolution": 0.5}, "skill": None},       # another tool
    {"tool": "mcp_run_clustering"},                                                       # no settings given
])
def test_a_step_the_check_does_not_settle_goes_to_the_binder_alone(bad):
    calls: list = []
    events: list = []
    _to_card(_lab(calls, steps=[STEPS[0], bad]), events)
    binds = [m for k, m in calls if k == "bind"]
    assert len(binds) == 1
    listing = binds[0][1]["content"]
    assert "Group the cells" in listing and "Measure each cell" not in listing, "only the unsettled step"
    assert _doc(events)[1]["settings"] == {"resolution": 0.5}, "the binder's rule decides, as before"


def test_structured_steps_of_another_length_are_not_matched():
    calls: list = []
    events: list = []
    _to_card(_lab(calls, steps=[STEPS[0]]), events)
    assert "bind" in [k for k, _ in calls]
    assert _doc(events)[0]["settings"] == {"min_genes": 200, "max_pct_mt": 10}


# --- the review: one pass -------------------------------------------------------------------------------

def test_a_clean_review_is_one_call_and_a_problem_adds_exactly_one():
    calls: list = []
    _to_card(_lab(calls, plan_review=True), [])
    assert [k for k, _ in calls] == ["draft", "critic"]
    calls = []
    missing = {"issues": ["The plan is missing the differential expression analysis between DDX41 and WT; "
                          "no step runs run_de or run_pseudobulk_de."], "revised_agenda": AGENDA}
    _to_card(_lab(calls, plan_review=True, critic=missing,
                  final={"final_agenda": AGENDA, "reason": "QC first"}), [])
    assert [k for k, _ in calls] == ["draft", "critic", "finalize"], "no third call for a missing analysis"
    asked = json.loads(next(m for k, m in calls if k == "finalize")[-1]["content"].split("\n\n", 1)[1])
    assert "missing an analysis" in asked["unresolved"], "the insistence is part of the one PI round"


# --- the experts ------------------------------------------------------------------------------------------

def test_the_experts_share_their_prompt_and_end_with_the_persona():
    lab = _lab([])
    seen: list = []
    lab._complete_concurrent = lambda lists: seen.extend(lists) or ["input"] * len(lists)
    lab._meeting_critic = lambda *a, **k: (0.9, "ok")
    lab._complete = lambda messages, **k: "SYNTH"
    team = (Specialist("Statistician", "You weigh replication and power.", ("stats",)),
            Specialist("Retina biologist", "You know photoreceptor biology.", ("retina",)))
    lab.config = type(lab.config)(**{**lab.config.__dict__, "meeting_tools": False, "meeting_rounds": 1})
    lab._team_meeting_inner("Compare DDX41 with WT", "How should we do it?", team, "design", lambda e: None)
    assert len(seen) == 2 and seen[0][0] == seen[1][0], "one system prompt for every expert"
    a, b = seen[0][1]["content"], seen[1][1]["content"]
    common = len(a) - len(a.split("You are Statistician.", 1)[1]) - len("You are Statistician.")
    assert a[:common] == b[:common] and "Meeting topic:" in a[:common], "the shared context comes first"
    assert a.rstrip().endswith("You are Statistician. You weigh replication and power.\n\nYour input:".rstrip())
    assert "Retina biologist" not in a and "Statistician" not in seen[0][0]["content"]
