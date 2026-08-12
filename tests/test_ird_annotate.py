"""Tests for the IRD annotation layers + reason_for_inclusion cascade (docs/ird_filter_spec.md)."""
from __future__ import annotations

from bioagent.tools.ird_annotate import (
    annotate_ird_layers,
    dbscsnv_scores,
    hgmd_nearby,
    inclusion_reason,
    interval_overlap,
    is_protein_altering,
    retina_exon_genes,
)

# A dbscSNV row: cols 0-3 = chr/pos/ref/alt, ada = col16, rf = col17 (18 columns).
_DBSCSNV = "\t".join(["1", "860326", "A", "C"] + ["."] * 12 + ["0.80", "0.93"])
# HGMD rows (no 'chr' prefix): CHROM POS REF ALT GENE ISOFORM cDNA DISEASE PHENOTYPE REF PUBMED
_HGMD_EXACT = "1\t874816\tC\tCT\tSAMD11\tNM_152486.2\tc.682insT\tDisease_causing\tRP\tRef2013\t24176758"
_HGMD_NEAR = "1\t874820\tG\tA\tSAMD11\tNM_152486.2\tc.690G>A\tDisease_causing\tRP\tRef2013\t99999999"


def test_interval_overlap_boundaries():
    bed = ["chr1\t136562\t136738"]
    assert interval_overlap(bed, 136738)          # end is inclusive (1-based)
    assert interval_overlap(bed, 136563)
    assert not interval_overlap(bed, 136562)      # start is exclusive (0-based BED)
    assert not interval_overlap(bed, 200000)


def test_retina_exon_genes():
    lines = ["chr1\t569249\t569519\tOR4F16\tOR4F3\tOR4F29"]
    assert retina_exon_genes(lines, 569300) == ["OR4F16", "OR4F3", "OR4F29"]
    assert retina_exon_genes(lines, 1) == []


def test_atac_narrowpeak_overlap_uses_first_three_cols():
    peak = ["chr1\t9999\t10467\tpeak_1\t4025\t.\t17.3\t402.6\t397.7\t58"]
    assert interval_overlap(peak, 10000)
    assert not interval_overlap(peak, 9999)


def test_hgmd_exact_match_is_flagged_and_first():
    ann, is_match = hgmd_nearby([_HGMD_NEAR, _HGMD_EXACT], "chr1", 874816, "C", "CT")
    assert is_match is True
    assert ann.startswith("MATCH-1:874816:C>CT:SAMD11")     # exact match prefixed + placed first
    assert "874820" in ann                                  # the nearby one is still listed


def test_hgmd_no_exact_match():
    ann, is_match = hgmd_nearby([_HGMD_NEAR], "chr1", 874816, "C", "CT")
    assert is_match is False
    assert "MATCH-" not in ann and "874820" in ann


def test_dbscsnv_scores_parse_and_miss():
    assert dbscsnv_scores([_DBSCSNV], "chr1", 860326, "A", "C") == (0.80, 0.93)
    assert dbscsnv_scores([_DBSCSNV], "chr1", 999999, "A", "C") == (None, None)


def test_is_protein_altering():
    assert is_protein_altering("missense_variant")
    assert is_protein_altering("splice_donor_variant")
    assert is_protein_altering("frameshift_variant&splice_region_variant")   # &-joined
    assert not is_protein_altering("synonymous_variant")
    assert not is_protein_altering("intron_variant")
    assert not is_protein_altering("")


def test_inclusion_reason_cascade_order():
    # HGMD exact match wins even when common.
    assert inclusion_reason({"hgmd_match": True, "max_af": 0.9}) == "HGMD_match"
    # too common (>=0.005) and no HGMD ⇒ dropped.
    assert inclusion_reason({"max_af": 0.02, "consequence": "missense_variant"}) == "none"
    # rare + splice score >=0.6 ⇒ splice.
    assert inclusion_reason({"max_af": 1e-4, "ada_score": 0.7,
                             "consequence": "intron_variant"}) == "splice_prediction>0.6"
    # rare + protein-altering ⇒ protein-altering.
    assert inclusion_reason({"max_af": 1e-4, "consequence": "stop_gained"}) == "protein-altering"
    # rare but synonymous / no splice ⇒ none.
    assert inclusion_reason({"max_af": 1e-4, "consequence": "synonymous_variant"}) == "none"
    # novel (missing AF) counts as rare.
    assert inclusion_reason({"max_af": None, "consequence": "missense_variant"}) == "protein-altering"


def _fake_tabix(hits_at=874816):
    """A fake tabix keyed by path substring; returns canned lines for a variant at ``hits_at``."""
    def tabix(path, region):
        p = path.lower()
        if "hgmd" in p:
            return [f"1\t{hits_at}\tC\tCT\tSAMD11\tNM_1\tc.1\tDisease_causing\tRP\tRef\t123"]
        if "retina" in p:
            return [f"chr1\t{hits_at-10}\t{hits_at+10}\tSAMD11"]
        if "atac" in p:
            return [f"chr1\t{hits_at-10}\t{hits_at+10}\tpk\t5\t.\t1\t1\t1\t1"]
        if "dbscsnv" in p:
            return ["\t".join(["1", str(hits_at), "C", "CT"] + ["."] * 12 + ["0.72", "0.40"])]
        return []
    return tabix


def test_annotate_ird_layers_all_fields_and_hgmd_reason():
    rows = [{"location": "chr1:874816", "allele": "C/CT",
             "consequence": "frameshift_variant", "max_af": None}]
    out = annotate_ird_layers(rows, _fake_tabix(), hgmd_path="/x/hgmd.gz", retina_bed="/x/retina",
                              atac_path="/x/atac.gz", dbscsnv_template="/x/db/dbscSNV1.1.{chrom}")
    r = out[0]
    assert r["hgmd_match"] is True and r["reason_for_inclusion"] == "HGMD_match"   # HGMD wins the cascade
    assert r["retina_specific_exon"] == "SAMD11"
    assert r["atac_peak"] is True
    assert r["ada_score"] == 0.72 and r["rf_score"] == 0.40


def test_annotate_ird_layers_splice_reason_without_hgmd():
    # No HGMD layer; a rare intron variant with a high ada score ⇒ splice_prediction reason.
    rows = [{"location": "chr1:874816", "allele": "C/CT",
             "consequence": "intron_variant", "max_af": 1e-4}]
    out = annotate_ird_layers(rows, _fake_tabix(), dbscsnv_template="/x/db/dbscSNV1.1.{chrom}")
    assert out[0]["ada_score"] == 0.72
    assert out[0]["reason_for_inclusion"] == "splice_prediction>0.6"
    assert "hgmd_match" not in out[0]                     # layer skipped (no path)


def test_annotate_ird_layers_skips_empty_paths():
    rows = [{"location": "chr1:874816", "allele": "C/CT",
             "consequence": "missense_variant", "max_af": 1e-4}]
    out = annotate_ird_layers(rows, _fake_tabix())       # no reference paths at all
    assert "retina_specific_exon" not in out[0] and "hgmd_match" not in out[0]
    assert out[0]["reason_for_inclusion"] == "protein-altering"   # still computed from consequence+AF
