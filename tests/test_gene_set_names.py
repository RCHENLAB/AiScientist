"""A library the plan names but the host lacks: the tools say which ones ARE installed.

Run 7f2955e8e (2026-10-08) pre-registered MSigDB "M5"/"M6"; run_enrichment's error said only "run
scripts/fetch_genesets.py", and the Scientist spent its ORA attempts hunting for that script and for
.gmt files while GO_Biological_Process_2023, Reactome_2022 and MSigDB_Hallmark_2020 sat installed."""
from __future__ import annotations

from pathlib import Path

import pytest

from aiscientist.agents.research_harness import HarnessContext
from aiscientist.tools.run_enrichment.tool import make_tool as enrichment_tool, run_enrichment
from aiscientist.tools.run_gsea_prerank.tool import make_tool as gsea_tool, run_gsea_prerank


def _genesets(tmp_path: Path, monkeypatch) -> None:
    d = tmp_path / "genesets"
    d.mkdir()
    (d / "MSigDB_Hallmark_2020.gmt").write_text("HALLMARK_X\tdesc\tA\tB\tC\n")
    monkeypatch.setenv("AISCIENTIST_GENESETS_DIR", str(d))


def test_an_unknown_library_names_the_installed_ones(tmp_path, monkeypatch):
    # Both tools import gseapy before they look at library names; CI installs no gseapy.
    pytest.importorskip("gseapy")
    _genesets(tmp_path, monkeypatch)
    ctx = HarnessContext(decisions={}, workspace=tmp_path)
    for out in (run_enrichment({"gene_sets": ["M5", "M6"]}, ctx),
                run_gsea_prerank({"gene_sets": ["M5", "M6"]}, ctx)):
        assert out["status"] == "error"
        assert "Not installed: M5, M6" in out["error"]
        assert "Installed (use these names exactly): MSigDB_Hallmark_2020" in out["error"]
        assert "do not search for one" in out["error"]


def test_the_descriptions_list_the_installed_libraries(tmp_path, monkeypatch):
    _genesets(tmp_path, monkeypatch)
    for tool in (enrichment_tool(), gsea_tool()):
        assert "Installed libraries: MSigDB_Hallmark_2020" in tool.description
