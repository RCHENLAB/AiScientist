"""A plan that cannot be read stops the run; the plan's code steps get the skills that fit them, and
a step runs with its skill's guidance and code in hand.

Prod run 3ed7676de128 (2026-10-23) is the case: the PI's reply (Claude Haiku 5.5, 8,936 tokens,
under its ceiling) did not parse, the researcher's question became the only step of a fallback plan,
and the Scientist wrote its own signature scoring while the vetted `score_signature` skill sat one
read away. 4 of 47 runs on the Eye Server had ever read a skill. Scripted models throughout.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest

pytest.importorskip("yaml")

from aiscientist.agents import research_lab as rl  # noqa: E402
from aiscientist.agents import skills as skills_mod  # noqa: E402
from aiscientist.agents import skills_registry as registry  # noqa: E402
from aiscientist.agents.plan_doc import (  # noqa: E402
    PlanDoc,
    PlanStep,
    apply_edit,
    apply_ops,
    check_plan,
    parse_plan_yaml,
    sync_plan,
)
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness  # noqa: E402
from aiscientist.agents.research_lab import LabConfig, ResearchLab  # noqa: E402
from aiscientist.agents.skills import Skill, refresh_skills  # noqa: E402

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
DE_MD = """---
name: compare-two-groups-with-scanpy
description: Differential expression between two named groups of cells with scanpy rank_genes_groups (Wilcoxon). Use when a question compares exactly two groups.
metadata:
  category: single-cell
  aliases: "pairwise_de"
---
# Compare two groups

Rank genes between the two groups with a Wilcoxon test.
"""

SCORE_STEP = ("**Score the ISG signature** — Score every cell for the interferon-stimulated genes and "
              "compare the score between DDX41 and WT within each major cell class.")
NAMED_STEP = ("**Score the ISG signature** — Adapt the reference template `score_signature.py` via "
              "`run_code` and summarise the score per class.")
CLUSTER_STEP = "**Clustering** — Group the cells with `run_clustering` at resolution 0.5."
ACCEPT = json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A skill library of two skills in the registry layout, loaded in place of the real one."""
    root = tmp_path / "skills"
    for name, md, code in ((SIGNATURE, SIG_MD, SIG_CODE),
                           ("compare-two-groups-with-scanpy", DE_MD, "print('de')\n")):
        folder = root / "single-cell" / name
        (folder / "scripts").mkdir(parents=True)
        (folder / "SKILL.md").write_text(md, encoding="utf-8")
        (folder / "scripts" / "reference.py").write_text(code, encoding="utf-8")
    monkeypatch.setenv("AISCIENTIST_SKILLS_DIR", str(root))
    monkeypatch.delenv("AISCIENTIST_INDUCED_SKILLS_DIR", raising=False)
    lib: dict = {}
    monkeypatch.setattr(skills_mod, "SKILLS", lib)
    monkeypatch.setattr(rl, "ATOMIC_SKILLS", lib)        # research_lab binds the dict at import
    monkeypatch.setattr(skills_mod, "ALL_SKILLS", {})
    monkeypatch.setattr(skills_mod, "_SIGNATURE", ())
    monkeypatch.setattr(skills_mod, "_REGISTERED", {})
    refresh_skills()
    assert set(lib) == {SIGNATURE, "compare-two-groups-with-scanpy"}
    return lib


# --- the plan document carries the skill ---------------------------------------------------------------

def test_a_skill_survives_the_yaml_round_trip_and_a_sync():
    doc = PlanDoc("r", (PlanStep("s1", SCORE_STEP, tool="run_code", skill=SIGNATURE),))
    text = doc.to_yaml()
    assert f"    skill: {SIGNATURE}" in text
    assert parse_plan_yaml(text, request="r").steps[0].skill == SIGNATURE
    assert doc.to_public()[0]["skill"] == SIGNATURE
    synced, fresh = sync_plan(doc, [SCORE_STEP, CLUSTER_STEP], request="r",
                              tool_of=lambda t: None, runs_on_of=lambda t: "hpc:analysis")
    assert synced.steps[0].skill == SIGNATURE, "an unchanged step keeps its skill"
    assert fresh == [1] and synced.steps[1].skill is None


def test_check_plan_resolves_an_old_name_and_refuses_an_unknown_one(library):
    doc = PlanDoc("r", (PlanStep("s1", SCORE_STEP, tool="run_code", skill="score_signature"),
                        PlanStep("s2", CLUSTER_STEP, tool="run_code", skill="no-such-skill")))
    out, findings = check_plan(doc, {}, skill_name=rl._skill_name)
    assert out.steps[0].skill == SIGNATURE
    assert [(f["kind"], f["param"]) for f in findings] == [("unknown_skill", "no-such-skill")]
    assert "not in this deployment's skill library" in rl._PLAN_FINDING_WORDS["unknown_skill"].format(**findings[0])


def test_taking_a_skill_off_is_an_edit_and_other_changes_keep_it():
    before = PlanDoc("r", (PlanStep("s1", SCORE_STEP, tool="run_code", skill=SIGNATURE),))
    off = PlanDoc("r", (PlanStep("s1", SCORE_STEP, tool="run_code"),))
    doc, changes, _notes = apply_edit(before, off)
    assert doc.steps[0].skill is None
    assert changes == [{"kind": "skill", "step": 1, "id": "s1", "before": SIGNATURE, "after": ""}]
    same, none, _ = apply_edit(before, before)
    assert none == [] and same.steps[0] is before.steps[0]
    assert apply_ops(before, [{"op": "change", "step": "s1", "skill": None}]).steps[0].skill is None
    assert apply_ops(before, [{"op": "change", "step": "s1", "title": "Score it"}]).steps[0].skill == SIGNATURE
    assert "took the skill off step 1" in rl._hand_edit_note(changes)


# --- a scripted lab ------------------------------------------------------------------------------------

def _lab(tmp_path, *, plan_replies, match_reply="[]", record=None, plan_yaml=True, codes=("print(1)",),
         verdicts=(), run_code_tool=None):
    """A lab whose PI returns ``plan_replies`` in order and whose matcher returns ``match_reply``.
    Each attempt of a step runs ``codes`` with run_code, one call each, then finishes; the Critic
    returns ``verdicts`` in order, then accepts. ``run_code_tool`` replaces the stand-in run_code.
    ``record`` collects the Scientist's briefs, the Critic's payloads and the tool calls."""
    record = record if record is not None else {}
    replies = list(plan_replies)
    critic_replies = list(verdicts)

    def complete(messages):
        system = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in system:
            record.setdefault("plan_calls", []).append(messages)
            return replies.pop(0) if replies else "no plan"
        if "match the steps of an analysis plan" in system:
            record.setdefault("match_calls", []).append(messages[-1]["content"])
            return match_reply
        if "filling in a form" in system:
            return "[]"
        if "rigorous scientific Critic" in system:
            record.setdefault("critic", []).append(json.loads(messages[-1]["content"]))
            return critic_replies.pop(0) if critic_replies else ACCEPT
        return "FINAL REPORT"

    def run_code(args, _ctx):
        record.setdefault("tools", []).append(("run_code", dict(args)))
        return {"status": "ok", "stdout": "scored"}

    def chat(messages, _tools):
        done = sum(m.get("role") == "tool" for m in messages)
        if not done:
            record.setdefault("briefs", []).append(
                next(m["content"] for m in messages if m.get("role") == "user"))
        if done < len(codes):
            return {"content": "", "tool_calls": [{"id": f"t{done}", "type": "function", "function": {
                "name": "run_code", "arguments": json.dumps({"code": codes[done]})}}]}
        return {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
            "name": "finish", "arguments": json.dumps({"answer": "Scored every cell."})}}]}

    catalog = [run_code_tool or HarnessTool("run_code", "run Python", {"type": "object", "properties": {
        "code": {"type": "string"}}}, run_code),
               HarnessTool("run_clustering", "cluster", {"type": "object", "properties": {
                   "resolution": {"type": "number"}}}, lambda a, c: {"status": "ok"})]
    workspace = tmp_path / "run"
    workspace.mkdir(exist_ok=True)
    return ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m", workspace=workspace),
                       LabConfig(plan_yaml=plan_yaml, plan_review=False, auto_select_skill=False,
                                 mode="single", planner="linear"),
                       complete_fn=complete,
                       scientist=ResearchHarness(catalog=catalog, chat_fn=chat)), record, workspace


def _agenda(*steps):
    return json.dumps({"breadth": "specific", "agenda": list(steps), "later": []})


def _run(lab, decide=None, *, review=True):
    events: list[dict] = []
    seen: list[list[str]] = []

    def plan_review(_kind, payload):
        seen.append(list(payload))
        doc = next((e for e in reversed(events) if e.get("type") == "plan_doc"), None)
        return (decide or (lambda *_a: {"action": "approve"}))(len(seen), payload, doc)

    result = lab.run("Score the ISG signature in DDX41 vs WT", events.append,
                     plan_review if review else None)
    return result, events, seen


def _of(events, kind):
    return [e for e in events if e.get("type") == kind]


# --- a plan that cannot be read ------------------------------------------------------------------------

def test_a_reply_that_is_not_a_plan_is_asked_for_again_and_every_reply_is_kept(tmp_path, library):
    lab, record, workspace = _lab(tmp_path, plan_replies=["Here is my plan. Step one: score the cells.",
                                                          _agenda(SCORE_STEP)])
    result, events, seen = _run(lab)
    assert seen == [[SCORE_STEP]] and result.agenda == [SCORE_STEP]
    assert len(record["plan_calls"]) == 2
    retry = record["plan_calls"][1]
    assert retry[-2] == {"role": "assistant", "content": "Here is my plan. Step one: score the cells."}
    assert "could not be read as the plan" in retry[-1]["content"]
    assert _of(events, "plan_reformat") and not _of(events, "plan_unparsed")
    replies = sorted((workspace / "artifacts" / "process" / "plan_replies").glob("reply-*.txt"))
    assert [p.name for p in replies] == ["reply-01.txt", "reply-02.txt"]
    assert replies[0].read_text() == "Here is my plan. Step one: score the cells."


@pytest.mark.parametrize("review", [True, False])
def test_no_plan_runs_nothing_instead_of_the_question_as_a_step(tmp_path, library, review):
    lab, record, _ws = _lab(tmp_path, plan_replies=["prose", "still prose"])
    result, events, seen = _run(lab, review=review)
    assert seen == [], "no plan card is shown"
    assert result.agenda == [] and result.rounds == []
    assert result.final_answer == rl._PLAN_FAILED_MESSAGE
    assert _of(events, "plan_unparsed") and _of(events, "plan_failed")
    assert not _of(events, "pi_agenda"), "the question never becomes a plan"
    assert "tools" not in record and "briefs" not in record, "nothing ran"


def test_a_revision_that_cannot_be_read_keeps_the_plan_the_researcher_had(tmp_path, library):
    lab, _record, _ws = _lab(tmp_path, plan_replies=[_agenda(SCORE_STEP), "prose", "prose"])

    def decide(n, _payload, _doc):
        return ({"action": "revise", "feedback": "Also add a figure of the score per class."}
                if n == 1 else {"action": "approve"})

    result, events, seen = _run(lab, decide)
    assert _of(events, "plan_revision_failed")
    assert seen[1] == seen[0] == [SCORE_STEP], "the plan is re-presented unchanged"
    assert result.agenda == [SCORE_STEP] and result.rounds, "the kept plan still runs"


# --- skills matched to the plan's steps -----------------------------------------------------------------

def test_a_code_step_gets_the_skill_that_fits_and_runs_with_it(tmp_path, library):
    lab, record, workspace = _lab(tmp_path, plan_replies=[_agenda(SCORE_STEP)],
                                  match_reply=json.dumps([{"step": 1, "skill": "score_signature"}]))
    result, events, _seen = _run(lab)

    # The plan: matched by its description (the old name in the reply resolves), shown on the card.
    assert "score-gene-signatures-with-scanpy: Score every cell" in record["match_calls"][0]
    assert _of(events, "plan_doc")[0]["steps"][0]["skill"] == SIGNATURE
    assert _of(events, "plan_skills")[0]["assigned"][0]["skill"] == SIGNATURE
    assert f"skill: {SIGNATURE}" in lab.approved_plan_yaml

    # The step: the skill's guidance and code are in the brief, not one read away.
    brief = record["briefs"][0]
    assert f"The approved plan assigns the skill `{SIGNATURE}`" in brief
    assert "sc.tl.score_genes(adata, SIGNATURE, ctrl_size=50)" in brief
    assert "Score with `sc.tl.score_genes` against 50 control genes" in brief
    assert _of(events, "step_skill")[0]["skill"] == SIGNATURE

    # The record: the run's manifest names the skill, and the Critic checks the code against it.
    manifest = json.loads((workspace / "manifest.json").read_text())
    assert manifest["skills"] == [{"name": SIGNATURE, "version": "local", "registry_release": None}]
    assigned = record["critic"][0]["ASSIGNED_SKILL"]
    assert assigned["name"] == SIGNATURE and assigned["skill_functions"] == ["scanpy.tl.score_genes"]
    assert assigned["code_it_ran"] == [{"tool_call": 1, "tool": "run_code", "ok": True, "chars": 8,
                                        "shown": "full", "calls_skill_functions": [], "code": "print(1)"}]
    assert "used the skill's method" in record["critic"][0]["assigned_skill_note"]
    assert result.rounds


def test_a_code_step_gets_no_settings_from_the_binder(tmp_path, library):
    """Seen on the served model (DSV4 on HPC3): the binder pinned run_code's `skill` argument as a
    setting of the step. A code step's skill is its `skill:`; it has no settings."""
    lab, record, _ws = _lab(tmp_path, plan_replies=[_agenda(NAMED_STEP)])
    original = lab._complete_fn

    def complete(messages):
        if "filling in a form" in messages[0]["content"]:
            record.setdefault("bind_calls", []).append(messages)
            return json.dumps([{"step": 1, "settings": {"code": "print(1)", "skill": SIGNATURE}}])
        return original(messages)

    lab._complete_fn = complete
    _result, events, _seen = _run(lab)
    step = _of(events, "plan_doc")[0]["steps"][0]
    assert step["skill"] == SIGNATURE and "settings" not in step
    assert "bind_calls" not in record, "a code step is not sent to the binder"


def test_a_step_that_names_a_skill_by_its_old_name_gets_it_without_a_model_call(tmp_path, library):
    lab, record, _ws = _lab(tmp_path, plan_replies=[_agenda(NAMED_STEP)])
    _result, events, _seen = _run(lab)
    assert "match_calls" not in record
    assert _of(events, "plan_doc")[0]["steps"][0]["skill"] == SIGNATURE


def test_a_pipeline_tool_step_and_an_unknown_name_get_no_skill(tmp_path, library):
    lab, record, workspace = _lab(tmp_path, plan_replies=[_agenda(CLUSTER_STEP, SCORE_STEP)],
                                  match_reply=json.dumps([{"step": 2, "skill": "made-up-skill"}]))
    _result, events, _seen = _run(lab)
    assert "1. " not in record["match_calls"][0], "the run_clustering step is not offered for matching"
    assert [s.get("skill") for s in _of(events, "plan_doc")[0]["steps"]] == [None, None]
    assert not _of(events, "plan_skills")
    assert json.loads((workspace / "manifest.json").read_text())["skills"] == []


def test_a_skill_taken_off_the_card_is_not_used(tmp_path, library):
    lab, record, workspace = _lab(tmp_path, plan_replies=[_agenda(SCORE_STEP)],
                                  match_reply=json.dumps([{"step": 1, "skill": SIGNATURE}]))

    def decide(n, _payload, doc):
        if n == 1:
            return {"action": "edit", "plan_yaml": doc["yaml"].replace(f"    skill: {SIGNATURE}\n", "")}
        return {"action": "approve"}

    _result, events, _seen = _run(lab, decide)
    assert _of(events, "plan_edited")[0]["changes"] == [
        {"kind": "skill", "step": 1, "id": "s1", "before": SIGNATURE, "after": ""}]
    assert "The approved plan assigns the skill" not in record["briefs"][0]
    assert json.loads((workspace / "manifest.json").read_text())["skills"] == []
    assert "ASSIGNED_SKILL" not in record["critic"][0]


COMPARE_STEP = ("**Per-class comparison** — Within each major class, summarise the ISG score by arm as "
                "n, mean, median and IQR with `run_code`.")
FIGURE_STEP = "**Score figures** — Plot the ISG score per class and arm as violins with `run_code`."


def test_a_skill_goes_to_the_step_that_runs_it_not_to_the_steps_that_use_what_it_made(tmp_path, library):
    """Run 74873590ca7b: the model gave the scoring skill to the scoring step and to the comparison
    and figure steps after it; the figure step's Critic then asked why it had not scored again."""
    every = json.dumps([{"step": n, "skill": SIGNATURE} for n in (1, 2, 3)])
    lab, _record, _ws = _lab(tmp_path, plan_replies=[_agenda(SCORE_STEP, COMPARE_STEP, FIGURE_STEP)],
                             match_reply=every)
    _result, events, _seen = _run(lab)
    assert [s.get("skill") for s in _of(events, "plan_doc")[0]["steps"]] == [SIGNATURE, None, None]
    assert _of(events, "plan_skill_repeats")[0]["dropped"] == [
        {"step": 2, "skill": SIGNATURE, "kept_on": 1}, {"step": 3, "skill": SIGNATURE, "kept_on": 1}]
    assert "does not run the skill again" in rl._PLAN_SKILLS_SYSTEM
    assert len(_of(events, "step_skill")) == 1


def test_a_step_whose_own_text_names_the_skill_keeps_it_beside_another(tmp_path, library):
    lab, _record, _ws = _lab(tmp_path, plan_replies=[_agenda(SCORE_STEP, NAMED_STEP)],
                             match_reply=json.dumps([{"step": 1, "skill": SIGNATURE}]))
    _result, events, _seen = _run(lab)
    assert [s.get("skill") for s in _of(events, "plan_doc")[0]["steps"]] == [SIGNATURE, SIGNATURE]
    assert not _of(events, "plan_skill_repeats")


def _scoring_script() -> str:
    """Shaped like run 74873590ca7b's scoring script: 3,500 characters, with the skill's call after
    the 2,000th, where the Critic's 2,000-character cut used to end it."""
    head = ["import os", "import numpy as np", "import scanpy as sc",
            "adata = sc.read_h5ad(os.path.join(os.environ['AISCIENTIST_WORK'], 'adata_qc.h5ad'))",
            "GENES = ['Isg15', 'Ifit1', 'Ifit3', 'Irf7', 'Stat1', 'Bst2', 'Usp18', 'Rtp4']"]
    checks = [f"detected_{i} = float((adata.raw[:, GENES[{i % 8}]].X > 0).mean())  # nuclei expressing "
              f"signature gene {i % 8}" for i in range(22)]
    call = ["assert len(GENES) / 8 >= 0.8, 'NOT SCORED'",
            "sc.tl.score_genes(",
            "    adata, gene_list=GENES, score_name='score_ISG',",
            "    ctrl_size=50, random_state=0, use_raw=True,",
            ")"]
    tail = [f"summary_{i} = adata.obs.groupby(['majorclass', 'sampleid'])['score_ISG'].quantile({i / 10})"
            for i in range(1, 12)]
    tail.append("adata.write(os.path.join(os.environ['AISCIENTIST_WORK'], 'adata_scored.h5ad'))")
    return "\n".join(head + checks + call + tail)


LIST_FILES = "print(os.listdir(os.environ['AISCIENTIST_WORK']))"


def test_the_critic_reads_each_script_of_a_skill_step_whole_and_sees_where_it_calls_the_skill(
        tmp_path, library):
    script = _scoring_script()
    assert script.index("sc.tl.score_genes(") > 2_000 and len(script) > 3_000
    lab, record, _ws = _lab(tmp_path, plan_replies=[_agenda(SCORE_STEP)],
                            match_reply=json.dumps([{"step": 1, "skill": SIGNATURE}]),
                            codes=(LIST_FILES, script))
    _run(lab)
    assigned = record["critic"][0]["ASSIGNED_SKILL"]
    ran = assigned["code_it_ran"]
    assert [r["code"] for r in ran] == [LIST_FILES, script]
    assert [(r["tool_call"], r["shown"], r["chars"]) for r in ran] == [
        (1, "full", len(LIST_FILES)), (2, "full", len(script))]
    assert [r["calls_skill_functions"] for r in ran] == [[], ["scanpy.tl.score_genes"]]
    assert "scripts_not_shown" not in assigned
    note = record["critic"][0]["assigned_skill_note"]
    assert "lines that call the skill's own functions" in note and "does not have to run the skill again" in note


def test_a_skill_step_is_counted_once_by_the_code_that_ran_its_method(tmp_path, library, monkeypatch):
    """Run 74873590ca7b put 7 successes in skill_versions.sqlite3 for one use: every run_code of the
    three steps that had the skill, two of them only listing files. Here the step lists its files,
    then scores; the score fails, the Critic asks for a revision, and the second attempt scores. That
    is one use of the skill, and it worked."""
    from dataclasses import replace
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_STATS", str(tmp_path / "counts.sqlite3"))
    library[SIGNATURE] = replace(library[SIGNATURE], registry_release="v0.1.0", version="1.0.0",
                                 requirements=registry.requirements({}))
    ran: list[str] = []

    class Sandbox:
        supports_skill_policy = True

        def __call__(self, code):
            ran.append(code)
            first_score = "score_genes" in code and sum("score_genes" in c for c in ran) == 1
            return {"status": "error", "error": "KeyError: 'Isg15'"} if first_score else {"status": "ok"}

    revise = json.dumps({"verdict": "revise", "score": 0.3, "critique": "The scoring call failed."})
    lab, record, _ws = _lab(tmp_path, plan_replies=[_agenda(SCORE_STEP, COMPARE_STEP)],
                            match_reply=json.dumps([{"step": 1, "skill": SIGNATURE}]),
                            codes=("print('inspect')", SIG_CODE), verdicts=(revise,),
                            run_code_tool=rl.make_run_code_tool(Sandbox()))
    _run(lab)
    assert len(record["critic"]) == 3 and len(ran) == 6, "two attempts of step 1, one of step 2"
    with sqlite3.connect(tmp_path / "counts.sqlite3") as db:
        assert db.execute("SELECT release, name, version, success, error FROM skill_versions").fetchall() == [
            ("v0.1.0", SIGNATURE, "1.0.0", 1, 0)]


# --- the active skill belongs to its step ----------------------------------------------------------------

def test_the_active_skill_belongs_to_its_step_even_in_parallel():
    from aiscientist.agents.loop_utils import ContextThreadPoolExecutor

    a = Skill(name="a", registry_release="v0.1.0")
    b = Skill(name="b", registry_release="v0.1.0")
    ctx = HarnessContext(decisions={})
    seen: dict = {}
    both = threading.Barrier(2)

    def step(skill):
        with registry.step_scope(skill):
            both.wait(timeout=5)
            seen[skill.name] = registry.active_skill(ctx).name

    with ContextThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(step, [a, b]))
    assert seen == {"a": "a", "b": "b"}
    with registry.step_scope(None):
        assert registry.active_skill(ctx) is None, "a step with no skill runs no skill's policy"
        registry.record_use(ctx, a)
        assert registry.active_skill(ctx) is a, "a skill read during the step becomes its skill"
    with registry.step_scope(None):
        assert registry.active_skill(ctx) is None, "nothing carries over to the next step"


# --- the card --------------------------------------------------------------------------------------------

STEP_HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const sandbox = { state: {}, escapeHtml, renderMarkdown: (s) => escapeHtml(s) };
const api = vm.runInNewContext(src + "\n;({ planStepHtml, planYamlEdit })", sandbox);
const step = { id: "s1", tool: "run_code", skill: "score-gene-signatures-with-scanpy" };
const yaml = process.env.PLAN_YAML;
console.log(JSON.stringify({
  waiting: api.planStepHtml("**Score** — score cells", 1, step, false, false, { remove: true }),
  done: api.planStepHtml("**Score** — score cells", 1, step, false, false, null),
  off: api.planYamlEdit(yaml, { id: "s1", skill: null }),
}));
"""


def test_the_card_shows_the_skill_and_takes_it_off_as_one_line_of_the_document():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    import os

    doc = PlanDoc("r", (PlanStep("s1", SCORE_STEP, tool="run_code", skill=SIGNATURE),))
    source = (Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js").read_text(encoding="utf-8")
    block = source[source.index("function planStepParts"):source.index("function showPlanPanel")]
    done = subprocess.run([node, "-e", STEP_HARNESS], input=block,
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PLAN_YAML": doc.to_yaml()})
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert 'class="plan-step-skill"' in out["waiting"] and SIGNATURE in out["waiting"]
    assert "data-skill-remove" in out["waiting"]
    assert 'class="plan-step-skill"' in out["done"] and "data-skill-remove" not in out["done"], \
        "a plan that is no longer waiting shows its skill but cannot change it"
    edited = parse_plan_yaml(out["off"]["yaml"], request="r")
    assert edited.steps[0].skill is None
    assert apply_edit(doc, edited)[1] == [
        {"kind": "skill", "step": 1, "id": "s1", "before": SIGNATURE, "after": ""}]
