"""The two sections a researcher needs are built from the record, not asked for.

The report handed to the researcher named no tool, stated no parameter, and described the input
only as "an AnnData matrix containing 15,307 cells" — so the one question a reader has ("what did
the code actually do to my data?") was answerable only by opening technical_report.md, which is
not written for them. Asking the writer model for these sections is not enough: a model can
paraphrase a tool list into vagueness, drop it for length, or name a tool that never ran. These
are assembled from the execution record instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

# The precondition is that gateway.app IMPORTS — it pulls fastapi and paramiko at module scope,
# and guarding on one of them only moves the failure to the other. Without a guard the import
# raises during COLLECTION, which pytest treats as fatal and aborts the whole session: unguarded
# modules took CI from 1,525 passing tests to "33 skipped, 3 errors" and kept main red from
# 2026-08-20 to 2026-09-08. CI installs the gateway extra so these actually RUN; the guard is
# what keeps a leaner environment skipping cleanly instead of taking every other test down.
pytest.importorskip("aiscientist.gateway.app")

from aiscientist.gateway.app import (  # noqa: E402
    _dataset_section, _insert_record_sections, _pipeline_section,
)


def _bundle(tmp_path: Path, dr: dict) -> Path:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "dataset_results.json").write_text(json.dumps(dr), encoding="utf-8")
    return tmp_path


def _result(steps: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(rounds=[{"scientist_result": {"steps": steps}}])


_DR = {
    "dataset_path": "/u/Ddx41_DEG.h5ad", "cells": 15307, "genes": 33696,
    "dataset_kind": "h5ad_single_cell",
    "obs_categoricals": {
        "sampleid": {"n": 2, "values": ["DDX41", "WT"]},
        "orig.ident": {"n": 1, "values": ["0"]},
        "majorclass": {"n": 11, "values": ["AC", "BC", "Cone", "Endothelial", "HC", "MG",
                                           "Microglia", "Pericyte", "RGC", "RPE", "Rod"]},
    },
}


def test_the_dataset_section_states_the_replication(tmp_path):
    """The number that decides which conclusions are legal, and the one the manuscript omitted."""
    out = _dataset_section(_bundle(tmp_path, _DR), _result([]))
    assert "15,307 cells" in out and "33,696 genes" in out
    assert "`sampleid`" in out and "DDX41" in out
    assert "no biological replication" in out, "one library must be stated, not left to inference"
    assert "descriptive" in out
    assert "RGC" in out and "Rod" in out, "all 11 labels — a silent truncation loses the skipped ones"


def test_the_dataset_section_says_when_qc_removed_nothing(tmp_path):
    out = _dataset_section(_bundle(tmp_path, _DR), _result([
        {"tool": "run_scanpy_qc", "ok": True,
         "result": {"status": "ok", "cells_before": 15307, "cells_after": 15307,
                    "genes_after": 22387}}]))
    assert "15,307 of 15,307 cells kept (0 removed)" in out
    assert "already been quality-controlled" in out


def test_the_pipeline_section_explains_each_tool_and_flags_chosen_values(tmp_path):
    out = _pipeline_section(_result([
        {"tool": "run_scanpy_qc", "ok": True, "args": {"max_pct_mt": 10},
         "result": {"status": "ok"}}]))
    assert "`run_scanpy_qc`" in out
    assert "mitochondrial" in out, "the tool must be described, not just named"
    assert "`max_pct_mt` = `10`" in out
    assert "chosen for this run" in out, "a value the model picked must not read as the default"


def test_a_refused_call_is_not_reported_as_what_ran(tmp_path):
    """`ok` means the executor did not raise; a tool returning {"status": "error"} still has it.
    In the real bundle the first run_de was REFUSED with force=false and the analysis came from a
    later force=true call — reporting the first would tell the reader the guard was respected."""
    out = _pipeline_section(_result([
        {"tool": "run_de", "ok": True, "args": {"groupby": "sampleid", "force": False},
         "result": {"status": "error", "error": "refused"}},
        {"tool": "run_de", "ok": True, "args": {"groupby": "sampleid", "force": True},
         "result": {"status": "ok"}}]))
    assert "`force` = `True`" in out
    assert "`force` = `False`" not in out


def test_the_sections_land_after_the_abstract():
    body = "# T\n\n## Abstract\n\nabs\n\n## Results\n\nres\n"
    out = _insert_record_sections(body, "## The dataset\n\nd", "## What was run\n\nw")
    assert out.index("## Abstract") < out.index("## The dataset") < out.index("## Results")


def test_nothing_is_inserted_when_there_is_no_record(tmp_path):
    assert _dataset_section(tmp_path, _result([])) == ""
    assert _pipeline_section(_result([])) == ""
    assert _insert_record_sections("# T\n\nbody", "", "") == "# T\n\nbody"


def test_the_pipeline_section_reads_real_LabRound_objects_not_only_dicts():
    """Production hands LabRound dataclasses; the section read only the dict shape and came out
    EMPTY in every real report (verified on a live run: '## The dataset' present, '## What was
    run' absent). The tests all passed because they built dict rounds."""
    from aiscientist.agents.research_lab import CriticVerdict, LabRound
    rnd = LabRound(round_no=1, step_index=0, step="QC", specialist="s",
                   scientist_result={"steps": [{"tool": "run_scanpy_qc", "ok": True,
                                                "args": {"max_pct_mt": 5.0},
                                                "result": {"status": "ok"}}]},
                   verdict=CriticVerdict(verdict="accept", score=0.9, critique=""))
    out = _pipeline_section(SimpleNamespace(rounds=[rnd]))
    assert "## What was run" in out and "`run_scanpy_qc`" in out
    assert "`max_pct_mt` = `5.0`" in out and "chosen for this run" in out


# --- a folder of Cell Ranger libraries ------------------------------------------------------------
# Prod run c57071e7dc94 bound a whole Cell Ranger delivery, and its report said only "**Input.**
# `dataset` — single_cell_other (single_cell_other).": the run-start profile cannot parse a 10x .h5,
# and the layout recognised afterwards stayed in memory, never reaching data/dataset_results.json.

def _delivery(root: Path) -> Path:
    """Two fake libraries that differ in everything the section reports."""
    one = root / "Sample1_WT" / "outs"
    (one / "raw_feature_bc_matrix").mkdir(parents=True)
    (one / "filtered_feature_bc_matrix").mkdir()
    (one / "analysis").mkdir()
    for name in ("raw_feature_bc_matrix.h5", "filtered_feature_bc_matrix.h5", "metrics_summary.csv",
                 "possorted_genome_bam.bam", "possorted_genome_bam.bam.bai"):
        (one / name).write_bytes(b"")
    two = root / "Sample2_KO"          # a 10x Cloud download: packed directories, no BAM, no analysis/
    two.mkdir()
    for name in ("raw_feature_bc_matrix.tar.gz", "filtered_feature_bc_matrix.h5",
                 "filtered_feature_bc_matrix.tar.gz"):
        (two / name).write_bytes(b"")
    return root


def _cellqc_round(libs: list[dict]):
    from aiscientist.agents.research_lab import CriticVerdict, LabRound
    before = sum(lib["cells_cellranger"] or 0 for lib in libs)
    return SimpleNamespace(rounds=[LabRound(
        round_no=1, step_index=0, step="QC", specialist="s",
        scientist_result={"steps": [{"tool": "run_cellqc", "ok": True, "args": {}, "result": {
            "status": "ok", "libraries": libs, "n_libraries": len(libs), "cells_before": before,
            "cells_after": sum(lib["cells_after_doublets"] for lib in libs), "genes_after": 21000}}]},
        verdict=CriticVerdict(verdict="accept", score=0.9, critique=""))])


_LIBS = [{"sample": "Sample1_WT", "cells_cellranger": 7000, "cells_after_filter": 6900,
          "cells_after_doublets": 6200},
         {"sample": "Sample2_KO", "cells_cellranger": 6559, "cells_after_filter": 6450,
          "cells_after_doublets": 5815}]


def test_a_cell_ranger_delivery_is_named_in_the_dataset_section(tmp_path):
    from aiscientist.gateway.app import _cellranger_layout_local, _record_input_layout
    from aiscientist.tools.datasets import run_dataset_smoke_analysis

    folder = _delivery(tmp_path / "CellQC_testdata")
    art = tmp_path / "artifacts"
    primary = folder / "Sample1_WT" / "outs" / "filtered_feature_bc_matrix.h5"
    decisions = {"dataset_result": run_dataset_smoke_analysis(primary, art / "data")["result"]}
    assert _record_input_layout(decisions, _cellranger_layout_local(folder), art / "data")

    on_disk = json.loads((art / "data" / "dataset_results.json").read_text(encoding="utf-8"))
    assert on_disk["cellranger_layout"]["n_libraries"] == 2, "the layout must reach the profile on disk"

    out = _dataset_section(art, _cellqc_round(_LIBS))
    input_line = next(ln for ln in out.splitlines() if ln.startswith("**Input.**"))
    assert "`CellQC_testdata/`" in input_line
    assert "2 libraries (`Sample1_WT`, `Sample2_KO`)" in input_line
    assert "13,559 cells as called by Cell Ranger, summed over the 2 libraries CellQC read" in input_line
    assert "genes" not in input_line, "CellQC reports no pre-QC gene count; none may be made up"
    assert "single_cell_other" not in out
    assert "| Sample1_WT | `.h5`, directory | `.h5`, directory | yes | yes | yes |" in out
    assert "| Sample2_KO | `.tar.gz` | `.h5`, `.tar.gz` | no | no | no |" in out
    assert "10x Cloud" in out, "a packed matrix directory must be explained"
    # The QC paragraph, from a LabRound (the production shape) — it used to read dicts only.
    assert "12,015 of 13,559 cells kept (1,544 removed) and 21,000 genes retained" in out
    assert "Sample2_KO: 6559 → 6450 → 5815" in out


def test_a_delivery_without_counts_gets_none_invented(tmp_path):
    """No QC step, or one library CellQC could not count: the delivery is described, sized never."""
    layout = {"root": "/data/Users/shared/jinl14/CellQC_testdata/Sample3_GSM5676874", "n_libraries": 1,
              "libraries": [{"sample": "Sample3_GSM5676874", "raw": ["h5"], "filtered": ["h5", "dir"],
                             "bam": True, "clusters": False, "metrics": True, "packed": False}]}
    art = _bundle(tmp_path, {"dataset_kind": "single_cell_other", "cellranger_layout": layout})
    out = _dataset_section(art, _result([]))
    assert ("**Input.** `Sample3_GSM5676874/` — a 10x Cell Ranger delivery of 1 library "
            "(`Sample3_GSM5676874`).") in out
    assert "| Sample3_GSM5676874 | `.h5` | `.h5`, directory | yes | no | yes |" in out
    assert "cells" not in out

    uncounted = [dict(_LIBS[0], cells_cellranger=None), _LIBS[1]]
    input_line = next(ln for ln in _dataset_section(art, _cellqc_round(uncounted)).splitlines()
                      if ln.startswith("**Input.**"))
    assert "cells" not in input_line, "a sum over a library with no count is a floor, not the size"


def test_a_single_10x_h5_takes_its_size_from_the_qc_step(tmp_path):
    art = _bundle(tmp_path, {"dataset_kind": "single_cell_other", "format": ".h5",
                             "dataset_path": "/staged/filtered_feature_bc_matrix.h5"})
    out = _dataset_section(art, _result([
        {"tool": "run_scanpy_qc", "ok": True,
         "result": {"status": "ok", "cells_before": 8000, "genes_before": 36601,
                    "cells_after": 7400, "genes_after": 21000}}]))
    assert ("**Input.** `filtered_feature_bc_matrix.h5` — single_cell_other; 8,000 cells x 36,601 "
            "genes, as counted by the QC step.") in out
