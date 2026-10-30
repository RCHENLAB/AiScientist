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

Ranks the chunks of the corpus text cache (`mechanism_cache/chunks.jsonl`, vectors in `chunk_vecs.npy`) for 1-6 queries by meaning (PubMedBERT, on a CPU) and by their exact words (BM25), merges the two, and returns the passages verbatim. No language model runs on HPC3: the session's model writes the queries and, from the returned passages only, the answer. It needs no GPU allocation, and the user's API key never leaves the gateway.

## When the agent uses it

- In a literature plan (a general question the router sends to the literature line), next to PubMed: the question, with the exact words the model wrote for it as `terms`, in any session that reaches HPC3; the answer cites the passages with the abstracts.
- A literature step in a session that has no served model on HPC3 (the user's own API key), where `deep_literature` is not offered.
- Any time the model wants the passages themselves rather than a PaperQA answer.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `queries` | array of string | required | 1-6 focused questions or statements, each a full sentence; one call serves all of them |
| `top_k` | integer | `12` | passages returned in total, at most 30 |
| `max_per_paper` | integer | `3` | at most this many passages from one paper |
| `terms` | array of string | — | optional: up to 12 exact words or short phrases a passage that answers would contain, with their spelling variants (gene symbols, technical terms, e.g. 'dominant negative', 'ellipsoid zone'). Each is counted in the corpus and ranks the passages that contain it; the queries' own words are used anyway |
| `pmids` | array of string | — | restrict the search to these PMIDs (empty = the whole corpus) |
<!-- /generated:parameters -->

## Outputs

- `contexts`: one item per passage, with `citation`, the paper's `title`, `authors`, `year`, `journal` and `doi` (from the corpus manifest, when it has them), `pmid`, `page`, `chunk`, `text`, `score` (the fused rank score), `cosine` and the `query` that found it. The key and its shape are the ones `deep_literature` returns, so the References builder and the chat grounding read both.
- `terms`: every word and term used, with its `kind` (`gene` or `word`), `source` (`query` or `model`), `n_passages` (how many chunks hold it) and `use` (`filter`: rare, `rank`: common, `absent`: in no chunk), so the model can see what its words did.
- `failed` with a reason when no passage matched, never `ok` with an empty list.

## How the passages are chosen

- Each query ranks the chunks three ways, each list cut at 200: by cosine similarity with PubMedBERT, by BM25 over the query's own words (stop words dropped), and by BM25 over `terms` when the model gives them. The lists are merged by reciprocal rank (score = sum of 1 / (60 + rank)): a chunk near the top of any list comes up, one high on several comes first. The words are kept apart from `terms`, so the model's topic words cannot drown the question's own. The merged list then gives three passages for each one the embedding's own ranking gives, so the embedding's best three always stay in the top 12: the words rescue a passage the embedding ranks low, and a passage the embedding ranks first is not pushed out by words that merely occur in other passages.
- Words match the way papers write them: PDF ligatures ("haploinsufﬁciency") as letters, hyphenated and spaced spellings alike, word endings by stem ("haploinsufficient" finds "haploinsufficiency"). A gene symbol matches case-sensitively as a whole word; a capitalised abbreviation of the field (ERG, RPE, OCT, ...) or a token the HGNC table does not know is a plain word.
- A query that names a gene the corpus has is answered first from the chunks that name it, ranked among themselves; the rest of the corpus follows, so a gene few chunks name still gets a full list.
- The best remaining chunk of each query in turn, so one broad query cannot crowd out the others; at most `max_per_paper` per paper and `top_k` in all.
- A gene the model adds in `terms` ranks passages but does not put them first: only a gene the query itself names does.
- Why: a chunk's vector is its topic, so the one sentence that states the fact asked about barely moves it. On 24 genes the corpus reports as haploinsufficient, "Is X haploinsufficient?" found the evidence in its 12 passages for 13 by embedding alone, 22 with the words and 24 with the model's terms; on 32 other graded questions, answering passages in the 12 went from 2.1 to 3.2 per question with the terms, better on 20 and worse on 2 (`docs/FINDINGS.md`, Validation results).

## Where it runs

The gateway first uses its copy of the index on the Eye Server, when the copy and the offline
embedding model are there: the same retrieval, in a worker thread on the gateway's CPU, with one
embedding model loaded per process and no Slurm job. Otherwise the HPC3 job below runs, as it does
when the copy is stale: before a search the gateway compares the copy with the HPC3 index (the
sizes and modification times of its files and the manifest's checksum, read with one `stat` on the
login node at most every 5 minutes, `AISCIENTIST_LOCAL_CORPUS_CHECK_S`), and searches HPC3 while they
differ. A session that cannot reach HPC3 searches the copy as it is. Local retrieval can enable the
literature line without `AISCIENTIST_PAPERQA_ON_HPC`; the Corpus scan still needs its HPC3
configuration.

The copy lives in `<data root>/reference/retigene/` (`AISCIENTIST_LOCAL_CORPUS_ROOT` to change it):
`current` names the version searches read, `versions/<time>/` holds `mechanism_cache/`,
`manifest.csv`, `hgnc_complete_set.txt` and `SOURCE.json` (what was copied, from where, when), and
`hf_cache/` the embedding model. The gateway's environment needs the `corpus` extra
(`sentence-transformers`, with torch from the CPU index); on the Eye Server, `AISCIENTIST_EXTRAS` in
`config/deploy.env` adds it to every release.

To add papers, put the PDFs and their manifest rows in the corpus on HPC3, then on the Eye Server,
as the service account, with the running release:

```sh
sudo -u aiscientist /data/aiscientist/app/current/.venv/bin/python \
    /data/aiscientist/app/current/scripts/update_corpus_index.py --rebuild
```

It rebuilds the index on HPC3 in one CPU Slurm job inside `paperqa.sif` (only the new papers are
extracted and embedded; the result is checked and put in place, the previous index kept as
`mechanism_cache.prev`), then copies it: only the files that changed, through RCIC's transfer node
`access-hpc3.rcic.uci.edu` (RCIC allows no data transfer on the login nodes), into a new version
that becomes current in one step, so a search never reads a half-copied index. A copy that does not
have one vector per chunk is never made current. Without `--rebuild` it only copies; `--check` says
whether the copy is the HPC3 index; `--rollback` switches back to the previous version. It logs in
to HPC3 with `--hpc-user` and `--ssh-key`, by default the shared model server's login.
`scripts/sync_corpus_index.py` copies from any other source or into another folder.

<!-- generated:runs-on -->
As a Slurm job inside `paperqa.sif` on HPC3 (the PubMedBERT index lives on dfs3b), through `python -m aiscientist.tools.paperqa_cli`. Its job runs no language model: it needs no GPU allocation.

Slurm resources for its job: 2 CPUs, 8 GB, up to 00:10:00.

Needs: `sentence_transformers`.

Reads private data: no.

On the fast chat path: yes.
<!-- /generated:runs-on -->

## Known limits

- No LLM re-ranking: the merge is by rank only. A question whose answer is worded differently from it in every paper still depends on the embedding.
- Gene symbols are matched as written, not expanded to their HGNC aliases.
- New papers reach the index only when someone runs `scripts/update_corpus_index.py --rebuild`; nothing watches the corpus folder.
- A rebuild on HPC3 swaps the index with two renames: a search job that opens the index in that instant fails and must be asked again.
- Excerpts of the corpus papers go to the session's model provider as prompt text.

## Code and tests

- `tool.py`; the shared corpus loaders and the hybrid ranking (`TextIndex`, `hybrid_rank`) in `../_lib/corpus.py`; the job entry point is `../paperqa_cli.py`.
- The Eye Server copy: `../../gateway/local_corpus.py` (searching it) and `../../gateway/corpus_index.py` (copying it); `scripts/update_corpus_index.py`, `scripts/build_mechanism_cache.py`.
- Tests: `tests/test_search_corpus.py`, `tests/test_corpus_hybrid.py`, `tests/test_local_corpus.py`, `tests/test_local_corpus_route.py`, `tests/test_corpus_index.py`.

## What the model is told

<!-- generated:model-description -->
> Search the lab's curated literature corpus and get back the matching PASSAGES, verbatim, each
> with its PMID, page and citation. It does not write an answer: you read the passages and write
> it yourself. Give 1-6 focused questions or statements as full sentences (a bare list of gene
> symbols retrieves poorly), e.g. 'Does loss of one copy of PRPF31 cause autosomal dominant
> retinitis pigmentosa?', and optionally `terms`: the exact words a passage that answers would use
> (e.g. 'haploinsufficiency', 'one functional copy'); passages are ranked by meaning and by these
> words together. Cite ONLY a PMID that appears in the returned passages and quote only sentences
> that appear in them; anything else is removed from your answer. Returns status failed when the
> corpus has nothing, then use literature_search.
<!-- /generated:model-description -->

## Gene tables and saving results

In the console, a list question that names the RetiGene papers ("What are haploinsufficient genes in
RetiGene papers?"), or names no source at all ("What are haploinsufficient genes?"), is a curation of
the whole corpus on HPC3, with its own plan card and two tables (`gateway/corpus_scan.py`). This tool
serves the other questions through the literature line, a list question that names PubMed or the
literature among them.

A client that asks for the chat route (`route="chat"`, the API) gets, for a gene-list question, a
kept full corpus scan when there is one. Otherwise the session model extracts a table from the
passages this tool returns. Each kept row needs an exact verbatim evidence quote containing the gene
and a source PMID and page. The answer labels this as a corpus search rather than a complete scan
and keeps `literature/genes.csv` with the run. No verified rows means an empty table and an explicit
explanation, never a gene list from model memory.

**Save tables to my data** in Runs saves a run's tables as a dataset folder: a curation's two
tables; otherwise these gene tables, a scan's `haploinsufficiency/genes.csv`, or a literature
answer's `papers.csv`. The run's own downloads stay. Citation checks warn about uncited paragraphs;
they do not attempt to infer which individual sentences state paper facts.
