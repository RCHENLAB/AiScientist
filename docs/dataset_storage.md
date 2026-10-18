# Dataset storage

Where uploaded datasets live on the gateway host, how they are named, and how they are removed.

## Two layouts

**Managed storage** is on when the deployment sets `AISCIENTIST_DATA_ROOT` (or `DATA_ROOT`) to a
volume outside the code directory:

```
<AISCIENTIST_DATA_ROOT>/
  datasets/<uuid>/original/    # verified uploaded files
  datasets/<uuid>/processed/   # files derived from them
  analysis_results/            # the results dir, unless AISCIENTIST_RESULTS_DIR says otherwise
  temp/<owner>/uploads/        # uploads in progress, never shown as datasets
  exports/
```

Each dataset gets a UUID; its name is only shown, so the same name uploaded twice never makes a
`(1)` or `_2` folder. An upload stays in `temp/` until every file of it (a folder's whole manifest)
is there with the right size, and only then moves to `datasets/<uuid>/`. With uploads on HPC3, the
same layout is used below the account's HPC3 uploads folder (`temp/<uuid>/original`, then
`datasets/<uuid>/{original,processed}` once the copy is verified), and the registry records the
HPC3 path.

**Without a data root** nothing moves: uploads stay in `<AISCIENTIST_RESULTS_DIR>/<owner>/uploads/`
(or `runs/console/` for local development) under their own names, as do rows written before a data
root was configured. Configuring one later does not move existing files (see *Migration*), and an
upload that was still in progress at that moment starts again.

Back up the data root together with the registry database (`AISCIENTIST_DATABASE_URL`), so UUIDs
and folders still match after a restore.

## The registry

Each row of `datasets` carries `uuid`, owner, name, storage path, size, `status`, `uploaded_at` and
`updated_at`. Start-up adds these columns to an existing database and fills them in for old rows,
without changing their paths. Numeric ids stay valid.

## Removing data

- **Delete a dataset** (Datasets page): its files are removed first, then its row. If the files
  cannot be removed (no HPC3 session for a dataset kept there, a permission error), the row stays and
  the page says why. What can be removed: the managed `datasets/<uuid>` tree, the owner's own
  uploads folder (on this host or on HPC3, current or legacy layout), and results saved with
  "Save to my data" (`<lab folder>/<user>/AiScientist_results/`). Nothing else in the personal lab
  folder, no symlink, and no path another dataset row also covers. A bound server folder is the
  lab's: deleting it removes its row and this session's HPC3 copy, never the folder.
- **Cancel an upload**: its partial and temporary files go, on this host and on HPC3.
- **An interrupted upload** stays resumable for `AISCIENTIST_UPLOAD_TTL_SECONDS` (default 86400,
  24 hours) after its last write, from this browser or another one of the account's. After that a
  sweep (at start-up, then every 5 minutes) removes it; an HPC3 temp folder of a disconnected
  account goes at its next connection. A copy to HPC3 that failed is kept for "Retry copy" for the
  same time, unless `AISCIENTIST_UPLOAD_KEEP_SETTLED_DAYS` says otherwise.

## Reconciliation

`POST /api/datasets/reconcile` with `{"cleanup": false, "all_owners": false}` (the defaults) reports,
for the caller's own rows on this host, those whose files are missing. An admin with
`all_owners: true` also gets the folders that no row refers to: UUID folders under
`<data root>/datasets/` and entries of each account's legacy uploads folder (under
`AISCIENTIST_RESULTS_DIR` and `AISCIENTIST_LEGACY_RESULTS_DIR`, which defaults to the code
directory's `runs/console`). `cleanup: true` deletes the missing rows and those folders, except a
folder published less than an hour ago (its row is being written), anything the HPC3 copy queue
still holds (a folder still uploading, a file kept for "Retry copy"), hidden entries and symlinks.
Paths on HPC3 and bound server folders are never touched.

## Migration

`POST /api/datasets/migrate` with `{"dataset_id": <id>, "apply": false}` shows where a legacy dataset
on this host would go; `apply: true` copies it into `datasets/<uuid>/original/` and points its row
at the copy. The source is never deleted, an existing destination is never overwritten, and a
symlink is refused. Back up the database and the data first, and keep the sources until the copies
and the backup are checked.
