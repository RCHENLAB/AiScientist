"""The report folder of a run (``reporting/report_folder.py``, RCHENLAB/AiScientist issue 113).

The four layouts are those of real runs (an atlas query, CellQC, a plan that never ran, a RetiGene
curation), rebuilt with made-up contents: the file names and where they sit are what matters.
Pinned down: the target tree; exactly one YAML and no JSON, workflow or process file; every result
once by content, two different files of one name both kept; results outside ``artifacts`` collected;
links rewritten and resolving; the summary's five sections and its length; a plan-only run reported
as not executed; the run's own folders untouched and nothing run again."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from aiscientist.reporting import report_folder as R

PLAN = """# AiScientist plan. Run it as it is, or edit it.
plan: 1
request: Where are LRAT and RHO expressed in this retina atlas?
steps:
  - id: s1
    title: Resolve the gene symbols
    tool: resolve_genes
    skill: explore-atlas-expression
    does: Map the names onto the atlas symbols.
  - id: s2
    title: Query the atlas
    tool: submit_query
    settings:
      genes: [LRAT, RHO]
    does: Submit the query and wait for it.
"""


def version(state: str, n: int, body: str = PLAN) -> str:
    return body + R.PLAN_TRAILER + f"\n_review:\n  version: {n}\n  state: {state}\n  kind: agenda\n"


def make_run(root: Path, files: dict[str, str | bytes], name: str = "6a9b4f99bbb5") -> Path:
    run = root / name
    for rel, content in files.items():
        path = run / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return run


def tree(dest: Path) -> list[str]:
    return sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())


MANIFEST = json.dumps({
    "app": {"tag": "v0.4.11"},
    "skills": [{"name": "explore-atlas-expression", "version": "0.0.1"}],
    "mcp_servers": [{"name": "atlas-expression-exploration", "version": "0.0.3"}],
    "atlas": {"path": "/dfs/datasets/heca/v2026.11.1/HRCA_snRNA_allcells.h5ad", "version": "heca/v2026.11.1"},
})
LONG_SUMMARY = (
    "# Cell-class expression of RHO and LRAT\n\n## Abstract\n\n"
    + "RHO is rod-specific and LRAT is restricted to the RPE in this atlas. " * 40
    + "\n\nSee `tables/by_class.csv` and [the dot plot](figures/dotplot.png).\n\n"
    "## Methods\n\nWe queried the atlas.\n\n## Results\n\n### Expression by major class\n\nNumbers.\n\n"
    "## Limitations\n\n- Descriptive only: no test was run.\n- One atlas, so no replication.\n\n"
    "## Conclusion\n\nSame as the abstract.\n\n## Files available in the run\n\n- many\n\n"
    "## Choices made for this run\n\n- several\n"
)


def atlas_run(root: Path) -> Path:
    q = "extra/gene_expression/HRCA__RHO_LRAT"
    return make_run(root, {
        "artifacts/croissant.json": "{}",
        "artifacts/data/agent_decision_trace.json": '{"a": 1}',
        "artifacts/data/dataset_manifest.json": '{"b": 2}',
        "artifacts/data/dataset_results.json": '{"format": "h5ad", "cells": 3177310, "genes": 36601}',
        "artifacts/data/qc_metrics.json": '{"format": "h5ad", "cells": 3177310, "genes": 36601}',
        "artifacts/data/dataset_summary.md": "3,177,310 nuclei.\n",
        f"artifacts/{q}/dotplot.png": b"PNG-dot",
        f"artifacts/{q}/dotplot.pdf": b"PDF-dot",
        f"artifacts/{q}/summary_majorclass.tsv": "class\tRHO\nRod\t15.8\n",
        "artifacts/extra/gene_expression/mcp_result.json": '{"files": []}',
        "artifacts/extra/plan/current.json": '{"version": 3, "filename": "v03.yaml", "state": "approved"}',
        "artifacts/extra/plan/v01.yaml": version("revised", 1, PLAN.replace("LRAT and RHO", "LRAT")),
        "artifacts/extra/plan/v02.yaml": version("revised", 2),
        "artifacts/extra/plan/v03.yaml": version("approved", 3),
        "artifacts/figures/workflow.dot": "digraph {}",
        "artifacts/figures/workflow.png": b"PNG-workflow",
        "artifacts/figures/dotplot.png": b"PNG-dot",                      # the same plot, filed twice
        "artifacts/tables/by_class.csv": "class,RHO\nRod,15.8\n",
        "artifacts/process/agenda.json": "{}",
        "artifacts/process/inputs.json": json.dumps({"shared_inputs": [{"dataset": "heca", "version": "v2026.11.1"}]}),
        "artifacts/process/lab_result.json": "{}",
        "artifacts/process/round_01.json": "{}",
        "artifacts/process/run_state.json": '{"keep": "me"}',
        "artifacts/process/transcript.md": "talk",
        "artifacts/process/event_log.txt": "log",
        "artifacts/process/variant_offline_diagnostics.log": "vep",
        "artifacts/process/plan_replies/reply-01.txt": "reply",
        "artifacts/report/plan.md": "# Plan\n\n1. Resolve. Output: `tables/by_class.csv`, [plot](figures/dotplot.png).\n",
        "artifacts/report/plan.yaml": PLAN,
        "artifacts/report/summary.md": LONG_SUMMARY,
        "checkpoint_stalled.md": "stalled",
        "hpc3_home.json": "{}",
        "loop.log": "loop",
        "manifest.json": MANIFEST,
    })


def cellqc_run(root: Path) -> Path:
    same = {"manifest.tsv": "sample\tncell\nS1\t16750\n", "metrics.csv": "sample,kept\nS1,16750\n",
            "report.html": "<html>QC</html>", "report_slides.pdf": b"PDF-slides", "S1_doublet_summary.txt": "ok\n"}
    files: dict[str, str | bytes] = {}
    for name, content in same.items():
        files[f"result/cellqc/{name}"] = content
        files[f"artifacts/result/{name}"] = content
    files |= {
        "result/cellqc/S1.h5ad": b"H5AD-S1" * 10,                          # only under result/
        "result/S1.processed.20261106-2019.7f585b5a73c3.h5ad": b"H5AD-processed" * 10,
        "result/S1.processed.20261106-2019.7f585b5a73c3.h5ad.json": '{"name": "S1.processed.h5ad", "n_cells": 16750}',
        "artifacts/data/result_dataset.json": json.dumps({"name": "S1.processed.h5ad", "n_cells": 16750,
                                                           "n_genes": 33696, "layers": ["counts"]}),
        "artifacts/data/cellqc_result.json": '{"path": "/data/x", "files": []}',
        "artifacts/tables/cellqc_metrics.csv": "sample,kept\nS1,16750\n",        # = result/cellqc/metrics.csv
        "artifacts/tables/cells.csv": "cell,sample\nAAAC,S1\n",
        "artifacts/figures/violin_cellqc_after_qc.png": b"PNG-violin",
        "artifacts/figures/workflow.png": b"PNG-workflow",
        "artifacts/extra/plan/current.json": '{"version": 2, "filename": "v02.yaml", "state": "approved"}',
        "artifacts/extra/plan/v01.yaml": version("revised", 1),
        "artifacts/extra/plan/v02.yaml": version("approved", 2),
        "artifacts/process/run_state.json": "{}",
        "artifacts/report/summary.md": ("**Quality control done** on S1.\n\n- **16,750 of 18,681 cells kept**.\n"
                                        "- ⚠️ S1: Doublet filtering needs review.\n\n"
                                        "Details: `tables/cells.csv`, `result/report.html`.\n"),
        "logs/hpc3/analysis/job.log": "slurm",
        "manifest.json": json.dumps({"app": {"tag": "v0.4.8"}, "skills": [{"name": "qc-snrna-with-cellqc", "version": "1.2.0"}]}),
        "stage_out.json": "{}",
    }
    return make_run(root, files, "7f585b5a73c3")


def plan_only_run(root: Path) -> Path:
    return make_run(root, {
        "artifacts/plan/v01.yaml": version("revised", 1),
        "artifacts/plan/v02.yaml": version("revised", 2),
        "artifacts/plan/v03.yaml": version("cancelled", 3),
        "artifacts/plan/current.json": '{"version": 3, "filename": "v03.yaml", "state": "cancelled"}',
        "artifacts/process/run_state.json": "{}",
        "artifacts/process/timing.jsonl": "{}\n",
        "artifacts/process/plan_replies/reply-01.txt": "reply",
        "hpc3_home.json": "{}",
        "manifest.json": json.dumps({"app": {"tag": "v0.4.11"}, "skills": []}),
    }, "ec5f54243133")


def curation_run(root: Path) -> Path:
    return make_run(root, {
        "artifacts/croissant.json": "{}",
        "artifacts/curation_9ce59c8975/genes.csv": "gene,grade\nPRPF31,A\n",
        "artifacts/curation_9ce59c8975/hits.csv": "gene,quote\nPRPF31,q\n",
        "artifacts/curation/dominant_negative_genes.csv": "gene,claim\nRHO,stated\n",
        "artifacts/curation/dominant_negative_evidence.csv": "gene,pmid\nRHO,1\n",
        "artifacts/curation/task_spec.json": json.dumps({"task": "dominant-negative retinal disease genes",
                                                         "target_class": "dominant_negative"}),
        "artifacts/process/timing.jsonl": "{}\n",
        "curation_tables.json": json.dumps({"n_genes": 32, "counts": {"stated": 28, "contested": 4},
                                            "curation": "dominant-negative retinal disease genes"}),
        "logs/hpc3/paperqa/scan.log": "log",
        "stage_out.json": "{}",
    }, "044e0feb6c26")


def snapshot(run: Path) -> dict[str, tuple[int, int]]:
    return {p.relative_to(run).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in run.rglob("*") if p.is_file()}


def test_atlas_run_gives_the_target_tree_with_one_yaml_and_no_machine_files(tmp_path):
    run = atlas_run(tmp_path / "runs")
    before = snapshot(run)
    dest = tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    assert tree(dest) == [
        "plan.md", "plan.yaml",
        "results/extra/data/dataset_summary.md",
        "results/figures/dotplot.png",                               # the plot once; its copy under extra is the same file
        "results/figures/gene_expression/HRCA__RHO_LRAT/dotplot.pdf",
        "results/tables/by_class.csv",
        "results/tables/gene_expression/HRCA__RHO_LRAT/summary_majorclass.tsv",
        "results/tables/records/dataset_results.tsv",               # qc_metrics.json held the same values
        "summary.md",
    ]
    assert sorted(p.name for p in dest.iterdir()) == ["plan.md", "plan.yaml", "results", "summary.md"]
    assert (dest / "plan.yaml").read_text() == PLAN and yaml.safe_load((dest / "plan.yaml").read_text())["plan"] == 1
    # what the JSON measured is in a table, field by field
    assert (dest / "results/tables/records/dataset_results.tsv").read_text() == (
        "field\tvalue\nformat\th5ad\ncells\t3177310\ngenes\t36601\n")
    assert snapshot(run) == before                                   # the run's own folders are untouched


def test_what_is_left_out_is_named_with_its_reason(tmp_path):
    folder = R.plan_report_folder(atlas_run(tmp_path))
    why = dict(folder.left_out)
    assert why["artifacts/figures/workflow.png"] == "workflow schematic"
    assert why["artifacts/process/transcript.md"] == "process record or log"
    assert why["artifacts/process/variant_offline_diagnostics.log"] == "process record or log"
    assert why["loop.log"].startswith("outside the result folders")
    assert why["artifacts/croissant.json"].startswith("machine record")
    assert why["artifacts/extra/plan/v01.yaml"] == "an earlier plan version or the plan pointer"
    assert why["artifacts/extra/plan/current.json"] == "an earlier plan version or the plan pointer"
    assert why["artifacts/data/qc_metrics.json"] == "identical to results/tables/records/dataset_results.tsv"
    assert why["artifacts/extra/gene_expression/HRCA__RHO_LRAT/dotplot.png"] == "identical to results/figures/dotplot.png"
    assert folder.same_as["artifacts/extra/gene_expression/HRCA__RHO_LRAT/dotplot.png"] == "results/figures/dotplot.png"
    assert (folder.plan_state, folder.plan_versions, folder.executed) == ("approved", 3, True)
    # nothing of the run is unaccounted for: every file is in the report, is the plan, or has a reason
    placed = {e.origin for e in folder.entries} | set(why) | {"artifacts/report/summary.md", "artifacts/report/plan.md"}
    assert {p.relative_to(tmp_path / "6a9b4f99bbb5").as_posix() for p in (tmp_path / "6a9b4f99bbb5").rglob("*") if p.is_file()} <= placed


def test_summary_has_the_five_sections_within_the_length_and_links_that_resolve(tmp_path):
    run, dest = atlas_run(tmp_path / "runs"), tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    md = (dest / "summary.md").read_text()
    assert tuple(h for h in md.splitlines() if h.startswith("## ")) == tuple(f"## {s}" for s in R.SUMMARY_SECTIONS)
    narrative = md[: md.index("## Files")].split("\n", 1)[1]
    assert R.words(narrative) <= R.NARRATIVE_WORD_LIMIT
    assert "RHO is rod-specific" in md and "Same as the abstract" not in md and "Choices made" not in md
    assert "Descriptive only: no test was run." in md                              # the limitations survive
    for fact in ("`heca` version `v2026.11.1`", "`explore-atlas-expression` 0.0.1",
                 "`atlas-expression-exploration` 0.0.3", "`resolve_genes`, `submit_query`", "AiScientist v0.4.11"):
        assert fact in md, fact
    assert "[plan.yaml](plan.yaml)" in md and "(results/tables/by_class.csv)" in md
    # the readable plan's paths now point into the report
    plan_md = (dest / "plan.md").read_text()
    assert "`results/tables/by_class.csv`" in plan_md and "[plot](results/figures/dotplot.png)" in plan_md
    # a summary the caller wrote is used as given, and still held to the form
    dest2 = tmp_path / "report2"
    assert R.build_report_folder(run, dest2, key_results="RHO marks rods. See `tables/by_class.csv`.",
                                 limitations="One atlas.") == []
    md2 = (dest2 / "summary.md").read_text()
    assert "RHO marks rods. See `results/tables/by_class.csv`." in md2 and "One atlas." in md2


def test_cellqc_results_are_collected_from_both_roots_once(tmp_path):
    run, dest = cellqc_run(tmp_path / "runs"), tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    got = tree(dest)
    assert [p for p in got if p.startswith("results/cellqc/")] == [
        "results/cellqc/S1_doublet_summary.txt", "results/cellqc/manifest.tsv", "results/cellqc/metrics.csv",
        "results/cellqc/report.html", "results/cellqc/report_slides.pdf"]                 # one copy of each
    assert [p for p in got if p.startswith("results/datasets/")] == [
        "results/datasets/S1.processed.20261106-2019.7f585b5a73c3.h5ad",                 # outside artifacts/
        "results/datasets/cellqc/S1.h5ad"]
    assert "results/tables/cellqc_metrics.csv" not in got                                # = cellqc/metrics.csv
    assert "results/tables/cells.csv" in got and "results/figures/violin_cellqc_after_qc.png" in got
    assert [p for p in got if p.startswith("results/tables/records/")] == ["results/tables/records/result_dataset.tsv"]
    assert (dest / "results/datasets/cellqc/S1.h5ad").read_bytes() == b"H5AD-S1" * 10
    md = (dest / "summary.md").read_text()
    assert md.count("Doublet filtering needs review") == 1                               # a warning is said once
    assert "## Limitations\n\n- ⚠️ S1: Doublet filtering needs review." in md
    assert "`results/tables/cells.csv`" in md and "`results/cellqc/report.html`" in md   # old paths rewritten
    assert "`qc-snrna-with-cellqc` 1.2.0" in md and "16,750 cells, 33,696 genes" in md


def test_two_different_files_of_one_name_are_both_kept(tmp_path):
    run = cellqc_run(tmp_path / "runs")
    (run / "artifacts/result/metrics.csv").write_text("sample,kept\nS1,99999\n")          # no longer the same
    dest = tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    kept = sorted(p for p in tree(dest) if "metrics" in p and p.startswith("results/cellqc/"))
    assert kept == ["results/cellqc/metrics.csv", "results/cellqc/metrics__artifacts-result.csv"]
    assert {(dest / p).read_text() for p in kept} == {"sample,kept\nS1,16750\n", "sample,kept\nS1,99999\n"}


def test_a_plan_that_never_ran_is_reported_as_not_executed(tmp_path):
    run, dest = plan_only_run(tmp_path / "runs"), tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    assert tree(dest) == ["plan.md", "plan.yaml", "summary.md"]
    folder = R.plan_report_folder(run)
    assert (folder.plan_state, folder.plan_versions, folder.executed) == ("cancelled", 3, False)
    md = (dest / "summary.md").read_text()
    assert "This run was not executed: the plan was cancelled. 3 plan versions were drafted" in md
    assert "no analysis ran" in md and "Nothing was computed" in md
    assert R.PLAN_TRAILER not in (dest / "plan.yaml").read_text() and "_review" not in (dest / "plan.yaml").read_text()


def test_a_curation_keeps_its_tables_and_its_task_spec_as_the_plan(tmp_path):
    run, dest = curation_run(tmp_path / "runs"), tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    assert tree(dest) == [
        "plan.md", "plan.yaml",
        "results/tables/curation/dominant_negative_evidence.csv", "results/tables/curation/dominant_negative_genes.csv",
        "results/tables/curation_9ce59c8975/genes.csv", "results/tables/curation_9ce59c8975/hits.csv",
        "summary.md"]
    spec = yaml.safe_load((dest / "plan.yaml").read_text())
    assert spec == {"task": "dominant-negative retinal disease genes", "target_class": "dominant_negative"}
    md = (dest / "summary.md").read_text()
    assert "lists 32 genes (by claim: 28 stated, 4 contested)" in md
    assert "## Question\n\ndominant-negative retinal disease genes" in md


def test_an_unknown_json_result_is_carried_into_a_table_not_dropped(tmp_path):
    run = make_run(tmp_path / "runs", {
        "artifacts/extra/newtool/scores.json": json.dumps({"genes": [{"g": "RHO", "s": 0.5}, {"g": "LRAT", "s": None}],
                                                           "empty": [], "note": "a\tb"}),
        "artifacts/report/plan.yaml": PLAN,
    })
    dest = tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    table = (dest / "results/tables/records/newtool/scores.tsv").read_text()
    assert table.splitlines() == ["field\tvalue", "genes[0].g\tRHO", "genes[0].s\t0.5", "genes[1].g\tLRAT",
                                  "genes[1].s\tnull", "empty\t[]", 'note\t"a\tb"']
    assert R.flatten_json({"a": {"b": [1, {"c": "x"}]}, "d": {}}) == [("a.b[0]", "1"), ("a.b[1].c", "x"), ("d", "{}")]


def test_the_check_names_what_does_not_belong(tmp_path):
    run, dest = atlas_run(tmp_path / "runs"), tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    (dest / "results/extra/state.json").write_text("{}")
    (dest / "results/figures/workflow.png").write_bytes(b"x")
    (dest / "results/extra/second.yaml").write_text("a: 1\n")
    (dest / "process").mkdir()
    (dest / "process/transcript.md").write_text("x")
    (dest / "results/empty").mkdir()
    (dest / "plan.md").write_text("[gone](results/tables/missing.csv)\n")
    (dest / "summary.md").write_text((dest / "summary.md").read_text().replace("## Limitations", "## Caveats")
                                     + "\n" + "word " * 10)
    problems = R.check_report_folder(dest)
    for expected in ("expected exactly one YAML file, plan.yaml; found ['plan.yaml', 'results/extra/second.yaml']",
                     "JSON file in the report: results/extra/state.json",
                     "workflow file in the report: results/figures/workflow.png",
                     "process folder in the report: process/transcript.md",
                     "unexpected entry at the top of the report: process",
                     "empty folder: results/empty",
                     "plan.md links to results/tables/missing.csv, which is not in the report"):
        assert expected in problems, (expected, problems)
    assert any(p.startswith("summary.md sections are") for p in problems)
    long = "# t\n\n## Question\n\n" + "word " * 400 + "\n\n## Key results\n\nx\n\n## Method and inputs\n\nx\n\n## Limitations\n\nx\n\n## Files\n\n- a\n"
    assert R.check_summary(long) == ["summary.md has 410 words outside Files; the limit is 300"]


def test_shorten_cuts_at_sentences_and_counts_words_not_marks():
    text = "One two three. Four five six seven. Eight nine."
    assert R.shorten(text, 3) == "One two three."
    assert R.shorten(text, 7) == "One two three. Four five six seven."
    assert R.shorten(text, 100) == text
    assert R.shorten("alpha beta gamma delta epsilon", 2) == "alpha beta …"
    assert R.words("| a | b |\n|---|---:|\n| 1 | 2 |") == 4
    assert R.words("[the plot](results/figures/a_b_c.png) shows **RHO**") == 4


def test_symlinks_are_never_followed_and_a_used_destination_is_refused(tmp_path):
    outside = tmp_path / "secret.csv"
    outside.write_text("secret\n")
    run = make_run(tmp_path / "runs", {"artifacts/report/plan.yaml": PLAN, "artifacts/tables/a.csv": "a\n"})
    (run / "artifacts/tables/link.csv").symlink_to(outside)
    (run / "artifacts/linked").symlink_to(tmp_path)
    dest = tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    assert tree(dest) == ["plan.md", "plan.yaml", "results/tables/a.csv", "summary.md"]
    with pytest.raises(FileExistsError):
        R.build_report_folder(run, dest)


def test_a_model_written_narrative_is_kept_with_the_run_and_used_by_the_folder(tmp_path):
    run = atlas_run(tmp_path / "runs")
    (run / "artifacts/report/report.md").write_text(
        "# Report\n\nRHO is rod-specific: it marks rods (15.8 CP10k).\n", encoding="utf-8")
    seen = []

    def model(messages):
        seen.append(messages)
        return 'Here it is:\n{"key_results": "RHO marks rods (15.8 CP10k). ' + "More detail. " * 200 + \
               '", "limitations": "One atlas; descriptive only."}'

    key, limits = R.write_narrative(model, run)
    assert key.startswith("RHO marks rods (15.8 CP10k).") and R.words(key) <= R.KEY_RESULTS_WORDS
    assert limits == "One atlas; descriptive only."
    system, user = seen[0][0]["content"], seen[0][1]["content"]
    assert f"at most {R.KEY_RESULTS_WORDS} words" in system and "Return ONLY JSON" in system
    assert "where to click to download" in system                       # what the summary leaves out
    assert user.startswith("QUESTION\nWhere are LRAT and RHO expressed in this retina atlas?")
    assert "RHO is rod-specific" in user                                # what the run recorded
    kept = (run / "artifacts/report/narrative.md").read_text()
    assert kept.startswith("## Key results\n\nRHO marks rods") and "## Limitations\n\nOne atlas; descriptive only." in kept

    dest = tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    md = (dest / "summary.md").read_text()
    assert "RHO marks rods (15.8 CP10k)." in md and "One atlas; descriptive only." in md
    assert "RHO is rod-specific and LRAT is restricted" not in md       # not the run's long abstract
    assert not any("narrative" in p for p in tree(dest))                # the narrative is not a result


def test_a_reply_with_a_number_the_run_did_not_record_is_not_used(tmp_path):
    run = atlas_run(tmp_path / "runs")
    (run / "artifacts/report/report.md").write_text(
        "# Report\n\nRHO marks rods (15.8 CP10k) in 3,177,310 nuclei.\n", encoding="utf-8")
    why: list[str] = []
    made_up = '{"key_results": "RHO marks rods (15.8 CP10k) in 3177310 nuclei, 92% of them.", "limitations": "None."}'
    assert R.write_narrative(lambda m: made_up, run, problems=why) is None
    assert why == ["the model's summary has numbers the run did not record: 92"]
    assert not (run / "artifacts/report/narrative.md").exists()
    faithful = '{"key_results": "RHO marks rods (15.8 CP10k) in 3,177,310 nuclei.", "limitations": "One atlas."}'
    assert R.write_narrative(lambda m: faithful, run) is not None
    # digits inside names are not numbers; one number written two ways is the same number
    assert R.unsupported_numbers("log2FC, Ddx41, H5AD, v0.4.15, CP10k", "nothing") == []
    assert R.unsupported_numbers("31303 cells, 10 %", "31,303 cells at 10.0 %") == []
    # the markers of a numbered list are not numbers of the run; a count the record spells out is one it has
    assert R.unsupported_numbers("1. RHO marks rods.\n2. LRAT marks the RPE.\n 3) Both.", "RHO and LRAT") == []
    assert R.unsupported_numbers("In 2 samples, 5 genes.", "Two samples were compared.") == ["5"]
    listed = '{"key_results": "1. RHO marks rods (15.8 CP10k).\\n2. Seen in 3,177,310 nuclei.", "limitations": "None."}'
    assert R.write_narrative(lambda m: listed, run) is not None
    assert "do not round it" in R.NARRATIVE_SYSTEM


def test_a_reply_that_cannot_be_used_leaves_the_runs_own_summary(tmp_path):
    run = atlas_run(tmp_path / "runs")
    for reply in ("I cannot do that.", '{"key_results": ""}', '{"limitations": "x"}', "{not json}"):
        assert R.write_narrative(lambda m, reply=reply: reply, run) is None
    assert not (run / "artifacts/report/narrative.md").exists()
    assert R.parse_narrative('{"key_results": "A result."}') == ("A result.", "None recorded.")
    empty = make_run(tmp_path / "runs", {"artifacts/plan/v01.yaml": version("draft", 1)}, "nothingyet")
    assert R.write_narrative(lambda m: pytest.fail("no record, no call"), empty) is None


def test_the_zip_holds_the_folder_under_the_run_id(tmp_path):
    import io
    import zipfile

    run, dest = cellqc_run(tmp_path / "runs"), tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    buf = io.BytesIO()
    n = R.zip_report_folder(dest, buf, "7f585b5a73c3")
    with zipfile.ZipFile(buf) as z:
        names = z.namelist()
        assert n == len(names) == len(tree(dest)) and all(x.startswith("7f585b5a73c3/") for x in names)
        assert "7f585b5a73c3/summary.md" in names and "7f585b5a73c3/results/datasets/cellqc/S1.h5ad" in names
        assert z.getinfo("7f585b5a73c3/results/datasets/cellqc/S1.h5ad").compress_type == zipfile.ZIP_STORED
        assert z.getinfo("7f585b5a73c3/summary.md").compress_type == zipfile.ZIP_DEFLATED
        assert z.read("7f585b5a73c3/results/datasets/cellqc/S1.h5ad") == b"H5AD-S1" * 10


def test_other_top_level_folders_of_a_run_are_not_read(tmp_path):
    run = make_run(tmp_path / "runs", {"artifacts/report/plan.yaml": PLAN, "artifacts/tables/a.csv": "a\n",
                                       ".report-work/6a9b4f99bbb5/results/tables/a.csv": "a\n",
                                       "logs/hpc3/job.log": "x", ".bundle-1.zip": b"zip"})
    folder = R.plan_report_folder(run)
    assert sorted(e.dest for e in folder.entries) == ["plan.md", "plan.yaml", "results/tables/a.csv"]
    assert dict(folder.left_out) == {".bundle-1.zip": "outside the result folders (a run record or a log)"}


# The layout of a run since the native MCP migration, from the file lists of production runs of
# 2026-11-15 (a CellQC run with two calls, a DE run with one): a call's files under
# ``artifacts/extra/mcp/<server>/<call>/``, a second copy one ``mcp/`` level down, the record of each
# call beside its files and again per task, and the gateway's own copies in ``artifacts/``.
CELLQC_SUMMARY = (
    "**Quality control done** — `run_cellqc`.\n\n"
    "- **31,303 of 34,553 cells kept (90.6%)**; 33,696 genes.\n"
    "- ⚠️ Sample1_WT: Doublet filtering needs review; see the technical details in the Downloads panel.\n\n"
    "**Result:** `qc.processed.h5ad` — 31,303 cells × 33,696 genes; `X` is log-normalised. "
    "It is kept on HPC3 until the session ends, then with the run on the server — Runs → “Save to my data” keeps it.\n"
    "Optional technical details in Downloads: `tables/cells.csv`, `result/report.html`.\n"
)


def mcp_cellqc_run(root: Path) -> Path:
    files: dict[str, str | bytes] = {
        "manifest.json": json.dumps({"app": {"tag": "v0.4.16"}}),
        "artifacts/report/plan.yaml": PLAN,
        "artifacts/report/summary.md": CELLQC_SUMMARY,
        "artifacts/data/result_dataset.json": json.dumps({
            "name": "qc.processed.h5ad", "n_cells": 31303, "n_genes": 33696,
            "saved_to": "/data/users/someone/datasets/x/original/qc.processed.h5ad"}),
        "artifacts/extra/mcp/.compatibility.json": "{}",
        "artifacts/tables/cells.csv": "cell,kept\na,1\n",
    }
    for call, html in (("default", "<html>first call</html>"), ("both_samples", "<html>both samples</html>")):
        base = f"artifacts/extra/mcp/cellqc/{call}"
        record = json.dumps({"status": "ok", "call": call})
        for prefix in (base, base.replace("extra/mcp/", "extra/mcp/mcp/")):
            files.update({
                f"{prefix}/result/report.html": html,
                f"{prefix}/result/metrics.csv": f"sample,cells\n{call},1\n",
                f"{prefix}/result/{call}.processed.h5ad": f"h5ad of {call}".encode(),
                f"{prefix}/figures/violin_cellqc_after_qc.png": f"png of {call}".encode(),
                f"{prefix}/tables/cellqc_metrics.csv": f"metric,{call}\n",
                f"{prefix}/run_cellqc.json": record,
                f"{prefix}/tasks/qc-{call}/run_cellqc.json": record,
            })
    files.update({"artifacts/result/report.html": "<html>both samples</html>",
                  "artifacts/result/metrics.csv": "sample,cells\nboth_samples,1\n"})
    return make_run(root, files, name="c2b4d03d524b")


def test_mcp_results_go_under_their_server_and_cellqc_sets_stay_whole(tmp_path):
    run = mcp_cellqc_run(tmp_path)
    dest = tmp_path / "out"
    assert R.build_report_folder(run, dest) == []
    got = tree(dest)
    # Two CellQC calls: one folder per call, each set whole; the gateway's copy and the second copy
    # one mcp/ level down are the same files, kept once.
    for call in ("default", "both_samples"):
        assert f"results/cellqc/{call}/report.html" in got
        assert f"results/cellqc/{call}/metrics.csv" in got
        assert f"results/datasets/cellqc/{call}/{call}.processed.h5ad" in got
        assert f"results/figures/cellqc/{call}/violin_cellqc_after_qc.png" in got
        assert f"results/tables/cellqc/{call}/cellqc_metrics.csv" in got
        assert f"results/tables/records/cellqc/{call}/run_cellqc.tsv" in got
    assert not [p for p in got if p.startswith(("results/cellqc/report", "results/cellqc/metrics"))]
    assert not [p for p in got if "/mcp/" in p or "/tasks/" in p or "default/result" in p]


def test_a_server_called_once_has_no_call_folder():
    assert R.classify("artifacts/extra/mcp/scrna-analysis/default/figures/volcano_Rod.png") == (
        "result", "results/figures/scrna-analysis/volcano_Rod.png")
    assert R.classify("artifacts/extra/mcp/scrna-analysis/default/tables/de_all.csv") == (
        "result", "results/tables/scrna-analysis/de_all.csv")
    assert R.classify("artifacts/extra/mcp/cellqc/default/result/report.html") == (
        "result", "results/cellqc/report.html")
    assert R.classify("artifacts/extra/mcp/cellqc/a/result/report.html", multi_call=frozenset({"cellqc"})) == (
        "result", "results/cellqc/a/report.html")
    assert R._record_dest("artifacts/extra/mcp/scrna-analysis/default/tables/de_significance.json") == (
        "results/tables/records/scrna-analysis/de_significance.tsv")


def test_the_summary_says_nothing_about_where_to_click_or_how_long_a_file_is_kept(tmp_path):
    run = mcp_cellqc_run(tmp_path)
    dest = tmp_path / "out"
    assert R.build_report_folder(run, dest) == []
    md = (dest / "summary.md").read_text(encoding="utf-8")
    for said in ("Downloads", "Save to my data", "Runs →", "until the session ends", "kept on HPC3",
                 "Optional technical details"):
        assert said not in md
    assert "31,303 of 34,553 cells kept (90.6%)" in md
    assert "⚠️ Sample1_WT: Doublet filtering needs review." in md
    assert "`X` is log-normalised." in md
    # The dataset was saved into the owner's data, so it is not in the folder, and the summary says so.
    assert "`qc.processed.h5ad` (31,303 cells, 33,696 genes); saved in the owner's data, not in this folder." in md


def test_strip_ui_text_keeps_the_result_and_drops_the_lines_about_the_website():
    assert R.strip_ui_text("A. See the Downloads panel. B.") == "A. B."
    assert R.strip_ui_text("- ⚠️ X needs review; see the optional technical details.") == "- ⚠️ X needs review."
    assert R.strip_ui_text("Optional technical details in Downloads: `a.csv`.\nKept.") == "Kept."
    assert R.strip_ui_text("Nothing about the site here.") == "Nothing about the site here."


def test_a_json_record_too_large_for_a_table_is_named_so(tmp_path, monkeypatch):
    run = make_run(tmp_path, {"artifacts/report/plan.yaml": PLAN,
                              "artifacts/extra/big/values.json": json.dumps({"v": list(range(50))})})
    monkeypatch.setattr(R, "JSON_LIMIT", 20)
    folder = R.plan_report_folder(run)
    why = dict(folder.left_out)["artifacts/extra/big/values.json"]
    assert "too large to carry into a table" in why
    # ... and the summary says so: a result is never left out silently, and the folder has no JSON
    dest = tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    files = (dest / "summary.md").read_text().split("## Files")[1]
    assert "Not in this folder: `artifacts/extra/big/values.json`" in files and "too large to carry" in files
    assert not any(p.endswith(".json") for p in tree(dest))


def test_the_written_reports_links_lead_to_its_files_in_the_folder(tmp_path):
    # report.md names its figures from the run's folder (the renderer reads them there); in the
    # report folder it is in results/report/, so its links are named from there. An MCP server's
    # report links into extra/ (moved by the bundle cleanup), or, from before that was followed,
    # into the folder's old place.
    import re
    report = ("# Report\n\n![umap](figures/umap.png)\n\n![dot](extra/gene_expression/q1/dot.png)\n\n"
              "![old](gene_expression/q2/dot.png)\n\n[the table](tables/a.csv), `figures/umap.png`, "
              "[atlas](https://example.org/x), ![gone](figures/absent.png)\n")
    run = make_run(tmp_path, {"artifacts/report/plan.yaml": PLAN,
                              "artifacts/report/report.md": report,
                              "artifacts/report/report.pdf": b"%PDF-1.4 report",
                              "artifacts/figures/umap.png": b"\x89PNG umap",
                              "artifacts/extra/gene_expression/q1/dot.png": b"\x89PNG q1",
                              "artifacts/extra/gene_expression/q2/dot.png": b"\x89PNG q2",
                              "artifacts/tables/a.csv": "a\n1\n"})
    dest = tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    md = (dest / "results/report/report.md").read_text()
    targets = re.findall(r"\]\(([^)\s]+)\)", md)
    assert targets == ["../figures/umap.png", "../figures/gene_expression/q1/dot.png",
                       "../figures/gene_expression/q2/dot.png", "../tables/a.csv", "https://example.org/x",
                       "figures/absent.png"]
    assert all((dest / "results/report" / t).is_file() for t in targets[:4])
    # only the links change: the text is the PDF's
    assert "`figures/umap.png`" in md and md.startswith("# Report\n")
    assert (run / "artifacts/report/report.md").read_text() == report


def test_the_written_report_has_its_own_folder_and_leads_the_file_index(tmp_path):
    run = make_run(tmp_path, {"artifacts/report/plan.yaml": PLAN,
                              "artifacts/report/report.md": "# Report\n\nThe answer.\n",
                              "artifacts/report/report.pdf": b"%PDF-1.4 report",
                              "artifacts/tables/a.csv": "a\n1\n"})
    dest = tmp_path / "report"
    assert R.build_report_folder(run, dest) == []
    assert tree(dest) == ["plan.md", "plan.yaml", "results/report/report.md", "results/report/report.pdf",
                          "results/tables/a.csv", "summary.md"]
    files = (dest / "summary.md").read_text().split("## Files")[1]
    assert files.index("`results/report/`") < files.index("`results/tables/`")
