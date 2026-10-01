"""A tool can read the file a run_code step wrote, not only the previous tool's checkpoint.

Every analysis tool used to find its input by a FIXED name — adata_qc.h5ad → adata_clustered.h5ad →
adata_de.h5ad, and run_de's rank_<groupby>_<group>.rnk for GSEA. So an upstream step done in
run_code broke the chain: the next tool reported its checkpoint missing (3 of the 8 failed tool
calls in the archived runs), and the ways round it were to overwrite a tool's checkpoint (3 archived
snippets did) or to redo the rest of the line in run_code, without the tools' guards. `input` names
the file instead.

What these tests pin:
  * without `input` nothing changes — same checkpoint, same error when it is missing;
  * with it, the named file is read and the tools' own checkpoints are left alone;
  * `input` cannot reach outside this run's work/ and artifacts/ directories;
  * every result says which file it read (`read_from`), and a set `input` is recorded as a
    non-default setting.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bioagent.tools import catalog as tools_catalog
from bioagent.tools.run_clustering import tool as run_clustering_tool
from bioagent.tools.run_de import tool as run_de_tool
from bioagent.tools.run_gsea_prerank import tool as run_gsea_prerank_tool
from bioagent.tools.run_scanpy_qc import tool as run_scanpy_qc_tool
from bioagent.tools._lib.scrna import _run_files

_READS_A_FILE = ("run_scanpy_qc", "run_clustering", "run_de", "run_depth_matched_de",
                 "run_gsea_prerank", "run_doublet_detection", "run_integration",
                 "run_pseudobulk_de", "run_composition", "run_marker_annotation")


def _ctx(ws: Path, **decisions) -> SimpleNamespace:
    return SimpleNamespace(workspace=ws, decisions=decisions)


def _file(ws: Path, rel: str) -> Path:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


# --- where `input` may point ---------------------------------------------------------------------


def test_every_spelling_of_a_file_in_the_run_resolves_to_it(tmp_path):
    ws = tmp_path / "run"
    mine = _file(ws, "work/adata_subset.h5ad")
    exported = _file(ws, "artifacts/data/cells.h5ad")

    for spelled in ("adata_subset.h5ad", "work/adata_subset.h5ad", "./work/adata_subset.h5ad",
                    "$BIOAGENT_WORK/adata_subset.h5ad", "${BIOAGENT_WORK}/adata_subset.h5ad",
                    str(mine)):
        found, why = _run_files(_ctx(ws), spelled, (".h5ad",))
        assert found == [mine.resolve()], (spelled, why)
    for spelled in ("artifacts/data/cells.h5ad", "$BIOAGENT_ARTIFACTS/data/cells.h5ad"):
        found, why = _run_files(_ctx(ws), spelled, (".h5ad",))
        assert found == [exported.resolve()], (spelled, why)


def test_nothing_outside_the_run_is_read(tmp_path):
    ws = tmp_path / "run"
    (ws / "work").mkdir(parents=True)
    outside = _file(tmp_path, "other/elsewhere.h5ad")
    (ws / "work" / "link.h5ad").symlink_to(outside)     # a link is judged by where it POINTS

    for spelled in (str(outside), "../../other/elsewhere.h5ad", "work/../../other/elsewhere.h5ad",
                    "link.h5ad"):
        found, why = _run_files(_ctx(ws), spelled, (".h5ad",))
        assert found == [] and "outside" in why, spelled


def test_a_missing_file_is_refused_with_the_files_that_do_exist(tmp_path):
    ws = tmp_path / "run"
    _file(ws, "work/adata_qc.h5ad")
    _file(ws, "work/mine.h5ad")

    found, why = _run_files(_ctx(ws), "mnie.h5ad", (".h5ad",))

    assert found == []
    assert "adata_qc.h5ad, mine.h5ad" in why, "the model needs the real names to correct itself"


def test_the_wrong_kind_of_file_is_refused(tmp_path):
    ws = tmp_path / "run"
    _file(ws, "work/table.csv")

    found, why = _run_files(_ctx(ws), "table.csv", (".h5ad",))

    assert found == [] and ".h5ad" in why


def test_a_pattern_selects_several_files_only_where_the_tool_allows_it(tmp_path):
    ws = tmp_path / "run"
    a = _file(ws, "work/my_rank_A.rnk")
    b = _file(ws, "work/my_rank_B.rnk")
    _file(ws, "work/my_rank_notes.txt")

    found, _ = _run_files(_ctx(ws), "my_rank_*", (".rnk",), pattern_ok=True)
    assert found == [a.resolve(), b.resolve()]
    found, _ = _run_files(_ctx(ws), "my_rank_*.rnk", (".rnk",))      # a star is just a name here
    assert found == []


# --- what the model is told, and what gets recorded --------------------------------------------


def test_every_tool_that_reads_a_file_offers_input():
    catalog = {t.name: t for t in tools_catalog.scrna_catalog()}
    for name in _READS_A_FILE:
        spec = catalog[name].parameters["properties"].get("input")
        assert spec, f"{name} has no `input`"
        assert spec["type"] == "string" and spec["default"] == ""
        assert "work/" in spec["description"] and "artifacts/" in spec["description"]


def test_a_set_input_is_recorded_as_a_non_default_setting():
    """So the technical report says which step read a file of the model's choosing."""
    from bioagent.agents.research_harness import nondefault_params

    schema = {t.name: t for t in tools_catalog.scrna_catalog()}["run_de"].parameters

    recorded = nondefault_params(schema, {"input": "adata_alpha.h5ad"})

    assert [(r["param"], r["value"], r["kind"]) for r in recorded] == [
        ("input", "adata_alpha.h5ad", "selection")]
    assert nondefault_params(schema, {}) == []


# --- end to end, on real scanpy objects -----------------------------------------------------------


def _raw_dataset(path: Path, *, n_per_arm: int = 40, n_genes: int = 120) -> None:
    """Raw counts: two cell types x two arms, with a handful of genes up in KO."""
    import anndata as ad
    import pandas as pd

    rng = np.random.default_rng(0)
    rows, arms, types_ = [], [], []
    for cell_type in ("Alpha", "Beta"):
        for arm in ("KO", "WT"):
            for _ in range(n_per_arm):
                x = rng.poisson(4.0, n_genes).astype(np.float32)
                if arm == "KO":
                    x[:6] += rng.poisson(30.0, 6)
                rows.append(x)
                arms.append(arm)
                types_.append(cell_type)
    a = ad.AnnData(np.vstack(rows))
    a.var_names = [f"GENE{i}" for i in range(n_genes)]
    a.obs_names = [f"c{i}" for i in range(a.n_obs)]
    a.obs["condition"] = pd.Categorical(arms)
    a.obs["celltype"] = pd.Categorical(types_)
    a.write(path)


@pytest.fixture()
def qc_ctx(tmp_path):
    """A run whose QC checkpoint was written by run_scanpy_qc itself."""
    pytest.importorskip("scanpy")
    raw = tmp_path / "raw.h5ad"
    _raw_dataset(raw)
    ctx = _ctx(tmp_path / "run", dataset_path=str(raw))
    out = run_scanpy_qc_tool.run_scanpy_qc({"min_genes": 1, "min_cells": 1, "max_pct_mt": 100.0}, ctx)
    assert out["status"] == "ok" and out["read_from"] == "the bound dataset (raw.h5ad)"
    return ctx


def test_run_de_reads_the_file_a_run_code_step_wrote_and_rewrites_no_checkpoint(qc_ctx):
    import anndata as ad

    work = Path(qc_ctx.workspace) / "work"
    qc_bytes = (work / "adata_qc.h5ad").read_bytes()
    cells = ad.read_h5ad(work / "adata_qc.h5ad")
    cells[cells.obs["celltype"] == "Alpha"].copy().write(work / "adata_alpha.h5ad")  # the run_code step

    contrast = {"groupby": "condition", "reference": "WT", "stratify_by": "celltype"}
    mine = run_de_tool.run_de({**contrast, "input": "adata_alpha.h5ad"}, qc_ctx)
    usual = run_de_tool.run_de(contrast, qc_ctx)

    assert mine["status"] == "ok" and mine["read_from"] == "work/adata_alpha.h5ad"
    assert set(mine["cells_by_group_and_arm"]) == {"Alpha"}
    assert usual["read_from"] == "work/adata_qc.h5ad"
    assert set(usual["cells_by_group_and_arm"]) == {"Alpha", "Beta"}
    assert (work / "adata_qc.h5ad").read_bytes() == qc_bytes


def test_a_line_whose_qc_was_done_in_run_code_continues_in_the_tools(qc_ctx):
    """The dead end this removes: QC done in run_code under its own name, so every tool after it
    refused to run and the rest of the analysis had to be hand-written too."""
    work = Path(qc_ctx.workspace) / "work"
    (work / "adata_qc.h5ad").rename(work / "my_qc.h5ad")
    assert run_clustering_tool.run_clustering({}, qc_ctx)["error"].startswith("run_scanpy_qc must run first")

    clustered = run_clustering_tool.run_clustering({"input": "my_qc.h5ad", "n_pcs": 10}, qc_ctx)
    de = run_de_tool.run_de({}, qc_ctx)                   # and the chain carries on from the tool

    assert clustered["status"] == "ok" and clustered["read_from"] == "work/my_qc.h5ad"
    assert de["status"] == "ok" and de["read_from"] == "work/adata_clustered.h5ad"


def test_qc_can_start_from_a_file_instead_of_the_bound_dataset(qc_ctx, tmp_path):
    import anndata as ad

    work = Path(qc_ctx.workspace) / "work"
    raw = ad.read_h5ad(tmp_path / "raw.h5ad")
    raw[raw.obs["celltype"] == "Beta"].copy().write(work / "beta_only.h5ad")

    out = run_scanpy_qc_tool.run_scanpy_qc(
        {"min_genes": 1, "min_cells": 1, "max_pct_mt": 100.0, "input": "beta_only.h5ad"}, qc_ctx)

    assert out["status"] == "ok" and out["read_from"] == "work/beta_only.h5ad"
    assert out["cells_before"] == 80


def test_a_bad_input_is_an_error_that_reads_nothing(qc_ctx):
    out = run_de_tool.run_de({"input": "/etc/passwd.h5ad"}, qc_ctx)
    assert out["status"] == "error" and "outside" in out["error"]


# --- GSEA on ranked lists of the model's own ---------------------------------------------------------


def test_gsea_ranks_the_lists_it_is_given(tmp_path, monkeypatch):
    pd = pytest.importorskip("pandas")
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "TestPathways.gmt").write_text("SET_A\tdesc\tG1\tG2\tG3\n", encoding="utf-8")
    monkeypatch.setenv("BIOAGENT_GENESETS_DIR", str(gdir))
    seen: list[str] = []

    def _prerank(rnk, gene_sets, **_kwargs):
        seen.append(Path(rnk).name)
        return SimpleNamespace(res2d=pd.DataFrame([{
            "Term": "TestPathways.gmt__SET_A", "NES": 1.8, "FDR q-val": 0.01,
            "NOM p-val": 0.001, "Lead_genes": "G1;G2"}]))

    fake = types.ModuleType("gseapy")
    fake.prerank = _prerank
    monkeypatch.setitem(sys.modules, "gseapy", fake)
    ws = tmp_path / "run"
    for group in ("A", "B"):
        _file(ws, f"work/my_rank_{group}.rnk").write_text("G1\t3.0\nG2\t2.0\nG3\t-1.0\n",
                                                          encoding="utf-8")

    out = run_gsea_prerank_tool.run_gsea_prerank(
        {"gene_sets": ["TestPathways"], "input": "my_rank_*.rnk"}, _ctx(ws))

    assert out["status"] == "ok"
    assert seen == ["my_rank_A.rnk", "my_rank_B.rnk"]
    assert out["groups"] == ["my_rank_A", "my_rank_B"]
    assert out["read_from"] == ["work/my_rank_A.rnk", "work/my_rank_B.rnk"]
    assert out["params"]["ranking_statistic"] == "as supplied in `input`"
