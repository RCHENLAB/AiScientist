---
name: phenotype_variant_diagnosis
description: Diagnose a rare-disease CASE — a VCF **plus** the patient's clinical description (free text, any language) — by reconciling two independent tracks: the prioritised variant shortlist (VEP/ClinVar/predictors) and a phenotype-driven per-disease differential (HPO → LIRICAL post-test probability). Use ONLY when the case's phenotype/symptoms/diagnosis are actually given; a VCF alone is the variant_annotation protocol.
tools: annotate_variants, map_phenotype_to_hpo, run_lirical, literature_search, run_code
data_type: variants
---

# Phenotype + Variant Case-Diagnosis Protocol

**Purpose.** Answer *which disease does this patient have, and which variant explains it* — a question
neither half can answer alone. A variant shortlist cannot say *which disease* (a rare-disease VCF yields
dozens of plausible alleles); a phenotype cannot say *which variant* (inherited retinal disease symptoms
overlap heavily across genes). This protocol runs the two tracks **independently** and reconciles them,
so the answer is supported by genotype **and** phenotype rather than by either one's guess.

|  |  |
|---|---|
| **Input** | one VCF (`.vcf` / `.vcf.gz`) **+** the patient's clinical description (free text, any language) |
| **Output** | a per-disease differential with post-test probabilities, reconciled against the prioritised variant shortlist |
| **Engine** | offline VEP in `vep.sif` (Track A) · in-process HPO mapping on the gateway · **LIRICAL v2.4.1** + Exomiser in `lirical.sif` on an HPC3 CPU node (Track B) |
| **Not for** | a VCF with **no** phenotype → use the `variant_annotation` protocol; a phenotype with no VCF still works (LIRICAL runs phenotype-only) |

> **✅ Verified end-to-end on HPC3.** Demo case `CASE_A` (synthetic note written to the published
> phenotype of IMPG2 vitelliform macular dystrophy, OMIM:616152) + its paired VCF. The free-text note
> mapped to **4 observed + 12 excluded** HPO terms with no human curation, and LIRICAL ranked the true
> answer **IMPG2 at #1** (compositeLR **12.647**) — reproduced on both the synthetic VCF and a real WGS VCF.

---

## Quick start

**You need two things: a VCF, and the patient's description in words.** Nothing has to be in HPO format.

1. **Attach the VCF** — the console's dataset slot (`.vcf` / `.vcf.gz`).
2. **Give the clinical description** — either way works, you do not have to choose:
   * paste it straight into the chat question, **or**
   * attach it with **"Attach a case note"** (a *separate* slot — the VCF keeps the dataset slot).
3. **Ask for the diagnosis**, e.g. `complete the research with the VCF file and case note`.

The note can be as informal as the clinician's own writing, in any language:

```text
Adult patient, slowly progressive loss of CENTRAL vision. Bilateral central scotoma.
Both maculae show a vitelliform ("egg-yolk") lesion with surrounding atrophic change.
Hearing is normal — no history of hearing loss. No ataxia. No polydactyly.
```

**What you should get back** (this is the demo case's real output — use it as the shape to expect):

| | |
|---|---|
| **Observed** | `HP:0007663` Reduced visual acuity · `HP:0000603` Central scotoma · `HP:0007677` Vitelliform macular lesion · `HP:0007754` Macular dystrophy |
| **Excluded** | `HP:0000365` Hearing impairment · `HP:0001251` Ataxia · `HP:0001249` Intellectual disability · `HP:0010442` Polydactyly · … (12 total) |
| **Differential** | **IMPG2** vitelliform macular dystrophy at rank #1 |

> **The "no" sentences are doing real work — do not trim them.** Writing what the patient *does not*
> have is as load-bearing as writing what they do: in testing, adding 5 pertinent negatives drove the
> wrong front-runner from **rank #1 to #18** (compositeLR +7.447 → −2.368) and let IMPG2 take the top
> spot. A note stripped to positives only is a materially weaker input.

> **If it returns 0 terms, the note never reached the mapper** — check the note is actually attached (or
> pasted in the question). The tool refuses to invent a phenotype, so an empty profile is always an input
> problem, never a "no findings" answer.

> **How to read the "parameters" in each step.**
> - **🔬 Agent-chosen (scientific)** — decided per study; **audit these**: the gene panel / `regions_bed`,
>   `max_pop_af`, and the clinical text passed to the mapper.
> - **🧭 Auto-detected** — `assembly` from the VCF header (GRCh37 fallback); LIRICAL's genotype-aware vs
>   phenotype-only mode, chosen by whether the VCF + a matching Exomiser DB are present.
> - **⚙️ Fixed (infra)** — gateway-injected, never model-set: the HPO lexicon release, `lirical.sif`, the
>   Exomiser DB path, and the extraction LLM call's decoding settings.

---

## At a glance — two independent tracks, then reconcile

| # | Step | Track | Tool / skill | 🔬 Key agent-chosen params | Output |
|---|------|-------|--------------|----------------------------|--------|
| 1 | **QC the callset** | A | `vcf_qc_stats` (run_code) | `SEQ_TYPE` = WGS \| WES | `vcf_qc_summary.csv` |
| 2 | **Narrow: panel + drop-common** | A | `annotate_variants` | `genes` / `regions_bed`, `max_pop_af` | in-panel rare set |
| 3 | **Annotate** | A | `annotate_variants` | 🧭 `assembly` auto-detected | `variant_annotation.tsv` + tables |
| 4 | **Prioritise** | A | `annotate_variants` (built-in) | disease-model AF thresholds | `high_priority_variants.csv` |
| 5 | **Free text → HPO** | B | `map_phenotype_to_hpo` | the clinical text, **verbatim** | `observed` + `excluded` HPO IDs |
| 6 | **Per-disease differential** | B | `run_lirical` | `hpo_terms`, `excluded_hpo` | ranked diseases + post-test prob |
| 7 | **Reconcile** | A×B | *(report from 4 + 6)* | — | the actual answer |
| 8 | **Literature context** | — | `literature_search` | gene + phenotype query | citations |

> **Tracks A and B are INDEPENDENT — do not make one wait on the other.** `run_lirical` does *not*
> consume `annotate_variants`' output: in genotype-aware mode it scores the raw VCF itself through its
> own Exomiser database. Plan them as distinct agenda steps that meet only at Step 7. The methods+results
> report is assembled automatically — **do not add a report-writing step.**

---

## Step-by-step

<details open>
<summary><b>Precondition</b> — run this protocol ONLY if the case actually has a phenotype</summary>

This protocol applies **if and only if** the study carries the patient's clinical description: symptoms,
a diagnosis, an HPO list, or a case note. Language and formality do not matter — `夜盲、视野缩窄、ERG 熄灭型`,
`Stargardt`, and `RP with macular involvement` are all usable.

**If there is no phenotype anywhere, use `variant_annotation` instead** and plan no phenotype step. Do
**not** infer a phenotype from gene names, the file name, or the fact that it is an eye study — a
fabricated phenotype produces a confident, wrong differential, which is worse than none. `run_lirical`
refuses an empty phenotype; do not work around it.

**✅ Verify:** the description is quoted from the case, not from the agent's assumption.
</details>

<details>
<summary><b>Steps 1–4 · Track A (variants)</b> — as in the <code>variant_annotation</code> protocol</summary>

Follow [`variant_annotation/PROTOCOL.md`](../variant_annotation/PROTOCOL.md); the essentials:

1. **QC the callset** — Ti/Tv, call rate. A flagged callset caveats everything downstream.
2. **Known genes FIRST + drop common** — restrict to the disease-gene panel (`genes` / `regions_bed`),
   set `max_pop_af=0.01`. **State the panel and the cutoff.** Expand genome-wide only if in-panel is negative.
3. **Annotate** — assembly is auto-detected; `annotate_variants` normalises internally (do **not** plan a
   separate normalise step). Report `execution_mode`.
4. **Prioritise** — lead with the `high_priority_variants` shortlist the tool already wrote. Report from
   its tables; do not hand-roll `run_code` to re-derive them.

**✅ Verify:** `execution_mode == offline_vep` · assembly matches the VCF header · the shortlist leads.

<sub>Source: `preset_pipelines/variant_annotation/SKILL.md` · `vcf_offline.run_offline_annotation`</sub>
</details>

<details open>
<summary><b>Step 5 · Free text → HPO</b> — the LLM does language, the ONTOLOGY owns identity</summary>

**What.** `map_phenotype_to_hpo` turns the clinician's prose into real, ontology-validated HPO IDs,
splitting them into **observed** (present) and **excluded** (explicitly absent).

**Why it is built as a closed set.** HPO IDs one digit apart are *different real phenotypes*
(`HP:0000662` Nyctalopia vs `HP:0000622` Blurred vision), so a fabricated-but-real ID fails **silently** —
LIRICAL conditions on the wrong phenotype and returns a confident wrong differential. Therefore the model
is never allowed to author an ID:

```text
1. EXTRACT   (LLM)   prose → clinical phrases + negation + the verbatim source span
2. RETRIEVE  (code)  each phrase → real candidate terms from the bundled HPO release
3. SELECT    (LLM)   the model picks a candidate NUMBER — it never types an ID
4. VALIDATE  (code)  re-check against the ontology; the canonical name comes from HPO, not the model
```

**🔬 Agent-chosen:** the clinical text. Pass the clinician's words **verbatim**, not a paraphrase — or
call with **no arguments at all** to map the attached case note.

**⚙️ Fixed infra:** the bundled HPO release (**2026-06-23**, 19,120 current + 577 obsolete terms — so this
step runs offline, no network), and the extraction call's decoding settings. Those settings are
load-bearing: the served Qwen3.6 is a *reasoning* model, and with its thinking trace enabled it exhausts
the token budget before emitting any JSON, returning an empty profile. The call therefore runs with
**thinking disabled** and a generous ceiling.

**Report, don't hide:**
- **`unmapped` phrases** — findings the ontology could not match are a real limitation of the
  differential, not noise.
- **`excluded_hpo`** — pass it through to LIRICAL. An explicit "hearing is normal" is *evidence*; LIRICAL
  uses absent findings to push diseases DOWN.
- If it returns **no observed terms**, stop this track and say so — the text had no usable phenotype.

**✅ Verify this step:** every reported term carries the phrase it came from (the tool returns them), so a
clinician can **audit** the mapping instead of trusting it · negated findings landed in `excluded`, not
`observed` · a family member's disease ("her mother had RP") did **not** become the patient's phenotype.

<sub>Source: `src/bioagent/tools/hpo_terms/mapper.py` (extract → retrieve → select → validate) · `src/bioagent/tools/hpo_terms/index.py` (the ontology gate)</sub>
</details>

<details open>
<summary><b>Step 6 · Per-disease differential</b> — LIRICAL likelihood ratios</summary>

**What.** `run_lirical` scores each candidate disease against the HPO profile and returns a ranked
differential with a **compositeLR** (log10 likelihood ratio) and a post-test probability.

**🧭 Auto-detected mode:**

| Mode | When | Meaning |
|---|---|---|
| **Genotype-aware** | VCF **+** a matching Exomiser DB present | variants sharpen the phenotype ranking |
| **Phenotype-only** | otherwise | still a valid posterior from symptoms alone |

**Report which mode ran**, plus `phenotype_notes` — the IDs the ontology gate dropped or forwarded
(obsolete → `replaced_by`), so a reader knows exactly which terms were scored.

**⚠️ The post-test probability saturates — never present it as clinician confidence.** Above a likelihood
ratio of ~10⁶ the Bayesian posterior pins near 100%, so a *wrong* answer and the *right* answer can read
**99.97% vs 99.96%** — a coin flip presented as certainty. In testing the posterior swung **8×** on the
choice between two near-synonymous HPO terms while the gene ranking barely moved. **Report the rank and
the compositeLR; treat the percentage as a sorting key, not a probability of being right.**

**✅ Verify this step:** the mode is stated · `phenotype_notes` are reported · the ranking is presented by
rank + compositeLR, with the saturation caveat attached to any percentage shown.

<sub>Source: `src/bioagent/tools/phenotype_dx.py` (`parse_lirical_tsv`, `hpo_release_drift`, `normalize_assembly`) · LIRICAL v2.4.1 in `lirical.sif`</sub>
</details>

<details open>
<summary><b>Step 7 · Reconcile</b> — the step that actually answers the question</summary>

Put the two tracks side by side and say what they agree on. **Report BOTH tracks even when they
disagree — the disagreement is often the finding.**

| Outcome | What it means | How to report it |
|---|---|---|
| **Concordant** | the top LIRICAL disease's gene also carries a prioritised variant in Track A | **the strong result** — name the disease, its rank + compositeLR, the gene, and the specific variant(s) with ClinVar/predictor evidence |
| **Phenotype-only hit** | LIRICAL ranks a disease high, Track A found nothing in that gene | say so plainly: the causal variant may have been **filtered out** (panel too narrow, AF cutoff, an intronic/structural variant VEP does not flag) — **not** that the disease is excluded |
| **Variant-only hit** | a compelling variant in a gene LIRICAL did not rank | **expected, NOT a contradiction.** LIRICAL scores against *curated* HPO/OMIM annotations, so a new gene-disease association or a phenotype expansion **cannot** rank, by construction. Read a low rank as "not in the curated annotations", never as "not causal" — then use `literature_search` on that gene + phenotype for evidence the curation lags |

**✅ Verify this step:** both tracks are reported · a variant-only hit is framed as a curation gap, not a
refutation · the named answer cites *both* its phenotype evidence and its variant evidence.

<sub>Source: `src/bioagent/tools/phenotype_dx.py` (`reconcile`)</sub>
</details>

<details>
<summary><b>Step 8 · Literature context</b></summary>

**What.** For the reconciled gene/disease, pull real citations via `literature_search` — especially for a
variant-only hit, where recent literature is exactly what the curated annotations are missing.

**Why / honesty.** Cite **only** what the tool returns — never invent a PMID or a claim.

**✅ Verify this step:** every citation resolves to a returned record · claims are attributed.
</details>

---

## Caveats to carry (state them, don't bury them)

- **LIRICAL's post-test probability is calibrated against curated annotations, not truth.** It is a
  research-triage ranking, **never** a clinical diagnosis — and see the saturation warning in Step 6.
- **The phenotype came from free text through an LLM extraction step.** Report each mapped term with its
  source phrase so a clinician can audit the mapping rather than trust it.
- **Most eye/IRD VCFs are GRCh37/hg19.** LIRICAL then needs the hg19 Exomiser DB, and note that
  CADD and REVEL are staged for **both builds** (a GRCh37 run gets both); only AlphaMissense is **GRCh38-only**, so on GRCh37 that one column is
  blank. Report what IS present; do not fabricate the rest.
- **An empty HPO profile is an input problem, not a finding.** The tool refuses to invent a phenotype, so
  0 terms means the note never reached it — never report "no phenotypic findings".

## Grounding & honesty (applies to every step)

Report ONLY HPO terms the mapper returned and diseases/genes LIRICAL ranked — never author an HPO ID, a
disease, or a rank. ClinVar significance is reported verbatim; do not upgrade "uncertain". Always state
the **genome assembly**, the **`execution_mode`**, and **which LIRICAL mode** ran. Frame every conclusion
as a hypothesis requiring orthogonal validation (phasing, segregation, functional work), not a diagnosis.

## Method provenance

Ensembl VEP (offline cache) · ClinVar · gnomAD · SIFT/PolyPhen · CADD + REVEL (GRCh37 + GRCh38) · AlphaMissense (GRCh38 only) ·
**HPO release 2026-06-23** (bundled lexicon, offline) · **LIRICAL v2.4.1** + Exomiser 2406_hg19, in
`lirical.sif` on HPC3. Full tool inventory: [`docs/vcf_pipeline_tools.md`](../../docs/vcf_pipeline_tools.md) ·
mapping design: [`docs/free_text_to_hpo_mapping.md`](../../docs/free_text_to_hpo_mapping.md) · a worked
post-mortem of a wrong-answer-ranked-first case:
[`docs/case_a_why_a_wrong_answer_ranked_first.md`](../../docs/case_a_why_a_wrong_answer_ranked_first.md).

<sub>This protocol renders `preset_pipelines/phenotype_variant_diagnosis/SKILL.md` — SKILL.md is the file the
agent actually loads; regenerate this document if the skill's steps change.</sub>
