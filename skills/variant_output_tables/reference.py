"""Reference template — write the standard variant-annotation result tables from the annotated TSV.

Turns the per-variant table `annotate_variants` already persisted
(`tables/variant_annotation.tsv`, columns: location, allele, gene_symbol, gene_id, consequence,
impact, amino_acids, sift, polyphen, max_af, rsid, clinical_significance) into the five standard
deliverables + a summary JSON — WITHOUT re-parsing the VCF or re-running VEP (annotate_variants did
that). Pure stdlib (csv / json / collections), so there are NO pandas dtype pitfalls; this is a
tested template to adapt-and-run via run_code, NOT code to rewrite from scratch. ADAPT only the
thresholds (RARE_AF, HIGH_IMPACT, damaging predictors) if the study needs different cutoffs.
"""
import csv
import json
import os
from collections import Counter
from pathlib import Path

ART = Path(os.environ.get("BIOAGENT_ARTIFACTS", "."))
TABLES = ART / "tables"
DATA = ART / "data"
TABLES.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)

# annotate_variants already wrote this COMPLETE per-variant table — read it, do NOT re-parse the VCF.
SRC = TABLES / "variant_annotation.tsv"
if not SRC.exists():
    raise SystemExit(f"{SRC} not found — run annotate_variants first (it persists this table).")
with SRC.open(encoding="utf-8") as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
n = len(rows)
if n == 0:
    raise SystemExit(f"{SRC} has no annotated variants.")

# ADAPT these only if the study needs different cutoffs.
RARE_AF = 0.001                                              # gnomAD AF < 0.1%
HIGH_IMPACT = {"HIGH"}
DAMAGING_POLYPHEN = {"probably_damaging", "possibly_damaging"}


def _is_rare(af: str) -> bool:
    """No population-frequency record OR AF below the cutoff ⇒ rare/novel (likelier deleterious)."""
    try:
        return float(af) < RARE_AF
    except (TypeError, ValueError):
        return True


def _is_damaging(row: dict) -> bool:
    return (row.get("impact", "") in HIGH_IMPACT
            or str(row.get("sift", "")).startswith("deleterious")
            or str(row.get("polyphen", "")) in DAMAGING_POLYPHEN)


def _write_distribution(column: str, path: Path, label: str, empty_as: str) -> dict:
    """Count a column's values (blank ⇒ ``empty_as``) and write a Count/Percentage CSV."""
    counts = Counter((r.get(column) or "").strip() or empty_as for r in rows)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([label, "Count", "Percentage"])
        for value, count in counts.most_common():
            w.writerow([value, count, f"{100 * count / n:.2f}%"])
    return dict(counts.most_common())


by_consequence = _write_distribution(
    "consequence", TABLES / "variant_consequence_distribution.csv", "Consequence", "unknown")
by_impact = _write_distribution(
    "impact", TABLES / "variant_impact_distribution.csv", "Impact", "unknown")
by_clinical = _write_distribution(
    "clinical_significance", TABLES / "variant_clinical_significance.csv",
    "Clinical Significance", "not_in_clinvar")

# ClinVar Pathogenic / Likely-Pathogenic — verbatim significance, never upgraded.
pathogenic = [r for r in rows
              if (r.get("clinical_significance") or "").lower() in ("pathogenic", "likely_pathogenic")]
with (TABLES / "clinvar_pathogenic_variants.csv").open("w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["Gene", "Position", "rsID", "Disease"])
    for r in pathogenic:
        w.writerow([r.get("gene_symbol", ""), r.get("location", ""), r.get("rsid", ""),
                    r.get("clinical_significance", "")])

# High-priority shortlist (the study question): RARE + high-impact/deleterious + NOT in ClinVar.
# (The tool's own `high_priority_variants` is the broader pathogenic-inclusive set; this file is the
# unclassified-candidate shortlist.)
high_priority = [r for r in rows
                 if not (r.get("clinical_significance") or "").strip()
                 and _is_rare(r.get("max_af", ""))
                 and _is_damaging(r)]
with (TABLES / "high_priority_variants.csv").open("w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["Gene", "Position", "rsID", "Consequence", "Impact",
                "gnomAD_AF", "SIFT", "PolyPhen", "ClinVar_Status"])
    for r in high_priority:
        w.writerow([r.get("gene_symbol", ""), r.get("location", ""), r.get("rsid", ""),
                    r.get("consequence", ""), r.get("impact", ""), r.get("max_af", ""),
                    r.get("sift", ""), r.get("polyphen", ""), "not_in_clinvar"])

summary = {
    "total_variants": n,
    "consequence_distribution": by_consequence,
    "impact_distribution": by_impact,
    "clinical_significance_distribution": by_clinical,
    "n_pathogenic_clinvar": len(pathogenic),
    "n_rare_variants_af_lt_0.1": sum(1 for r in rows if _is_rare(r.get("max_af", ""))),
    "n_high_priority_rare_deleterious": len(high_priority),
    "tables_produced": [
        "variant_consequence_distribution.csv", "variant_impact_distribution.csv",
        "variant_clinical_significance.csv", "clinvar_pathogenic_variants.csv",
        "high_priority_variants.csv"],
}
(DATA / "annotated_results_summary.json").write_text(
    json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps(summary, indent=2))
