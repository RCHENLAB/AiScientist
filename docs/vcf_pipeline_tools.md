# The variant (VCF) line: tools and data

Every program and reference dataset the variant path uses: what it is, where it is in the code,
and which setting turns it on. The files on HPC3 and their sizes are in
[`hpc3_assets.md`](hpc3_assets.md); how tools, skills and pipelines fit together is in
[`skills_and_pipelines_architecture.md`](skills_and_pipelines_architecture.md).

## The path

A VCF lists the positions where one person's DNA differs from the reference, about five million
for a whole genome. The path turns that list into annotated, prioritized variants:

```
VCF ── filter ── normalize ── annotate (VEP, ClinVar, gnomAD, predictors) ── reduce ── splice score ── tables
```

One tool does all of it: [`annotate_variants`](../src/aiscientist/tools/annotate_variants/TOOL.md).
It has two ways to run:

- **Offline, on HPC3** (`AISCIENTIST_VARIANT_ON_HPC=1`): a Slurm job inside `vep.sif` that reads
  the VCF in place and annotates it against a local VEP cache, with no network. This is the path
  for a whole-genome or whole-exome VCF. Code: `annotate_variants/offline.py`, entered through
  `tools/variant_cli.py`.
- **REST** (the fallback, and the only path without HPC3): the Ensembl VEP REST API, called from
  the gateway and capped at `max_variants` (500 by default). Code: `annotate_variants/tool.py`.

The genome build is read from the VCF header; `AISCIENTIST_VEP_ASSEMBLY` (default `GRCh37`) is
used only when the header does not say.

## Stages of the offline path

| Stage | Program | What it does | Runs when |
|---|---|---|---|
| Filter | bcftools | keeps `PASS` variants, and only the regions of `regions_bed` when one is given | `pass_only` (the default) or a `regions_bed` |
| Normalize | bcftools norm | splits multi-allelic sites and left-aligns indels, so an indel matches its ClinVar and gnomAD record | a reference FASTA is set for the build |
| Annotate | Ensembl VEP release 112, `--offline --cache` | gene, consequence, impact, SIFT, PolyPhen, gnomAD frequencies | always |
| ClinVar | VEP `--custom` | clinical significance and review status | the ClinVar file for the build is set |
| Predictors | VEP plugins | CADD, AlphaMissense and REVEL scores, and the MANE Select transcript | `AISCIENTIST_VEP_PLUGINS=1` and the file for the build exists |
| HGVS names | VEP `--hgvs` | standard `c.` and `p.` names | a reference FASTA is set for the build |
| Reduce | `apply_variant_filters` | drops variants more common than `max_pop_af` and outside the `genes` panel | the caller or the deployment sets them |
| Splice score | OpenSpliceAI | the largest of the four splice delta scores per variant, and its site | `AISCIENTIST_SPLICEAI=1` and a reference FASTA |
| IRD layers | `ird_annotate.py` | HGMD, retina exon, retina ATAC and dbscSNV lookups | `AISCIENTIST_IRD_ANNOTATE=1` |
| Disease model | `ird_prioritize.py` | a per-gene inheritance model (dominant, recessive, X-linked) from how many rare alleles the gene carries; it ranks the shortlist and drops nothing | always |
| Tables | `annotate_variants/tool.py` | `tables/variant_annotation.tsv` and the five deliverable tables | always |

Each optional stage is skipped, not failed, when its data is missing, and the tool's result says
which ran: `normalized`, `predictors` and `variant_filters`.

A variant counts as damaging for the high-priority shortlist if any of these holds: VEP impact
`HIGH`, SIFT deleterious, PolyPhen damaging, CADD ≥ 20, REVEL > 0.5, AlphaMissense > 0.564, or a
SpliceAI delta ≥ 0.5.

## Programs and data

| Program or data | What it is | Where |
|---|---|---|
| Ensembl VEP | Variant Effect Predictor: for each variant, the gene, the change and its severity | `vep.sif` (release 112) |
| VEP cache | the gene and transcript annotations VEP reads offline, one per build | `<reference>/vep_annotation/GRCh38`, `GRCh37` |
| bcftools, tabix | filtering, normalizing and indexing VCFs | `vep.sif`; also in `analysis.sif` for skills run with `run_code` |
| ClinVar | NCBI's database of variants with a clinical interpretation | `<reference>/vep_annotation/clinvar_GRCh38.vcf.gz`, `clinvar_GRCh37.vcf.gz` |
| gnomAD | population allele frequencies | inside the VEP cache |
| SIFT, PolyPhen | missense effect predictors built into VEP | inside the VEP cache |
| CADD | a genome-wide deleteriousness score, coding and non-coding | `<reference>/vep_annotation/plugins/cadd/`, one file per build |
| AlphaMissense | a missense pathogenicity score | `<reference>/vep_annotation/plugins/alphamissense/` (GRCh38 only) |
| REVEL | an ensemble missense pathogenicity score | `<reference>/vep_annotation/plugins/revel/`, one file per build |
| OpenSpliceAI | a splice-disruption predictor (PyTorch) | the conda environment `<root>/envs/openspliceai`, models in `<reference>/spliceai/OSAI-MANE-10000nt` |
| Reference FASTA | the genome sequence, for normalization, HGVS names and SpliceAI | `<reference>/vep_annotation/ref/` |
| IRD gene panel | the lab's curated RetNet list of inherited-retinal-disease genes | `src/aiscientist/tools/gene_panels/ird_retnet.txt` |

`<root>` is `/dfs3b/ruic20_lab/software/AiScientist` and `<reference>` is
`/dfs3b/ruic20_lab/software/reference`.

**Predictor files are specific to a genome build.** The lookup is by coordinate, so a GRCh38 file
on a GRCh37 run would return the score of a different variant. Each build has its own setting, and
an empty one means that predictor is skipped for that build.

**How OpenSpliceAI runs.** It needs PyTorch, which `vep.sif` does not have, so it lives in its own
conda environment. The annotation job runs the environment's `openspliceai` binary as a subprocess
inside `vep.sif`, with the environment, the models and the reference FASTA bound read-only.
Inference takes about 50 seconds per variant on a CPU, so the stage runs after the reduction, on
the variants that are left. `AISCIENTIST_SPLICEAI_MAX_VARIANTS` (0 = no cap) skips it, with a note
in the result, when the set is still larger than the cap.

## A rare-disease study

The causal variant of an inherited disease is rare and usually in a known gene, so the reduction
is known genes first, common variants dropped:

- `genes`: keep only variants in a gene panel, after annotation.
- `regions_bed`: restrict the VCF to a panel's regions before VEP. On a whole-genome VCF this cuts
  the annotation from most of an hour to minutes; `genes` does not, because it filters afterwards.
- `max_pop_af`: drop variants whose gnomAD frequency is above the cutoff. Variants with no
  frequency are kept.

The Scientist passes these as arguments. A deployment can set defaults that apply when it does
not: `AISCIENTIST_DEFAULT_GENE_PANEL` (a panel name, `ird`), `AISCIENTIST_DEFAULT_REGIONS_BED` and
`AISCIENTIST_DEFAULT_MAX_POP_AF`.

## Skills and pipelines on this line

Skills in [`skills/`](../skills/README.md), which the Scientist adapts and runs with `run_code`:

- `normalize_vcf`: bcftools norm, as a separate step. Needs a reference FASTA.
- `vcf_qc_stats`: Ti/Tv, het/hom, SNP, indel and multi-allelic counts, call rate (bcftools stats,
  with cyvcf2 and a pure-Python fallback).
- `clinical_variant_prioritization`: a research-triage tier per variant from ClinVar, gnomAD
  frequency and the predictors. Not a clinical ACMG classification.
- `variant_output_tables`: the standard result tables from an annotated TSV.
- `build_variant_db_tiledbvcf`: a TileDB-VCF database for a cohort. It needs `tiledbvcf-py`, which
  `analysis.sif` does not carry.

Pipelines in [`preset_pipelines/`](../preset_pipelines/README.md): `variant_annotation`, and
`phenotype_variant_diagnosis`, which adds the patient's phenotype (`map_phenotype_to_hpo`,
`run_lirical`).

## Settings

Every path defaults to its location under `<reference>` or `<root>` (`gateway/settings.py`), so a
deployment sets the switches and the GRCh37 files, which have no default. The full list is section
9 of [`configs/aiscientist.example.env`](../configs/aiscientist.example.env).

| Setting | Default | What it turns on |
|---|---|---|
| `AISCIENTIST_VARIANT_ON_HPC` | off | the offline path |
| `AISCIENTIST_VEP_PLUGINS` | off | CADD, AlphaMissense, REVEL and MANE Select |
| `AISCIENTIST_SPLICEAI` | off | OpenSpliceAI |
| `AISCIENTIST_IRD_ANNOTATE` | off | the IRD layers; `AISCIENTIST_IRD_HGMD`, `_DBSCSNV`, `_RETINA_EXONS` and `_ATAC` name their files |
| `AISCIENTIST_REF_FASTA_GRCH37`, `AISCIENTIST_VEP_REVEL_GRCH37` | empty | normalization, HGVS names and SpliceAI, and REVEL, on GRCh37. The files are staged: `ref/hg19.fa` and `plugins/revel/new_tabbed_revel_grch37.tsv.gz`. |
| `AISCIENTIST_VEP_ALPHAMISSENSE_GRCH37` | empty | AlphaMissense on GRCh37. No file is staged for it. |
| `AISCIENTIST_VEP_FORK`, `AISCIENTIST_VEP_TIME_LIMIT` | 8, 04:00:00 | the annotation job's CPUs and time limit |

## Building and staging

- `deploy/vep/build_and_stage.sh` builds `vep.sif` and downloads the VEP caches and ClinVar. The
  VEP release in the image tag and the cache release must match; change both together.
- `deploy/vep/stage_annotation_dbs.sh` downloads the predictor files and the reference FASTA that
  are missing, creates the OpenSpliceAI environment, and prints the settings to add. It can be run
  again safely. Details: `deploy/vep/README.md` and `deploy/vep/PREDICTOR_STAGING.md`.
- Downloads run in a Slurm job or through `access-hpc3.rcic.uci.edu`, never on a login node.
