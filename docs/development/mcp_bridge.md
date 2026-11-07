# Pinned MCP client bridge

`agents/mcp_bridge.py` registers tools from reviewed MCP `tools/list` snapshots and
calls them through the server image's `aiscientist-mcp` CLI. The gateway needs no
MCP or analysis packages. Registration reads local configuration and schemas; it
does not start a server, discover tools over the network, or download a catalog.

This is the first-stage client bridge for [issue #94](https://github.com/RCHENLAB/AiScientist/issues/94).
Atlas expression uses it; the existing 23 application tools and their images remain
in place. The supported execution path is a CPU image job through
`run_in_environment`. GPU routing, `inprocess` servers, sampling, model endpoint
settings and an authenticated HTTP transport are not implemented by this bridge.
Real website/HPC3 end-to-end validation remains outstanding.

## Registry and tool schemas

`configs/mcp_servers.json` is the default registry. A deployment can set
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
| `runs_on` | Currently accepts only `hpc:<name>` CPU image jobs. This is not a GPU or partition selector. |
| `tools_file` | Relative path to a tool snapshot confined to the registry directory. |
| `tools_sha256` | SHA-256 of the exact snapshot bytes, checked before registration. |
| `adapter` | Optional: `json` by default, or the built-in `atlas-expression` adapter for the Atlas server. |
| `prefix` | Optional prefix added to planner tool names; the original name is sent to MCP. Default: empty. |
| `timeout` | Optional CLI call timeout in seconds, from 1 to 86400. Default: 15000. |
| `resources` | Optional mapping from original tool name to `[cpus, mem_gb, time_limit]`. Omitted tools inherit the skill's resources. |

Each snapshot is a JSON array of `{name, description, inputSchema}` objects.
Schemas must describe objects and pass JSON Schema Draft 2020-12 validation.
References must be local fragments; schema loading cannot resolve network or
filesystem references. Duplicate server names, skill bindings and planner names
are rejected, as are collisions with existing planner tools.

The planner gets the catalog description and a copy of the input schema. If the
simple top-level schema omits `additionalProperties`, the bridge sets it to `false`.
Root references and composed schemas retain their authored semantics, as do
schemas that explicitly permit additional properties. Calls
are validated before scheduling, including required arguments and types. Arguments
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
cannot replace its dataset checks with the generic JSON adapter.

The bridge constructs the command from trusted configuration and shell-quotes the
tool's JSON arguments. The existing executor owns the CPU Slurm job, image
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
JSON text is decoded, and plain text is retained as content blocks. Malformed
responses fail instead of being reported as success.

The default JSON adapter keeps server data under `result`, separate from execution
status, artifact metadata and provenance. It writes the full result to
`artifacts/mcp/<server>/<tool>.json`, replacing that tool's prior result in the run.
A result exceeding 14,000 serialized characters is replaced in the transport
response by `response_omitted` and `full_result`. Arbitrary file paths returned by
a server are not followed or copied by this adapter. Servers with other
deliverables need an application-owned adapter.

Every call that reaches the executor records the server name, package version, release tag, image
digest, schema checksum and `runs_on` in the run manifest's `mcp_servers` list.
Responses also carry this identity under `mcp`. The existing environment path
records the skill, image and command hash.

## Atlas binding

The configured package is `atlas-expression-exploration` 0.0.3, release
`atlas-expression-exploration-v0.0.3`, with this image:

```text
docker://ghcr.io/lijinbio/mcp-atlas-expression-exploration@sha256:5e9b13da5d180e7983945a7f1f4182e4b12876d3ec8f59ed0a0aa7b43d041d8a
```

Its skill is `explore-atlas-expression-with-aiscientist-mcp`. The nine planner names
stay unchanged: `resolve_genes`, `submit_query`, `query_status`, `get_summary`,
`plot_dotplot`, `plot_violin`, `plot_featureplot`, `write_report` and `list_queries`.
Transport and registration are generic; `AtlasAdapter` in
`agents/atlas_expression.py` owns these domain restrictions and outputs:

- The primary dataset must be a `.h5ad` matching a normalized absolute path in the
  deployment's `AISCIENTIST_ATLAS_FILES` allowlist. Entries are semicolon-separated
  `label=/absolute/path.h5ad` pairs; a label ending in `latest` is not accepted.
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

The checked-in Atlas snapshot was obtained through a real stdio MCP listing from
the [0.0.3 source release](https://github.com/lijinbio/aiscientist-mcp/releases/tag/atlas-expression-exploration-v0.0.3),
commit `c4581015ef5f6b573894c18bf9023d8fe3a5250a`, with `mcp==2.3.0`,
`mcp-types==2.3.0` and `pydantic==2.14.2`. Its SHA-256 is
`069f629e1056c37b57d3e6173d6b257200c4cc8b70782b128fa6a6111fcb0020`.
From that source checkout, with `AISCI_EXPORT_SCRIPT` set to the absolute path of
this application's exporter, the source-only reproduction is:

```bash
PYTHONPATH=packages/common/src:packages/atlas-expression-exploration/src \
uv run --isolated --no-project \
  --with mcp==2.3.0 --with mcp-types==2.3.0 --with pydantic==2.14.2 \
  python "$AISCI_EXPORT_SCRIPT" \
  --server 'python -m aiscientist_mcp.atlas_expression_exploration.server' \
  > /tmp/atlas-tools.json
```

This verifies the metadata and stdio listing from the pinned source; it is not a
production image smoke test or website/HPC3 end-to-end validation.
