---
name: vcf_qc_stats
description: Reference template — compute VCF callset QC metrics (Ti/Tv, Het/Hom, SNP/indel/multiallelic counts, call rate) and flag values outside expected WGS/WES ranges.
---

## When to use

Run this on a VCF **before** interpreting individual variants, to judge whether the callset itself is
trustworthy. `annotate_variants` reports the PASS/non-PASS split but nothing about callset quality —
a low Ti/Tv or an off-range Het/Hom means the consequence/pathogenicity tables downstream describe a
noisy callset (excess false positives, contamination, or a sample swap), and the report should say so
(route the caveat to Diagnostics, not the manuscript's conclusions).

## Details & adaptation

Ported from operon's `variant-calling-vcf-statistics` protocol. Metrics + operon's expected ranges:

| Metric | WGS | WES | Flag if |
|---|---|---|---|
| Ti/Tv | ~2.0–2.1 | ~2.8–3.3 | WGS <1.8 / >2.5; WES <2.5 / >3.5 |
| Het/Hom | 1.5–2.0 | 1.5–2.0 | cohort outlier |
| Call rate | >95% | >95% | <90% |

An off-range Ti/Tv is the strongest single quick signal of a false-positive-heavy callset.

**ADAPT:** `INPUT_VCF` and `SEQ_TYPE` (`"WGS"` or `"WES"` — sets the Ti/Tv band). The template degrades
gracefully: it prefers `bcftools stats` (full metrics incl. Het/Hom + call rate from per-sample
counts), falls back to `cyvcf2`, and finally to a pure-stdlib Ti/Tv-only scan if neither is installed
(genotype metrics need bcftools or cyvcf2). Writes `tables/vcf_qc_summary.csv` + `data/vcf_qc.json`
with a `qc_flags` list.

## Run
Fetch with `read_skill_reference("vcf_qc_stats", file="reference.py")`, set `INPUT_VCF` / `SEQ_TYPE`,
run via `run_code`. Report Ti/Tv and any `qc_flags`; if flagged, frame the downstream variant tables
as describing a lower-confidence callset.
