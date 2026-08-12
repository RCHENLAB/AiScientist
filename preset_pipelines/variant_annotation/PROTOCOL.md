---
name: variant_annotation
description: Interpret a VCF's variants (germline/somatic, WGS/WES) — gene + functional consequence (Ensembl VEP), ClinVar pathogenicity, gnomAD frequency, deleteriousness (CADD/REVEL/AlphaMissense) + splice disruption (SpliceAI), then prioritise. Rare-disease known-gene-first (IRD). Offline, WGS-scale.
tools: annotate_variants, run_code, literature_search
---

# Variant Annotation & Prioritisation Protocol

**Purpose.** Take a VCF of called variants and interpret them — for each variant: the gene it hits,
its functional consequence, its clinical significance (ClinVar), its population rarity (gnomAD), and
its predicted damage (CADD / REVEL / AlphaMissense / SpliceAI) — then rank the actionable ones. Built
for a **rare-disease, known-gene-first** study (inherited retinal disease, IRD), and for **WGS-scale**
VCFs: it runs offline against a local cache, restricts to the disease panel **before** annotation, and
tiers the survivors by **inheritance model** — the lab's IRD protocol, reproduced in our code.

|  |  |
|---|---|
| **Input** | one VCF (`.vcf` / `.vcf.gz`) — germline or somatic, WGS or WES |
| **Output** | `tables/variant_annotation.tsv` (per-variant) + 5 deliverable tables + a disease-model-tiered shortlist |
| **Engine** | `bcftools` + Ensembl **VEP `--offline`** + ClinVar + gnomAD + predictor plugins + IRD annotation layers, inside `vep.sif` on an HPC3 CPU node |
| **Not for** | scRNA-seq expression (use the annotation / DE pipelines instead) |

> **✅ Verified end-to-end on HPC3 (2026-07-13, same benchmark VCF).** Panel-before-VEP cut whole-VCF
> annotation from **~52 min → 99 s** (VEP sees 1,544 in-panel variants, not 4.67 M). The shortlist led
> with **real retinal-disease genes — CRB1, TRPM1, CROCC** (the mitochondrial / PRAMEF noise of the
> generic run was gone); ClinVar P/LP = **CRB1 + USH2A**; standout = a **compound-heterozygous CRB1**
> candidate (a ClinVar-pathogenic allele + a novel loss-of-function allele in the same patient). This is
> the lab-style IRD output the earlier generic run could not produce.

> **How to read the "parameters" in each step.** Three kinds of knob feed the annotation tool, kept
> separate on purpose so a researcher knows what to audit:
> - **🔬 Agent-chosen (scientific)** — decided per study; **audit these**: `genes` / `regions_bed` (the
>   panel), `max_pop_af` (rarity cutoff), `pass_only`, `sample`, and the disease-model AF thresholds.
> - **🧭 Auto-detected** — `assembly` is read from the VCF header (chr1 contig length → GRCh37 vs GRCh38);
>   the gateway sets it per run. **GRCh37/hg19 is the fallback** when the header is silent (most eye/IRD
>   data is GRCh37).
> - **⚙️ Fixed (infra)** — gateway-injected, never model-set, no effect on the science: the VEP cache dir,
>   the ClinVar VCF, the reference FASTA, `--fork` width, the predictor-plugin paths, and the IRD
>   reference files (HGMD / retina-exon / ATAC). Shown once so you can see the method, not re-audited.

---

## At a glance — the pipeline

| # | Step | Tool / skill | 🔬 Key agent-chosen params | Output |
|---|------|--------------|----------------------------|--------|
| 1 | **QC the callset** | `vcf_qc_stats` (run_code) | `SEQ_TYPE` = WGS \| WES | `vcf_qc_summary.csv` |
| 2 | **Normalise** *(internal to Step 4 — not a separate agenda step)* | inside `annotate_variants` | — (uses the ref FASTA) | left-aligned input to VEP |
| 3 | **Narrow: panel (before VEP) + drop-common** | `annotate_variants` | `regions_bed` / `genes`, `max_pop_af` | in-panel rare set |
| 4 | **Annotate** (VEP + ClinVar + gnomAD + predictors + **IRD layers**) | `annotate_variants` | 🧭 `assembly` auto-detected (GRCh37 fallback) | `variant_annotation.tsv` + 5 tables |
| 5 | **Summarise the landscape** | *(report from Step 4 tables)* | — | consequence / impact dists |
| 6 | **Prioritise + tier by disease model** | `annotate_variants` (built-in) + `clinical_variant_prioritization` | dominant/recessive/X AF thresholds | `high_priority_variants.csv` + `Disease_Model` |
| 7 | **Literature context** | `literature_search` | gene + phenotype query | citations |

> Plan these as **distinct agenda steps** — a real variant study is not one "annotate" call. The heavy
> compute lives inside `annotate_variants` (Step 4); Steps 5–6 **report from the tables it already wrote**
> (never hand-roll `run_code` to re-derive them). The methods+results report is assembled automatically —
> **do not add a report-writing step.**

### Rare-disease / IRD strategy (DEFAULT for a Mendelian study)

The causal variant is **rare** and usually in a **known disease gene**, so narrow *before* interpreting
(Rui Chen's protocol): **(a)** restrict to the known disease-gene panel — pass a **panel BED** as
`regions_bed` so the offline line restricts **before VEP** (the compute saving above), looking in exons
**and** introns; **(b)** drop anything with gnomAD population AF above the rarity floor (`max_pop_af` —
the lab's IRD base cutoff is `0.005`) — too common to cause a rare disease; novel/no-frequency variants
are kept. Then **tier the survivors by inheritance model** (Step 6). Expand to the whole genome **only
if** the in-panel search is negative — and say so when you do.

---

## Step-by-step

<details open>
<summary><b>Step 1 · QC the callset</b> — is this VCF even trustworthy?</summary>

**What.** Compute Ti/Tv, Het/Hom, SNP/indel/multiallelic counts and call rate, and flag anything
outside the expected WGS/WES band **before** interpreting individual variants.

**Why.** `annotate_variants` reports the PASS/non-PASS split but *nothing* about callset quality. A low
Ti/Tv or an off-range Het/Hom means the downstream tables describe a noisy callset (excess false
positives, contamination, a sample swap) — the report must carry that caveat (→ Diagnostics, not the
manuscript conclusions).

**🔬 Agent-chosen:** `SEQ_TYPE` = `"WGS"` or `"WES"` (sets the Ti/Tv band).

**What runs** — the skill prefers `bcftools stats`, degrading to `cyvcf2` → a stdlib Ti/Tv scan:
```bash
bcftools stats -s - normalized.vcf.gz     # Ti/Tv (TSTV), per-sample Het/Hom + call rate (PSC)
```
Expected ranges it flags against:

| Metric | WGS | WES | Flag if |
|---|---|---|---|
| Ti/Tv | ~2.0 | ~3.0 | WGS <1.8 or >2.5 · WES <2.5 or >3.5 |
| Het/Hom | 1.5–2.0 | 1.5–2.0 | outside band |
| Call rate | >95% | >95% | <90% |

**✅ Verify this step:** Ti/Tv reported up front · any `qc_flags` surfaced · if flagged, downstream tables
are framed as lower-confidence (not silently trusted).

<sub>Source: `skills/vcf_qc_stats/reference.py`</sub>
</details>

<details>
<summary><b>Step 2 · Normalise the VCF</b> — <i>done INSIDE Step 4 on the offline path; not a separate step</i></summary>

> **Not a separate agenda step (offline path).** `annotate_variants` runs `bcftools norm` itself inside
> `vep.sif` before VEP, so you do **not** plan a standalone normalise step. It also can't run as its own
> `run_code` step — that sandbox (`analysis.sif`) has no bcftools and no reference-FASTA bind. Shown here
> for auditability: this is the normalisation the tool applies for you.

**What.** `bcftools norm`: split multiallelic sites → left-align indels against the reference FASTA.

**Why.** A non-left-aligned indel **silently fails** to match ClinVar/gnomAD and gets falsely reported
as `not_in_clinvar`.

**🔬 Agent-chosen:** none (the reference FASTA is fixed infra).

**What runs:**
```bash
bcftools norm -m-any -f <ref.fa> -Oz -o normalized.vcf.gz input.vcf.gz
#             │        └ left-align anchor (reference)
#             └ split multiallelic records into biallelic rows
```

**✅ Verify this step:** the tool's log shows `bcftools norm` ran before VEP · no multiallelic sites remain
in the annotated table · record count ≥ input (splits add rows).

<sub>Source: `vcf_offline.build_norm_cmd`</sub>
</details>

<details>
<summary><b>Step 3 · Narrow — known genes first, drop the common</b> (rare-disease DEFAULT, and the speed fix)</summary>

**What.** Restrict to the study's known disease-gene panel and remove common variants, *before* the
expensive annotation. On a WGS VCF the offline line restricts with `bcftools -R panel.bed` **before VEP**
— so VEP annotates only in-panel variants. This is the difference between a ~52-minute whole-genome run
and a ~100-second panel run.

**Why.** See the rare-disease strategy above: the causal allele is rare and in a known gene. Restricting
first also removes off-target noise (mitochondrial, PRAMEF, lncRNA) that otherwise floods the shortlist.

**🔬 Agent-chosen:**

| Param | Value | Meaning |
|---|---|---|
| `regions_bed` | e.g. `retcap.bed` (IRD capture panel) | panel as a BED — **restricts BEFORE VEP** (the compute saving) |
| `genes` | e.g. `["ABCA4","RPGR","USH2A",…]` | …or by symbol (filters AFTER VEP — no time saving) |
| `max_pop_af` | `0.005` (lab IRD base) | drop gnomAD AF above this (novel variants kept) |

**State the panel and the AF cutoff.** If the in-panel search is negative, say so before expanding
genome-wide. (For a general, non-Mendelian VCF with no candidate-gene prior: skip the panel, annotate
genome-wide.) The default panel + rarity floor can be set once as deploy defaults
(`BIOAGENT_DEFAULT_REGIONS_BED` / `BIOAGENT_DEFAULT_MAX_POP_AF`), and any caller-supplied value overrides them.

**✅ Verify this step:** panel + AF cutoff are stated in the report · the variant count drops as expected
(e.g. 4.67 M → 1,544 in-panel → 54 rare) · a negative in-panel result is called out explicitly.

<sub>Source: `vcf_offline.build_filter_cmd` (`-R`) · `variant_annotation.apply_variant_filters` (`max_pop_af`)</sub>
</details>

<details>
<summary><b>Step 4 · Annotate</b> — offline VEP + ClinVar + gnomAD + predictors + IRD layers</summary>

**What.** Run `annotate_variants` on the normalised, panel-restricted VCF. Per variant you get: the
gene, the **MANE-Select** transcript + **HGVS** `c.`/`p.` name, the most-severe consequence, VEP impact,
**SIFT/PolyPhen** and (as staged) **CADD / REVEL / AlphaMissense** + **SpliceAI** splice scores, the max
**gnomAD/1000G** population AF, and the **ClinVar** significance with review-status stars. On the IRD
path it then adds the **IRD annotation layers** (below).

**🧭 Auto-detected:** `assembly` is read from the VCF header (chr1 contig length → GRCh37 vs GRCh38); the
gateway sets it per run. **GRCh37/hg19 is the fallback** (most eye/IRD data is GRCh37). Pass `assembly`
explicitly only to correct a mis-detection; STATE the build used, never mix builds.

**⚙️ Fixed infra** (gateway-injected, model never sends): VEP `cache_dir`, `clinvar_vcf`, `ref_fasta`,
`--fork` width, the CADD/AlphaMissense/REVEL plugin paths, and the IRD reference files.

**What runs** — two commands, exactly as the tool builds them:
```bash
# ① PASS-filter (+ panel / sample), streamed by bcftools (never materialised in Python)
bcftools view -f PASS,. [-R panel.bed] [-s SAMPLE] input.vcf.gz -Oz -o filtered.vcf.gz

# ② offline VEP against the local cache — no network, forked, one JSON object per line
vep --offline --cache --dir_cache <cache> --assembly GRCh37 --species homo_sapiens \
    --fork 8 --input_file filtered.vcf.gz --format vcf --json --output_file out.json \
    --no_stats --force_overwrite \
    --symbol --canonical --biotype --sift b --polyphen b --af --af_gnomade --af_gnomadg \
    --hgvs --fasta <ref.fa> --dir_plugins <plugins> \
    --plugin CADD,snv=<cadd.tsv.gz> --plugin AlphaMissense,file=<am.tsv.gz> --plugin REVEL,file=<revel.tsv.gz> \
    --custom file=<clinvar.vcf.gz>,short_name=ClinVar,format=vcf,type=exact,coords=0,fields=CLNSIG%CLNDN%CLNREVSTAT
```
Each predictor is **blank unless its data is staged**; report what IS present, never fabricate the rest.

**IRD annotation layers** (opt-in via `ird_annotate`; tabix lookups against the lab's staged reference
files — Rui Chen authorised reuse; the HGMD there is a public version):

| Layer | What it adds | Reference |
|---|---|---|
| **HGMD proximity** | is the variant at/near a known disease-mutation locus (±15 bp, exact MATCH flagged) | lab public HGMD |
| **Retina-specific exon** | does it fall in a retina-expressed exon | retina exon BED |
| **Retina ATAC** | does it fall in a retina open-chromatin peak (regulatory) | retina ATAC BED |
| **dbscSNV (ada/rf)** | splice-altering score for near-splice variants | dbscSNV |

**Damaging thresholds** (used to build the shortlist — `variant_annotation._is_damaging`):

| Predictor | Damaging if | Reference |
|---|---|---|
| CADD phred | ≥ 20 | top ~1% deleterious |
| REVEL | > 0.5 | Ioannidis 2016 |
| AlphaMissense | > 0.564 | Cheng 2023 |
| SpliceAI ΔS / dbscSNV | ≥ 0.5 / ≥ 0.6 | Jaganathan 2019 / Jian 2014 |

> **⚠️ Predictor coverage is PER-BUILD (fixed 2026-07-17).** Each predictor is resolved for the run's
> own assembly, and one that is not staged for that build is **skipped — never substituted** with the
> other build's file (a tabix lookup is by coordinate, so a cross-build hit returns a *different
> variant's* score — worse than a blank column).
>
> | Predictor | GRCh38 | GRCh37 |
> |---|---|---|
> | **CADD** | ✅ staged | ✅ **staged** (80 GB, `whole_genome_SNVs.grch37.tsv.gz`) |
> | **REVEL** | ✅ staged | ✅ **staged 2026-07-27** (`new_tabbed_revel_grch37.tsv.gz`, rebuilt from the raw release, tabix-indexed on `hg19_pos`) |
> | AlphaMissense | ✅ staged | ❌ — upstream publishes an hg19 file; needs staging |
> | **reference FASTA** (norm + HGVS + SpliceAI) | ✅ staged | ✅ **staged 2026-07-27** (`ref/hg19.fa`) |
> | **SpliceAI** | ✅ | ✅ — unblocked by the FASTA (annotation `grch37` was already wired) |
>
> **The GRCh37 reference is UCSC hg19, NOT Ensembl GRCh37 — this distinction is load-bearing.** The lab's
> GATK VCFs are chr-prefixed with a **16571 bp chrM** (hg19); Ensembl GRCh37 uses bare `1` and a 16569 bp
> chrM. Mixing them makes `bcftools norm -f` fail to match contigs. The staged `ref/hg19.fa` was verified
> against a real lab WGS VCF header — **93/93 contigs present**, `chr1=249250621`, `chrM=16571`.
>
> **One file gates three features** — `bcftools norm`, VEP `--hgvs`, and SpliceAI all check `ref_fasta`,
> so before it was staged a GRCh37 run silently got none of them. The norm gap was the dangerous one: a
> non-left-aligned indel fails to match ClinVar and is reported `not_in_clinvar` — a silent false
> negative on clinical data.
>
> *History: until 2026-07-17 the gateway gated the whole predictor block on `assembly == GRCh38`, so
> GRCh37 runs got no predictors at all even though CADD GRCh37 was already staged — a flag nobody
> flipped, not missing data. SpliceAI kept that assembly check until 2026-07-27; it is now gated on the
> reference FASTA it actually requires.* Set `BIOAGENT_VEP_ALPHAMISSENSE_GRCH37` once that is staged and
> it lights up with no code change.

**✅ Verify this step:** `execution_mode == offline_vep` (a `rest` run on a big VCF only sampled the top
variants — must NOT be read as whole-genome coverage) · `--assembly` matches the VCF header build
(hg19→GRCh38 mislabels ~90% of genes) · `n_pass + n_nonpass == total` · a known control (a ClinVar-
pathogenic in the panel) is recovered with the expected `c.`/`p.`.

<sub>Source: `vcf_offline.build_filter_cmd` + `vcf_offline.build_vep_cmd` + `vcf_offline.run_offline_annotation` (IRD block) + `ird_annotate.annotate_ird_layers`</sub>
</details>

<details>
<summary><b>Step 5 · Summarise the landscape</b> — report from the Step-4 tables (no run_code)</summary>

**What.** Distribution **by consequence** and **by predicted impact**
(`variant_consequence_distribution.csv` / `variant_impact_distribution.csv`), the real PASS vs non-PASS
split, and how many variants were **not** in ClinVar.

**Why / honesty.** Never write "all PASS" unless `n_nonpass == 0`. Absence of a ClinVar record is **not**
evidence of benignity — say so.

**✅ Verify this step:** the numbers cited match the CSVs · the PASS split is the real `n_pass`/`n_nonpass`
· "not in ClinVar" is framed as unknown, not benign.
</details>

<details>
<summary><b>Step 6 · Prioritise + tier by disease model</b></summary>

**What.** Lead with the `high_priority_variants` shortlist — the **pathogenic / likely-pathogenic**
ClinVar calls PLUS the **rare** (max pop AF < 0.1%) variants that are high-impact (frameshift,
stop-gained, splice), predicted deleterious by any predictor, OR splice-disrupting. For each: gene,
location, rsID, the score(s) that flagged it, the ClinVar condition. Then **TIER** each by **inheritance
model**, which `annotate_variants` now assigns per gene deterministically (a `Disease_Model` column):

| Model | Rule | Effect on ranking |
|---|---|---|
| **Dominant** | ≥ 1 allele with AF ≤ 1e-4 | fits → ranked up |
| **Recessive (comp-het)** | ≥ 2 alleles each AF ≤ 5e-3 in the same gene | fits → ranked up; flags candidate compound-hets |
| **X-linked** | on chrX under the above | fits → ranked up |

The `reason_for_inclusion` cascade records *why* each variant survived: `HGMD_match → too-common-drop →
splice ≥ 0.6 → protein-altering`. For finer ACMG-style ordering, adapt the `clinical_variant_prioritization`
skill (`PATHOGENIC_CLINVAR → VUS_FAVOR_PATH → … → BENIGN`).

**Why / honesty.** This is a **research-triage** ordering, **NOT** a clinical ACMG diagnosis — carry that
caveat. Never upgrade a ClinVar "uncertain" call. A compound-het candidate needs **phasing** (confirm the
two alleles are in *trans*) before any interpretation. A common (high-AF) variant is unlikely causal even
if high-impact — note the frequency.

**🔬 Agent-chosen:** the disease-model AF thresholds (dominant vs recessive) — adapt to the study.

**✅ Verify this step:** the shortlist leads · each flagged variant names its evidence + its `Disease_Model`
· the ACMG-is-not-this caveat is present · compound-het candidates are marked "needs phasing" · no
"uncertain"→"pathogenic" upgrades.

<sub>Source: `ird_prioritize.annotate_disease_model` + `ird_annotate.inclusion_reason` + `skills/clinical_variant_prioritization/reference.py`</sub>
</details>

<details>
<summary><b>Step 7 · Literature context</b></summary>

**What.** For the prioritised pathogenic genes/variants, pull real citations (gene + disease/phenotype)
via `literature_search` to ground the interpretation.

**Why / honesty.** Cite **only** what the tool returns — never invent a PMID or a claim.

**✅ Verify this step:** every citation resolves to a returned record · claims are attributed.
</details>

> **Phenotype-driven ranking (built, wiring in progress).** For the phenotype→gene connection the pipeline
> uses **HPO** terms (inferred from the referral upstream, no forced human-in-the-loop) to drive
> **Exomiser** — a structured gene-phenotype knowledge base, not the LLM's own knowledge. Exomiser is
> installed on HPC3 and the HPO inference is built; it adds a phenotype-match score to the shortlist.

---

## Grounding & honesty (applies to every step)

Report ONLY genes, consequences, impacts, rsIDs and clinical significances that `annotate_variants`
actually returned — never invent one. ClinVar significance is reported verbatim (`pathogenic`,
`likely_pathogenic`, `uncertain_significance`); do not upgrade "uncertain". Always state the **genome
assembly**, the **`execution_mode`**, and the **number of variants annotated vs skipped**. Frame
biological/clinical implications as hypotheses requiring orthogonal validation (phasing, segregation,
functional work), not diagnoses.

## Method provenance

Ensembl VEP (offline cache) · ClinVar (dated VCF) · gnomAD exome+genome AF · SIFT/PolyPhen · CADD ·
REVEL · AlphaMissense · SpliceAI (OpenSpliceAI) · IRD layers (public HGMD, retina-specific exons, retina
ATAC, dbscSNV) · disease-model tiering — all inside `vep.sif` on HPC3. Full inventory (versions, sizes,
staging status, env vars): [`docs/vcf_pipeline_tools.md`](../../docs/vcf_pipeline_tools.md) and the IRD
parity plan [`docs/ird_pipeline_parity_roadmap.md`](../../docs/ird_pipeline_parity_roadmap.md).

<sub>This protocol's command excerpts are pulled from the cited source functions — regenerate to keep them
faithful to what runs.</sub>
