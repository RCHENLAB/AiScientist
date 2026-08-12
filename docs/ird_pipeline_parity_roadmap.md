# IRD Pipeline Parity Roadmap

**Goal.** Bring the AiScientist variant line up to parity with the lab's IRD (inherited retinal
disease) reference pipeline — the ANNOVAR-based, retina-specialized, curated output
(`output_annotated (1).analysis`). Benchmark run: `cb720f958f06` (same input VCF as the lab sample).

**Why.** On that same VCF our generic VEP pipeline annotated correctly (94% gene concordance) but its
prioritized shortlist was clinically off-target — mitochondrial / PRAMEF / lncRNA noise, missing the
RP1L1 / CRB1-class IRD candidates the lab surfaces. The gap is *specialization*, not annotation
correctness. Full assessment: `scratchpad/VCF_Report_Credibility_Assessment.docx`.

**Legend — gap type:** `CODE` = new logic, `DATA` = stage a reference file, `LIC` = licensed/external
data (needs the user), `WIRE` = already coded, just enable/pass params.

---

## The 11 layers (lab does → we have → gap)

| # | IRD layer | Lab evidence (columns) | Our current state | Gap | Phase |
|---|-----------|------------------------|-------------------|-----|-------|
| 1 | **Known-gene / panel first** | IRD genes only (RP1L1, CRB1…) | `annotate_variants` accepts `genes` / `regions_bed`; **no default IRD panel exists** | DATA+WIRE | 1 |
| 2 | **Disease-model AF** | ≤1e-4 dominant / ≤5e-3 comp-het / X rules | single flat `max_pop_af` only | CODE | 2 |
| 3 | **Protein-altering focus** | 360 protein-altering + stopgain/frameshift | consequence/impact present; not a primary filter tier | CODE | 2 |
| 4 | **Known-mutation match (HGMD)** | `HGMD_mutations_nearby`, `HGMD_match` | ClinVar only | LIC (or ClinVar+LOVD substitute) | 3 |
| 5 | **Retina-specific exons** | `retina-specific_exon` (Eric Pierce RNA-seq) | none | LIC/DATA (need the BED from the lab) | 3 |
| 6 | **Splice prediction** | `ada_score`, `rf_score` (dbscSNV) | SpliceAI full pipeline built, **gated OFF** (`BIOAGENT_SPLICEAI`); dbscSNV not wired | WIRE (SpliceAI) + DATA (dbscSNV) | 1/3 |
| 7 | **Deep predictor panel** | 15+ (REVEL, CADD, MetaSVM/LR, VEST3, PROVEAN, conservation) | VEP `--plugin` for CADD/REVEL/AlphaMissense **coded**; data not staged → columns empty. SIFT/PolyPhen only | DATA+WIRE | 1 |
| 8 | **Gene-level constraint** | pLI, pRec, RVIS, GDI, GHIS, P(HI/rec), dom/rec RF-SVM | none | CODE+DATA | 2 |
| 9 | **Phenotype integration** | Exomiser (ExGeneSPheno…), MGI mouse, ZFIN zebrafish | none; no HPO input path | CODE+DATA+UX (patient HPO) | 3 |
| 10 | **Compound-het phasing** | `GATK_Phase_Annotation` (cis/trans) | none | CODE | 2 |
| 11 | **Regulatory accessibility** | `MacATAC` / `PercATAC` (retina ATAC peaks) | none | LIC/DATA (need retina ATAC BED) | 3 |

---

## Phased execution plan

### Phase 0 — Deploy the already-fixed regressions (prerequisite)
The benchmark run was also degraded by two config bugs already fixed in code this session but **not yet
deployed**. Deploy first so parity work runs on a healthy base.
- `1588a3a` — `.env` inline-comment fix → vLLM runs the configured **128K** (not 32K); `RUN_CODE_ON_HPC`
  parses **true** → `run_code` (incl. QC) runs on HPC3 with `bcftools`/`cyvcf2`. Fixes QC layer (D1).
- Action: `sync_deploy.sh` + restart + dedupe the `.env` `VLLM_MAX_MODEL_LEN` key. **Owner: Yijun.**

### Phase 1 — Turn on what's already built (no new logic) — **highest leverage, lowest risk**
Fixes the biggest defects (off-target shortlist, empty predictors) mostly by staging data + passing params.
1. **IRD gene panel** (layer 1). Create a default IRD panel gene list (public **RetNet**, ~280 genes) as
   a repo asset + a panel BED; make the variant preset/gateway pass it as `genes`/`regions_bed` for an
   IRD study (known-gene-first; expand genome-wide only if negative). *CODE(small)+DATA(public).*
2. **Stage the deep predictors** (layer 7). Stage **CADD**, **REVEL**, **AlphaMissense** on HPC3 and set
   `BIOAGENT_VEP_CADD_SNV/INDELS`, `BIOAGENT_VEP_REVEL`, `BIOAGENT_VEP_ALPHAMISSENSE`. The VEP `--plugin`
   wiring already exists → columns populate. *DATA (CADD/AlphaMissense already staging per project memory;
   REVEL to fetch).*
3. **Enable SpliceAI** (layer 6). `BIOAGENT_SPLICEAI=1` (OpenSpliceAI already installed + verified).
   *WIRE.*
4. Re-run the benchmark VCF; confirm the shortlist now leads with panel IRD genes + populated predictors.

### Phase 2 — Disease-model prioritization logic (code)
5. **Disease-model AF tiers** (layer 2): dominant ≤1e-4 / recessive comp-het ≤5e-3 (≥2 rare variants per
   gene) / X-linked rules, replacing the single flat cutoff. *CODE.*
6. **Gene-level constraint** (layer 8): stage gnomAD constraint (pLI/pRec), RVIS, GDI; annotate + fold
   into `clinical_variant_prioritization` tiering. *CODE + DATA(public).*
7. **Compound-het phasing** (layer 10): cis/trans from genotypes (read-backed or trio when available).
   *CODE.*
8. **Protein-altering tier** (layer 3): promote protein-altering/LoF as an explicit tier in the shortlist
   logic. *CODE (folds into 5–7).*

### Phase 3 — External-data / licensed layers (BLOCKED on data the user must supply)
9. **Retina-specific exons** (layer 5): need the Eric Pierce retina RNA-seq exon **BED** from the lab.
10. **Regulatory ATAC** (layer 11): need the retina **ATAC-seq peak BED** from the lab.
11. **HGMD** (layer 4): licensed (HGMD Professional). Decide: obtain license, or substitute ClinVar+LOVD.
12. **Phenotype / Exomiser** (layer 9): stage the Exomiser data bundle **and** add a patient-**HPO**-terms
    input path (UX). Then wire Exomiser gene-phenotype scoring (+ optional MGI/ZFIN).

---

## Located reference data (2026-07-12 — the lab's whole IRD pipeline is already on HPC3)

The pipeline that produced the reference sample is `/dfs3b/ruic20_lab/chen/pipeline_restructure/
pipeline_restructure/` (owner "chen"; JShao has a copy). Its reference data is staged and reusable —
so **Phase 3 is almost entirely UNblocked** (data present, no license/download needed):

| Layer | File on HPC3 | Size |
|-------|--------------|------|
| 1 IRD panel | `chenlab/Data/WGS_data/retNet_all_gene_clean.csv` (258 genes) → copied into the repo as `src/bioagent/tools/gene_panels/ird_retnet.txt` | — |
| 4 HGMD | `chen/Pipeline/monkey_analysis/HGMD_2014_hg19_hg38…` (+ HGMD_missense/splicing_2016) | — |
| 5 Retina-specific exons | `…/bin/annotate_filter/Eric_Pierce_human_RNA-seq_derived_retina-specific_exons` (BED: chrom start end [gene]) | 310K |
| 6 Splice | `…/reference/SpliceAI` (+ our own OpenSpliceAI) | 34G |
| 7 Predictors | `…/reference/CADD.1.4` (96G), `…/reference/revel` (2.3G), `…/bin/dbNSFP3.5a` (131G, 15+ predictors) | — |
| 9 Phenotype | `…/bin/exomiser` + `1run_exomiser_pipeline.sh` + a template `.exomiser.yml` | — |
| 11 Retina ATAC | `…/reference/ATACseq/92526MacATAC_92556MacATAC.macs._peaks.narrowPeak.gz` (matches the sample's MacATAC column) | 20M |

## Blockers (all data cleared — only a product decision remains)
- [x] ~~Retina-specific exon BED~~ — on HPC3 + authorized.
- [x] ~~Retina ATAC-seq peak BED~~ — on HPC3 + authorized.
- [x] ~~HGMD~~ — public version on HPC3; Rui authorized reuse (no license blocker).
- [x] ~~REVEL / CADD / dbNSFP~~ — all staged on HPC3 + authorized.
- [ ] **Patient HPO phenotype input** — the ONE open item: decide how phenotype terms enter a run
      (per-study field?) so Exomiser (layer 9) is meaningful. Not a file — a UX/input decision.

## Decisions LOCKED (2026-07-12)
- **Permission GRANTED** — Rui Chen (PI) authorized reuse of the lab pipeline's reference data; the
  **HGMD there is a public version** (no license blocker). Read-only, with acknowledgment.
- **Strategy = A, sharpened:** REPRODUCE the lab's annotate/filter/prioritize LOGIC in OUR scripts
  (do NOT wrap/run their ANNOVAR/Perl monolith), implement **only the necessary parts** (skip
  align/call/CNV/SV/trio — we start from a VCF), and **read their staged reference data for now**
  (their data is on the same HPC3 filesystem our VEP job runs on; VEP plugins consume their
  CADD/REVEL/dbNSFP directly). The extracted spec is `docs/ird_filter_spec.md`.
- **B = the oracle, not the product:** run the lab's VCF-input tail (`annotate_filter/
  pipeline_filter_and_annotate.py`) on the same VCF to DIFF against our port (proves parity), and
  read it as the SPEC. Not shipped; too fragile (Python2, home-dir, IRD-only, BAM-oriented monolith).
- **Parity target:** clinical-grade (same candidates surface), not byte-identical — engines differ
  (our VEP/Ensembl vs their ANNOVAR/refGene); ~94% gene concordance already measured.

## Status
- Phase 0: fixes coded (`1588a3a`), **awaiting deploy**.
- Phase 1: **IN PROGRESS** on branch `feat/ird-parity` —
  - [x] IRD RetNet panel imported as a repo asset (`src/bioagent/tools/gene_panels/`, loader + tests).
  - [x] lab filter/prioritize LOGIC extracted to `docs/ird_filter_spec.md`.
  - [x] **panel WIRED (POST-VEP)**: `BIOAGENT_DEFAULT_GENE_PANEL` → gateway injects it as the default
        `genes` (deterministic known-gene-first; caller overrides). NOTE: `genes` filters AFTER VEP, so
        it scopes the RESULT but does NOT cut annotation time.
  - [x] **pre-VEP region restriction (the annotation-time saving)**: `BIOAGENT_DEFAULT_REGIONS_BED` →
        gateway injects `regions_bed` + binds the BED into vep.sif → bcftools restricts the VCF BEFORE
        VEP, so VEP annotates only panel variants (~45-60 min → minutes on a WGS VCF). This is why the
        earlier `genes`-only wiring did not speed cb720 up. DEPLOY: point it at the assembly-matched IRD
        capture BED — the lab's `reference/design/retcap_v5_final_1_Covered.bed` (5,723 regions, ~1.7 Mb
        ≈ 0.05% of the genome) — with its UCSC `browser`/`track` header lines stripped:
        `grep -vE '^(browser|track)' retcap_v5_final_1_Covered.bed > retcap_v5.clean.bed`. (Optional
        lever 2: a pre-VEP AF≥1% drop via bcftools + the lab's staged gnomAD sites — only matters for a
        genome-wide run; with the retcap BED there is little left, so it is not required for IRD.)
  - [x] **default rarity floor**: `BIOAGENT_DEFAULT_MAX_POP_AF` (e.g. 0.005) injected as `max_pop_af`
        (caller overrides).
  - [ ] predictors: **do NOT wire `BIOAGENT_VEP_*` at the lab's copies** — his CADD/dbNSFP are
        ANNOVAR-format/GRCh37, not VEP-plugin-compatible (see spec "Located reference files"). Stage the
        public VEP-format CADD/REVEL/dbNSFP (`deploy/vep/stage_annotation_dbs.sh`) + `BIOAGENT_VEP_PLUGINS=1`.
        DEPLOY action; code ready.
  - [ ] enable SpliceAI (GRCh38-only today; GRCh37 studies fall back to dbNSFP ada/rf once wired).
- Phase 2: **core STARTED** on `feat/ird-parity` —
  - [x] disease-model gene-level tiering (`tools/ird_prioritize.py`: dominant ≤1e-4 / recessive ≥2
        ≤5e-3 / X) + WIRED into `summarize_annotations` — the high-priority shortlist now LEADS with
        model-fitting candidates and carries a `Disease_Model` column; mito/off-target sinks. (694 green.)
  - [x] annotation LAYERS **built + wired GATED-OFF** (`tools/ird_annotate.py`, tested): HGMD
        15bp/MATCH, retina-specific-exon + ATAC interval overlap, dbscSNV ada/rf, + the
        `reason_for_inclusion` cascade (spec §2). Runs inside the VEP job on the reduced set via tabix
        (injectable; a miss is non-fatal). Enable with `BIOAGENT_IRD_ANNOTATE=1`; HGMD/dbscSNV default
        to the located lab files. **DEPLOY prep:** the retina-exon BED + ATAC narrowPeak need
        bgzip+tabix indexing first (set `BIOAGENT_IRD_RETINA_EXONS`/`_ATAC` after); dbscSNV files need a
        `.tbi` too (else that layer silently skips). **Needs an HPC3 run to verify tabix regions/binds.**
  - [ ] feed `reason_for_inclusion` into the shortlist/report; gene-constraint (pLI/RVIS/GDI); add
        the IRD fields as columns on the per-variant table.
- Phase 3: data authorized + located; only patient-HPO input open (Exomiser). Retina-exon/ATAC files
  located but need bgzip+tabix indexing (deploy prep).

*Living doc — update the Status + check the blockers as phases land. Related: `docs/vcf_pipeline_tools.md`,
the `variant_annotation` preset, and the `clinical_variant_prioritization` skill.*
