"""Removing the HPC3 originals of datasets that are now kept on the server (RCHENLAB/AiScientist
issue 6).

A dataset that lived only on HPC3 is copied to the server when its owner connects
(``app._home_dataset``). The copy's ``source.json`` names the HPC3 original, which is kept. This
module removes such originals, explicitly and in two steps, each a Slurm job of
``deploy/hpc3/aiscientist_remove_originals.sh`` (never a command on a login node):

    check    the gateway reads every ``source.json`` of the account and keeps, with the reason, any
             original it must not touch; a dry-run job then checks each remaining one on HPC3
             (kind, no link, size equal to the server copy's) and says what it would remove
    remove   only after that dry run finished, and within a day of it: the originals it would
             remove, and that are still candidates now, go to a second job, which checks each one
             again before removing it
    settle   the job's log says what was removed; each dataset's ``source.json`` records it, so
             the next check does not list it again

What is never removed: anything outside the session's own HPC3 uploads folder
(``<shared root>/uploads/<user>/``); an original copied by another HPC3 account; one whose server
copy is missing, incomplete or no longer what the dataset reads; one a dataset row still reads on
HPC3; and, unless the deployment sets ``AISCIENTIST_REMOVE_PERSONAL_ORIGINALS=1``, anything in the
user's personal lab folder (``<lab>/<user>/``, where results were saved and old uploads went).
The script refuses the same things on its own.

Plans are kept per account in ``<workspace>/.hpc-originals.json``. No SSH here: the gateway
supplies the executor and the backend.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import dataset_storage
from .slurm_job import SlurmJobSpec

SCRIPT_SRC = Path(__file__).resolve().parents[3] / "deploy" / "hpc3" / "aiscientist_remove_originals.sh"
SCRIPT_NAME = "remove_originals.sh"
JOB_PREFIX = "aiscientist-originals-"
PERSONAL_ENV = "AISCIENTIST_REMOVE_PERSONAL_ORIGINALS"
STORE_NAME = ".hpc-originals.json"
KEEP_PLANS = 10
PLAN_TTL_S = 24 * 3600        # a removal must follow its dry run within a day
LOG_MAX_BYTES = 1024 * 1024

_TERMINAL_OK = ("COMPLETED",)
_TERMINAL_BAD = ("FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL",
                 "DEADLINE", "PREEMPTED", "REVOKED")
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def personal_allowed(env: "dict[str, str] | None" = None) -> bool:
    """True when the deployment lets originals in personal lab folders be removed (off by default)."""
    value = (env if env is not None else os.environ).get(PERSONAL_ENV, "")
    return value.strip().lower() in ("1", "true", "yes", "on")


def _inside(path: str, base: str) -> bool:
    base = base.rstrip("/")
    return bool(base) and path.startswith(base + "/") and len(path) > len(base) + 1


def _clean_abs(path: str) -> bool:
    if not path.startswith("/") or any(c in path for c in "\t\n\r\x00"):
        return False
    parts = path.split("/")[1:]
    return bool(parts) and all(p not in ("", ".", "..") for p in parts)


def _local_bytes(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file() and not p.is_symlink())


def _kind(source: dict, original: Path, hpc3_path: str) -> str:
    """``file`` or ``dir``: recorded in source.json since it says so, otherwise read off the copy
    (one file named as the original is a file; anything else a folder). The script checks it."""
    if source.get("kind") in ("file", "dir"):
        return str(source["kind"])
    entries = list(original.iterdir()) if original.is_dir() else []
    name = hpc3_path.rstrip("/").rsplit("/", 1)[-1]
    return "file" if len(entries) == 1 and entries[0].is_file() and entries[0].name == name else "dir"


# --- what may be removed -------------------------------------------------------------------------

def candidates(owner: str, hpc_user: str, *, uploads_dir: str, personal_dir: str,
               rows_by_uuid: "dict[str, str]", still_on_hpc: Iterable[str],
               allow_personal: bool) -> "tuple[list[dict], list[dict]]":
    """``(to check, kept)`` for every ``source.json`` in the account's dataset folder.

    ``rows_by_uuid`` maps each of the account's dataset UUIDs to the path its row reads;
    ``still_on_hpc`` lists the paths its rows still read on HPC3. Each entry to check is
    ``{"path", "kind", "bytes", "dataset", "name", "personal"}``; each kept one ``{"path", "name",
    "reason"}``."""
    try:
        root = dataset_storage.datasets_root(owner)
    except ValueError:
        return [], []
    on_hpc = [p.rstrip("/") for p in still_on_hpc if p]
    check: list[dict] = []
    kept: list[dict] = []
    for source_file in sorted(root.glob("*/source.json")) if root.is_dir() else []:
        tree = source_file.parent
        try:
            source = json.loads(source_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            kept.append({"path": "", "name": tree.name, "reason": "its source.json cannot be read"})
            continue
        path = str(source.get("hpc3_path") or "").rstrip("/")
        identity = tree.name
        row_path = rows_by_uuid.get(identity, "")
        name = path.rsplit("/", 1)[-1] or identity

        def keep(reason: str) -> None:
            kept.append({"path": path, "name": name, "dataset": identity, "reason": reason})

        if source.get("hpc3_removed"):
            continue                                        # done before: nothing to list
        if not _clean_abs(path):
            keep("its HPC3 path is not a clean absolute path")
            continue
        if source.get("hpc3_user") and source.get("hpc3_user") != hpc_user:
            keep(f"copied by the HPC3 account {source.get('hpc3_user')}: only its own sessions remove it")
            continue
        personal = False
        if _inside(path, uploads_dir):
            pass
        elif _inside(path, personal_dir):
            personal = True
            if not allow_personal:
                keep("in your personal lab folder, which AiScientist does not clean unless the "
                     "deployment allows it")
                continue
        else:
            keep("outside your HPC3 uploads folder")
            continue
        original = tree / "original"
        if not row_path or not Path(row_path).is_relative_to(tree):
            keep("no dataset reads this server copy")
            continue
        if not original.is_dir():
            keep("the server copy is missing")
            continue
        try:
            expected = int(source.get("bytes"))
        except (TypeError, ValueError):
            keep("source.json does not say the copy's size")
            continue
        have = _local_bytes(original)
        if have != expected:
            keep(f"the server copy holds {have} of {expected} bytes")
            continue
        if any(p == path or _inside(p, path) or _inside(path, p) for p in on_hpc):
            keep("a dataset still reads it on HPC3")
            continue
        check.append({"path": path, "kind": _kind(source, original, path), "bytes": expected,
                      "dataset": identity, "name": name, "personal": personal})
    return check, kept


def manifest(entries: Iterable[dict]) -> str:
    """The script's manifest: ``<kind> TAB <bytes> TAB <path>`` per line."""
    return "".join(f"{e['kind']}\t{int(e['bytes'])}\t{e['path']}\n" for e in entries)


# --- the job --------------------------------------------------------------------------------------

def remote_paths(shared_root: str, user: str, plan_id: str, phase: str) -> dict:
    """Where a plan's files go on HPC3, under ``<shared root>/bin/<user>/``."""
    bin_dir = f"{shared_root.rstrip('/')}/bin/{user}"
    return {"bin": bin_dir, "script": f"{bin_dir}/{SCRIPT_NAME}",
            "dir": f"{bin_dir}/originals",
            "manifest": f"{bin_dir}/originals/{plan_id}.{phase}.tsv",
            "log": f"{bin_dir}/originals/{plan_id}.{phase}.log"}


def command(script: str, shared_root: str, user: str, manifest_path: str, *,
            personal_dir: str = "", dry_run: bool = True) -> str:
    """The script invocation the job runs."""
    parts = [f"bash {shlex.quote(script)}", f"--root {shlex.quote(shared_root.rstrip('/'))}",
             f"--user {shlex.quote(user)}", f"--manifest {shlex.quote(manifest_path)}"]
    if personal_dir:
        parts.append(f"--personal {shlex.quote(personal_dir.rstrip('/'))}")
    if dry_run:
        parts.append("--dry-run")
    return " ".join(parts)


def job_spec(payload: str, user: str, log_path: str, submit_dir: str, *,
             partition: str, account: str = "") -> SlurmJobSpec:
    """A one-CPU job on the CPU partition (``standard``, charged to the lab's account) that runs
    ``payload``."""
    lines = ["#!/bin/bash", f"#SBATCH --job-name={JOB_PREFIX}{user}", f"#SBATCH --partition={partition}"]
    if account:
        lines.append(f"#SBATCH --account={account}")
    lines += ["#SBATCH --cpus-per-task=1", "#SBATCH --mem=2G", "#SBATCH --time=01:00:00",
              f"#SBATCH --output={log_path}", "#SBATCH --open-mode=truncate", payload]
    return SlurmJobSpec(script="\n".join(lines), job_name=f"{JOB_PREFIX}{user}", submit_dir=submit_dir)


def stage(executor: Any, shared_root: str, user: str, plan_id: str, phase: str,
          entries: Iterable[dict]) -> dict:
    """Put the script and this phase's manifest on HPC3 (through the transfer host); returns the
    remote paths."""
    paths = remote_paths(shared_root, user, plan_id, phase)
    executor.exec(f"mkdir -p {shlex.quote(paths['dir'])}", timeout=30)
    executor.put_file(str(SCRIPT_SRC), paths["script"])
    fd, local = tempfile.mkstemp(prefix="originals-", suffix=".tsv")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(manifest(entries))
        executor.put_file(local, paths["manifest"])
    finally:
        os.unlink(local)
    return paths


def submit(backend: Any, executor: Any, plan: dict, phase: str, entries: list[dict], *,
           shared_root: str, partition: str, account: str = "", personal_dir: str = "") -> dict:
    """Stage and submit one phase (``check`` = the dry run, ``remove``); returns its job record."""
    user, plan_id = plan["hpc_user"], plan["id"]
    paths = stage(executor, shared_root, user, plan_id, phase, entries)
    payload = command(paths["script"], shared_root, user, paths["manifest"],
                      personal_dir=personal_dir, dry_run=phase == "check")
    job_id = backend.submit(job_spec(payload, user, paths["log"], paths["bin"],
                                     partition=partition, account=account))
    return {"job_id": job_id, "status": "queued", "log": paths["log"], "submitted_at": _now(),
            "entries": len(entries)}


def parse_log(text: str) -> dict:
    """``{"results": [{"verdict", "path", "bytes" | "reason"}], "summary": {...} | None}`` from a
    job's log; ``summary`` is the ``ORIGINALS_RESULT`` line (None when the job did not reach it)."""
    results: list[dict] = []
    summary = None
    for line in (text or "").splitlines():
        if line.startswith("ORIGINALS_RESULT "):
            summary = {}
            for field in line.split()[1:]:
                key, _, value = field.partition("=")
                summary[key] = int(value) if value.isdigit() else value
            continue
        verdict, _, rest = line.partition("\t")
        middle, _, path = rest.partition("\t")
        if verdict in ("WOULD_REMOVE", "REMOVED") and middle.isdigit() and path:
            results.append({"verdict": verdict.lower(), "bytes": int(middle), "path": path})
        elif verdict == "SKIP" and path:
            results.append({"verdict": "skip", "reason": middle, "path": path})
    return {"results": results, "summary": summary}


def is_terminal(state: str) -> bool:
    """True when a Slurm state (``sacct``) is one a job does not leave."""
    head = (state or "").split()[0].upper() if state else ""
    return bool(head) and head.startswith(_TERMINAL_OK + _TERMINAL_BAD)


def settle_job(job: dict, state: str, log_text: str) -> bool:
    """Bring a queued job record up to date from its Slurm ``state`` and its log. True when it
    changed. A job still queued or running, or one Slurm has no record of yet, is left."""
    if job.get("status") != "queued":
        return False
    head = (state or "").split()[0].upper() if state else ""
    if not is_terminal(state):
        if head:
            job["slurm_state"] = head
        return False
    parsed = parse_log(log_text)
    job["slurm_state"] = head
    job["results"] = parsed["results"]
    job["summary"] = parsed["summary"]
    ok = (head.startswith(_TERMINAL_OK) and parsed["summary"] is not None
          and str(parsed["summary"].get("job", "")) in ("", str(job.get("job_id"))))
    job["status"] = "done" if ok else "failed"
    if not ok:
        job["error"] = (f"the job ended {head}" if not head.startswith(_TERMINAL_OK)
                        else "the job finished without its result line")
    job["ended_at"] = _now()
    return True


def record_removed(owner: str, plan: dict) -> int:
    """Mark each original the plan's removal job removed in its dataset's ``source.json``. Returns
    how many were marked."""
    removed = {r["path"] for r in ((plan.get("remove") or {}).get("results") or [])
               if r.get("verdict") == "removed"}
    marked = 0
    for entry in plan.get("candidates") or []:
        if entry["path"] not in removed:
            continue
        try:
            source_file = dataset_storage.dataset_dir(entry["dataset"], owner) / "source.json"
            source = json.loads(source_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if source.get("hpc3_path", "").rstrip("/") != entry["path"] or source.get("hpc3_removed"):
            continue
        source["hpc3_removed"] = {"at": _now(), "job_id": plan["remove"].get("job_id"),
                                  "plan": plan["id"]}
        source["note"] = "This dataset was kept only on HPC3; this folder holds its copy, and the HPC3 original was removed."
        source_file.write_text(json.dumps(source, indent=2), encoding="utf-8")
        marked += 1
    return marked


def removable(plan: dict, now_candidates: Iterable[dict], *, now: "float | None" = None) -> "tuple[list[dict], str]":
    """What a removal of ``plan`` may take: the originals its dry run would remove that are still
    candidates now, unchanged. ``(entries, "")``, or ``([], why not)``."""
    check = plan.get("check") or {}
    if check.get("status") != "done":
        return [], "the check on HPC3 has not finished"
    if plan.get("remove"):
        return [], "these originals were already submitted for removal"
    if (now if now is not None else time.time()) - float(plan.get("created_ts") or 0) > PLAN_TTL_S:
        return [], "the check is more than a day old: check again"
    would = {r["path"]: r["bytes"] for r in check.get("results") or [] if r.get("verdict") == "would_remove"}
    current = {(c["path"], c["kind"], int(c["bytes"])): c for c in now_candidates}
    entries = [c for (path, kind, size), c in current.items() if would.get(path) == size]
    if not entries:
        return [], "nothing the check found can be removed now"
    return entries, ""


# --- plans of one account -------------------------------------------------------------------------

def new_plan(owner: str, hpc_user: str, check: list[dict], kept: list[dict], *, personal: bool) -> dict:
    return {"id": uuid.uuid4().hex[:12], "owner": owner, "hpc_user": hpc_user,
            "created_at": _now(), "created_ts": time.time(), "personal": personal,
            "candidates": check, "kept": kept, "check": None, "remove": None}


def _store(store_dir: Path) -> Path:
    return Path(store_dir) / STORE_NAME


def load_plans(store_dir: Path) -> list[dict]:
    try:
        data = json.loads(_store(store_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [p for p in data if isinstance(p, dict) and p.get("id")] if isinstance(data, list) else []


def get_plan(store_dir: Path, plan_id: str) -> "dict | None":
    return next((p for p in load_plans(store_dir) if p["id"] == plan_id), None)


def save_plan(store_dir: Path, plan: dict) -> None:
    with _LOCK:
        plans = [p for p in load_plans(store_dir) if p["id"] != plan["id"]] + [plan]
        path = _store(store_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(plans[-KEEP_PLANS:], indent=1), encoding="utf-8")
        os.replace(tmp, path)


def public(plan: dict) -> dict:
    """A plan as the console reads it, with the totals it shows."""
    check = plan.get("check") or {}
    remove = plan.get("remove") or {}
    would = [r for r in check.get("results") or [] if r.get("verdict") == "would_remove"]
    done = [r for r in remove.get("results") or [] if r.get("verdict") == "removed"]
    return {**plan,
            "candidate_bytes": sum(int(c["bytes"]) for c in plan.get("candidates") or []),
            "would_remove": len(would), "would_remove_bytes": sum(r["bytes"] for r in would),
            "removed": len(done), "removed_bytes": sum(r["bytes"] for r in done)}
