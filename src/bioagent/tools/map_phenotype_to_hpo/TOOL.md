---
name: map_phenotype_to_hpo
summary: Turns free-text clinical notes into validated HPO term IDs, observed and excluded, each traced to its source phrase.
category: annotation
runs_on: inprocess
entry: tool:make_hpo_mapping_tool
chat: true
order: 510
line: phenotype
owner: phenotype
---
# map_phenotype_to_hpo

Converts a clinical description in any language (a referral note, a diagnosis line, a symptom list) into HPO term IDs. The model extracts the findings and whether each is present or absent; the bundled HPO index owns the IDs, so only terms that exist in the release come back, each with the phrase it came from.

## When the agent uses it

- Before `run_lirical` or `diagnose_disease`: it is the only sanctioned way to get HPO IDs. A case note attached to the run takes precedence over the `text` argument.
- On the fast chat path.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `text` | string | — | the patient's clinical description / diagnosis / symptom list, VERBATIM — copy the clinician's own words, do not paraphrase or translate them yourself (e.g. 'night blindness since childhood, constricted visual fields, nonrecordable ERG, no hearing loss'). Omit ONLY when a case note is attached to the run, in which case the attached note is mapped. |
<!-- /generated:parameters -->

## Outputs

- `hpo_terms` (observed), `excluded_hpo` (findings the text says are absent), `unmapped` (phrases it could not match) and `text_source`.

## Where it runs

<!-- generated:runs-on -->
In the gateway process; it returns in seconds and needs no HPC3 job.

Reads private data: yes; its results stay inside the run.

On the fast chat path: yes.
<!-- /generated:runs-on -->

## Known limits

- Phrase extraction needs the session's local model. Without one (an external-API-only session) it returns `mode=needs_llm` and no terms.
- The HPO release is bundled (`hpo_lexicon.tsv.gz`, built by `scripts/build_hpo_lexicon.py`); a newer LIRICAL data release can contain terms it does not know.

## Code and tests

- `tool.py`: extraction, matching and the record; `index.py`: the HPO index; `__init__.py`: the bundled IRD table and `infer_hpo_terms`.
- Tests: `tests/test_hpo_mapper.py`, `tests/test_hpo_terms.py`, `tests/test_case_note.py`.

## What the model is told

<!-- generated:model-description -->
> Convert a patient's clinical description in FREE TEXT (any language — a referral note, a
> diagnosis line, a symptom list) into validated HPO term IDs for run_lirical. ALWAYS use this
> instead of writing HPO IDs yourself: HPO IDs one digit apart are different real phenotypes, so a
> guessed ID silently produces a wrong differential. This tool extracts each finding, matches it
> against the real HPO release, and returns ONLY IDs that exist in the ontology, each with the
> phrase it came from. Returns `hpo_terms` (observed, pass straight to run_lirical) and
> `excluded_hpo` (findings the text says are ABSENT, e.g. 'no hearing loss'), plus `unmapped` for
> phrases it could not match — report those rather than substituting your own IDs.
<!-- /generated:model-description -->
