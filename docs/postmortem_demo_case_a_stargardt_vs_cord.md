# Postmortem — I predicted Stargardt, LIRICAL said cone-rod dystrophy, and LIRICAL was right

**Date:** 2026-07-15 · **Verdict: the tool was correct; my test fixture was mislabelled.**

This is a record of a wrong prediction I made, why it was wrong, and why the machinery that contradicted
me was doing exactly the right thing. It is worth writing down because the failure mode — *a human
writes a case note that does not say what he thinks it says* — is the failure mode this whole phenotype
line exists to surface, and it caught **me** on the very first fixture I built.

## 1. How the fixture was designed

The ask was a synthetic VCF + case to test whether the VCF+HPO pipeline scores. The obvious design — one
disease, one obviously-matching note — proves nothing: the genotype alone would answer it, and the
phenotype could be doing no work at all and you couldn't tell.

So `demo_case.vcf` was built with **two genuinely plausible recessive candidates at once**:

| Gene | Variant (GRCh37) | Zygosity | Verified against Ensembl VEP as |
|---|---|---|---|
| **ABCA4** | `chr1:94473807 C>T` (rs1800553) | het | `c.5882G>A` **p.Gly1961Glu** — a classic Stargardt allele |
| **ABCA4** | `chr1:94528837 G>A` | het | `c.1591G>T` **p.Glu531Ter**, stop_gained → **compound het** |
| **MKKS** | `chr20:10394008 C>T` (rs28937875) | **hom** | `c.155G>A` **p.Gly52Asp** — classic Bardet-Biedl (BBS6) |

Both are AR. Both are well curated. **The genotype cannot break the tie** — a compound-het
truncating+missense in ABCA4 and a homozygous pathogenic missense in MKKS are both compelling. Only the
phenotype can. Two notes were written against the identical VCF:

- **Note A** — macular, intended as "Stargardt-shaped"
- **Note B** — syndromic RP (obesity, polydactyly, renal cysts), intended as "BBS-shaped"

**The prediction, written down before running anything** (`EXPECTED_RESULTS.md`):

> | Note | Expected top disease | Expected gene |
> |---|---|---|
> | **A** | **Stargardt disease 1** (OMIM:248200) | **ABCA4** |
> | **B** | **Bardet-Biedl syndrome 6** (OMIM:605231) | **MKKS** |

## 2. What actually happened

Measured on HPC3 (job 54193633), genotype-aware, the production argv from `build_lirical_cmd`:

| Note | #1 | posttest | Predicted? |
|---|---|---|---|
| **A** | **ABCA4** — *Cone-rod dystrophy 3* (OMIM:604116) | **94.78%** | gene ✅ / disease ❌ |
| **B** | **MKKS** — *Bardet-Biedl syndrome 6* (OMIM:605231) | **51.11%** | ✅ exactly |

**The headline claim held**: same VCF, and the phenotype flipped the top gene. The negated terms worked
too — MKKS falls to #5 under note A with a **compositeLR of −6.577**, i.e. actively pushed down.

But under note A, my predicted **Stargardt disease 1 came 3rd at 0.00%** (compositeLR −0.633). Not close.

## 3. Why LIRICAL was right and I was wrong

Not opinion — this is `phenotype.hpoa`, the annotation file LIRICAL actually scores against.

The four HPO terms note A yields:

| | HPO | In **Stargardt 1**? | In **Cone-rod dystrophy 3**? |
|---|---|---|---|
| macular atrophy | HP:0007754 Macular dystrophy | ✗ | ✗ |
| photophobia | HP:0000613 Photophobia | ✗ | ✗ |
| central vision loss | **HP:0000572 Visual loss** | ✗ | **✓** |
| colour discrimination | **HP:0000551 Color vision defect** | ✗ | **✓ — frequency 10/10** |

CORD3 is annotated with **two of my four terms**, one of them at **100% frequency (10/10)**. Stargardt 1
is annotated with **none of them** — its annotations are a different clinical picture: `HP:0007663`
Reduced visual acuity (15/15), `HP:0000550` Undetectable ERG (3/5), juvenile onset (5/5).

So the likelihood ratio has no choice, and it is not being stupid — **it is reading my note correctly and
I was not**:

> I wrote "**marked photophobia**" and "**colour discrimination has deteriorated**".
> Those two are the textbook signs of **cone dysfunction**. That is cone-rod dystrophy.
> I labelled the file `case_note_A_stargardt.txt` and then described a cone-rod patient in it.

ABCA4 causes Stargardt, cone-rod dystrophy **and** RP19 — one gene, three diseases, distinguished
precisely by which photoreceptors lead. LIRICAL picked the one my text described. **The gene — the
actionable answer — was right at 94.78%.**

## 3b. The follow-up that matters more than the postmortem

Re-run end to end with the **real Qwen3.6-35B-A3B** doing the extraction (job 54196284), i.e. the actual
production chain rather than my hand-picked terms. The model read the same note and chose *slightly*
different terms — `HP:0000505 Visual impairment` + `HP:0007401 Macular atrophy` where I had picked
`HP:0000572 Visual loss` + `HP:0007754 Macular dystrophy`. Same note. Same VCF. Same disease.

| Note A, terms from | #1 gene | #1 disease | posttest | Stargardt lands |
|---|---|---|---|---|
| me, by hand | **ABCA4** | Cone-rod dystrophy 3 | **94.78%** | #3, 0.00% |
| **the real LLM** | **ABCA4** | Cone-rod dystrophy 3 | **12.36%** | #2, 0.44% |

**The gene is robust to term choice. The posterior is not — it moved 8×.**

This is the single most useful number in this document. It means:

- **Judge the GENE, and judge the A-vs-B flip.** Both survived every variation we threw at them.
- **Do not put LIRICAL's posterior in front of a clinician as a confidence.** "94.78%" and "12.36%" are
  the same clinical case, the same VCF, the same gene — the difference is entirely which near-synonym
  the extraction step happened to pick from the ontology. A calibrated probability whose input is an
  LLM's word choice is not calibrated *end to end*, whatever it is conditional on its HPO terms.
- **This bounds the whole line's honest claim**: it ranks genes well; it does not measure confidence in
  a disease. See the "silent degradation" posture — this belongs in the technical report's Diagnostics,
  not as a headline number in the manuscript.

## 4. What this says about the system (the part worth keeping)

- **The phenotype is genuinely driving the ranking.** Not a slogan: same VCF, top gene flipped, and a
  wrong-for-the-phenotype gene got a **negative** composite LR rather than merely a lower one.
- **It is sensitive enough to out-resolve its operator.** It separated cone-rod from Stargardt on two
  words in a referral note. That is the whole value proposition of a likelihood-ratio model over "the
  gene has a pathogenic variant, ship it" — and a reason to trust it *more*, not less.
- **It also means garbage phrasing → confident wrong disease.** The flip side of that sensitivity is
  that an imprecise note is not ignored, it is *believed*. Which is exactly why `map_phenotype_to_hpo`
  reports the phrase each term came from: so a clinician can see that "photophobia" is what moved the
  answer, and say "no, that's not what I meant". Auditability is not decoration here — it is the
  control for this failure.
- **My EXPECTED_RESULTS.md hedge was load-bearing.** It said: *"Also fine: the differential names a
  different ABCA4 disease for A … The GENE is the signal."* Written because ABCA4's three-disease
  spread was foreseeable. Judge the gene, and judge the A-vs-B flip — not the disease label.

## 5. Actions

- [x] Recorded here rather than quietly re-labelling the fixture and pretending the prediction held.
- [ ] **Rename** `case_note_A_stargardt.txt` → the note describes cone-rod dystrophy; the filename is
      the lie, not the note. Either rename it, or rewrite it to be actually Stargardt-shaped (drop
      photophobia + colour defect; add `HP:0007663` reduced visual acuity, juvenile onset, undetectable
      ERG). **Rewriting is the better test** — it would prove the model can separate two ABCA4 diseases
      on the same gene, which is a sharper claim than A-vs-B across two genes.
- [ ] Update `EXPECTED_RESULTS.md` with the measured numbers, and keep the original prediction visible
      next to them.

## Appendix — reproduce

```bash
# HPC3, job 54193633 — the argv comes from build_lirical_cmd, i.e. what production runs
lirical prioritize -p HP:0007754,HP:0000613,HP:0000572,HP:0000551 \
  -n HP:0000662,HP:0000365,HP:0010442 \
  -d <lirical/data> -o out_A -x A -f tsv \
  --vcf demo_case.nochr.vcf --assembly hg19 -ed19 <exomiser/2406_hg19> --sample-id DEMO_CASE_01

# the annotations that decided it
awk -F'\t' '$1=="OMIM:248200"' phenotype.hpoa   # Stargardt disease 1
awk -F'\t' '$1=="OMIM:604116"' phenotype.hpoa   # Cone-rod dystrophy 3
```
