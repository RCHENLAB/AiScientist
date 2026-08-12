---
name: clinical_variant_prioritization
description: Reference template — assign a research-triage priority tier (pathogenic → VUS → benign) per variant from ClinVar + gnomAD AF (disease-model thresholds) + in-silico predictors. NOT a clinical ACMG diagnosis.
---

## When to use

After `annotate_variants`, when the study wants the actionable variants **ordered by priority** rather
than a flat `high_priority_variants` shortlist — e.g. "which of these 200 rare variants should a
clinician look at first". It turns the annotation table into ranked tiers (PATHOGENIC_CLINVAR →
LIKELY_PATHOGENIC → VUS_FAVOR_PATH → VUS → LIKELY_BENIGN → BENIGN_CLINVAR).

Do **not** use it to make a clinical call — it is a triage helper, not a diagnosis (see the caveat).

## Details & adaptation

Ported from operon's `variant-calling-clinical-interpretation` `classify_variant()` heuristic. The
tiering combines three sources, in this precedence:

1. **ClinVar verbatim** — a pathogenic/benign ClinVar record sets the tier directly; `uncertain` /
   `conflicting` are NOT upgraded (they fall through to the computed band).
2. **gnomAD allele frequency vs a disease model** — operon thresholds: dominant AF < 1e-4, recessive
   AF < 1e-2; AF > 0.05 is ACMG **BA1** stand-alone benign ("too common to be a rare-disease cause").
   Prefer gnomAD FAF over raw AF when available.
3. **In-silico predictors as SUPPORTING evidence only** (ACMG PP3/BP4): SIFT, PolyPhen, and — when
   the annotation table carries them (see the predictor-enrichment roadmap) — REVEL > 0.5,
   CADD ≥ 20, AlphaMissense > 0.564. A rare variant that is LoF/HIGH-impact **or** has ≥2 predictor
   hits becomes `VUS_FAVOR_PATH` — still a VUS, never PATHOGENIC from computation alone.

**ADAPT:** `DISEASE_MODEL` (`dominant`/`recessive`) and the cutoffs. Pure stdlib; reads
`tables/variant_annotation.tsv`, writes `tables/prioritized_variants.csv` (sorted by tier) +
`data/prioritization_summary.json` (tier counts + the caveat).

⚠️ **Grounding:** research triage only, NOT a clinical ACMG/AMP diagnosis. ClinVar is verbatim and
never upgraded; computational scores are supporting evidence; every prioritized variant needs
orthogonal validation (segregation, phenotype, expert review) before any clinical use.

## Run
Fetch with `read_skill_reference("clinical_variant_prioritization", file="reference.py")`, set
`DISEASE_MODEL`, run via `run_code`. Report the tier counts and lead the actionable section with the
top tiers, always carrying the research-triage caveat.
