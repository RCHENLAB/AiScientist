"""run_marker_annotation takes its markers from a curated reference when the tissue has one, and
withdraws a label none of whose specific markers is in the cluster.

Found by the 2026-10-02 HPC3 e2e on a human macula sample: the model wrote the retina panel from
memory (OPN4/NEFM/MEF2C as cone markers, RLBP1 under bipolar, astrocyte genes as Muller
discriminators), 1,631 Muller glia came out "Unassigned"/"ganglion", microglia came out "cone",
and the Critic accepted it. These tests run the real tool on small synthetic objects."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

sc = pytest.importorskip("scanpy")
ad = pytest.importorskip("anndata")

from aiscientist.agents.research_harness import HarnessContext  # noqa: E402
from aiscientist.tools.run_marker_annotation.tool import (  # noqa: E402
    available_references,
    run_marker_annotation,
)

# What each synthetic cluster expresses (log-normalised units), on top of a quiet background.
_HUMAN = {
    "0": {"RLBP1": 3.0, "GLUL": 4.0, "SLC1A3": 2.0, "APOE": 2.0, "VSX2": 0.4},      # Muller glia
    "1": {"VSX2": 1.5, "GRM6": 2.0, "TRPM1": 3.0, "PRKCA": 1.0},                   # ON bipolar
    "2": {"VSX2": 0.9, "GRIK1": 2.5},                                              # OFF bipolar
    "3": {"C1QA": 2.5, "C1QB": 2.0, "P2RY12": 1.0},                                # microglia
}
# The panel the model actually passed on 2026-10-02 (abridged): wrong genes, retina type names.
_MODEL_PANEL = {
    "Photoreceptor (cone)": ["OPN4", "NEFM", "MEF2C"],
    "Bipolar cell": ["GRM6", "GRM7", "RLBP1"],
    "Ganglion cell": ["RBPMS", "CHL1", "NTRK1"],
    "Muller glia": ["GFAP", "S100B", "PAX2"],
    "Doublet (mixed)": ["TUBB2A", "RLBP1"],
}


def _object(spec: dict[str, dict[str, float]], n: int = 40, mouse: bool = False) -> "ad.AnnData":
    rng = np.random.default_rng(0)
    named = sorted({g for genes in spec.values() for g in genes}
                   | {"RHO", "NRL", "GNAT1", "ARR3", "GNAT2", "GFAP", "PAX2", "RBPMS", "SNCG",
                      "ONECUT1", "GAD1", "PECAM1", "OPN4", "NEFM", "MEF2C", "GRM7", "CHL1",
                      "NTRK1", "S100B", "TUBB2A", "RLBP1", "GLUL"})
    genes = named + [f"BG{i}" for i in range(120)]
    clusters = [c for c in spec for _ in range(n)]
    # A sparse background, as in real data: most genes are zero in most cells.
    X = np.where(rng.random((len(clusters), len(genes))) < 0.02,
                 rng.uniform(0.0, 0.15, size=(len(clusters), len(genes))), 0.0)
    for i, c in enumerate(clusters):
        for g, v in spec[c].items():
            X[i, genes.index(g)] = v + rng.uniform(-0.1, 0.1)
    names = [(g[:1] + g[1:].lower()) if mouse and not g.startswith("BG") else g for g in genes]
    a = ad.AnnData(X=X.astype("float32"))
    a.var_names = names
    a.obs_names = [f"c{i}" for i in range(len(clusters))]
    a.obs["leiden"] = clusters
    a.raw = a.copy()
    return a


def _run(tmp_path: Path, a: "ad.AnnData", args: dict) -> dict:
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    a.write_h5ad(work / "adata_clustered.h5ad")
    return run_marker_annotation(args, HarnessContext(decisions={}, workspace=tmp_path))


def _table(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "artifacts/tables/cluster_cell_types.json").read_text())


def test_retina_is_a_curated_reference():
    assert "retina" in available_references()


def test_a_model_written_retina_panel_is_replaced_by_the_curated_reference(tmp_path):
    out = _run(tmp_path, _object(_HUMAN), {"panel": _MODEL_PANEL})
    assert out["status"] == "ok"
    ref = out["reference"]
    assert ref["name"] == "retina" and ref["species"] == "human" and ref["auto_applied"]
    assert ref["caller_types_mapped"]["Photoreceptor (cone)"] == "Cone photoreceptor"
    assert out["labels"] == {"0": "Muller glia", "1": "Bipolar cell", "2": "Bipolar cell",
                             "3": "Microglia"}
    # The custom type keeps its own genes, minus another lineage's specific marker.
    assert ref["genes_dropped"] == {"Doublet (mixed)": ["RLBP1"]}
    assert any("RLBP1: Muller glia" in w for w in out["warnings"])
    # OFF bipolar cells pass on GRIK1 (GRM6/TRPM1 mark ON bipolar cells only).
    assert "GRIK1" in _table(tmp_path)["2"]["canonical_markers_enriched"]


def test_a_named_reference_needs_no_panel_and_finds_mouse_symbols(tmp_path):
    out = _run(tmp_path, _object(_HUMAN, mouse=True), {"reference": "retina"})
    assert out["reference"]["species"] == "mouse" and not out["reference"]["auto_applied"]
    assert out["labels"]["0"] == "Muller glia" and out["labels"]["3"] == "Microglia"


def test_an_unknown_reference_is_refused_with_the_list(tmp_path):
    out = _run(tmp_path, _object(_HUMAN), {"reference": "liver"})
    assert out["status"] == "error" and "retina" in out["error"]


def test_a_missing_expected_lineage_is_a_warning_and_absent_usual_ones_are_noted(tmp_path):
    no_glia = {k: v for k, v in _HUMAN.items() if k != "0"}
    out = _run(tmp_path, _object(no_glia), {"reference": "retina"})
    assert any("No cluster was labelled Muller glia" in w and "RLBP1 detected in" in w
               for w in out["warnings"])
    rod = next(n for n in out["composition_notes"] if n.startswith("No Rod photoreceptor cluster"))
    assert "RHO detected in" in rod and "depleted" in rod          # ~2% background detection


def test_a_label_whose_markers_are_not_enriched_is_withdrawn(tmp_path):
    # Without a reference: lineage A's marker X is just as high in the cluster called B, so the two
    # clusters called A carry nothing that sets them apart as A.
    spec = {"0": {"XGENE": 3.0}, "1": {"XGENE": 3.0}, "2": {"XGENE": 3.0, "YGENE": 4.0}}
    out = _run(tmp_path, _object(spec), {
        "reference": "none", "panel": {"A": ["XGENE"], "B": ["YGENE"]},
        "discriminators": {"A": ["XGENE"], "B": ["YGENE"]}, "dominance_ratio": 1.0})
    assert out["labels"] == {"0": "Unassigned", "1": "Unassigned", "2": "B"}
    assert out["canonical_check_failed"] == ["0", "1"]
    row = _table(tmp_path)["0"]
    assert row["withdrawn_label"] == "A" and "XGENE" in row["evidence"]


def test_a_cluster_with_glul_but_not_rlbp1_is_not_called_muller_glia(tmp_path):
    # Run f3b8268c4fd4: 27 cells called Muller glia on Glul alone (+2.81) with Rlbp1 +0.28 in 44% of
    # them, while their own top genes (Best2, Ptgds, Trpm3) were no retinal glia's. GLUL is shared
    # with astrocytes and ocular epithelia; RLBP1 is the marker Muller glia cannot be called without.
    spec = {**_HUMAN, "4": {"GLUL": 3.0, "APOE": 2.0, "BEST2": 3.0, "PTGDS": 2.5}}
    out = _run(tmp_path, _object(spec), {"reference": "retina"})
    assert out["labels"]["0"] == "Muller glia"            # RLBP1 + GLUL: still Muller glia
    assert out["labels"]["4"] == "Unassigned"
    assert "4" in out["canonical_check_failed"]
    row = _table(tmp_path)["4"]
    assert row["withdrawn_label"] == "Muller glia" and row["unassigned_reason"] == "withdrawn"
    assert "RLBP1" in row["evidence"] and "shared" in row["evidence"]
    # ...and it is described by what it does express.
    assert {"BEST2", "PTGDS"} <= set(out["unassigned_detail"]["4"]["top_markers"])
    assert out["reference"]["defining"]["Muller glia"] == ["RLBP1"]


def test_an_unassigned_cluster_says_which_lineages_it_mixes(tmp_path):
    # Microglia markers mean 1.75 against bipolar 1.37: both present, neither 1.5x the other.
    spec = {**_HUMAN, "4": {"VSX2": 1.5, "GRM6": 2.0, "TRPM1": 2.0, "C1QA": 2.0, "P2RY12": 1.5}}
    out = _run(tmp_path, _object(spec), {"reference": "retina"})
    detail = out["unassigned_detail"]["4"]
    assert detail["reason"] == "mixed"
    assert detail["evidence"].startswith("mixed: Microglia") and "Bipolar cell" in detail["evidence"]
    assert _table(tmp_path)["1"]["top_markers"]           # every cluster carries its top markers


def test_a_labelled_cluster_led_by_another_lineages_genes_is_named(tmp_path):
    # Rod markers clear the checks, but the cluster's own top genes are ganglion-cell genes.
    spec = {**_HUMAN, "4": {"RHO": 3.0, "NRL": 2.0, "GNAT1": 2.5},
            "5": {"RHO": 2.0, "NRL": 1.5, "GNAT1": 1.8, "NEFL": 3.0, "NEFM": 2.5}}
    out = _run(tmp_path, _object(spec), {"reference": "retina"})
    assert out["labels"]["5"] == "Rod photoreceptor"
    crossed = _table(tmp_path)["5"]["top_markers_of_other_lineages"]
    assert any(c.startswith("NEFL (Retinal ganglion cell)") for c in crossed)
    assert any("5 (Rod photoreceptor)" in w and "NEFL" in w for w in out["warnings"])
    assert "top_markers_of_other_lineages" not in _table(tmp_path)["4"]


def test_counts_in_place_of_log_expression_are_refused(tmp_path):
    # Run 7f2955e8e: an object whose X and .raw held integer counts reached the annotation; with
    # thresholds in log units, 52.9% of cells came out Unassigned.
    a = _object(_HUMAN)
    a.X = np.round(a.X * 100)
    a.raw = a.copy()
    out = _run(tmp_path, a, {"reference": "retina"})
    assert out["status"] == "error" and "integer counts" in out["error"] and "log units" in out["error"]


def test_run_de_refuses_counts_too(tmp_path):
    from aiscientist.tools.run_de.tool import run_de
    a = _object(_HUMAN)
    a.X = np.round(a.X * 100)
    a.raw = a.copy()
    out = _run_tool(tmp_path, a, run_de, {"groupby": "leiden"})
    assert out["status"] == "error" and "integer counts" in out["error"]
    assert "log2 fold-changes" in out["error"]


def _run_tool(tmp_path: Path, a: "ad.AnnData", fn, args: dict) -> dict:
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    a.write_h5ad(work / "adata_clustered.h5ad")
    return fn(args, HarnessContext(decisions={}, workspace=tmp_path))


def test_reference_none_does_not_let_a_panel_rename_retina_lineages(tmp_path):
    # Run 4de7ab04a239: reference "none" + a panel written from DE tables with names and genes
    # crossed ("Cone photoreceptor" = rod genes) — 8,132 rods came out cones.
    spec = {**_HUMAN, "4": {"RHO": 4.0, "NRL": 2.0, "GNAT1": 3.0},
            "5": {"ARR3": 4.0, "PDE6H": 3.0, "GNAT2": 2.5}}
    crossed = {"Cone photoreceptor": ["RHO", "NRL", "GNAT1"], "Rod photoreceptor": ["ARR3", "PDE6H"],
               "Muller glia": ["RLBP1", "GLUL"], "Microglia": ["C1QA"]}
    out = _run(tmp_path, _object(spec), {"reference": "none", "panel": crossed,
                                         "discriminators": crossed})
    assert out["labels"]["4"] == "Rod photoreceptor" and out["labels"]["5"] == "Cone photoreceptor"
    assert out["reference"]["name"] == "retina"
    assert any("`reference: none` was overridden" in w for w in out["warnings"])
    bad = out["reference"]["caller_panel_contradictions"]
    assert "Rod photoreceptor" in bad["Cone photoreceptor"] and "Cone photoreceptor" in bad["Rod photoreceptor"]


def test_reference_none_still_works_for_a_tissue_without_one(tmp_path):
    spec = {"0": {"ALB": 3.0}, "1": {"KRT19": 3.0}}
    out = _run(tmp_path, _object(spec), {"reference": "none", "panel": {"Hepatocyte": ["ALB"],
                                                                         "Cholangiocyte": ["KRT19"]},
                                         "dominance_ratio": 1.0})
    assert out["reference"] == {} and out["labels"] == {"0": "Hepatocyte", "1": "Cholangiocyte"}


def test_the_annotation_draws_its_own_umap_and_marker_dot_plot(tmp_path):
    """The labels' two figures used to exist only when the plan carried a model-written figure
    step; a plan that asks for QC and annotation has none."""
    a = _object(_HUMAN)
    a.obsm["X_umap"] = np.random.default_rng(1).normal(size=(a.n_obs, 2)).astype("float32")
    out = _run(tmp_path, a, {"reference": "retina"})
    assert out["status"] == "ok"
    assert out["figures"] == ["figures/umap_cell_types.png", "figures/dotplot__cell_type_markers.png"]
    for rel in out["figures"]:
        assert (tmp_path / "artifacts" / rel).stat().st_size > 5_000
    # Without an embedding the dot plot is still drawn, and nothing fails.
    out = _run(tmp_path / "no_umap", _object(_HUMAN), {"reference": "retina"})
    assert out["status"] == "ok" and out["figures"] == ["figures/dotplot__cell_type_markers.png"]
