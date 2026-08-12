# VCF variant-annotation pipeline — plan + team decisions

## Resolved by the team (2026-07-08)

- **SpliceAI — NOT blocked.** Jin Li: install via bioconda, or use OpenSpliceAI
  (github.com/Kuanhao-Chao/OpenSpliceAI, easier to deploy); the lab uses SpliceAI heavily and supports
  both. Leaning toward running the model (conda/OpenSpliceAI) rather than the large BaseSpace
  precomputed scores — feasible now that the known-gene panel keeps the variant set small.
- **CADD + other DBs — storage OK, download-once pattern.** Jin: keep one local DB dir under the lab
  shared folder, download each DB ONCE, bind-mount it into the container; the agent checks which files
  exist and only downloads the missing ones (→ `deploy/vep/stage_annotation_dbs.sh`). Lab shared
  storage has >20 TB free (can grow past 50 TB), so CADD (87 GB) is fine.
- **Analysis strategy — rare-disease, KNOWN GENES FIRST (Rui Chen, PI).** For IRD: (1) restrict to the
  known IRD gene panel (Meng to provide the gene list) — its exons + introns — expanding to WGS only if
  negative; (2) drop variants with population AF > 1% up front. Wired as `annotate_variants`' `genes` +
  `max_pop_af` params + the `regions_bed` region restriction; encoded in the variant preset. **OPEN:
  Meng's known-IRD-gene list** (plugs into `genes` / a panel BED).

The original decision email (as sent) follows.

---

> Copy from here down. Prepared 2026-07-08.

---

**Subject:** VCF variant-annotation pipeline — plan, workflow, and tools/cost (need your call on two items)

Hi Jin Li,

We're building out the variant (VCF) analysis path in AiScientist. Before we finalize the reference-data
staging on HPC3, I'd like your input on a couple of cost decisions. Here's what we're doing, the
workflow, the tools and their cost, and the two decisions I need from you.

## 1. What we're building

Two capabilities:

- **Variant annotation.** Take a VCF (one person's ~5 million DNA variants) and, for each variant,
  attach meaning: which gene and what functional change (Ensembl VEP), how common it is in the
  population (gnomAD), how likely it is to be damaging (predictors: SIFT/PolyPhen + CADD / AlphaMissense
  / REVEL), and whether it is a known disease variant (ClinVar). Then prioritize the actionable
  variants (pathogenic → uncertain → benign) and produce a report.
- **Variant database (population scale).** Store many samples' variants in a TileDB-VCF database, so we
  can add samples incrementally and run fast cross-sample / population-scale queries.

**Goal this week:** the annotation path runs end-to-end on HPC3 for a real whole-genome VCF, offline,
at publication-grade annotation quality, and deployed.

## 2. Workflow

```
VCF ─▶ normalize ─▶ QC ─▶ annotate (VEP + ClinVar + gnomAD + predictors) ─▶ prioritize ─▶ report
                                                                      └─▶ (cohorts) TileDB-VCF database
```

- **normalize** (bcftools): left-align / split variants so they match the databases — otherwise a real
  pathogenic indel silently fails to match ClinVar/gnomAD.
- **QC**: Ti/Tv, Het/Hom, call rate — is the callset trustworthy before we interpret it?
- **annotate → prioritize → report**: the core of the path.

Everything runs offline on HPC3 (Singularity + Slurm) against locally-staged reference data.
**Why local rather than the online APIs:** a whole-genome VCF has ~5M variants; the Ensembl REST API
is rate-limited and caps at a few hundred variants, so it cannot annotate at that scale — the local
cache does the whole file in ~30–60 min. (HPC3 compute nodes *do* have internet — I verified this — so
this is about throughput/scale, not connectivity. Reproducibility is a secondary benefit; data
residency is not a concern here since VEP only sends coordinates and our data is de-identified.)

## 3. Tools and cost

"Cost" = one-time download/setup + permanent storage + any container rebuild.

| Tool / data | What it is | Cost | Necessary? |
|---|---|---|---|
| Ensembl VEP + offline cache | annotation engine + local gene annotations | already staged (~42 GB) | **necessary** |
| bcftools / tabix | normalize, filter, stats, index VCFs | already in the container | **necessary** |
| ClinVar | known disease-causing variants | already staged (~0.4 GB) | **necessary** |
| gnomAD | population allele frequency (rarity) | already in the cache | **necessary** |
| SIFT / PolyPhen | built-in missense damage predictors | free (inside VEP) | **necessary** |
| AlphaMissense | DeepMind missense pathogenicity score | 0.6 GB — **already downloaded** | recommend (cheap, high value) |
| REVEL | ensemble missense pathogenicity score | ~0.5 GB + small prep step | recommend (cheap) |
| Reference genome FASTA (GRCh38 + GRCh37) | needed for normalization + standard variant names (HGVS) | ~3 GB each | needed for normalize |
| VEP plugin scripts | glue that lets VEP read CADD/AlphaMissense/REVEL | tiny; **may need a vep.sif rebuild** | needed if we use predictors |
| TileDB-VCF (`tiledbvcf-py`) | the population-scale variant database | dependency only; **needs an analysis.sif rebuild** | for capability 2 |
| **CADD** | genome-wide deleteriousness score (incl. non-coding) | **87 GB**, ~hours to download, permanent storage | **decision — see below** |
| **SpliceAI** | splice-disruption predictor (best in class) | needs an **Illumina/BaseSpace login**; ~few GB | **decision — see below** |

**Storage context:** the lab's dfs3b allocation is **600 TiB and currently ~97% used (~16 TiB free)**.
The full staging above is ~150 GB (<1% of the remaining headroom), so it fits — but we are near the
cap. The real future storage consumer is the population-scale TileDB databases, not these reference
tables; that will need its own plan.

## 4. Two decisions I need from you

1. **SpliceAI** — its precomputed scores need an Illumina/BaseSpace account to download. Do we have one?
   If not, we ship without it for now (its value is concentrated in splice-region variants) and add it
   later once we have access.
2. **CADD (87 GB)** — it fits, but since we're at 97% of the dfs3b quota, I don't want to add it
   reflexively. Do you want CADD as a permanent addition (it's the only predictor that scores
   non-coding variants), or should we go with AlphaMissense + REVEL (which cover coding/missense at a
   fraction of the size) and add CADD only if a study needs non-coding scoring?

Everything else (VEP cache, ClinVar, gnomAD, AlphaMissense, bcftools, reference FASTA, TileDB) is
either already staged or cheap, so I'll proceed with those unless you object.

Thanks,
Yijun
