"""``preview_records``: a few rows of one record set, read from the head of its file.

Only the head is read (a bounded number of bytes), so a preview of a 20 GB table costs the same as
one of a small sample sheet. Where the bytes are:

* a result's tables are in the run's bundle on this host;
* a dataset is where its registry row says: on this host, or in the account's storage on HPC3,
  which is read through the account's own connected HPC3 session (the preview says so when there
  is none, rather than opening one).

The rows are parsed by :func:`aiscientist.metadata.preview_rows`: mlcroissant when it is installed,
reading the record set's fields from a one-file Croissant document over the sample, else the
standard ``csv`` module over the same fields.
"""

from __future__ import annotations

import shlex
from pathlib import Path, PurePosixPath

from .. import metadata as md
from . import metadata_store as store

HEAD_BYTES = 256 * 1024


class PreviewError(Exception):
    """Why a preview cannot be given, in words for the caller."""


def _safe_rel(rel: str) -> str:
    p = PurePosixPath(rel or "")
    if p.is_absolute() or ".." in p.parts:
        raise PreviewError("That file is not part of the dataset.")
    return p.as_posix()


def _local_head(path: Path, root: Path) -> bytes:
    try:
        target = path.resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            raise PreviewError("The file is no longer there.")
        with target.open("rb") as fh:
            return fh.read(HEAD_BYTES)
    except OSError as exc:
        raise PreviewError(f"The file could not be read: {exc.strerror or exc}") from None


def _remote_head(user, path: str) -> bytes:
    from . import app as gw
    conn = gw._owner_hpc_conn(user.username, user.id)
    if conn is None:
        raise PreviewError("This dataset is kept on HPC3: connect your HPC3 session in AiScientist, "
                           "then ask again.")
    if not any(path == base or path.startswith(base + "/") for base in gw._hpc_uploads_dirs(conn)
               + [f"{gw._storage_base(conn)}/{gw._RESULTS_FOLDER}"]):
        raise PreviewError("That file is outside your HPC3 storage.")
    res = conn.executor.exec(f"head -c {HEAD_BYTES} -- {shlex.quote(path)} | base64 -w0", timeout=60.0)
    if not res.ok:
        raise PreviewError("The file could not be read on HPC3.")
    import base64
    try:
        return base64.b64decode((res.stdout or "").strip())
    except ValueError:
        raise PreviewError("The file could not be read on HPC3.") from None


def preview(user, ref: str, record_set: str, limit: int) -> dict:
    loc = store.record_location(user.id, ref)
    if loc is None:
        raise PreviewError("No dataset or result with that id in this account.")
    doc = loc["document"]
    rs, file_obj = md.pick_record_set(doc, record_set)
    if rs is None:
        names = [r.get("name") for r in doc.get("recordSet") or []]
        raise PreviewError("It has no record set to preview." if not names else
                           f"No record set {record_set!r}; it has: {', '.join(map(str, names))}.")
    if file_obj is None:
        raise PreviewError("That record set's file is not described in the metadata.")
    rel = _safe_rel(str(file_obj.get("contentUrl") or file_obj.get("name") or ""))
    if loc["kind"] == "result":
        from . import app as gw
        base = gw.CONSOLE_RUNS_DIR / gw.safe_name(user.username) / str(loc["run_id"]) / "artifacts"
        head = _local_head(base / rel, base)
    else:
        stored = loc.get("dataset_path")
        if not stored:
            raise PreviewError("The dataset's files are not recorded.")
        is_folder = str(loc.get("dataset_kind") or "") == "folder" or str(loc.get("dataset_kind") or "").startswith("server-dir")
        target = f"{stored.rstrip('/')}/{rel}" if is_folder else stored
        from . import app as gw
        from . import server_data as sd
        local_root = gw.CONSOLE_RUNS_DIR / gw.safe_name(user.username)
        if Path(target).exists() and Path(target).resolve().is_relative_to(local_root.resolve()):
            head = _local_head(Path(target), local_root)
        elif sd.root_of(target, sd.server_data_roots()) is not None and Path(target).is_file():
            head = _local_head(Path(target), sd.root_of(target, sd.server_data_roots()))
        else:
            head = _remote_head(user, target)
    rows, how, notes = md.preview_rows(doc, rs, file_obj, head, limit=limit,
                                       truncated=len(head) >= HEAD_BYTES)
    return {"uuid": loc["uuid"], "record_set": rs.get("name") or rs.get("@id"),
            "file": file_obj.get("name") or rel, "fields": [f.get("name") for f in rs.get("field") or []],
            "rows": rows, "read_with": how, "notes": notes}
