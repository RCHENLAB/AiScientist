"""The short report: asked for, or automatic when QC is all a run did (gateway/app.py).

2026-10-11, testing CellQC alone: the manuscript writer must fill an Abstract, Study question,
Results, Discussion, Limitations, Conclusion and Methods — several pages around one QC table."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiscientist.agents.scope import RunScope
from aiscientist.gateway.app import (
    _build_brief_report, _insert_verification_note, _tools_that_ran, _wants_brief_report)

_QC = {"status": "ok", "cells_before": 18681, "cells_after": 16750, "genes_after": 33696,
       "thresholds": {"min_counts": 500, "min_features": 300, "max_pct_mt": 10},
       "warnings": ["Sample1_WT: the two doublet callers agree poorly (kappa 0.07)."],
       "counts_layer": "ambient-corrected integer UMI counts from CellQC (soupx)",
       "libraries": [{"sample": "Sample1_WT", "cells_cellranger": 18681, "cells_after_filter": 18011,
                      "cells_after_doublets": 16750, "frac_retained": 0.8966, "doublets_removed": 1261,
                      "doublet_kappa": 0.0731, "ambient_soupx_contamination_mean": 0.025,
                      "median_pct_mt": 0.7509,
                      "removed_only_by": {"min_counts": 4, "min_features": 27, "max_pct_mt": 541,
                                          "several_thresholds": 98}}]}


class _Result:
    def __init__(self, steps):
        self.rounds = [{"step": "**QC** — `run_cellqc`", "verdict": {"verdict": "accept", "score": 0.9},
                        "scientist_result": {"steps": steps}}]
        self.agenda = ["**QC** — `run_cellqc`"]


def _art(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    for sub in ("data", "tables", "figures", "report"):
        (art / sub).mkdir(parents=True)
    (art / "data" / "dataset_results.json").write_text(json.dumps({
        "dataset_path": "/x/Sample1_WT/filtered_feature_bc_matrix.h5", "cells": 18681, "genes": 33696,
        "cellranger_layout": {"n_libraries": 1, "all_have_bam": True,
                              "libraries": [{"sample": "Sample1_WT", "bam": True}]}}))
    (art / "data" / "result_dataset.json").write_text(json.dumps({
        "name": "Sample1_WT.processed.20261011-1200.abc.h5ad", "n_cells": 16750, "n_genes": 33696,
        "counts_layer": "ambient-corrected integer UMI counts from CellQC (soupx)",
        "obs_columns": ["sampleid"], "obsm": [], "location": "hpc3", "kept_days": 3}))
    (art / "tables" / "cells.csv").write_text("barcode\n")
    (art / "figures" / "violin_cellqc_after_qc.png").write_bytes(b"png")
    return art


_QC_STEPS = [{"tool": "inspect_dataset", "ok": True, "result": {"status": "ok"}},
             {"tool": "run_cellqc", "ok": True, "args": {"species": "mouse"}, "result": _QC},
             {"tool": "run_code", "ok": True, "result": {"status": "ok", "stdout": "x"}},
             {"tool": "finish", "ok": True, "result": {}}]


@pytest.mark.parametrize("surface", ["chat", "brief"])
@pytest.mark.parametrize("case", ["one", "several", "no_metrics", "no_warning", "high_agreement", "zero"])
def test_default_doublet_display_preserves_counts_warnings_and_downloads(tmp_path, surface, case):
    from copy import deepcopy
    from aiscientist.gateway.app import _qc_chat_reply

    art = _ddx41_art(tmp_path)
    qc = deepcopy(_DDX41)
    if case == "several":
        qc["libraries"].append(deepcopy(_QC["libraries"][0]))
        qc["warnings"] += _QC["warnings"]
    elif case == "no_metrics":
        (art / "tables/cellqc_metrics.csv").unlink()
    elif case == "no_warning":
        qc["warnings"] = []
    elif case == "high_agreement":
        qc["libraries"][0]["doublet_kappa"] = 0.9
        qc["warnings"] = []
    elif case == "zero":
        qc["libraries"][0]["doublets_removed"] = 0
    qc["warnings"].append("Sample2_Ddx41: step ambient failed — missing raw matrix")
    original = deepcopy(qc)
    files = {p: p.read_bytes() for p in art.rglob("*") if p.is_file()}
    result = _Result([{"tool": "run_cellqc", "ok": True, "result": qc}])
    text = (_qc_chat_reply(art, result) if surface == "chat"
            else _build_brief_report(art, "Run QC", result, None))

    for detail in ("doubletfinder", "scdblfinder", "kappa", "agree poorly", "1,192", "pANN"):
        assert detail.lower() not in text.lower(), text
    assert "doublets removed" in text.lower()
    assert ("929" if case != "zero" else "Doublets removed: 0") in text
    assert "step ambient failed — missing raw matrix" in text
    assert ("Doublet filtering needs review" in text) == (case != "high_agreement")
    assert "tables/cells.csv" in text
    if case != "no_metrics":
        assert "tables/cellqc_metrics.csv" in text
    assert qc == original
    assert all(p.read_bytes() == content for p, content in files.items())


def test_a_run_that_only_did_qc_gets_the_short_report_unasked():
    assert _tools_that_ran(_Result(_QC_STEPS))[:2] == ["inspect_dataset", "run_cellqc"]
    assert _wants_brief_report(None, _Result(_QC_STEPS))
    assert _wants_brief_report(RunScope(), _Result(_QC_STEPS))
    more = _QC_STEPS + [{"tool": "run_clustering", "ok": True, "result": {"status": "ok"}}]
    assert not _wants_brief_report(RunScope(), _Result(more))
    assert _wants_brief_report(RunScope(brief_report=True), _Result(more))     # asked for
    failed = _QC_STEPS + [{"tool": "run_de", "ok": False, "result": {"status": "error"}}]
    assert _wants_brief_report(None, _Result(failed))          # nothing beyond QC succeeded
    assert not _wants_brief_report(None, _Result([]))          # nothing ran at all: not "QC only"


def test_the_short_report_is_the_tools_own_numbers_and_nothing_else(tmp_path):
    art = _art(tmp_path)
    md = _build_brief_report(art, "只做 QC，报告尽量简短", _Result(_QC_STEPS),
                             RunScope(keep=("run_cellqc",), brief_report=True))
    assert md.startswith("# Quality-control summary: Sample1_WT")
    assert "As requested, this run did quality control only (`run_cellqc`)" in md
    # A result without the kept cells' median says whose median it shows.
    assert "median % mito before filtering |" in md
    assert "| Sample1_WT | 18,681 | 18,011 | 16,750 | 89.7% | 2.5% | 0.75 |" in md
    assert ("the count, gene and mitochondrial filters removed 670 cells: 4 only for too few counts, "
            "27 only for too few genes, 541 only for too much mitochondrial RNA, 98 for more than "
            "one of these.") in md
    assert "Doublets removed: 1,261 of the 18,011 filtered cells (7.0%)." in md
    assert "kappa" not in md
    assert "Sample1_WT: Doublet filtering needs review" in md
    assert "Sample1_WT.processed.20261011-1200.abc.h5ad" in md   # the result matrix is named
    assert "figures/violin_cellqc_after_qc.png" in md and "## What was run" in md
    for heading in ("## Abstract", "## Discussion", "## Methods", "## References", "## Limitations"):
        assert heading not in md
    assert len(md.split()) < 700
    # The verification summary still goes on the end.
    noted = _insert_verification_note(md, {"numbers_checked": 9, "numbers_exact": 9})
    assert noted.rstrip().endswith("could not be traced.") and "## Verification" in noted


def test_a_short_report_of_a_longer_run_shows_its_cell_types(tmp_path):
    art = _art(tmp_path)
    (art / "tables" / "celltype_composition.csv").write_text(
        "cell_type,n_cells,pct\nRod photoreceptor,13520,80.72\nUnassigned,741,4.42\n")
    steps = _QC_STEPS + [{"tool": "run_marker_annotation", "ok": True, "result": {"status": "ok"}}]
    md = _build_brief_report(art, "QC and annotate, brief report", _Result(steps),
                             RunScope(brief_report=True))
    assert md.startswith("# Analysis summary: Sample1_WT")
    assert "| Rod photoreceptor | 13,520 | 80.72 |" in md and "This is the short report" in md


# --- prod run 6275df1213ab: CellQC alone on one mouse retina library (Sample2_Ddx41) -------------
# Every headline number was right; the wording was not. The values below are that run's own.
_DDX41 = {"status": "ok", "cells_before": 15872, "cells_after": 14553, "genes_after": 33696,
          "thresholds": {"min_counts": 500, "min_features": 300, "max_pct_mt": 10.0},
          "doublets": {"decider": "doubletfinder", "callers": ["doubletfinder", "scdblfinder"]},
          "warnings": ["Sample2_Ddx41: the two doublet callers agree poorly (kappa 0.14), so the "
                       "choice of decider drives which cells are removed."],
          "libraries": [{"sample": "Sample2_Ddx41", "cells_cellranger": 15872,
                         "cells_after_filter": 15482, "cells_after_doublets": 14553,
                         "frac_retained": 0.9169, "doublets_removed": 929,
                         "doublet_kappa": 0.1447634434698273,
                         "ambient_soupx_contamination_mean": 0.016,
                         "median_pct_mt": 0.7745, "median_pct_mt_kept": 0.6897,
                         "removed_by_mito_total": 360,
                         "removed_only_by": {"min_counts": 2, "min_features": 28, "max_pct_mt": 317,
                                             "several_thresholds": 43},
                         "nuclear_fraction_median": 0.342894251649782,
                         "nuclear_fraction_iqr": [0.246474851059584, 0.41493278242629855]}]}
# A subset of CellQC's metrics.csv columns, as tables/cellqc_metrics.csv holds them.
_DDX41_METRICS = {
    "sampleid": "Sample2_Ddx41", "cellranger_estimated_number_of_cells": '"15,872"',
    "filter_ncell_before": "15872", "filter_ncell_after": "15482", "filter_ncell_removed": "390",
    "filter_fail_mincount": "3", "filter_fail_minfeature": "71", "filter_fail_mito": "360",
    "filter_fail_mincount_only": "2", "filter_fail_minfeature_only": "28",
    "filter_fail_mito_only": "317", "filter_fail_multiple": "43",
    "filter_median_pct_counts_mt": "0.7745", "doublet_doubletfinder_ndoublet": "929",
    "doublet_doubletfinder_frac": "0.0600051672910476", "doublet_decider": "doubletfinder",
    "doublet_ncell_before": "15482", "doublet_ncell_after": "14553",
    "ambient_soupx_contamination_mean": "0.016", "ambient_soupx_counts_removed_frac": "0.0160163",
    "doublet_scdblfinder_ndoublet": "1437", "doublet_scdblfinder_frac": "0.0928174654437411",
    "concordance_doubletfinder_vs_scdblfinder_kappa": "0.1447634434698273",
    "concordance_doubletfinder_vs_scdblfinder_both_doublet": "245",
    "nf_median": "0.342894251649782", "nf_q25": "0.246474851059584", "nf_q75": "0.41493278242629855",
    "frac_retained": "0.9169"}
# The run's one run_code call: it reloaded the checkpoint to print its shape and layers.
_CHECK_CODE = ("import os, scanpy as sc\n"
               "a = sc.read_h5ad(os.path.join(os.environ['AISCIENTIST_WORK'], 'adata_qc.h5ad'))\n"
               "print(a.shape, a.obs.columns.tolist()[:40], list(a.layers.keys()))\n")
_DDX41_STEPS = [{"tool": "run_cellqc", "ok": True, "args": {"species": "mouse"}, "result": _DDX41},
                {"tool": "run_code", "ok": True, "args": {"code": _CHECK_CODE},
                 "result": {"status": "ok", "stdout": "(14553, 33696) ['sampleid'] ['counts']"}},
                {"tool": "finish", "ok": True, "result": {}}]


def _ddx41_art(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    for sub in ("data", "tables", "figures", "report"):
        (art / sub).mkdir(parents=True)
    (art / "data" / "dataset_results.json").write_text(json.dumps({
        "dataset_path": "/x/Sample2_Ddx41/filtered_feature_bc_matrix.h5",
        "cellranger_layout": {"n_libraries": 1, "libraries": [{"sample": "Sample2_Ddx41"}]}}))
    (art / "tables" / "cellqc_metrics.csv").write_text(
        ",".join(_DDX41_METRICS) + "\n" + ",".join(_DDX41_METRICS.values()) + "\n")
    # The QC-only cells.csv header, verbatim: no cluster, label or UMAP column.
    (art / "tables" / "cells.csv").write_text(
        "barcode,sampleid,n_genes_by_counts,total_counts,total_counts_mt,pct_counts_mt,"
        "total_counts_ribo,pct_counts_ribo,total_counts_hb,pct_counts_hb,raw_total_counts,"
        "raw_n_genes_by_counts,raw_pct_counts_mt,raw_pct_counts_ribo,raw_pct_counts_hb,"
        "doubletfinder_pANN,doubletfinder_class,scdblfinder_score,scdblfinder_class,"
        "nuclear_fraction,exon,intron,nuclear_fraction_primary_only\n")
    return art


def _ddx41_report(tmp_path: Path) -> "tuple[Path, str]":
    art = _ddx41_art(tmp_path)
    # "Do only cellqc ..." read as a keep-list of every QC tool: what the run was ALLOWED to run.
    scope = RunScope(keep=("run_cellqc", "run_scanpy_qc", "run_doublet_detection"))
    return art, _build_brief_report(
        art, "Do only cellqc quality controll part and generate the report and result to me",
        _Result(_DDX41_STEPS), scope)


def test_the_scope_says_what_ran_not_what_the_scope_allowed(tmp_path):
    _, md = _ddx41_report(tmp_path)
    scope = next(line for line in md.splitlines() if line.startswith("**Scope.**"))
    assert scope.startswith("**Scope.** As requested, this run did quality control only "
                            "(`run_cellqc`);")
    assert "run_scanpy_qc" not in scope and "run_doublet_detection" not in scope
    # A limit that is not "QC only" is not presented as the reason the run stopped at QC.
    other = _build_brief_report(_ddx41_art(tmp_path / "b"), "no DE", _Result(_DDX41_STEPS),
                                RunScope(exclude=("run_de",)))
    assert "**Scope.** This run did quality control only (`run_cellqc`)" in other


def test_the_cells_table_is_described_by_its_columns(tmp_path):
    art, md = _ddx41_report(tmp_path)
    assert ("- `tables/cells.csv` — one row per cell: QC metrics, doublet calls, nuclear fraction."
            in md)
    files = md.split("## Files", 1)[1]
    assert "cluster" not in files and "UMAP" not in files and "label" not in files
    (art / "tables" / "cells.csv").write_text("barcode,n_genes_by_counts,leiden,cell_type,UMAP_1,"
                                              "UMAP_2\n")
    md = _build_brief_report(art, "q", _Result(_DDX41_STEPS), None)
    assert "one row per cell: QC metrics, cluster, cell-type label, UMAP." in md


def test_the_qc_numbers_say_what_they_count(tmp_path):
    _, md = _ddx41_report(tmp_path)
    # The kept cells' median % mito, not CellQC's over every barcode before filtering (0.77).
    assert "| median % mito (kept cells) |" in md
    assert "| Sample2_Ddx41 | 15,872 | 15,482 | 14,553 | 91.7% | 1.6% | 0.69 |" in md
    # The *_only counts are said to be "only", with each threshold's total beside them.
    assert ("— the count, gene and mitochondrial filters removed 390 cells: 2 only for too few counts "
            "(3 in total), 28 only for too few genes (71 in total), 317 only for too much "
            "mitochondrial RNA (360 in total), 43 for more than one of these.") in md
    assert "Doublets removed: 929 of the 15,482 filtered cells (6.0%)." in md
    assert "Doublet filtering needs review" in md
    assert "kappa" not in md and "scDblFinder" not in md and "1,192" not in md
    assert "Nuclear fraction (share of intronic reads): median 0.34, IQR 0.25–0.41." in md


def test_no_instruction_to_the_model_and_a_check_is_called_a_check(tmp_path):
    _, md = _ddx41_report(tmp_path)
    assert "Report it." not in md
    assert ("**2. `run_code`** — Ran a short purpose-written script that only read the run's data "
            "to check it; it wrote no table, figure or file.") in md
    assert "a step no packaged tool covers" not in md


def test_every_number_in_the_short_report_traces_and_the_statement_reads_right(tmp_path):
    from aiscientist.reporting.verification import build_verification, manuscript_note
    art, md = _ddx41_report(tmp_path)
    result = _Result(_DDX41_STEPS)
    statement, summary = build_verification(art, md, result.agenda, result.rounds,
                                            {"citations": [], "reason": "none"}, brief=True)
    assert summary["numbers_untraced"] == 0, summary["untraced"]
    assert summary["numbers_inconsistent"] == 0
    assert "Discussion" not in statement
    assert "Nothing in the report was checked against published literature (none)." in statement
    note = manuscript_note(summary)
    assert "Nothing in the report was checked against published literature." in note
    assert "1 are" not in note and "1 are" not in statement
    assert "- The one planned step completed and was accepted by the reviewer." in statement


# --- A QC-only run is answered in the chat, with no report (Yijun, 2026-10-16, on 6275df1213ab) ---

def _with_result_dataset(art: Path) -> Path:
    (art / "data" / "result_dataset.json").write_text(json.dumps({
        "name": "Sample2_Ddx41.processed.20261014-1204.6275df1213ab.h5ad", "n_cells": 14553,
        "n_genes": 33696, "location": "hpc3", "kept_days": 3, "until_disconnect": True,
        "counts_layer": "ambient-corrected integer UMI counts from CellQC (soupx), not the Cell "
                        "Ranger matrix: ambient RNA has already been subtracted from every cell"}))
    (art / "result").mkdir()
    (art / "result" / "report.html").write_text("<html></html>")
    return art


def test_a_qc_only_run_gets_a_short_chat_reply_with_the_tools_numbers(tmp_path):
    from aiscientist.gateway.app import _qc_chat_reply, _qc_only_run
    art = _with_result_dataset(_ddx41_art(tmp_path))
    applied = dict(_DDX41, ambient={"applied": "soupx", "compared": "decontx"})
    result = _Result([dict(_DDX41_STEPS[0], result=applied)] + _DDX41_STEPS[1:])
    assert _qc_only_run(result)
    reply = _qc_chat_reply(art, result)
    assert reply.startswith("**Quality control done** — `run_cellqc` on Sample2_Ddx41.")
    assert "- **14,553 of 15,872 cells kept (91.7%)**; 33,696 genes." in reply
    assert ("- Filters removed 390: 317 only for mitochondrial RNA above 10%, 28 only for fewer than "
            "300 genes, 2 only for fewer than 500 counts, 43 for more than one.") in reply
    assert "Doublets removed: 929 of the 15,482 filtered cells (6.0%)." in reply
    assert ("- Median mitochondrial RNA in the kept cells 0.69%; ambient RNA 1.6% (SoupX, already "
            "subtracted from the counts).") in reply
    assert ("**Result:** `Sample2_Ddx41.processed.20261014-1204.6275df1213ab.h5ad` — 14,553 cells × "
            "33,696 genes") in reply
    assert "kept on HPC3 until the session ends, then with the run on the server" in reply
    assert "`result/report.html`" in reply
    # The diagnostic is a concise warning; the comparison stays in Downloads.
    assert reply.count("Doublet filtering needs review") == 1
    assert "kappa" not in reply and "Report it" not in reply
    # A reply, not a report: no sections, no literature, no "not run" list.
    for word in ("## ", "Methods", "Limitations", "Conclusion", "literature", "differential"):
        assert word not in reply, word
    assert len(reply.strip().splitlines()) <= 11


def test_more_than_qc_is_not_answered_in_the_chat_only():
    from aiscientist.gateway.app import _code_wrote_outputs, _qc_only_run
    # The run's own run_code only reloaded the checkpoint and printed it: a check, nothing written.
    assert not _code_wrote_outputs(_Result(_DDX41_STEPS))
    audit = _DDX41_STEPS + [{"tool": "run_code", "ok": True, "args": {"code": "x = 1"},
                             "result": {"status": "ok", "tables": ["tables/rod_leak.csv"]}}]
    assert _qc_only_run(_Result(audit)) and _code_wrote_outputs(_Result(audit))
    saved = [{"tool": "run_code", "ok": True, "args": {"code": "plt.savefig('a.png')"},
              "result": {"status": "ok"}}]
    assert _code_wrote_outputs(_Result(saved))
    # Run 57c6b0c6ba0c's script, verbatim in what matters: it READ the metrics table through
    # AISCIENTIST_ARTIFACTS on HPC3, which reports every file a script writes and reported none.
    read_only = [{"tool": "run_code", "ok": True, "args": {"code": (
        "art=os.environ['AISCIENTIST_ARTIFACTS']\n"
        "df=pd.read_csv(os.path.join(art, 'tables/cellqc_metrics.csv'))\nprint(df.head())\n")},
        "result": {"status": "ok", "execution_mode": "hpc_slurm", "stdout": "..."}}]
    assert not _code_wrote_outputs(_Result(_DDX41_STEPS + read_only))
    hpc_wrote = [dict(read_only[0], result=dict(read_only[0]["result"], tables=["tables/x.csv"]))]
    assert _code_wrote_outputs(_Result(hpc_wrote))
    more = _DDX41_STEPS + [{"tool": "run_clustering", "ok": True, "result": {"status": "ok"}}]
    assert not _qc_only_run(_Result(more))
    assert not _qc_only_run(_Result([]))
    failed_qc = [{"tool": "run_cellqc", "ok": False, "result": {"status": "error"}}]
    assert not _qc_only_run(_Result(failed_qc))


def test_the_chat_reply_for_several_libraries_and_for_scanpy_qc(tmp_path):
    from aiscientist.gateway.app import _qc_chat_reply
    two = dict(_DDX41, cells_before=34553, cells_after=31303,
               libraries=[_QC["libraries"][0], _DDX41["libraries"][0]],
               warnings=_QC["warnings"] + [w + " Report it." for w in _DDX41["warnings"]])
    art = _ddx41_art(tmp_path / "two")
    reply = _qc_chat_reply(art, _Result([{"tool": "run_cellqc", "ok": True, "result": two}]))
    assert "- **31,303 of 34,553 cells kept (90.6%)**; 33,696 genes." in reply
    assert "| Sample2_Ddx41 | 15,872 | 15,482 | 14,553 | 929 | 91.7% |" in reply
    assert "- Thresholds: `min_counts` = 500, `min_features` = 300, `max_pct_mt` = 10.0." in reply
    assert "kappa" not in reply and reply.count("Doublet filtering needs review") == 2
    assert "Report it" not in reply                                             # warning, once
    assert "ambient RNA 1.6% (SoupX estimate)" not in reply   # per-library detail stays in the table
    scanpy = {"status": "ok", "cells_before": 5000, "cells_after": 4600, "genes_before": 30000,
              "genes_after": 18000, "warnings": [],
              "thresholds": {"min_genes": 200, "min_cells": 3, "max_pct_mt": 20}}
    reply = _qc_chat_reply(_art(tmp_path / "scanpy"),
                           _Result([{"tool": "run_scanpy_qc", "ok": True, "result": scanpy}]))
    assert reply.startswith("**Quality control done** — `run_scanpy_qc`.")
    assert "- **4,600 of 5,000 cells kept (92.0%)**; 18,000 genes." in reply
    assert "`min_genes` = 200" in reply and "result/report.html" not in reply


def test_a_qc_reports_statement_lists_no_labels_or_literature(tmp_path):
    """A QC report had no labels and nothing to check against the literature: run 6275df1213ab's
    statement listed both as unchecked."""
    from aiscientist.reporting.verification import build_verification, manuscript_note
    art, md = _ddx41_report(tmp_path)
    result = _Result(_DDX41_STEPS)
    statement, summary = build_verification(art, md, result.agenda, result.rounds,
                                            {"citations": [], "reason": "none"}, brief=True,
                                            qc_only=True)
    assert "literature" not in statement and "Cell-type labels" not in statement
    assert "## 2. Quality-control accounting" in statement
    assert "## 3. Planned steps that did not finish as planned" in statement
    assert summary["literature_checked"] is None
    assert "literature" not in manuscript_note(summary)
    # Without the option the statement is unchanged.
    statement, _ = build_verification(art, md, result.agenda, result.rounds,
                                      {"citations": [], "reason": "none"}, brief=True)
    assert "## 2. Cell-type labels" in statement and "published literature" in statement


def test_the_dataset_ready_line_names_only_what_the_dataset_holds():
    """Runs 6275df1213ab and caa188e8e672 (QC only) announced their dataset "with the clusters, cell
    types and embeddings"; it had none of them."""
    from aiscientist.gateway.app import _dataset_holds
    qc = {"obs_columns": ["sampleid", "n_genes_by_counts", "total_counts", "pct_counts_mt",
                          "doubletfinder_class"], "obsm": []}
    assert _dataset_holds(qc) == "with each cell's QC metrics"
    full = {"obs_columns": ["sampleid", "leiden", "cell_type"], "obsm": ["X_umap"]}
    assert _dataset_holds(full) == "with the clusters, cell types and embeddings"
    assert _dataset_holds({"obs_columns": ["leiden"], "obsm": ["X_umap"]}) == \
        "with the clusters and embeddings"
    assert _dataset_holds({}) == "as QC left it"
