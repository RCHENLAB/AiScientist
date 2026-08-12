---
name: build_variant_db_tiledbvcf
description: Reference template — build/query a scalable TileDB-VCF variant database (cohort/population scale).
---

## When to use

ADAPT this via run_code for the OPTIONAL variant-database-management step (skip it for a single VCF).
TileDB-VCF stores many single-sample VCFs in one sparse-array dataset with incremental sample
addition (no expensive merges), compressed storage, and fast region/sample queries.

## Details & adaptation

Requirements (heavy, optional — installed in the analysis image, NOT the gateway env):
  mamba install -c conda-forge -c bioconda -c tiledb tiledbvcf-py bcftools
Ingested VCFs must be **single-sample** and **indexed** (.tbi via tabix or .csi via bcftools):
  bgzip sample.vcf && tabix -p vcf sample.vcf.gz
Env conventions (see skills/README): BIOAGENT_WORK for the dataset dir, BIOAGENT_ARTIFACTS for exports.

## Run
Fetch the template with `read_skill_reference("build_variant_db_tiledbvcf", file="reference.py")`, adapt the CONFIG / marker / threshold values to THIS dataset, then execute it via `run_code` (reads checkpoints from `BIOAGENT_WORK`, writes under `BIOAGENT_ARTIFACTS`). If a purpose-built tool already covers the step, use the tool instead.
