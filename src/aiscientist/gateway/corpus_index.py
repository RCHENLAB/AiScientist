"""The Eye Server copy of the RetiGene corpus index: copying it from HPC3, and rebuilding the index
on HPC3 when papers are added. ``scripts/update_corpus_index.py`` is the command; searches read the
copy through ``local_corpus``.

The copy's root is ``local_corpus.local_root()`` (by default ``<data root>/reference/retigene``)::

    current -> versions/<UTC time>     the version searches read
    versions/<UTC time>/               mechanism_cache/, manifest.csv, hgnc_complete_set.txt, SOURCE.json
    hf_cache/                          the offline embedding model, shared by the versions

A copy starts as hard links to the current version. rsync then compares checksums and replaces only
the files that changed: it writes a changed file beside its link and renames it into place, so the
version being searched is never modified and an unchanged file takes no space. The new version is
checked (one vector per chunk, a manifest) and becomes current when a symlink is renamed over
``current``, in one step. A copy identical to the current version is discarded. The two newest
versions are kept, so ``rollback`` can switch back.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import local_corpus

# RCIC allows no data transfer on the HPC3 login nodes: copies go through its transfer node.
TRANSFER_HOST = "access-hpc3.rcic.uci.edu"
_LOGIN_HOST = re.compile(r"(?:hpc3|login-i\d+)(?:\.rcic\.uci\.edu)?", re.IGNORECASE)
VERSIONS = "versions"
CURRENT = "current"
SOURCE_FILE = "SOURCE.json"
KEEP = 2


class SyncError(RuntimeError):
    """The copy was not made; the current version is unchanged."""


@dataclass
class SyncResult:
    version: Path               # the current version after the sync
    changed: bool               # False: the source equals the current version, nothing switched
    files: list[str] = field(default_factory=list)   # files that differ from the previous version
    chunks: int = 0
    papers: int = 0


def source_host(source: str) -> str:
    """The host of an rsync source ``[user@]host:path``; empty for a local path."""
    head, sep, _ = source.partition(":")
    return head.rsplit("@", 1)[-1] if sep and "/" not in head else ""


def ssh_command(user: str = "", key: str = "") -> list[str]:
    """The ``ssh`` an unattended copy logs in with: a key, never a password or a Duo prompt."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        cmd += ["-i", key, "-o", "IdentitiesOnly=yes"]
    if user:
        cmd += ["-l", user]
    return cmd


def rsync_command(src: str, dst: Path, *, ssh: list[str] | None = None, delete: bool = False) -> list[str]:
    # Checksums detect changed contents even when timestamps and sizes match; -t keeps the source's
    # modification times, which is what local_corpus compares with the HPC3 index.
    cmd = ["rsync", "-rltc", "--delay-updates", "--protect-args"]
    if delete:
        cmd.append("--delete")
    if ssh:
        cmd += ["-e", shlex.join(ssh)]
    return cmd + ["--", src, str(dst)]


def versions(root: Path) -> list[Path]:
    """The versions under ``root``, oldest first (their names are UTC times)."""
    d = root / VERSIONS
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))


def current(root: Path) -> Path | None:
    link = root / CURRENT
    return link.resolve() if link.is_symlink() and link.exists() else None


def check(version: Path) -> str:
    """Why ``version`` must not be searched, or ``""``."""
    cache = version / "mechanism_cache"
    for name in ("chunks.jsonl", "chunk_vecs.npy"):
        if not (cache / name).is_file():
            return f"mechanism_cache/{name} is missing"
    if not (version / "manifest.csv").is_file():
        return "manifest.csv is missing"
    try:
        if not local_corpus.vectors_match_chunks(cache):
            return "chunk_vecs.npy does not have one row per line of chunks.jsonl"
    except (OSError, ValueError) as exc:
        return f"the index could not be read ({type(exc).__name__})"
    return ""


def _files(d: Path) -> dict[str, os.stat_result]:
    return {str(p.relative_to(d)): p.stat() for p in d.rglob("*")
            if p.is_file() and p.name != SOURCE_FILE and ".~tmp~" not in p.parts}


def _link_tree(src: Path, dst: Path) -> None:
    """``dst`` as hard links to every file of ``src``: no data is copied."""
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if p.is_dir():
            (dst / rel).mkdir(parents=True, exist_ok=True)
        elif p.is_file() and p.name != SOURCE_FILE:
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            os.link(p, dst / rel)


def _counts(cache: Path) -> tuple[int, int]:
    """(chunks, papers) of a text cache."""
    chunks, pmids = 0, set()
    with (cache / "chunks.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                chunks += 1
                with contextlib.suppress(ValueError, KeyError, TypeError):
                    pmids.add(str(json.loads(line)["pmid"]))
    return chunks, len(pmids)


def switch(root: Path, version: Path) -> None:
    """Make ``version`` current in one step: a new symlink renamed over ``current``."""
    link = root / CURRENT
    if link.exists() and not link.is_symlink():
        raise SyncError(f"{link} is a directory, not the symlink this layout uses")
    tmp = root / f".{CURRENT}-{os.getpid()}-{time.monotonic_ns()}"
    os.symlink(Path(VERSIONS) / version.name, tmp)
    os.replace(tmp, link)


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[None]:
    """One sync at a time per copy."""
    import fcntl

    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".sync.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError(f"another sync of {root} is running") from None
        yield


def _prune(root: Path, keep: int) -> None:
    live = current(root)
    old = [v for v in versions(root) if v != live]
    for v in old[:max(0, len(old) - (keep - 1))]:
        shutil.rmtree(v, ignore_errors=True)


def sync(source: str, root: Path, *, manifest: str = "paperqa_manifest.csv", with_model: bool = False,
         with_hgnc: bool = False, ssh: list[str] | None = None, keep: int = KEEP,
         run: Callable[..., Any] = subprocess.run, clock: Callable[[], float] = time.time) -> SyncResult:
    """Copy the index at ``source`` (``[user@]access-hpc3.rcic.uci.edu:<corpus root>`` or a local
    directory) into a new version under ``root`` and make it current. See the module docstring."""
    if source.startswith("-") or Path(manifest).name != manifest:
        raise ValueError("source must be a directory and manifest must be a filename")
    host = source_host(source)
    if _LOGIN_HOST.fullmatch(host):
        raise ValueError(f"{host} is an HPC3 login node, where RCIC allows no data "
                         f"transfer: copy through {TRANSFER_HOST} instead")
    source = source.rstrip("/")
    with _locked(root):
        (root / VERSIONS).mkdir(parents=True, exist_ok=True)
        for leftover in (root / VERSIONS).glob(".incoming-*"):
            shutil.rmtree(leftover, ignore_errors=True)
        live = current(root)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
        name = stamp
        while (root / VERSIONS / name).exists():
            name = f"{stamp}-{time.monotonic_ns() % 10_000:04d}"
        incoming = root / VERSIONS / f".incoming-{name}"
        incoming.mkdir()
        try:
            if live is not None:
                _link_tree(live, incoming)
            pairs = [("mechanism_cache/", incoming / "mechanism_cache", True),
                     (manifest, incoming / "manifest.csv", False)]
            if with_hgnc:
                pairs.append(("hgnc_complete_set.txt", incoming / "hgnc_complete_set.txt", False))
            if with_model:      # outside the versions: the model changes rarely and is large
                pairs.append(("hf_cache/", root / "hf_cache", False))
            for name_, target, delete in pairs:
                if name_.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                run(rsync_command(f"{source}/{name_}", target, ssh=ssh, delete=delete), check=True)
            problem = check(incoming)
            if problem:
                raise SyncError(f"the copy from {source} cannot be searched: {problem}")
            new, old = _files(incoming), (_files(live) if live is not None else {})
            changed = sorted(k for k in new.keys() | old.keys()
                             if k not in new or k not in old or not os.path.samestat(new[k], old[k]))
            chunks, papers = _counts(incoming / "mechanism_cache")
            if live is not None and not changed:
                shutil.rmtree(incoming)
                return SyncResult(live, False, [], chunks, papers)
            (incoming / SOURCE_FILE).write_text(json.dumps({
                "source": source, "manifest": manifest,
                "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock())),
                "chunks": chunks, "papers": papers, "changed": changed,
                "files": {k: {"size": st.st_size, "mtime": int(st.st_mtime)} for k, st in sorted(new.items())},
            }, indent=1), encoding="utf-8")
            version = root / VERSIONS / name
            os.rename(incoming, version)
        except BaseException:
            shutil.rmtree(incoming, ignore_errors=True)
            raise
        switch(root, version)
        _prune(root, keep)
        return SyncResult(version, True, changed, chunks, papers)


def rollback(root: Path) -> Path:
    """Make the version before the current one current again."""
    with _locked(root):
        live = current(root)
        older = [v for v in versions(root) if live is None or v.name < live.name]
        if not older:
            raise SyncError(f"{root} has no version before {live.name if live else 'the current one'}")
        problem = check(older[-1])
        if problem:
            raise SyncError(f"{older[-1].name} cannot be searched: {problem}")
        switch(root, older[-1])
        return older[-1]


# ---------------------------------------------------------------------------------------------
# Rebuilding the index on HPC3
# ---------------------------------------------------------------------------------------------

REBUILD_MARKER = "AISCIENTIST_INDEX_BUILD "


def rebuild_inner_command(*, stage: str, archive: str, image: str, root: str, papers: str, manifest: str,
                          container_bin: str = "apptainer") -> str:
    """The job's command: unpack the staged code, then ``build_mechanism_cache.py --replace``
    inside ``paperqa.sif``, which builds beside the index and puts the result in place."""
    from .slurm_job import container_exec

    binds_ro = tuple(dict.fromkeys(p for p in (stage, papers, os.path.dirname(manifest))
                                   if p and not (p + "/").startswith(root.rstrip("/") + "/")))
    hf = f"{root.rstrip('/')}/hf_cache"
    inner = (f"env HF_HOME={shlex.quote(hf)} HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 "
             f"python3 {shlex.quote(stage + '/scripts/build_mechanism_cache.py')} --replace "
             f"--papers {shlex.quote(papers)} --manifest {shlex.quote(manifest)}")
    return (f"tar xzf {shlex.quote(archive)} -C {shlex.quote(stage)}\n"
            + container_exec(image, inner, binds_ro=binds_ro, binds_rw=(root,), container_bin=container_bin))


def parse_rebuild_log(text: str) -> dict[str, Any] | None:
    """The build's summary line (``build_mechanism_cache.py --replace`` prints it last)."""
    for line in reversed(text.splitlines()):
        if line.startswith(REBUILD_MARKER):
            with contextlib.suppress(ValueError):
                out = json.loads(line[len(REBUILD_MARKER):])
                return out if isinstance(out, dict) else None
    return None
