# Shared datasets

A shared dataset is one the lab maintains for every account, such as the Human Eye Cell Atlas
(HECA). It has no owner. Its maintainers publish it in versions, and a published version never
changes, so every account reads the same bytes and a result can name exactly what it was computed
from. Shared datasets are not counted in any account's storage and are never removed by age.

Code: `src/aiscientist/gateway/shared_datasets.py` (store, publishing, reading),
`gateway/shared_routes.py` (read API), `aiscientist-admin shared-dataset` (operator commands),
`deploy/hpc3/sync_shared_dataset.sh` (the HPC3 copy). Tables: `shared_datasets`,
`shared_dataset_versions`, `shared_dataset_files`, `shared_dataset_maintainers`.

Accounts can list shared datasets and read their versions, files and changelogs through the API.
Attaching one to a chat, runs reading it, the Files → Shared tab, per-version Croissant metadata and
MCP access are not built yet.

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
