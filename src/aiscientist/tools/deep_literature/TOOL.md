---
name: deep_literature
summary: Answers a scientific question from the literature with PaperQA2 and returns a cited answer with its evidence.
category: literature
runs_on: hpc:literature
entry: tool:make_paperqa_tool
chat: true
order: 410
line: literature
owner: literature
---
# deep_literature

Runs PaperQA2 (search, gather evidence, synthesize) over a local paper index and returns an answer with the passages it rests on. The index and the embedding model live on HPC3, so the work runs there, calling the session's served model.

## When the agent uses it

- When a step needs an answer with evidence rather than a list of papers.
- As the literature track of `diagnose_disease`.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `question` | string | required | a focused, answerable scientific question |
<!-- /generated:parameters -->

## Outputs

- A cited answer and the supporting contexts.

## Where it runs

<!-- generated:runs-on -->
As a Slurm job inside `paperqa.sif` on HPC3 (the PubMedBERT index lives on dfs3b), through `python -m aiscientist.tools.paperqa_cli`, calling the session's served model. It is left out of the catalog when the session has no GPU allocation.

Needs: `paper-qa`.

Reads private data: no.

On the fast chat path: yes.
<!-- /generated:runs-on -->

## Known limits

- Minutes, not seconds. Since 2026-09-30 its LLM calls use a timeout sized for a reasoning model and a low reasoning effort (`ddf043c`); before that they timed out on Qwen3.8.
- It needs the session's GPU allocation (it calls the served model from HPC3), so it is absent in sessions that use only an external API.

## Code and tests

- `tool.py`: the PaperQA configuration and the record; the job entry point is `../paperqa_cli.py`.
- Tests: `tests/test_paperqa_search.py`.

## What the model is told

<!-- generated:model-description -->
> Answer a focused scientific question against the published literature with a grounded, CITED
> answer (PaperQA2 deep RAG: search -> gather evidence -> synthesize). Use this — not
> literature_search — when you need an actual answer with evidence (e.g. 'Is RHO downregulation
> linked to photoreceptor apoptosis in retinitis pigmentosa?'), not just a list of papers. Returns
> a cited answer plus the supporting contexts. Cite ONLY what it returns. Heavier/slower than
> literature_search.
<!-- /generated:model-description -->
