"""Where user data lives on the gateway host, and conservative maintenance of registry/disk references.

The gateway host is the one persistent home of a user's data (RCHENLAB/AiScientist issues 5 and 6).
With a data root configured (``AISCIENTIST_DATA_ROOT``) the layout is one folder per account::

    <data root>/
    ├── users/<account>/datasets/<uuid>/   one dataset: an upload, a saved run result, or one brought
    │   ├── original/                      back from HPC3: its files as received (a file or a tree)
    │   └── processed/                     what an uploaded archive unpacked to
    ├── temp/<account>/                    uploads in progress (``uploads/``) and copies being
    │                                      assembled (``stage/<uuid>``); never a finished dataset
    ├── analysis_results/                  the run store when ``AISCIENTIST_RESULTS_DIR`` is unset
    ├── corpus_scans/<key>/                finished corpus scans, shared by every account (corpus_scan.py)
    └── exports/

``<account>`` is the account's name as the gateway writes it everywhere else (letters, digits, ``-``
and ``_``), and ``<uuid>`` is the dataset's ``datasets.uuid``. HPC3 receives a copy of a dataset when a
run needs it there, in the session's own temporary area, never at upload time.
"""
from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

# A tree moved into datasets/ gets its registry row right after: reconcile leaves trees younger than
# this alone, so a cleanup never removes an upload that is being recorded.
_PUBLISH_GRACE_S = 3600


def configured() -> bool:
    """Managed storage is on only where the deployment names its data root
    (``AISCIENTIST_DATA_ROOT``, or ``DATA_ROOT``). Without one, uploads, runs and results stay where
    they were (``AISCIENTIST_RESULTS_DIR`` / ``runs/console``): a deploy never moves them by itself,
    and never onto a home directory's disk."""
    return bool(os.environ.get("AISCIENTIST_DATA_ROOT") or os.environ.get("DATA_ROOT"))


def data_root() -> Path:
    return Path(os.environ.get("AISCIENTIST_DATA_ROOT") or os.environ.get("DATA_ROOT")
                or Path.home() / ".local" / "share" / "aiscientist").expanduser().resolve()


def ensure_layout() -> Path:
    root = data_root()
    for name in ("users", "analysis_results", "temp", "exports"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def account_name(username: str) -> str:
    """An account's folder name: the gateway's ``safe_name`` of its username."""
    return "".join(c for c in (username or "") if c.isalnum() or c in "-_") or "guest"


def _check_owner(owner: str) -> str:
    if not owner or account_name(owner) != owner:
        raise ValueError("invalid account name")
    return owner


def user_dir(owner: str) -> Path:
    """``<data root>/users/<account>``: everything this account keeps."""
    return data_root() / "users" / _check_owner(owner)


def datasets_root(owner: str) -> Path:
    """``<data root>/users/<account>/datasets``: this account's datasets, one UUID folder each."""
    return user_dir(owner) / "datasets"


def dataset_dir(dataset_uuid: str, owner: str) -> Path:
    canonical = str(uuid.UUID(dataset_uuid))
    return datasets_root(owner) / canonical


def temp_dir(dataset_uuid: str, owner: str) -> Path:
    """Where a dataset is assembled before it is published (a migration, a copy back from HPC3)."""
    return data_root() / "temp" / _check_owner(owner) / "stage" / str(uuid.UUID(dataset_uuid))


def _locate(path: str | Path) -> "tuple[str | None, str, Path] | None":
    """``(account, uuid, the dataset's folder)`` for a path inside a managed dataset; account is
    None for the flat ``<data root>/datasets/<uuid>`` layout written before datasets were kept per
    account. None for any other path. Resolves first, so a symlink or ``..`` cannot lead in."""
    try:
        resolved = Path(path).resolve()
        rel = resolved.relative_to(data_root())
    except (ValueError, OSError, RuntimeError):
        return None
    parts = rel.parts
    try:
        if len(parts) >= 4 and parts[0] == "users" and parts[2] == "datasets":
            owner = _check_owner(parts[1])
            identity = str(uuid.UUID(parts[3]))
            if identity != parts[3]:
                return None
            return owner, identity, data_root().joinpath(*parts[:4])
        if len(parts) >= 2 and parts[0] == "datasets":
            identity = str(uuid.UUID(parts[1]))
            if identity != parts[1]:
                return None
            return None, identity, data_root() / "datasets" / identity
    except ValueError:
        return None
    return None


def managed_uuid(path: str | Path) -> str | None:
    found = _locate(path)
    return found[1] if found else None


def managed_owner(path: str | Path) -> str | None:
    """The account whose dataset folder holds ``path``; None outside one (or in the flat layout)."""
    found = _locate(path)
    return found[0] if found else None


def managed_tree(path: str | Path) -> Path | None:
    """The dataset folder (``…/datasets/<uuid>``) that holds ``path``, or None."""
    found = _locate(path)
    return found[2] if found else None


def is_owned(path: str | Path, owner: str) -> bool:
    """True when ``path`` lies inside one of this account's dataset folders."""
    found = _locate(path)
    return bool(found and found[0] is not None and found[0] == owner)


def location_of(path: str) -> str:
    """``hpc3`` for a dataset path on the cluster's storage (``AISCIENTIST_LAB_STORAGE`` or
    ``AISCIENTIST_HPC_SHARED_ROOT``), else ``server``: on this host."""
    from .settings import LAB_STORAGE
    roots = [LAB_STORAGE, os.environ.get("AISCIENTIST_HPC_SHARED_ROOT", "")]
    text = str(path or "")
    return "hpc3" if any(r and text.startswith(r.rstrip("/") + "/") for r in roots) else "server"


def publish_dataset(staged_dir: str | Path, dataset_uuid: str, owner: str) -> Path:
    """Publish a verified staged tree as ``users/<owner>/datasets/<uuid>/original``, returning it.

    The caller verifies its complete manifest first. A repeated call after a successful
    move is safe; an existing destination never silently overwrites another tree.
    """
    ensure_layout()
    source = Path(staged_dir).resolve()
    if Path(staged_dir).absolute() != source or any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("Dataset staging contains a symlink")
    root = dataset_dir(dataset_uuid, owner)
    target = root / "original"
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("Dataset path escapes managed storage")
    if not source.is_relative_to(data_root() / "temp") or source == data_root() / "temp":
        raise ValueError("Dataset staging must be inside DATA_ROOT/temp")
    if target.exists():
        if source.exists():
            raise FileExistsError("Dataset UUID already published")
        return target
    root.mkdir(parents=True, exist_ok=True)
    (root / "processed").mkdir(exist_ok=True)
    source.rename(target)
    return target


def remove_managed_dataset(path: str | Path) -> None:
    """Delete exactly the UUID tree containing a stored path; reject symlink escapes."""
    if Path(path).absolute() != Path(path).resolve():
        raise ValueError("Dataset path contains a symlink")
    root = managed_tree(path)
    if root is None:
        raise ValueError("Path is not a managed dataset")
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("Dataset path escapes managed storage")
    if root.exists():
        shutil.rmtree(root)


def legacy_results_roots() -> list[Path]:
    """Known local upload layouts; arbitrary registered paths are never trusted roots."""
    roots = [Path(os.environ.get("AISCIENTIST_LEGACY_RESULTS_DIR")
                  or Path(__file__).resolve().parents[3] / "runs" / "console")]
    if os.environ.get("AISCIENTIST_RESULTS_DIR"):
        roots.append(Path(os.environ["AISCIENTIST_RESULTS_DIR"]))
    return list(dict.fromkeys(root.expanduser().resolve() for root in roots))


def _queued_upload_names(uploads: Path) -> "set[str]":
    """Top-level names in a legacy ``uploads/`` that its HPC3 copy queue (``uploads/.staging``)
    refers to: each job's local file and each folder upload. Unreadable entries name nothing."""
    import json
    names: set[str] = set()
    for path in (uploads / ".staging").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if path.name.startswith("folder-"):
            names.add(str(data.get("folder") or ""))
            continue
        names.add(str(data.get("folder") or ""))
        try:
            names.add(Path(str(data.get("local_path") or "")).relative_to(uploads).parts[0])
        except (ValueError, IndexError):
            pass
    names.discard("")
    return names


def _managed_folders() -> "list[tuple[Path, Path]]":
    """Every dataset folder on disk with the folder it sits in: ``users/*/datasets/<uuid>`` and the
    flat ``datasets/<uuid>``. A name that is not a UUID, a link, or a file is not one."""
    parents = [data_root() / "datasets"]
    users = data_root() / "users"
    if users.is_dir():
        parents += [d / "datasets" for d in sorted(users.iterdir())
                    if d.is_dir() and not d.is_symlink() and account_name(d.name) == d.name]
    found = []
    for parent in parents:
        if not parent.is_dir() or parent.is_symlink():
            continue
        for folder in parent.iterdir():
            if folder.is_symlink() or not folder.is_dir():
                continue
            try:
                if str(uuid.UUID(folder.name)) == folder.name:
                    found.append((folder, parent))
            except ValueError:
                continue
    return found


def reconcile(*, user_id: int | None = None, cleanup: bool = False) -> dict:
    """Dry-run local managed/legacy storage; remote and bound paths are left alone."""
    from sqlalchemy import select
    from .db import session_scope
    from .models import Dataset, User

    report = {"dry_run": not cleanup, "missing": [], "orphans": [], "errors": []}
    with session_scope() as session:
        rows = session.scalars(select(Dataset)).all()
        all_paths = [Path(row.path).resolve() for row in rows]
        legacy = []
        for base in legacy_results_roots():
            for owner in session.scalars(select(User)).all():
                root = base / account_name(owner.username) / "uploads"
                if root.resolve() == root:
                    legacy.append((owner.id, root))
        local_roots = [data_root() / "datasets", data_root() / "users"] + [root for _, root in legacy]
        for row in rows:
            if user_id is not None and row.user_id != user_id:
                continue
            path = Path(row.path).resolve()
            if not any(path != root and path.is_relative_to(root) for root in local_roots):
                continue
            if not path.exists():
                report["missing"].append(row.public())
                if cleanup:
                    session.delete(row)
        candidates = list(_managed_folders()) if user_id is None else []
        # Legacy names have no UUID metadata. Only administrators may remove orphans, and never one
        # the upload queue still holds: a folder being uploaded is registered only at its end, and a
        # file whose copy to HPC3 failed is kept on this host for "Retry copy".
        if user_id is None:
            for _, root in legacy:
                if root.is_dir():
                    queued = _queued_upload_names(root)
                    candidates.extend((path, root) for path in root.iterdir()
                                      if not path.name.startswith(".") and not path.is_symlink()
                                      and path.name not in queued)
        recent = time.time() - _PUBLISH_GRACE_S
        for folder, root in candidates:
            if folder.resolve() != folder or not folder.is_relative_to(root) or folder == root:
                continue
            try:
                if folder.stat().st_mtime > recent:   # just published: its row is being written
                    continue
            except OSError:
                continue
            if any(p == folder or p.is_relative_to(folder) or folder.is_relative_to(p)
                   for p in all_paths):
                continue
            report["orphans"].append(str(folder))
            if cleanup:
                try:
                    if folder.is_dir():
                        shutil.rmtree(folder)
                    else:
                        folder.unlink()
                except OSError as exc:
                    report["errors"].append({"path": str(folder), "error": str(exc)})
        if cleanup:
            session.commit()
    return report


def migrate_dataset(user_id: int, dataset_id: int, *, apply: bool = False) -> dict:
    """Copy a legacy dataset on this host into its owner's dataset folder, then repoint its row;
    never delete the source. (A dataset kept on HPC3 is brought back by ``dataset_homing``.)"""
    from .db import session_scope
    from .models import Dataset

    with session_scope() as session:
        row = session.get(Dataset, dataset_id)
        if row is None or row.user_id != user_id:
            raise ValueError("Dataset not found")
        owner = account_name(row.owner.username)
        source_path = Path(row.path)
        source = source_path.resolve()
        if source_path.is_symlink() or source_path.absolute() != source:
            raise ValueError("Legacy dataset path contains a symlink")
        if source.is_dir() and any(p.is_symlink() for p in source.rglob("*")):
            raise ValueError("Legacy dataset contains a symlink")
        if not source.exists():
            raise ValueError("Legacy dataset is missing")
        target = dataset_dir(row.uuid, owner) / "original"
        destination = target if source.is_dir() else target / source.name
        result = {"dry_run": not apply, "source": str(source), "destination": str(destination)}
        if is_owned(source, owner):
            result["already_managed"] = True
            return result
        if apply:
            ensure_layout()
            stage = temp_dir(row.uuid, owner)
            if stage.exists() or target.exists():
                raise FileExistsError("Migration destination already exists; inspect before retrying")
            stage.mkdir(parents=True)
            if source.is_dir():
                shutil.copytree(source, stage, dirs_exist_ok=True)
            else:
                shutil.copy2(source, stage / source.name)
            publish_dataset(stage, row.uuid, owner)
            row.path = str(destination)
            session.commit()
        return result
