---
name: diagnose_disease
summary: Combines LIRICAL's probabilities with literature evidence into one adjudicated ranking of candidate diseases.
category: annotation
runs_on: inprocess
entry: tool:make_diagnose_disease_tool
needs: [literature_fn=tool:deep_literature, lirical_fn=tool:run_lirical]
order: 530
line: phenotype
owner: phenotype
---
# diagnose_disease

The full differential. It takes LIRICAL's ranking and the literature evidence (PaperQA2 through `deep_literature`) and returns one ranked list. When the two disagree, the literature is weighted higher and the candidate is flagged `agreement='conflict'`; when LIRICAL cannot answer (not staged, an error, or a gene not curated into OMIM/HPOA), the differential is built from the literature instead of coming back empty.

## When the agent uses it

- Whenever the actual diagnosis is wanted rather than LIRICAL's raw output; with `candidate_genes` from the variant shortlist.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `hpo_terms` | array of string | required | observed HPO term IDs for the patient's phenotype (required), e.g. ['HP:0000510','HP:0000662'] |
| `excluded_hpo` | array of string | — | HPO term IDs the patient explicitly does NOT have (optional) |
| `candidate_genes` | array of string | — | gene symbols from the variant shortlist to ask the literature about even if LIRICAL never scored them (this is the gap-fill path), e.g. ['CRB1','ABCA4'] |
| `max_literature_queries` | integer | — | cap on literature queries, default 6 (each is a full RAG loop) |
| `sample_id` | string | — | the proband's sample id in the VCF (optional) |
| `vcf_path` | string | — | VCF for genotype-aware scoring (defaults to the run's dataset) |
<!-- /generated:parameters -->

## Outputs

- One ranked list of candidate diseases with each track's evidence and the agreement flag.

## Where it runs

<!-- generated:runs-on -->
In the gateway process; it returns in seconds and needs no HPC3 job.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- The weights are design choices in `tool.py` (`LITERATURE_WEIGHT = 0.65`, `LIRICAL_WEIGHT = 0.35`).
- It composes the FINAL executors of `run_lirical` and `deep_literature` (`needs:` in the manifest), so it runs them on HPC3 exactly as they would run alone. Without `deep_literature` it degrades to the LIRICAL track.

## Code and tests

- `tool.py`: reconciliation, adjudication and the record; `evidence.py`: the literature evidence track (questions, grading).
- Tests: `tests/test_phenotype_dx.py`, `tests/test_phenotype_evidence.py`, `tests/test_registry.py` (binding to the routed executors).

## What the model is told

<!-- generated:model-description -->
> FULL differential diagnosis: combines LIRICAL's calibrated post-test probability with the
> published literature (deep_literature / PaperQA2) and returns ONE ranked list. Prefer this over
> run_lirical whenever you want the actual diagnosis rather than LIRICAL's raw output. It is the
> only path that still answers when LIRICAL cannot: if LIRICAL is not staged, errors, or the gene
> is not curated into OMIM/HPOA, the differential is built from the literature instead of coming
> back empty. When the two tracks DISAGREE the literature is weighted higher (a cited, retrieved
> refutation outranks a curated call, because the curation lags the literature) and the candidate
> is flagged `agreement='conflict'`. Get `hpo_terms` from map_phenotype_to_hpo — never write HPO
> IDs from memory. Pass `candidate_genes` from the variant shortlist (annotate_variants): genes
> LIRICAL did not score are exactly where the literature track earns its keep. Each returned
> candidate carries `final_score`, `agreement` (concordant | conflict | literature_only |
> lirical_only | unsupported), `decision_note`, LIRICAL's untouched `posttest_prob`, and the
> ClinGen `evidence_tier` + PMIDs. `final_score` is a RANKING score, NOT a probability — quote
> `posttest_prob` when you need a calibrated number, and cite only the PMIDs returned.
<!-- /generated:model-description -->
