"""Offline tests for skill induction — the lab writing its own reusable templates.

The guards are the point. Induction is the one feature here that WRITES EXECUTABLE CODE TO DISK,
so most of these tests are about what it refuses: a bad name (it becomes a directory), code that
does not compile, an oversized body, a collision with a curated skill, and any attempt to land in
the repo's git-tracked ``skills/``.
"""

from __future__ import annotations

import json

from bioagent.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
from bioagent.agents.research_lab import (
    CriticVerdict, LabConfig, LabRound, ResearchLab, make_run_code_tool,
)
from bioagent.agents.skill_induction import (
    InducedSkill, candidates, induce, write_skill,
)
from bioagent.agents.skills import Skill, register_skill

_INDUCE_MARK = "curating a lab's library of reusable analysis templates"

GOOD_CODE = (
    "import os\n"
    "import scanpy as sc\n"
    "# --- CONFIG (adapt to your dataset) ---\n"
    "GROUP_KEY = 'celltype'\n"
    "MIN_CELLS = 10\n"
    "# --------------------------------------\n"
    "adata = sc.read_h5ad(os.environ['BIOAGENT_DATASET'])\n"
    "counts = adata.obs[GROUP_KEY].value_counts()\n"
    "keep = counts[counts >= MIN_CELLS].index\n"
    "out = os.path.join(os.environ['BIOAGENT_ARTIFACTS'], 'tables', 'group_sizes.csv')\n"
    "counts.loc[keep].to_csv(out)\n"
    "print('wrote', out)\n"
)


def _round(step="Summarize population sizes", code=GOOD_CODE, verdict="accept", ok=True):
    return LabRound(1, 1, step, "Generalist",
                    {"final_answer": "done", "steps": [
                        {"tool": "run_code", "args": {"code": code}, "ok": ok, "summary": "ran"}]},
                    CriticVerdict(verdict, 0.9, ""))


def _reply(**over):
    base = {"keep": True, "reason": "a reusable per-group size summary",
            "name": "summarize_group_sizes",
            "description": "Count cells per group and write the table.",
            "when_to_use": "When you need per-group cell counts.", "code": GOOD_CODE}
    base.update(over)
    return json.dumps(base)


# --- candidate selection -----------------------------------------------------

def test_only_accepted_successful_run_code_steps_are_candidates():
    rounds = [
        _round(step="kept"),
        _round(step="rejected by the critic", verdict="revise"),
        _round(step="the code errored", ok=False),
        _round(step="too short to be a skill", code="print(1)\n"),
    ]
    assert [c["step"] for c in candidates(rounds)] == ["kept"]


def test_a_non_run_code_step_is_never_a_candidate():
    r = LabRound(1, 1, "s", "G", {"final_answer": "a", "steps": [
        {"tool": "run_qc", "args": {"min_genes": 200}, "ok": True}]}, CriticVerdict("accept", 1, ""))
    assert candidates([r]) == []


# --- validation: what induction refuses --------------------------------------

def _induce_one(reply, taken=frozenset()):
    return induce(candidates([_round()]), lambda _m: reply,
                  existing_manifest="", tool_names="run_qc", taken=set(taken))


def test_a_kept_skill_round_trips():
    kept, rejected = _induce_one(_reply())
    assert rejected == [] and len(kept) == 1
    assert kept[0].name == "summarize_group_sizes"
    assert kept[0].origin_step == "Summarize population sizes"


def test_declining_is_a_normal_outcome():
    kept, rejected = _induce_one(json.dumps({"keep": False, "reason": "one-off inspection"}))
    assert kept == [] and rejected == ["one-off inspection"]


def test_a_name_that_is_not_a_safe_directory_is_refused():
    for bad in ("../escape", "Has Spaces", "a", "trailing-dash-", "x" * 60, "", "with/slash"):
        kept, rejected = _induce_one(_reply(name=bad))
        assert kept == [] and "invalid skill name" in rejected[0], bad


def test_a_name_is_case_normalized_rather_than_rejected():
    """Casing is a style slip, not a safety problem — normalize it instead of losing the skill."""
    kept, _ = _induce_one(_reply(name="Summarize_Group_Sizes"))
    assert [k.name for k in kept] == ["summarize_group_sizes"]


def test_code_that_does_not_compile_is_refused():
    kept, rejected = _induce_one(_reply(code="def broken(:\n    pass\n" + "# pad\n" * 40))
    assert kept == [] and "does not compile" in rejected[0]


def test_an_oversized_template_is_refused():
    kept, rejected = _induce_one(_reply(code="x = 1\n" * 4000))
    assert kept == [] and "too large" in rejected[0]


def test_a_collision_with_an_existing_skill_is_refused():
    kept, rejected = _induce_one(_reply(), taken={"summarize_group_sizes"})
    assert kept == [] and "already exists" in rejected[0]


def test_a_missing_description_is_refused_so_the_index_stays_useful():
    kept, rejected = _induce_one(_reply(description="  "))
    assert kept == [] and "no description" in rejected[0]


def test_a_garbled_reply_is_refused_not_raised():
    kept, rejected = _induce_one("sorry, I can't do JSON")
    assert kept == [] and rejected == ["unparseable reply"]


def test_an_llm_failure_is_recorded_not_raised():
    def boom(_m):
        raise RuntimeError("endpoint down")
    kept, rejected = induce(candidates([_round()]), boom, existing_manifest="",
                            tool_names="", taken=set())
    assert kept == [] and "endpoint down" in rejected[0]


def test_max_new_caps_how_many_a_single_run_can_add():
    names = iter(["skill_one", "skill_two", "skill_three"])
    kept, _ = induce([{"step": f"s{i}", "code": GOOD_CODE, "summary": ""} for i in range(3)],
                     lambda _m: _reply(name=next(names)),
                     existing_manifest="", tool_names="", taken=set(), max_new=2)
    assert [k.name for k in kept] == ["skill_one", "skill_two"]


def test_two_candidates_cannot_claim_the_same_name_within_one_run():
    kept, rejected = induce([{"step": "a", "code": GOOD_CODE, "summary": ""},
                             {"step": "b", "code": GOOD_CODE, "summary": ""}],
                            lambda _m: _reply(), existing_manifest="", tool_names="",
                            taken=set(), max_new=2)
    assert len(kept) == 1 and "already exists" in rejected[0]


# --- writing to disk ---------------------------------------------------------

def test_write_skill_produces_a_loadable_skill_folder(tmp_path):
    skill = InducedSkill("my_new_skill", "Does a thing.", "When you need a thing.", GOOD_CODE,
                         origin_step="Summarize population sizes")
    out = write_skill(tmp_path, skill, origin_run="run-123")
    assert out is not None
    folder, name = out
    assert name == "my_new_skill" and (folder / "SKILL.md").exists()
    assert (folder / "reference.py").read_text() == GOOD_CODE

    # it parses back through the SAME loader the curated library uses, and is marked as induced
    import os
    from bioagent.agents import skills as skills_mod
    os.environ["BIOAGENT_INDUCED_SKILLS_DIR"] = str(tmp_path)
    try:
        lib = skills_mod._load_skills()
    finally:
        del os.environ["BIOAGENT_INDUCED_SKILLS_DIR"]
    assert "my_new_skill" in lib
    assert lib["my_new_skill"].summary == "Does a thing."
    assert lib["my_new_skill"].files["reference.py"] == GOOD_CODE
    md = (folder / "SKILL.md").read_text()
    assert "induced: true" in md and "origin_run: run-123" in md
    assert "not hand-curated" in md          # the reader is warned what they are looking at


def test_write_skill_never_overwrites_an_existing_folder(tmp_path):
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "SKILL.md").write_text("hand written, do not clobber")
    skill = InducedSkill("taken", "d", "w", GOOD_CODE)
    assert write_skill(tmp_path, skill) is None
    assert (tmp_path / "taken" / "SKILL.md").read_text() == "hand written, do not clobber"


def test_write_skill_refuses_a_traversing_name(tmp_path):
    assert write_skill(tmp_path, InducedSkill("../../evil", "d", "w", GOOD_CODE)) is None
    assert not (tmp_path.parent.parent / "evil").exists()


def test_a_curated_skill_always_wins_over_an_induced_one_of_the_same_name(tmp_path):
    import os
    from bioagent.agents import skills as skills_mod
    curated, induced_root = tmp_path / "curated", tmp_path / "induced"
    for root, desc in ((curated, "the curated one"), (induced_root, "the induced one")):
        (root / "shared_name").mkdir(parents=True)
        (root / "shared_name" / "SKILL.md").write_text(
            f"---\nname: shared_name\ndescription: {desc}\n---\n\nbody\n")
    os.environ["BIOAGENT_SKILLS_DIR"] = str(curated)
    os.environ["BIOAGENT_INDUCED_SKILLS_DIR"] = str(induced_root)
    try:
        lib = skills_mod._load_skills()
    finally:
        del os.environ["BIOAGENT_SKILLS_DIR"], os.environ["BIOAGENT_INDUCED_SKILLS_DIR"]
    assert lib["shared_name"].summary == "the curated one"


def test_register_skill_is_additive_only():
    existing = Skill(name="already_here", summary="original")
    assert register_skill(existing) is True
    assert register_skill(Skill(name="already_here", summary="impostor")) is False
    from bioagent.agents.skills import SKILLS
    assert SKILLS["already_here"].summary == "original"
    del SKILLS["already_here"]


# --- wired into a run --------------------------------------------------------

def _call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _finish():
    return {"content": "", "tool_calls": [
        {"id": "f1", "type": "function",
         "function": {"name": "finish", "arguments": json.dumps({"answer": "done"})}}]}


def _run_lab(tmp_path, *, induction=True, seen=None):
    agenda = ["Summarize the population sizes"]

    def scientist(messages, _tools):
        if any(m.get("role") == "tool" for m in messages):
            return _finish()
        return _call("run_code", {"code": GOOD_CODE})

    def complete(messages):
        sys_p = messages[0]["content"]
        if seen is not None:
            seen.append(sys_p)
        if _INDUCE_MARK in sys_p:
            return _reply()
        if "rigorous scientific Critic" in sys_p:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        if "Principal Investigator of a bioinformatics lab" in sys_p:
            return json.dumps(agenda)
        return "FINAL REPORT"

    cfg = LabConfig(skill_induction=induction, induced_skills_dir=str(tmp_path))
    catalog = [*default_catalog(), make_run_code_tool(lambda _c: {"status": "ok", "stdout": "ok"})]
    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"), cfg,
                      complete_fn=complete,
                      scientist=ResearchHarness(catalog=catalog, chat_fn=scientist))
    events: list[dict] = []
    lab.run("Characterize the dataset", on_event=events.append)
    return events


def test_a_run_induces_a_skill_at_the_end(tmp_path):
    from bioagent.agents.skills import SKILLS
    try:
        events = _run_lab(tmp_path)
        induced = [e for e in events if e["type"] == "skill_induced"]
        assert induced and induced[0]["name"] == "summarize_group_sizes"
        assert (tmp_path / "summarize_group_sizes" / "reference.py").exists()
        # available in-process to the NEXT run, without a restart
        assert "summarize_group_sizes" in SKILLS
    finally:
        SKILLS.pop("summarize_group_sizes", None)


def test_induction_off_by_default_never_asks_and_writes_nothing(tmp_path):
    seen: list[str] = []
    events = _run_lab(tmp_path, induction=False, seen=seen)
    assert not any(_INDUCE_MARK in s for s in seen)
    assert not any(e["type"].startswith("skill_indu") for e in events)
    assert list(tmp_path.iterdir()) == []


def test_a_run_with_no_directory_configured_writes_nothing(tmp_path):
    """The flag alone must do nothing: with nowhere safe to write, induction stays off."""
    cfg = LabConfig(skill_induction=True, induced_skills_dir=None)
    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"), cfg,
                      complete_fn=lambda _m: "{}",
                      scientist=ResearchHarness(catalog=default_catalog(),
                                                chat_fn=lambda *_a, **_k: _finish()))
    assert lab._induce_skills([_round()], lambda _e: None) == []


# --- versioned evolution: an improvement lands ALONGSIDE, never on top -----------------------

def test_supersedes_writes_the_next_version_instead_of_overwriting(tmp_path):
    first = InducedSkill("group_sizes", "v1", "w", GOOD_CODE)
    folder1, name1 = write_skill(tmp_path, first)
    (folder1 / "reference.py").write_text("ORIGINAL")

    better = InducedSkill("group_sizes", "v2, faster", "w", GOOD_CODE, supersedes="group_sizes")
    folder2, name2 = write_skill(tmp_path, better)

    assert name1 == "group_sizes" and name2 == "group_sizes_v2"
    assert folder1 != folder2
    # the older version is untouched and still on disk — that is the whole point
    assert (folder1 / "reference.py").read_text() == "ORIGINAL"
    assert "supersedes: group_sizes" in (folder2 / "SKILL.md").read_text()
    assert "kept alongside" in (folder2 / "SKILL.md").read_text()


def test_versions_chain_and_are_capped(tmp_path):
    write_skill(tmp_path, InducedSkill("proc", "v1", "w", GOOD_CODE))
    names = []
    for _ in range(12):
        out = write_skill(tmp_path, InducedSkill("proc", "better", "w", GOOD_CODE, supersedes="proc"))
        if out is None:
            break
        names.append(out[1])
    assert names[:3] == ["proc_v2", "proc_v3", "proc_v4"]
    assert len(names) == 8          # v2..v9, then the chain is full and it stops
    assert write_skill(tmp_path, InducedSkill("proc", "x", "w", GOOD_CODE, supersedes="proc")) is None


def test_a_name_clash_without_supersedes_is_still_refused(tmp_path):
    write_skill(tmp_path, InducedSkill("proc", "v1", "w", GOOD_CODE))
    assert write_skill(tmp_path, InducedSkill("proc", "again", "w", GOOD_CODE)) is None


def test_superseding_an_unknown_skill_is_refused():
    kept, rejected = _induce_one(_reply(supersedes="no_such_skill"))
    assert kept == [] and "unknown skill" in rejected[0]


def test_declaring_supersedes_lets_a_known_name_through():
    kept, rejected = _induce_one(_reply(supersedes="summarize_group_sizes"),
                                 taken={"summarize_group_sizes"})
    assert rejected == [] and kept and kept[0].supersedes == "summarize_group_sizes"


def test_the_manifest_prefers_the_newest_version_but_keeps_the_old_loadable():
    from bioagent.agents.skills import Skill as S, skill_manifest, superseded_names
    lib = {"proc": S(name="proc", summary="v1"),
           "proc_v2": S(name="proc_v2", summary="v2", supersedes="proc"),
           "other": S(name="other", summary="unrelated")}
    assert superseded_names(lib) == {"proc"}
    m = skill_manifest(lib)
    assert "proc_v2 — v2" in m and "other — unrelated" in m
    assert "\n- proc — v1" not in m          # hidden from the brief...
    assert lib["proc"].summary == "v1"       # ...but still in the library, resolvable by name


# --- frontmatter: YAML block scalars ----------------------------------------------------------

def test_a_folded_description_parses_to_its_text_not_the_marker(tmp_path):
    """`description: >-` followed by an indented block. Before this was handled, the skill
    advertised itself in the manifest as the literal '>-'."""
    from bioagent.agents import skills as sk
    d = tmp_path / "folded"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: folded\ndescription: >-\n  Recover a corpus of paper PDFs from PMIDs,\n"
        "  using a tiered ladder of legal sources.\nlicense: MIT\n---\n\nbody text\n")
    lib = sk._load_from(d.parent)
    assert lib["folded"].summary == (
        "Recover a corpus of paper PDFs from PMIDs, using a tiered ladder of legal sources.")
    assert lib["folded"].doc == "body text"


def test_a_literal_block_keeps_its_line_breaks(tmp_path):
    from bioagent.agents import skills as sk
    d = tmp_path / "lit"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: lit\ndescription: |\n  line one\n  line two\n---\n\nb\n")
    assert sk._load_from(d.parent)["lit"].summary == "line one\nline two"


def test_plain_single_line_frontmatter_is_unchanged(tmp_path):
    from bioagent.agents import skills as sk
    d = tmp_path / "plain"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: plain\ndescription: One line.\ninduced: true\n---\n\nb\n")
    s = sk._load_from(d.parent)["plain"]
    assert s.summary == "One line." and s.induced is True
