# Corpus retrieval evaluation

The default stays **hybrid** until lab-reviewed results justify a change. The evaluation compares the current
hybrid (gene priority, separate question/model BM25 rankings, reciprocal-rank fusion, dense interleaving) with
embedding cosine only (`dense`) and BM25 only (`bm25`, query and model terms together, no gene priority).
The experimental keyword is internal to ranking; the LLM tool schema and production defaults are unchanged.
All three use identical corpus chunks, queries, input terms, top-k budget, round-robin selection, chunk deduplication
and per-paper caps. Scores are method-specific and should not be compared across methods.

Copy `examples/retrieval_eval/questions.template.json` and replace all placeholders with lab-reviewed questions
and labels. Remove the top-level and per-question `placeholder` flags; the script rejects placeholders.
The lab must establish expected hits, the correct corpus/index snapshot and the deployment revision.

Each question has a unique `id`, `category` (`gene-specific`, `mechanism`, `paraphrase`, `exhaustive-list`),
nonempty `queries` (executed verbatim) and optional `terms` (a list of strings). Labels are:

- `relevant_passages`: objects such as `{"pmid": "12345678", "chunk": 0}`. These identify passages by the
  `(pmid, chunk)` pair in `chunks.jsonl`; corpus pairs must be unique. Do not use the array index as the chunk ID.
- `expected_pmids`: strings identifying supporting papers; exhaustive-list coverage needs a complete reviewed set.
- `supporting_passages`: optional passage objects supporting the answer; defaults to `relevant_passages` if omitted.
  An explicit empty list means there are no passage-level support labels.

Passage precision is relevant retrieved passages / retrieved passages; passage recall is relevant retrieved /
labeled relevant passages. Supporting-paper recall is expected PMIDs retrieved / expected PMIDs. Answer support
is a Boolean hit on any labeled supporting passage **or** expected paper. This measures available retrieval
support, not correctness of a generated answer. Missing/empty reference sets are unscored (`null`), excluded from
macro means; counts accompany each mean. Precision is zero when references exist but retrieval is empty.
Passage precision requires sufficiently complete relevance labels to interpret meaningfully.

Exhaustive-list questions run again at a larger enumeration budget, using the same per-paper cap; enumeration
coverage is expected PMIDs reached / expected PMIDs. It is reported separately from top-k answering. Ranking
depth defaults to the production 200 per query; enumeration depth is at least its requested budget. This is still
budgeted retrieval and is not proof of exhaustive corpus coverage. Gene-query regressions are separately listed
whenever hybrid has lower precision, passage recall, paper recall or answer support than either baseline.

On the corpus server, in the repo's Python environment with NumPy and the existing offline sentence-transformers
model cache available, run (replace paths with the lab's snapshot):

```bash
python scripts/eval_corpus_retrieval.py \
  --chunks /corpus/mechanism_cache/chunks.jsonl \
  --vectors /corpus/mechanism_cache/chunk_vecs.npy \
  --genes /corpus/hgnc_complete_set.txt --hf-cache /corpus/hf_cache \
  --questions /lab/retrieval_questions.json --output-dir /lab/retrieval_results \
  --top-k 12 --max-per-paper 3 --enumeration-budget 200 \
  --deployment-revision LAB_DEPLOYED_GIT_SHA
```

Alternatively corpus paths are resolved from the existing `AISCIENTIST_PAPERQA_*`, `AISCIENTIST_MECHANISM_CACHE`
and `AISCIENTIST_HGNC_TABLE` configuration. Explicit vector paths are loaded read-only, never rebuilt. Supply
`--embedding-model` if the index uses a different model; its identity must be confirmed by the lab, since `.npy`
files do not encode it. Models load offline only. Python callers/tests can inject `main(..., embed_fn=...)` or
`evaluate(..., embed_fn=...)` without any model dependency or download.

`results.json` contains per-question passages, rank, score, query, terms, metrics, method/category macro means,
gene regressions and provenance; `report.md` summarizes the same run. Provenance includes SHA256 hashes of
chunks, vectors, gene table and question set, chunk count, embedding model, checkout Git revision/dirty flag,
optional deployed revision, UTC timestamp, budgets and method definitions. Preserve this code revision/diff
with the results if running a dirty checkout. The JSON also includes the executed question set.

Query embeddings are computed once per question and reused, with the shared cost attributed to hybrid/dense;
BM25 embedding cost is zero. Ranking timing includes term preparation/selection but excludes model/index loading.
The fixed hybrid/dense/BM25 order shares a term cache, so timings after the first method are warm; enumeration
ranking has its own timing. Repeat runs on an otherwise idle server before drawing latency conclusions.
If proposing a future default change, record its evidence and rationale in method provenance; this evaluator
records no default change.
