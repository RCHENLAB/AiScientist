"""run_de and run_pseudobulk_de no longer overwrite each other's DE tables.

Both wrote `de_<key>_all.csv`, `de_<key>_universe.txt`, `rank_<key>_*.rnk` and `rank_<key>_index.csv`
under the same key when `run_pseudobulk_de` grouped by the column a stratified `run_de` had used,
but only `run_de` wrote the per-group tables and the two labels beside them. After both ran, the
combined table and the universe were the pseudobulk test's while the per-group tables and the
"exploratory_ranking" label were still run_de's, so run_enrichment chose its input genes from the
cell-level Wilcoxon tables, by fold change, and reported them as the replicated test's enrichment.
A group literally called `all` had its own table written over by the combined one, too.

Now the pseudobulk tool writes under its own key, `pseudobulk_<group_key>`, with complete per-group
tables and its own labels; run_enrichment and run_gsea_prerank prefer a replicated test when the
caller names none, and say which table they read.
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from types import SimpleNamespace

import pytest

from aiscientist.tools._lib.scrna import group_table_slug, preferred_de_keys, pseudobulk_de_key


def test_the_names_and_the_preference():
    assert pseudobulk_de_key("majorclass") == "pseudobulk_majorclass"
    assert pseudobulk_de_key("") == "pseudobulk"
    assert group_table_slug("all") == "all_group" and group_table_slug("All") == "All_group"
    assert group_table_slug("Club/Secretory") == "Club_Secretory"
    assert preferred_de_keys(["leiden", "majorclass", "pseudobulk_majorclass", "cell_type"]) == [
        "pseudobulk_majorclass", "cell_type", "majorclass", "leiden"]
    assert preferred_de_keys([]) == []


# --- the real tools on a small replicated design ------------------------------------------------

def _design(tmp_path):
    """Two libraries per arm, three cell types, enough cells per library and type to test."""
    pytest.importorskip("scanpy")
    import anndata as ad
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(1)
    genes = [f"Gene{i}" for i in range(80)]
    n = 480
    x = rng.poisson(5.0, (n, len(genes))).astype(np.float32) + 1.0
    arm = np.array(["KO" if i % 2 else "WT" for i in range(n)])
    x[arm == "KO", :6] *= 3.0                     # a real condition effect on six genes
    adata = ad.AnnData(x)
    adata.var_names = genes
    adata.obs_names = [f"c{i}" for i in range(n)]
    adata.obs["condition"] = pd.Categorical(arm)
    adata.obs["sample"] = pd.Categorical([f"{a}{(i // 2) % 2 + 1}" for i, a in enumerate(arm)])
    adata.obs["majorclass"] = pd.Categorical([["Rod", "Cone", "MG"][(i // 4) % 3] for i in range(n)])
    path = tmp_path / "retina.h5ad"
    adata.write(path)
    ctx = SimpleNamespace(workspace=tmp_path, decisions={"dataset_path": str(path)})
    from aiscientist.tools.run_scanpy_qc.tool import run_scanpy_qc
    assert run_scanpy_qc({"min_genes": 1, "min_cells": 1, "max_pct_mt": 100.0}, ctx)["status"] == "ok"
    return ctx, tmp_path / "artifacts" / "tables"


def _digest(paths):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def _both(tmp_path):
    from aiscientist.tools.run_de.tool import run_de
    from aiscientist.tools.run_pseudobulk_de.tool import run_pseudobulk_de
    ctx, tables = _design(tmp_path)
    de = run_de({"groupby": "condition", "reference": "WT", "stratify_by": "majorclass"}, ctx)
    assert de["status"] == "ok" and de["inference"] == "exploratory_ranking"
    before = _digest([*tables.glob("de_majorclass_*"), *tables.glob("rank_majorclass_*")])
    pb = run_pseudobulk_de({"sample_key": "sample", "condition_key": "condition",
                            "group_key": "majorclass"}, ctx)
    assert pb["status"] == "ok", pb
    return ctx, tables, before, pb


def test_pseudobulk_leaves_run_des_tables_alone_and_writes_its_own_complete_set(tmp_path):
    _ctx, tables, before, pb = _both(tmp_path)
    # run_de's files are byte for byte what run_de wrote
    assert _digest([*tables.glob("de_majorclass_*"), *tables.glob("rank_majorclass_*")]) == before
    assert json.loads((tables / "de_majorclass_inference.json").read_text())["inference"] == "exploratory_ranking"
    # the pseudobulk test's own hand-off set
    assert pb["de_key"] == "pseudobulk_majorclass" and pb["de_table"] == "tables/de_pseudobulk_majorclass_all.csv"
    key = "pseudobulk_majorclass"
    for name in (f"de_{key}_all.csv", f"de_{key}_universe.txt", f"rank_{key}_index.csv",
                 "pseudobulk_all.csv"):
        assert (tables / name).is_file(), name
    tested = list(pb["results_by_group"])
    for grp in tested:
        assert (tables / f"de_{key}_{grp}.csv").is_file() and (tables / f"rank_{key}_{grp}.rnk").is_file()
        rows = (tables / f"de_{key}_{grp}.csv").read_text().splitlines()
        # complete: every tested gene, not the top-500 slice of the combined table
        assert len(rows) - 1 == pb["results_by_group"][grp]["n_genes_tested"]
    label = json.loads((tables / f"de_{key}_inference.json").read_text())
    assert label["tool"] == "run_pseudobulk_de" and label["inference"] == "replicated"
    assert label["unit_of_replication"] == "sample" and label["stratify_by"] == "majorclass"
    sig = json.loads((tables / f"de_{key}_significance.json").read_text())
    assert sig["per_group_tables"] == "complete" and set(sig["significant_by_group"]) == set(tested)


def _fake_gseapy(monkeypatch, seen):
    import pandas as pd

    def enrich(gene_list, gene_sets, background=None, outdir=None, verbose=False):
        seen.setdefault("enrich", []).append(sorted(gene_list))
        return SimpleNamespace(results=pd.DataFrame([{
            "Term": "t", "Gene_set": "Test", "Adjusted P-value": 0.01, "Combined Score": 3.0,
            "Overlap": "2/10", "Genes": "Gene0;Gene1"}]))

    def prerank(rnk, gene_sets, **_kw):
        seen.setdefault("prerank", []).append(rnk)
        return SimpleNamespace(res2d=pd.DataFrame([{
            "Term": "Test.gmt__t", "NES": 1.5, "NOM p-val": 0.01, "FDR q-val": 0.02,
            "Lead_genes": "Gene0"}]))

    fake = types.ModuleType("gseapy")
    fake.enrich, fake.prerank = enrich, prerank
    monkeypatch.setitem(sys.modules, "gseapy", fake)


def _genesets(tmp_path, monkeypatch):
    gdir = tmp_path / "genesets"
    gdir.mkdir()
    (gdir / "Test.gmt").write_text("t\tdesc\t" + "\t".join(f"Gene{i}" for i in range(10)) + "\n")
    monkeypatch.setenv("AISCIENTIST_GENESETS_DIR", str(gdir))


def test_enrichment_reads_the_replicated_test_unless_told_otherwise(tmp_path, monkeypatch):
    ctx, tables, _before, pb = _both(tmp_path)
    _genesets(tmp_path, monkeypatch)
    seen: dict = {}
    _fake_gseapy(monkeypatch, seen)
    from aiscientist.tools.run_enrichment.tool import run_enrichment

    out = run_enrichment({"gene_sets": ["Test"], "padj": 1.0, "lfc": 0.0}, ctx)
    assert out["status"] == "ok", out
    assert out["de_table_read"] == "tables/de_pseudobulk_majorclass_all.csv"
    assert "input_inference" not in out                       # a replicated test is inferential
    assert out["selection"]["upstream_table"] == "complete"
    assert out["background_source"] == "tested_universe"
    assert set(out["groups"]) <= {f"{g}{d}" for g in pb["results_by_group"] for d in ("", " (up)", " (down)")}

    # Named explicitly, the cell-level ranking is still there, with its own label.
    told = run_enrichment({"gene_sets": ["Test"], "groupby": "majorclass", "lfc": 0.0}, ctx)
    assert told["de_table_read"] == "tables/de_majorclass_all.csv"
    assert told["input_inference"] == "exploratory_ranking"


def test_gsea_ranks_the_replicated_test_and_only_its_own_groups(tmp_path, monkeypatch):
    ctx, tables, _before, pb = _both(tmp_path)
    _genesets(tmp_path, monkeypatch)
    seen: dict = {}
    _fake_gseapy(monkeypatch, seen)
    from aiscientist.tools.run_gsea_prerank.tool import run_gsea_prerank

    out = run_gsea_prerank({"gene_sets": ["Test"], "permutations": 10}, ctx)
    assert out["status"] == "ok", out
    read = [p.rsplit("/", 1)[-1] for p in out["read_from"]]
    assert read and all(r.startswith("rank_pseudobulk_majorclass_") for r in read)
    assert set(out["groups"]) == set(pb["results_by_group"])
    assert out["params"]["ranking_statistic"].startswith("pseudobulk test statistic")

    # A shorter key that the longer one starts with (an ungrouped pseudobulk test beside the
    # per-cell-type one): each reads exactly the groups its own index names.
    (tables / "rank_pseudobulk_index.csv").write_text("slug,group\nall_cells,all_cells\n")
    (tables / "rank_pseudobulk_all_cells.rnk").write_text("Gene0\t2.0\nGene1\t-1.0\n")
    short = run_gsea_prerank({"gene_sets": ["Test"], "permutations": 10, "groupby": "pseudobulk"}, ctx)
    assert [p.rsplit("/", 1)[-1] for p in short["read_from"]] == ["rank_pseudobulk_all_cells.rnk"]
    long = run_gsea_prerank({"gene_sets": ["Test"], "permutations": 10,
                             "groupby": "pseudobulk_majorclass"}, ctx)
    assert sorted(p.rsplit("/", 1)[-1] for p in long["read_from"]) == sorted(read)


def test_a_group_called_all_keeps_its_own_table(tmp_path, monkeypatch):
    pytest.importorskip("scanpy")
    import anndata as ad
    import numpy as np
    import pandas as pd

    from aiscientist.tools.run_de.tool import run_de
    rng = np.random.default_rng(2)
    x = rng.poisson(4.0, (200, 40)).astype(np.float32) + 1.0
    x[:100, :5] *= 4.0
    adata = ad.AnnData(x)
    adata.var_names = [f"Gene{i}" for i in range(40)]
    adata.obs_names = [f"c{i}" for i in range(200)]
    adata.obs["panel"] = pd.Categorical(["all"] * 100 + ["subset"] * 100)
    path = tmp_path / "x.h5ad"
    adata.write(path)
    ctx = SimpleNamespace(workspace=tmp_path, decisions={"dataset_path": str(path)})
    from aiscientist.tools.run_scanpy_qc.tool import run_scanpy_qc
    assert run_scanpy_qc({"min_genes": 1, "min_cells": 1, "max_pct_mt": 100.0}, ctx)["status"] == "ok"
    assert run_de({"groupby": "panel"}, ctx)["status"] == "ok"
    tables = tmp_path / "artifacts" / "tables"
    combined = (tables / "de_panel_all.csv").read_text()
    assert ",subset," in combined.replace("\n", ",") or "\nsubset," in combined
    assert "\nall," in combined                               # the combined table has both groups
    own = (tables / "de_panel_all_group.csv").read_text().splitlines()
    assert len(own) > 1 and all(line.startswith("all,") for line in own[1:])

    _genesets(tmp_path, monkeypatch)
    seen: dict = {}
    _fake_gseapy(monkeypatch, seen)
    from aiscientist.tools.run_enrichment.tool import run_enrichment
    out = run_enrichment({"gene_sets": ["Test"], "padj": 1.0, "lfc": 0.0}, ctx)
    assert out["status"] == "ok" and out["de_table_read"] == "tables/de_panel_all.csv"
    groups = {g.split(" (")[0] for g in out["genes_per_group"]}
    assert groups == {"all", "subset"}
    # group "all" was read from its own complete table, not from the combined one
    assert sum(n for g, n in out["genes_per_group"].items() if g.startswith("all")) == len(own) - 1
