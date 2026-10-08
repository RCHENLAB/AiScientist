"""run_cellqc: recognising a Cell Ranger delivery, staging it, configuring CellQC, validating what it
wrote and merging it into the checkpoint the line reads. CellQC itself is replaced by a fake
``cellqc`` executable that writes CellQC's result files; the real pipeline is exercised on HPC3."""
from __future__ import annotations

import gzip
import io
import json
import os
import stat
import sys
import tarfile
from pathlib import Path

import pytest

from aiscientist.tools._lib.cellranger import cellranger_layout_hint, describe_cellranger_layout

# --- recognising a delivery ------------------------------------------------------------------------


def test_the_three_forms_of_a_library_are_recognised():
    paths = [
        "/up/S1/raw_feature_bc_matrix.h5", "/up/S1/filtered_feature_bc_matrix.h5",
        "/up/S1/raw_feature_bc_matrix.tar.gz", "/up/S1/filtered_feature_bc_matrix.tar.gz",
        "/up/S1/analysis.tar.gz", "/up/S1/possorted_genome_bam.bam",
        "/up/S1/possorted_genome_bam.bam.bai", "/up/S1/metrics_summary.csv",
        "/up/S2/outs/raw_feature_bc_matrix", "/up/S2/outs/filtered_feature_bc_matrix",
        "/up/S2/outs/analysis",
        "/up/S3/filtered_feature_bc_matrix.h5",             # no raw matrix: not a library
    ]
    layout = describe_cellranger_layout(paths, "/up")
    assert layout["n_libraries"] == 2
    s1, s2 = layout["libraries"]
    assert s1["sample"] == "S1" and s1["bam"] and s1["packed"] and s1["clusters"] and s1["metrics"]
    assert s2["sample"] == "S2" and s2["dir"] == "/up/S2/outs"   # outs/ takes its parent's name
    assert not s2["bam"] and not s2["packed"] and s2["mtx_dirs"]
    assert not layout["all_have_bam"] and layout["any_packed"]


def test_the_bound_folder_can_itself_be_the_library():
    layout = describe_cellranger_layout(
        ["raw_feature_bc_matrix.h5", "filtered_feature_bc_matrix.h5"], "/up/Sample3_GSM5676874")
    assert layout["libraries"][0]["sample"] == "Sample3_GSM5676874"


def test_anything_else_is_not_a_delivery():
    assert describe_cellranger_layout(["/up/a.h5ad", "/up/notes.txt"], "/up") is None
    assert cellranger_layout_hint(None) == ""


def test_the_hint_names_the_qc_route_and_what_not_to_plan():
    layout = describe_cellranger_layout(
        ["raw_feature_bc_matrix.h5", "filtered_feature_bc_matrix.h5"], "/up/S1")
    hint = cellranger_layout_hint(layout)
    assert "`run_cellqc`" in hint and "Do NOT plan `run_scanpy_qc`" in hint
    assert "Do NOT plan `run_doublet_detection`" in hint and "1 library (S1)" in hint
    # Cell Ranger writes integer counts, so even with no file read the audit is ruled out.
    assert "PROVENANCE IS SETTLED" in hint and "Do NOT plan a `run_code` step" in hint


# --- what the matrices hold -----------------------------------------------------------------------------

# 5 features x 4 barcodes, the way Cell Ranger lays it out: CSC, one column per BARCODE. Barcode 3 is
# empty; feature 4 is an antibody, which Cell Ranger's UMI/gene metrics leave out.
_COUNTS = [[3, 0, 1, 0],
           [0, 2, 0, 0],
           [1, 1, 4, 0],
           [0, 5, 2, 0],
           [7, 0, 9, 0]]
_TYPES = ["Gene Expression"] * 4 + ["Antibody Capture"]
# metrics_summary.csv as Cell Ranger 9 writes it (header row, one value row; thousands separators).
_METRICS = ('Estimated Number of Cells,Median Genes per Cell,Number of Reads,Valid UMIs,'
            'Median UMI Counts per Cell\n"4","2","342,370,742",97.8%,"4"\n')


def _write_10x_h5(path, counts=_COUNTS, *, types=_TYPES, genome="GRCm39", dtype="int32", v2=False):
    h5py = pytest.importorskip("h5py")
    np = pytest.importorskip("numpy")
    dense = np.asarray(counts, dtype=dtype)
    n_features, n_barcodes = dense.shape
    data, indices, indptr = [], [], [0]
    for col in range(n_barcodes):
        rows = np.nonzero(dense[:, col])[0]
        data += list(dense[rows, col])
        indices += list(rows)
        indptr.append(len(data))
    with h5py.File(path, "w") as f:
        g = f.create_group(genome if v2 else "matrix")
        g["data"] = np.asarray(data, dtype=dtype)
        g["indices"] = np.asarray(indices, dtype="int64")
        g["indptr"] = np.asarray(indptr, dtype="int64")
        g["shape"] = np.asarray([n_features, n_barcodes], dtype="int32")
        g["barcodes"] = np.asarray([f"AAAC{i}-1".encode() for i in range(n_barcodes)])
        ids = np.asarray([f"ENSMUSG{i:011d}".encode() for i in range(n_features)])
        names = np.asarray([f"Gene{i}".encode() for i in range(n_features)])
        if v2:
            g["genes"], g["gene_names"] = ids, names
        else:
            feats = g.create_group("features")
            feats["id"], feats["name"] = ids, names
            feats["feature_type"] = np.asarray([t.encode() for t in types])
            feats["genome"] = np.asarray([genome.encode()] * n_features)
            f.attrs["chemistry_description"] = "Single Cell 3' v4 (polyA)"
            f.attrs["library_ids"] = np.asarray([b"3v4_WT_1M"])
            f.attrs["software_version"] = "cellranger-9.0.1"
    return path


def test_a_cell_ranger_h5_is_read_the_right_way_round(tmp_path, monkeypatch):
    from aiscientist.tools._lib import cellranger

    path = _write_10x_h5(tmp_path / "filtered_feature_bc_matrix.h5")
    monkeypatch.setattr(cellranger, "_CHUNK_ENTRIES", 3)   # several barcodes per read, and splits
    m = cellranger.describe_10x_h5(str(path))
    assert (m["layout"], m["n_barcodes"], m["n_features"]) == ("v3", 4, 5)
    assert m["genomes"] == {"GRCm39": 5} and m["feature_types"]["Antibody Capture"] == 1
    assert m["library_ids"] == ["3v4_WT_1M"] and m["software_version"] == "cellranger-9.0.1"
    c = m["counts"]
    assert c["integer"] and c["non_negative"] and c["empty_barcodes"] == 1
    # Gene Expression only: per-barcode UMIs 4, 8, 7, 0 and genes 2, 3, 3, 0 (the antibody's 7 and 9
    # are left out, as metrics_summary.csv leaves them out).
    assert c["total_umis"] == 19
    assert (c["median_umis_per_barcode"], c["median_genes_per_barcode"]) == (5.5, 2.5)


def test_the_reader_agrees_with_scanpy(tmp_path):
    sc = pytest.importorskip("scanpy")
    path = _write_10x_h5(tmp_path / "m.h5", types=["Gene Expression"] * 5)
    from aiscientist.tools._lib.cellranger import describe_10x_h5

    adata = sc.read_10x_h5(str(path))
    m = describe_10x_h5(str(path))
    assert adata.shape == (m["n_barcodes"], m["n_features"])
    assert int(adata.X.sum()) == m["counts"]["total_umis"] == 35


def test_the_cell_ranger_2_layout_is_read_too(tmp_path):
    from aiscientist.tools._lib.cellranger import describe_10x_h5

    m = describe_10x_h5(str(_write_10x_h5(tmp_path / "v2.h5", genome="mm10", v2=True)))
    assert m["layout"] == "v2" and m["genomes"] == {"mm10": 5} and m["n_barcodes"] == 4
    assert m["counts"]["total_umis"] == 35          # no feature types: every feature counts


def test_anything_but_a_10x_matrix_reads_as_none(tmp_path):
    h5py = pytest.importorskip("h5py")
    from aiscientist.tools._lib.cellranger import describe_10x_h5

    with h5py.File(tmp_path / "x.h5ad", "w") as f:
        f.create_group("obs")
        f["X"] = [[1.0]]
    (tmp_path / "empty.h5").write_bytes(b"")
    assert describe_10x_h5(str(tmp_path / "x.h5ad")) is None
    assert describe_10x_h5(str(tmp_path / "empty.h5")) is None
    assert describe_10x_h5(str(tmp_path / "missing.h5")) is None


def test_metrics_summary_numbers_and_the_check(tmp_path):
    from aiscientist.tools._lib.cellranger import (check_against_metrics, describe_10x_h5,
                                                   parse_metrics_summary)

    metrics = parse_metrics_summary(_METRICS)
    assert metrics["Number of Reads"] == 342370742 and metrics["Valid UMIs"] == pytest.approx(0.978)
    assert parse_metrics_summary("not,a\n") == {} and parse_metrics_summary("") == {}
    m = describe_10x_h5(str(_write_10x_h5(tmp_path / "m.h5")))
    check = check_against_metrics(m, metrics)
    # 4 cells exactly; medians 5.5 vs 4 is off, 2.5 vs 2 is within Cell Ranger's rounding.
    assert [c["match"] for c in check["checks"]] == [True, False, True] and not check["agrees"]
    assert check_against_metrics(m, {"Number of Reads": 1.0}) is None


def _delivery_with_matrix(tmp_path, counts=_COUNTS, dtype="int32", metrics=_METRICS):
    lib = tmp_path / "Sample1_WT"
    lib.mkdir()
    _write_10x_h5(lib / "filtered_feature_bc_matrix.h5", counts, dtype=dtype)
    (lib / "raw_feature_bc_matrix.h5").write_bytes(b"")
    (lib / "metrics_summary.csv").write_text(metrics, encoding="utf-8")
    return lib


def test_the_hint_states_the_facts_and_rules_out_the_audit(tmp_path):
    from aiscientist.gateway.app import _cellranger_layout_local

    agree = _METRICS.replace('"4","2","342,370,742",97.8%,"4"', '"4","2.5","342,370,742",97.8%,"5.5"')
    layout = _cellranger_layout_local(_delivery_with_matrix(tmp_path, metrics=agree))
    lib = layout["libraries"][0]
    assert lib["matrix"]["n_barcodes"] == 4 and lib["metrics_check"]["agrees"]
    hint = cellranger_layout_hint(layout)
    assert "Sample1_WT: 4 barcodes x 5 features (4 Gene Expression, 1 Antibody Capture; genome " \
           "GRCm39 = mouse), int32 integer UMI counts: 19 UMIs" in hint
    assert "it reproduces its metrics_summary.csv (4 cells" in hint
    assert "species MOUSE" in hint and "PROVENANCE IS SETTLED" in hint


def test_a_dfs3b_delivery_reads_its_staged_primary_and_fetches_the_summaries(tmp_path):
    """HPC3 storage: only the primary's matrix is staged here; every summary is read over SFTP (file
    content never goes through the login node, so the only command sent is the `find`)."""
    from types import SimpleNamespace

    from aiscientist.gateway.app import _cellranger_layout_remote
    from aiscientist.gateway.executor import ExecResult

    staged = _write_10x_h5(tmp_path / "staged.h5")
    listing = "".join(f"/dfs/up/{s}/{n}\n" for s in ("S1", "S2") for n in (
        "raw_feature_bc_matrix.h5", "filtered_feature_bc_matrix.h5", "metrics_summary.csv"))
    sent, read = [], []

    def _exec(cmd, timeout=60.0):
        sent.append(cmd)
        return ExecResult(command=cmd, exit_status=0, stdout=listing, stderr="")

    def _read_bytes(path, max_bytes=None):
        read.append(path)
        return _METRICS.encode()

    conn = SimpleNamespace(executor=SimpleNamespace(exec=_exec, read_bytes=_read_bytes))
    layout = _cellranger_layout_remote(conn, "/dfs/up/",
                                       {"/dfs/up//S1/filtered_feature_bc_matrix.h5": str(staged)})
    s1, s2 = layout["libraries"]
    assert s1["matrix"]["n_barcodes"] == 4 and "metrics_check" in s1
    assert "matrix" not in s2 and s2["metrics_summary"]["Estimated Number of Cells"] == 4
    assert read == ["/dfs/up/S1/metrics_summary.csv", "/dfs/up/S2/metrics_summary.csv"]
    assert len(sent) == 1 and sent[0].startswith("find ")


def test_a_matrix_that_is_not_counts_is_flagged_not_waved_through(tmp_path):
    from aiscientist.gateway.app import _cellranger_layout_local

    logged = [[0.5 if v else 0 for v in row] for row in _COUNTS]
    hint = cellranger_layout_hint(_cellranger_layout_local(
        _delivery_with_matrix(tmp_path, logged, dtype="float32")))
    assert "⚠ PROVENANCE" in hint and "NON-INTEGER values" in hint
    assert "PROVENANCE IS SETTLED" not in hint


def test_a_lone_10x_h5_gets_a_profile(tmp_path):
    from aiscientist.tools.datasets import run_dataset_smoke_analysis

    filtered = _write_10x_h5(tmp_path / "filtered_feature_bc_matrix.h5")
    dr = run_dataset_smoke_analysis(filtered, tmp_path / "out")["result"]
    assert (dr["cells"], dr["genes"]) == (4, 5) and dr["tenx_h5"]["genomes"] == {"GRCm39": 5}
    assert "read at upload" in dr["note"] and "needs no `run_code` audit" in dr["note"]
    raw = run_dataset_smoke_analysis(_write_10x_h5(tmp_path / "raw_feature_bc_matrix.h5"),
                                     tmp_path / "out2")["result"]
    assert "cells" not in raw, "a raw matrix's barcodes are mostly empty droplets, not cells"


# --- the gateway side ----------------------------------------------------------------------------------


def test_a_folder_upload_picks_the_filtered_matrix_not_the_largest_h5():
    from aiscientist.gateway.app import _primary_rank
    ranked = sorted(["molecule_info.h5", "raw_feature_bc_matrix.h5", "filtered_feature_bc_matrix.h5"],
                    key=lambda n: _primary_rank(n, 1, {"molecule_info.h5": 9e9}.get(n, 1)))
    assert ranked[0] == "filtered_feature_bc_matrix.h5"
    assert _primary_rank("x.h5ad", 3, 1) < _primary_rank("filtered_feature_bc_matrix.h5", 1, 1)


def test_the_layout_reaches_the_planners_data_profile(tmp_path):
    from aiscientist.agents.research_harness import HarnessContext
    from aiscientist.agents.research_lab import LabConfig, ResearchLab
    from aiscientist.gateway.app import _cellranger_layout_local, _record_input_layout

    lib = tmp_path / "Sample1_WT"
    lib.mkdir()
    for name in ("raw_feature_bc_matrix.h5", "filtered_feature_bc_matrix.h5"):
        (lib / name).write_bytes(b"")
    decisions = {"dataset_result": {"dataset_kind": "single_cell_other"}}
    line = _record_input_layout(decisions, _cellranger_layout_local(lib))
    assert line and "1 library (Sample1_WT)" in line and "CellQC" in line
    lab = ResearchLab(HarnessContext(decisions=decisions, tunnel_port=1, model="m"), LabConfig(),
                      complete_fn=lambda m: "x")
    assert "⚠ INPUT is a 10x Cell Ranger delivery" in lab._dataset_context()
    assert _record_input_layout({}, None) is None


def test_the_planner_reads_the_matrix_facts_once(tmp_path):
    from aiscientist.agents.research_harness import HarnessContext
    from aiscientist.agents.research_lab import LabConfig, ResearchLab
    from aiscientist.gateway.app import _cellranger_layout_local, _record_input_layout
    from aiscientist.tools.datasets import run_dataset_smoke_analysis

    lib = _delivery_with_matrix(tmp_path)
    profile = run_dataset_smoke_analysis(lib / "filtered_feature_bc_matrix.h5", tmp_path / "data")
    decisions = {"dataset_result": profile["result"]}
    _record_input_layout(decisions, _cellranger_layout_local(lib))
    lab = ResearchLab(HarnessContext(decisions=decisions, tunnel_port=1, model="m"), LabConfig(),
                      complete_fn=lambda m: "x")
    ctx = lab._dataset_context()
    assert ctx.startswith("Dataset profile: 4 cells x 5 genes")
    assert ctx.count("4 barcodes x 5 features") == 1, "the primary's facts, stated once"
    assert "PROVENANCE IS SETTLED" in ctx


# --- the tool, with a fake CellQC ----------------------------------------------------------------------

_FAKE_CELLQC = r'''#!{python}
"""Stands in for `cellqc`: records its argv and writes CellQC's result/ files."""
import csv, json, os, sys
from pathlib import Path
log = Path(os.environ["FAKE_CELLQC_LOG"])
with log.open("a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
if "-n" in sys.argv:
    sys.exit(0)
out = Path(sys.argv[sys.argv.index("-d") + 1])
samples = [row for row in csv.DictReader(open(sys.argv[-1]), delimiter="\t")]
res = out / "result"
res.mkdir(parents=True, exist_ok=True)
import anndata as ad, numpy as np, pandas as pd
genes = ["mt-Co1", "mt-Nd1"] + [f"Gene{{i}}" for i in range(58)]
rows, man, st = [], [], []
for k, s in enumerate(samples):
    rng = np.random.default_rng(k)
    n = 40 + 10 * k
    X = rng.poisson(3, size=(n, len(genes))).astype("float64")
    obs = pd.DataFrame({{"sampleid": s["sample"],
                        "pct_counts_mt": rng.uniform(0, 4, n),
                        "n_genes_by_counts": (X > 0).sum(1), "total_counts": X.sum(1),
                        "doubletfinder_class": pd.Categorical(["Singlet"] * n)}},
                       index=[f"{{s['sample']}}_BC{{i}}" for i in range(n)])
    var = pd.DataFrame({{"gene_ids": genes, "mt": [g.startswith("mt-") for g in genes]}}, index=genes)
    ad.AnnData(X=X, obs=obs, var=var).write_h5ad(res / f"{{s['sample']}}.h5ad")
    man.append({{"sample": s["sample"], "included": "True", "ncell": n, "reason": "", "fallback": ""}})
    st.append({{"sample": s["sample"], "step": "nuclear_fraction", "status": "skipped",
                "message": "no BAM"}})
    rows.append({{"sampleid": s["sample"], "filter_ncell_before": 100, "filter_ncell_after": 80,
                 "doublet_ncell_after": n, "frac_retained": 0.4 if k == 0 else 0.9,
                 "filter_fail_mincount_only": 5, "filter_fail_minfeature_only": 3,
                 "filter_fail_mito_only": 7, "filter_fail_multiple": 5, "filter_fail_mito": 9,
                 "filter_n_mt_genes": 13, "filter_mt_matched_by": "pattern",
                 "filter_median_pct_counts_mt": 1.2,
                 "ambient_soupx_contamination_mean": 0.31 if k == 0 else 0.05,
                 "ambient_decontx_counts_removed_frac": "NA",
                 "ambient_decontx_contamination_mean": 0.07,
                 "doublet_doubletfinder_ndoublet": 4,
                 "concordance_doubletfinder_vs_scdblfinder_kappa": 0.1,
                 "nf_median": "NA"}})
def write(path, rows, delim=","):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=delim)
        w.writeheader(); w.writerows(rows)
write(res / "metrics.csv", rows)
write(res / "manifest.tsv", man, "\t")
write(res / "qc_status.csv", st)
(res / "report.html").write_text("<html>CellQC report</html>")
sys.exit(int(os.environ.get("FAKE_CELLQC_RC", "0")))
'''


def _tar_gz(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _web_summary(chem: str, ref: str) -> str:
    return f'<script>var data = [["Chemistry","{chem}"],["Transcriptome","{ref}"]];</script>'


@pytest.fixture
def delivery(tmp_path):
    """A 10x Cloud library (archives, GEM-X) and a classic outs/ library (Next GEM), both mouse."""
    root = tmp_path / "cohort"
    s1 = root / "S1"
    s1.mkdir(parents=True)
    for m in ("raw_feature_bc_matrix", "filtered_feature_bc_matrix"):
        (s1 / f"{m}.h5").write_bytes(b"")
        _tar_gz(s1 / f"{m}.tar.gz", {"barcodes.tsv.gz": gzip.compress(b"AAA-1\n"),
                                     "features.tsv.gz": gzip.compress(b"g\n"),
                                     "matrix.mtx.gz": gzip.compress(b"%\n")})
    _tar_gz(s1 / "analysis.tar.gz", {"clustering/gene_expression_graphclust/clusters.csv": b"x\n"})
    (s1 / "web_summary.html").write_text(_web_summary("Single Cell 3' v4 (polyA)", "GRCm39-2024-A"))
    s2 = root / "S2" / "outs"
    for m in ("raw_feature_bc_matrix", "filtered_feature_bc_matrix"):
        (s2 / m).mkdir(parents=True)
        (s2 / f"{m}.h5").write_bytes(b"")
    (s2 / "web_summary.html").write_text(_web_summary("Single Cell 3' v3 (polyA)", "GRCm39-2024-A"))
    return root


@pytest.fixture
def fake_cellqc(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "cellqc"
    exe.write_text(_FAKE_CELLQC.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "cellqc_argv.jsonl"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CELLQC_LOG", str(log))
    return log


def _ctx(ws: Path, root: "Path | None"):
    from aiscientist.agents.research_harness import HarnessContext
    return HarnessContext(decisions={"dataset_root": str(root)} if root else {}, workspace=ws)


def test_cellqc_end_to_end_with_a_fake_pipeline(tmp_path, delivery, fake_cellqc):
    pytest.importorskip("scanpy")
    from aiscientist.tools.run_cellqc.tool import run_cellqc
    ws = tmp_path / "run"
    out = run_cellqc({"max_pct_mt": 5}, _ctx(ws, delivery))
    assert out["status"] == "ok", out

    # Staged without writing into the delivery: archives unpacked into directories of their name.
    staged = ws / "work" / "cellqc" / "lnfiles"
    assert (staged / "S1" / "filtered_feature_bc_matrix" / "barcodes.tsv.gz").is_file()
    assert (staged / "S1" / "analysis" / "clustering" / "gene_expression_graphclust" / "clusters.csv").is_file()
    assert (staged / "S2" / "raw_feature_bc_matrix").is_symlink()
    assert not (delivery / "S1" / "filtered_feature_bc_matrix").exists()

    # Configured from the data: mouse gene sets, and the caller's mito threshold.
    config = json.loads((ws / "work" / "cellqc" / "config.yaml").read_text())
    assert config["geneset"]["mt"]["patterns"] == ["^mt-"]
    assert config["filterbycount"]["mito"] == 5.0
    assert out["assumptions"]["species"][0] == "mouse" and "GRCm39" in out["assumptions"]["species"][1]
    assert any("disagree on the 10x chemistry" in w for w in out["warnings"])   # v4 vs v3
    lines = (ws / "work" / "cellqc" / "samples.txt").read_text().splitlines()
    assert lines[0] == "sample\tcellranger\tnreaction" and len(lines) == 3

    # A dry run first, then the run.
    calls = [json.loads(ln) for ln in fake_cellqc.read_text().splitlines()]
    assert "-n" in calls[0] and "-n" not in calls[1]

    # The merged checkpoint has the shape the rest of the line reads.
    import anndata as ad
    adata = ad.read_h5ad(ws / "work" / "adata_qc.h5ad")
    assert set(adata.obs["sampleid"]) == {"S1", "S2"} and adata.n_obs == out["cells_after"] == 90
    assert "counts" in adata.layers and adata.raw is not None and "highly_variable" in adata.var
    assert out["obs_sample_key"] == "sampleid" and out["doublets"]["removed_upstream"]

    # The validation checklist became warnings; CellQC's report is in the artifacts.
    text = "\n".join(out["warnings"])
    assert "S1 kept only 40% of its Cell Ranger cells" in text and "failed only the max_pct_mt" in text
    assert "S1: mean ambient contamination 31%" in text
    assert out["libraries"][0]["ambient_decontx_contamination_mean"] == 0.07   # the compared method
    assert "agree poorly (kappa 0.10)" in text
    assert "nuclear_fraction" not in text                       # skipped without a BAM is expected
    assert (ws / "artifacts" / "cellqc" / "report.html").is_file()
    assert out["libraries"][0]["removed_only_by"]["max_pct_mt"] == 7


def test_a_nonzero_exit_with_results_is_a_warning_not_a_failure(tmp_path, delivery, fake_cellqc,
                                                                 monkeypatch):
    pytest.importorskip("scanpy")
    from aiscientist.tools.run_cellqc.tool import run_cellqc
    monkeypatch.setenv("FAKE_CELLQC_RC", "1")                     # e.g. the slide deck failed
    out = run_cellqc({"samples": ["S2"]}, _ctx(tmp_path / "run", delivery))
    assert out["status"] == "ok" and out["n_libraries"] == 1
    assert any("exited with status 1" in w for w in out["warnings"])


def test_a_mixed_species_cohort_is_refused_and_one_species_runs(tmp_path, delivery, fake_cellqc):
    # The 2026-10-02 test folder held two mouse libraries and a human one: QC'd together, the human
    # cells were filtered on mouse "^mt-" genes.
    pytest.importorskip("scanpy")
    from aiscientist.tools.run_cellqc.tool import run_cellqc
    (delivery / "S2" / "outs" / "web_summary.html").write_text(
        _web_summary("Single Cell 3' v3 (polyA)", "GRCh38-2024-A"))
    out = run_cellqc({}, _ctx(tmp_path / "run", delivery))
    assert out["status"] == "error" and "different species" in out["error"]
    assert out["species_by_library"] == {"human": ["S2"], "mouse": ["S1"]}
    assert "`samples`" in out["error"]
    one = run_cellqc({"samples": ["S2"]}, _ctx(tmp_path / "run2", delivery))
    assert one["status"] == "ok" and one["assumptions"]["species"][0] == "human"


def test_without_a_folder_it_points_at_run_scanpy_qc(tmp_path, fake_cellqc):
    from aiscientist.tools.run_cellqc.tool import run_cellqc
    out = run_cellqc({}, _ctx(tmp_path / "run", None))
    assert out["status"] == "error" and "run_scanpy_qc" in out["error"]


def test_without_cellqc_installed_it_says_so(tmp_path, delivery, monkeypatch):
    from aiscientist.tools.run_cellqc.tool import run_cellqc
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    out = run_cellqc({}, _ctx(tmp_path / "run", delivery))
    assert out["status"] == "not_enabled" and "run_scanpy_qc" in out["note"]


def test_a_folder_with_no_library_is_explained(tmp_path, fake_cellqc):
    from aiscientist.tools.run_cellqc.tool import run_cellqc
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "a.h5ad").write_bytes(b"")
    out = run_cellqc({}, _ctx(tmp_path / "run", tmp_path / "data"))
    assert out["status"] == "error" and "No Cell Ranger library" in out["error"]


def test_scrublet_does_not_remove_a_second_set_after_cellqc(tmp_path):
    ad = pytest.importorskip("anndata")
    import numpy as np
    import pandas as pd
    from aiscientist.tools.run_doublet_detection.tool import run_doublet_detection
    work = tmp_path / "run" / "work"
    work.mkdir(parents=True)
    X = np.ones((5, 3))
    obs = pd.DataFrame({"doubletfinder_class": pd.Categorical(["Singlet"] * 5)},
                       index=[f"c{i}" for i in range(5)])
    a = ad.AnnData(X=X, obs=obs)
    a.layers["counts"] = X.copy()
    a.write_h5ad(work / "adata_qc.h5ad")
    out = run_doublet_detection({}, _ctx(tmp_path / "run", None))
    assert out["status"] == "ok" and out["skipped"] and out["cells_after"] == 5
    assert out["doublets_called_upstream"] == ["doubletfinder_class"]
    assert "Scrublet was NOT run" in out["note"]


def test_text_columns_are_written_as_plain_strings_for_the_older_anndata_downstream():
    # run_cellqc writes with anndata 0.13 / pandas 3 (nullable strings); analysis.sif's anndata 0.12
    # reads those but cannot write them back, so the next tool died saving its checkpoint.
    ad = pytest.importorskip("anndata")
    import numpy as np
    import pandas as pd
    from aiscientist.tools.run_cellqc.tool import _plain_strings
    if hasattr(pd.options, "future") and hasattr(pd.options.future, "infer_string"):
        pd.set_option("future.infer_string", True)   # pandas 3 behaviour: str re-inferred
    obs = pd.DataFrame({"sampleid": pd.Categorical(pd.array(["a", "b"], dtype="string")),
                        "note": pd.array(["x", "y"], dtype="string")},
                       index=pd.Index(pd.array(["c1", "c2"], dtype="string")))
    var = pd.DataFrame({"gene_ids": pd.array(["E1", "E2", "E3"], dtype="string")},
                       index=pd.Index(pd.array(["G1", "G2", "G3"], dtype="string")))
    a = ad.AnnData(X=np.ones((2, 3)), obs=obs, var=var)
    a.raw = a.copy()                                     # .raw keeps its own var
    _plain_strings(a)
    assert a.raw.var.index.dtype == object
    assert a.obs.index.dtype == object and a.var.index.dtype == object
    assert a.obs["note"].dtype == object and a.var["gene_ids"].dtype == object
    assert a.obs["sampleid"].cat.categories.dtype == object
    assert a.raw.var["gene_ids"].dtype == object
    if hasattr(pd.options, "future") and hasattr(pd.options.future, "infer_string"):
        pd.set_option("future.infer_string", False)
