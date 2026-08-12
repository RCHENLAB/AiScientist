---
name: normalize_vcf
description: Reference template — normalize a VCF (atomize MNPs, split multiallelic, left-align indels) with bcftools norm before annotation or ClinVar/gnomAD matching.
---

## When to use

dbSNP, ClinVar and gnomAD store variants in canonical **left-aligned, parsimonious** representation —
a non-normalized indel or a multiallelic site will **fail to match its database record**, so a
genuinely pathogenic variant is silently reported as `not_in_clinvar`. Our **REST** annotation path
only comma-splits ALT alleles and does **not** left-align — so for an indel-heavy VCF annotated over
REST, running this step first is a real correctness fix.

**Under the OFFLINE VEP path (`BIOAGENT_VARIANT_ON_HPC=1`), do NOT plan a separate normalize step —
it is redundant AND cannot run there.** `annotate_variants` already runs `bcftools norm -m-any -f REF`
internally (inside `vep.sif`, which ships bcftools) before VEP — it splits multiallelics and
left-aligns against the reference, which is exactly what ClinVar/gnomAD matching needs (the only thing
its internal norm skips is `--atomize` MNP-splitting, a minor gap for SNV/indel DB matching). And a
standalone `run_code` normalize could not run anyway: `run_code` executes in `analysis.sif`, which has
**no bcftools** and does not bind the reference FASTA, so this skill's own guards exit immediately.
bcftools + the reference only coexist on the offline annotate path — so rely on `annotate_variants`
for normalization there, never a separate CodeAct step.

Also skip it if the VCF is already normalized (e.g. a GATK/DRAGEN single-caller output you know is
left-aligned) — normalizing twice is harmless but wasted time.

## Details & adaptation

Ported from operon's `variant-calling-variant-normalization` protocol. The pipeline order matters:

1. `bcftools norm --atomize` — split MNPs into individual SNPs.
2. `bcftools norm -m-any` — split multiallelic sites into biallelic records (so record count can rise).
3. `bcftools norm -f REF` — left-align and trim indels against the reference FASTA.

Downstream split requirement (operon): PLINK **yes**, ClinVar matching **yes**, VEP either, Hail no.

**ADAPT:** `INPUT_VCF` and `REF_FASTA` — the reference genome FASTA (+ `.fai`) must match the VCF's
assembly (GRCh38 vs GRCh37); set it via `BIOAGENT_REF_FASTA`. Writes `normalized.vcf.gz` (indexed)
to `BIOAGENT_WORK` for the annotation step to read, and a `normalization_summary.json` with pre/post
record counts. Needs `bcftools` on PATH (analysis image only — the skill exits with a clear message
if it is absent or the reference FASTA is missing, since left-alignment cannot be faked).

## Run
Fetch the template with `read_skill_reference("normalize_vcf", file="reference.py")`, set `INPUT_VCF`
/ `REF_FASTA` for THIS dataset, then run it via `run_code` (reads from `BIOAGENT_WORK` /
`BIOAGENT_DATASET`, writes the normalized VCF back under `BIOAGENT_WORK`). Then point
`annotate_variants` at `normalized.vcf.gz`.
