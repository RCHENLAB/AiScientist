# Corpus retrieval evaluation

The scientific evaluator and its lab-label template now live in [aiscientist-mcp](https://github.com/lijinbio/aiscientist-mcp/blob/main/docs/retrieval_evaluation.md). They preserve hybrid/dense/BM25 comparisons, passage and paper labels, enumeration coverage and provenance. A template is never scored as a validated benchmark.

The app retains a thin `scripts/eval_corpus_retrieval.py` operator entry point. Set `AISCIENTIST_CORPUS_MAINTENANCE_SCRIPTS` to the absolute non-symlink `scripts` directory of the reviewed MCP checkout, and run inside the matching published literature image with read-only inputs and private writable outputs. The app does not import scientific packages or install embedding dependencies. Use the native repository's explicit flags or `LITERATURE_*` settings; see [maintenance](development/literature-maintenance.md).
