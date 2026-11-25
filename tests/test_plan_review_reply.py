"""A reply to a plan under review is read by the model, and the planner can look datasets up.

RCHENLAB/AiScientist#120 (item 2 of #154). In a chat with no dataset attached, the reply "look for
the datasets again and write them into the plan" came back several times with the datasets still
missing and the step holding the reply almost word for word. Two causes: the reply was sorted by
fixed word lists into ONE of question or change, and the reviser had no list of datasets and no
field to put one in. Pinned here with scripted models, in English and Chinese:

* the model reads a reply into its parts (question, change, look-up, or several), and the word lists
  are the fallback when that call fails;
* the planner is given the datasets the researcher can use (own and shared, with versions and
  files), and a step names the one it reads in its ``dataset`` field, checked against that list;
* the reviser never pastes the reply into a step, and a request it cannot carry out is refused in
  words with the plan unchanged;
* the earlier questions and answers of the review go into the next answer;
* a question never changes the plan.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("yaml")

from aiscientist.agents import plan_datasets  # noqa: E402
from aiscientist.agents.plan_doc import (  # noqa: E402
    PlanDoc, PlanError, PlanStep, apply_edit, apply_ops, check_plan, dataset_ref, parse_plan_yaml)
from aiscientist.agents.plan_reply import copies_reply, fallback_reading, read_plan_reply  # noqa: E402
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness  # noqa: E402
from aiscientist.agents.research_lab import LabConfig, ResearchLab  # noqa: E402
from plan_turns import instruction  # noqa: E402

QC = ("**QC & normalization** — Measure each cell's quality with `mcp_run_scanpy_qc` and remove cells "
      "detecting fewer than 200 genes.")
EXPR = ("**Expression in the retina** — Summarise the expression of RHO per cell type with "
        "`mcp_run_clustering` at resolution 0.5.")
AGENDA = [QC, EXPR]
LOOKUP_EN = "look for the datasets again and write them into the plan"
LOOKUP_ZH = "再去找一下数据集并写进计划"

HECA_FILES = [
    {"name": "HRCA_snRNA_allcells.h5ad",
     "facets": {"tissue": "retina", "modality": "snRNA", "n_obs": 3180000, "obs_columns": ["majorclass", "celltype"]}},
    {"name": "Lens_scRNA_allcells.h5ad", "facets": {"tissue": "lens", "modality": "scRNA"}},
]
CATALOGUE = [
    {"source": "own", "id": "11111111-1111-1111-1111-111111111111", "name": "ko_vs_wt.h5ad",
     "kind": "file", "size_bytes": 2_000_000},
    {"source": "shared", "id": "heca", "name": "HECA", "description": "Human eye cell atlas",
     "latest": "v2026.11.2", "versions": [
         {"label": "v2026.11.2", "status": "published", "files": HECA_FILES},
         {"label": "v2026.11.1", "status": "published", "files": HECA_FILES},
         {"label": "v2026.10.1", "status": "withdrawn", "files": []}]},
]
HRCA = {"name": "HECA", "version": "v2026.11.2", "file": "HRCA_snRNA_allcells.h5ad"}


# --- reading a reply --------------------------------------------------------------------------------

def _reader(parts):
    return lambda _messages: json.dumps({"parts": parts})


@pytest.mark.parametrize("reply, parts", [
    ("why is step 2 at resolution 0.5?", [{"kind": "question", "text": "why is step 2 at resolution 0.5?"}]),
    ("第2步为什么用 0.5 的分辨率？", [{"kind": "question", "text": "第2步为什么用 0.5 的分辨率？"}]),
    ("use resolution 0.8 in step 2", [{"kind": "change", "text": "use resolution 0.8 in step 2"}]),
    ("把第2步的分辨率改成 0.8", [{"kind": "change", "text": "把第2步的分辨率改成 0.8"}]),
    (LOOKUP_EN, [{"kind": "lookup", "text": "look for the datasets again"},
                 {"kind": "change", "text": "write them into the plan"}]),
    (LOOKUP_ZH, [{"kind": "lookup", "text": "再去找一下数据集"}, {"kind": "change", "text": "写进计划"}]),
    ("Which datasets does step 2 use? Use HECA instead",
     [{"kind": "question", "text": "Which datasets does step 2 use?"},
      {"kind": "change", "text": "Use HECA instead"}]),
    ("第2步用的是哪些数据集？换成 HECA",
     [{"kind": "question", "text": "第2步用的是哪些数据集？"}, {"kind": "change", "text": "换成 HECA"}]),
])
def test_the_model_reads_each_part_of_a_reply(reply, parts):
    seen = []

    def complete(messages):
        seen.append(messages)
        return json.dumps({"parts": parts})

    reading = read_plan_reply(reply, plan_text="1. QC\n2. Expression", complete=complete, system="SYS")
    assert reading.source == "model"
    assert [(p.kind, p.text) for p in reading.parts] == [(p["kind"], p["text"]) for p in parts]
    assert seen[0][0] == {"role": "system", "content": "SYS"} and reply in seen[0][-1]["content"]


@pytest.mark.parametrize("reply, kinds", [
    ("why is step 2 at resolution 0.5?", ["question"]),
    ("第2步为什么用 0.5 的分辨率？", ["question"]),
    ("use resolution 0.8 in step 2", ["change"]),
    ("把第2步的分辨率改成 0.8", ["change"]),
    (LOOKUP_EN, ["lookup", "change"]),
    (LOOKUP_ZH, ["lookup", "change"]),
    ("Which datasets does step 2 use? Use HECA instead", ["question", "change"]),
    ("第2步用的是哪些数据集？换成 HECA", ["question", "change"]),
])
def test_the_word_lists_are_the_fallback_when_the_model_cannot_read_it(reply, kinds):
    def broken(_messages):
        raise RuntimeError("model down")

    for complete in (None, broken, lambda _m: "not json", lambda _m: '{"parts": [{"kind": "wish"}]}'):
        reading = read_plan_reply(reply, plan_text="1. QC", complete=complete, system="SYS")
        assert reading.source == "words" and reading.kinds() == kinds, (reply, complete)
    assert fallback_reading(reply).kinds() == kinds


def test_a_pasted_reply_is_told_from_a_step_that_carries_it_out():
    assert copies_reply(f"**Data** — {LOOKUP_EN} with `run_code`.", LOOKUP_EN)
    assert copies_reply(f"**数据** — {LOOKUP_ZH}", LOOKUP_ZH)
    assert not copies_reply("**Data** — Read HECA v2026.11.2 HRCA_snRNA_allcells.h5ad with `run_code`.", LOOKUP_EN)
    # A short request may well be said by the step that carries it out.
    assert not copies_reply("**Dot plot** — Draw a dot plot of the markers with `run_code`.",
                            "add a dot plot of the markers")


# --- the catalogue and a step's dataset ------------------------------------------------------------

def test_the_catalogue_lists_own_and_shared_datasets_with_versions_and_files():
    text = plan_datasets.catalogue_text(CATALOGUE)
    assert "ko_vs_wt.h5ad (file" in text
    assert "HECA [heca]" in text and "v2026.11.2, v2026.11.1, v2026.10.1 (withdrawn)" in text
    assert "HRCA_snRNA_allcells.h5ad — retina; snRNA; 3,180,000 cells; obs: majorclass, celltype" in text
    assert "/" not in text.replace("w/", ""), "no server path goes into a prompt"
    assert plan_datasets.catalogue_text([]) == ""


def test_a_steps_dataset_is_checked_against_the_catalogue():
    check = plan_datasets.checker(CATALOGUE)
    ref, why = check({"name": "heca", "file": "HRCA_snRNA_allcells.h5ad"})
    assert ref == {**HRCA, "source": "shared", "id": "heca"} and not why, "latest is read as its label"
    assert check({"name": "HECA", "version": "v2026.11.1"})[0]["version"] == "v2026.11.1"
    assert check({"name": "ko_vs_wt.h5ad"})[0] == {"name": "ko_vs_wt.h5ad", "source": "own",
                                                   "id": "11111111-1111-1111-1111-111111111111"}
    for bad, words in (({"name": "Mouse atlas"}, "no dataset named 'Mouse atlas'"),
                       ({"name": "HECA", "version": "v1999"}, "has no version 'v1999'"),
                       ({"name": "HECA", "version": "v2026.10.1"}, "was withdrawn"),
                       ({"name": "HECA", "file": "Brain.h5ad"}, "has no file 'Brain.h5ad'"),
                       ({"name": "ko_vs_wt.h5ad", "version": "v2"}, "has no versions")):
        ref, why = check(bad)
        assert ref is None and words in why, (bad, why)


def test_the_dataset_field_is_written_read_edited_and_checked():
    doc = PlanDoc("q", (PlanStep("s1", QC, tool="mcp_run_scanpy_qc"),
                        PlanStep("s2", EXPR, tool="mcp_run_clustering",
                                 dataset={**HRCA, "source": "shared", "id": "heca"})))
    text = doc.to_yaml()
    assert "    dataset:\n      name: HECA\n      version: v2026.11.2\n      file: HRCA_snRNA_allcells.h5ad" in text
    back = parse_plan_yaml(text, request="q")
    assert back.steps[1].dataset == doc.steps[1].dataset and back.steps[0].dataset is None
    assert doc.steps[1].to_public()["dataset"]["file"] == "HRCA_snRNA_allcells.h5ad"
    edited = apply_ops(doc, [{"op": "change", "step": "s1", "dataset": {"name": "ko_vs_wt.h5ad"}},
                             {"op": "change", "step": "s2", "dataset": None}])
    _doc, changes, _notes = apply_edit(doc, edited)
    assert [(c["kind"], c["step"]) for c in changes] == [("dataset", 1), ("dataset", 2)]
    checked, findings = check_plan(edited, {}, datasets=plan_datasets.checker(CATALOGUE))
    assert not findings and checked.steps[0].dataset["source"] == "own"
    _checked, findings = check_plan(apply_ops(doc, [{"op": "change", "step": "s1", "dataset": "Nope"}]), {},
                                    datasets=plan_datasets.checker(CATALOGUE))
    assert [f["kind"] for f in findings] == ["unknown_dataset"]
    with pytest.raises(PlanError):
        dataset_ref({"name": "HECA", "path": "/data/store/heca"})


# --- in the loop -------------------------------------------------------------------------------------

def _catalog(calls: list) -> list[HarnessTool]:
    def tool(name: str, props: dict) -> HarnessTool:
        def run(args, _ctx):
            calls.append((name, dict(args)))
            return {"status": "ok", "tool": name}
        return HarnessTool(name, f"{name} test double", {"type": "object", "properties": props}, run)

    return [tool("mcp_run_scanpy_qc", {"min_genes": {"type": "integer", "default": 200}}),
            tool("mcp_run_clustering", {"resolution": {"type": "number", "default": 1.0}})]


def _scientist_chat(messages, _tools):
    return {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
        "name": "finish", "arguments": json.dumps({"answer": "done"})}}]}


def _complete(counts: dict, prompts: list, *, reading=None, revise=None, answers=None):
    """The lab's models: the PI drafts AGENDA; the reply reader returns ``reading`` (a list of parts,
    or a callable of the reply); the reviser returns ``revise``; an answer is the next of ``answers``."""
    answers = iter(answers or ["Step 2 reads no dataset: none is attached to this chat."] * 6)

    def complete(messages):
        system = instruction(messages)
        user = messages[-1]["content"]
        # Everything the call is given: in the plan phase's conversation, the earlier turns too.
        prompts.append((system[:50], "\n".join(m["content"] for m in messages[1:])))
        if "read a researcher's reply to an analysis plan" in system:
            counts["read"] = counts.get("read", 0) + 1
            said = user.rsplit("The researcher's reply:\n", 1)[1].split("\n\nReturn", 1)[0]
            parts = reading(said) if callable(reading) else reading
            return json.dumps({"parts": parts}) if parts is not None else "?"
        if "answering a researcher's question" in system:
            counts["answer"] = counts.get("answer", 0) + 1
            return next(answers)
        if "changing an analysis plan" in system:
            counts["revise"] = counts.get("revise", 0) + 1
            return revise if isinstance(revise, str) else json.dumps(revise or {"ops": []})
        if "Principal Investigator of a bioinformatics lab" in system:
            counts["plan"] = counts.get("plan", 0) + 1
            return json.dumps({"agenda": AGENDA})
        if "revising ONE step" in system:
            counts["patch"] = counts.get("patch", 0) + 1
            return '{"step": 0, "new_text": ""}'
        if "filling in a form" in system:
            return "[]"
        if "rigorous scientific Critic" in system:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        return "FINAL REPORT"
    return complete


def _lab(complete, *, catalogue=CATALOGUE) -> ResearchLab:
    calls: list = []
    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"),
                      LabConfig(plan_yaml=True, plan_review=False, auto_select_skill=False,
                                mode="single", planner="linear"),
                      complete_fn=complete,
                      scientist=ResearchHarness(catalog=_catalog(calls), chat_fn=_scientist_chat))
    if catalogue is not None:
        lab.dataset_catalog = lambda: list(catalogue)
    return lab


def _run(lab: ResearchLab, *replies: str):
    events: list[dict] = []
    seen: list[list[str]] = []
    queue = iter([*({"action": "revise", "feedback": r} for r in replies), {"action": "approve"}])

    def review(_kind, payload):
        seen.append(list(payload))
        return next(queue)

    lab.run("Where is RHO expressed in the retina?", events.append, review)
    return events, seen


def _of(events, kind):
    return [e for e in events if e.get("type") == kind]


def _last_doc(events):
    return _of(events, "plan_doc")[-1]["steps"]


@pytest.mark.parametrize("reply, reading", [
    (LOOKUP_EN, [{"kind": "lookup", "text": "look for the datasets again"},
                 {"kind": "change", "text": "write them into the plan"}]),
    (LOOKUP_ZH, [{"kind": "lookup", "text": "再去找一下数据集"}, {"kind": "change", "text": "写进计划"}]),
    (LOOKUP_EN, None),     # the model's reading failed: the word lists still see the look-up
])
def test_a_look_up_writes_real_datasets_into_the_plan_and_never_the_reply(reply, reading):
    counts: dict = {}
    prompts: list = []
    ops = {"ops": [{"op": "change", "step": "s2", "dataset": HRCA,
                    "does": "Summarise the expression of RHO per cell type in HECA's HRCA snRNA file "
                            "with `mcp_run_clustering` at resolution 0.5."}]}
    events, seen = _run(_lab(_complete(counts, prompts, reading=reading, revise=ops)), reply)
    found = _of(events, "plan_lookup")
    assert found and "HECA v2026.11.2 (shared, 2 files)" in found[0]["found"] and found[0]["datasets"] == 2
    step = _last_doc(events)[1]
    assert step["dataset"] == {**HRCA, "source": "shared", "id": "heca"}
    assert not any(copies_reply(s, reply) for s in seen[-1]), "the step never holds the reply"
    revise_prompt = next(u for s, u in prompts if s.startswith("You are changing an analysis plan"))
    assert "HECA [heca]" in revise_prompt and "HRCA_snRNA_allcells.h5ad" in revise_prompt, \
        "the reviser is given the catalogue"
    assert "patch" not in counts and counts["plan"] == 1


@pytest.mark.parametrize("reply", [LOOKUP_EN, LOOKUP_ZH])
def test_a_look_up_that_finds_nothing_says_so_and_changes_nothing(reply):
    counts: dict = {}
    events, seen = _run(_lab(_complete(counts, [], reading=None), catalogue=[]), reply)
    refused = _of(events, "plan_refused")
    assert refused and "no datasets were found" in refused[0]["reason"]
    assert seen[1] == seen[0] and not _of(events, "plan_edited")
    assert "revise" not in counts and "patch" not in counts and counts["plan"] == 1


def test_a_reviser_that_pastes_the_reply_into_a_step_is_refused():
    counts: dict = {}
    pasted = {"ops": [{"op": "change", "step": "s2", "title": "Datasets", "does": LOOKUP_EN}]}
    events, seen = _run(_lab(_complete(counts, [], reading=None, revise=pasted)), LOOKUP_EN)
    assert _of(events, "plan_refused") and seen[1] == seen[0]
    assert not any(LOOKUP_EN in s for s in seen[-1])
    assert "patch" not in counts and counts["plan"] == 1, "no redraft writes it in instead"


def test_a_dataset_that_does_not_exist_is_refused_with_the_plan_unchanged():
    counts: dict = {}
    ops = {"ops": [{"op": "change", "step": "s2", "dataset": {"name": "Mouse retina atlas"}}]}
    events, seen = _run(_lab(_complete(counts, [], reading=None, revise=ops)), LOOKUP_EN)
    refused = _of(events, "plan_refused")
    assert refused and "no dataset named 'Mouse retina atlas'" in refused[0]["reason"]
    assert seen[1] == seen[0] and "dataset" not in _last_doc(events)[1]


def test_a_request_the_reviser_cannot_carry_out_is_refused_in_words():
    counts: dict = {}
    cannot = json.dumps({"cannot": "No tool here aligns raw FASTQ files; upload a count matrix instead."})
    events, seen = _run(_lab(_complete(counts, [], reading=[{"kind": "change", "text": "align the FASTQs first"}],
                                       revise=cannot)), "align the FASTQs first")
    refused = _of(events, "plan_refused")
    assert refused and refused[0]["reason"].startswith("No tool here aligns raw FASTQ files")
    assert seen[1] == seen[0] and "patch" not in counts and counts["plan"] == 1


@pytest.mark.parametrize("reply, reading", [
    ("Which datasets does step 2 use? Use HECA instead",
     [{"kind": "question", "text": "Which datasets does step 2 use?"}, {"kind": "change", "text": "Use HECA instead"}]),
    ("第2步用的是哪些数据集？换成 HECA",
     [{"kind": "question", "text": "第2步用的是哪些数据集？"}, {"kind": "change", "text": "换成 HECA"}]),
])
def test_a_question_and_a_change_get_an_answer_and_a_change_to_that_step_only(reply, reading):
    counts: dict = {}
    prompts: list = []
    ops = {"ops": [{"op": "change", "step": "s2", "dataset": HRCA}]}
    events, seen = _run(_lab(_complete(counts, prompts, reading=reading, revise=ops)), reply)
    kinds = [e["type"] for e in events if e["type"] in ("plan_answer", "plan_edited")]
    assert kinds == ["plan_answer", "plan_edited"], "the answer first, then the change"
    assert _of(events, "plan_answer")[0]["then_change"] is True
    edited = _of(events, "plan_edited")[0]
    assert {c["step"] for c in edited["changes"]} == {2} and seen[1][0] == seen[0][0]
    answer_prompt = next(u for s, u in prompts if s.startswith("You are the Principal Investigator, answering"))
    assert "HECA [heca]" in answer_prompt, "a question about data is answered from the catalogue"
    assert reading[0]["text"] in answer_prompt and reading[1]["text"] not in answer_prompt.split("asks:")[-1]


@pytest.mark.parametrize("reply", ["why is step 2 at resolution 0.5?", "第2步为什么用 0.5 的分辨率？",
                                   "should we raise the resolution to 0.8?"])
def test_a_question_never_changes_the_plan(reply):
    counts: dict = {}
    events, seen = _run(_lab(_complete(counts, [], reading=[{"kind": "question", "text": reply}])), reply)
    assert _of(events, "plan_answer") and seen[1] == seen[0]
    assert not _of(events, "plan_edited") and "revise" not in counts and "patch" not in counts
    assert counts["plan"] == 1


def test_earlier_questions_and_answers_go_into_the_next_answer():
    counts: dict = {}
    prompts: list = []
    first, second = "why resolution 0.5?", "and would 0.8 split the rods?"
    lab = _lab(_complete(counts, prompts, reading=lambda said: [{"kind": "question", "text": said}],
                         answers=["0.5 keeps the major classes whole.", "Yes, 0.8 splits rods by depth."]))
    _run(lab, first, second)
    answers = [u for s, u in prompts if s.startswith("You are the Principal Investigator, answering")]
    assert len(answers) == 2
    assert "0.5 keeps the major classes whole." not in answers[0]
    earlier = answers[1].rsplit("The researcher asks:", 1)[0]
    assert first in earlier and "0.5 keeps the major classes whole." in earlier, \
        "the second answer is given after the first question and its answer"
    assert second not in earlier.rsplit("You read a researcher's reply", 1)[0]
    # The reading of the second reply sees the first exchange too, and the log is kept with the plan.
    readings = [u for s, u in prompts if s.startswith("You read a researcher's reply")]
    assert first in readings[1]
    saved = lab.plan_context["review"]
    assert [r["reply"] for r in saved] == [first, second] and "0.8 splits" in saved[1]["answer"]


def test_the_review_log_is_kept_across_a_saved_plan():
    """A reply to a saved plan is handled by a lab built anew; the review so far comes with it."""
    counts: dict = {}
    prompts: list = []
    lab = _lab(_complete(counts, prompts, reading=lambda said: [{"kind": "question", "text": said}],
                         answers=["Rods are 70% of the cells."]))
    doc = PlanDoc("Where is RHO expressed?", (PlanStep("s1", QC, tool="mcp_run_scanpy_qc"),
                                              PlanStep("s2", EXPR, tool="mcp_run_clustering")))

    class Waiting(Exception):
        pass

    def wait(_kind, _payload):
        raise Waiting

    with pytest.raises(Waiting):
        lab.run(doc.request, saved_plan=doc, plan_review=wait,
                plan_context={"review": [{"reply": "how many rods?", "answer": "About 70% are rods."}]},
                saved_plan_decision={"action": "revise", "feedback": "and how many cones?"})
    answer = next(u for s, u in prompts if s.startswith("You are the Principal Investigator, answering"))
    assert "how many rods?" in answer and "About 70% are rods." in answer
    assert [r["reply"] for r in lab.plan_context["review"]] == ["how many rods?", "and how many cones?"]


def test_a_hand_edit_naming_a_dataset_nobody_has_is_refused():
    counts: dict = {}
    lab = _lab(_complete(counts, []))
    events: list[dict] = []
    decisions = iter(["edit", "approve"])

    def review(_kind, _payload):
        action = next(decisions)
        if action == "approve":
            return {"action": "approve"}
        doc = parse_plan_yaml(_of(events, "plan_doc")[-1]["yaml"], request="q")
        bad = apply_ops(doc, [{"op": "change", "step": "s2", "dataset": {"name": "Nope", "version": "v1"}}])
        return {"action": "edit", "plan_yaml": bad.to_yaml()}

    lab.run("Where is RHO expressed?", events.append, review)
    rejected = _of(events, "plan_edit_rejected")
    assert rejected and "no dataset named 'Nope'" in rejected[0]["problems"][0]


# --- review of PR #207 ---------------------------------------------------------------------------------

# A step dictated word for word, with a tool the test catalogue has (a step naming no tool of the
# catalogue would be a code step, which a deployment may hold on the card).
DICTATED = "Add a step: re-cluster the rods with `mcp_run_clustering` at resolution 1.2 to split the rod subtypes"
DICTATED_STEP = ("**Rod subtypes** — Re-cluster the rods with `mcp_run_clustering` at resolution 1.2 to split "
                 "the rod subtypes.")


def test_a_dictated_step_is_the_change_asked_for_not_an_echo():
    """The step holds the reply's words because the reply dictated it: it carries no words said to the
    planner, so it is adopted (in the document revision and in the redraft fallback alike)."""
    assert not copies_reply("**DE in rods** — Run `run_de` comparing KO versus WT in rods using the Wilcoxon test.",
                            "Add a step: run `run_de` comparing KO versus WT in rods using the Wilcoxon test")
    assert not copies_reply(DICTATED_STEP, DICTATED)
    counts: dict = {}
    ops = {"ops": [{"op": "add", "after": "s2", "title": "Rod subtypes", "tool": "mcp_run_clustering",
                    "does": DICTATED_STEP.split(" — ", 1)[1]}]}
    events, seen = _run(_lab(_complete(counts, [], reading=[{"kind": "change", "text": DICTATED}], revise=ops)),
                        DICTATED)
    assert not _of(events, "plan_refused") and len(seen[-1]) == 3 and "rod subtypes" in seen[-1][2]


def test_a_redraft_that_writes_the_dictated_step_is_kept():
    counts: dict = {}
    dictated = DICTATED_STEP
    base = _complete(counts, [], reading=[{"kind": "change", "text": DICTATED}], revise={"ops": []})

    def complete(messages):
        if "Principal Investigator of a bioinformatics lab" in messages[0]["content"] and \
                "asked for these changes" in messages[-1]["content"]:
            return json.dumps({"agenda": [*AGENDA, dictated]})
        return base(messages)

    events, seen = _run(_lab(complete), DICTATED)
    assert not _of(events, "plan_refused") and seen[-1][-1] == dictated


@pytest.mark.parametrize("reading", [
    None,                                                     # the word lists: a change, no look-up
    [{"kind": "lookup", "text": "Find the DE genes between KO and WT in this data"},
     {"kind": "change", "text": "add a volcano plot"}],       # the model calling it a look-up
])
@pytest.mark.parametrize("catalogue", [None, CATALOGUE])
def test_a_request_for_analysis_is_never_held_up_as_a_look_up(reading, catalogue):
    reply = "Find the DE genes between KO and WT in this data and add a volcano plot"
    assert fallback_reading(reply).kinds() == ["change"]
    counts: dict = {}
    prompts: list = []
    lab = _lab(_complete(counts, prompts, reading=reading, revise="not operations"), catalogue=catalogue)
    events, _seen = _run(lab, reply)
    assert not _of(events, "plan_refused") and not _of(events, "plan_lookup")
    assert counts.get("patch") == 1, "the document revision could not be read: the old path runs, no refusal"
    revise = next(u for s, u in prompts if s.startswith("You are changing an analysis plan"))
    assert "Find the DE genes between KO and WT" in revise and "add a volcano plot" in revise, \
        "the whole request reaches the reviser"


@pytest.mark.parametrize("reply, kinds", [
    ("check what the QC thresholds mean", ["change"]),          # the old classifier's reading, no look-up
    ("find the cells with high mitochondrial reads and drop them", ["change"]),
    ("do we have enough cells in the rods?", ["question"]),
    ("Which datasets does the plan use?", ["question"]),
    ("what shared datasets are available?", ["lookup"]),
    ("查一下有哪些数据集可以用", ["lookup"]),
])
def test_the_look_up_words_need_a_dataset_and_a_question_about_what_exists(reply, kinds):
    assert fallback_reading(reply).kinds() == kinds


def test_two_uploads_with_one_name_are_told_apart_by_their_id():
    rows = [{"source": "own", "id": "aaaaaaaa-1111-0000-0000-000000000000", "name": "filtered_feature_bc_matrix.h5",
             "uploaded": "2026-10-02"},
            {"source": "own", "id": "bbbbbbbb-2222-0000-0000-000000000000", "name": "filtered_feature_bc_matrix.h5",
             "uploaded": "2026-11-12"}]
    text = plan_datasets.catalogue_text(rows)
    assert "uploaded 2026-10-02) [id aaaaaaaa]" in text and "uploaded 2026-11-12) [id bbbbbbbb]" in text
    check = plan_datasets.checker(rows)
    ref, why = check({"name": "filtered_feature_bc_matrix.h5"})
    assert ref is None and "2 of your datasets are named" in why and "id bbbbbbbb, uploaded 2026-11-12" in why
    assert "shared" not in why
    for given in ({"name": "filtered_feature_bc_matrix.h5", "id": "bbbbbbbb"},
                  {"name": "filtered_feature_bc_matrix.h5", "id": "bbbbbbbb-2222-0000-0000-000000000000"}):
        assert check(given)[0]["id"] == rows[1]["id"]
    # An edited name wins over the id left beside it.
    shared = [*rows, *CATALOGUE]
    assert plan_datasets.checker(shared)({"name": "HECA", "id": "bbbbbbbb"})[0]["source"] == "shared"
