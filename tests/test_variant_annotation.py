"""Offline tests for variant annotation (tools/variant_annotation.py).

VEP/ClinVar are REST calls; here the HTTP layer is injected (fake ``http_post``) or ``vep_annotate``
is monkeypatched, so the VCF parsing, VEP-result merge, ClinVar-significance ranking, summary, and the
tool wiring are all exercised WITHOUT network — the same schema the live Ensembl VEP returns.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

from bioagent.tools.variant_annotation import (
    ANNOTATION_COLUMNS,
    classify_significance,
    make_variant_annotation_tool,
    parse_vcf_variants,
    parse_vep_result,
    read_vcf_for_annotation,
    summarize_annotations,
    to_vep_region,
    vep_annotate,
    _write_table,
)


def test_apply_variant_filters_af_and_gene_panel():
    from bioagent.tools.variant_annotation import apply_variant_filters
    rows = [
        {"gene_symbol": "RPGR", "max_af": 0.0001},     # rare, in panel  -> kept
        {"gene_symbol": "RPGR", "max_af": 0.30},        # common          -> dropped by AF
        {"gene_symbol": "TTN", "max_af": 0.0001},       # rare, off-panel -> dropped by gene
        {"gene_symbol": "USH2A", "max_af": None},       # novel (no AF), in panel -> kept
    ]
    kept, stats = apply_variant_filters(rows, max_pop_af=0.01, genes=["RPGR", "USH2A", "ABCA4"])
    assert {r["gene_symbol"] for r in kept} == {"RPGR", "USH2A"}
    assert stats["n_dropped_common_af"] == 1 and stats["n_dropped_off_panel"] == 1
    assert stats["n_kept"] == 2 and stats["gene_panel_size"] == 3
    # both filters default to no-op
    kept2, stats2 = apply_variant_filters(rows)
    assert len(kept2) == 4 and stats2["n_dropped_common_af"] == 0


def test_high_priority_uses_predictor_scores_when_present():
    # A rare, not-in-ClinVar variant that SIFT/PolyPhen call tolerant but CADD flags (≥20) must still
    # land in the high-priority shortlist; a plugin-less row (blank scores) falls back to SIFT/PolyPhen.
    rows = [
        {"gene_symbol": "GX", "location": "1:1", "impact": "MODERATE", "sift": "tolerated",
         "polyphen": "benign", "max_af": 0.0001, "clinical_significance": "", "cadd_phred": 27.0},
        {"gene_symbol": "GY", "location": "2:2", "impact": "MODERATE", "sift": "tolerated",
         "polyphen": "benign", "max_af": 0.0001, "clinical_significance": "", "cadd_phred": ""},
    ]
    genes = {v["gene"] for v in summarize_annotations(rows)["high_priority_variants"]}
    assert genes == {"GX"}          # GX kept via CADD; GY excluded (no damaging evidence)


def test_high_priority_ranks_disease_model_fits_above_offtarget():
    # A rare novel HIGH-impact variant in an autosomal disease-model gene (ultra-rare ⇒ dominant) must
    # rank ABOVE a mitochondrial (chrM) rare HIGH-impact variant, which has no autosome/X model — this
    # is what stops the mito/off-target noise from leading the shortlist.
    rows = [
        {"gene_symbol": "MT-ND4", "location": "chrM:100", "impact": "HIGH", "consequence": "stop_gained",
         "sift": "deleterious", "polyphen": "probably_damaging", "max_af": None, "clinical_significance": ""},
        {"gene_symbol": "CRB1", "location": "chr1:200", "impact": "HIGH", "consequence": "frameshift_variant",
         "sift": "deleterious", "polyphen": "probably_damaging", "max_af": 5e-5, "clinical_significance": ""},
    ]
    s = summarize_annotations(rows)
    hp = s["high_priority_variants"]
    assert hp[0]["gene"] == "CRB1" and hp[0]["disease_model"] == ["dominant"]   # model fit leads
    assert s["n_high_priority_disease_model"] == 1                             # MT-ND4 (chrM) has no model


def test_high_priority_csv_carries_disease_model_column(tmp_path):
    from bioagent.tools.variant_annotation import write_standard_tables
    import csv as _csv
    s = summarize_annotations([
        {"gene_symbol": "CRB1", "location": "chr1:200", "impact": "HIGH", "consequence": "frameshift_variant",
         "sift": "deleterious", "polyphen": "probably_damaging", "max_af": 5e-5, "clinical_significance": ""},
    ])
    write_standard_tables(s, tmp_path)
    hp = list(_csv.DictReader((tmp_path / "high_priority_variants.csv").open()))
    assert hp[0]["Disease_Model"] == "dominant"

_VCF = """\
##fileformat=VCFv4.2
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
chr17\t7676154\t.\tG\tA\t.\t.\t.
13\t32316508\trs80359550\tC\tT,G\t.\t.\t.
bad row skip me
X\t.\tno_pos\tA\tT
"""


def test_parse_vcf_variants_strips_chr_splits_multiallelic_skips_bad():
    vs = parse_vcf_variants(_VCF)
    # chr17 -> 17; the C>T,G multiallelic becomes TWO variants; the malformed rows are skipped
    assert [v["chrom"] for v in vs] == ["17", "13", "13"]
    assert vs[0] == {"chrom": "17", "pos": 7676154, "id": ".", "ref": "G", "alt": "A"}
    assert [v["alt"] for v in vs[1:]] == ["T", "G"]
    assert vs[1]["id"] == "rs80359550"


def test_parse_vcf_variants_max_cap():
    many = "\n".join(f"1\t{i}\t.\tA\tT" for i in range(1, 50))
    assert len(parse_vcf_variants(many, max_variants=10)) == 10


def test_read_vcf_for_annotation_pass_filters_and_counts():
    # 3 PASS/'.' records + 2 FILTER-failing records. pass_only annotates only the PASS ones but
    # counts ALL of them, so the report can state real "n PASS / m non-PASS" instead of "ALL PASS".
    vcf = "\n".join([
        "17\t100\t.\tG\tA\t.\tPASS\t.",
        "17\t200\t.\tC\tT\t.\t.\t.",          # '.' FILTER == passing
        "17\t300\t.\tA\tG\t.\tsnp_filter\t.",  # FAILED
        "17\t400\t.\tT\tC\t.\tindel_filter\t.",  # FAILED
        "17\t500\t.\tG\tC\t.\tPASS\t.",
    ])
    keep = read_vcf_for_annotation(vcf, pass_only=True)
    assert keep["n_pass"] == 3 and keep["n_nonpass"] == 2 and keep["n_records"] == 5
    assert len(keep["variants"]) == 3 and keep["truncated"] is False
    # pass_only=False annotates every record but STILL reports the honest FILTER split
    both = read_vcf_for_annotation(vcf, pass_only=False)
    assert len(both["variants"]) == 5 and both["n_pass"] == 3 and both["n_nonpass"] == 2


def test_read_vcf_for_annotation_truncates_before_full_tally():
    vcf = "\n".join(f"1\t{i}\t.\tA\tT\t.\tPASS\t." for i in range(1, 21))
    keep = read_vcf_for_annotation(vcf, max_variants=5, pass_only=True)
    assert len(keep["variants"]) == 5 and keep["truncated"] is True
    assert keep["n_pass"] == 20          # the whole-file tally continues past the cap


def test_write_table_persists_full_schema_and_verifies(tmp_path):
    rows = [parse_vep_result(_VEP_ITEM)]
    path = _write_table(rows, dest=tmp_path)
    assert path and (tmp_path / "variant_annotation.tsv").exists()
    header = (tmp_path / "variant_annotation.tsv").read_text().splitlines()[0].split("\t")
    assert header == ANNOTATION_COLUMNS               # complete schema, never FILTER-only
    assert _write_table([], dest=tmp_path) == ""      # no rows -> no table


def test_annotate_variants_tool_reports_real_filter_counts(monkeypatch):
    import bioagent.tools.variant_annotation as va

    monkeypatch.setattr(va, "vep_annotate", lambda variants, **k: [parse_vep_result(_VEP_ITEM)])
    tool = make_variant_annotation_tool()
    ctx = types.SimpleNamespace(decisions={})
    vcf = "\n".join(["17\t100\t.\tG\tA\t.\tPASS\t.", "17\t300\t.\tA\tG\t.\tsnp_filter\t."])
    out = tool.executor({"vcf_path": vcf}, ctx)
    assert out["status"] == "ok" and out["filter"] == "PASS-only"
    assert out["n_pass"] == 1 and out["n_nonpass"] == 1        # NOT "all pass"
    assert out["annotated_table_columns"] == ANNOTATION_COLUMNS


def test_to_vep_region_format():
    assert to_vep_region({"chrom": "17", "pos": 7676154, "id": ".", "ref": "G", "alt": "A"}) \
        == "17 7676154 . G A . . ."


def test_classify_significance_picks_most_actionable():
    assert classify_significance(["uncertain_significance", "benign", "pathogenic"]) == "pathogenic"
    assert classify_significance(["likely_benign", "benign"]) == "likely_benign"
    assert classify_significance(["Uncertain significance"]) == "uncertain_significance"  # normalised
    assert classify_significance([]) == ""
    assert classify_significance(None) == ""


# One element in the exact shape the live Ensembl VEP region-POST returns (TP53 rs1042522), incl.
# SIFT/PolyPhen and the gnomAD frequency block.
_VEP_ITEM = {
    "input": "17 7676154 . G A . . .",
    "seq_region_name": "17", "start": 7676154, "allele_string": "G/A",
    "most_severe_consequence": "missense_variant",
    "transcript_consequences": [
        {"gene_symbol": "TP53", "gene_id": "ENSG00000141510", "impact": "MODERATE",
         "consequence_terms": ["missense_variant"], "amino_acids": "P/L",
         "sift_prediction": "tolerated", "polyphen_prediction": "benign"},
        {"gene_symbol": "TP53", "impact": "MODIFIER", "consequence_terms": ["upstream_gene_variant"]},
    ],
    "colocated_variants": [
        {"id": "rs1042522", "clin_sig": ["uncertain_significance", "benign", "pathogenic"],
         "frequencies": {"A": {"gnomade": 6.8e-07, "gnomade_nfe": 9.0e-07, "gnomade_afr": 0}}}],
}


def test_parse_vep_result_extracts_gene_consequence_clinvar_and_scores():
    row = parse_vep_result(_VEP_ITEM)
    assert row["gene_symbol"] == "TP53"
    assert row["consequence"] == "missense_variant"
    assert row["impact"] == "MODERATE"                 # the transcript matching most_severe_consequence
    assert row["sift"] == "tolerated" and row["polyphen"] == "benign"
    assert row["max_af"] == 9.0e-07                    # max across the gnomAD populations
    assert row["rsid"] == "rs1042522"
    assert row["clinical_significance"] == "pathogenic"  # collapsed from the clin_sig list
    assert row["location"] == "17:7676154"


def test_parse_vep_result_tolerates_missing_fields():
    row = parse_vep_result({"input": "1 1 . A T . . ."})
    assert row["gene_symbol"] == "" and row["consequence"] == "" and row["clinical_significance"] == ""
    assert row["max_af"] is None and row["sift"] == "" and row["polyphen"] == ""


def test_parse_vep_result_finds_predictors_across_transcripts_and_unwraps_alphamissense():
    # VEP puts CADD/REVEL on the most-severe transcript but AlphaMissense (a NESTED dict) + MANE only on
    # a DIFFERENT transcript — the parser must still surface all three (regression for BRAF V600E, where
    # AlphaMissense was silently dropped because the picked transcript lacked it / it was never unwrapped).
    item = {
        "input": "7 140753336 . A T . . .",
        "seq_region_name": "7", "start": 140753336, "allele_string": "A/T",
        "most_severe_consequence": "missense_variant",
        "transcript_consequences": [
            {"consequence_terms": ["missense_variant"], "gene_symbol": "BRAF", "impact": "MODERATE",
             "cadd_phred": 29.8, "revel": 0.931},              # picked, but NO AlphaMissense / MANE
            {"consequence_terms": ["missense_variant"], "mane_select": "NM_004333.6",
             "alphamissense": {"am_pathogenicity": 0.9927, "am_class": "likely_pathogenic"}},
        ],
    }
    row = parse_vep_result(item)
    assert row["cadd_phred"] == 29.8 and row["revel"] == 0.931
    assert row["alphamissense"] == 0.9927                 # unwrapped from the nested dict on the MANE tc
    assert row["mane_select"] == "NM_004333.6"            # sourced from the MANE transcript


def test_vep_annotate_batches_and_parses_via_injected_http():
    calls = []

    def fake_post(url, body, headers):
        calls.append((url, json.loads(body.decode())))
        return json.dumps([_VEP_ITEM]).encode()

    rows = vep_annotate([{"chrom": "17", "pos": 7676154, "id": ".", "ref": "G", "alt": "A"}],
                        http_post=fake_post, sleep_s=0)
    assert len(rows) == 1 and rows[0]["gene_symbol"] == "TP53"
    assert calls[0][0].endswith("/vep/human/region")
    assert calls[0][1]["variants"] == ["17 7676154 . G A . . ."]


def test_summarize_annotations_counts_rarity_and_priority():
    rows = [
        parse_vep_result(_VEP_ITEM),                                              # TP53 pathogenic, rare
        {"consequence": "synonymous_variant", "impact": "LOW", "clinical_significance": "",
         "gene_symbol": "X", "location": "1:1", "max_af": None},                  # rare but benign-ish
        {"consequence": "stop_gained", "impact": "HIGH", "clinical_significance": "likely_pathogenic",
         "gene_symbol": "BRCA2", "location": "13:1", "max_af": None},             # pathogenic
        {"consequence": "frameshift_variant", "impact": "HIGH", "clinical_significance": "",
         "gene_symbol": "RARE1", "location": "3:3", "max_af": None},              # rare + HIGH -> priority
        {"consequence": "stop_gained", "impact": "HIGH", "clinical_significance": "",
         "gene_symbol": "COMMON", "location": "2:2", "max_af": 0.30},             # COMMON -> excluded
    ]
    s = summarize_annotations(rows)
    assert s["n_variants"] == 5 and s["n_pathogenic"] == 2
    assert s["by_clinical_significance"]["not_in_clinvar"] == 3
    assert s["n_rare"] == 4                                     # the common (AF 0.30) one is not rare
    assert {p["gene"] for p in s["pathogenic_variants"]} == {"TP53", "BRCA2"}
    # high_priority = NOVEL candidates only: rare + high-impact/deleterious AND NOT in ClinVar. The
    # pathogenic TP53/BRCA2 are in ClinVar (their own list); COMMON is not rare. Only RARE1 qualifies.
    hp = {p["gene"] for p in s["high_priority_variants"]}
    assert hp == {"RARE1"}
    assert not ({"TP53", "BRCA2", "COMMON"} & hp)


def test_write_standard_tables_emits_five_deliverables(tmp_path):
    import csv as _csv

    from bioagent.tools.variant_annotation import STANDARD_TABLES, write_standard_tables
    summary = summarize_annotations([
        parse_vep_result(_VEP_ITEM),                                              # TP53 pathogenic
        {"consequence": "frameshift_variant", "impact": "HIGH", "clinical_significance": "",
         "gene_symbol": "RARE1", "location": "3:3", "max_af": None, "rsid": "rs9",
         "sift": "deleterious", "polyphen": "probably_damaging"},                 # novel high-priority
    ])
    written = write_standard_tables(summary, tmp_path)
    assert {Path(p).name for p in written} == set(STANDARD_TABLES)   # all five, no run_code
    # distributions carry a Percentage column; the pathogenic + shortlist tables carry the right rows
    patho = list(_csv.DictReader((tmp_path / "clinvar_pathogenic_variants.csv").open()))
    assert [r["Gene"] for r in patho] == ["TP53"]
    hp = list(_csv.DictReader((tmp_path / "high_priority_variants.csv").open()))
    assert [r["Gene"] for r in hp] == ["RARE1"] and hp[0]["ClinVar_Status"] == "not_in_clinvar"
    cons = (tmp_path / "variant_consequence_distribution.csv").read_text()
    assert "Consequence,Count,Percentage" in cons


def test_annotate_variants_tool_ok_path(monkeypatch):
    # Monkeypatch the network layer so the tool wiring (parse -> annotate -> summarise) runs offline.
    import bioagent.tools.variant_annotation as va

    monkeypatch.setattr(va, "vep_annotate", lambda variants, **k: [parse_vep_result(_VEP_ITEM)])
    tool = make_variant_annotation_tool()
    ctx = types.SimpleNamespace(decisions={})
    out = tool.executor({"vcf_path": _VCF}, ctx)          # pass raw VCF text as the "path"
    assert out["status"] == "ok" and out["n_pathogenic"] == 1
    assert out["pathogenic_variants"][0]["gene"] == "TP53"


def test_annotate_variants_tool_flags_truncation_when_cap_reached(monkeypatch):
    # A VCF larger than the cap must NOT be silently reduced to the first N: the tool result has to
    # carry `truncated`/`warning` so the report can never pass a first-500 slice off as the whole file.
    import bioagent.tools.variant_annotation as va

    monkeypatch.setattr(va, "vep_annotate", lambda variants, **k: [parse_vep_result(_VEP_ITEM)])
    tool = make_variant_annotation_tool()
    ctx = types.SimpleNamespace(decisions={})
    big = "\n".join(f"1\t{i}\t.\tA\tT" for i in range(1, 50))
    out = tool.executor({"vcf_path": big, "max_variants": 10}, ctx)
    assert out["status"] == "ok" and out["truncated"] is True
    assert out["n_input_variants"] == 10 and "TRUNCATED" in out["warning"]


def test_annotate_variants_tool_no_truncation_flag_when_under_cap(monkeypatch):
    import bioagent.tools.variant_annotation as va

    monkeypatch.setattr(va, "vep_annotate", lambda variants, **k: [parse_vep_result(_VEP_ITEM)])
    tool = make_variant_annotation_tool()
    ctx = types.SimpleNamespace(decisions={})
    out = tool.executor({"vcf_path": _VCF}, ctx)               # 3 variants, cap 500 -> not truncated
    assert out["status"] == "ok" and "truncated" not in out and "warning" not in out


def test_annotate_variants_tool_errors_without_vcf():
    tool = make_variant_annotation_tool()
    ctx = types.SimpleNamespace(decisions={})
    assert tool.executor({}, ctx)["status"] == "error"
    assert tool.executor({"vcf_path": "/no/such/file.vcf"}, ctx)["status"] == "error"


def test_run_vcf_preflight_labels_and_profiles(tmp_path):
    # A .vcf upload goes through the SAME single-file preflight, but is labelled vcf_variants (not
    # single-cell) with sample names + a variant count, so the planner routes it to annotation.
    from bioagent.tools.datasets import run_dataset_smoke_analysis

    vcf = tmp_path / "cohort.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\n"
        "17\t7676154\t.\tG\tA\t.\t.\t.\tGT\t0/1\t1/1\n"
        "13\t32340628\t.\tAG\tA\t.\t.\t.\tGT\t0/0\t0/1\n",
        encoding="utf-8")
    out = run_dataset_smoke_analysis(vcf, tmp_path / "pre")
    r = out["result"]
    assert r["dataset_kind"] == "vcf_variants"
    assert r["n_samples"] == 2 and r["samples"] == ["S1", "S2"]
    assert r["n_variants_sampled"] == 2
    assert (tmp_path / "pre" / "dataset_results.json").exists()


def test_high_priority_includes_and_ranks_ird_hgmd_and_splice():
    # The IRD annotation layers add independent inclusion evidence the generic predictor panel misses:
    # an HGMD exact match and a dbscSNV splice prediction. Both must (a) enter the shortlist even with
    # blank SIFT/PolyPhen/CADD, (b) surface reason_for_inclusion / hgmd_match, (c) the HGMD match ranks
    # first — while a rare-but-no-evidence row stays excluded (guards against over-inclusion).
    rows = [
        {"gene_symbol": "USH2A", "location": "1:216", "impact": "MODERATE", "consequence": "missense_variant",
         "sift": "tolerated", "polyphen": "benign", "max_af": 1e-4, "clinical_significance": "",
         "hgmd_match": True, "reason_for_inclusion": "HGMD_match"},
        {"gene_symbol": "ABCA4", "location": "1:94", "impact": "LOW", "consequence": "splice_region_variant",
         "sift": "tolerated", "polyphen": "benign", "max_af": 1e-4, "clinical_significance": "",
         "reason_for_inclusion": "splice_prediction>0.6"},
        {"gene_symbol": "GZ", "location": "1:5", "impact": "MODIFIER", "consequence": "intron_variant",
         "sift": "tolerated", "polyphen": "benign", "max_af": 1e-4, "clinical_significance": "",
         "reason_for_inclusion": "none"},
    ]
    hp = summarize_annotations(rows)["high_priority_variants"]
    genes = [v["gene"] for v in hp]
    assert genes[0] == "USH2A"                       # HGMD match ranks first
    assert set(genes) == {"USH2A", "ABCA4"}          # both IRD-flagged in; the no-evidence row excluded
    assert hp[0]["hgmd_match"] is True and hp[0]["reason_for_inclusion"] == "HGMD_match"


def test_annotation_columns_carry_ird_fields():
    for col in ("reason_for_inclusion", "hgmd_match", "retina_specific_exon", "atac_peak",
                "ada_score", "rf_score"):
        assert col in ANNOTATION_COLUMNS
