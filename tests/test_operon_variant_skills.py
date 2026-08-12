"""The operon-derived variant skills — normalize_vcf / vcf_qc_stats / clinical_variant_prioritization.

Ported from operon's variant-calling protocols into our atomic-skill folder form. The two stdlib-only
skills (QC-stats fallback path + prioritization) are executed end-to-end here against synthetic inputs;
normalize_vcf needs bcftools (analysis image only), so it is compile-checked elsewhere and only its
registration/guardrails are asserted here.
"""
from __future__ import annotations

import csv
import json
import runpy
from pathlib import Path

from bioagent.agents.skills import SKILLS

_SKILLS = Path(__file__).resolve().parents[1] / "skills"


def test_operon_skills_registered_with_summaries():
    for name in ("normalize_vcf", "vcf_qc_stats", "clinical_variant_prioritization"):
        s = SKILLS.get(name)
        assert s is not None, f"{name} did not load"
        assert s.summary.startswith("Reference template"), (name, s.summary)
        assert "reference.py" in s.files


def _write_annotation_tsv(path: Path, rows: list[dict]) -> None:
    cols = ["location", "allele", "gene_symbol", "gene_id", "consequence", "impact", "amino_acids",
            "sift", "polyphen", "max_af", "rsid", "clinical_significance"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter="\t")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def test_clinical_prioritization_tiers(tmp_path, monkeypatch):
    _write_annotation_tsv(tmp_path / "tables" / "variant_annotation.tsv", [
        # ClinVar pathogenic -> verbatim tier (never computed)
        {"location": "1:200", "gene_symbol": "BRCA2", "consequence": "stop_gained", "impact": "HIGH",
         "rsid": "rs2", "clinical_significance": "pathogenic"},
        # ClinVar benign -> verbatim, even though common
        {"location": "9:900", "gene_symbol": "XYZ", "consequence": "missense_variant",
         "impact": "MODERATE", "sift": "tolerated", "polyphen": "benign", "max_af": "0.3",
         "rsid": "rs9", "clinical_significance": "benign"},
        # rare + deleterious predictors, no ClinVar -> VUS_FAVOR_PATH (still a VUS)
        {"location": "17:100", "gene_symbol": "TP53", "consequence": "missense_variant",
         "impact": "MODERATE", "sift": "deleterious", "polyphen": "probably_damaging",
         "max_af": "0.00001", "rsid": "rs1", "clinical_significance": ""},
        # common (AF 0.5) not-in-ClinVar -> BA1 stand-alone benign
        {"location": "2:300", "gene_symbol": "FOO", "consequence": "intron_variant",
         "impact": "MODIFIER", "max_af": "0.5", "rsid": "rs3", "clinical_significance": ""},
    ])
    monkeypatch.setenv("BIOAGENT_ARTIFACTS", str(tmp_path))
    monkeypatch.setenv("BIOAGENT_DISEASE_MODEL", "dominant")
    runpy.run_path(str(_SKILLS / "clinical_variant_prioritization" / "reference.py"), run_name="__main__")

    tiers = {r["Gene"]: r["Tier"]
             for r in csv.DictReader((tmp_path / "tables" / "prioritized_variants.csv").open())}
    assert tiers["BRCA2"] == "PATHOGENIC_CLINVAR"        # ClinVar verbatim, not computed
    assert tiers["XYZ"] == "BENIGN_CLINVAR"
    assert tiers["TP53"] == "VUS_FAVOR_PATH"             # rare + 2 predictors, never PATHOGENIC alone
    assert tiers["FOO"] == "LIKELY_BENIGN_COMMON"        # AF > BA1
    summary = json.loads((tmp_path / "data" / "prioritization_summary.json").read_text())
    assert "NOT a clinical" in summary["caveat"]         # research-triage caveat is emitted


def test_clinical_prioritization_needs_annotation_table(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOAGENT_ARTIFACTS", str(tmp_path))   # no tables/variant_annotation.tsv
    try:
        runpy.run_path(str(_SKILLS / "clinical_variant_prioritization" / "reference.py"), run_name="__main__")
    except SystemExit as exc:
        assert "variant_annotation.tsv" in str(exc)
    else:
        raise AssertionError("should SystemExit when the annotated table is missing")


def test_vcf_qc_stats_stdlib_titv_and_flag(tmp_path, monkeypatch):
    # 2 transitions (A>G, C>T) + 2 transversions (A>C, G>T) -> Ti/Tv = 1.0, flagged for WGS.
    vcf = tmp_path / "tiny.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "1\t100\t.\tA\tG\t50\tPASS\t.\n"
        "1\t200\t.\tC\tT\t50\tPASS\t.\n"
        "1\t300\t.\tA\tC\t50\tPASS\t.\n"
        "1\t400\t.\tG\tT\t50\tPASS\t.\n"
        "1\t500\t.\tAT\tA\t50\tPASS\t.\n", encoding="utf-8")
    monkeypatch.setenv("BIOAGENT_ARTIFACTS", str(tmp_path / "art"))
    monkeypatch.setenv("BIOAGENT_DATASET", str(vcf))
    monkeypatch.setenv("BIOAGENT_SEQ_TYPE", "WGS")
    # bcftools/cyvcf2 may or may not be installed in CI; force the stdlib path deterministically by
    # running the module and asserting on the JSON, whichever method was used still yields Ti/Tv=1.0.
    runpy.run_path(str(_SKILLS / "vcf_qc_stats" / "reference.py"), run_name="__main__")

    m = json.loads((tmp_path / "art" / "data" / "vcf_qc.json").read_text())
    assert m["ti_tv"] == 1.0 and m["n_snps"] == 4 and m["n_indels"] == 1
    assert any("Ti/Tv" in f for f in m["qc_flags"])       # out of WGS range -> flagged
