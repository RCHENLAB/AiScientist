"""A run whose analysis was done in generic code is not a QC run, and its short report carries it.

Prod run 74873590ca7b (2026-10-23, Claude Haiku 5.5): "Score every cell for an interferon-stimulated
gene signature and compare the score between DDX41 and WT within each major cell class. Write a short
report." It ran `run_scanpy_qc`, then three `run_code` steps (a vetted skill) that wrote ten tables
and three figures. Because `run_code` is not an analysis tool, the run counted as QC-only: the writer
was told "This run did quality control only", the claim audit was skipped, the short report was titled
"Quality-control summary" with none of the ISG results, and the exported dataset had no score.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.research_lab import CriticVerdict, LabConfig, LabRound, ResearchLab
from aiscientist.agents.scope import code_wrote_outputs, qc_only, qc_only_run, tools_that_ran
from aiscientist.gateway.app import _build_brief_report, _code_step_outputs, _wants_brief_report

QUESTION = ("Score every cell for an interferon-stimulated gene signature and compare the score between "
            "DDX41 and WT within each major cell class. Write a short report.")
QC_RESULT = {"status": "ok", "cells_before": 15307, "cells_after": 15307, "genes_after": 22387,
             "thresholds": {"min_genes": 200, "min_cells": 3, "max_pct_mt": 5.0}}
SCORE = "**ISG signature score per nucleus** — Apply the skill `score-gene-signatures-with-scanpy` with `run_code`."
COMPARE = "**Per-class DDX41 vs WT comparison** — Within each `majorclass`, summarise the score by arm."
FIGS = "**Score figures per class** — Produce with `run_code` a UMAP and a violin plot."


def _hpc(**written):
    return {"status": "ok", "execution_mode": "hpc_slurm", "stdout": "ok", **written}


def _round(i, step, steps, verdict="accept"):
    return {"step_index": i, "step": step, "verdict": {"verdict": verdict, "score": 0.8},
            "scientist_result": {"final_answer": f"step {i} done", "steps": steps}}


def _rounds():
    """The four steps of run 74873590ca7b, with the files each one reported (round_0N.json)."""
    return [
        _round(1, "**QC & normalization** — Run `run_scanpy_qc`.",
               [{"tool": "run_scanpy_qc", "ok": True, "result": QC_RESULT}]),
        _round(2, SCORE, [
            {"tool": "run_code", "ok": True, "result": _hpc()},          # read the checkpoint only
            {"tool": "run_code", "ok": True, "result": _hpc(tables=[
                "tables/isg_gene_detection_per_arm.csv", "tables/isg_scores_per_nucleus.csv"])},
            {"tool": "run_code", "ok": True, "result": _hpc(tables=["tables/isg_ddx41_vs_wt_by_majorclass.csv"])},
            {"tool": "run_code", "ok": True, "result": _hpc(figures=["figures/signature_ISG_by_majorclass_arm.png"])}]),
        _round(3, COMPARE, [{"tool": "run_code", "ok": True, "result": _hpc(
            tables=["tables/isg_ddx41_vs_wt_cliffs_raw_vs_depthbinned.csv"])}]),
        _round(4, FIGS, [{"tool": "run_code", "ok": True, "result": _hpc(
            figures=["figures/isg_umap_by_arm.png", "figures/isg_violin_by_majorclass_arm.png"])}]),
    ]


ANSWER = """# Report: ISG Signature Score by Major Cell Class, DDX41 vs WT

**Status:** Descriptive effect sizes only. There is one library per arm, so no condition-level p-value is valid.

## Summary

- All 15,307 nuclei were scored with a 14-gene interferon-stimulated gene set.
- MG nuclei (787 DDX41, 535 WT) show the clearest DDX41 excess: median difference 0.094, Cliff's delta 0.379.
- See `tables/isg_ddx41_vs_wt_by_majorclass.csv`.

![ISG score by class](figures/signature_ISG_by_majorclass_arm.png)

Figures: `figures/isg_umap_by_arm.png` and `figures/isg_violin_by_majorclass_arm.png`.
"""


def _result(rounds=None, answer=ANSWER):
    return types.SimpleNamespace(rounds=_rounds() if rounds is None else rounds, final_answer=answer,
                                 agenda=[r["step"] for r in (_rounds() if rounds is None else rounds)])


def _art(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    for sub in ("data", "tables", "figures", "report"):
        (art / sub).mkdir(parents=True)
    (art / "data" / "dataset_results.json").write_text(json.dumps({
        "dataset_path": "/data/Ddx41_DEG.h5ad", "cells": 15307, "genes": 33696}))
    (art / "tables" / "cells.csv").write_text("barcode,sampleid,majorclass\n")
    (art / "tables" / "isg_ddx41_vs_wt_by_majorclass.csv").write_text(
        "majorclass,n_DDX41,n_WT,median_diff,cliffs_delta\nMG,787,535,0.094,0.379\n")
    for name in ("isg_gene_detection_per_arm", "isg_scores_per_nucleus",
                 "isg_ddx41_vs_wt_cliffs_raw_vs_depthbinned"):
        (art / "tables" / f"{name}.csv").write_text("a\n1\n")
    for name in ("signature_ISG_by_majorclass_arm", "isg_umap_by_arm", "isg_violin_by_majorclass_arm",
                 "violin_qc_violin", "workflow"):
        (art / "figures" / f"{name}.png").write_bytes(b"\x89PNG")
    return art


# --- what counts as QC only ---------------------------------------------------------------------------

def test_code_that_wrote_tables_is_analysis_and_the_run_is_not_qc_only():
    rounds = _rounds()
    assert qc_only(tools_that_ran(rounds)), "by its tools alone, the run looks like QC"
    assert code_wrote_outputs(rounds) and not qc_only_run(rounds)
    read_only = [_round(1, "QC", [{"tool": "run_scanpy_qc", "ok": True, "result": QC_RESULT},
                                  {"tool": "run_code", "ok": True, "result": _hpc()}])]
    assert qc_only_run(read_only), "a script that wrote nothing is a check, and the run stays QC"


def test_a_qc_run_with_code_analysis_still_gets_the_short_report_not_a_manuscript():
    """Run 1e887ed2bf8e (CellQC plus a run_code audit) keeps its short report."""
    assert _wants_brief_report(None, _result())


# --- the write-up -------------------------------------------------------------------------------------

def _lab_round(i, step, steps):
    return LabRound(1, i, step, "sci", {"final_answer": f"step {i} done", "steps": steps},
                    CriticVerdict("accept", 0.8, ""))


def test_the_writer_is_not_told_the_run_did_qc_only_and_the_claim_audit_runs(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_CLAIM_AUDIT", "1")
    calls: list = []

    def complete(messages):
        calls.append(messages)
        if "CANDIDATE HEADLINE FINDINGS" in messages[0]["content"]:
            return json.dumps({"claims": []})
        return "# Report"

    profile = {"dataset_kind": "single_cell",
               "obs_categoricals": {"orig.ident": {"n": 1, "values": ["0"]},
                                    "sampleid": {"n": 2, "values": ["DDX41", "WT"]}},
               "design_by_arm": {"condition_column": "sampleid", "label_column": "majorclass",
                                 "cells_by_arm": {"DDX41": 6260, "WT": 9047},
                                 "qc_median_by_arm": {"nCount_RNA": {"DDX41": 3078.0, "WT": 1916.0}}}}
    lab = ResearchLab(HarnessContext(decisions={"dataset_result": profile}, tunnel_port=1, model="m"),
                      LabConfig(), complete_fn=complete)
    rounds = [_lab_round(r["step_index"], r["step"], r["scientist_result"]["steps"]) for r in _rounds()]
    events: list = []
    lab._synthesize(QUESTION, rounds, events.append)
    writer = [m for m in calls if "Principal Investigator writing the final research report" in m[0]["content"]]
    assert writer, "the writer wrote the answer"
    assert "This run did quality control only" not in writer[-1][1]["content"]
    assert [e for e in events if e.get("type") == "claim_audit"], "the claim audit ran"
    assert not [e for e in events if e.get("qc_only")]


# --- the short report ---------------------------------------------------------------------------------

def test_the_short_report_carries_the_code_steps_results(tmp_path):
    art = _art(tmp_path)
    md = _build_brief_report(art, QUESTION, _result())
    assert md.startswith("# Report: ISG Signature Score by Major Cell Class, DDX41 vs WT\n")
    assert "Quality-control summary" not in md and "quality control only" not in md
    assert f"**Request.** {QUESTION}" in md.split("\n## ", 1)[0]
    assert "median difference 0.094, Cliff's delta 0.379" in md
    # The figures the code made that the answer does not show, each with the step that made it.
    # Named in the answer's text but not shown there (as in 74873590ca7b): still embedded.
    assert "![Isg umap by arm (step 4: Score figures per class)](figures/isg_umap_by_arm.png)" in md
    assert "](figures/isg_violin_by_majorclass_arm.png)" in md
    assert md.count("figures/signature_ISG_by_majorclass_arm.png") == 1, "shown once, where the answer put it"
    assert "violin_qc_violin" not in md and "workflow.png" not in md, "only the analysis code's figures"
    # Each table the answer does not name, with the step that wrote it.
    assert ("- `tables/isg_ddx41_vs_wt_cliffs_raw_vs_depthbinned.csv` — step 3: "
            "Per-class DDX41 vs WT comparison.") in md
    assert "- `tables/isg_scores_per_nucleus.csv` — step 2: ISG signature score per nucleus." in md
    assert "- `tables/isg_ddx41_vs_wt_by_majorclass.csv`" not in md, "the answer already names it"
    assert "## Files" in md


def test_its_numbers_are_traced_to_the_tables(tmp_path):
    from aiscientist.reporting.verification import build_verification
    art = _art(tmp_path)
    result = _result()
    md = _build_brief_report(art, QUESTION, result)
    _statement, summary = build_verification(art, md, result.agenda, result.rounds,
                                             {"citations": [], "reason": "none"}, brief=True)
    assert summary["numbers_checked"] >= 5, "the answer's numbers are in the report and checked"
    assert summary["numbers_untraced"] == 0 and summary["numbers_inconsistent"] == 0, summary["untraced"]


def test_a_rejected_steps_files_are_not_presented_as_results(tmp_path):
    rounds = _rounds()
    rounds[3]["verdict"]["verdict"] = "revise"
    md = _build_brief_report(_art(tmp_path), QUESTION, _result(rounds))
    assert "](figures/isg_umap_by_arm.png)" not in md, "not embedded as a result"
    assert [p for _n, _t, p in _code_step_outputs(_result(rounds))] == [
        "tables/isg_gene_detection_per_arm.csv", "tables/isg_scores_per_nucleus.csv",
        "tables/isg_ddx41_vs_wt_by_majorclass.csv", "figures/signature_ISG_by_majorclass_arm.png",
        "tables/isg_ddx41_vs_wt_cliffs_raw_vs_depthbinned.csv"]


def test_with_no_written_answer_the_code_built_report_does_not_call_it_qc(tmp_path):
    md = _build_brief_report(_art(tmp_path), QUESTION, _result(answer=""))
    assert md.startswith("# Analysis summary") and "quality control only" not in md


# --- the processed dataset ------------------------------------------------------------------------------

def test_the_exported_dataset_carries_a_score_a_step_saved_under_its_own_name(tmp_path):
    ad = pytest.importorskip("anndata")
    np = pytest.importorskip("numpy")
    sp = pytest.importorskip("scipy.sparse")
    from aiscientist.tools._lib.result_dataset import export
    ws = tmp_path / "run"
    (ws / "work").mkdir(parents=True)
    counts = sp.csr_matrix(np.random.default_rng(0).poisson(2, (40, 12)).astype(np.float32))
    qc = ad.AnnData(sp.csr_matrix(np.log1p(counts.toarray())))
    qc.obs_names = [f"N{i:03d}" for i in range(40)]
    qc.var_names = [f"G{i}" for i in range(12)]
    qc.layers["counts"] = counts
    qc.write_h5ad(ws / "work" / "adata_qc.h5ad")
    scored = qc.copy()
    scored.obs["score_ISG"] = np.linspace(-0.1, 0.3, 40)
    scored.write_h5ad(ws / "work" / "adata_scored_isg.h5ad")         # run 74873590ca7b's name
    (ws / "work" / "reference_atlas.h5ad").write_bytes(b"not an adata_ checkpoint")
    out = export(ws, "Ddx41_DEG.processed.20261023-1719.74873590ca7b.h5ad", {"run_id": "74873590ca7b"})
    assert out["status"] == "ok", out
    assert "score_ISG" in out["result_dataset"]["obs_columns"]
    assert "adata_scored_isg.h5ad" in out["result_dataset"]["checkpoints"]
    assert "score_ISG" in (ws / "artifacts" / "tables" / "cells.csv").read_text().splitlines()[0]
