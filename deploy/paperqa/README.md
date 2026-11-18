# Native literature runtime and corpus deployment

The app uses the independently published [literature MCP](https://github.com/lijinbio/aiscientist-mcp/tree/main/packages/literature), pinned by its MCP catalog and matching instruction skill. PaperQA, embedding models and retrieval/extraction implementations are in that runtime. The app keeps corpus transfer/versioning, selected-model credentials and budget enforcement, plan review, curation specifications, result caches and publication.

## HPC deployment

Configure `LITERATURE_CORPUS_ROOT`, `LITERATURE_PAPERS`, `LITERATURE_MANIFEST`, `LITERATURE_CACHE_DIR`, `LITERATURE_HF_CACHE` and, when needed, `LITERATURE_INDEX_DIR` / `LITERATURE_EMBEDDING` in trusted server settings. Roots are read-only in an analysis; native job data belongs to the isolated run. The gateway stages the catalog's immutable image on an authorized compute allocation. It does not bind application scientific tools into that image.

Corpus scans use the selected session model through the guarded gateway relay (including supported native Anthropic text/JSON/tool requests) or the session's existing cluster endpoint. API credentials stay on the gateway. The pilot and each subsequent paid call enforce the approved cap; one already-started call may cross it. An unpriced standalone endpoint cannot claim a positive spending cap. The app does not provision a replacement GPU model for a scan.

## Local retrieval without HPC

Operators may pre-pull the same reviewed Docker digest and set `AISCIENTIST_LOCAL_CORPUS_MCP=docker` for offline corpus retrieval. Set `AISCIENTIST_LOCAL_CORPUS_ROOT` to the public corpus mirror containing `current`/`versions` (or a flat cache) and `hf_cache`. The container has read-only corpus/model mounts, no network, a private temporary output mount, and fixed CPU/memory/process limits. The app does not import scientific packages or load an embedding model in its process.

Public Europe PMC lookup is a separate opt-in: `AISCIENTIST_LOCAL_PUBLIC_LITERATURE_MCP=docker`. This fixed native command allows outbound networking for `literature_search`, mounts no corpus or model cache, and inherits no model key or endpoint override. It supports the ordinary cited literature conversation using the session's selected gateway model. Plain chat needs no HPC. Deployment must already permit this service account to use Docker; this code does not change permissions or mount the Docker socket.

## Maintenance

`AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME=mcp` is the only runtime. `scripts/update_corpus_index.py --rebuild` stages the small operator bundle into the published literature image; its reference roots must be configured explicitly. Set `AISCIENTIST_CORPUS_MAINTENANCE_IMAGE` to an immutable Docker URI or a verified absolute SIF to override the catalog for operator work. The transfer/check/rollback paths retain the original corpus versioning and no longer require app `corpus` or `literature` extras.

Manifest preparation/download helpers remain in this app. Scientific maintenance commands `build_paperqa_directory_index.py`, `query_paperqa_index.py` and `peek_paperqa_embeddings.py` now live in the native repository's `scripts/`. Run them inside the published runtime with that scripts directory mounted read-only. `embed_corpus.slurm` shows the offline CPU index-build command; supply the scheduler account and partition explicitly. Use the same embedding model for build and retrieval.

Final-image validation and historical limitations are recorded in the native package README and `docs/development/issue94_validation.md`; a protocol pass alone does not establish report grounding or exhaustive corpus coverage.
