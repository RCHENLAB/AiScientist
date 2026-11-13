# Literature planning and index maintenance

The gateway owns the query formatter and curation draft contract in
`aiscientist.literature_contract`. It validates and names a draft before scheduling
it; the literature server validates that draft again. PDF extraction, embeddings,
ranking, quote checks and curation remain in the scientific runtime. A fresh-process
regression blocks all four legacy tool packages, `_lib.corpus`, and the external
scientific package while importing the gateway and choosing its local Docker route.

The four legacy tools and unregistered deployment fallback remain until the real
website acceptance gate passes. No helper relocation is evidence that a full
curation or scientific parity gate has passed.

## Native operator maintenance

`build_mechanism_cache.py` and `eval_corpus_retrieval.py` are operator commands,
not model-callable MCP tools. Set `AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME=mcp`
**inside the published literature image**. Their lazy loader uses the installed
`aiscientist_mcp.literature` implementation; do not install that scientific package
into the gateway. Native mode never falls back to the app's old tools. The scripts
still support explicit `legacy` mode for deployments before cutover.

For an HPC index rebuild, set these in the operator's deployment environment:

```sh
export AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME=mcp
# Omit this when the app already registers the pinned literature server image.
export AISCIENTIST_CORPUS_MAINTENANCE_IMAGE='docker://ghcr.io/liujs123456/mcp-literature@sha256:655ad065365672a67b8b6e8ba3f1cc71a7afde141e28ed47b73f17f9eb1c148b'
python scripts/update_corpus_index.py --rebuild
```

The update command preserves the existing CPU Slurm submission, corpus settings,
`.next`/`.prev` index replacement, transfer-node sync, rollback and fingerprint
checks. In native mode its archive contains only the build script and its runtime
loader. The job binds the corpus writable and staged scripts read-only, disables
network access and uses the existing offline embedding cache. It does not stage
the app or its old scientific tool folders into the container. An explicitly
configured local SIF can also be used; the operator must verify it against the
published image pin before use. Run compute through Slurm, not on a login node.

For manual evaluation, mount this checkout read-only into that exact image, plus
read-only corpus/model files and a private writable output directory. Run the
existing `scripts/eval_corpus_retrieval.py` arguments with the runtime setting
above and `LITERATURE_CORPUS_ROOT` set to the mounted corpus root (add any separate
read-only input roots to `LITERATURE_ALLOWED_ROOTS`). The scheduled rebuild sets
these bounds from its trusted corpus paths automatically. This preserves dense/BM25/hybrid comparisons and separate enumeration
metrics; it does not change the production retrieval default.

## Public search depth measurement

`measure_pubmed_depth.py` retains its frozen eight-hit baseline. With the same
native runtime setting, its 100-hit path uses actual `tools/list` and
`literature_search` MCP calls in the published image. It enables abstracts and
PubMed-only results, uses fresh scratch per call, and restores full results from
confined evidence when the protocol response is a bounded preview. Native timings
include process and MCP transport startup, so the JSON records the selected
runtime. The command needs public Europe PMC network access and uses no language
model. Keep this distinction when comparing timings with historical in-process
results.

### Depth-measurement operator bundle

The image does not contain the app. Mount a read-only checkout, or stage only these
five files with the same relative layout into a read-only operator directory:

- `scripts/measure_pubmed_depth.py`
- `scripts/_literature_runtime.py`
- `src/aiscientist/literature_contract.py`
- `src/aiscientist/agents/literature_answer.py`
- `src/aiscientist/agents/loop_utils.py`

The loader imports these fixed platform source files in an isolated module namespace;
it never executes `aiscientist.__init__` or `aiscientist.agents.__init__`. Thus the
measurement uses the same twelve-paper merge, DOI/PMID de-duplication and reference
selection as the application, without importing its tool catalog or scientific
packages. A missing helper fails with a staging error rather than silently changing
the measurement. Keep all five files from the same app revision.

For example, on an allocated compute node with an already verified image:

```sh
apptainer exec --bind /path/operator:/operator:ro /path/literature-pinned.sif \
  env AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME=mcp \
  python3 /operator/scripts/measure_pubmed_depth.py 'PRPF31 deletion'
```

This public-search benchmark needs network access. It neither rebuilds nor writes
the shared corpus. Its private per-call MCP scratch is removed after reading full
results. Native index rebuilding remains a separate offline operation.

## Remaining cutover work

This preparation keeps legacy runtime targets and the legacy operator default for
unregistered deployments. After the website acceptance gates pass, retirement must
remove those legacy targets and tool exports, switch the operator default together
with the default MCP catalog, and migrate or retire the old CLI/source-bundle tests.
Do not infer full retirement from import isolation or synthetic operator smoke.
