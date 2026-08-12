---
name: variant_annotation
description: Interpret a VCF's variants (germline/somatic, WGS/WES) — gene + functional consequence (Ensembl VEP), ClinVar pathogenicity, gnomAD frequency, deleteriousness (CADD/REVEL/AlphaMissense) + splice disruption (SpliceAI), then prioritise. Rare-disease known-gene-first (IRD). Offline, WGS-scale.
tools: annotate_variants, run_code, literature_search
---

# Variant Annotation & Prioritisation Protocol

**Purpose.** Take a VCF of called variants and interpret them — for each variant: the gene it hits,
its functional consequence, its clinical significance (ClinVar), its population rarity (gnomAD), and
its predicted damage (CADD / REVEL / AlphaMissense / SpliceAI) — then rank the actionable ones. Built
for a **rare-disease, known-gene-first** study (e.g. inherited retinal disease), and for **WGS-scale**
VCFs (runs offline against a local cache; every passing variant is annotated, no network cap).

|  |  |
|---|---|
| **Input** | one VCF (`.vcf` / `.vcf.gz`) — germline or somatic, WGS or WES |
| **Output** | `tables/variant_annotation.tsv` (per-variant) + 5 deliverable tables + a prioritised shortlist |
| **Engine** | `bcftools` + Ensembl **VEP `--offline`** + ClinVar + gnomAD + predictor plugins, inside `vep.sif` on an HPC3 CPU node |
| **Not for** | scRNA-seq expression (use the annotation / DE pipelines instead) |

> **How to read the "parameters" in each step.** Two kinds of knob feed the annotation tool, and this
> protocol keeps them separate on purpose:
> - **🔬 Agent-chosen (scientific)** — the PI/Scientist decides these per study and you should audit them:
>   `genes` / `regions_bed` (the panel), `max_pop_af` (rarity cutoff), `pass_only`, `sample`.
> - **🧭 Auto-detected** — `assembly` is read from the VCF header (chr1 contig length → GRCh37 vs GRCh38)
>   and the gateway overrides the configured default per run; the model rarely sets it. **GRCh37/hg19 is
>   the fallback** when the header is silent (most eye/IRD data is GRCh37).
> - **⚙️ Fixed (infra)** — the gateway injects these; the model never sets them, they don't change the
>   science: the VEP cache dir, the ClinVar VCF, the reference FASTA, `--fork` width, the predictor-plugin
>   file paths. Shown once (Step 4) so you can see the method, not re-audited per run.

---

## At a glance — the 7 steps

| # | Step | Tool / skill | 🔬 Key agent-chosen params | Output |
|---|------|--------------|----------------------------|--------|
| 1 | **QC the callset** | `vcf_qc_stats` (run_code) | `SEQ_TYPE` = WGS \| WES | `vcf_qc_summary.csv` |
| 2 | **Normalise** *(internal to Step 4 on the offline path — not a separate agenda step)* | inside `annotate_variants` | — (uses the ref FASTA) | left-aligned input to VEP |
| 3 | **Narrow: panel + drop-common** | `annotate_variants` | `genes` / `regions_bed`, `max_pop_af=0.01` | in-panel rare set |
| 4 | **Annotate** | `annotate_variants` | 🧭 `assembly` auto-detected (GRCh37 fallback) | `variant_annotation.tsv` + 5 tables |
| 5 | **Summarise the landscape** | *(report from Step 4 tables)* | — | consequence / impact dists |
| 6 | **Prioritise + tier** | `clinical_variant_prioritization` (run_code) | disease-model AF thresholds | `high_priority_variants.csv` + tiers |
| 7 | **Literature context** | `literature_search` | gene + phenotype query | citations |

> Plan these as **distinct agenda steps** — a real variant study is not one "annotate" call. The heavy
> compute lives inside `annotate_variants` (Step 4); Steps 5–6 **report from the tables it already wrote**
> (never hand-roll `run_code` to re-derive them). The methods+results report is assembled automatically —
> **do not add a report-writing step.**

### Rare-disease strategy (DEFAULT for a Mendelian / IRD study)

The causal variant is **rare** and usually in a **known disease gene**, so narrow *before* interpreting
(Rui Chen's protocol): **(a)** restrict to the known disease-gene panel (Step 3 `genes`/`regions_bed`),
looking in exons **and** introns; **(b)** drop anything with gnomAD population AF > 1% (`max_pop_af=0.01`)
— too common to cause a rare disease; novel/no-frequency variants are kept. Expand to the whole genome
**only if** the in-panel search is negative — and say so when you do.

---

## Step-by-step

<details open>
<summary><b>Step 1 · QC the callset</b> — is this VCF even trustworthy?</summary>

**What.** Compute Ti/Tv, Het/Hom, SNP/indel/multiallelic counts and call rate, and flag anything
outside the expected WGS/WES band **before** interpreting individual variants.

**Why.** `annotate_variants` reports the PASS/non-PASS split but *nothing* about callset quality. A low
Ti/Tv or an off-range Het/Hom means the downstream consequence/pathogenicity tables describe a noisy
callset (excess false positives, contamination, a sample swap) — the report must carry that caveat
(→ Diagnostics, not the manuscript conclusions).

**🔬 Agent-chosen:** `SEQ_TYPE` = `"WGS"` or `"WES"` (sets the Ti/Tv band).

**What runs** — the skill prefers `bcftools stats`, degrading to `cyvcf2` → a stdlib Ti/Tv scan:
```bash
bcftools stats -s - normalized.vcf.gz     # Ti/Tv (TSTV), per-sample Het/Hom + call rate (PSC)
```
Expected ranges it flags against (from operon's `variant-calling-vcf-statistics`):

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
as `not_in_clinvar`. (Only a REST-path run on a small, indel-heavy, non-normalised VCF might normalise up front.)

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
<summary><b>Step 3 · Narrow — known genes first, drop the common</b> (rare-disease DEFAULT)</summary>

**What.** Restrict to the study's known disease-gene panel and remove common variants, *before* the
expensive annotation — on a WGS VCF the offline line restricts BEFORE VEP, a large compute saving.

**Why.** See the rare-disease strategy above: the causal allele is rare and in a known gene.

**🔬 Agent-chosen:**

| Param | Value | Meaning |
|---|---|---|
| `genes` | e.g. `["ABCA4","RPGR","USH2A",…]` | IRD panel by symbol… |
| `regions_bed` | e.g. `ird_panel.bed` | …or as a BED (restricts before VEP) |
| `max_pop_af` | `0.01` | drop gnomAD AF > 1% (novel variants kept) |

**State the panel and the AF cutoff.** If the in-panel search is negative, say so before expanding
genome-wide. (For a general, non-Mendelian VCF with no candidate-gene prior: skip the panel, annotate
genome-wide.)

**✅ Verify this step:** panel + AF cutoff are stated in the report · the variant count drops as expected
· a negative in-panel result is called out explicitly.
</details>

<details>
<summary><b>Step 4 · Annotate</b> — the core: offline VEP + ClinVar + gnomAD + predictors</summary>

**What.** Run `annotate_variants` on the normalised, panel-restricted VCF. Per variant you get: the
gene, the **MANE-Select** transcript + **HGVS** `c.`/`p.` name, the most-severe consequence, VEP impact,
**SIFT/PolyPhen** and (as staged) **CADD / REVEL / AlphaMissense** + **SpliceAI** splice scores, the max
**gnomAD/1000G** population AF, and the **ClinVar** significance with review-status stars.

**🧭 Auto-detected:** `assembly` is read from the VCF header (chr1 contig length → GRCh37 vs GRCh38) and
the gateway overrides the configured default per run — you rarely set it. **GRCh37/hg19 is the fallback**
(most eye/IRD data is GRCh37). Pass `assembly` explicitly only to correct a mis-detection; STATE the build
used, never mix builds. (The Step-3 panel/AF carry through.)

**⚙️ Fixed infra** (gateway-injected, model never sends): VEP `cache_dir`, `clinvar_vcf`, `ref_fasta`,
`--fork` width, and the CADD/AlphaMissense/REVEL plugin file paths.

> **⚠️ Predictor coverage is GRCh38-only today.** CADD / REVEL / AlphaMissense / SpliceAI are gated to
> `assembly == GRCh38` (their staged data is GRCh38-build). On a **GRCh37 eye dataset they silently do NOT
> run** — you get gene/consequence/ClinVar/gnomAD + SIFT/PolyPhen, but not the deleteriousness/splice
> panel. Closing this needs GRCh37 predictor data staged (or a liftover), not a flag flip.

**What runs** — two commands, exactly as the tool builds them:
```bash
# ① PASS-filter (+ optional panel / sample), streamed by bcftools (never materialised in Python)
bcftools view -f PASS,. [-R panel.bed] [-s SAMPLE] input.vcf.gz -Oz -o filtered.vcf.gz

# ② offline VEP against the local cache — no network, forked, one JSON object per line
vep --offline --cache --dir_cache <cache> --assembly GRCh38 --species homo_sapiens \
    --fork 8 --input_file filtered.vcf.gz --format vcf --json --output_file out.json \
    --no_stats --force_overwrite \
    --symbol --canonical --biotype --sift b --polyphen b --af --af_gnomade --af_gnomadg \
    --hgvs --fasta <ref.fa> \
    --dir_plugins <plugins> \
    --plugin CADD,snv=<cadd.tsv.gz> --plugin AlphaMissense,file=<am.tsv.gz> --plugin REVEL,file=<revel.tsv.gz> \
    --custom file=<clinvar.vcf.gz>,short_name=ClinVar,format=vcf,type=exact,coords=0,fields=CLNSIG%CLNDN%CLNREVSTAT
```
An empty plugin/FASTA set reproduces the baseline SIFT/PolyPhen annotation exactly — each predictor is
**blank unless its data is staged**; report what IS present, never fabricate the rest.

**"Damaging" thresholds** (used to build the shortlist — `variant_annotation._is_damaging`):

| Predictor | Damaging if | Reference |
|---|---|---|
| CADD phred | ≥ 20 | top ~1% deleterious |
| REVEL | > 0.5 | Ioannidis 2016 |
| AlphaMissense | > 0.564 | Cheng 2023 |
| SpliceAI ΔS | ≥ 0.5 | Jaganathan 2019 (high-precision) |

**✅ Verify this step:** `execution_mode == offline_vep` (a `rest` run on a big VCF only sampled the top
variants — must NOT be read as whole-genome coverage) · `--assembly` matches the VCF header build
(hg19→GRCh38 mislabels ~90% of genes) · `n_pass + n_nonpass == total` · a known control (a ClinVar-
pathogenic in the panel) is recovered with the expected `c.`/`p.`.

<sub>Source: `vcf_offline.build_filter_cmd` + `vcf_offline.build_vep_cmd` + `_VEP_ANNOT_FLAGS`</sub>
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
<summary><b>Step 6 · Prioritise + tier the actionable variants</b></summary>

**What.** Lead with the `high_priority_variants` shortlist — the **pathogenic / likely-pathogenic**
ClinVar calls PLUS the **rare** (max pop AF < 0.1%) variants that are high-impact (frameshift,
stop-gained, splice), predicted deleterious by any predictor, OR splice-disrupting (SpliceAI ΔS ≥ 0.5 —
name the event, e.g. donor-loss). For each: gene, location, rsID, the score(s) that flagged it, the
ClinVar condition. Then **TIER** each variant with the `clinical_variant_prioritization` skill:
`PATHOGENIC_CLINVAR → VUS_FAVOR_PATH → … → BENIGN`, from ClinVar + gnomAD AF + the predictors.

**Why / honesty.** This is a **research-triage** ordering, **NOT** a clinical ACMG diagnosis — carry that
caveat. Never upgrade a ClinVar "uncertain" call. A common (high-AF) variant is unlikely causal even if
high-impact — note the frequency.

**🔬 Agent-chosen:** the disease-model AF thresholds inside the skill (adapt to dominant vs recessive).

**✅ Verify this step:** the shortlist leads · each flagged variant names its evidence · the ACMG-is-not-
this caveat is present · no "uncertain"→"pathogenic" upgrades.

<sub>Source: `skills/clinical_variant_prioritization/reference.py`</sub>
</details>

<details>
<summary><b>Step 7 · Literature context</b></summary>

**What.** For the prioritised pathogenic genes/variants, pull real citations (gene + disease/phenotype)
via `literature_search` to ground the interpretation.

**Why / honesty.** Cite **only** what the tool returns — never invent a PMID or a claim.

**✅ Verify this step:** every citation resolves to a returned record · claims are attributed.
</details>

---

## Grounding & honesty (applies to every step)

Report ONLY genes, consequences, impacts, rsIDs and clinical significances that `annotate_variants`
actually returned — never invent one. ClinVar significance is reported verbatim (`pathogenic`,
`likely_pathogenic`, `uncertain_significance`); do not upgrade "uncertain". Always state the **genome
assembly**, the **`execution_mode`**, and the **number of variants annotated vs skipped**. Frame
biological/clinical implications as hypotheses requiring orthogonal validation, not diagnoses.

## Method provenance

Ensembl VEP (offline cache) · ClinVar (dated VCF) · gnomAD exome+genome AF · SIFT/PolyPhen · CADD ·
REVEL · AlphaMissense · SpliceAI (OpenSpliceAI), all inside `vep.sif` on HPC3. Full inventory (versions,
sizes, staging status, env vars): [`docs/vcf_pipeline_tools.md`](../../../docs/vcf_pipeline_tools.md).

<sub>This protocol's command excerpts are pulled from the cited source functions — regenerate to keep them
byte-identical to what runs.</sub>
</content>
</invoke>
