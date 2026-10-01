---
name: annotate_variants
summary: Annotates VCF variants with consequence, gene, impact, population frequency and ClinVar significance, and writes the standard deliverable tables.
category: annotation
runs_on: hpc:variant
entry: tool:make_variant_annotation_tool
order: 500
line: variant
owner: variant
---
# annotate_variants

Annotates each variant of a VCF with its functional consequence, affected gene, predicted impact, population frequency and ClinVar significance. On HPC3 it runs Ensembl VEP offline against a local cache; small VCFs, or a session without HPC3, use the VEP REST API instead. It writes the complete per-variant table and the five standard deliverable tables, so no post-processing code is needed.

## When the agent uses it

- On a bound VCF, before any phenotype or diagnosis step that uses variants.
- With `genes` or `regions_bed` to restrict the call set, and with a gene panel (IRD) for disease-model tiering.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `vcf_path` | string | — | path to a .vcf/.vcf.gz (defaults to the run's dataset) |
| `assembly` | string | — | GRCh38 (default) or GRCh37/hg19 |
| `species` | string | — | Ensembl species (default human) |
| `pass_only` | boolean | — | annotate only FILTER=PASS/'.' variants (default true) |
| `max_variants` | integer | — | cap variants annotated (default 500) |
| `max_pop_af` | number | — | drop variants with gnomAD population AF above this (e.g. 0.01 for a rare-disease study); 0 = keep all (default) |
| `genes` | array of string | — | restrict to this known disease-gene panel (gene symbols); empty = all |
| `regions_bed` | string | — | offline line only: a BED to restrict annotation to a panel's regions BEFORE VEP (big compute saving on a WGS VCF) |
<!-- /generated:parameters -->

## Outputs

- `tables/variant_annotation.tsv` (gene, position, rsID, consequence, impact, ClinVar, gnomAD AF, SIFT, PolyPhen) and five deliverable tables: consequence, impact and clinical-significance distributions, the ClinVar pathogenic list, and the rare unclassified high-priority shortlist.
- Result: counts by consequence, impact and significance, PASS vs non-PASS counts, the pathogenic and likely pathogenic variants.

## Where it runs

<!-- generated:runs-on -->
As a Slurm job inside `vep.sif` on HPC3 (offline VEP with a local cache), through `python -m aiscientist.tools.variant_cli`. Small VCFs, or a session without HPC3, use the Ensembl REST API from the gateway instead (capped at a few hundred variants).

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- Predictor scores (CADD, REVEL, AlphaMissense, SpliceAI) appear only when their plugin files are configured on HPC3.
- The REST path is capped (`max_variants`, 500 by default).
- Each call is a full VEP job (30 to 60 minutes on a WGS VCF); calling it again re-runs and overwrites the tables.

## Code and tests

- `tool.py`: the REST path, the tables and the record; `offline.py`: the offline VEP command and parsing; `ird_annotate.py` and `ird_prioritize.py`: the IRD layers and gene-level tiering (the lab panel is in `../gene_panels/`). The job entry point is `../variant_cli.py`.
- Tests: `tests/test_variant_annotation.py`, `tests/test_ird_annotate.py`, `tests/test_ird_prioritize.py`, `tests/test_variant_output_tables_skill.py`.

## What the model is told

<!-- generated:model-description -->
> Annotate a VCF's variants with functional consequence, the affected gene, predicted impact, and
> ClinVar clinical significance (pathogenicity), via the Ensembl VEP REST API. Pass `vcf_path` (a
> .vcf or .vcf.gz on the host, or the run's loaded dataset). Filters to PASS variants by default
> (pass_only) and returns REAL FILTER counts (n_pass / n_nonpass) — never assume 'all PASS'.
> Writes the COMPLETE per-variant annotated table to `tables/variant_annotation.tsv`
> (gene/position/rsID/consequence/impact/ClinVar/gnomAD AF/SIFT/PolyPhen) AND the five standard
> deliverable tables (consequence/impact/clinical-significance distributions, the ClinVar
> pathogenic list, the rare-unclassified high-priority shortlist) — so you do NOT need ANY
> run_code to post-process: report directly from these files. Returns counts by consequence /
> impact / clinical significance and the list of pathogenic + likely-pathogenic variants (gene,
> location, rsID). For a rare-disease study, set `max_pop_af` (e.g. 0.01) to drop common variants
> and/or `genes` to focus on a known disease-gene panel first. Reports ONLY what VEP/ClinVar
> return; never invents a gene, consequence, or significance.
<!-- /generated:model-description -->
