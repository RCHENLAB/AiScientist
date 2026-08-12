"""Reference template — research-triage priority tier per variant (ClinVar + gnomAD AF + predictors).

Refines the `high_priority_variants` shortlist into ordered tiers so a report can lead with the most
likely-actionable variants. Ported from operon's variant-calling-clinical-interpretation
`classify_variant()` heuristic (disease-model AF thresholds + ClinVar + in-silico predictors),
adapted to our annotate_variants table.

⚠️ This is a **research-triage prioritizer, NOT a clinical ACMG/AMP diagnosis**. Computational
predictions are SUPPORTING evidence only (ACMG PP3/BP4); a real classification needs segregation,
phenotype, and expert review. ClinVar significance is used VERBATIM — never upgraded.

ADAPT: DISEASE_MODEL and the thresholds if the study needs them. Reads tables/variant_annotation.tsv
(written by annotate_variants); pure stdlib. Uses REVEL/CADD/AlphaMissense columns too IF present.
"""
import csv
import json
import os
from collections import Counter
from pathlib import Path

ART = Path(os.environ.get("BIOAGENT_ARTIFACTS", "."))
TABLES = ART / "tables"
(ART / "data").mkdir(parents=True, exist_ok=True)

SRC = TABLES / "variant_annotation.tsv"
if not SRC.exists():
    raise SystemExit(f"{SRC} not found — run annotate_variants first (it persists this table).")
with SRC.open(encoding="utf-8") as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
if not rows:
    raise SystemExit(f"{SRC} has no annotated variants.")

# ADAPT: inheritance model sets the AF ceiling for "rare enough to be causal" (operon thresholds).
DISEASE_MODEL = os.environ.get("BIOAGENT_DISEASE_MODEL", "dominant")   # "dominant" | "recessive"
RARE_AF = {"dominant": 1e-4, "recessive": 1e-2}.get(DISEASE_MODEL, 1e-4)
BA1_AF = 0.05                       # too common to be a rare-disease cause (ACMG BA1 stand-alone benign)
LOF_CONSEQUENCES = ("frameshift", "stop_gained", "splice_donor", "splice_acceptor", "start_lost")
# In-silico predictor cutoffs (operon): predictions are PP3/BP4 SUPPORTING only.
REVEL_PATH, CADD_PATH, ALPHAMISSENSE_PATH = 0.5, 20.0, 0.564


def _af(row: dict) -> float:
    try:
        return float(row.get("max_af", "") or 0.0)
    except ValueError:
        return 0.0


def _num(row: dict, key: str):
    try:
        return float(row.get(key, "") or "")
    except (TypeError, ValueError):
        return None


def _predictor_support(row: dict) -> list[str]:
    """The SUPPORTING computational hits for this variant (never sufficient alone)."""
    hits = []
    if str(row.get("sift", "")).startswith("deleterious"):
        hits.append("SIFT_deleterious")
    if str(row.get("polyphen", "")) in ("probably_damaging", "possibly_damaging"):
        hits.append("PolyPhen_damaging")
    revel, cadd, am = _num(row, "revel"), _num(row, "cadd_phred"), _num(row, "alphamissense")
    if revel is not None and revel > REVEL_PATH: hits.append(f"REVEL>{REVEL_PATH}")
    if cadd is not None and cadd >= CADD_PATH: hits.append(f"CADD>={CADD_PATH}")
    if am is not None and am > ALPHAMISSENSE_PATH: hits.append("AlphaMissense_pathogenic")
    return hits


def _is_lof(row: dict) -> bool:
    cons = str(row.get("consequence", ""))
    return str(row.get("impact", "")) == "HIGH" or any(k in cons for k in LOF_CONSEQUENCES)


def _tier(row: dict) -> tuple[str, str]:
    """(tier, evidence) — ClinVar verbatim first; else a computed VUS band. Never upgrades ClinVar,
    never emits PATHOGENIC from computation alone."""
    clnsig = str(row.get("clinical_significance", "")).lower()
    if "pathogenic" in clnsig and "conflicting" not in clnsig:
        tier = "PATHOGENIC_CLINVAR" if clnsig.strip() == "pathogenic" else "LIKELY_PATHOGENIC_CLINVAR"
        return tier, f"ClinVar={clnsig}"
    if "benign" in clnsig and "conflicting" not in clnsig:
        return "BENIGN_CLINVAR", f"ClinVar={clnsig}"

    af = _af(row)
    if af > BA1_AF:
        return "LIKELY_BENIGN_COMMON", f"gnomAD AF {af:.3g} > BA1 {BA1_AF} (too common for a rare disease)"

    support = _predictor_support(row)
    lof = _is_lof(row)
    rare = af < RARE_AF
    ev = f"AF={af:.3g} ({'rare' if rare else 'low-freq'}, {DISEASE_MODEL} cutoff {RARE_AF:g})"
    if support:
        ev += "; " + "+".join(support)
    if lof:
        ev += "; LoF/HIGH-impact"
    if rare and (lof or len(support) >= 2):
        return "VUS_FAVOR_PATH", ev            # rare + LoF or ≥2 predictors — prioritize, still a VUS
    if rare and len(support) == 1:
        return "VUS_ELEVATED", ev
    if rare:
        return "VUS", ev
    return "VUS_LIKELY_BENIGN", ev             # between the disease-model cutoff and BA1


_ORDER = ["PATHOGENIC_CLINVAR", "LIKELY_PATHOGENIC_CLINVAR", "VUS_FAVOR_PATH", "VUS_ELEVATED",
          "VUS", "VUS_LIKELY_BENIGN", "LIKELY_BENIGN_COMMON", "BENIGN_CLINVAR"]
_rank = {t: i for i, t in enumerate(_ORDER)}

out_rows = []
for r in rows:
    tier, evidence = _tier(r)
    out_rows.append({
        "Gene": r.get("gene_symbol", ""), "Location": r.get("location", ""),
        "rsID": r.get("rsid", ""), "Consequence": r.get("consequence", ""),
        "Impact": r.get("impact", ""), "max_AF": r.get("max_af", ""),
        "ClinVar": r.get("clinical_significance", ""), "Tier": tier, "Evidence": evidence,
    })
out_rows.sort(key=lambda d: _rank.get(d["Tier"], len(_ORDER)))

with (TABLES / "prioritized_variants.csv").open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=["Gene", "Location", "rsID", "Consequence", "Impact",
                                       "max_AF", "ClinVar", "Tier", "Evidence"])
    w.writeheader()
    w.writerows(out_rows)

counts = Counter(d["Tier"] for d in out_rows)
summary = {
    "disease_model": DISEASE_MODEL, "rare_af_cutoff": RARE_AF, "ba1_af": BA1_AF,
    "total_variants": len(out_rows),
    "tier_counts": {t: counts.get(t, 0) for t in _ORDER if counts.get(t, 0)},
    "caveat": "Research triage only — NOT a clinical ACMG/AMP diagnosis. Computational predictors are "
              "PP3/BP4 supporting evidence; ClinVar significance is reported verbatim, never upgraded.",
}
(ART / "data" / "prioritization_summary.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
