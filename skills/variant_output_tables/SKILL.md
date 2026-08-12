---
name: variant_output_tables
description: Reference template — write the standard variant-annotation result tables from the annotated TSV.
---

## When to use

Turns the per-variant table `annotate_variants` already persisted
(`tables/variant_annotation.tsv`, columns: location, allele, gene_symbol, gene_id, consequence,
impact, amino_acids, sift, polyphen, max_af, rsid, clinical_significance) into the five standard
deliverables + a summary JSON — WITHOUT re-parsing the VCF or re-running VEP (annotate_variants did
that). Pure stdlib (csv / json / collections), so there are NO pandas dtype pitfalls; this is a
tested template to adapt-and-run via run_code, NOT code to rewrite from scratch. ADAPT only the
thresholds (RARE_AF, HIGH_IMPACT, damaging predictors) if the study needs different cutoffs.

## Run
Fetch the template with `read_skill_reference("variant_output_tables", file="reference.py")`, adapt the CONFIG / marker / threshold values to THIS dataset, then execute it via `run_code` (reads checkpoints from `BIOAGENT_WORK`, writes under `BIOAGENT_ARTIFACTS`). If a purpose-built tool already covers the step, use the tool instead.
