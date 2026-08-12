# Validation report — `phenotype_variant_diagnosis` (free text → HPO → LIRICAL)

> **Provenance.** The case identifier is redacted (`CASE_A`). The gene, the disease and the two
> variants named below are public ClinVar/OMIM knowledge, and the clinical note used as input was
> *constructed* from those public annotations — it is not a patient's record. What is deliberately
> not published anywhere in this repository is which case this was.

**Verdict: the line works, and the phenotype is demonstrably doing the work.** On an identical VCF, two
different clinical notes flip the top gene; on a known solved case the pipeline recovers the right answer
at rank 1 from free text alone, with no human curation of HPO terms.

**Scope & honesty.** Every case below is **synthetic** (no real patient) but built around **real,
ClinVar-classified variants** and **real solved-case answers**, so "correct" is defined in advance. All
numbers are measured on HPC3 with job IDs cited — none are estimates. The evidence tables in §1–§3 were
previously only in `sample_data/` (gitignored, never on main); this report is where they live now.

**How the phenotype was produced.** The HPO terms in every run below came from
`map_phenotype_to_hpo` driven by the **real Qwen3.6-35B-A3B** (via OpenRouter / the served model) —
*not* hand-curated. That distinction turns out to matter a great deal (§4).

---

## 1. Headline — the full chain on a known answer

**Case:** IMPG2 vitelliform macular dystrophy (OMIM:616152), compound heterozygous `p.Arg1088*` +
`p.Arg131Cys` — both real ClinVar-pathogenic loci. A complete clinical referral note (positives = IMPG2's
real HPO annotations; negatives = what a real work-up records) went in as **free text**.

**Stage 1 — free text → HPO** (real Qwen3.6, no human curation): **4 observed + 12 excluded**

| | Terms |
|---|---|
| **Observed** | `HP:0007663` Reduced visual acuity · `HP:0000603` Central scotoma · `HP:0007677` Vitelliform macular lesion · `HP:0007754` Macular dystrophy |
| **Excluded** | hearing impairment · ataxia · muscle weakness · peripheral neuropathy · intellectual disability · seizure · … (12 total) |

All four observed terms *are* IMPG2's curated disease annotations (Reduced visual acuity at 8/8 frequency).

**Stage 2 — HPO + VCF → differential** (LIRICAL genotype-aware, hg19; jobs **54221697 / 54221787**):

| VCF | Rank 1 | compositeLR | Rank 2 |
|---|---|---|---|
| synthetic `sample_impg2.vcf` (5 variants) | **IMPG2 — Macular dystrophy, vitelliform 5** | **12.647** | IMPG2 RP56, LR −0.913 |
| real CASE_A WGS (**4.9 M variants**) | **IMPG2 — Macular dystrophy, vitelliform 5** | **12.647** | RP27, LR 3.204 |

Both VCFs give IMPG2 the **identical** LR — the answer is driven by the two pathogenic variants plus the
phenotype, independent of genomic background. On the real WGS, rank 2 trails by **9.4 orders of
magnitude**.

---

## 2. Does the phenotype actually drive the scoring? (the discrimination test)

A single case that lands on the right answer proves little — the genotype alone might be doing all the
work. So `demo_case.vcf` was built to carry **two equally compelling recessive candidates at once**:

| Gene | Variants | Why it is compelling |
|---|---|---|
| **ABCA4** | `p.Gly1961Glu` (classic Stargardt allele) + `p.Glu531Ter` (stop_gained) | compound het, truncating + pathogenic missense |
| **MKKS** | `p.Gly52Asp` **homozygous** | classic Bardet-Biedl (BBS6), ClinVar pathogenic |

Both autosomal recessive, both well curated — **the genotype cannot break the tie.** Two different notes
were written against this *identical* VCF, and each was mapped by the real LLM.

**Result** (HPC3 jobs **54193633 / 54196284**, genotype-aware — *these are the real-LLM-term runs*):

| Note | #1 gene | #1 disease | posttest | Predicted? |
|---|---|---|---|---|
| **A** — macular, photophobia, colour discrimination | **ABCA4** | Cone-rod dystrophy 3 (OMIM:604116) | 12.36% | gene ✅ / disease ❌ |
| **B** — syndromic RP: obesity, polydactyly, renal cysts | **MKKS** | Bardet-Biedl syndrome 6 (OMIM:605231) | 51.11% | ✅ exactly |

**The headline claim holds: same VCF, the note flipped the top gene.** And the negations did real work —
under note A, MKKS is actively pushed *down* to #5 with **compositeLR −6.577**.

> **The disease label for A was CLAUDE'S wrong prediction, not the tool's error.** Claude predicted Stargardt;
> LIRICAL said cone-rod dystrophy — and note A says "photophobia" and "colour discrimination has
> deteriorated", which *is* cone dysfunction — Claude mislabelled its own fixture. Full analysis:
> [`postmortem_demo_case_a_stargardt_vs_cord.md`](../../docs/postmortem_demo_case_a_stargardt_vs_cord.md).

---

## 3. What the negatives are worth (the starved-input experiment)

Same VCF, same case, the only variable being **how much of the clinical prose was supplied**:

| Input | IMPG2 result | Wrong answer (neurodegeneration syndrome) |
|---|---|---|
| **Diagnosis label only** → 2 vague terms, **0 exclusions** | rank **2**, LR 7.289 | rank **1**, LR **+7.447** |
| **+ 5 routine negations** ("hearing normal, no neuropathy, no ataxia, no intellectual disability, no weakness") | rank **1**, LR 7.474 | rank **18**, LR **−2.368** |
| **Complete referral note** (4 observed + 12 excluded) | rank **1**, LR **12.647** | — |

The wrong answer swung **9.8 orders of magnitude** and left the top 4. Nothing about the genome changed.

*Non-obvious but true:* a finding the patient is stated to **lack**, when the disease also lacks it, is
positive evidence *for* that disease — which is why IMPG2's own LR goes **up** (7.289 → 7.474) when the
negations are added, not just the wrong answer's going down.

**Five words of prose outweighed 4.9 million variants of genotype.** That is the case for this line, in
one number. Full derivation: [`case_a_why_a_wrong_answer_ranked_first.md`](../../docs/case_a_why_a_wrong_answer_ranked_first.md).

---

## 4. ⚠️ Do not report the posterior as confidence

The single most important operational finding. **Same case, same gene, same VCF — only the HPO term
wording differed:**

| HPO terms used | #1 result | posttest | compositeLR | actual LR |
|---|---|---|---|---|
| **Claude-authored** (`Visual loss` / `Macular dystrophy`) | ABCA4 — Cone-rod dystrophy 3 | **94.78%** | ≈ 5.195 | ≈ 156,514 |
| **Qwen3.6 extraction** (`Visual impairment` / `Macular atrophy`) | ABCA4 — Cone-rod dystrophy 3 | **12.36%** | ≈ 3.085 | ≈ 1,216 |

> **⚠️ Read this comparison correctly — it is LLM-vs-LLM, not expert-vs-LLM.** The first row's terms were
> **written by Claude** when authoring this fixture's prediction table, *not* curated by a clinician.
> (The source doc calls them "my hand-picked terms" — that "my" is Claude's.) So this measures **how far
> two language models drift from each other on the same note**; it is **not** evidence about deviation
> from expert ground truth. **No clinician-curated HPO baseline exists for these fixtures** — that gap is
> unclosed, and closing it needs the advisor's eyes, not another model.

**The instability is worse than the percentages suggest.** The `posttest` column differs by 7.7×, but the
underlying evidence differs by **129× (2.11 orders of magnitude)** — the posterior *compressed* it. The
`compositeLR`/LR columns above are back-computed from the recorded posteriors with the formula below,
which reproduces LIRICAL's own numbers to four decimals (validated in §"How the score is computed").

**Near-synonymous term choice moved 129× of evidence while the gene ranking did not move at all.** Two
compounding reasons the percentage misleads:

1. **The posterior saturates.** It is plain Bayes from a uniform prior of 1/8621, so above a likelihood
   ratio of ~10⁶ every strong candidate renders as "99.9-something %". In the CASE_A case the gap
   between a *wrong* rank 1 (99.9692%) and the *right* rank 2 (99.9557%) was a 1.44× difference in an LR
   of ~28 million — a coin flip displayed as a 0.0135-point "win".
2. **It is calibrated against curated annotations, not truth**, and a 5-variant synthetic VCF is nothing
   like the ~4 M-variant WGS its priors expect.

> **Report the rank and the `compositeLR`. Treat the percentage as a sorting key, never as a probability
> of being right.** Judge the gene and the A-vs-B flip, not the number.

---

## 5. How the score is actually computed — where the "weights" come from

There are **no tunable weight coefficients**. LIRICAL is a likelihood-ratio model: every piece of
evidence contributes an LR, and they multiply — i.e. they *add* in log space.

```
each OBSERVED term   LR_i = P(term | disease) / P(term | not disease)
each EXCLUDED term   LR_j = same, for the finding being ABSENT   ← can be < 1, i.e. NEGATIVE in log space
genotype             LR_g = variant pathogenicity × gene–disease link × inheritance fit  (Exomiser)

compositeLR = Σ log₁₀(LR_i) + Σ log₁₀(LR_j) + log₁₀(LR_g)
posttest    = Bayes( uniform prior 1/8621 , 10^compositeLR )
```

The only hand-set constant is the **uniform prior of 1/8621** (all 8,621 diseases equally likely a
priori).

**Verified.** Applying that formula to two independently recorded `compositeLR` values reproduces
LIRICAL's own published percentages to four decimals:

| Case | compositeLR | formula gives | recorded |
|---|---|---|---|
| IMPG2 (rank 2) | 7.289 | **99.9557 %** | 99.9557 % ✅ |
| wrong answer (rank 1) | 7.447 | **99.9692 %** | 99.9692 % ✅ |

**What the weights come out to in the discrimination test.** We recorded only `posttest` at the time, so
these `compositeLR` values are back-computed with the verified formula:

| Run | posttest | **compositeLR** | actual LR |
|---|---|---|---|
| Note A → ABCA4 / CORD3 (Qwen terms) | 12.36 % | **3.085** | 1,216 |
| Note B → MKKS / BBS6 (Qwen terms) | 51.11 % | **3.955** | 9,011 |
| Note A → ABCA4 / CORD3 (Claude terms) | 94.78 % | **5.195** | 156,514 |
| **Note A → MKKS** (measured directly) | — | **−6.577** | **0.00000026** |

That last row is the clearest illustration of the mechanism: under note A, MKKS's composite is
**negative**, meaning the accumulated evidence is *against* it — note A states "no polydactyly, no
obesity", which are exactly BBS6's defining features, so those excluded terms contribute LRs far below 1.
**That is the discrimination working, expressed as a number.**

> **Not yet available: the per-term breakdown.** The TSV we parse (`parse_lirical_tsv`) exposes only the
> *composite* — `compositeLR`, `posttest`, `pretest`. Which individual HPO term contributed how much LR
> lives in LIRICAL's **HTML** output, which these runs did not retain. So "term X was worth Y" cannot be
> stated from current data; it needs a re-run that keeps the HTML.

---

## 6. What broke in production, and what fixed it (2026-07-17)

The line above works — but in production it silently returned **zero HPO terms**, and the failure is
worth recording because nothing in the logs said "error".

**Symptom.** A correctly-attached case note (2 984 chars, confirmed present in the run's persisted
`run_state.json`, `text_source=attached_case_note`) produced `mode=needs_llm, n_observed:0` on **every**
call, and the step failed after 3 retries.

**Root cause.** The extraction call ran the served Qwen3.6 — a *reasoning* model — with thinking **on**
and `max_tokens=800`. The reasoning trace exhausted the budget before emitting any JSON, so `content`
came back empty (`finish_reason=length`) and the mapper saw nothing to parse.

**Reproduced on a real Qwen3.6**, same note:

| Config | Result |
|---|---|
| thinking ON, 800 tok *(= production)* | empty content → **0 observed** |
| thinking ON, 4 000 tok *(just raise the cap)* | **still empty** → 0 observed |
| thinking OFF, 800 tok | `mode=llm` → **4 observed + 12 excluded** ✅ |

Raising `max_tokens` alone does **not** fix it — default-effort thinking consumed **4 529** reasoning
tokens on this note while the actual answer was only ~520. **Disabling thinking is the fix.**

**Does turning thinking off cost accuracy?** Benchmarked across 5 labelled cases, think-on (with an
adequate 16 000-token budget) vs think-off: **both passed every labelled check**, 3/5 term sets were
**identical**, the one difference had think-**off** picking the *more specific* term (Dyschromatopsia vs
Color vision defect), and think-on returned **empty** on the dense note even at 16 000 tokens. No
measurable quality gain from thinking; strictly better reliability without it. *(N=5, single run each —
evidence, not a published benchmark.)*

**Fixed** in `6f15f87` (`think=False`) + `474129b`/`d18d407` (ceiling → 10 000, sized to the real output
bound of `_MAX_PHRASES=40` objects).

---

## 7. Limits (carry these)

- **LIRICAL scores against curated HPO/OMIM annotations.** A gene whose disease association is new, or a
  phenotype expansion of a known gene, **cannot** rank — by construction. A low rank means "not in the
  curated annotations", never "not causal".
- **The phenotype came from free text through an LLM step.** Every reported term must carry the source
  phrase it came from so a clinician can *audit* rather than trust it.
- **Predictor coverage is PER-BUILD** (corrected 2026-07-17, extended 2026-07-27). **CADD and REVEL are
  now staged for both GRCh37 and GRCh38**, and a UCSC **hg19 reference FASTA** is staged, which unblocks
  `bcftools norm`, HGVS `c.`/`p.` naming and **SpliceAI** on GRCh37 (all three gate on that one file).
  Only AlphaMissense remains GRCh38-only, so that one column stays blank on GRCh37 — **skipped, never
  substituted** with GRCh38 data, since a cross-build tabix hit returns a different variant's score.
  Report what IS present; do not fabricate the rest. *(The runs in §1–§3 predate all of this: at that
  time the gateway gated the whole predictor block to GRCh38, so they carry no predictor scores at all,
  and no normalization — a re-run would differ, plausibly for the better.)*
- **Synthetic fixtures.** Real variants and real solved-case answers, but invented patients; and the
  CASE_A phenotype was partly derived from its own diagnosis label (mildly circular).
- **An empty HPO profile is an input problem, not a finding** — the tool refuses to invent a phenotype,
  so 0 terms means the text never reached it. Never report "no phenotypic findings".

## Provenance

HPO release **2026-06-23** (bundled lexicon, offline; verified md5-identical to LIRICAL's own `hp.json`) ·
**LIRICAL v2.4.1** + Exomiser 2406_hg19 in `lirical.sif` on HPC3 · extraction by **Qwen3.6-35B-A3B**
(OpenRouter / served) · HPC3 jobs 54193633, 54196284, 54221697, 54221787 (2026-07-15 → 07-17).
Pipeline definition: [`preset_pipelines/phenotype_variant_diagnosis/SKILL.md`](../../preset_pipelines/phenotype_variant_diagnosis/SKILL.md)
(the file the agent loads) and its rendered protocol
[`PROTOCOL.md`](../../preset_pipelines/phenotype_variant_diagnosis/PROTOCOL.md).
