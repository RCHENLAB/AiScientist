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

from bioagent.gateway.app import _dataset_section, _insert_record_sections, _pipeline_section


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
    from bioagent.agents.research_lab import CriticVerdict, LabRound
    rnd = LabRound(round_no=1, step_index=0, step="QC", specialist="s",
                   scientist_result={"steps": [{"tool": "run_scanpy_qc", "ok": True,
                                                "args": {"max_pct_mt": 5.0},
                                                "result": {"status": "ok"}}]},
                   verdict=CriticVerdict(verdict="accept", score=0.9, critique=""))
    out = _pipeline_section(SimpleNamespace(rounds=[rnd]))
    assert "## What was run" in out and "`run_scanpy_qc`" in out
    assert "`max_pct_mt` = `5.0`" in out and "chosen for this run" in out
