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
