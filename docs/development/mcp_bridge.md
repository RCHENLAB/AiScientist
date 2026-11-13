# Pinned MCP client bridge

`agents/mcp_bridge.py` registers tools from reviewed MCP `tools/list` snapshots and
calls them through the server image's `aiscientist-mcp` CLI. The gateway needs no
MCP or analysis packages. Registration reads local configuration and schemas; it
does not start a server, discover tools over the network, or download a catalog.

This client bridge implements the transport for [issue #94](https://github.com/RCHENLAB/AiScientist/issues/94).
The default registry includes Atlas and the seven released analysis servers below.
Registration still requires the matching installed skill and an appropriate executor.
CPU, GPU and `inprocess` declarations
use the pinned image executor and the skill's policy; `AISCI_EXECUTOR=inline` makes
the server finish in that allocation. This does not import server code on the
gateway or start nested Slurm jobs. Model-backed tools use the selected session's
model through trusted runtime settings and the guarded relay described below.
The server transport is stdio; authenticated remote MCP HTTP is not implemented.

| Server | Package version | Replaced scientific tools |
| --- | --- | ---: |
| `scrna-analysis` | `0.0.5` | 11 |
| `cellqc` | `0.0.5` | 1 |
| `literature` | `0.0.4` | 4 |
| `variant-annotation` | `0.0.2` | 1 |
| `phenotype-diagnosis` | `0.0.4` | 3 |
| `scgpt-annotation` | `0.0.3` | 1 |
| `data-utilities` | `0.0.2` | 2 |

The seven releases are published in `liujs123456/aiscientist-mcp` and paired with
`liujs123456/aiscientist-skills` `v0.2.5`. The registry records the exact OCI digest,
schema checksum and resource policy for every server; version tags alone are
never executed. Atlas retains its upstream `0.0.3` pin. Changing the release
owner during upstream promotion requires reviewing actual published assets and
updating both the server registry and skills release together.

## Registry and tool schemas

`src/aiscientist/agents/mcp_catalog/mcp_servers.json` is the default registry. It and its snapshots ship as Python package data, so
non-editable wheel and Docker installs use the same reviewed defaults as a source
checkout. The base package declares `jsonschema`; no gateway extra is needed to
load the catalog or System overview. The registry is loaded once per version of its file.
A registry that cannot be used (invalid, unreadable, or without `jsonschema`) leaves the MCP
tools out and is logged; runs, the System page and every other tool are unaffected, and Atlas's
shell entry point stays closed. A deployment can set
`AISCIENTIST_MCP_CONFIG` to another local registry. Its top-level fields are
`schema_version: 1` and `servers`, an array of server records:

| Field | Contract |
| --- | --- |
| `name` | Unique server identifier. |
| `version` | Fixed package version, such as `0.0.3`; moving version labels are refused. |
| `release_tag` | Must equal `<name>-v<version>`. |
| `image` | A `docker://` image reference with a full SHA-256 digest. Tags alone are refused. |
| `entrypoint` | A single executable name, such as `atlas-expression-exploration-mcp`; no shell command or arguments. |
| `skill` | Installed, allowed skill that supplies the existing environment and access policy. One server per skill binding. |
| `runs_on` | `hpc:<name>` and `inprocess` use the contained CPU image runner; `gpu:scgpt` selects its configured GPU allocation. The model cannot select a partition or account. |
| `tools_file` | Relative path to a tool snapshot confined to the registry directory. |
| `tools_sha256` | SHA-256 of the exact snapshot bytes, checked before registration. |
| `adapter` | `json` by default; `atlas-expression` is required for Atlas, `retigene` for retigene-rag, and `analysis` for the seven known analysis servers. |
| `prefix` | Optional prefix added to planner tool names; the original name is sent to MCP. Default: empty. |
| `aliases` | Optional original-name to planner-name mapping, applied before `prefix`. Wire names and resource keys remain unchanged. |
| `timeout` | Optional CLI call timeout in seconds, from 1 to 86400. Default: 15000. |
| `settings_env` | Optional list of approved nonsecret deployment environment names for an analysis server. Values come from the gateway deployment, never from model arguments or registry values. |
| `resources` | Optional mapping from original tool name to `[cpus, mem_gb, time_limit]`. Omitted tools inherit the skill's resources. |

Each snapshot is a JSON array of `{name, description, inputSchema}` objects.
Schemas must describe objects and pass JSON Schema Draft 2020-12 validation.
References must be local fragments; schema loading cannot resolve network or
filesystem references. Duplicate server names, skill bindings and planner names
are rejected. A server whose planner names collide with existing planner tools is
left out of the catalog and logged. The gateway plan
step `write_report` is reserved; a server with that wire name must use an alias
or prefix. MCP tool-to-skill bindings also supply the installed skill's
`exploration` and `writes-report` declarations to the planner.

The planner gets the catalog description and a copy of the original input schema.
The generic bridge preserves its semantics, including `dependentSchemas`, branches
and permissive additional properties. Atlas retains its existing domain restriction
on unknown arguments; analysis adapters also reject unknown arguments. Calls are validated against the complete schema before
scheduling, including required arguments and types.

The planner's parameter list is the schema's top-level `properties`. Plan settings
fix part of a future call, so each setting is checked on its own
(`agents/tool_schema.py`): its value against its property's schema (or a matching
`patternProperties` or `additionalProperties` schema), with local `$defs` references,
and its name against the declared properties when `additionalProperties` is `false`.
Supplied nested objects and arrays are complete values. Rules that relate several
arguments (`required`, `dependentRequired`, `dependentSchemas`, a root `if`/`then`,
`allOf`, `anyOf` or `oneOf`) are not checked at plan time: execution always validates
the full call with the original schema before a job is scheduled.
MCP plan settings retain their JSON types; unquoted call-syntax JSON literals and
the Python scalar spellings `True`, `False`, and `None` are parsed without evaluating
code. Non-MCP tools keep their existing plan coercion and
validation behavior. Tool names with hyphens or a leading underscore work in plan
classification, call syntax, edits and scope matching. Arguments
must also serialize as strict JSON, so NaN and Infinity are rejected. Tool names,
descriptions and results do not select executables, images, resources, imports or
access policy. There is no automatic registration of a server's other tools or
execution of its instructions.

## Execution and results

Registration requires both an image runner and an allowed, installed skill whose
image string exactly matches the registry's digest-pinned image. A mismatch disables
that server and logs the reason. `run_in_environment` checks the binding again at
call time after refreshing skills. Direct model calls to `run_in_environment` for
a registry-bound skill are refused and directed to its typed MCP tools.
Atlas's direct shell entry remains forbidden even if a custom registry omits
Atlas. Its server and skill binding must use the Atlas adapter, so configuration
cannot replace its dataset checks with the generic JSON adapter. The same holds for
retigene-rag and its skill `query-retigene-papers-with-aiscientist-mcp`, which must
use the `retigene` adapter.

The bridge constructs the command from trusted configuration and shell-quotes the
tool's JSON arguments. The existing executor owns the allocation, image
provisioning, dataset mounts, artifact transfer and the skill's declared network,
secret and other access restrictions. The bridge does not add gateway fallback or
permit the model to override job resources.

Inside the container, `AISCI_DATA_DIR=$AISCIENTIST_ENV_DIR` preserves server state
across this run's calls. `AISCI_EXECUTOR=inline` forces any job submitted by the
server to finish in the outer allocation. The bridge does not launch another
Slurm scheduler or configure `AISCI_SLURM`/`AISCI_JOB_PREFIX` inside that container.

The released common CLI starts the configured server over stdio and calls one
tool. An MCP tool error produces a nonzero CLI exit, which the executor reports
as an error. Successful output may be a structured JSON object or text blocks;
JSON text is decoded only when it represents an object; scalar, array and plain
text remain the original ordered content blocks. Malformed
responses fail instead of being reported as success.

The default JSON adapter keeps server data under `result`, separate from execution
status, artifact metadata and provenance. It writes the full result to
`artifacts/mcp/<server>/<tool>.json`, replacing that tool's prior result in the run.
A result exceeding 14,000 serialized characters is replaced in the transport
response by `response_omitted` and `full_result`. Arbitrary file paths returned by
a server are not followed or copied by this adapter. Servers with other
deliverables need an application-owned adapter. Such an adapter may also bind
folders the deployment names read-only into the job, and run something for the
job's duration; the model can set neither.

## retigene-rag

The `retigene` adapter (`agents/retigene.py`) serves the RetiGene papers' search and
curation tools. The registry does not list retigene-rag yet: a row needs the released
image digest and its tools snapshot.

- **Index.** `AISCIENTIST_RETIGENE_INDEX` names the index's HPC3 folder
  (`mechanism_cache/sentences.jsonl`, `chunks.jsonl`, `chunk_vecs.npy`, `manifest.csv`,
  `hgnc_complete_set.txt`). It is bound read-only into every call's job. Without it,
  the tools are not offered.
- **Model.** `submit_curation` reads with the session's own model. The job writes each
  prompt to a folder under the run's work directory (`RETIGENE_PROVIDER=relay`). For the
  job's duration, the gateway answers each prompt with the session's lab-role completion,
  role `curation` (`gateway/mcp_model_relay.py`). That covers the user's API key, the lab's
  shared model and the session's GPU. Each call is recorded in the user's usage as any lab
  call is. On a user's key, the curation's `max_cost_usd` and the relay's cap are what is
  left of their budget, and a spike allowance is announced for the key while the job runs.
  No key and no model address enters the job.
- **Cache.** Model replies are kept in the user's tool cache
  (`$AISCIENTIST_TOOL_CACHE/retigene-rag/cache`), so a question asked again reads nothing
  twice.
- **Outputs.** The collector copies a curation's `genes.csv`, `sentences.csv` and
  `report.md` to `artifacts/retigene/<curation_id>/` and names every server path by where
  the run keeps it.
- **Bounds.** `search_sentences` pages are at most 40 lines, `get_evidence` at most 20 rows
  and `get_genes` at most 100 rows. A longer result is trimmed to fit the transport bound,
  says how much was left out, and is kept whole in `artifacts/retigene/<tool>.json`.
- **Reference.** `evaluate` scores only against the bundled curator reference.

Every call the server answers records the server name, package version, release tag, image
digest, schema checksum and `runs_on` in the run manifest's `mcp_servers` list. Refused calls
and failed jobs are not recorded there. Responses, failed ones included, carry this identity
under `mcp`. The existing environment path
records the skill, image and command hash.

## Atlas binding

The configured package is `atlas-expression-exploration` 0.0.3, release
`atlas-expression-exploration-v0.0.3`, with this image:

```text
docker://ghcr.io/lijinbio/mcp-atlas-expression-exploration@sha256:5e9b13da5d180e7983945a7f1f4182e4b12876d3ec8f59ed0a0aa7b43d041d8a
```

Its skill is `explore-atlas-expression-with-aiscientist-mcp`. The nine planner names
are `resolve_genes`, `submit_query`, `query_status`, `get_summary`,
`plot_dotplot`, `plot_violin`, `plot_featureplot`, `atlas_write_report` and `list_queries`.
`atlas_write_report(query_id)` calls the server's unchanged `write_report` tool to
produce a query's `report.md`. The separate gateway plan step `write_report`
requests the whole run's PDF/DOCX manuscript and is never dispatched to Atlas.
Transport and registration are generic; `AtlasAdapter` in
`agents/atlas_expression.py` owns these domain restrictions and outputs:

- The primary dataset must be a `.h5ad` matching a normalized absolute path in the
  deployment's `AISCIENTIST_ATLAS_FILES` allowlist. Entries are semicolon-separated
  `label=/absolute/path.h5ad` pairs; a label ending in `latest` is not accepted.
  Without a valid configured atlas these tools are not offered; direct shell
  access stays blocked with an explicit deployment-not-configured message.
  A session copy is also accepted when the executor's `dataset_source` is listed.
  An explicit `atlas` argument must name the attached dataset or that authorized
  source; after validation it is omitted from the call so the server reads its
  bound session copy through `ATLAS_H5AD`.
- `ATLAS_H5AD=$AISCIENTIST_DATASET` uses the attached dataset's read-only mount.
  Successful calls record the configured release label, resolved path, size and
  mtime in the manifest. Session copies also record `copied_from` in the manifest,
  without returning that source host path. Size and mtime are identity metadata,
  not a checksum.
- `submit_query` inherits the skill's 4 CPUs, 300 GB and `04:00:00`. The other
  eight tools have registry overrides of 2 CPUs, 16 GB and `00:30:00`. The outer
  executor schedules all calls; `submit_query` blocks under the inline executor.
- When the executor provides its user's tool cache, `ATLAS_HGNC` points to
  `$AISCIENTIST_TOOL_CACHE/atlas-expression/hgnc_complete_set.txt`. Without a usable
  cache the server keeps the table in this run's directory. The model cannot
  select a cache path or mount; the first download follows deployment network policy.
- `gene_resolution.tsv`, `summary_*.tsv`, PDF/PNG figures and `report.md` enter
  `artifacts/gene_expression/<query_id>/`. `query.h5ad`, logs and job state remain
  in work storage. The executor mirrors deliverables to the website.
- The existing flat Atlas result fields are retained, with execution status and
  provenance protected from replacement by server data. Failed query states are
  reported as errors. Report Markdown is omitted from the transport response;
  summary rows are bounded and point to `full_table`, with `rows_omitted` recorded.
  Paths under the server's output root are rewritten to `gene_expression/...`.
  `gene_expression/mcp_result.json` holds the response without report Markdown,
  is replaced each call, and supplies evidence for other oversized results.

## Analysis bindings and outputs

`agents/mcp_analysis.py` is the trusted adapter for `cellqc`, `scrna-analysis`,
`scgpt-annotation`, `variant-annotation`, `phenotype-diagnosis`, `literature` and
`data-utilities`. These names cannot use the generic adapter. Their planner prefix
must begin with `mcp_`, such as `mcp_run_cellqc`.
Wire names remain unchanged. Atlas's behavior and default pin are unchanged.

Inputs come from the executor's bound dataset, dataset folder and attached input
records. An explicit `dataset`, `dataset_paths`, `path`, `vcf_path` or `regions_bed`
must identify an exact attachment (path or unique attached name); the bridge never
accepts a new host path from the model. The server's `<PREFIX>_DATASET` and
`<PREFIX>_READ_ROOTS` are derived from the mounted `AISCIENTIST_*` bindings. CellQC
uses the bound folder root so raw and filtered matrices remain available together.
scRNA's `input` is a run-local AnnData or ranking filename.
`SCRNA_CHECKPOINT_ROOT=$AISCIENTIST_WORK` allows its worker to import the preceding
CellQC checkpoint. A changed upstream QC handoff requires a new scRNA `analysis_id`;
the server records imported checkpoint identities and refuses stale reuse. The attached clinical case note overrides a model-provided
phenotype mapping text. The app's detected `mcp_variant_assembly` overrides variant
assembly arguments, and offline annotation retains the whole-VCF `max_variants=0`
contract.

External references and models are deployment settings. `settings_env` admits only
explicit server-specific names listed in `mcp_analysis.py`, such as
`VARIANT_VEP_CACHE_DIR_GRCH38`, `SCGPT_MODEL_DIR`, `SCRNA_GENESETS_DIR`,
`LIRICAL_DATA_DIR`, `LITERATURE_CORPUS_ROOT` and `LITERATURE_MODEL_ENDPOINT`.
The bridge shell-quotes their current deployment values. Reference parents are
passed to the runner as trusted read-only mounts so sidecars work; mounting `/`
is refused. Environment names for credentials, executable commands, interpreters,
dataset bindings and read-root overrides are not accepted. Endpoint URLs cannot
embed credentials or query parameters. `settings_env` neither grants network
access nor supplies a secret. Operator credentials use the existing skill policy
and protected job-file staging; the collector redacts policy secret values from
JSON evidence as well as the returned response.

Model-backed tools resolve the selected session model once per call. A live
user-owned cluster allocation supplies its current endpoint. A saved API key or
shared gateway model uses `RelayBinding`: the job starts a token-protected
loopback chat facade and exchanges private request/response files with the
gateway. Provider credentials remain on the gateway, whose existing credential
proxy, key guard, data-boundary check, usage accounting and stop control apply to
every request. The requested model must match the bound session model; a global
deployment model never silently replaces it. The worker token is only a local
transport credential and is removed when the job exits.

The gateway also supplies a stable hashed model identity, so a fresh local relay
port does not invalidate an existing analysis. For a model with known pricing,
the trusted relay declares its usage protocol. Extraction receives the gateway's
token usage and per-call cost estimate, feeds the original pilot projection, and
stops if that projection exceeds `max_cost_usd`. The worker and gateway check the
running spend before each serialized call; the gateway also applies its configured
ceiling. A request already in flight can cross the threshold. Missing prices or
lost accounting stop subsequent priced calls, with unknown cost reported as null.
Unpriced endpoints continue to reject a positive cost cap.

The relay supports text messages, JSON-object requests, function tools and their
results, explicit tool choice, and buffered SSE responses. Unsupported API fields
are rejected. Native Anthropic uses provider-supported sampling and reasoning
settings, with adaptations surfaced by the gateway; JSON-object output is checked
after completion and an invalid object is a failed request with its real usage
still recorded. This is a bounded transport adapter, not a claim of compatibility
with every OpenAI API option.

The collector accepts `artifact_root` only inside
`$AISCI_DATA_DIR/<server>/<analysis_id>/`, refuses symlinks in sources and
destinations, and copies its regular files to
`$AISCIENTIST_ARTIFACTS/mcp/<server>/<analysis_id>/`. An explicit `checkpoint_path`
and CellQC's multi-species `checkpoints` may copy only regular `adata_*.h5ad` files
from that same analysis into `AISCIENTIST_WORK`. Failed results do not publish new
checkpoints. This is the bridge's only cross-server checkpoint handoff; arbitrary
returned paths are never followed.

Task-bearing calls retain full JSON evidence at
`mcp/<server>/<analysis_id>/tasks/<task_id>/<tool>.json`. Polling one task cannot
replace another task's evidence. A managed `<operation>.json` alias may advance
on a new scientific call; a status call refreshes only the alias already owned
by that same task. Untracked historical files are preserved. A server's
own `full_result` is read only when it identifies regular JSON within the same
analysis. Paths in the returned data and evidence are rewritten to artifact-relative
paths, checkpoint filenames or redacted attachment/server labels. Scientific prose
such as `(LCA3)/Retinitis` is preserved; a slash alone is not evidence of a server path.
Oversized responses retain a bounded view of scientific outcomes and a `full_result`
link. The adapter first removes duplicate flat fields and excess artifact navigation
before shortening evidence. A shortened view explicitly records omissions; missing
rows are not evidence of absence. Literature previews retain returned answers and
source identifiers, and phenotype previews preserve negative association findings.
Execution status and provenance remain protected. CellQC parameter declarations
identify supported fields separately from the values actually used by a completed task.
A server `status=error`, failed state or MCP error cannot become `status=ok` merely
because the containing process exited successfully.

Successful current outputs also populate the existing `tables/`, `figures/` and
`data/` report paths with real copies. A per-run ownership record tracks their
checksums: a rerun removes only its own unchanged stale copies, and polling an older
completed task cannot replace current report files or checkpoints. Namespaced
evidence remains available independently. Failed results do not replace those
successful compatibility outputs.

An explicit report-only follow-up can revise a finished run without rerunning
scientific tools. The normal plan review shows one `write_report` step targeting
the original run. Its durable review and prior report live under
`process/report_revisions/<revision_id>/`; the scientific plan and accepted run
state remain unchanged. The writer verifies each accepted full-result artifact's
task identity and checksum before constructing its report view. Historical
truncated claims are re-evaluated against those results. The session's selected
model, budget and cancellation controls also govern this revision.

CellQC's `result_directory` is separately confined and recursively inventoried.
Every regular file, including per-library matrices, is copied to the run's
`result/cellqc/`; links, traversal and special files are refused before publication.
The gateway's internal `data/cellqc_result.json` is rebuilt from that real inventory
and the verified run-local destination, enabling the existing ZIP, save and stage-out
flows. Only this internal pointer retains the destination's absolute path; public
MCP evidence uses relative paths. Nested result paths retain their directory structure
in downloads and saved datasets. Atomic export staging and rollback directories stay
on the result filesystem so separate Apptainer bind mounts do not cause cross-device
renames. Incomplete internal directories prevent stage-out rather than appearing as
valid scientific results.

## Dedicated literature routes and local retrieval

When a literature server is registered, the console's corpus curation and PubMed
answer routes use that same MCP image through `gateway/literature_mcp.py`. They
retain their plan/spec review, public-corpus cache, cancellation, curation tables,
and download layout. The selected session model is used for extraction, including
native Anthropic, the lab's shared model, or the session's existing cluster model.
No second GPU/model is provisioned implicitly. Paid scans pass the remaining cost
cap without rounding a small positive amount to zero. Failed pilots are recovered
through the same analysis's status tool so their measured spend and stop reason
remain available; a failed scan does not publish or cache completed tables.
PubMed and corpus retrieval use separate analysis identifiers because they run
concurrently. The complete synced JSON is read only from the expected tool and
analysis under this run's artifacts. Unregistered deployments keep their existing
routes during the migration acceptance period.

The local corpus mirror can use the published server without an HPC connection:

```bash
AISCIENTIST_LOCAL_CORPUS_MCP=docker
AISCIENTIST_LOCAL_CORPUS_ROOT=/srv/corpus/retigene
```

This is opt-in. The service user must already have Docker access, and an operator
must pre-pull the exact literature image digest in `AISCIENTIST_MCP_CONFIG`.
The installed skill must bind that same image. The root contains the versioned
`current/mechanism_cache/`, manifest and `hf_cache/` used by the existing mirror;
flat mirrors also work. A cache-only mirror need not contain the source PDFs.
The local runner invokes only `search_corpus`, verifies the actual stdio schema
against the reviewed snapshot, and uses the image's native MCP client/server.
It imports no scientific server code into the gateway.

The container has no network, credentials, home or Docker socket mount. Its root
filesystem and corpus/model mounts are read-only; only a private run directory
and bounded `/tmp` are writable. It runs as the service user's numeric UID with
2 CPUs, 8 GiB memory, 128 PIDs and a 10-minute timeout. Cancellation removes its
uniquely named container. The existing mirror-freshness check still decides when
HPC retrieval is preferred. The legacy local implementation remains available
when this option is unset, pending complete tool retirement.

The published literature 0.0.3 digest was exercised on the Eye Server using its
existing public mirror and offline PubMedBERT: live schema verification and a
PRPF31 retrieval returned two passages in 19.12 seconds with network disabled.
An in-flight cancellation left no owned container. This validates local retrieval,
not a generative model or a complete corpus extraction.

## Exporting and reviewing a catalog

The released common 0.0.2 CLI's `tools` command omits input schemas.
`scripts/export_mcp_tools.py` uses MCP `Client.list_tools` over stdio to export
the full descriptions and input schemas. It follows pagination, rejects duplicate
names and repeated cursors, and applies a total timeout. It initializes and lists
the server only; it does not call analysis tools.

For a release update, export from the released digest-pinned image when available:

```bash
image='docker://ghcr.io/lijinbio/mcp-atlas-expression-exploration@sha256:5e9b13da5d180e7983945a7f1f4182e4b12876d3ec8f59ed0a0aa7b43d041d8a'
apptainer exec "$image" python - --server atlas-expression-exploration-mcp \
  < scripts/export_mcp_tools.py > /tmp/atlas-tools.json
sha256sum /tmp/atlas-tools.json
```

Review the exported descriptions and schemas, the release's image digest, the
skill binding and any adapter requirements before updating the registry and its
checksum. Schema changes are reviewed code changes, not runtime discoveries.
The released CLI does not attest the live schemas on each call: keeping the
snapshot paired with the exact release image is part of this review contract.

The Atlas snapshot is the stdio MCP listing of the
[0.0.3 source release](https://github.com/lijinbio/aiscientist-mcp/releases/tag/atlas-expression-exploration-v0.0.3)
(`mcp==2.3.0`, `mcp-types==2.3.0`, `pydantic==2.14.2`). From that source checkout, with
`AISCI_EXPORT_SCRIPT` set to the absolute path of this application's exporter:

```bash
PYTHONPATH=packages/common/src:packages/atlas-expression-exploration/src \
uv run --isolated --no-project \
  --with mcp==2.3.0 --with mcp-types==2.3.0 --with pydantic==2.14.2 \
  python "$AISCI_EXPORT_SCRIPT" \
  --server 'python -m aiscientist_mcp.atlas_expression_exploration.server' \
  > /tmp/atlas-tools.json
```

### Explicit ordered workflows

A request such as `Run exactly these registered MCP tool calls in this order`,
followed by numbered `` `mcp_tool_name` {"argument": "value"} `` entries, is treated
as a structured plan input. Every name must belong to a configured analysis
adapter, every JSON object must be complete and unambiguous, and the live tool's
full input schema validates it before review. Unavailable tools fail planning.
This applies to the registered literature pipeline and other analysis servers;
ordinary literature questions still use the search/curation console.

The plan document stores all supplied settings directly, including nested arrays,
objects and multiline source code. The normal plan card still permits approval,
edits and cancellation. It does not add analyses or a report step to this literal
call list. During execution the approved settings are the complete argument
object: omitted fields retain server defaults, the model cannot add `rerun` or
change an omitted scientific setting, and another scientific tool is refused for
that step. Read-only metadata/status checks remain available. The session's
selected model, resource, billing, budget and cancellation paths are unchanged.
# Saved plans across tool retirement

Restoring a saved plan does not rename retired scientific tools automatically. A
pending step that names an unavailable migrated tool requires a replacement plan
and normal review. The error names a replacement only when the reviewed analysis
server registers that exact wire identity and its public name is in the live
catalog. Completed historical steps can still be reused without reinstalling the
old implementation. Their original settings and evidence remain unchanged.

Continuations restore the complete approved YAML document and planning context,
including nested JSON arguments and explicit workflow scope. Changing a typed
step through the existing re-run flow opens the normal editable plan card in the
same run before execution. The waiting card also saves the continuation's prior
rounds, so a gateway restart does not turn that review into a new analysis. On
approval, the reviewed tool and settings replace the old step's pins, while the
change reason still informs downstream dependency selection. A reviewed plan
containing only registered job-status calls uses the exact-call guard and cannot
launch a new scientific job. Prose-only historical continuations retain their
existing behavior. Added, removed or reordered steps require a newly reviewed
study rather than silently changing continuation indices.
