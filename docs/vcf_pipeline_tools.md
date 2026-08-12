# VCF pipeline — tool & data inventory

The canonical list of every tool and reference dataset the variant (VCF) path uses: what it is (in
plain terms), where it lives in our code, and its status. Companion to
[`skills_and_pipelines_architecture.md`](skills_and_pipelines_architecture.md). Last updated 2026-07-08.

## The path in one line

A VCF is a diff: ~5M positions where one person's DNA differs from the reference. The path turns that
raw diff into interpreted, prioritized variants:

```
VCF ── normalize ── QC ── annotate (VEP + ClinVar + gnomAD + predictors) ── prioritize ── report
                                                                      └── (cohort) TileDB-VCF database
```

## Status legend

- **live** — wired and running today.
- **staging** — being downloaded / set up (this week).
- **deferred** — planned, not started (reason noted).

## Tools & data

| Tool / data | What it is (plain) | Type | Where in our code | Status |
|---|---|---|---|---|
| **Ensembl VEP** | Variant Effect Predictor — for each variant: which gene, what change (consequence), severity (impact) | CLI + REST | `tools/vcf_offline.py` (offline), `tools/variant_annotation.py` (REST) | **live** |
| VEP offline cache | Local copy of all gene/transcript annotations so VEP runs with no network, WGS-scale | reference data | `deploy/vep/` → HPC3 `vep_annotation/{GRCh38,GRCh37}` | **live** (GRCh38 25 GB, GRCh37 17 GB) |
| **bcftools** | Swiss-army CLI for VCFs (filter, normalize, stats, query) | CLI | in `vep.sif`; used by `vcf_offline.py`, `normalize_vcf`, `vcf_qc_stats` | **live** |
| tabix / htslib | Index + random-access for bgzipped VCF/TSV (fast region lookup) | CLI/lib | in `vep.sif`; indexes cache + plugin files | **live** |
| **ClinVar** | NCBI database of variants clinically known to cause disease (with a review-status "star" rating) | reference data (VCF) | VEP `--custom`; `vep_annotation/clinvar_{GRCh38,GRCh37}.vcf.gz` | **live** (~180 MB each) |
| **gnomAD** | Population allele frequencies — how common a variant is (common ⇒ likely harmless) | reference data (in cache) | VEP `--af_gnomade/--af_gnomadg` flags | **live** |
| SIFT, PolyPhen | Built-in VEP predictors: does a missense change break the protein? | in VEP | `_VEP_ANNOT_FLAGS` (`--sift b --polyphen b`) | **live** |
| **CADD** | Genome-wide deleteriousness score (incl. non-coding); cutoff ≥20 = damaging | VEP plugin + data | `vep_annotation/plugins/cadd/` (GRCh38 SNVs) | **staged + verified** (87 GB; BRAF V600E cadd_phred=29.8) |
| **AlphaMissense** | DeepMind's missense pathogenicity score; >0.564 = likely pathogenic | VEP plugin + data | `vep_annotation/plugins/alphamissense/` (hg38) | **staged + verified** (643 MB; am_pathogenicity=0.9927) |
| **REVEL** | Ensemble missense pathogenicity score; >0.5 = likely pathogenic | VEP plugin + data | `vep_annotation/plugins/revel/` | **staged + verified** (675 MB GRCh38-tabbed; BRAF V600E revel=0.931) |
| **SpliceAI** (OpenSpliceAI) | Predicts whether a variant disrupts RNA splicing (the splice-prediction class VEP's protein predictors miss) — 4 delta scores (Acceptor/Donor × Gain/Loss); max ≥0.5 = likely splice-altering | PyTorch model (conda env) | `tools/vcf_offline.py` (`build_spliceai_cmd`/`parse_spliceai_vcf`, gated stage) | **installed + verified** — OpenSpliceAI 0.0.7 env + OSAI-MANE-10000nt models; BRAF/SAMD11 donor variants scored DS_DL 0.917/0.755 end-to-end INSIDE vep.sif |
| MANE Select / HGVS | Pick the one clinically-standard transcript per gene; emit standard `c.`/`p.` variant names | VEP flags | `build_vep_cmd` (`--mane_select` with plugins; `--hgvs` with `BIOAGENT_REF_FASTA`) | **code done** — activates when plugins / ref FASTA are staged |
| **TileDB-VCF** | A database that stores many samples' variants in one compressed, indexed array for fast population queries + incremental sample addition | Python lib | `skills/build_variant_db_tiledbvcf/` | **live** (skill; heavy dep, analysis image) |
| cyvcf2 | Fast Python VCF reader | Python lib | `skills/vcf_qc_stats/` fallback path | **live** (optional) |

## Skills that use these tools

Atomic skills (`skills/<name>/`, folder form — see the architecture doc). Each is a rewritable
template the agent fetches and runs via `run_code`:

- `normalize_vcf` — **bcftools norm** (atomize MNPs, split multiallelic, left-align indels). Must run
  before annotation, or a non-canonical indel silently misses ClinVar/gnomAD. Needs a reference FASTA.
- `vcf_qc_stats` — **bcftools stats** (or cyvcf2 / stdlib fallback): Ti/Tv, Het/Hom, counts, call rate.
- `clinical_variant_prioritization` — pure stdlib; tiers variants from ClinVar + gnomAD AF + predictors.
- `build_variant_db_tiledbvcf` — **TileDB-VCF** cohort database.

The end-to-end workflow that composes them: `preset_pipelines/variant_annotation/SKILL.md`.

## Rare-disease / known-gene workflow (Rui Chen's IRD protocol)

For an inherited-disease study the causal variant is rare and usually in a known gene, so `annotate_variants`
takes two reduction params (both default off → general runs unchanged):

- `genes` — a known disease-gene panel (gene symbols); keeps only variants in those genes. Or
  `regions_bed` (offline line) to restrict BEFORE VEP for a big compute saving on a WGS VCF.
- `max_pop_af` — drop variants with gnomAD population AF above this (e.g. `0.01` removes everything
  >1%); novel / no-frequency variants are kept.

Strategy: known genes (exons + introns) first, common variants dropped; expand to the whole genome only
if the known-gene search is negative. The result carries `variant_filters` (how many were dropped).
**Open:** the known-IRD-gene list (from Meng) plugs into `genes` / a panel BED.

## Staging the DBs (Jin Li's download-once pattern)

`deploy/vep/stage_annotation_dbs.sh` — idempotent: checks which DB files already exist under the shared
plugins dir and downloads only the missing ones (AlphaMissense, CADD, REVEL, reference FASTA), then
prints the `BIOAGENT_VEP_*` env lines to set. Bind-mount that dir read-only into `vep.sif`.

## Reference-data footprint on HPC3

Staged in the lab's SHARED reference dir `/dfs3b/ruic20_lab/software/reference/vep_annotation/` (Jin
Li's convention — download once, mount read-only, reuse across projects; the `vep.sif` container stays
under `.../bioagent/containers/`). The lab's dfs3b quota is **600 TiB, ~97% used
(~16 TiB free)**; Jin Li confirmed the lab keeps **>20 TB free (can grow past 50 TB)** for this, so
the ~150 GB of predictor data is not a blocker — but download each DB ONCE and bind-mount it (see the
staging script). Cohort variant databases are the real future consumer:

| Component | Size |
|---|---|
| `containers/vep.sif` (VEP + bcftools + tabix) | 230 MB |
| VEP cache GRCh38 / GRCh37 | 25 GB / 17 GB |
| ClinVar (both assemblies) | ~360 MB |
| CADD GRCh38 (staging) | 87 GB |
| AlphaMissense hg38 (staging) | 643 MB |
| OpenSpliceAI conda env (torch/cuda libs, `bioagent/envs/openspliceai`) | ~7 GB |
| OpenSpliceAI-MANE-10000nt models (5 × 2.8 MB, `reference/spliceai/`) | ~14 MB |
| **Total (current + this week's plugins + SpliceAI)** | **~137 GB** |

REVEL (+ optional dbNSFP bundle) and GRCh37 plugin copies would add more later; ~130 GB is ~0.8% of
the ~16 TiB remaining headroom — fits, but not free (the lab is at 97% of its 600 TiB quota).

## Env vars / paths

The code for all of the below is wired (`tools/vcf_offline.py`) and **gated + graceful**: every extra
is off/skipped unless its env var is set and the data exists, so the baseline SIFT/PolyPhen annotation
runs unchanged until the deploy env is flipped.

```
BIOAGENT_VARIANT_ON_HPC=1            # use the offline VEP line (default OFF → REST fallback, 500-cap)
BIOAGENT_VEP_IMAGE=.../containers/vep.sif
BIOAGENT_VEP_CACHE_DIR_GRCH38=.../vep_annotation/GRCh38
BIOAGENT_VEP_CACHE_DIR_GRCH37=.../vep_annotation/GRCh37
BIOAGENT_VEP_CLINVAR_GRCH38=.../vep_annotation/clinvar_GRCh38.vcf.gz

# Predictor plugins — set once the data is staged (master switch defaults OFF):
BIOAGENT_VEP_PLUGINS=1              # turn on CADD/AlphaMissense/REVEL + MANE-Select
BIOAGENT_VEP_PLUGINS_DIR=.../vep_annotation/plugins/vep_plugins   # the .pm scripts (VEP --dir_plugins; no sif rebuild)
BIOAGENT_VEP_CADD_SNV=.../vep_annotation/plugins/cadd/whole_genome_SNVs.tsv.gz
BIOAGENT_VEP_CADD_INDELS=...       # optional
BIOAGENT_VEP_ALPHAMISSENSE=.../vep_annotation/plugins/alphamissense/AlphaMissense_hg38.tsv.gz
BIOAGENT_VEP_REVEL=.../vep_annotation/plugins/revel/new_tabbed_revel_grch38.tsv.gz

# Normalization + HGVS names — needs the reference genome FASTA (+ .fai) for the assembly:
BIOAGENT_REF_FASTA=.../ref/GRCh38.primary_assembly.fa

# SpliceAI (OpenSpliceAI) — splice-disruption scoring; separate conda env, run INSIDE vep.sif (its
# conda-forge python runs under the container's glibc — validated). OFF by default; needs BIOAGENT_REF_FASTA:
BIOAGENT_SPLICEAI=1                                              # master switch (default OFF)
BIOAGENT_SPLICEAI_BIN=.../bioagent/envs/openspliceai/bin/openspliceai
BIOAGENT_SPLICEAI_MODELS=.../reference/spliceai/OSAI-MANE-10000nt   # 5-model ensemble (~14 MB)
BIOAGENT_SPLICEAI_MAX_VARIANTS=0       # 0 = NO cap (default); set >0 as an optional safety valve
#                                        (~50 s/variant on CPU, so keep the panel/AF filter tight)
```

**How SpliceAI runs.** OpenSpliceAI needs PyTorch, which is NOT in `vep.sif`, so it lives in its own
conda env (`.../bioagent/envs/openspliceai`, Python 3.10 + torch). The offline annotation runs inside
`vep.sif`, and the SpliceAI stage execs the env's `openspliceai variant` binary as a subprocess — the
conda-forge python runs cleanly under the container's glibc (verified on HPC3). The gateway bind-mounts
the env dir + the model dir + the ref-FASTA dir (read-only; the `.fai` is present so pyfaidx never
rebuilds) and points `HOME`/`TORCH_HOME` at a writable per-run dir. Because inference is ~50 s/variant
on CPU, the stage is meant to run **on the variants left after the gene-panel / AF reduction** — there is
NO hard cap by default (`BIOAGENT_SPLICEAI_MAX_VARIANTS=0`), but a `>0` value is an optional safety valve
that skips it (with a loud note) if the set is still huge, so a mis-configured whole-WGS run can't hang
for days. Adds `spliceai_max_ds` + `spliceai_site` columns;
a max delta ≥0.5 also counts as damaging in the high-priority shortlist.

Each predictor is added to the VEP command only when its var is set AND the file exists; the
`bcftools norm` (left-align) stage runs only when `BIOAGENT_REF_FASTA` points at a real file.
`annotate_variants` reports `normalized` + `predictors` so a run states its annotation depth honestly.

Build/stage kit: `deploy/vep/` (`build_and_stage.sh`, `vep.def`, `README.md`, `PREDICTOR_STAGING.md`).
