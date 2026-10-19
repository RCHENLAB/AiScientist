"""UUID upload sessions layered over the existing resumable transfer machinery."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from uuid import UUID, uuid4

from . import dataset_storage


def enabled(conn) -> bool:
    """UUID upload sessions under ``DATA_ROOT/temp`` when the deployment configured a data root
    (:func:`dataset_storage.configured`); otherwise the account's ``uploads/`` as before."""
    return getattr(conn, "managed_storage", False) or dataset_storage.configured()


def workspace(conn) -> Path:
    if not enabled(conn):
        return conn.workspace
    # owner is gateway-sanitized, but keep this primitive safe for other callers too.
    if not conn.owner or Path(conn.owner).name != conn.owner or conn.owner in (".", ".."):
        raise ValueError("invalid upload owner")
    return dataset_storage.data_root() / "temp" / conn.owner


def uploads(conn) -> Path:
    return workspace(conn) / "uploads"


def ttl_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("AISCIENTIST_UPLOAD_TTL_SECONDS", "86400")))
    except ValueError:
        return 86400.0


def remote_identity(path: str) -> str | None:
    match = re.search(r"/(?:temp|datasets)/([0-9a-f-]{36})/(?:original|processed)(?:/|$)", path)
    return str(UUID(match[1])) if match else None


def session_path(conn, identifier: str) -> Path:
    if str(UUID(identifier)) != identifier:
        raise ValueError("invalid dataset UUID")
    return uploads(conn) / ".sessions" / f"{identifier}.json"


def reserve(conn, name: str, files=None) -> dict:
    identifier = str(uuid4())
    chosen = files if isinstance(files, list) else None
    meta = {"uuid": identifier, "owner": conn.owner, "original_name": name,
            "files": chosen, "created_at": time.time()}
    path = session_path(conn, identifier)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta), encoding="utf-8")
    (uploads(conn) / identifier).mkdir(parents=True)
    return {"folder": identifier, "merged": False, "have": [], "name": name}


def session(conn, identifier: str) -> dict:
    path = session_path(conn, identifier)
    meta = json.loads(path.read_text(encoding="utf-8"))
    if meta.get("owner") != conn.owner:
        raise ValueError("upload owner mismatch")
    return meta


def publish(conn, identifier: str, expected: int = 0) -> dict:
    meta = session(conn, identifier)
    root = uploads(conn) / identifier
    dest = dataset_storage.dataset_dir(identifier, conn.owner) / "original"
    source = root if root.is_dir() else dest
    files = [p for p in source.rglob("*") if p.is_file()]
    if any(p.is_symlink() or not p.resolve().is_relative_to(source.resolve()) for p in source.rglob("*")):
        raise ValueError("upload contains an unsafe path")
    manifest = meta.get("files")
    actual = {p.relative_to(source).as_posix(): p.stat().st_size for p in files}
    if manifest is not None:
        wanted = {str(item[0]): int(item[1]) for item in manifest}
        if wanted != actual:
            raise ValueError("folder is incomplete or file sizes do not match the manifest")
    elif len(files) != expected:
        raise ValueError("folder is incomplete")
    if root.is_dir():
        dataset_storage.publish_dataset(root, identifier, conn.owner)
    return {"name": meta["original_name"], "path": str(dest), "uuid": identifier,
            "size": sum(actual.values()), "count": len(files), "kind": "folder"}


def cancel(conn, identifier: str) -> None:
    session(conn, identifier)  # ownership and UUID validation before touching the tree
    root = uploads(conn) / identifier
    if root.is_symlink():
        raise ValueError("unsafe upload directory")
    from . import tus_upload
    for path in (uploads(conn) / ".parts").glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("folder") == identifier:
            if tus_upload._lock(path.stem).locked():
                raise OSError("An upload request is still closing; retry cancellation")
            for suffix in (".part", ".incoming", ".json"):
                path.with_suffix(suffix).unlink(missing_ok=True)
    if root.exists():
        shutil.rmtree(root)
    session_path(conn, identifier).unlink(missing_ok=True)


def expire_sessions(conn) -> None:
    from . import app, staging, tus_upload
    root = uploads(conn) / ".sessions"
    cutoff = time.time() - ttl_seconds()
    for path in root.glob("*.json"):
        if path.stat().st_mtime < cutoff:
            try:
                jobs = [job for job in staging.StagingStore(uploads(conn) / ".staging").load_all()
                        if job.folder == path.stem]
                busy = False
                for sidecar in (uploads(conn) / ".parts").glob("*.json"):
                    meta = json.loads(sidecar.read_text(encoding="utf-8"))
                    if meta.get("folder") == path.stem and tus_upload._lock(sidecar.stem).locked():
                        busy = True
                        break
                queue = app._STAGING.get(conn.owner)
                running = bool(queue and any(job.id in queue._futures and not queue._futures[job.id].done()
                                             for job in jobs))
                if busy or running or any(job.updated_at >= cutoff for job in jobs):
                    continue
                meta = session(conn, path.stem)
                # Only a copy to HPC3 leaves anything there; a file kept on this host goes with cancel().
                remote_jobs = [job for job in jobs if job.dest != "server"]
                pending_remote = bool(remote_jobs or meta.get("remote_cleanup_pending"))
                hpc_user = remote_jobs[0].hpc_user if remote_jobs else meta.get("cleanup_hpc_user", "")
                live = next((c for c in app.CONNECTIONS.values()
                             if c.owner == conn.owner and c.executor is not None
                             and (not hpc_user or app._hpc_user(c) == hpc_user)), None)
                if pending_remote and live is not None:
                    import shlex
                    remote = f"{app._hpc_uploads_dir(live)}/temp/{path.stem}"
                    result = live.executor.exec(f"rm -rf -- {shlex.quote(remote)}")
                    if not result.ok:
                        raise OSError(result.stderr or "Expired remote upload cleanup failed")
                    pending_remote = False
                old_time = path.stat().st_mtime
                cancel(conn, path.stem)
                if pending_remote:
                    # The bytes expire now; retain a small cleanup record until SSH reconnects.
                    meta["remote_cleanup_pending"] = True
                    meta["cleanup_hpc_user"] = hpc_user
                    path.write_text(json.dumps(meta), encoding="utf-8")
                    os.utime(path, (old_time, old_time))
                if queue:
                    for job in jobs:
                        if job.state in staging.ACTIVE:
                            queue.cancel(job.id)
                        queue.forget(job.id)
                else:
                    store = staging.StagingStore(uploads(conn) / ".staging")
                    for job in jobs:
                        store.delete(job.id)
            except (ValueError, FileNotFoundError):
                continue


def expire_all() -> None:
    """Sweep disconnected accounts as well as connected ones: stopped uploads nobody continued for
    ``ttl_seconds`` go, in either layout."""
    from types import SimpleNamespace
    from . import app, tus_upload
    if not dataset_storage.configured():
        for parts in app.CONSOLE_RUNS_DIR.glob("*/uploads/.parts"):
            if parts.is_dir() and not parts.is_symlink():
                tus_upload._cleanup_parts(parts)
        return
    root = dataset_storage.data_root() / "temp"
    for owner in root.iterdir() if root.is_dir() else ():
        if not owner.is_dir() or owner.is_symlink():
            continue
        conn = SimpleNamespace(owner=owner.name, mock=False, managed_storage=True)
        tus_upload._cleanup_parts(uploads(conn) / ".parts")
        expire_sessions(conn)
