# Dataset and result metadata (Croissant)

Every dataset and every analysis result in AiScientist carries a `croissant.json`: metadata in
[Croissant 1.1](https://docs.mlcommons.org/croissant/docs/croissant-spec-1.1.html) (MLCommons,
JSON-LD on schema.org). It says what the data is, where it came from, how its files are laid out,
what its tables' columns mean and how it may be reused. People read it on the Datasets page, and
agents read it through the MCP server. This page is the development guideline, followed by a
reference for the parts a contributor touches.

## The rules

1. **Metadata is required.** Every new dataset type, pipeline, or result type must produce valid
   Croissant 1.1 metadata. A feature is not done until it does. In practice, a new way of creating
   a dataset row goes through `auth_routes.record_dataset` or `record_folder_dataset`, with what
   was measured (checksums, the upload peek, the file list). A new kind of run result ends in
   `_write_result_metadata` (in `gateway/app.py`).
2. **Generate, don't hand-write.** Documents are built in code by `aiscientist.metadata`
   (`build_dataset`, `build_result`) from facts plus the owner's review. Never edit a stored
   `croissant.json`: it is rebuilt from its facts whenever the owner saves the form, and on every
   scheduled re-check.
3. **Validate in CI.** `tests/test_croissant_metadata.py` builds documents from the small fixture
   datasets in `tests/fixtures/croissant/` the way the gateway does, then validates them with
   mlcroissant. CI installs mlcroissant (`requirements-dev.txt`) and fails if it is missing. A new
   file format or result type gets a fixture and a test there. Stored metadata is re-checked weekly
   by the gateway (`AISCIENTIST_METADATA_RECHECK_HOURS`, default 168), and on demand with
   `aiscientist-admin metadata-check`.
4. **Record provenance.** Every result records its inputs by UUID, the pipeline (route, presets,
   skills, planner, mode), the AiScientist version and commit, the parameters of each accepted
   tool call, the models called, the run id, and the start and end times.
5. **Keep identifiers stable.** Refer to a dataset by its UUID (`Dataset.uuid`; `@id` is
   `urn:uuid:<uuid>`), never by its folder name or path. A result's UUID is UUIDv5 of its run id,
   so a run always has the same result UUID.
6. **Keep sensitive data out of metadata.** Never put protected health information, patient or
   sample identifiers, API keys, or server paths into metadata. The code enforces what it can:
   - table values are read only to infer column types;
   - a VCF's sample names are counted, never kept;
   - files are named relative to the dataset's root;
   - free text is scrubbed of absolute paths;
   - the validator rejects any document containing a server path or something that looks like a
     credential;
   - the review form refuses both.

   For human data, the owner fills in the consent and use-restriction fields (the RAI extension).
7. **Update metadata with the data.** Deleting a dataset deletes its metadata in the same database
   commit. Adding files to a folder dataset re-drafts its metadata with the earlier checksums kept.
   Saving a result to "my data" drafts metadata for the new dataset, linked to the run's result.

## What is generated, and when

| Event | What gets metadata | Where it is stored |
|---|---|---|
| An upload completes (single file, folder, unpacked archive) | The dataset row | `metadata_records`, keyed by the dataset's UUID |
| A server folder or file is bound | The dataset row (`source: server`) | same |
| A run finishes (research run or corpus scan) | The run's result | `metadata_records` (`kind: result`), and `croissant.json` in the run's bundle |
| "Save to my data" on a run's result | The saved dataset, derived from the result | `metadata_records` and a `saved_from` lineage link |
| The gateway starts, then weekly | Rows that predate their metadata get drafted; every document is rebuilt and validated | same |

Accounts must be on for the registry. Without accounts, a run still writes `croissant.json` into
its bundle.

## What a document holds

- **Metadata:** the name, a description, the creator (the owning account), the dates, the version,
  the license, `citeAs`, and `conditionsOfAccess`. The license defaults to *private: no license
  granted* until the owner picks one.
- **Resources (`distribution`):** one `cr:FileObject` per file whose SHA-256 was measured, with its
  size and `encodingFormat`. A file whose checksum was never measured is a `cr:FileSet` whose
  `includes` is exactly its path (Croissant requires a checksum on every FileObject). A folder with
  more than 500 files lists 500 and groups the rest. `encodingFormat` is a media type and, where
  one exists, the EDAM format IRI.
- **Structure (`recordSet`):** one record set per CSV/TSV table (plain or gzip). It names each
  column from the table's header and gives it an atomic type (`sc:Integer`, `sc:Float`,
  `sc:Boolean`, `sc:Date`, `sc:Text`) inferred from the sampled rows. AnnData, 10x HDF5 and VCF
  files are described in prose from their headers: the shape, the obs columns and layers, the
  genome build, the sample count.
- **Semantics:** organism (NCBI Taxonomy), tissue (UBERON) and cell types (Cell Ontology) are listed
  under `about`, and the assay (EFO) under `measurementTechnique`. Each is a `DefinedTerm` with its
  ontology IRI. EDAM data types go in `additionalType`. Source accessions (GEO, SRA, ENA,
  BioProject, BioSample, ArrayExpress) are linked through `sameAs` and listed in `identifier`.
- **Provenance:** a result has `prov:wasDerivedFrom` and `isBasedOn` pointing at its inputs, and a
  `prov:wasGeneratedBy` activity (`urn:aiscientist:run:<run_id>`). The activity holds the start and
  end times, the inputs (`prov:used`), the software and model agents, and the
  `aiscientist:pipeline` and `aiscientist:parameters` values. A dataset saved from a result points
  at that result the same way.
- **Responsible AI:** when the owner marks the data as derived from human subjects, `conformsTo`
  adds `http://mlcommons.org/croissant/RAI/1.0`, and `rai:personalSensitiveInformation`,
  `rai:dataUseCases` and `rai:dataCollection` hold the consent, the use restrictions and how the
  data was collected.

Every drafted value has a provenance entry: where it came from (`upload-check`, `computed`,
`run-record`, `account`, `registry`, `file-content`, `file-name`, `default`, `owner`), how confident
the drafting was (0–1), and whether the owner reviewed it. The confidence summary is kept per
FileObject, FileSet and RecordSet (the `provenance.json` and `confidence.json` shapes of the
Boulangerie catalog). A value below 0.7, or a placeholder, is flagged for the owner to check.

### AiScientist's own terms

The `aiscientist:` prefix is this page:
`https://github.com/RCHENLAB/AiScientist/blob/main/docs/development/metadata.md#`.

| Term | On | Value |
|---|---|---|
| `aiscientist:pipeline` | `prov:Activity` | JSON: `route`, `mode`, `planner`, `presets`, `skills`, `plan_mode`, `autonomous` |
| `aiscientist:parameters` | `prov:Activity` | JSON list: `{step, tool, args}` for each tool call of an accepted step; code and free text are left out, and paths are cut to the file name |
| `aiscientist:status` | `prov:Activity` | `done`, `incomplete` or `failed` |

## Where the code is

| Path | What it does |
|---|---|
| `src/aiscientist/metadata/` | Builds and validates the documents. It knows nothing about the web server or the database. `croissant.py` holds the builders, `facts.py` what is measured, `form.py` the review form, `vocab.py` and `formats.py` the vocabularies, `validate.py` the checks, `preview.py` record previews. |
| `src/aiscientist/gateway/metadata_store.py` | The registry: drafting, review, results, lineage, search, access tokens, the re-check |
| `src/aiscientist/gateway/metadata_routes.py` | The HTTP API under `/api` |
| `src/aiscientist/gateway/mcp_server.py` | The MCP server at `/mcp` |
| `frontend/console/` | The Metadata dialog and the MCP section on the Datasets page; the `croissant.json` link on Runs |

**Adding a file format:** add it to `metadata/formats.py`, with its EDAM IDs checked against
[OLS](https://www.ebi.ac.uk/ols4/ontologies/edam). If mlcroissant can read it, add a fixture and a
test.

**Adding an ontology term:** add it to `metadata/vocab.py`, again checking its ID and label in OLS.
A made-up ID is worse than none, because a machine trusts it. A term outside the list can still be
entered in the form, where it is kept as a label with no ID.

## HTTP API (the logged-in account only)

| Endpoint | What it does |
|---|---|
| `GET /api/datasets/{id-or-uuid}/metadata` | Returns the form, the document, provenance, confidence and validation |
| `PUT /api/datasets/{id-or-uuid}/metadata` | Saves the review; the document is rebuilt |
| `GET /api/datasets/{id-or-uuid}/croissant.json` | Returns the document (`application/ld+json`) |
| `GET /api/results/{run-id-or-uuid}/metadata`, `.../croissant.json` | The same for a run's result |
| `GET /api/metadata/{uuid}/lineage?direction=upstream\|downstream\|both&depth=3` | Returns the lineage graph |
| `GET /api/metadata/vocabulary` | Returns the form's ontology choices and licenses |
| `GET/POST/DELETE /api/account/tokens` | Lists, makes or revokes personal access tokens for MCP clients |

## MCP server

`POST /mcp` speaks MCP over Streamable HTTP (JSON responses, stateless; protocol versions
2025-06-18, 2025-03-26 and 2024-11-05).

**Authentication:** a request acts as one account. It sends either `Authorization: Bearer <token>`,
with a token made on the Datasets page (only its SHA-256 is stored), or the console's session
cookie. Every lookup is filtered on that account, as `/api/datasets` is. A revoked token, a disabled
account, or a browser page from another origin is refused.

**Tools:**
- `list_datasets`: the account's datasets and results.
- `search_datasets`: search by text, organism, assay, tissue or accession (labels or ontology IDs).
- `get_dataset_metadata`: the `croissant.json`, with its provenance and confidence.
- `get_provenance`: the lineage graph, upstream and/or downstream.
- `preview_records`: a few rows of a table, read with mlcroissant. Data kept on HPC3 needs the
  account's HPC3 session to be connected.

**Resources:** `aiscientist://datasets/<uuid>/croissant.json` and
`aiscientist://results/<uuid>/croissant.json`.

To connect Claude Code:

```bash
claude mcp add --transport http aiscientist https://<host>/mcp --header "Authorization: Bearer <token>"
```

The research agent itself reads the bound datasets' metadata when it plans. Each dataset's
description, ontology terms, accessions and citation are in the planner's dataset context, marked
as reviewed by the owner or as drafted, so a drafted organism is checked against the data rather
than trusted.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `AISCIENTIST_PUBLIC_URL` | unset | The public address (e.g. `https://<host>`). When set, each document's `url` is its API address; otherwise it is the `urn:uuid:` identifier. |
| `AISCIENTIST_METADATA_RECHECK_HOURS` | `168` | How often stored metadata is re-drafted and re-validated; `0` runs it once, at start. |
| The `metadata` extra (`pip install -e ".[metadata]"`) | not installed | Adds mlcroissant, the reference validator, which is also used for previews. Without it, documents are validated with AiScientist's own rules and previews use the `csv` module. |
