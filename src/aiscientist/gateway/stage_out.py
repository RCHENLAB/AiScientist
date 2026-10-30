"""Bringing a run's results home from HPC3 before its folder there is removed (RCHENLAB/AiScientist
issue 6).

What counts as a result
-----------------------
A run on HPC3 writes three kinds of files. Only the first two are results:

* ``artifacts/``: the report, figures, tables and the run's record. Copied to the server after every
  step, as before; the run is published from that copy.
* ``result/``: what the run hands on, too large for the bundle: the processed dataset
  (``<input>.processed.<time>.<run>.h5ad`` and its ``.json`` record) and CellQC's result folder.
  Copied home by this module when the session ends, every file under ``result/``, whether or not a
  record names it.
* Everything else is machinery and never comes back: ``work/`` (the AnnData checkpoints a step reads
  from the one before), the copies of the inputs, the synced source, job scripts and their
  args/result JSON, the agent's shell home and downloads.

The job logs (``*.out``, ``*.err``, ``*.log`` of each job of the run, at most
:data:`LOG_MAX_BYTES` each) are copied too, to ``<run>/logs/hpc3/``, so a failure can still be read
after the HPC3 folder is gone. They are not results: a log that cannot be copied is reported and
does not keep the folder.

Where it goes, and how it is checked
------------------------------------
Into the run's own folder on the server, ``<results dir>/<owner>/<run_id>/result/``, where a run that
computed on the server keeps the same outputs. A result the user already saved to their data is not
copied again: its saved copy stands for it when it still has the size HPC3 reports. Each file goes
through a ``.part`` file that resumes after an interruption and must arrive at the size HPC3 lists.
Then the SHA-256 of every server copy is computed and written to ``<run>/stage_out.json`` (the
manifest) and the run's records are pointed at the server copies (``location: gateway``).

The removal on HPC3 (``deploy/hpc3/aiscientist_session_clean.sh --verify-dir``) takes the manifest
and hashes the HPC3 files again on a compute node: a run folder is removed only when every file under
its ``result/`` is in the manifest with the same size and checksum. Anything else keeps the folder.

This module knows nothing of HTTP or of the gateway's connections; the gateway calls it from a
worker thread, and the tests drive it with a fake executor.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

MANIFEST_NAME = "stage_out.json"
RESULT_DIR = "result"
LOG_DIR = Path("logs") / "hpc3"
LOG_SUFFIXES = (".out", ".err", ".log")
LOG_MAX_BYTES = 20 * 1024 * 1024
# The pointer files a run writes into ``artifacts/data/`` for what it hands on.
_POINTER_RE = re.compile(r"(?:result_dataset(?:\.[A-Za-z0-9_-]+)?|cellqc_result)\.json")

# (bytes done, bytes in all, files done, files in all), as compute.ByteCounter
ByteCounter = Callable[[int, int, int, int], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    """The SHA-256 of a whole file, whatever its size (``metadata.facts.sha256_file`` stops at a cap)."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def safe_rel(rel: str) -> "str | None":
    """A path inside the result folder as HPC3 listed it; None for one that could leave it."""
    rel = str(rel or "")
    if not rel or "\x00" in rel or "\t" in rel or "\n" in rel or rel.startswith("/"):
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def list_remote_files(ex: Any, root: str) -> "list[tuple[str, int]]":
    """Every regular file under ``root`` on HPC3, as ``(path relative to root, size)``, hidden files
    included: the removal checks every file, so every file has to come home. A missing folder is an
    empty list. One ``find`` of metadata on the login node, like the Storage tab's listing."""
    q = shlex.quote(root.rstrip("/"))
    res = ex.exec(f"if [ -d {q} ] && [ ! -L {q} ]; then find {q} -type f -printf '%s\\t%P\\n'; fi",
                  timeout=300.0)
    if not getattr(res, "ok", False):
        raise OSError((getattr(res, "stderr", "") or f"{root} cannot be listed on HPC3").strip()[:300])
    out: list[tuple[str, int]] = []
    for line in (getattr(res, "stdout", "") or "").splitlines():
        size, _, rel = line.partition("\t")
        clean = safe_rel(rel)
        if clean is None or not size.isdigit():
            raise OSError(f"unexpected file name in {root}: {rel!r}")
        out.append((clean, int(size)))
    return sorted(out)


# --- the run's records of what it hands on --------------------------------------------------------

def pointer_files(run_dir: Path) -> list[Path]:
    """The run's records of what it hands on (``artifacts/data/result_dataset*.json``,
    ``cellqc_result.json``)."""
    data = Path(run_dir) / "artifacts" / "data"
    if not data.is_dir():
        return []
    return sorted(p for p in data.iterdir() if _POINTER_RE.fullmatch(p.name) and p.is_file()
                  and not p.is_symlink())


def _read(path: Path) -> "dict | None":
    try:
        rec = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _records(run_dir: Path) -> "list[tuple[Path, dict]]":
    out = []
    for p in pointer_files(run_dir):
        rec = _read(p)
        if rec is None:
            continue
        for item in (rec.get("datasets") or [rec]):
            if isinstance(item, dict) and item.get("name"):
                out.append((p, item))
    return out


def _result_rel(rec: dict, remote_result: str) -> "str | None":
    """Where a record's handed-on output sits inside ``result/`` on HPC3, from its ``path``."""
    path = str(rec.get("path") or "")
    prefix = remote_result.rstrip("/") + "/"
    return safe_rel(path[len(prefix):]) if path.startswith(prefix) else None


def saved_copy(rel: str, records: "list[tuple[Path, dict]]", remote_result: str) -> "Path | None":
    """The copy on the server that a result already saved to the user's data has of ``rel``, or None.
    ``rel`` is a file under ``result/``: the saved file itself, its ``.json`` record beside it, or a
    file inside a saved folder."""
    for _p, rec in records:
        saved = str(rec.get("saved_to") or "")
        own = _result_rel(rec, remote_result) if rec.get("location") == "hpc3" else None
        if not saved or not own or not Path(saved).is_absolute():
            continue
        if rel == own:
            return Path(saved)
        if rel == own + ".json":
            return Path(saved).with_name(Path(saved).name + ".json")
        if rel.startswith(own + "/"):                      # a file inside a saved folder
            return Path(saved) / rel[len(own) + 1:]
    return None


# --- one run ------------------------------------------------------------------------------------

def stage_out_run(ex: Any, pull: Callable[..., int], *, run_id: str, run_dir: Path,
                  remote_run: str, log_dirs: "Iterable[tuple[str, str]]" = (), session: str = "",
                  hpc_user: str = "", on_bytes: "ByteCounter | None" = None) -> dict:
    """Bring one run's results home (see the module docstring) and write its manifest.

    ``remote_run`` is the run's folder on HPC3 that holds ``result/``; ``run_dir`` the run's folder
    on the server; ``log_dirs`` the ``(job line, folder)`` pairs holding the run's job logs;
    ``pull(src, dest, directory=False, on_bytes=...)`` copies one file (the compute
    backend's ``stage_out``). Returns ``{"run_id", "status", "files", "bytes", "copied",
    "copied_bytes", "entries", "logs", "log_errors"}``; ``status`` is ``done`` (results came home),
    ``nothing`` (the run has no ``result/`` on HPC3) or ``failed`` (with ``error``: the folder must be
    kept). Never raises."""
    run_dir = Path(run_dir)
    remote_result = f"{remote_run.rstrip('/')}/{RESULT_DIR}"
    out: dict[str, Any] = {"run_id": run_id, "status": "nothing", "files": 0, "bytes": 0,
                           "copied": 0, "copied_bytes": 0, "entries": [], "logs": 0,
                           "log_errors": []}
    started = time.monotonic()
    try:
        listing = list_remote_files(ex, remote_result)
        records = _records(run_dir)
        entries: list[dict] = []
        to_copy: list[tuple[str, int, Path]] = []
        for rel, size in listing:
            saved = saved_copy(rel, records, remote_result)
            if saved is not None and saved.is_file() and not saved.is_symlink() \
                    and saved.stat().st_size == size:
                entries.append({"path": rel, "size": size, "server": str(saved), "saved": True})
                continue
            dest = run_dir / RESULT_DIR / rel
            to_copy.append((rel, size, dest))
            entries.append({"path": rel, "size": size, "server": str(dest), "saved": False})
        grand = sum(size for _r, size, _d in to_copy)
        count = on_bytes or (lambda *_a: None)
        done = 0
        count(0, grand, 0, len(to_copy))
        for i, (rel, size, dest) in enumerate(to_copy, 1):
            dest.parent.mkdir(parents=True, exist_ok=True)
            base = done
            pull(f"{remote_result}/{rel}", dest, directory=False,
                 on_bytes=lambda got, _of, _n, _m, _b=base, _i=i: count(_b + got, grand, _i - 1, len(to_copy)))
            if dest.stat().st_size != size:
                raise OSError(f"{rel} arrived as {dest.stat().st_size} bytes, expected {size}")
            done += size
            count(done, grand, i, len(to_copy))
        for e in entries:
            e["sha256"] = sha256(Path(e["server"]))
        out.update(files=len(entries), bytes=sum(e["size"] for e in entries),
                   copied=len(to_copy), copied_bytes=grand, entries=entries)
        if entries:
            out["status"] = "done"
            _point_records_home(run_dir, remote_result, entries)
    except Exception as exc:  # noqa: BLE001 - reported; the caller keeps the run's folder
        out.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
        return out
    logs, errors = _copy_logs(ex, pull, run_dir, log_dirs)
    out.update(logs=logs, log_errors=errors, seconds=round(time.monotonic() - started, 1))
    if out["status"] == "done" or logs:
        write_manifest(run_dir, out, session=session, hpc_user=hpc_user, remote=remote_result)
    return out


def write_manifest(run_dir: Path, result: dict, *, session: str, hpc_user: str, remote: str) -> Path:
    """``<run>/stage_out.json``: every result file with its size, checksum and where it is kept here.
    A later stage-out of the same run (a continued run) replaces it."""
    path = Path(run_dir) / MANIFEST_NAME
    doc = {"run_id": result["run_id"], "session": session, "hpc_user": hpc_user, "remote": remote,
           "staged_at": _now(), "files": [{k: e[k] for k in ("path", "size", "sha256", "server", "saved")}
                                          for e in result.get("entries") or []],
           "logs": result.get("logs", 0)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def read_manifest(run_dir: Path) -> "dict | None":
    return _read(Path(run_dir) / MANIFEST_NAME)


def verify_tsv(entries: "Iterable[dict]") -> str:
    """What the removal on HPC3 checks a run's ``result/`` against: one line per file,
    ``<sha256>\\t<size>\\t<path>``."""
    return "".join(f"{e['sha256']}\t{int(e['size'])}\t{e['path']}\n" for e in entries)


def _point_records_home(run_dir: Path, remote_result: str, entries: list[dict]) -> int:
    """Point each record of an output that was only on HPC3 at its copy here: ``location: gateway``,
    the server ``path``, no longer kept "until the session ends". A saved one keeps its ``saved_to``."""
    copied = {e["path"]: e for e in entries if not e["saved"]}
    local_result = Path(run_dir) / RESULT_DIR
    changed = 0
    for pointer in pointer_files(run_dir):
        rec = _read(pointer)
        if rec is None:
            continue
        touched = False
        for item in (rec.get("datasets") or [rec]):
            if not isinstance(item, dict) or item.get("location") != "hpc3" or item.get("saved_to"):
                continue
            own = _result_rel(item, remote_result)
            if own is None:
                continue
            inside = [e for p, e in copied.items() if p == own or p.startswith(own + "/")]
            if not inside:
                continue
            item.update(location="gateway", path=str(local_result / own), until_disconnect=False,
                        kept_days=0, staged_out_at=_now())
            if len(inside) == 1 and inside[0]["path"] == own:
                item["sha256"] = inside[0]["sha256"]
            touched = True
        if rec.get("datasets"):
            # The aggregate's top level repeats its first dataset.
            first = rec["datasets"][0]
            if isinstance(first, dict):
                rec.update({k: first[k] for k in ("location", "path", "until_disconnect", "kept_days",
                                                  "staged_out_at", "sha256") if k in first})
        if touched:
            tmp = pointer.with_name(pointer.name + ".tmp")
            tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
            tmp.replace(pointer)
            changed += 1
    return changed


def _copy_logs(ex: Any, pull: Callable[..., int], run_dir: Path,
               log_dirs: "Iterable[tuple[str, str]]") -> "tuple[int, list[str]]":
    """Copy the run's job logs home, best-effort: ``<run>/logs/hpc3/<job line>/<file>``. Returns
    ``(copied, problems)``."""
    copied, problems = 0, []
    for line, remote in dict.fromkeys((str(n), str(d).rstrip("/")) for n, d in log_dirs if d):
        line = re.sub(r"[^A-Za-z0-9_-]", "_", line) or "jobs"
        try:
            files = list_remote_files(ex, remote)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{remote}: {exc}"[:200])
            continue
        for rel, size in files:
            if "/" in rel or not rel.endswith(LOG_SUFFIXES):
                continue
            if size > LOG_MAX_BYTES:
                problems.append(f"{rel}: {size} bytes, over the {LOG_MAX_BYTES // 2 ** 20} MB limit")
                continue
            dest = Path(run_dir) / LOG_DIR / line / rel
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                pull(f"{remote}/{rel}", dest, directory=False)
                copied += 1
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{rel}: {type(exc).__name__}: {exc}"[:200])
    return copied, problems
