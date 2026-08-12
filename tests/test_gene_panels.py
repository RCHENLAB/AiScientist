"""Tests for the known-gene panels (IRD-parity roadmap, Phase 1 layer 1)."""
from __future__ import annotations

import pytest

from bioagent.tools.gene_panels import available_panels, load_gene_panel


def test_ird_panel_loads_and_is_sane():
    genes = load_gene_panel("ird")
    # The lab's RetNet list is ~258 genes; guard the ballpark so a truncated asset is caught.
    assert 240 <= len(genes) <= 280
    assert genes == sorted(set(genes))  # sorted + de-duplicated


def test_ird_panel_contains_canonical_ird_genes():
    genes = set(load_gene_panel("ird"))
    # The genes the lab reference shortlist led with (and classic IRD genes) must be present.
    for g in ("CRB1", "RP1L1", "USH2A", "EYS", "ABCA4", "RPGR", "RPE65", "RHO"):
        assert g in genes


def test_panel_name_is_case_insensitive_and_aliased():
    assert load_gene_panel("IRD") == load_gene_panel("ird_retnet") == load_gene_panel("retnet")


def test_comments_and_blank_lines_are_ignored():
    genes = load_gene_panel("ird")
    assert not any(g.startswith("#") for g in genes)
    assert "" not in genes


def test_unknown_panel_raises():
    # An unknown panel must raise, not silently return [] (which would annotate genome-wide).
    with pytest.raises(KeyError):
        load_gene_panel("nope")


def test_available_panels_deduplicated_by_file():
    names = available_panels()
    assert "ird" in names
    assert len(names) == 1  # all current aliases map to the one IRD file


def test_settings_reads_default_gene_panel(monkeypatch):
    # The gateway applies this panel deterministically as known-gene-first (no longer model-dependent).
    from bioagent.gateway.settings import HPCSettings
    monkeypatch.setenv("BIOAGENT_DEFAULT_GENE_PANEL", "ird")
    s = HPCSettings.from_env()
    assert s.default_gene_panel == "ird"
    assert len(load_gene_panel(s.default_gene_panel)) > 200


def test_settings_default_gene_panel_empty_by_default(monkeypatch):
    # Unset ⇒ empty ⇒ genome-wide (no accidental panel restriction for a non-IRD deployment).
    from bioagent.gateway.settings import HPCSettings
    monkeypatch.delenv("BIOAGENT_DEFAULT_GENE_PANEL", raising=False)
    assert HPCSettings.from_env().default_gene_panel == ""


def test_settings_reads_default_regions_bed(monkeypatch):
    # The PRE-VEP region restriction (the compute-saving panel) — separate from the gene-name filter.
    from bioagent.gateway.settings import HPCSettings
    monkeypatch.setenv("BIOAGENT_DEFAULT_REGIONS_BED", "/ref/retcap_v5.clean.bed")
    assert HPCSettings.from_env().default_regions_bed == "/ref/retcap_v5.clean.bed"
    monkeypatch.delenv("BIOAGENT_DEFAULT_REGIONS_BED", raising=False)
    assert HPCSettings.from_env().default_regions_bed == ""
