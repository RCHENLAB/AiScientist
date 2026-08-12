# Phenotype → disease confidence (differential diagnosis) — design spec

**Origin.** Rui Chen, meeting note: IRD symptoms overlap heavily across diseases, so a phenotype alone
cannot confirm a diagnosis — but *phenotype + the gene-annotation results* should let us estimate **how
confident we are in each candidate disease** ("RP 70% / LCA 20% / Usher 10%"). This is a **disease-level
differential diagnosis with a confidence per disease**, sitting downstream of the VCF pipeline.

> Supersedes the earlier gene-centric draft. The unit of output is a **disease**, not a gene.

## 1. The problem, stated precisely

Symptoms are **many-to-many** with diseases (night blindness + field loss ⇒ RP, LCA, CSNB, Usher,
choroideremia …), so `P(disease | symptoms)` is a **broad, non-specific** distribution. The genetic
findings are **specific** (a biallelic pathogenic CRB1 strongly implicates a handful of diseases), so they
**sharpen** that distribution:

```
P(disease | symptoms, genetics) ∝ P(symptoms | disease) · P(genetics | disease) · P(disease)
                                   └ broad / overlapping ┘  └ specific, disambiguates ┘
```

The **posterior over diseases** is the confidence the advisor asked for. This is exactly what a class of
phenotype-driven diagnostic tools computes — we adopt one rather than invent it.

## 2. Two tracks — and why they must NOT be blended into one number

| | Question it answers | Output | Trust |
|---|---|---|---|
| **PRIMARY — LIRICAL** | *given the gene is a cause, how well does THIS patient match this disease?* | calibrated **post-test probability** per disease (+ per-symptom likelihood ratio) | the confidence number |
| **EVIDENCE — PaperQA2 (literature)** | *is this gene–disease association even established, and how strong is the evidence?* | a **ClinGen validity tier** (Definitive/Strong/Moderate/Limited/Disputed/Refuted) + PMIDs | a grade, not a probability |

They answer **different questions**, so their outputs are **different currencies**. Averaging a calibrated
probability with an evidence grade manufactures false precision. The reconciliation (§5, layer 1) therefore
keeps them separate — literature can **attach** support to a LIRICAL call or **rescue** a candidate LIRICAL
missed, and **never moves `posttest_prob`**.

> Note: a second, explicitly separate **decision** layer (§5, layer 2) does rank the two against each
> other, because the answer a clinician reads has to be one ordered list. It emits a `final_score` on
> its own named axis and still never rewrites `posttest_prob`. Keeping the ranking score and the
> calibrated probability as *different fields* is what preserves the point of this section.

## 3. Why the matching engine is LIRICAL/curated-KB, NOT RAG

Symptom→disease matching is a **look-up + probability** problem, not a reading-comprehension one:
1. The gene↔disease↔phenotype relationships (with **symptom frequencies**) are already curated by clinical
   experts in **HPO / HPOA / OMIM / Orphanet** — the distillation of the literature, done rigorously. RAG
   would re-derive this from noisy paper text, model-dependently. Using the curated KB is strictly better.
2. It needs a **calibrated probability**; LIRICAL computes per-symptom **likelihood ratios** (information
   content × annotation frequency) → a posterior. An LLM reading chunks yields a vibe, not a calibrated LR.
3. **Symptom overlap** is handled correctly by this statistics (rare/specific symptoms weigh more); RAG has
   no principled weighting.
4. It is **auditable** (per-symptom LR); RAG gives "the model thought so + quotes".

**RAG's legitimate role is the long tail + citations** (§4, EVIDENCE track), not the matcher.

## 4. Architecture & data flow

```
patient symptoms ─► HPO inference (tools/hpo_terms, built) ─┐
                                                            ▼
VCF ─► annotate ─► variant shortlist (gene+variant+disease-model) ─► [PRIMARY] LIRICAL
                                     │  (HPO terms + variants)          → per-disease post-test prob
                                     └────────────────────────────────► [EVIDENCE] PaperQA2 (long tail)
                                                                          → ClinGen tier + PMIDs
                                                            ▼
                                         reconcile (attach / rescue — never blend) ─► differential:
                                             RP12 (CRB1) 0.70  [lit: STRONG, PMID…]
                                             LCA1 (GUCY2D) 0.20
                                             ⚑ NEWGENE (lit STRONG, LIRICAL missed → review)
```

## 5. Reconciliation, then adjudication — two layers, on purpose

**Layer 1 — provenance (`tools/phenotype_dx.reconcile`).** Keeps the currencies apart, so what each
track actually said stays auditable forever.
- **ATTACH** — literature evidence (tier + PMIDs) is attached to the matching LIRICAL candidate as
  support; its `posttest_prob` is **unchanged**.
- **RESCUE** — a literature-supported association LIRICAL did not surface, with ClinGen tier ≥ threshold
  (default STRONG), is added to a **separate** `literature_rescued` list, flagged for human review, with
  `posttest_prob = None` (no fabricated probability).

**Layer 2 — decision (`tools/phenotype_dx.adjudicate`).** A clinician needs ONE ordered list, so this
layer does weigh the tracks against each other — and weighs the **literature higher** (0.65 vs 0.35).
That is not a preference for prose over statistics: LIRICAL's posterior is only as current as the
OMIM/HPOA curation it reads, and that curation lags the literature by months to years, so when a
retrieved, cited body of work contradicts a curated call, **the curated call is the stale one**.

| `agreement` | when | scored on |
|---|---|---|
| `concordant` | both tracks positive | `0.65·tier + 0.35·posttest_prob` |
| `conflict` | literature DISPUTES/REFUTES a LIRICAL candidate | same sum, tier **negative** → it sinks below every uncontradicted candidate |
| `literature_only` | LIRICAL never surfaced it (not curated, or could not run) | tier alone — **this is the gap-fill** |
| `lirical_only` | literature not asked, or corpus returned nothing | `posttest_prob`, **no penalty** (absence of data ≠ evidence of absence) |
| `unsupported` | passages retrieved, none supported the link | `posttest_prob` × 0.85 (small: the corpus is bounded) |

**The invariant survives both layers:** `posttest_prob` is never rewritten, and no probability is
invented for a literature-only candidate. `final_score` is a **ranking** score on its own named axis,
not a calibrated probability, and every candidate carries the `agreement` branch that produced it plus
a `decision_note`.

`diagnose()` is the entry point that runs both tracks and both layers. It is what makes the line able
to answer a case LIRICAL alone cannot — LIRICAL not staged, LIRICAL erroring, or the gene simply not
curated — instead of returning an empty differential.

## 6. Output schema (per disease)
```json
{ "disease_name": "Retinitis pigmentosa 12", "disease_id": "OMIM:600105", "gene": "CRB1",
  "posttest_prob": 0.70, "composite_lr": 1200.5, "matched_hpo": ["HP:0000512", "HP:0000662"],
  "evidence_tier": "STRONG", "evidence_pmids": ["12345678"], "sources": ["lirical","literature"],
  "flags": [] }
```

## 7. Integration with the VCF pipeline
Downstream layer: consumes the pipeline's variant shortlist (already carries gene/variant/disease-model)
+ the patient HPO terms; emits the disease differential. The manuscript's prioritised table gains a
**per-disease Confidence** column with the per-symptom LR and the evidence trail.

## 8. Status & plan
- **Done (scaffold + runner + build kit, committed):** `tools/phenotype_dx.py` — data model, LIRICAL
  TSV parser, two-track reconciliation, PaperQA2 **placeholder**, PLUS the real runner: a GA4GH
  Phenopacket-v2 builder (`build_phenopacket`), the LIRICAL v2 `prioritize` command builder
  (`build_lirical_cmd`, phenotype-only vs genotype-aware), and `run_lirical` orchestration (injectable,
  so build/parse are tested without a live LIRICAL). In-container CLI `tools/phenotype_cli.py`
  (the `variant_cli` counterpart). Build kit `deploy/lirical/` (`lirical.def` = JRE 17 + LIRICAL v2 CLI;
  `build_and_stage.sh`; `README.md`). Gateway config `HPCSettings.phenotype_on_hpc` + `lirical_*`
  (gated OFF). `tests/test_phenotype_dx.py` (14 tests). All offline; no cluster/LIRICAL/network in CI.
- **BUILT + VERIFIED on HPC3 (2026-07-14):** `lirical.sif` (LIRICAL **v2.4.1**) built via Sylabs
  `--remote` and staged at `…/containers/lirical.sif`; LIRICAL data + the fresh **Exomiser 2406_hg19**
  variant DB (27.7 GB `.mv.db`) staged under `…/reference/lirical/`. Both modes smoke-tested end-to-end:
  *phenotype-only* (HPO → 8,621 diseases, RP subtypes on top, all tied — the overlap problem, as
  expected) and *genotype-aware* (a test ABCA4 `p.(G1961E)` variant **sharpened** the differential to the
  ABCA4 diseases, RP19 96.22%). The `build_lirical_cmd` flags were corrected against the real
  `prioritize --help` (v2.4.1 is CLI-args mode: `-p`/`-n`/`-d`/`-o`/`-x`/`-f`/`-ed19`, not a phenopacket).
- **Next (this line):** wire the gateway step — an app.py `SlurmAnalysisExecutor` for the phenotype line
  + catalog registration, mirroring the VEP wiring (~`app.py:3067`). **First fix the reconcile join:**
  LIRICAL's genotype-aware TSV emits `entrezGeneId` (`NCBIGene:24`), not a gene *symbol*, and
  `reconcile` keys on the symbol — map entrez→symbol via the staged `hgnc_complete_set.txt` before the
  merge. Then set the `.env` block (`deploy/lirical/README.md`) + deploy.
- **⚠️ Exomiser finding (resolved):** the plan assumed we'd reuse the Exomiser already on HPC3. We
  couldn't for LIRICAL v2 — the lab's install is `1805_hg19` + `exomiser-cli-10.1.0` (2018-era, Exomiser
  10.x schema, **hg19 only**), and LIRICAL v2 needs Exomiser data **≥ 2302**. Resolved by staging a fresh
  **2406_hg19** DB (~19 GB download → 27.7 GB `.mv.db`). **Phenotype-only** needs no Exomiser DB.
- **Phenotype input (Rui Chen's answer):** physicians use **free text**, so map free text → HPO with an
  LLM. The current `tools/hpo_terms.infer_hpo_terms` is a **keyword matcher** against a curated IRD
  table — a starting point, not the LLM mapper Rui asked for. Upgrading it (LLM extraction, then
  validate the returned IDs against `hp.json`) is the phenotype-input work item; `run_lirical` already
  takes an `hpo_terms` list + `excluded_hpo` from whatever produces them.
- **DONE (2026-08-05) — the evidence track is built and wired:** `tools/phenotype_evidence.py`
  implements the contract runner over the existing `deep_literature` (PaperQA2) tool, so the
  `paperqa2_evidence(runner=…)` seam is no longer a placeholder. The grading is deliberately NOT the
  model's word: retrieval decides existence (no passage ⇒ `NONE`), the retrieved passages **cap** the
  tier via `evidence_ceiling()` (counting *independent sources*, not chunks — a claimed DEFINITIVE off
  one case report is recorded as LIMITED), and every graded claim keeps the passage that produced it.
  `adjudicate()` (§5 layer 2) + `diagnose()` turn the two tracks into one ranked answer, and the new
  `diagnose_disease` tool exposes it — bound in the registry AFTER routing so it composes the *routed*
  `run_lirical` and `deep_literature` and follows them onto HPC3. Tests: `tests/test_phenotype_evidence.py`
  (17) + the adjudication/diagnose cases in `tests/test_phenotype_dx.py` and `tests/test_registry.py`.
  **Still to verify on the server:** end-to-end against the real /dfs3b PubMedBERT corpus — the grading
  ladder is calibrated against the ClinGen rubric, not yet against how this corpus actually retrieves.
- **Later:** calibrate LIRICAL's probabilities on the lab's solved cases (Meng Wang is compiling them)
  → a reliability curve.

## 9. Advisor decisions (from Rui Chen, 2026-07-14 email)
1. **Phenotype input** — ✅ **free text → HPO via LLM** (physicians don't use HPO terms). See §8.
2. **Calibration set** — ✅ Meng Wang to compile a set of **solved IRD cases** (VCF + known diagnosis) as
   the test/calibration set. Until then the posterior is uncalibrated (LIRICAL ships a default prior).
3. **Tool** — ✅ LIRICAL approved ("方案批准了"). It outputs per-disease post-test probabilities from
   phenotype + VCF (vs Phenomizer = phenotype-only, or Exomiser's gene-level disease mode).

## 10. Reuses
HPO inference `tools/hpo_terms/` (built) · Exomiser on HPC3 (LIRICAL uses its variant DB) ·
`literature_search`/PaperQA2 (evidence track) · the IRD variant pipeline (candidate genes/variants) ·
gap context [`ird_pipeline_parity_roadmap.md`](ird_pipeline_parity_roadmap.md) (layer 9).
