# Dataset storage

Where users' datasets live, how they reach HPC3 when a run needs them there, and how they are
removed.

## Where data is kept

With a data root (`AISCIENTIST_DATA_ROOT`, or `DATA_ROOT`) on a volume outside the code directory,
the gateway host is the one persistent home of every user's data. HPC3 holds a copy only while a
session's runs use it.

```
<AISCIENTIST_DATA_ROOT>/                    e.g. /data/aiscientist/data
├── users/<account>/
│   └── datasets/<uuid>/                    one dataset
│       ├── original/                       its files: an upload as sent (one file or a folder
│       │                                   tree), a result saved from a run, or a dataset that
│       │                                   was kept on HPC3 and brought here
│       ├── processed/                      what an uploaded archive unpacked to
│       └── source.json                     only for a dataset brought from HPC3: where it was,
│                                           its size, and whether that original was removed
├── temp/<account>/
│   ├── uploads/                            uploads in progress: never shown as datasets
│   └── stage/<uuid>/                       a dataset being assembled (copied back from HPC3)
├── analysis_results/                       the run store, unless AISCIENTIST_RESULTS_DIR says otherwise
├── corpus_scans/<key>/                     finished corpus scans, one per corpus version, reused by
│                                           every account (deploy/paperqa/README.md)
└── exports/
```

`<account>` is the AiScientist account name as the gateway writes it everywhere else (letters,
digits, `-` and `_`), so one account's data is one folder: its size is one `du`, and its backup or
removal touches nothing of anyone else's. `<uuid>` is the dataset's `datasets.uuid`, also its
Croissant `@id` (`urn:uuid:<uuid>`). A name is only shown, so the same name uploaded twice never
makes a `(1)` or `_2` folder.

Back up `users/` together with the registry database (`AISCIENTIST_DATABASE_URL`), so UUIDs and
folders still match after a restore. `temp/` needs no backup.

**Without a data root** uploads stay in `<AISCIENTIST_RESULTS_DIR>/<owner>/uploads/` (or
`runs/console/` for local development) under their own names, and with `AISCIENTIST_UPLOADS_ON_HPC=1`
they are copied to the account's HPC3 uploads folder instead. Once a data root is configured,
datasets kept only on HPC3 are brought to it when their owner connects; files already on this host
stay where they are until migrated (see *Moving existing datasets*).

## Uploads

A file reaches the gateway over tus (8 MiB chunks, each checked by its SHA-256). Its last request
answers at once; a background queue, per account, then

1. compares the whole file with the SHA-256 the browser sent, when it sent one (a mismatch fails the
   upload: the file on the server is not the user's);
2. checks its size and moves it from `temp/<account>/uploads/` into `users/<account>/datasets/<uuid>/original/`;
3. for an archive the user asked to unpack, unpacks it into `processed/<name>` (links, absolute
   paths and decompression bombs are refused, as on HPC3);
4. records the dataset.

A folder is one dataset: it is published and recorded once every file of its manifest is there and
checked, whichever arrives last, the browser's "that was the last file" or the last check. A check
that a restart interrupted continues when the gateway starts again, whether or not the account is
connected. Nothing is written to HPC3 at upload time.

## When a run needs the data on HPC3

HPC3's compute nodes cannot see the gateway host's disks. When a run's analysis or `run_code` runs on
HPC3, every dataset bound to it from the account's dataset folder (and each bound server folder) is
copied, at the start of the run, into the session's own area,
`<HPC3 shared root>/sessions/<session>/inputs/` (see
[`hpc3_storage_layout.md`](hpc3_storage_layout.md)). The copy is incremental (a second run in the
same session copies nothing) and resumable, and goes through the transfer host, not a login node.
Tools on the gateway host read the original in place. The session's copies are removed when it
disconnects, and the sweep removes those a session left without disconnecting (after 3 days); a run
continued in a later session copies its inputs again.

Several bound folders are several samples: each is read as a folder (a Cell Ranger library in it is
named after its dataset, not after `original/`), and `run_cellqc` reads the libraries of all of them
in one step. A bound dataset the run cannot use is named to the planner with the reason.

## Results

A run's deliverables (report, figures, tables) come back to `analysis_results/` (or
`AISCIENTIST_RESULTS_DIR`) after each step. Its processed dataset and CellQC's result folder stay
beside the run, on HPC3 for an HPC3 run, until the session ends: then they are copied into the run's
folder here (`<results>/<account>/<run_id>/result/`), each file checked by size and its SHA-256
recorded in `<run_id>/stage_out.json`, before the HPC3 folder is removed (what counts as a result,
and how the removal checks it, is in [`hpc3_storage_layout.md`](hpc3_storage_layout.md)). They stay
with the run until the run is deleted.

They are saved to the user's data when the run completes if the run asked for it; "Save to my data"
on the Runs page does the same later. Saving one makes it a dataset:

- kept on HPC3: copied back in the background into a new `users/<account>/datasets/<uuid>/original/`,
  each file checked by size, and listed when it is all there. The request answers 202 at once, and
  the console shows the copy's progress and says when the dataset is in Files → Datasets. At the
  session's end the saved copy stands for the HPC3 file, which is not copied a second time. While
  the copy home is still running or failed, the run's folder is kept at disconnect, and the next
  connect brings it home;
- kept on the gateway host (a run that computed here, or one whose session has ended): moved there.

## Moving existing datasets

**Kept only on HPC3.** When an account connects, each of its datasets that lives only on HPC3, in a
place its session may read (its uploads there, or results saved to its personal lab folder), is
copied in the background into `users/<account>/datasets/<uuid>/original/`, file by file through
`temp/<account>/stage/<uuid>/` so an interrupted copy continues at the next connection. Once every
file has arrived at the size HPC3 reports, the dataset's row points at the copy, and `source.json`
beside it names the HPC3 path, its size and whether it is a file or a folder. A run that still
names the HPC3 path (a chat attached it before the move) reads the copy here. The HPC3 original is
not deleted then. Once the copy is backed up, the Storage tab removes the originals in two Slurm
jobs: a dry run that checks each one against its copy, then, on the user's confirmation, the
removal; only originals in the account's own HPC3 uploads folder, and in its personal lab folder
only where the deployment allows it (see
[`hpc3_storage_layout.md`](hpc3_storage_layout.md#originals-of-datasets-kept-on-the-server)).
A removed original is recorded in `source.json`, which still names it, so a run that names it reads
the copy. A row whose data is gone from HPC3 is left as it is. Bound server folders are never
moved. `AISCIENTIST_MOVE_HPC_DATASETS=0` turns this off.

**On this host, in an older layout.** `POST /api/datasets/migrate` with
`{"dataset_id": <id>, "apply": false}` shows where a dataset would go; `apply: true` copies it into
`users/<account>/datasets/<uuid>/original/` and points its row at the copy. The source is never
deleted, an existing destination is never overwritten, and a symlink is refused.

## The registry

Each row of `datasets` carries `uuid`, owner, name, storage path, size, `status`, `uploaded_at` and
`updated_at`. `GET /api/datasets` also says where each one is (`location`: `server` or `hpc3`). Start-up
adds these columns to an existing database and fills them in for old rows, without changing their
paths. Numeric ids stay valid.

## Removing data

- **Delete a dataset** (Files → Datasets): its files are removed first, then its row. If the files
  cannot be removed (no HPC3 session for a dataset still kept there, a permission error), the row
  stays and the page says why. What can be removed: the account's own `users/<account>/datasets/<uuid>`
  folder (and the session's HPC3 copy of it), the account's uploads folder (on this host or on HPC3,
  current or legacy layout), and results saved to the personal lab folder on HPC3
  (`<lab folder>/<user>/AiScientist_results/`). Never another account's folder, nothing else in the
  personal lab folder, no symlink, and no path another dataset row also covers. A bound server
  folder is the lab's: deleting it removes its row and this session's HPC3 copy, never the folder.
- **Delete a run** (Runs): `POST /api/runs/delete` removes the run's folder on this host
  (`<results>/<account>/<run_id>`: report, figures, tables, process record, and a processed dataset
  kept beside it that was not saved), then its row and its result's metadata. Refused while the run
  or a run continuing it is executing, while its result is being copied to the user's data, and
  while a dataset row points inside the folder. Datasets saved from the run stay; its model calls stay
  in the account's usage; its HPC3 working folder goes with the session's other runs when the
  session ends.
- **Cancel an upload**: its partial and temporary files go.
- **An interrupted upload** stays resumable for `AISCIENTIST_UPLOAD_TTL_SECONDS` (default 86400,
  24 hours) after its last write, from this browser or another one of the account's. After that a
  sweep (at start-up, then every 5 minutes) removes it. A check that failed is kept for "Retry" for
  the same time, unless `AISCIENTIST_UPLOAD_KEEP_SETTLED_DAYS` says otherwise.

## Reconciliation

`POST /api/datasets/reconcile` with `{"cleanup": false, "all_owners": false}` (the defaults) reports,
for the caller's own rows on this host, those whose files are missing. An admin with
`all_owners: true` also gets the folders that no row refers to: UUID folders under every
`users/<account>/datasets/` (and the flat `<data root>/datasets/` of earlier versions), and entries of
each account's legacy uploads folder (under `AISCIENTIST_RESULTS_DIR` and
`AISCIENTIST_LEGACY_RESULTS_DIR`, which defaults to the code directory's `runs/console`).
`cleanup: true` deletes the missing rows and those folders, except a folder published less than an
hour ago (its row is being written), anything the upload queue still holds (a folder still
uploading, a file kept for "Retry"), hidden entries and symlinks. Paths on HPC3 and bound server
folders are never touched.
