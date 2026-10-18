"""UUID dataset storage and conservative maintenance of registry/disk references."""
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
    for name in ("datasets", "analysis_results", "temp", "exports"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def dataset_dir(dataset_uuid: str) -> Path:
    canonical = str(uuid.UUID(dataset_uuid))
    return data_root() / "datasets" / canonical


def temp_dir(dataset_uuid: str) -> Path:
    return data_root() / "temp" / str(uuid.UUID(dataset_uuid))


def managed_uuid(path: str | Path) -> str | None:
    try:
        rel = Path(path).resolve().relative_to(data_root() / "datasets")
        return str(uuid.UUID(rel.parts[0]))
    except (ValueError, IndexError):
        return None


def publish_dataset(staged_dir: str | Path, dataset_uuid: str) -> Path:
    """Publish a verified staged tree, returning its original/ directory.

    The caller verifies its complete manifest first. A repeated call after a successful
    move is safe; an existing destination never silently overwrites another tree.
    """
    ensure_layout()
    source = Path(staged_dir).resolve()
    if Path(staged_dir).absolute() != source or any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("Dataset staging contains a symlink")
    root = dataset_dir(dataset_uuid)
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
    identity = managed_uuid(path)
    if identity is None:
        raise ValueError("Path is not a managed dataset")
    root = dataset_dir(identity)
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
                name = "".join(c for c in owner.username if c.isalnum() or c in "-_") or "guest"
                root = base / name / "uploads"
                if root.resolve() == root:
                    legacy.append((owner.id, root))
        local_roots = [data_root() / "datasets"] + [root for _, root in legacy]
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
        candidates = []
        parent = data_root() / "datasets"
        if user_id is None and parent.exists():
            for folder in parent.iterdir():
                if folder.is_symlink() or not folder.is_dir():
                    continue
                try:
                    if dataset_dir(folder.name) == folder:
                        candidates.append((folder, parent))
                except ValueError:
                    continue
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
    """Copy a legacy dataset, then repoint its row; never delete the source."""
    from .db import session_scope
    from .models import Dataset

    with session_scope() as session:
        row = session.get(Dataset, dataset_id)
        if row is None or row.user_id != user_id:
            raise ValueError("Dataset not found")
        source_path = Path(row.path)
        source = source_path.resolve()
        if source_path.is_symlink() or source_path.absolute() != source:
            raise ValueError("Legacy dataset path contains a symlink")
        if source.is_dir() and any(p.is_symlink() for p in source.rglob("*")):
            raise ValueError("Legacy dataset contains a symlink")
        if not source.exists():
            raise ValueError("Legacy dataset is missing")
        target = dataset_dir(row.uuid) / "original"
        destination = target if source.is_dir() else target / source.name
        result = {"dry_run": not apply, "source": str(source), "destination": str(destination)}
        if managed_uuid(source):
            result["already_managed"] = True
            return result
        if apply:
            ensure_layout()
            stage = temp_dir(row.uuid) / "migration"
            if stage.exists() or target.exists():
                raise FileExistsError("Migration destination already exists; inspect before retrying")
            stage.mkdir(parents=True)
            if source.is_dir():
                shutil.copytree(source, stage, dirs_exist_ok=True)
            else:
                shutil.copy2(source, stage / source.name)
            publish_dataset(stage, row.uuid)
            row.path = str(destination)
            session.commit()
        return result
