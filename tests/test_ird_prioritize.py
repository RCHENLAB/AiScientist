"""Tests for the disease-model gene-level tiering (IRD-parity Phase 2; spec: docs/ird_filter_spec.md)."""
from __future__ import annotations

import pytest

from bioagent.tools.ird_prioritize import (
    annotate_disease_model,
    gene_models,
    parse_af,
)


def test_parse_af_handles_missing_and_novel_as_rarest():
    assert parse_af(None) == 0.0
    assert parse_af("") == 0.0
    assert parse_af(".") == 0.0
    assert parse_af("NA") == 0.0
    assert parse_af("0.0031") == pytest.approx(0.0031)
    assert parse_af(0.01) == 0.01
    assert parse_af("garbage") == 0.0


def test_dominant_single_ultra_rare_autosomal():
    # One variant at AF <= 1e-4 on an autosome ⇒ the gene is a dominant candidate.
    v = [{"gene": "RHO", "chrom": "chr3", "max_af": 5e-5}]
    assert gene_models(v)["RHO"] == {"dominant"}


def test_recessive_needs_two_rare_alleles():
    two = [{"gene": "USH2A", "chrom": "chr1", "max_af": 1e-3},
           {"gene": "USH2A", "chrom": "chr1", "max_af": 2e-3}]
    assert gene_models(two)["USH2A"] == {"recessive"}          # 2 alleles <=5e-3, none <=1e-4
    one = [{"gene": "USH2A", "chrom": "chr1", "max_af": 1e-3}]
    assert "USH2A" not in gene_models(one)                     # a single 5e-3 hit ⇒ no recessive model


def test_gene_can_be_both_dominant_and_recessive():
    v = [{"gene": "ABCA4", "chrom": "chr1", "max_af": 5e-5},   # ultra-rare ⇒ dominant
         {"gene": "ABCA4", "chrom": "chr1", "max_af": 2e-3}]   # +this makes 2 alleles <=5e-3 ⇒ recessive
    assert gene_models(v)["ABCA4"] == {"dominant", "recessive"}


def test_novel_variant_counts_as_rarest():
    v = [{"gene": "CRB1", "chrom": "chr1", "max_af": None}]    # not in gnomAD ⇒ treated as 0.0
    assert gene_models(v)["CRB1"] == {"dominant"}


def test_x_linked_rule():
    v = [{"gene": "RPGR", "chrom": "chrX", "max_af": 3e-5}]
    assert gene_models(v)["RPGR"] == {"x_linked"}             # X uses the dominant (hemizygous) threshold
    # a single 5e-3 X variant does not qualify (needs <=1e-4)
    assert "RPGR" not in gene_models([{"gene": "RPGR", "chrom": "chrX", "max_af": 2e-3}])


def test_common_variant_does_not_fit_even_if_gene_qualifies():
    v = [{"gene": "EYS", "chrom": "chr6", "max_af": 5e-5},     # makes EYS dominant
         {"gene": "EYS", "chrom": "chr6", "max_af": 2e-2}]     # this one is common (>5e-3)
    out = annotate_disease_model(v)
    assert out[0]["fits_disease_model"] and out[0]["disease_model"] == ["dominant"]
    assert not out[1]["fits_disease_model"] and out[1]["disease_model"] == []


def test_annotate_is_pure_and_adds_keys():
    v = [{"gene": "RHO", "chrom": "chr3", "max_af": 5e-5}]
    out = annotate_disease_model(v)
    assert out is not v and out[0] is not v[0]         # new list + new dicts
    assert "disease_model" not in v[0]                 # input untouched
    assert out[0]["disease_model"] == ["dominant"] and out[0]["fits_disease_model"] is True


def test_chrom_and_af_field_fallbacks():
    # chrom derived from a 'location' string; AF read from gnomAD_AF/filt_freq aliases.
    v = [{"gene_symbol": "PDE6B", "location": "chr4:600000", "gnomAD_AF": "0.00003"}]
    m = gene_models(v)
    assert m["PDE6B"] == {"dominant"}
    v2 = [{"gene": "PDE6B", "chrom": "4", "filt_freq": 5e-5}]  # bare '4' ⇒ autosome
    assert gene_models(v2)["PDE6B"] == {"dominant"}


def test_gene_with_no_qualifying_variant_absent():
    v = [{"gene": "TTN", "chrom": "chr2", "max_af": 0.2}]      # common only
    assert "TTN" not in gene_models(v)
    out = annotate_disease_model(v)
    assert out[0]["fits_disease_model"] is False
