# Literature planning and index maintenance

The gateway owns the query formatter and curation draft contract in
`aiscientist.literature_contract`. It validates and names a draft before scheduling
it; the literature server validates that draft again. PDF extraction, embeddings,
ranking, quote checks and curation remain in the scientific runtime. A fresh-process
regression blocks all four legacy tool packages, `_lib.corpus`, and the external
scientific package while importing the gateway and choosing its local Docker route.

The application owns planning, model-session relay, transfer, cancellation and publication. All seven literature/phenotype scientific tools execute through registered native MCP servers. Local no-HPC search is an explicitly enabled Docker deployment with the same pinned literature image; public lookup and offline corpus retrieval have separate network policies. Scientific and report validation are separate gates; see the issue94 validation record for actual outcomes.

## Native operator maintenance

`build_mechanism_cache.py` and `eval_corpus_retrieval.py` are fixed operator shims, not model-callable MCP tools. Set `AISCIENTIST_CORPUS_MAINTENANCE_SCRIPTS` to the absolute, non-symlink `scripts` directory of a reviewed MCP checkout. Their algorithm implementations and dependencies live in that repository. Run the shim **inside the published literature image**, or let the gateway's update command stage the fixed cache builder. Missing configuration fails before compute submission. `AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME=mcp` is the only runtime; there is no legacy fallback and no server-package installation in the gateway.

For an HPC index rebuild, set these in the operator's deployment environment:

```sh
export AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME=mcp
export AISCIENTIST_CORPUS_MAINTENANCE_SCRIPTS=/path/to/reviewed/aiscientist-mcp/scripts
export AISCIENTIST_HPC_HOST=login.cluster.example.edu
export AISCIENTIST_HPC_TRANSFER_HOST=transfer.cluster.example.edu
export AISCIENTIST_CPU_PARTITION=standard
export AISCIENTIST_CPU_ACCOUNT=your_cpu_charge_account
export AISCIENTIST_PAPERQA_PAPERS=/shared/corpus/papers
export AISCIENTIST_PAPERQA_MANIFEST=/shared/corpus/paperqa_manifest.csv
export AISCIENTIST_LOCAL_CORPUS_ROOT=/data/reference/corpus-copy
# Omit this when the app already registers the pinned literature server image.
export AISCIENTIST_CORPUS_MAINTENANCE_IMAGE='docker://ghcr.io/lijinbio/mcp-literature@sha256:05ab531fb89a886e43af07d9730252c0db81a7b7e52c46b5f81bc457f53460ad'
python scripts/update_corpus_index.py --rebuild \
  --hpc-user your_cluster_user --ssh-key /path/to/operator_key
```

Use an existing operator-authorized SSH identity; this command does not provision keys or grant access. `--hpc-root` can replace the remote corpus-root settings, and `--destination` can replace `AISCIENTIST_LOCAL_CORPUS_ROOT`. Keep the reviewed external checkout revision with the operator evidence. These `AISCIENTIST_PAPERQA_*` names are retained only as platform transfer inputs; inside the image the staged job sets the corresponding bounded `LITERATURE_*` references.

The update command preserves the existing CPU Slurm submission, corpus settings,
`.next`/`.prev` index replacement, transfer-node sync, rollback and fingerprint
checks. In native mode its archive contains only the app transport shim/runtime loader and the reviewed external cache builder with its SHA-256 manifest. The job binds the corpus writable and staged scripts read-only, sets offline Hugging Face/Transformers resolution, and uses the existing embedding cache. It does not impose container-level network isolation on the cluster. It does not stage
the app or its old scientific tool folders into the container. An explicitly
configured local SIF can also be used; the operator must verify it against the
published image pin before use. Run compute through Slurm, not on a login node.

For manual evaluation, mount this checkout read-only into that exact image, plus
read-only corpus/model files and a private writable output directory. Also mount the reviewed MCP `scripts` directory read-only and set `AISCIENTIST_CORPUS_MAINTENANCE_SCRIPTS` to that **container-visible** path, not its host path. The fixed external script must remain a regular file; the wrapper refuses symlinked ancestors and unknown operator names. Run the
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

## Validation scope

Import isolation, maintenance unit tests, and synthetic operator smoke do not establish scientific or report acceptance. Keep the frozen native-image tests, selected-session-model website results, and any failed or corrected report attempts in the validation record. Never rebuild or move an immutable image tag for an operator-script-only update.
