"""The variant_output_tables.py atomic skill — the correct, tested replacement for the CSV-dumping /
summary-dict run_code the orchestrator kept hand-writing (and botching) for a VCF run.

Unlike the scanpy templates (import-heavy → compile-only in CI), this skill is STDLIB-ONLY, so it is
executed end-to-end here against a well-formed annotated TSV — proving it emits the five standard
deliverables + summary with correct counts, no re-parsing of the VCF.
"""
from __future__ import annotations

import csv
import json
import runpy
from pathlib import Path

from bioagent.agents.skills import SKILLS
from bioagent.tools.variant_annotation import ANNOTATION_COLUMNS

_SKILL = Path(__file__).resolve().parents[1] / "skills" / "variant_output_tables" / "reference.py"


def _write_tsv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ANNOTATION_COLUMNS, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in ANNOTATION_COLUMNS})


def test_skill_is_registered_with_summary():
    s = SKILLS.get("variant_output_tables")
    assert s is not None and s.summary.startswith("Reference template")


def test_skill_writes_all_five_tables_and_summary(tmp_path, monkeypatch):
    rows = [
        # rare + deleterious + not-in-ClinVar -> high-priority
        {"location": "17:100", "gene_symbol": "TP53", "consequence": "missense_variant",
         "impact": "MODERATE", "sift": "deleterious", "polyphen": "probably_damaging",
         "max_af": "0.0001", "rsid": "rs1", "clinical_significance": ""},
        # ClinVar pathogenic -> pathogenic table, excluded from shortlist
        {"location": "1:200", "gene_symbol": "BRCA2", "consequence": "stop_gained", "impact": "HIGH",
         "max_af": "", "rsid": "rs2", "clinical_significance": "pathogenic"},
        # common (AF 0.5) -> not rare, excluded from shortlist
        {"location": "2:300", "gene_symbol": "FOO", "consequence": "intron_variant",
         "impact": "MODIFIER", "max_af": "0.5", "rsid": "rs3", "clinical_significance": ""},
        # HIGH impact + no AF (novel -> rare) + not-in-ClinVar -> high-priority
        {"location": "3:400", "gene_symbol": "BAR", "consequence": "frameshift_variant",
         "impact": "HIGH", "max_af": "", "rsid": "rs4", "clinical_significance": ""},
    ]
    _write_tsv(tmp_path / "tables" / "variant_annotation.tsv", rows)
    monkeypatch.setenv("BIOAGENT_ARTIFACTS", str(tmp_path))

    runpy.run_path(str(_SKILL), run_name="__main__")   # executes the template (stdlib-only)

    tdir = tmp_path / "tables"
    for name in ("variant_consequence_distribution.csv", "variant_impact_distribution.csv",
                 "variant_clinical_significance.csv", "clinvar_pathogenic_variants.csv",
                 "high_priority_variants.csv"):
        assert (tdir / name).exists(), f"{name} not written"

    summary = json.loads((tmp_path / "data" / "annotated_results_summary.json").read_text())
    assert summary["total_variants"] == 4
    assert summary["n_pathogenic_clinvar"] == 1                    # BRCA2 only
    assert summary["n_high_priority_rare_deleterious"] == 2         # TP53 + BAR (FOO common, BRCA2 in ClinVar)
    assert summary["clinical_significance_distribution"] == {"not_in_clinvar": 3, "pathogenic": 1}

    hp = list(csv.DictReader((tdir / "high_priority_variants.csv").open()))
    assert {r["Gene"] for r in hp} == {"TP53", "BAR"}
    assert all(r["ClinVar_Status"] == "not_in_clinvar" for r in hp)

    patho = list(csv.DictReader((tdir / "clinvar_pathogenic_variants.csv").open()))
    assert [r["Gene"] for r in patho] == ["BRCA2"]


def test_skill_errors_clearly_without_annotated_table(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOAGENT_ARTIFACTS", str(tmp_path))       # no tables/variant_annotation.tsv
    try:
        runpy.run_path(str(_SKILL), run_name="__main__")
    except SystemExit as exc:
        assert "variant_annotation.tsv" in str(exc)
    else:
        raise AssertionError("skill should SystemExit when the annotated table is missing")
