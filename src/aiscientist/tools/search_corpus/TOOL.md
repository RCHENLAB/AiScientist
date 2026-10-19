---
name: search_corpus
summary: Retrieval over the literature corpus without a language model: returns the matching passages, verbatim, with PMID, page and citation, for the session's model to answer from.
category: literature
runs_on: hpc:literature
order: 411
chat: true
line: literature
owner: literature
cpus: 2
mem_gb: 8
time_limit: "00:10:00"
---
# search_corpus

Embeds 1-6 queries with PubMedBERT on a CPU, takes the nearest chunks of the corpus text cache (`mechanism_cache/chunks.jsonl`, vectors in `chunk_vecs.npy`) by one matrix product, and returns them verbatim. No language model runs on HPC3: the session's model writes the queries and, from the returned passages only, the answer. It needs no GPU allocation, and the user's API key never leaves the gateway.

## When the agent uses it

- A literature step in a session that has no served model on HPC3 (the user's own API key), where `deep_literature` is not offered.
- Any time the model wants the passages themselves rather than a PaperQA answer.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `queries` | array of string | required | 1-6 focused questions or statements, each a full sentence; one call serves all of them |
| `top_k` | integer | `12` | passages returned in total, at most 30 |
| `max_per_paper` | integer | `3` | at most this many passages from one paper |
| `pmids` | array of string | — | restrict the search to these PMIDs (empty = the whole corpus) |
<!-- /generated:parameters -->

## Outputs

- `contexts`: one item per passage, with `citation`, `pmid`, `page`, `chunk`, `text`, `score` and the `query` that found it. The key and its shape are the ones `deep_literature` returns, so the References builder and the chat grounding read both.
- `failed` with a reason when no passage matched, never `ok` with an empty list.

## How the passages are chosen

- Cosine similarity of the query with each chunk. A chunk that contains a gene symbol written in the query as a word gets +0.05 (at most +0.10).
- The best remaining chunk of each query in turn, so one broad query cannot crowd out the others; at most `max_per_paper` per paper and `top_k` in all.

## Where it runs

<!-- generated:runs-on -->
As a Slurm job inside `paperqa.sif` on HPC3 (the PubMedBERT index lives on dfs3b), through `python -m aiscientist.tools.paperqa_cli`. Its job runs no language model: it needs no GPU allocation.

Slurm resources for its job: 2 CPUs, 8 GB, up to 00:10:00.

Needs: `sentence_transformers`.

Reads private data: no.

On the fast chat path: yes.
<!-- /generated:runs-on -->

## Known limits

- The ranking is the embedding alone (plus the gene-symbol boost); PaperQA's LLM re-ranking is absent.
- The cache is rebuilt by hand (`scripts/build_mechanism_cache.py`) when papers are added.
- Excerpts of the corpus papers go to the session's model provider as prompt text.

## Code and tests

- `tool.py`; shared corpus loaders in `../_lib/corpus.py`; the job entry point is `../paperqa_cli.py`.
- Tests: `tests/test_search_corpus.py`.

## What the model is told

<!-- generated:model-description -->
> Search the lab's curated literature corpus and get back the matching PASSAGES, verbatim, each
> with its PMID, page and citation. It does not write an answer: you read the passages and write
> it yourself. Give 1-6 focused questions or statements as full sentences (a bare list of gene
> symbols retrieves poorly), e.g. 'Does loss of one copy of PRPF31 cause autosomal dominant
> retinitis pigmentosa?'. Cite ONLY a PMID that appears in the returned passages and quote only
> sentences that appear in them; anything else is removed from your answer. Returns status failed
> when the corpus has nothing, then use literature_search.
<!-- /generated:model-description -->
