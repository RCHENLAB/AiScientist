---
name: phenotype_variant_diagnosis
description: Diagnose a rare-disease CASE — a VCF **plus** the patient's clinical description (free text, any language) — by reconciling two independent tracks: the prioritised variant shortlist (VEP/ClinVar/predictors) and a phenotype-driven per-disease differential (HPO → LIRICAL post-test probability). Use ONLY when the case's phenotype/symptoms/diagnosis are actually given; a VCF alone is the variant_annotation protocol.
tools: annotate_variants, map_phenotype_to_hpo, run_lirical, literature_search, run_code
data_type: variants
---

**Rare-disease case diagnosis** from a VCF **+ the patient's clinical description**. This protocol exists
because a variant shortlist alone cannot say *which disease*, and a phenotype alone cannot say *which
variant* — IRD symptoms overlap heavily. Reconciling the two is the whole point.

## Precondition — run this protocol ONLY if the case has a phenotype

This protocol applies **if and only if** the study actually carries the patient's clinical description:
symptoms, a diagnosis, an HPO list, or a case note. It does not matter which language it is in or how
informal it is ("夜盲、视野缩窄、ERG 熄灭型", "Stargardt", "RP with macular involvement" are all usable).

**Where the description comes from — either is fine, you do not have to care which:**
* the **research question** itself (the clinician pasted the note into the chat), or
* an **attached case note** (the console's "Attach a case note" slot). It is a separate slot from the
  dataset — the VCF keeps the dataset slot — and `map_phenotype_to_hpo` reads the attached note
  automatically when you call it with no `text`.

So: if the question carries the description, pass it as `text` VERBATIM. If it does not, call
`map_phenotype_to_hpo` with NO arguments — it will map the attached note. If neither exists, the tool
returns an error, and that is the signal to use `variant_annotation` instead.

**If there is no phenotype anywhere, use the `variant_annotation` protocol instead** and do not plan a
phenotype step. Do NOT invent, assume, or infer a phenotype from the gene names, the file name, or the
fact that it is an eye study — a fabricated phenotype produces a confident, wrong differential, which is
worse than no differential at all. `run_lirical` will refuse an empty phenotype; do not work around it.

## The two tracks (they are INDEPENDENT — do not make one wait on the other)

`run_lirical` does **not** consume `annotate_variants`' output: in genotype-aware mode it scores the raw
VCF itself via its own Exomiser database. So the tracks are parallel and only meet at the reconcile step.
Plan them as distinct agenda steps:

### Track A — variants (`annotate_variants`)
Follow the `variant_annotation` protocol's approach; the essentials, not restated in full here:
1. **QC the callset** (`vcf_qc_stats` skill) — Ti/Tv, call rate; a flagged callset caveats everything after.
2. **Known genes FIRST + drop common variants** — restrict to the disease-gene panel (`genes` /
   `regions_bed`) and set `max_pop_af=0.01`. STATE the panel and the cutoff. Expand genome-wide only if
   the in-panel search is negative.
3. **Annotate** — assembly is auto-detected from the VCF header; `annotate_variants` normalizes
   internally (do NOT plan a separate normalize step). Report `execution_mode`.
4. **Prioritise** — lead with the `high_priority_variants` shortlist the tool already wrote. Report from
   its tables; do not hand-roll `run_code` to re-derive them.

### Track B — phenotype (`map_phenotype_to_hpo` → `run_lirical`)
1. **`map_phenotype_to_hpo`** on the case description, **verbatim** — pass the clinician's own words, not
   your paraphrase (or no `text` at all, to map the attached note). It returns real, ontology-validated
   HPO IDs. **Never write HPO IDs yourself**: IDs one digit apart are different real phenotypes
   (HP:0000662 = Nyctalopia, HP:0000622 = Blurred vision), and a wrong-but-real ID fails silently.
   - Report its `unmapped` phrases — findings the ontology could not match are a real limitation of the
     differential, not noise to hide.
   - Pass `excluded_hpo` through as well: a finding the text says is ABSENT ("无听力障碍", "hearing is
     normal") is evidence, and LIRICAL uses it to push diseases DOWN.
   - If it returns **no observed terms**, stop this track and say so — the text had no usable phenotype.
2. **`run_lirical`** with those terms. With the VCF + the Exomiser DB present it scores GENOTYPE-AWARE
   (variants sharpen the ranking); otherwise PHENOTYPE-ONLY (still a valid posterior from symptoms alone).
   Report which mode ran, and the `phenotype_notes` (IDs the ontology gate dropped/forwarded).

## Reconcile — the step that actually answers the question

Put the two tracks side by side and say what they agree on:

- **Concordant** — the top LIRICAL disease's gene also carries a prioritised variant in Track A. This is
  the strong result: name the disease, its post-test probability, the gene, and the specific variant(s)
  with their ClinVar/predictor evidence.
- **Phenotype-only hit** — LIRICAL ranks a disease highly but Track A found nothing in that gene. Say so
  plainly: it may mean the causal variant was filtered out (panel too narrow, AF cutoff, an intronic /
  structural variant VEP does not flag), not that the disease is excluded.
- **Variant-only hit** — a compelling variant in a gene LIRICAL did not rank. **This is expected and is
  NOT a contradiction.** LIRICAL scores against *curated* HPO/OMIM annotations, so a gene whose
  disease association is new, or a phenotype expansion of a known gene, CANNOT rank — by construction.
  Treat a low LIRICAL rank as "not in the curated annotations", never as "not causal". Use
  `literature_search` on that gene + the phenotype to check for recent evidence the curation lags.

Report BOTH tracks even when they disagree — the disagreement is often the finding.

## Caveats to carry (state them, don't bury them)

- **LIRICAL's post-test probability is calibrated against curated annotations, not truth.** It is a
  research-triage ranking, not a clinical diagnosis. Never present it as a diagnosis.
- **Most eye/IRD VCFs are GRCh37/hg19.** LIRICAL then needs the hg19 Exomiser DB, and note that
  CADD and REVEL are staged for BOTH builds (a GRCh37 run gets both); only AlphaMissense is GRCh38-only, so on GRCh37 that one column is
  blank. Report what IS present; do not fabricate the rest.
- **The phenotype came from free text through an LLM extraction step.** Report the mapped terms with the
  phrase each came from (the tool returns them) so a clinician can audit the mapping rather than trust it.
