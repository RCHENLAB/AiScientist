# Corpus operator runbook

Use [the native literature deployment guide](README.md) for the current runtime, model policy and opt-in local Docker modes. The former app `paperqa.sif` build and app CLI have been retired.

1. Prepare/download the public papers and `paperqa_manifest.csv` with the app's manifest/download helpers; keep credentials and patient uploads separate.
2. Stage the public corpus and embedding weights using the cluster's authorized transfer channel. Keep the embedding identifier identical for indexing and search.
3. On a compute allocation, run the native repository's `scripts/build_paperqa_directory_index.py` in the verified literature image; `embed_corpus.slurm` accepts `MCP_REPO`, `LITERATURE_MCP_SIF`, and `BASE`.
4. Set `AISCIENTIST_CORPUS_MAINTENANCE_SCRIPTS` to the absolute, non-symlink `scripts` directory of the reviewed MCP checkout on the gateway; the command stages its fixed cache builder and records its SHA-256. Rebuild/sync the mechanism cache with `scripts/update_corpus_index.py --rebuild`, then `--check`. A completed sync atomically switches `current`; `--rollback` selects the previous copy.
5. Validate real native `tools/list` and a bounded `search_corpus` call. Record the image digest, corpus version, output citations and whether a model participated. A retrieval-only test incurs no generative model calls.

The app's plan/cancel/curation/cache/publication flow stays in the gateway. A session-selected model is never replaced by an operator default. Standalone answer-mode maintenance requires explicit model settings and is separate from an app session's credentials/budget.
