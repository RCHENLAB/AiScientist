"""A skill or pipeline declares the outputs a step should leave, and the step is checked against them
(RCHENLAB/AiScientist issue 10, part 1).

The declaration: ``outputs:`` in a SKILL.md's front matter (top level or under ``metadata:``, as a
list or a JSON string). What it does: the agent reads it with the skill, the plan shows it per step,
the Critic may not accept a step that did not leave a required output, the technical report's
Diagnostics name what is missing, and the Results panel labels each produced file. A skill without
the block works as before. Scripted models throughout.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("yaml")

from aiscientist.agents import preset_pipelines as pp  # noqa: E402
from aiscientist.agents import research_lab as rl  # noqa: E402
from aiscientist.agents import skill_outputs as so  # noqa: E402
from aiscientist.agents import skills as skills_mod  # noqa: E402
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness  # noqa: E402
from aiscientist.agents.research_lab import CriticVerdict, LabConfig, LabRound, ResearchLab  # noqa: E402
from aiscientist.agents.skills import refresh_skills  # noqa: E402

DE = "compare-two-groups-with-scanpy"
DE_MD = """---
name: compare-two-groups-with-scanpy
description: Differential expression between two named groups of cells with scanpy rank_genes_groups (Wilcoxon). Use when a question compares exactly two groups.
license: MIT
outputs:
  - path: tables/de_*_vs_*.csv
    kind: table
    description: One row per gene with log2 fold change, p-value, adjusted p-value and mean
      expression in each group.
    required: true
  - path: figures/volcano_*_vs_*.png
    kind: figure
    description: Volcano plot of the comparison, with the top 20 genes labelled.
    required: false
metadata:
  category: single-cell
  version: "1.0.0"
  aliases: "pairwise_de"
---
# Compare two groups

Rank genes between the two groups with a Wilcoxon test and write the table.
"""
PLAIN_MD = """---
name: score-gene-signatures-with-scanpy
description: Score every cell for one or more gene signatures with scanpy score_genes and summarise the score per group.
metadata:
  category: single-cell
---
# Score gene signatures

Score with `sc.tl.score_genes`.
"""
DE_STEP = ("**Compare KO with WT** — Rank the genes that differ between KO and WT cells and write "
           "the table.")
ACCEPT = json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})


# --- the declaration -------------------------------------------------------------------------------

def test_the_three_written_forms_give_the_same_entries():
    top = so.from_text(DE_MD)
    nested = so.from_text("""---
name: x
metadata:
  category: c
  outputs:
    - path: tables/de_*_vs_*.csv
      kind: table
      description: One row per gene with log2 fold change, p-value, adjusted p-value and mean expression in each group.
    - path: figures/volcano_*_vs_*.png
      kind: figure
      description: Volcano plot of the comparison, with the top 20 genes labelled.
      required: false
  version: "1.0.0"
---
body""")
    as_json = so.from_text("---\nname: x\nmetadata:\n  outputs: '" + json.dumps(
        [o.to_public() for o in top[0]]) + "'\n---\nbody")
    assert top == nested == as_json
    first, second = top[0]
    assert (first.path, first.kind, first.required) == ("tables/de_*_vs_*.csv", "table", True)
    assert first.description.endswith("mean expression in each group.")   # wrapped line joined
    assert second.required is False and top[1] == ()


def test_the_registry_string_form_may_quote_an_apostrophe_and_run_over_lines():
    """A registry that keeps every metadata value a string carries the list as JSON. YAML writes an
    apostrophe in a single-quoted string as '', and a long string may wrap onto the next lines."""
    text = """---
name: x
metadata:
  category: c
  outputs: '[{"path": "tables/a.csv", "kind": "table", "description": "The skill''s table.",
    "required": true}, {"path": "figures/b.png", "kind": "figure",
    "description": "A figure.", "required": false}]'
  version: "1.0.0"
---
body"""
    outputs, problems = so.from_text(text)
    assert problems == ()
    assert [(o.path, o.description, o.required) for o in outputs] == [
        ("tables/a.csv", "The skill's table.", True), ("figures/b.png", "A figure.", False)]
    meta, _ = skills_mod._parse_front_matter(text)
    assert meta["metadata"]["version"] == "1.0.0" and '"kind"' not in json.dumps(
        {k: v for k, v in meta["metadata"].items() if k != "outputs"})


def test_a_bad_entry_is_left_out_with_the_reason_and_the_rest_kept():
    text = """---
name: x
outputs:
  - path: /data/abs.csv
    kind: table
    description: absolute
  - path: ../up.csv
    kind: table
    description: escapes
  - path: tables/a.csv
    kind: spreadsheet
    description: unknown kind
  - path: tables/b.csv
    kind: table
  - path: tables/c.csv
    kind: table
    description: fine
    required: maybe
  - path: $AISCIENTIST_ARTIFACTS/tables/ok.csv
    kind: table
    description: The one good table.
---
body"""
    outputs, problems = so.from_text(text)
    assert [o.path for o in outputs] == ["tables/ok.csv"]
    assert len(problems) == 5
    assert any("relative" in p for p in problems) and any("spreadsheet" in p for p in problems)
    assert any("no description" in p for p in problems) and any("true or false" in p for p in problems)
    # a pipeline's entry must name the tool whose step writes it
    assert so.from_text(text.replace("tables/ok.csv", "tables/ok.csv\n    tool: run_de"),
                        needs_tool=True)[0][0].tool == "run_de"
    assert so.from_text(DE_MD, needs_tool=True)[0] == ()


def test_a_comment_after_a_value_is_not_part_of_it():
    text = """---
name: x
outputs:
  - path: tables/a.csv   # the main table
    kind: table          # table, figure, h5ad, json, report_section or folder
    description: "A table # with a hash in it"   # quoted text keeps its hash
    required: true       # the step is incomplete without it
---
body"""
    outputs, problems = so.from_text(text)
    assert problems == () and outputs[0] == so.DeclaredOutput(
        "tables/a.csv", "table", "A table # with a hash in it", True)


def test_no_declaration_is_no_outputs_and_no_problem():
    assert so.from_text(PLAIN_MD) == ((), ())
    assert so.from_text("no front matter at all") == ((), ())


# --- the skill library ----------------------------------------------------------------------------

@pytest.fixture
def library(tmp_path, monkeypatch, skill_release):
    root = tmp_path / "skills"
    for name, md in ((DE, DE_MD), ("score-gene-signatures-with-scanpy", PLAIN_MD)):
        folder = root / "single-cell" / name
        (folder / "scripts").mkdir(parents=True)
        (folder / "SKILL.md").write_text(md, encoding="utf-8")
        (folder / "scripts" / "reference.py").write_text("print('x')\n", encoding="utf-8")
    monkeypatch.delenv("AISCIENTIST_INDUCED_SKILLS_DIR", raising=False)
    skill_release(root)
    lib: dict = {}
    monkeypatch.setattr(skills_mod, "SKILLS", lib)
    monkeypatch.setattr(rl, "ATOMIC_SKILLS", lib)
    monkeypatch.setattr(skills_mod, "ALL_SKILLS", {})
    monkeypatch.setattr(skills_mod, "_SIGNATURE", ())
    monkeypatch.setattr(skills_mod, "_REGISTERED", {})
    refresh_skills()
    return lib


def test_a_released_skill_carries_its_outputs_and_its_metadata_stays_clean(library):
    de = library[DE]
    assert [o.path for o in de.outputs] == ["tables/de_*_vs_*.csv", "figures/volcano_*_vs_*.png"]
    # the entries' keys never leak into the metadata block the agent reads
    assert not {"kind", "description", "required", "path", "outputs"} & set(de.metadata)
    assert de.metadata.get("aliases") == "pairwise_de"
    assert library["score-gene-signatures-with-scanpy"].outputs == ()
    listed = {s["name"]: s for s in skills_mod.list_skills()}
    assert listed[DE]["outputs"][0]["kind"] == "table" and listed["score-gene-signatures-with-scanpy"]["outputs"] == []


def test_the_agent_reads_the_outputs_with_the_skill_and_a_skill_without_them_is_unchanged(library):
    tool = skills_mod.make_skill_reference_tool()
    got = tool.executor({"name": "pairwise_de"}, None)
    assert got["outputs"][0]["path"] == "tables/de_*_vs_*.csv" and "checks" in got["outputs_note"]
    plain = tool.executor({"name": "score-gene-signatures-with-scanpy"}, None)
    assert "outputs" not in plain and "outputs_note" not in plain and plain["doc"]


def test_a_pipeline_declares_outputs_per_tool_and_keeps_its_own_description(tmp_path, monkeypatch):
    folder = tmp_path / "pipes" / "demo"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("""---
name: demo
description: The demo protocol
tools: run_de
outputs:
  - path: tables/de_*_all.csv
    kind: table
    tool: run_de
    description: One row per gene and group.
    required: true
data_type: scrna
---
Plan the demo.""", encoding="utf-8")
    monkeypatch.setenv("AISCIENTIST_PIPELINES_DIR", str(tmp_path / "pipes"))
    loaded = pp._load_pipelines()["demo"]
    assert loaded.label == "The demo protocol"          # not the output's description
    assert loaded.data_type == "scrna" and loaded.tools == ("run_de",)
    assert [(o.path, o.tool) for o in loaded.outputs] == [("tables/de_*_all.csv", "run_de")]


# --- the check ------------------------------------------------------------------------------------

def _png(path: Path, *, blank: bool) -> None:
    Image = pytest.importorskip("PIL.Image")
    img = Image.new("RGB", (60, 40), (255, 255, 255))
    if not blank:
        for x in range(10, 50):
            img.putpixel((x, 20), (20, 20, 20))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def test_the_check_tells_present_missing_empty_blank_and_stale_apart(tmp_path):
    art = tmp_path / "artifacts"
    (art / "tables").mkdir(parents=True)
    declared = (
        so.DeclaredOutput("tables/de_*_vs_*.csv", "table", "DE table"),
        so.DeclaredOutput("tables/header_only.csv", "table", "a table"),
        so.DeclaredOutput("figures/blank.png", "figure", "a figure"),
        so.DeclaredOutput("figures/drawn.png", "figure", "a figure"),
        so.DeclaredOutput("tables/none_*.csv", "table", "absent", required=False),
        so.DeclaredOutput("data/summary.json", "json", "a summary"),
        so.DeclaredOutput("data/adata.h5ad", "h5ad", "the object"),
        so.DeclaredOutput("cellqc", "folder", "the CellQC folder"),
    )
    (art / "tables" / "de_KO_vs_WT.csv").write_text("gene,log2fc\nGfap,2.1\n")
    (art / "tables" / "header_only.csv").write_text("gene,log2fc\n")
    _png(art / "figures" / "blank.png", blank=True)
    _png(art / "figures" / "drawn.png", blank=False)
    (art / "data").mkdir()
    (art / "data" / "summary.json").write_text("{}")
    (art / "data" / "adata.h5ad").write_bytes(b"not hdf5 at all")
    (art / "cellqc" / "sample1").mkdir(parents=True)
    (art / "cellqc" / "sample1" / "metrics.csv").write_text("a\n1\n")
    got = {r.declared.path: (r.status, r.files) for r in so.check(declared, art)}
    assert got["tables/de_*_vs_*.csv"] == ("ok", ["tables/de_KO_vs_WT.csv"])
    assert got["tables/header_only.csv"][0] == "ok"        # a null result is a written table
    assert got["figures/blank.png"][0] == "blank"
    assert got["figures/drawn.png"][0] == "ok"
    assert got["tables/none_*.csv"][0] == "missing"
    assert got["data/summary.json"][0] == "empty"
    assert got["data/adata.h5ad"][0] == "invalid"
    assert got["cellqc"][0] == "ok"
    results = so.check(declared, art)
    lines = so.problem_lines(results)
    # the optional table is not a problem; the three bad required ones are
    assert len(lines) == 3 and not any("none_" in line for line in lines)
    header_only = next(r for r in results if r.declared.path == "tables/header_only.csv")
    assert header_only.ok and header_only.detail == "header only: no data rows"
    assert any(line.startswith("figures/blank.png (figure): a blank image") for line in lines)

    # A file from before the step does not count as the step's own output.
    old = time.time() - 3600
    os.utime(art / "tables" / "de_KO_vs_WT.csv", (old, old))
    stale = so.check(declared[:1], art, since=time.time())[0]
    assert stale.status == "stale" and "earlier" in so.problem_lines([stale])[0]


def test_a_glob_cannot_reach_outside_the_artifacts(tmp_path):
    art = tmp_path / "artifacts"
    art.mkdir()
    (tmp_path / "secret.csv").write_text("a\n1\n")
    (art / "link").symlink_to(tmp_path)
    assert so.check([so.DeclaredOutput("link/secret.csv", "table", "x")], art)[0].status == "missing"


def test_the_record_labels_each_produced_file(tmp_path):
    art = tmp_path / "artifacts"
    (art / "tables").mkdir(parents=True)
    (art / "tables" / "de_KO_vs_WT.csv").write_text("gene,log2fc\nGfap,2.1\n")
    results = so.check([so.DeclaredOutput("tables/de_*_vs_*.csv", "table", "One row per gene.")], art)
    so.record_step(art, step="s2", title="Compare", index=1, source="skill x 1.0.0", results=results)
    so.record_step(art, step="s1", title="QC", index=0, source="pipeline p", results=results[:0])
    labels = so.file_labels(art)
    assert labels == {"tables/de_KO_vs_WT.csv": {"description": "One row per gene.", "kind": "table",
                                                 "step": 2, "title": "Compare",
                                                 "source": "skill x 1.0.0", "status": "ok"}}


# --- the run: plan, brief, Critic, report ---------------------------------------------------------

def _lab(tmp_path, *, writes: "dict[str, str]", verdicts=(), match=DE):
    """A lab whose PI plans DE_STEP, whose matcher gives it ``match``, whose Scientist runs one
    run_code that writes ``writes`` (path -> text) under the run's artifacts, and whose Critic
    returns ``verdicts`` in order, then accepts."""
    record: dict = {}
    critic = list(verdicts)
    workspace = tmp_path / "run"
    (workspace / "artifacts").mkdir(parents=True, exist_ok=True)

    def complete(messages):
        system = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in system:
            return json.dumps({"breadth": "specific", "agenda": [DE_STEP], "later": []})
        if "match the steps of an analysis plan" in system:
            return json.dumps([{"step": 1, "skill": match}])
        if "filling in a form" in system:
            return "[]"
        if "rigorous scientific Critic" in system:
            record.setdefault("critic", []).append(json.loads(messages[-1]["content"]))
            return critic.pop(0) if critic else ACCEPT
        return "FINAL REPORT"

    def run_code(args, _ctx):
        for rel, text in writes.items():
            path = workspace / "artifacts" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return {"status": "ok", "stdout": "done"}

    def chat(messages, _tools):
        done = sum(m.get("role") == "tool" for m in messages)
        if not done:
            record.setdefault("briefs", []).append(
                next(m["content"] for m in messages if m.get("role") == "user"))
            return {"content": "", "tool_calls": [{"id": "t0", "type": "function", "function": {
                "name": "run_code", "arguments": json.dumps({"code": "write()"})}}]}
        return {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
            "name": "finish", "arguments": json.dumps({"answer": "Compared KO with WT."})}}]}

    catalog = [HarnessTool("run_code", "run Python", {"type": "object", "properties": {
        "code": {"type": "string"}}}, run_code)]
    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m", workspace=workspace),
                      LabConfig(plan_yaml=True, plan_review=False, auto_select_skill=False,
                                mode="single", planner="linear", max_revisions=1),
                      complete_fn=complete, scientist=ResearchHarness(catalog=catalog, chat_fn=chat))
    return lab, record, workspace


def _run(lab):
    events: list[dict] = []
    result = lab.run("Compare KO with WT", events.append, lambda _k, _p: {"action": "approve"})
    return result, events


def _of(events, kind):
    return [e for e in events if e.get("type") == kind]


def test_a_step_that_leaves_its_required_output_is_accepted_and_its_files_labelled(tmp_path, library):
    lab, record, workspace = _lab(tmp_path, writes={"tables/de_KO_vs_WT.csv": "gene,log2fc\nGfap,2.1\n"})
    result, events = _run(lab)
    # the plan card shows what the step will give
    step = _of(events, "plan_doc")[0]["steps"][0]
    assert step["skill"] == DE and [o["path"] for o in step["outputs"]] == [
        "tables/de_*_vs_*.csv", "figures/volcano_*_vs_*.png"]
    # the brief tells the Scientist what to leave, and that it is checked
    brief = record["briefs"][0]
    assert "tables/de_*_vs_*.csv (table, required): One row per gene" in brief
    assert "figures/volcano_*_vs_*.png (figure, optional)" in brief
    # the Critic sees the check; the step is accepted
    assert record["critic"][0]["DECLARED_OUTPUTS"][0]["status"] == "ok"
    assert "REQUIRED_OUTPUTS_MISSING" not in record["critic"][0]
    assert result.rounds[-1].verdict.verdict == "accept"
    assert result.rounds[-1].verdict.outputs["missing_required"] == []
    out = _of(events, "step_outputs")[0]
    assert out["sources"] == [f"skill {DE} 1.0.0"] and out["missing_required"] == []
    labels = so.file_labels(workspace / "artifacts")
    assert labels["tables/de_KO_vs_WT.csv"]["step"] == 1
    assert labels["tables/de_KO_vs_WT.csv"]["description"].startswith("One row per gene")


def test_a_step_without_its_required_output_is_never_accepted_and_the_report_says_why(tmp_path, library):
    lab, record, _ws = _lab(tmp_path, writes={"tables/other.csv": "a\n1\n"})
    result, events = _run(lab)
    # the model accepted twice; the floor turned both into revisions, and the step ended unaccepted
    assert [r.verdict.verdict for r in result.rounds] == ["revise", "revise"]
    assert all(c["REQUIRED_OUTPUTS_MISSING"] for c in record["critic"])
    critique = result.rounds[0].verdict.critique
    assert "auto-guard: required outputs missing" in critique and "tables/de_*_vs_*.csv" in critique
    assert _of(events, "step_force_advance")
    # the round carries the check through run_state.json and back
    again = LabRound.from_dict(json.loads(json.dumps(result.rounds[-1].to_dict())))
    assert again.verdict.outputs["missing_required"][0].startswith("tables/de_*_vs_*.csv (table): not written")
    # the technical report's Diagnostics name the file
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw
    diag = gw._summarize_pipeline_degradations(result)
    assert "required outputs declared by skill compare-two-groups-with-scanpy 1.0.0 were not produced" in diag
    assert "tables/de_*_vs_*.csv (table): not written" in diag


def test_a_skill_without_outputs_is_checked_for_nothing(tmp_path, library):
    lab, record, workspace = _lab(tmp_path, writes={}, match="score-gene-signatures-with-scanpy")
    result, events = _run(lab)
    assert result.rounds[-1].verdict.verdict == "accept" and result.rounds[-1].verdict.outputs is None
    assert "DECLARED_OUTPUTS" not in record["critic"][0] and not _of(events, "step_outputs")
    assert "outputs" not in _of(events, "plan_doc")[0]["steps"][0]
    assert "declares the outputs" not in record["briefs"][0]
    assert not (workspace / "artifacts" / so.RECORD_PATH).exists()


def test_a_pipeline_tool_step_is_checked_for_the_outputs_the_pipeline_declares(tmp_path, library):
    lab, record, workspace = _lab(tmp_path, writes={})
    lab._skills = [pp.PresetPipeline(key="demo", label="demo", prompt="p", tools=("run_de",), outputs=(
        so.DeclaredOutput("tables/de_*_all.csv", "table", "One row per gene and group.", True, "run_de"),))]
    expected, sources = lab._expected_outputs("**Markers** — Rank markers per cluster with `run_de`.")
    assert [o.path for o in expected] == ["tables/de_*_all.csv"] and sources == ["pipeline demo"]
    assert lab._expected_outputs("**Cluster** — Group the cells with `run_clustering`.") == ([], [])


def test_old_verdicts_without_a_check_still_load():
    v = CriticVerdict.from_dict({"verdict": "accept", "score": 0.8, "critique": "", "reviewed": True})
    assert v.outputs is None and v.verdict == "accept"


def test_every_preset_pipeline_in_the_repository_declares_valid_outputs():
    root = Path(__file__).resolve().parents[1] / "preset_pipelines"
    for skill_md in sorted(root.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        outputs, problems = so.from_text(text, needs_tool=True)
        name = skill_md.parent.name
        assert problems == (), f"{name}: {problems}"
        assert outputs, f"{name} declares no outputs"
        tools = {t.strip() for t in pp._parse_skill(text)[0].get("tools", "").split(",") if t.strip()}
        assert {o.tool for o in outputs} <= tools, f"{name}: an output names a tool the pipeline does not use"
        assert any(o.required for o in outputs), f"{name}: no required output"
