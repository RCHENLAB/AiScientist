"""Tests for the categorized process-artifact writer (Virtual-Lab meeting record)."""

from __future__ import annotations

import json

from bioagent.tools.research_bundle import write_process_artifacts

_RESULT = {
    "question": "Characterize the PBMC dataset",
    "agenda": ["Run QC", "Cluster and find markers"],
    "converged": True,
    "accepted_steps": 2,
    "final_answer": "Ten clusters with canonical T-cell and myeloid markers.",
    "rounds": [
        {"round_no": 1, "step": "Run QC",
         "scientist_result": {"final_answer": "2698 cells pass QC",
                              "steps": [{"tool": "run_scanpy_qc"}]},
         "verdict": {"verdict": "accept", "score": 0.9, "critique": "grounded"}},
        {"round_no": 2, "step": "Cluster and find markers",
         "scientist_result": {"final_answer": "10 Leiden clusters",
                              "steps": [{"tool": "run_clustering"}, {"tool": "run_de"}]},
         "verdict": {"verdict": "accept", "score": 0.95, "critique": "ok"}},
    ],
}


def test_writes_classified_process_files(tmp_path):
    written = write_process_artifacts(_RESULT, tmp_path / "process")
    names = {p.name for p in written}
    assert {"agenda.json", "lab_result.json", "round_01.json", "round_02.json", "transcript.md"} <= names

    agenda = json.loads((tmp_path / "process" / "agenda.json").read_text())
    assert agenda["agenda"] == ["Run QC", "Cluster and find markers"]
    # per-round JSON carries the Scientist + Critic record
    r2 = json.loads((tmp_path / "process" / "round_02.json").read_text())
    assert r2["verdict"]["verdict"] == "accept" and r2["step"] == "Cluster and find markers"


def test_transcript_is_human_readable(tmp_path):
    write_process_artifacts(_RESULT, tmp_path / "process")
    md = (tmp_path / "process" / "transcript.md").read_text(encoding="utf-8")
    assert "# Research transcript" in md
    assert "## Agenda (PI)" in md and "1. Run QC" in md
    assert "Round 1 — Run QC" in md and "run_scanpy_qc" in md          # tools listed
    assert "ACCEPT" in md and "Final report (PI synthesis)" in md
    assert "converged=True" in md and "accepted_steps=2/2" in md
