---
name: variant_annotation
description: Interpret a VCF's variants (germline/somatic, WGS/WES) — gene + functional consequence (Ensembl VEP), ClinVar pathogenicity, gnomAD frequency, deleteriousness (CADD/REVEL/AlphaMissense) + splice disruption (SpliceAI), then prioritise. Rare-disease known-gene-first (IRD). Offline, WGS-scale.
tools: annotate_variants, run_code, literature_search
data_type: variants
---

Germline/somatic **variant annotation** protocol. Use when the input is a **VCF** (variant calls,
`.vcf`/`.vcf.gz`) and the goal is to interpret those variants — their functional consequence, the
gene they hit, predicted impact, and **clinical significance (ClinVar pathogenicity)** — rather than
single-cell expression. NOT for scRNA-seq (use the annotation / DE pipelines for that).

Annotation is done by the curated `annotate_variants` tool, so you do NOT write your own VEP/HTTP
code for the core step. It has two execution modes behind the SAME interface — you don't choose, the
gateway routes:

* **Offline VEP on HPC3** (default when `BIOAGENT_VARIANT_ON_HPC` is enabled): `bcftools` PASS-filter
  → `vep --offline --cache --fork` over a bind-mounted local cache, as a CPU Slurm job. Scales to a
  **WGS-size VCF** — annotates EVERY passing variant (no cap), no network, no rate limits (~30–60 min).
* **REST fallback** (small VCFs, or the offline line unavailable): Ensembl VEP REST + ClinVar on the
  local sandbox — capped at a few hundred variants and rate-limited, fine for a small panel VCF but
  NOT a WGS one.

**No post-processing code needed — the tool does it.** `annotate_variants` writes the COMPLETE
per-variant table (`tables/variant_annotation.tsv`) AND the five standard deliverable tables itself:
consequence / impact / clinical-significance distributions, `clinvar_pathogenic_variants.csv`, and the
rare-unclassified `high_priority_variants.csv` shortlist (their names are in the result's
`standard_tables`). So do NOT write ANY `run_code` to post-process — just report from those files.
Only for a CUSTOM cut (non-default AF cutoff, extra columns, a bespoke filter) fetch + adapt the
`variant_output_tables.py` skill; the standard tables are already written.

Report the `execution_mode` the tool returns (`offline_vep` vs `rest`) so it is clear which ran — a
`rest` run on a big VCF only sampled the top variants and must not be read as whole-genome coverage.

## Rare-disease / known-gene strategy (DEFAULT for a Mendelian / IRD study)

For an inherited-disease study (e.g. inherited retinal disease, IRD), the causal variant is RARE and
usually in a KNOWN disease gene — so narrow aggressively before interpreting (Rui Chen's protocol):

1. **Known genes FIRST.** Restrict to the study's known disease-gene panel — pass the gene list as
   `annotate_variants`' `genes`, or a panel BED as `regions_bed` (the offline line restricts BEFORE VEP,
   a big compute saving on a WGS VCF). Look in those genes' exons AND introns. Expand to the whole
   genome ONLY if the known-gene search is negative.
2. **Drop common variants.** Set `max_pop_af=0.01` so any variant with gnomAD population AF > 1% is
   removed — a >1% variant is too common to cause a rare disease. (Novel / no-frequency variants are kept.)
3. Then annotate → prioritize as below, on the small remaining rare, in-panel set.

State the gene panel used and the AF cutoff. If the known-gene search is negative, say so before
expanding to the whole genome.

## Ordered plan (plan these as DISTINCT agenda steps — a real variant study is not one "annotate" call)

Plan the phases below as SEPARATE steps; do NOT collapse them into a single annotate step — EXCEPT
normalization (step 2), which `annotate_variants` performs INTERNALLY on the offline path, so it is NOT
a separate agenda step there (see step 2). The heavy compute lives inside `annotate_variants` (step 4),
so steps 5–6 REPORT from the tables it already wrote — never hand-roll `run_code` to re-derive those
tables, and never add a report-writing step (the methods + results report is assembled automatically).
The QC (step 1) and known-gene / AF narrowing (step 3) pre-analysis is what makes the annotation
trustworthy and focused; skipping those is how a variant study silently ships wrong or unfocused results.

1. **QC the callset** (`vcf_qc_stats` skill — `read_skill_reference` then `run_code`). Ti/Tv, Het/Hom,
   SNP/indel/multiallelic counts, call rate, flagged against WGS/WES expected ranges. Report Ti/Tv up
   front; a flagged callset means everything downstream describes lower-confidence data (route that
   caveat to Diagnostics). Always worth doing — cheap, and it says whether the calls are trustworthy.
2. **Normalization — done INSIDE `annotate_variants`; do NOT plan a separate normalize step (offline
   path).** Splitting multiallelics + **left-aligning indels** matters (a non-left-aligned indel silently
   FAILS to match ClinVar/gnomAD → falsely `not_in_clinvar`), but on the offline path `annotate_variants`
   already runs `bcftools norm -m-any -f REF` in `vep.sif` (which has bcftools) before VEP — so a separate
   step is redundant. It also CANNOT run as its own `run_code` step: `run_code` executes in `analysis.sif`,
   which has no `bcftools` and no reference-FASTA bind, so the `normalize_vcf` template exits and the model
   degrades to a hand-rolled `cyvcf2` normalize — which is WRONG (cyvcf2 does not left-align) and just
   burns retries. So under `BIOAGENT_VARIANT_ON_HPC` (the default) SKIP this as a step; annotation
   normalizes for you. (Only a REST-path run on a small, indel-heavy, known-non-normalized VCF benefits
   from a prior `bcftools norm` — and that still needs bcftools + the reference present, not the analysis
   sandbox.)
3. **Narrow to the disease genes + drop common variants** (rare-disease / IRD DEFAULT — Rui Chen's
   protocol, see the strategy section above). Restrict to the study's known disease-gene panel
   (`annotate_variants`' `genes`, or a panel BED via `regions_bed` — the offline line restricts BEFORE
   VEP, a big WGS compute saving) AND set `max_pop_af=0.01` (drop gnomAD AF > 1%; novel variants kept).
   STATE the panel + AF cutoff. Expand to the whole genome ONLY if the in-panel search is negative. (For
   a general, non-Mendelian VCF with no candidate-gene prior, skip the panel and annotate genome-wide.)
4. **Annotate** (`annotate_variants`): pass the (panel-restricted per step 3) VCF. The **assembly is
   auto-detected from the VCF header** (chr1 contig length → GRCh37 vs GRCh38; the gateway overrides the
   configured default per run) — you normally don't set it. Most eye/IRD datasets are **GRCh37/hg19**,
   which is also the fallback when the header is silent; pass `assembly` explicitly only to correct a
   mis-detected build. STATE the assembly used, and never mix builds. The tool also normalizes internally
   (split multiallelic + left-align against the reference) before VEP, so it does NOT need a pre-normalized input.
   The tool returns, per variant: the affected gene, the **MANE-Select** transcript + **HGVS** `c.`/`p.`
   name, the most-severe consequence, VEP impact, in-silico deleteriousness — **SIFT**, **PolyPhen**, and
   (as their data is staged) **CADD** phred (≥20 damaging), **REVEL** (>0.5), **AlphaMissense** (>0.564) —
   plus **SpliceAI** splice-disruption (`spliceai_max_ds` + `spliceai_site`: donor/acceptor gain/loss;
   ≥0.5 likely splice-altering), the max **gnomAD/1000G population allele frequency** (rarity), and the
   **ClinVar** clinical significance (with its review-status stars) — plus counts by consequence / impact
   / significance / rarity, the pathogenic list, a `high_priority` shortlist, the REAL FILTER counts
   (n_pass / n_nonpass), the annotated table `tables/variant_annotation.tsv`, and the five standard
   deliverable tables. Report the `execution_mode` (`offline_vep` vs `rest` — a `rest` run on a big VCF
   only sampled the top variants). Each predictor is blank unless its data is staged; report what IS
   present and don't fabricate the rest.
5. **Summarise the landscape** (report from the tables step 4 already wrote — no `run_code`). Distribution
   **by consequence** and **by predicted impact** (`variant_consequence_distribution.csv` /
   `variant_impact_distribution.csv`), the real PASS vs non-PASS split (never "all PASS" unless
   n_nonpass is 0), and how many variants were NOT in ClinVar (absence of a ClinVar record is NOT evidence
   of benignity — say so).
6. **Prioritise + tier the actionable variants.** Lead with the `high_priority_variants` shortlist: the
   **pathogenic / likely-pathogenic** ClinVar calls PLUS the **rare** (max population AF < 0.1%) variants
   that are high-`impact` (frameshift, stop-gained, splice), predicted deleterious by any predictor (SIFT
   deleterious / PolyPhen damaging / **CADD** ≥20 / **REVEL** >0.5 / **AlphaMissense** >0.564), OR
   **splice-disrupting** (**SpliceAI** `spliceai_max_ds` ≥0.5 — name the event, e.g. donor-loss). For each,
   give the gene, location, rsID, the predictor score(s) that flagged it, and the ClinVar condition; a
   common (high-AF) variant is unlikely causal even if high-impact — note the frequency. Then **TIER**
   them: fetch + adapt the `clinical_variant_prioritization` skill to rank each variant (PATHOGENIC_CLINVAR
   → VUS_FAVOR_PATH → … → BENIGN) from ClinVar + gnomAD AF (disease-model thresholds) + the predictors. It
   is a **research-triage** ordering, NOT a clinical ACMG diagnosis — carry that caveat; never upgrade a
   ClinVar "uncertain" call.
7. **Literature context** (`literature_search`): for the prioritised pathogenic genes/variants, pull real
   citations (gene + disease/phenotype) to ground the interpretation. Cite only what the tool returns.

**(Optional) Cohort variant database** (`run_code`): for MANY samples, adapt the reference template
`build_variant_db_tiledbvcf.py` to ingest the indexed VCFs into a TileDB-VCF dataset for region/sample
queries. Population-scale storage only; skip for a single VCF.

## Grounding

Report ONLY consequences, genes, impacts, and clinical significances that `annotate_variants`
actually returned — never invent a gene, a consequence, an rsID, or a pathogenicity call. ClinVar
significance is reported verbatim (e.g. `pathogenic`, `likely_pathogenic`, `uncertain_significance`);
do not upgrade "uncertain" to "pathogenic". State the genome assembly, the `execution_mode`, and the
number of variants annotated vs skipped. Frame biological/clinical implications as hypotheses
requiring orthogonal validation, not diagnoses. The methods + results report is assembled
automatically — do NOT plan a report-writing step.

## Tools & data used by this path

Ensembl VEP (gene/consequence/impact, MANE + HGVS) + ClinVar (clinical significance) + gnomAD
(population frequency) + in-silico predictors (SIFT, PolyPhen; CADD/AlphaMissense/REVEL + SpliceAI
splice-disruption as their data is staged), run via `bcftools`/`tabix` inside `vep.sif` (SpliceAI via
the OpenSpliceAI conda env, on the post-filter set); `normalize_vcf` / `vcf_qc_stats` /
`clinical_variant_prioritization` skills; `TileDB-VCF` for cohort databases. Full inventory (what each
is, sizes, staging status, env vars): [`docs/vcf_pipeline_tools.md`](../../docs/vcf_pipeline_tools.md).
