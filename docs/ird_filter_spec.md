# IRD annotate/filter/prioritize — SPEC (ported from the lab pipeline)

Authoritative spec for reproducing the lab's IRD annotate+filter+prioritize LOGIC in our own scripts
(Strategy A). Extracted from the lab pipeline on HPC3 (read-only, authorized by Rui Chen 2026-07-12;
HGMD there is a **public** version):
`/dfs3b/ruic20_lab/chen/pipeline_restructure/pipeline_restructure/bin/annotate_filter/`
(`annotationTools.py`, `pipeline_filter_and_annotate.py`). We reproduce the LOGIC on top of our VEP
annotation; we do NOT run their ANNOVAR/Perl code. Validate by diffing against a real lab run (the "B
oracle") on the same VCF.

## Defaults (their `pipeline_filter_and_annotate.py`)
- `cutoff = 0.005` — population-frequency cutoff for the "desired VCF" categorization.
- `splicing = 0.6` — dbscSNV ada/rf splice threshold.
- `filterCutoff = 0.01` — the pre-filter `filterVCF` cutoff.

## 1. Frequency (`filterVCF`, `addFreq`)
- `filt_freq` = the **highest ancestry-specific** allele frequency across a COMBINED controls panel:
  `ExAC + 1000G phase3 + HGVD + CHARGE + UK10K + 1000internal` (file
  `ExAC_1KGphase3_HGVD_CHARGE_1000internal_UK10K.gz`), plus gnomAD WES/WGS columns.
- Rare = `filt_freq < cutoff`. **Not gnomAD-only** — it's an ancestry-max over a control panel.
  - **Our mapping:** use the max population AF our VEP run already has (gnomAD/1000G). Closest available;
    note the panel differs, so freq-borderline calls may differ slightly from the lab. (Optional later:
    add the same combined controls file, staged on HPC3, as a custom-annotation source.)

## 2. `reason_for_inclusion` decision cascade (`parseAnnotationsForDesiredVCF`, cutoff=0.005, splicing=0.6)
Evaluated **in this order**; first match wins; `none` ⇒ dropped when filtering:
1. `HGMD_mutations_nearby=MATCH` present ⇒ **`HGMD_match`** — an exact HGMD hit is kept **regardless of
   frequency** (highest priority).
2. else if `filt_freq >= 0.005` ⇒ **dropped** (too common).
3. else if `variant_exceptions_list` present (≠ ".") ⇒ **`variant_exceptions_list`** (curated keep-list).
4. else if `ada_score >= 0.6` **or** `rf_score >= 0.6` ⇒ **`splice_prediction>0.6`**.
5. else if splicing (`Func.refGene`/`knownGene` startswith `splicing` or contains `&splicing`) **or**
   `ExonicFunc` is not `.` and not `synonymous_SNV` ⇒ **`protein-altering`** (any non-synonymous coding
   change or splice-region change).
6. else ⇒ **`none`** (synonymous / non-coding with no splice signal ⇒ dropped).

**Net inclusion rule:** keep a variant iff — exact HGMD match (any freq) **OR** (rare `< 0.005` **AND**
(on the exception list **OR** dbscSNV splice `≥ 0.6` **OR** protein-altering/splice-region)).

## 3. Annotation layers added (each an INFO tag; our equivalent)
| Lab tag | Source / rule | Our implementation |
|---|---|---|
| `HGMD_mutations_nearby` | HGMD (public v.2016) within **15 bp**; exact ⇒ `MATCH-` prefix | bedtools/tabix window vs the staged HGMD file |
| `retina-specific_exon` | overlap the Eric Pierce retina-specific-exon BED | bedtools intersect vs the staged BED |
| `ada_score` / `rf_score` | dbscSNV (in dbNSFP3.5a) | VEP dbscSNV/dbNSFP plugin at the staged file |
| SIFT/PolyPhen/REVEL/CADD/MetaSVM/… | dbNSFP3.5a | VEP CADD/REVEL/dbNSFP plugins at the staged files |
| `gene-disease_annotation` | gene→disease + inheritance table | staged table join |
| `MacATAC`/`PercATAC` | retina ATAC peak overlap | bedtools intersect vs the staged narrowPeak |
| SpliceAI | (their `reference/SpliceAI`; we also have OpenSpliceAI) | our SpliceAI stage (≥0.5), plus ada/rf ≥0.6 |

## 4. Gene-level tiering (TODO — extract before Phase 2 tiering)
The sample's `reason_for_inclusion` comment rows encode a **per-gene rollup** applied AFTER the
per-variant categorization (the final Excel/report grouping), with rules like:
- gene with **≥2** variants where **≥2** have freq `≤ 0.005`, on autosomes (compound-het / recessive);
- gene with **≥1** variant freq `≤ 0.0001`, on autosome (rare dominant);
- same `+ rf_percentage ≤ 50` (gene-level recessive/dominant RF score);
- X-chromosome variant rules.
These live in the report/excel step (`pipeline.custom.excel.test.py` / `excel.test.py`), not in
`parseAnnotationsForDesiredVCF`. **Extract the exact rules there before implementing Phase 2 gene-level
tiering.** Feeds `clinical_variant_prioritization`.

## Located reference files (for deploy) — IMPORTANT format caveat
Exact paths under `/dfs3b/ruic20_lab/chen/pipeline_restructure/pipeline_restructure/`:

**Directly usable by us (annotation files — bedtools/tabix, format-agnostic; "read his data for now"):**
- HGMD (public v.2016): `bin/annotate_filter/HGMD_v.12-20-2016.SNVs.INDELs.parsedforVCFannotationandindexingfixed.txt.gz` (+ `.tbi`)
- Retina-specific exons (BED): `bin/annotate_filter/Eric_Pierce_human_RNA-seq_derived_retina-specific_exons`
- Retina ATAC peaks: `reference/ATACseq/92526MacATAC_92556MacATAC.macs._peaks.narrowPeak.gz` (+ a Perc/PercATAC file)

**NOT directly usable by VEP plugins (his copies are ANNOVAR/custom format, mostly GRCh37):**
- CADD: `reference/CADD.1.4/annotationsGRCh37.tar.gz` — a **tarball, GRCh37**, NOT VEP's
  `whole_genome_SNVs.tsv.gz`. VEP's CADD plugin cannot read it.
- dbNSFP: `bin/dbNSFP3.5a/dbNSFP3.5a_variant.chr*` — **per-chromosome ANNOVAR files**, NOT the single
  bgzip+tabix dbNSFP VEP wants.
- REVEL: `reference/revel/revel_score.gz` (+ `.tbi`) — closer to a tabbed REVEL file; verify it matches
  the VEP REVEL plugin's expected columns before pointing at it.

⇒ **For the VEP-plugin predictors (CADD/REVEL/dbNSFP), stage the standard PUBLIC VEP-format
distributions** (that is what `deploy/vep/stage_annotation_dbs.sh` + the `BIOAGENT_VEP_*` defaults
already target) — same underlying data, VEP-compatible, and GRCh38+GRCh37. Do NOT wire `BIOAGENT_VEP_*`
at the lab's ANNOVAR copies. The HGMD/retina-exon/ATAC layers DO use the lab files directly.

## Parity note
Because our annotation engine is VEP/Ensembl and theirs is ANNOVAR/refGene, reproducing this logic yields
**clinical-grade parity** (the same candidate genes/variants surface), not a byte-identical table. The B
oracle diff measures concordance; ~94% gene-level agreement was already observed on the benchmark VCF.
