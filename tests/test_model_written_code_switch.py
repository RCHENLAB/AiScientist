"""Model-written code is a deployment switch, off by default (RCHENLAB/AiScientist#150).

Every task rests on a tested tool or skill, or it is not offered: the code tools are not offered and
are refused at dispatch, no plan from any source runs a step that needs them, and such a step is
reported as not available (with the closest skill when there is one) and recorded for the team.
Turning ``AISCIENTIST_MODEL_WRITTEN_CODE`` on restores the earlier behaviour. Scripted models
throughout.
"""
from __future__ import annotations

import json
import re

import pytest

from aiscientist.agents import code_policy
from aiscientist.agents import research_lab as rl
from aiscientist.agents import skills as skills_mod
from aiscientist.agents.code_policy import NotAvailable, not_available_markdown
from aiscientist.agents.mcp_plan_migration import PlanMigrationRequired
from aiscientist.agents.plan_doc import PlanDoc, PlanStep
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness, default_catalog
from aiscientist.agents.research_lab import LabConfig, ResearchLab, ResumeState, code_gate
from aiscientist.agents.skills import refresh_skills

from plan_turns import instruction  # noqa: E402  (tests/ is on sys.path)

SIGNATURE = "score-gene-signatures-with-scanpy"
SIG_MD = """---
name: score-gene-signatures-with-scanpy
description: Score every cell for one or more gene signatures with scanpy score_genes and summarise the score per group. Use when a question scores a gene set per cell.
metadata:
  category: single-cell
  aliases: "score_signature"
---
# Score gene signatures

Score with `sc.tl.score_genes` against 50 control genes, then report the score per group.
"""
SIG_CODE = "import scanpy as sc\n# ADAPT: SIGNATURE\nsc.tl.score_genes(adata, SIGNATURE, ctrl_size=50)\n"

QC = "**Quality control** — Filter low-quality cells with `mcp_run_scanpy_qc` at max_pct_mt 20."
CLUSTER = "**Clustering** — Group the cells with `mcp_run_clustering` at resolution 0.5."
SCORE = ("**Interferon signature score** — Score every cell for the interferon-stimulated genes and "
         "summarise the score per cluster.")
SCORE_CODE = ("**Interferon signature score** — Apply skill `score_signature` with `run_code` to "
              "score every cell for the interferon-stimulated genes.")
VELOCITY = ("**RNA velocity** — Infer RNA velocity trajectories from the spliced and unspliced "
            "counts and project them on the embedding.")
PLOT = "**Score by cluster** — Draw the interferon score per cluster with `mcp_run_clustering` labels."
ACCEPT = json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})

SCORE_MATCH = json.dumps({"unavailable": [{"step": 2, "closest_skill": SIGNATURE, "lacks": ""}],
                          "needs": []})
NO_MATCH = json.dumps({"unavailable": [{"step": 1, "closest_skill": "", "lacks": ""}], "needs": []})


@pytest.fixture
def code_on(monkeypatch):
    monkeypatch.setenv(code_policy.ENV_VAR, "1")


@pytest.fixture
def library(tmp_path, monkeypatch, skill_release):
    """A release holding one template skill (its values marked ADAPT), loaded in place of the real one."""
    folder = tmp_path / "skills" / "single-cell" / SIGNATURE
    (folder / "scripts").mkdir(parents=True)
    (folder / "SKILL.md").write_text(SIG_MD, encoding="utf-8")
    (folder / "scripts" / "reference.py").write_text(SIG_CODE, encoding="utf-8")
    monkeypatch.delenv("AISCIENTIST_INDUCED_SKILLS_DIR", raising=False)
    skill_release(tmp_path / "skills")
    lib: dict = {}
    monkeypatch.setattr(skills_mod, "SKILLS", lib)
    monkeypatch.setattr(rl, "ATOMIC_SKILLS", lib)
    monkeypatch.setattr(skills_mod, "ALL_SKILLS", {})
    monkeypatch.setattr(skills_mod, "_SIGNATURE", ())
    monkeypatch.setattr(skills_mod, "_REGISTERED", {})
    refresh_skills()
    assert set(lib) == {SIGNATURE}
    return lib


def _tool(name, calls):
    def run(args, ctx):
        calls.append((name, dict(args)))
        return {"status": "ok", "summary": f"{name} ran"}
    return HarnessTool(name, f"{name} tool", {"type": "object", "properties": {"code": {"type": "string"}}},
                       run)


def _lab(agenda, *, match=SCORE_MATCH, critic=None, final=None, config=None, extra=None):
    """A lab whose PI drafts ``agenda``; ``match`` is the classify reply naming the closest skill.
    Returns the lab, the steps the Scientist was briefed on, the system prompts seen and the events."""
    seen, briefed, events, calls = [], [], [], []

    def complete(messages):
        system = instruction(messages)
        seen.append(system)
        if extra is not None and (reply := extra(system, messages)) is not None:
            return reply
        if "no tested tool performs on this deployment" in system:
            return match
        if "reviewing a DRAFT analysis plan" in system:
            return json.dumps(critic or {"issues": [], "revised_agenda": []})
        if "finalizing the analysis plan" in system:
            return json.dumps({"final_agenda": final or list(agenda)})
        if "Principal Investigator of a bioinformatics lab" in system:
            return json.dumps({"agenda": list(agenda)})
        if "rigorous scientific Critic" in system:
            return ACCEPT
        return "FINAL REPORT."

    def chat(messages, tools):
        """Call the tool the step names, once, then finish."""
        brief = next(m["content"] for m in messages if m.get("role") == "user")
        step = brief.split("Step to execute now: ", 1)[-1].split("\n", 1)[0]
        named = re.search(r"`(mcp_run_\w+|run_code|read_text)`", step)
        if named and not any(m.get("role") == "tool" for m in messages):
            name, args = named.group(1), {"code": "print(1)"}
        else:
            name, args = "finish", {"answer": "the step's result"}
        return {"content": "", "tool_calls": [{"id": "c", "type": "function", "function": {
            "name": name, "arguments": json.dumps(args)}}]}

    catalog = [*default_catalog(), _tool("mcp_run_scanpy_qc", calls), _tool("mcp_run_clustering", calls),
               _tool("run_code", calls), _tool("read_text", calls)]
    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"),
                      config or LabConfig(plan_review=False, auto_select_skill=False, mode="single"),
                      complete_fn=complete, scientist=ResearchHarness(catalog=catalog, chat_fn=chat))

    def on_event(ev):
        events.append(ev)
        if ev.get("type") == "scientist_start":
            briefed.append(ev["step"])
    return lab, briefed, seen, events, on_event


def _held(events):
    return [row for ev in events if ev.get("type") == "steps_unavailable" for row in ev["steps"]]


# --- the switch ------------------------------------------------------------------------------------

def test_the_switch_is_off_unless_turned_on(monkeypatch):
    monkeypatch.delenv(code_policy.ENV_VAR, raising=False)
    assert not code_policy.model_written_code()
    assert code_policy.withheld_tools() == code_policy.WITHHELD_TOOLS
    for value in ("0", "false", "no", ""):
        monkeypatch.setenv(code_policy.ENV_VAR, value)
        assert not code_policy.model_written_code(), value
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv(code_policy.ENV_VAR, value)
        assert code_policy.model_written_code(), value
    assert code_policy.withheld_tools() == frozenset()


def test_the_legacy_brand_name_of_the_switch_works(monkeypatch):
    from aiscientist.core.config import apply_brand_env_aliases
    monkeypatch.delenv(code_policy.ENV_VAR, raising=False)
    monkeypatch.setenv("BIOAGENT_MODEL_WRITTEN_CODE", "1")
    apply_brand_env_aliases()
    assert code_policy.model_written_code()


# --- tools: not offered, refused at dispatch ----------------------------------------------------------

class _Executor:
    """An executor that can run images, so run_in_environment would be built."""

    def run_in_image(self, command, image, **kwargs):
        return {"status": "ok", "returncode": 0, "stdout": ""}


def _shell():
    from aiscientist.hpc.shell import HpcShell
    shell = HpcShell.__new__(HpcShell)
    shell.environments = object()          # lab environments enabled: request_environment is built
    shell.workspace = None
    return shell


def _names(catalog):
    return {t.name for t in catalog}


def test_with_the_switch_off_the_code_tools_are_not_offered(monkeypatch):
    from aiscientist.agents import mcp_bridge, registry
    handed = {}

    def build_mcp_tools(executor, environment_tool, *, existing_names=()):
        handed["environment_tool"] = environment_tool
        handed["existing"] = set(existing_names)
        return []
    monkeypatch.setattr(mcp_bridge, "build_mcp_tools", build_mcp_tools)
    names = _names(registry.build_scientist_catalog(code_executor=_Executor(), hpc_shell=_shell()))
    assert not names & code_policy.WITHHELD_TOOLS
    # The lookups stay: reading goes through them.
    assert {"list_dir", "read_text", "find_files", "stat_path", "disk_usage", "describe_environment"} <= names
    # The image runner is still handed to the MCP bridge (it runs every MCP server's call), and no MCP
    # server may register a withheld name.
    assert handed["environment_tool"] is not None and handed["environment_tool"].name == "run_in_environment"
    assert code_policy.WITHHELD_TOOLS <= handed["existing"]


def test_with_the_switch_on_the_code_tools_are_offered_as_before(code_on, monkeypatch):
    from aiscientist.agents import mcp_bridge, registry
    monkeypatch.setattr(mcp_bridge, "build_mcp_tools", lambda *a, **k: [])
    names = _names(registry.build_scientist_catalog(code_executor=_Executor(), hpc_shell=_shell()))
    assert code_policy.WITHHELD_TOOLS <= names


def _harness(names_called, executed):
    turns = iter([*names_called, "finish"])

    def chat(messages, tools):
        name = next(turns)
        args = {"answer": "done"} if name == "finish" else {"code": "print(1)", "command": "ls"}
        return {"content": "", "tool_calls": [{"id": name, "type": "function",
                                               "function": {"name": name, "arguments": json.dumps(args)}}]}
    catalog = [*default_catalog(), *(_tool(n, executed) for n in ("run_code", "run_shell", "run_in_environment"))]
    return ResearchHarness(catalog=catalog, chat_fn=chat)


def test_a_direct_call_to_a_code_tool_is_refused_at_dispatch(tmp_path):
    executed: list = []
    result = _harness(["run_code", "run_shell", "run_in_environment"], executed).run(
        "Do the step.", HarnessContext(workspace=tmp_path))
    assert executed == [], "a refused tool never runs, even when a caller put it in the catalog"
    refused = [e for e in result.errors if e["tool"] in code_policy.CODE_TOOLS]
    assert [e["tool"] for e in refused] == ["run_code", "run_shell", "run_in_environment"]
    assert all("turned off on this deployment" in e["error"] for e in refused)
    assert code_policy.refusal("request_environment") and code_policy.refusal("install_package")
    assert code_policy.refusal("mcp_run_de") is None and code_policy.refusal("read_text") is None


def test_with_the_switch_on_a_code_tool_runs(code_on, tmp_path):
    executed: list = []
    result = _harness(["run_code"], executed).run("Do the step.", HarnessContext(workspace=tmp_path))
    assert [name for name, _ in executed] == ["run_code"] and not result.errors


# --- the two replies ---------------------------------------------------------------------------------

def test_the_closest_skill_reply():
    item = NotAvailable(step=SCORE, skill=SIGNATURE, does="score every cell for gene signatures",
                        lacks="")
    assert item.line() == (
        '**Not available:** no tested tool or skill does "Interferon signature score". The closest '
        f"skill is `{SIGNATURE}`: score every cell for gene signatures. It does not "
        f"{code_policy.TEMPLATE_LACKS}.")
    told = NotAvailable(step=VELOCITY, skill=SIGNATURE, does="score cells", lacks="estimate RNA velocity")
    assert told.line().endswith("It does not estimate RNA velocity.")


def test_the_cannot_do_this_yet_reply():
    item = NotAvailable(step=VELOCITY)
    assert item.line() == ('**Not available:** no tested tool or skill does "RNA velocity", so we cannot '
                           "do this yet. The request has been recorded for the team.")
    # Not claimed when nothing could be recorded (no state directory).
    assert item.line(recorded=False).endswith("so we cannot do this yet.")
    assert not_available_markdown([item.to_dict()], recorded=True) == item.line() + "\n"
    assert not_available_markdown([], recorded=True) == ""


def test_a_step_that_needs_a_held_step_says_so():
    item = NotAvailable(step=PLOT, needs=SCORE)
    assert item.line() == ('**Not available:** "Score by cluster" needs the output of "Interferon '
                           'signature score", which no tested tool or skill does, so it does not run '
                           "either.")
    assert NotAvailable.from_dict(item.to_dict()) == item


def test_what_a_skill_does_comes_from_its_own_description():
    assert code_policy.skill_does("Score every cell for one or more gene signatures. Use when ...") == \
        "score every cell for one or more gene signatures"
    assert code_policy.skill_does("RNA velocity with scVelo.") == "RNA velocity with scVelo"


# --- plan sources ------------------------------------------------------------------------------------

def test_first_draft_runs_the_covered_step_and_names_the_closest_skill(library):
    lab, briefed, seen, events, on_event = _lab([QC, SCORE])
    result = lab.run("Score an interferon signature across the clusters of this dataset", on_event)
    assert briefed == [QC], "only the covered step reaches the Scientist"
    assert [r["step"] for r in result.unavailable_steps] == [SCORE]
    row = result.unavailable_steps[0]
    assert row["skill"] == SIGNATURE and row["does"].startswith("score every cell for one or more")
    assert [e["source"] for e in events if e.get("type") == "steps_unavailable"] == ["draft"]
    line = NotAvailable.from_dict(row).line()
    assert f"The closest skill is `{SIGNATURE}`" in line and "It does not run as published" in line


def test_a_request_no_step_of_which_is_covered_runs_nothing(library):
    lab, briefed, seen, events, on_event = _lab([VELOCITY], match=NO_MATCH)
    result = lab.run("Infer RNA velocity trajectories for this dataset", on_event)
    assert briefed == [] and result.rounds == [] and result.accepted_steps == 0
    assert result.final_answer.startswith('**Not available:** no tested tool or skill does "RNA velocity", '
                                          "so we cannot do this yet.")
    assert any(e.get("type") == "plan_not_available" for e in events)
    assert not any("rigorous scientific Critic" in s for s in seen), "no step was judged: none ran"


def test_the_score_request_names_the_template_skill_and_runs_no_code(library):
    """#150's acceptance case: scoring is named with its skill and nothing runs, not even a check."""
    lab, briefed, seen, events, on_event = _lab(
        [SCORE], match=json.dumps({"unavailable": [{"step": 1, "closest_skill": SIGNATURE}]}))
    result = lab.run("Score an interferon signature across the clusters of this dataset", on_event)
    assert briefed == [] and result.rounds == []
    assert f"The closest skill is `{SIGNATURE}`" in result.final_answer


def test_without_a_model_reply_the_skill_named_in_the_step_is_used(library):
    lab, briefed, seen, events, on_event = _lab([QC, SCORE_CODE], match="not json")
    result = lab.run("QC, then score the ISG signature", on_event)
    assert briefed == [QC]
    assert result.unavailable_steps[0]["skill"] == SIGNATURE, "the step's own `score_signature` alias"


def test_a_step_that_needs_a_held_step_is_held_too(library):
    match = json.dumps({"unavailable": [{"step": 2, "closest_skill": SIGNATURE}],
                        "needs": [{"step": 3, "needs": 2}]})
    lab, briefed, seen, events, on_event = _lab([QC, SCORE, PLOT], match=match)
    result = lab.run("Score the ISG signature per cluster", on_event)
    assert briefed == [QC]
    rows = {r["step"]: r for r in result.unavailable_steps}
    assert rows[PLOT]["needs"] == SCORE and rows[SCORE]["skill"] == SIGNATURE


def test_a_plan_review_that_adds_a_code_step_does_not_run_it(library):
    added = "**Per-cluster QC table** — Tabulate the QC metrics per cluster with `run_code`."
    lab, briefed, seen, events, on_event = _lab(
        [QC, CLUSTER], critic={"issues": ["missing per-cluster table"], "revised_agenda": [QC, CLUSTER, added]},
        final=[QC, CLUSTER, added], match=json.dumps({"unavailable": [{"step": 3}]}),
        config=LabConfig(plan_review=True, auto_select_skill=False, mode="single"))
    asked, complete = [], lab._complete_fn
    lab._complete_fn = lambda messages: (asked.append(messages), complete(messages))[1]
    result = lab.run("QC and cluster these cells", on_event)
    assert added not in briefed and briefed == [QC, CLUSTER]
    assert [e["source"] for e in events if e.get("type") == "steps_unavailable"] == ["plan_review"]
    assert [r["step"] for r in result.unavailable_steps] == [added]
    review = next(m for m in asked if "reviewing a DRAFT analysis plan" in m[0]["content"])
    assert "runs no code the AI writes" in json.loads(review[-1]["content"])["model_written_code"]


def test_a_revision_in_chat_cannot_add_a_code_step(library):
    """The reviewer asks for a step the PI can only write as code: it is held on the next card."""
    revised = [QC, SCORE_CODE]

    def extra(system, messages):
        if "revising ONE step" in system:
            return "not a patch"            # the patch cannot express it: a whole redraft follows
        if "Principal Investigator of a bioinformatics lab" in system and "asked for these changes" in \
                messages[-1]["content"]:
            return json.dumps({"agenda": revised})
        return None
    lab, briefed, seen, events, on_event = _lab([QC], extra=extra, match=json.dumps(
        {"unavailable": [{"step": 2, "closest_skill": SIGNATURE}]}))
    replies = iter([{"action": "revise", "feedback": "Add a step that scores the interferon signature"},
                    {"action": "approve"}])
    shown = []

    def review(kind, payload):
        shown.append(list(payload))
        return next(replies)
    result = lab.run("QC these cells", on_event, review)
    assert shown == [[QC], [QC]], "the revised plan is shown without the code step"
    assert briefed == [QC]
    assert [e["source"] for e in events if e.get("type") == "steps_unavailable"] == ["revision"]
    assert result.unavailable_steps[0]["step"] == SCORE_CODE


def test_a_saved_plan_shown_again_holds_its_code_step(library):
    saved = PlanDoc("QC", (PlanStep("s1", QC, tool="mcp_run_scanpy_qc"),
                           PlanStep("s2", SCORE_CODE, tool="run_code")))
    lab, briefed, seen, events, on_event = _lab([QC])
    shown = []

    def review(kind, payload):
        shown.append(list(payload))
        return {"action": "approve"}
    lab.run("QC", on_event, review, saved_plan=saved, saved_plan_decision=None)
    assert shown == [[QC]] and briefed == [QC]
    assert [e["source"] for e in events if e.get("type") == "steps_unavailable"] == ["saved_plan"]


def test_the_held_steps_survive_a_saved_plan_into_the_approved_run(library):
    """Plan mode saves the card and ends; approving it starts a new lab, whose reply still names the
    step that was not available."""
    lab, briefed, seen, events, on_event = _lab([QC, SCORE])

    class Saved(Exception):
        pass

    def review(kind, payload):
        raise Saved
    with pytest.raises(Saved):
        lab.run("QC, then score the ISG signature", on_event, review)
    context = json.loads(json.dumps(lab.plan_context))
    approved = PlanDoc("q", (PlanStep("s1", QC, tool="mcp_run_scanpy_qc"),))
    again, briefed2, _, _, on_event2 = _lab([QC])
    result = again.run("QC, then score the ISG signature", on_event2, approved_plan=approved,
                       plan_context=context)
    assert briefed2 == [QC]
    assert [r["step"] for r in result.unavailable_steps] == [SCORE]
    assert result.unavailable_steps[0]["skill"] == SIGNATURE


def test_an_approved_saved_plan_with_a_code_step_is_refused(library):
    approved = PlanDoc("QC", (PlanStep("s1", QC, tool="mcp_run_scanpy_qc"),
                              PlanStep("s2", SCORE_CODE, tool="run_code")))
    lab, briefed, seen, events, on_event = _lab([QC])
    with pytest.raises(PlanMigrationRequired, match="step 2 .*needs code the AI writes"):
        lab.run("QC", on_event, approved_plan=approved)
    assert briefed == []


def test_a_resumed_run_does_not_rerun_a_code_step(library):
    plan = PlanDoc("QC", (PlanStep("s1", QC, tool="mcp_run_scanpy_qc"),
                          PlanStep("s2", SCORE_CODE, tool="run_code")))
    lab, briefed, seen, events, on_event = _lab([QC])
    resume = ResumeState(agenda=[QC, SCORE_CODE], prior_rounds=[], from_step_index=1,
                         approved_plan=plan, redo_indices=frozenset({1}))
    with pytest.raises(PlanMigrationRequired):
        lab.run("QC", on_event, resume=resume)
    assert briefed == []


def test_a_retry_after_the_critic_is_never_a_code_alternative(library):
    lab, *_ = _lab([QC], extra=lambda system, m: json.dumps([
        "Recompute the table yourself with `run_code`", "Re-run `mcp_run_scanpy_qc` with max_pct_mt 25"])
        if "A single step in a research plan FAILED" in system else None)
    from aiscientist.agents.dag import TaskNode
    options = lab._propose_alternatives("QC", TaskNode(id="n1", goal=QC), "no result", [], lambda e: None)
    assert options == ["Re-run `mcp_run_scanpy_qc` with max_pct_mt 25"]


def test_exploration_does_not_propose_a_code_step():
    assert code_gate.code_step("**Audit** — Check the ambient fraction with `run_code`.")
    assert code_gate.code_step("**Audit** — Check the ambient fraction per cluster.")
    assert not code_gate.code_step(QC)


def test_exploration_turns_drop_code_steps(library, monkeypatch):
    explore = json.dumps({"hypotheses": [{"statement": "H", "prediction": "p", "test": "t"}],
                          "new_steps": [{"step": "**Ambient audit** — Re-derive ambient RNA with `run_code`.",
                                         "hypothesis": "H"}]})
    lab, briefed, seen, events, on_event = _lab(
        [QC], extra=lambda system, m: explore if "decide whether its result opened" in system else None,
        config=LabConfig(plan_review=False, auto_select_skill=False, mode="single", hypothesis_driven=True,
                         new_steps_need_approval=False))
    monkeypatch.setattr(lab, "_explores_after", lambda step: True)
    added = lab._explore_after_step("QC", QC, rl.CriticVerdict("accept", 0.9, "ok"),
                                    {"final_answer": "x" * 100, "steps": []}, [QC], [], on_event)
    assert added == []
    assert any(e.get("reason") == "model_written_code" for e in events if e.get("type") == "steps_pruned")


def test_a_campaigns_next_cycle_holds_its_code_steps(library):
    nxt = json.dumps({"continue": True, "reason": "more", "agenda": [CLUSTER, SCORE_CODE]})
    lab, briefed, seen, events, on_event = _lab(
        [QC], extra=lambda system, m: nxt if "deciding whether to run ANOTHER cycle" in system else None,
        match=json.dumps({"unavailable": [{"step": 2, "closest_skill": SIGNATURE}]}))
    steps, _ = lab._plan_next_cycle("QC", [], 2, on_event)
    assert steps == [CLUSTER]
    assert [e["source"] for e in events if e.get("type") == "steps_unavailable"] == ["next_cycle"]


def test_the_pi_is_told_that_code_is_off(library):
    lab, briefed, seen, events, on_event = _lab([QC])
    captured = []
    lab_complete = lab._complete_fn

    def spy(messages):
        captured.append(messages)
        return lab_complete(messages)
    lab._complete_fn = spy
    lab.run("QC these cells", on_event)
    pi = next(m for m in captured if "Principal Investigator of a bioinformatics lab" in m[0]["content"])
    assert "runs no code the AI writes" in pi[-1]["content"]


# --- what the hold leaves to run --------------------------------------------------------------------

REPORT = "**Written report** — Write a PDF report of this run with `write_report`."


@pytest.mark.parametrize("switch", ["off", "on"])
@pytest.mark.parametrize("plan_mode", [False, True])
def test_a_report_only_plan_runs_as_it_always_has(library, monkeypatch, switch, plan_mode):
    """A plan of the report step alone holds no step: it is written, never "not available"."""
    if switch == "on":
        monkeypatch.setenv(code_policy.ENV_VAR, "1")
    lab, briefed, seen, events, on_event = _lab([REPORT])
    shown = []

    def review(kind, payload):
        shown.append(list(payload))
        return {"action": "approve"}
    result = lab.run("Write a written report of this run as PDF", on_event, review if plan_mode else None)
    types = [e.get("type") for e in events]
    assert "plan_not_available" not in types and "steps_unavailable" not in types
    assert "report_step" in types and result.final_answer and not result.unavailable_steps
    assert shown == ([[REPORT]] if plan_mode else [])


def test_a_plan_whose_hold_leaves_only_the_report_runs_nothing(library):
    lab, briefed, seen, events, on_event = _lab([VELOCITY, REPORT], match=NO_MATCH)
    result = lab.run("Infer RNA velocity and write it up", on_event)
    assert briefed == [] and result.rounds == []
    assert any(e.get("type") == "plan_not_available" for e in events)
    assert [r["step"] for r in result.unavailable_steps] == [VELOCITY]


# --- steps that only read: the HPC3 lookups -----------------------------------------------------------

READ_TABLE = ("**Cells per major class** — Read `summary_majorclass.tsv` with `read_text` and list the "
              "classes by cell count.")


def test_the_shell_tool_names_are_the_ones_the_shell_builds():
    from aiscientist.hpc import shell as hs
    assert _names(hs.hpc_shell_catalog(_shell())) == set(hs.SHELL_TOOLS)
    assert set(hs.LOOKUP_TOOLS) <= set(hs.SHELL_TOOLS)


def test_a_step_that_reads_a_result_file_with_a_lookup_is_covered(library):
    """The lookups are built per HPC3 session, after the plan is checked; a step that names one is
    still that tool's step, not a step that needs code."""
    from aiscientist.hpc.shell import LOOKUP_TOOLS
    from aiscientist.agents.research_lab.tooling import _primary_tool
    assert _primary_tool(READ_TABLE) == "read_text" and not code_gate.needs_code(READ_TABLE)
    for name in LOOKUP_TOOLS:
        assert not code_gate.needs_code(f"**Look** — Use `{name}` on the run's result folder."), name
    lab, briefed, seen, events, on_event = _lab([QC, READ_TABLE], config=LabConfig(
        plan_review=False, auto_select_skill=False, mode="single", plan_yaml=True))

    def review(kind, payload):
        return {"action": "approve"}
    result = lab.run("QC, then list the cells per major class", on_event, review)
    assert briefed == [QC, READ_TABLE] and not result.unavailable_steps
    assert lab._approved_plan.step_for(READ_TABLE).tool == "read_text", "the plan document's step"


# --- the held steps are the current plan's ---------------------------------------------------------------

def _revising(lab_agenda, redraft, *, match):
    """A lab whose PI drafts ``lab_agenda`` and, asked for changes, redrafts the whole plan as
    ``redraft`` (no one-step patch)."""
    def extra(system, messages):
        if "revising ONE step" in system:
            return "not a patch"
        if "Principal Investigator of a bioinformatics lab" in system and "asked for these changes" in \
                messages[-1]["content"]:
            return json.dumps({"agenda": redraft})
        return None
    return _lab(lab_agenda, extra=extra, match=match)


def _approve_after(reply):
    replies = iter([reply, {"action": "approve"}])
    return lambda kind, payload: next(replies)


def test_a_redraft_without_the_held_step_takes_it_off(library):
    lab, briefed, seen, events, on_event = _revising([QC, VELOCITY], [QC, CLUSTER], match=json.dumps(
        {"unavailable": [{"step": 2}]}))
    result = lab.run("QC, then RNA velocity", on_event,
                     _approve_after({"action": "revise", "feedback": "Drop the velocity, cluster instead"}))
    assert briefed == [QC, CLUSTER]
    assert result.unavailable_steps == [], "the approved plan holds no step"
    last = [e for e in events if e.get("type") == "steps_unavailable"][-1]
    assert last["all"] == [] and last["source"] == "revision", "the card and the record are told"


def test_a_reworded_held_step_is_listed_once(library):
    reworded = VELOCITY.replace("Infer RNA velocity trajectories", "Estimate RNA velocity")
    lab, briefed, seen, events, on_event = _revising([QC, VELOCITY], [QC, reworded], match=json.dumps(
        {"unavailable": [{"step": 2}]}))
    result = lab.run("QC, then RNA velocity", on_event,
                     _approve_after({"action": "revise", "feedback": "Use the spliced counts only"}))
    assert [r["step"] for r in result.unavailable_steps] == [reworded]


def test_a_question_about_the_plan_keeps_its_held_steps(library):
    lab, briefed, seen, events, on_event = _lab([QC, VELOCITY], match=json.dumps(
        {"unavailable": [{"step": 2}]}), extra=lambda system, m: "Because it is the first step."
        if "answering a researcher's question about the analysis plan" in system else None)
    result = lab.run("QC, then RNA velocity", on_event,
                     _approve_after({"action": "revise", "feedback": "Why is QC the first step?"}))
    assert briefed == [QC]
    assert [r["step"] for r in result.unavailable_steps] == [VELOCITY]
    assert len([e for e in events if e.get("type") == "steps_unavailable"]) == 1, "told once"


# --- the switch on restores the earlier behaviour --------------------------------------------------------

def test_with_the_switch_on_a_code_step_runs_and_nothing_is_held(code_on, library):
    lab, briefed, seen, events, on_event = _lab([QC, SCORE_CODE])
    result = lab.run("QC, then score the ISG signature", on_event)
    assert briefed == [QC, SCORE_CODE]
    assert not _held(events) and result.unavailable_steps == []
    assert not any("no tested tool performs on this deployment" in s for s in seen)
    plan_and_review = code_gate.hold_code_steps(lab, [QC, SCORE_CODE], on_event, source="final")
    assert plan_and_review == [QC, SCORE_CODE]
    code_gate.refuse_code_steps([QC, SCORE_CODE], None)   # no refusal


def test_with_the_switch_on_the_untested_code_notice_is_back(code_on):
    from aiscientist.agents.code_provenance import run_provenance, untested_notice
    rnd = {"step_index": 1, "step": SCORE_CODE, "verdict": {"verdict": "accept"},
           "scientist_result": {"steps": [{"tool": "run_code", "ok": True, "args": {"code": "x=1"},
                                           "result": {"status": "ok", "tables": ["tables/score.csv"]}},
                                          {"tool": "finish", "ok": True}]}}
    assert untested_notice(run_provenance([rnd]), recorded=True)


def test_skill_induction_is_off_with_the_switch(tmp_path, monkeypatch):
    from aiscientist.agents import skill_induction
    monkeypatch.setattr(skill_induction, "candidates", lambda rounds: pytest.fail("induction ran"))
    lab, *_ = _lab([QC], config=LabConfig(plan_review=False, auto_select_skill=False, mode="single",
                                          skill_induction=True, induced_skills_dir=str(tmp_path)))
    assert lab._induce_skills([], lambda e: None) == []


# --- preset pipelines ----------------------------------------------------------------------------------

def test_each_preset_pipeline_runs_with_tools_alone_or_says_which_steps_are_not_available(library):
    """Every preset pipeline names run_code. Its tool steps are kept; its code steps are held."""
    from aiscientist.agents.preset_pipelines import PIPELINES
    assert PIPELINES, "the preset pipelines are in the repository"
    for key, pipeline in PIPELINES.items():
        steps = [f"**Step {i}** — Run `{tool}` on the dataset." for i, tool in enumerate(pipeline.tools, 1)]
        lab, briefed, seen, events, on_event = _lab(steps, match="{}")
        kept = code_gate.hold_code_steps(lab, steps, on_event, source="draft")
        held = [r["step"] for r in _held(events)]
        for step, tool in zip(steps, pipeline.tools):
            assert (step in held) == (tool in code_policy.WITHHELD_TOOLS), (key, tool)
            assert (step in kept) == (tool not in code_policy.WITHHELD_TOOLS), (key, tool)


def test_the_router_does_not_mark_every_protocol_unavailable_for_run_code():
    from aiscientist.agents.preset_pipelines import PIPELINES, select_pipeline
    seen = []

    def complete(messages):
        seen.append(messages[-1]["content"])
        return "none"
    usable = frozenset(t for p in PIPELINES.values() for t in p.tools) - code_policy.WITHHELD_TOOLS
    select_pipeline(complete, "Score a signature", "", tuple(PIPELINES.values()), available_tools=usable)
    assert "NOT AVAILABLE" not in seen[0]


# --- skill requests ----------------------------------------------------------------------------------

def test_a_held_step_is_recorded_with_the_closest_skill(tmp_path, monkeypatch):
    from aiscientist.gateway import skill_requests
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path))
    rows = [NotAvailable(step=SCORE, skill=SIGNATURE, does="score cells").to_dict(),
            NotAvailable(step=VELOCITY).to_dict()]
    rec = skill_requests.build_record(run_id="run1", account="u", question="Score and velocity",
                                      provenance={}, not_available=rows)
    assert rec["outcome"]["not_available"] == 2
    assert [r["step"] for r in rec["not_available"]] == [SCORE, VELOCITY]
    assert skill_requests.fallback_summary(rec)["closest_skill"] == SIGNATURE
    # The model's summary keeps the agent's closest skill when it names none itself.
    summary, by = skill_requests.summarize(
        rec, lambda messages, role="classify": json.dumps({"category": "gene signature scoring"}),
        [SIGNATURE])
    assert by == "model" and summary["closest_skill"] == SIGNATURE
    skill_requests.save({**rec, "summary": summary})
    row = skill_requests.listing()[0]
    assert row["not_available_steps"] == [SCORE, VELOCITY] and row["closest_skill"] == SIGNATURE


def test_the_feed_shows_each_held_step():
    pytest.importorskip("fastapi")
    from aiscientist.gateway.app import _lab_event_to_chat
    out = _lab_event_to_chat({"type": "steps_unavailable", "recorded": True,
                              "steps": [NotAvailable(step=VELOCITY).to_dict()]})
    assert [p["text"] for p in out] == ["🚫 " + NotAvailable(step=VELOCITY).line()]


def test_unknown_names_are_not_called_improvised_with_code_off():
    pytest.importorskip("fastapi")
    from aiscientist.gateway.app import _lab_event_to_chat
    out = _lab_event_to_chat({"type": "plan_tooling",
                              "findings": [{"kind": "unknown_tool", "tool": "run_velocity", "detail": ""}]})
    assert "IMPROVISED" not in out[0]["text"] and "no tested tool runs this step" in out[0]["text"]



def test_every_registered_mcp_tool_and_the_literature_and_report_steps_are_covered():
    """CellQC, scRNA, Atlas, variant, phenotype, literature and RetiGene requests plan their MCP
    tools: none of those steps is held."""
    from aiscientist.agents.mcp_bridge import configured_tool_names
    names = sorted(configured_tool_names())
    assert {"mcp_run_cellqc", "mcp_run_clustering", "mcp_annotate_variants", "mcp_run_lirical"} <= set(names)
    for name in names:
        assert not code_gate.needs_code(f"**Step** — Run `{name}` on the bound input."), name
    assert not code_gate.needs_code("**Literature grounding** — Search the literature with `deep_literature`.")
    assert not code_gate.needs_code("**Report** — Write the report with `write_report`.")
