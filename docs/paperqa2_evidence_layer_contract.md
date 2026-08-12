# Handoff — PaperQA2 as the "evidence track" for phenotype→disease diagnosis

**To:** the PaperQA2 / literature-embedding owner · **From:** Yijun's line · **Status:** ✅ **BUILT**
(2026-08-05) — see the box below before reading the rest as a to-do.

> **This contract is now implemented** in [`src/bioagent/tools/phenotype_evidence.py`](../src/bioagent/tools/phenotype_evidence.py),
> as a runner over the `deep_literature` (PaperQA2) tool that already ships. Build
> `make_deep_literature_runner(deep_literature_executor, ctx)` and pass it straight into
> `paperqa2_evidence(runner=…)` — or just call `phenotype_dx.diagnose(...)`, which does the whole
> thing. The rest of this document stands as the SPEC (the rules below are what the implementation
> enforces); two things changed in the building:
>
> 1. **The tier is enforced, not trusted.** Rule 1 below said "grounded only"; the implementation
>    makes that mechanical — `evidence_ceiling()` computes the strongest tier the retrieved passages
>    can support (counting *independent sources*, not chunks) and the model's stated grade is clamped
>    to it. It may grade lower, never higher.
> 2. **Records carry `evidence_status`** (`graded` / `contradicted` / `unsupported` / `ungraded`) on
>    top of the fields below. A bare `association=False` cannot distinguish "the corpus said nothing"
>    from "papers came back and none supported it", and the differential must treat those differently
>    — the first is absence of data and carries no penalty. A runner written to the plain contract
>    (no `evidence_status`) still works and is read as "not asked".
>
> **The consumer changed too, in a way that matters to you:** "Only STRONG+ associations get rescued"
> is no longer the end of it. `DISPUTED`/`REFUTED` records are now acted on — they **outrank** LIRICAL
> and demote its candidate (see §5 layer 2 of the design spec). So a refutation you return has real
> consequences: grade it on retrieved text, never on the absence of supporting text.

## TL;DR
Keep doing the PaperQA2 corpus + QA. Your part is the **EVIDENCE track**, not the diagnosis engine. Concretely:
given a **gene + a candidate disease + the patient's HPO terms**, return a **structured, grounded evidence
record** — *does the literature support this gene–disease link, and how strongly* — as a **ClinGen validity
tier + citations**. Do **not** try to output a probability or to rank diseases; a separate calibrated engine
(LIRICAL) does the actual symptom↔disease matching and confidence.

## Why this split (30-second version)
Symptom→disease matching is a look-up + probability problem over curated ontologies (HPO/OMIM), so the
matching + the confidence number come from **LIRICAL**, not from reading papers. But LIRICAL is blind to
anything **not yet curated** into OMIM/HPOA — newly reported genes, phenotype expansions, ultra-rare
associations. **That long tail is your job:** surface it from the literature, graded and cited, so a human
reviewer sees "LIRICAL missed this, but 3 papers support it."

## The interface contract (build to this)

**Function you provide** — plugs into `tools/phenotype_dx.paperqa2_evidence(..., runner=<yours>)`:

```python
def paperqa2_runner(*, gene: str, disease: str, hpo_terms: list[str]) -> dict:
    """Return the literature evidence for THIS gene–disease association, grounded in retrieved passages."""
    return {
        "association": True | False,          # does the corpus support a gene->disease link at all?
        "clingen_tier": "STRONG",             # one of the tiers below (evidence GRADE, never a probability)
        "disease": "Retinitis pigmentosa 12", # the disease the evidence is about (free text ok)
        "evidence": [                          # the passages that justify the tier — REQUIRED if association
            {"pmid": "12345678",
             "quote": "Biallelic CRB1 variants were identified in ... with night blindness and RP.",
             "study_type": "cohort"}           # case_report | cohort | functional | review
        ],
    }
```

**Rules (non-negotiable — these are what make it trustworthy):**
1. **Grounded only.** Every claim (`association`, `clingen_tier`) must be justified by passages you actually
   retrieved and return in `evidence` (PMID + quote). No `evidence` ⇒ `association=False`, `tier="NONE"`.
   Never emit an ungrounded model assertion.
2. **A tier, not a probability.** Output the ClinGen grade, not a number like 0.8. The calibrated
   probability is LIRICAL's; mixing the two manufactures false precision.
3. **Query from the specific inputs.** Build the retrieval query from `gene` + `disease` + the `hpo_terms`
   (the patient's specific phenotype), not a generic "what disease is this". Precise query → precise evidence.
4. **Corpus-bounded, and say so.** You cover the embedded corpus (~the key IRD papers). Out-of-corpus
   associations are simply `association=False` for you — that's fine; don't guess.

## ClinGen tier rubric (how to grade)
Use the ClinGen gene–disease *validity* framework (simplified):

| Tier | When |
|---|---|
| **DEFINITIVE** | replicated across multiple independent studies over years + functional/experimental support |
| **STRONG** | multiple unrelated probands/families, consistent, some experimental support |
| **MODERATE** | several probands, limited replication |
| **LIMITED** | a single case report / very sparse evidence |
| **DISPUTED / REFUTED** | evidence contradicts the association |
| **NONE** | nothing in the corpus |

Only **STRONG+** associations get "rescued" into the differential for review; weaker ones are recorded but
don't promote a candidate (tunable).

## Examples
- `gene=CRB1, disease="RP12", hpo=[HP:0000512, HP:0000662]` → `association=True, tier=DEFINITIVE, evidence=[…]`
  (CRB1→RP is textbook; LIRICAL already has this — your role here is just the citations).
- `gene=<2024-reported gene>, disease="atypical RP", hpo=[…]` → `association=True, tier=STRONG` **and LIRICAL
  scored it 0** → this is the valuable case: it gets flagged "literature-supported, LIRICAL missed → review".
- `gene=TTN, disease="RP", hpo=[…]` → `association=False, tier=NONE` (no IRD support) → dropped.

## Out of scope for this layer
- Ranking diseases / choosing the diagnosis (LIRICAL).
- Producing a probability or "confidence %" (LIRICAL).
- Symptom→disease matching itself (curated HPO/OMIM + LIRICAL).

## Practical notes
- **PaperQA2** (agentic, citation-grounded) is the right choice for this — its retrieve→rerank→cite loop is
  exactly what produces the grounded `evidence` list. Confirm the package version is PaperQA **2.x**.
- The consumer seam is already in the codebase: `tools/phenotype_dx.paperqa2_evidence(gene, disease,
  hpo_terms, runner=…)` and `reconcile(...)`. As long as your `runner` returns the dict above, it slots in
  with no changes on our side. ~~A placeholder returning `not_enabled` is wired now~~ → **a real runner
  over `deep_literature` is wired now** (`phenotype_evidence.make_deep_literature_runner`); the
  `not_enabled` placeholder remains only as the no-runner default. If you build a better grader, it
  replaces the runner and nothing else changes.
- Full design context: `docs/phenotype_gene_confidence_rag_spec.md`.
