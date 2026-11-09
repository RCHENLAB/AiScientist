# Shared datasets

A shared dataset is one the lab maintains for every account, such as the Human Eye Cell Atlas
(HECA). It has no owner. Its maintainers publish it in versions, and a published version never
changes, so every account reads the same bytes and a result can name exactly what it was computed
from. Shared datasets are not counted in any account's storage and are never removed by age.

Code: `src/aiscientist/gateway/shared_datasets.py` (store, publishing, reading),
`gateway/shared_routes.py` (read API), `aiscientist-admin shared-dataset` (operator commands),
`deploy/hpc3/sync_shared_dataset.sh` (the HPC3 copy). Tables: `shared_datasets`,
`shared_dataset_versions`, `shared_dataset_files`, `shared_dataset_maintainers`.

Accounts can list shared datasets and read their versions, files and changelogs through the API,
see them in Files → Shared (version select, Metadata, Changelog, a tissue × modality grid with each
file's size and Preview) and attach them to a chat, whole or one file, following `latest` or pinned
to a version. Runs read them in place, every published version has a `croissant.json`, MCP tokens
list them, and maintainers publish and withdraw versions from the Shared tab.

## Where the files are

On the gateway host, in `AISCIENTIST_SHARED_DATASETS_ROOT` (unset: the feature is off), owned by the
service account:

```
<AISCIENTIST_SHARED_DATASETS_ROOT>/          e.g. /data/aiscientist/shared-datasets
└── heca/                                    one shared dataset (its slug)
    ├── latest -> v2026.11                   the newest published version
    ├── v2026.11/                            files 0444, folders 0555
    │   ├── HRCA_snRNA_allcells.h5ad …       the published files
    │   ├── manifest.json                    name, size, sha256 and facets of every file
    │   ├── SHA256SUMS                       the same checksums, for `sha256sum -c`
    │   └── CHANGELOG.md
    └── v2026.12/                            a file unchanged since the previous version is a
                                             hard link to it, so it takes no extra space
```

On HPC3, one copy of each version in `<AISCIENTIST_HPC_SHARED_ROOT>/datasets/<slug>/<label>/`, with
its own `latest`, read-only and never swept ([HPC3 storage layout](hpc3_storage_layout.md)).

A file's facets are its tissue and modality, read from names such as `Iris_snRNA_allcells.h5ad`
(the token naming a modality, and what comes before it), and for an `.h5ad` its numbers of
observations and variables, the X dtype and the obs column names, read from the HDF5 tree without
loading the matrix. Maintainers can set or correct facets in a `facets.json` in the source folder
(`{"<file>": {"tissue": "retina"}}`); it is not published as a file.

## Who may do what

| Action | Any account | Its maintainers | Admins |
|---|---|---|---|
| List shared datasets, read a version's files, facets, changelog and manifest | yes | yes | yes |
| Publish a version, withdraw one | | yes | yes |
| Create a shared dataset, edit its description, add or remove maintainers | | | yes |

The operator commands run on the gateway host as the service account, which owns the store;
`--by` names the account a version is published for, and must be an admin or a maintainer.

## Publishing a version

1. The maintainers put the version's files, and optionally `facets.json`, in one folder inside an
   allowlisted server folder (`AISCIENTIST_SERVER_DATA_ROOTS`), and write a changelog: what changed
   (files, annotation columns, cells) and why.
2. Publish:

   ```bash
   aiscientist-admin shared-dataset publish heca v2026.11 \
       --from /data/Users/shared/<maintainer>/<folder> --changelog CHANGELOG-v2026.11.md --by <account>
   ```

   Each file is hashed while it is copied; a file that changes meanwhile stops the publish. The
   version becomes visible, and `latest` moves to it, only once every file is in place and read-only.
   A failed publish can be run again with the same label; a published label is never reused.
3. Copy it to HPC3, as an HPC3 account whose SSH key works for both the login and the transfer node:

   ```bash
   deploy/hpc3/sync_shared_dataset.sh --store "$AISCIENTIST_SHARED_DATASETS_ROOT" \
       --slug heca --label v2026.11 --user <ucinetid>
   ```

   The copy goes through the transfer node, `sha256sum -c` checks it in a Slurm job, and only then
   is it renamed into place and made read-only.
4. Record the HPC3 copy: `aiscientist-admin shared-dataset hpc-verified heca v2026.11`.

`aiscientist-admin shared-dataset verify heca [v2026.11]` re-hashes a version in the store against
its manifest. `withdraw` stops offering a version: its files stay, so earlier results can still be
reproduced, and `latest` moves to the newest version still published.

## API

Every logged-in account; no answer carries a server path.

| Request | Answer |
|---|---|
| `GET /api/shared-datasets` | `{"enabled", "datasets": [{slug, name, description, citation, maintainers, latest, can_publish, versions: [{label, status, published_at, size_bytes, n_files, manifest_sha256, on_hpc3}]}]}` |
| `GET /api/shared-datasets/{slug}?version=latest` | one version (a label or `latest`) with its changelog, who published it and its files: `{name, size_bytes, sha256, facets}` |
| `GET /api/shared-datasets/{slug}/versions/{label}/manifest.json` | the version's manifest |
| `GET /api/shared-datasets/{slug}/versions/{label}/preview?file=<name>` | a bounded look at one listed file (an `.h5ad` shows obs/var, never the matrix); `download?file=<name>` serves it |
| `PUT /api/conversations/{cid}/shared-datasets` | replace the chat's shared attachments, body `{"shared_datasets": [{"shared": "heca", "version": "latest" \| "<label>", "files": [..] \| null, "role": "reference"}]}`; saved apart from the account's own datasets (table `conversation_shared_datasets`, migration 0008) |
| `GET /api/shared-datasets/{slug}/versions/{label}/croissant.json` | the version's Croissant 1.1 metadata (also written into the version folder at publish) |
| `POST /api/shared-datasets/{slug}/publish` | maintainers and admins (403 otherwise): `{"source", "label", "changelog"}`; starts a background job, answers `202 {"job", "state", "lines"}` |
| `GET /api/shared-datasets/{slug}/publish-jobs/{job}` | the job's `state` (`running`, `done`, `error`), progress `lines`, `result` or `error` |
| `POST /api/shared-datasets/{slug}/versions/{label}/withdraw` | maintainers and admins: stop offering a version |

`GET /api/conversations/{cid}` also returns `shared_datasets`: `{shared, name, requested, version, files,
role, n_files, withdrawn}`, a reference and never a path. A whole version counts as one attachment toward the
12-per-chat limit, together with the account's own datasets. A withdrawn version cannot be newly
attached; a chat already pinned to it keeps it.

## Runs

A chat's shared attachments go to `POST /api/lab` as references (`shared_datasets`), never as paths.
At run start the gateway resolves each once (`shared_datasets.resolve`) and keeps what `latest` meant
for the whole run. A withdrawn version still resolves for an account with a chat pinned to it, and
for no other request; `latest` never reaches one.

* On the gateway host, tools read the store in place.
* On HPC3, a version whose copy is verified is bound read-only where it is (`hpc_primary`,
  `hpc_dataset_root`, `AISCIENTIST_INPUTS`). Nothing is copied into `sessions/<id>/inputs/` and the
  account's storage is unchanged. A version with no verified copy falls back to the per-session copy
  and the run says so.
* `_admit_bound_paths` admits the store and the HPC3 copies as allowlisted prefixes. A dfs3b path
  outside this account's own uploads and personal folder, this account's session folders and
  `<shared root>/datasets/` is refused.
* The planner is told the version and each file's tissue, modality, cells, genes and obs columns. Loading
  all of HRCA snRNA (3.18M nuclei) peaks at about 218 GiB of memory, and the plan is told so.
* A shared file attached as the primary dataset is accepted by the atlas bridge with no
  `AISCIENTIST_ATLAS_FILES` entry.
* A run never writes into a shared dataset's metadata: the PI's description is skipped for shared inputs.

Each run writes `artifacts/process/inputs.json`:

```json
{"shared_inputs": [{"dataset": "heca", "requested": "latest", "version": "v2026.11",
  "version_uuid": "…", "manifest_sha256": "…",
  "files": [{"name": "HRCA_snRNA_allcells.h5ad", "size": 19450278232, "sha256": "…"}]}]}
```

When a chat follows `latest` and a newer version has appeared since its previous run, the reply says which
version the run used. The result's `croissant.json` links the version's uuid as an input.

## Metadata and MCP

Publishing writes `croissant.json` into the version folder: each file is a `cr:FileObject` with the
manifest's sha256 (mlcroissant needs one), `version` is the label, and each file's facets are under the
`aiscientist:` namespace. `list_datasets`, `search_datasets` and `get_dataset_metadata` also return the
published versions, marked `"shared": true`, for every token, read-only (`aiscientist://datasets/<version uuid>/croissant.json`).

## Publishing from the console

On Files → Shared, a maintainer or admin sees **Publish new version** (source folder inside an
allowlisted server folder, label, changelog) and **Withdraw**. The form runs `shared_datasets.publish`
as a background job and shows its progress; the server checks `can_publish`, so anyone else gets 403.
A job is kept in the gateway's memory: after a restart, publish again with the same label until it is published.
