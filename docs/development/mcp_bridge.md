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
| `runs_on` | Currently accepts only `hpc:<name>` CPU image jobs. This is not a GPU or partition selector. |
| `tools_file` | Relative path to a tool snapshot confined to the registry directory. |
| `tools_sha256` | SHA-256 of the exact snapshot bytes, checked before registration. |
| `adapter` | Optional: `json` by default, or the built-in `atlas-expression` adapter for the Atlas server. |
| `prefix` | Optional prefix added to planner tool names; the original name is sent to MCP. Default: empty. |
| `aliases` | Optional original-name to planner-name mapping, applied before `prefix`. Wire names and resource keys remain unchanged. |
| `timeout` | Optional CLI call timeout in seconds, from 1 to 86400. Default: 15000. |
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
on unknown arguments. Calls are validated against the complete schema before
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
JSON text is decoded only when it represents an object; scalar, array and plain
text remain the original ordered content blocks. Malformed
responses fail instead of being reported as success.

The default JSON adapter keeps server data under `result`, separate from execution
status, artifact metadata and provenance. It writes the full result to
`artifacts/mcp/<server>/<tool>.json`, replacing that tool's prior result in the run.
A result exceeding 14,000 serialized characters is replaced in the transport
response by `response_omitted` and `full_result`. Arbitrary file paths returned by
a server are not followed or copied by this adapter. Servers with other
deliverables need an application-owned adapter.

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
