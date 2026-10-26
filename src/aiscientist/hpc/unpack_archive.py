#!/usr/bin/env python3
"""Unpack an uploaded archive on an HPC3 compute node, safely.

Run as a FILE, never imported by the platform at run time::

    python3 unpack_archive.py --archive A.zip --dest DIR [--max-bytes N] [--max-files N]
                              [--max-ratio R] [--keep-archive]

It is standard library only on purpose: the gateway syncs the source tree to dfs3b and a Slurm step
executes this file inside ``analysis.sif``, so nothing here may need the ``aiscientist`` package.

An uploaded archive is untrusted input, so:

* everything is unpacked into ``<dest>.unpacking`` and renamed to ``<dest>`` only when complete —
  ``<dest>`` existing therefore MEANS complete, which also makes a re-run after an interrupted job
  safe (it reports the existing result instead of unpacking again);
* bytes are counted as they are WRITTEN, never taken from the archive's own headers, against
  ``--max-bytes`` and against ``--max-ratio`` times the archive's size (a decompression bomb);
* the number of entries is capped (``--max-files``);
* an entry whose path is absolute or climbs out with ``..`` refuses the whole archive (that is not
  an accident); links and device files are skipped and counted, never created, so no entry can be
  written through a link;
* modes in the archive are ignored (no setuid, no unwritable directories).

Prints exactly one result line, ``AISCIENTIST_UNPACK_JSON {...}``, with ``status`` ``ok``, ``refused``
(the archive itself is the problem: retrying cannot help) or ``error``. Exit code 0 / 3 / 1.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tarfile
import zipfile

MARK = "AISCIENTIST_UNPACK_JSON"
CHUNK = 1024 * 1024
JUNK_DIRS = ("__MACOSX",)                       # resource forks a macOS zip carries
JUNK_FILES = (".DS_Store", "Thumbs.db")
RATIO_FLOOR = 64 * 1024 * 1024                  # do not judge a ratio before this much is written


class Refused(Exception):
    """The archive itself is unacceptable; unpacking it again would end the same way."""


def safe_parts(name: str) -> "list[str] | None":
    """The path components of an archive entry, or None for an entry with no usable name.
    Raises :class:`Refused` for an absolute path or one that leaves the destination."""
    name = name.replace("\\", "/")
    if "\x00" in name:
        raise Refused("an entry name contains a NUL byte")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise Refused(f"an entry has an absolute path: {name[:120]!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise Refused(f"an entry climbs out of the archive with '..': {name[:120]!r}")
    return parts or None


def is_junk(parts: "list[str]") -> bool:
    return parts[0] in JUNK_DIRS or parts[-1] in JUNK_FILES or parts[-1].startswith("._")


class Budget:
    """Running totals and the limits they are held to."""

    def __init__(self, archive_bytes: int, max_bytes: int, max_files: int, max_ratio: float) -> None:
        self.archive_bytes = max(1, archive_bytes)
        self.max_bytes, self.max_files, self.max_ratio = max_bytes, max_files, max_ratio
        self.bytes = 0
        self.files = 0
        self.skipped_links = 0
        self.skipped_junk = 0

    def add_file(self) -> None:
        self.files += 1
        if self.files > self.max_files:
            raise Refused(f"the archive holds more than {self.max_files:,} files")

    def add_bytes(self, n: int) -> None:
        self.bytes += n
        if self.bytes > self.max_bytes:
            raise Refused(f"the archive unpacks to more than {self.max_bytes / 1024 ** 3:.0f} GB")
        if self.bytes > RATIO_FLOOR and self.bytes / self.archive_bytes > self.max_ratio:
            raise Refused(f"the archive expands more than {self.max_ratio:.0f}x (a decompression bomb?)")


def target(root: str, parts: "list[str]") -> str:
    path = os.path.join(root, *parts)
    real_root = os.path.realpath(root)
    if os.path.commonpath([real_root, os.path.realpath(path)]) != real_root:   # belt and braces
        raise Refused(f"an entry resolves outside the destination: {'/'.join(parts)[:120]!r}")
    return path


def write_stream(src, path: str, budget: Budget) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as out:
        while True:
            block = src.read(CHUNK)
            if not block:
                break
            budget.add_bytes(len(block))
            out.write(block)


def unpack_zip(archive: str, root: str, budget: Budget) -> None:
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > budget.max_files * 2:          # cheap early exit; real counting is below
            raise Refused(f"the archive holds more than {budget.max_files:,} files")
        for info in infos:
            parts = safe_parts(info.filename)
            if parts is None:
                continue
            if is_junk(parts):
                budget.skipped_junk += 1
                continue
            # The Unix file TYPE, when the archive records one (many zips carry permission bits
            # only, or nothing): anything that is neither a file nor a folder is a link or a device.
            kind = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                budget.skipped_links += 1
                continue
            path = target(root, parts)
            if info.is_dir():
                os.makedirs(path, exist_ok=True)
                continue
            if info.flag_bits & 0x1:
                raise Refused("the archive is password-protected")
            budget.add_file()
            with zf.open(info) as src:
                write_stream(src, path, budget)


def unpack_tar(archive: str, root: str, budget: Budget) -> None:
    with tarfile.open(archive, "r:*") as tf:
        for member in tf:
            parts = safe_parts(member.name)
            if parts is None:
                continue
            if is_junk(parts):
                budget.skipped_junk += 1
                continue
            if member.isdir():
                os.makedirs(target(root, parts), exist_ok=True)
                continue
            if not member.isreg():                     # symlink, hard link, device, fifo
                budget.skipped_links += 1
                continue
            path = target(root, parts)
            budget.add_file()
            src = tf.extractfile(member)
            if src is None:
                continue
            with src:
                write_stream(src, path, budget)


def flatten_single_root(tmp: str) -> bool:
    """``data.zip`` usually holds one folder ``data/``: lift its content so the result is
    ``dest/<files>`` rather than ``dest/data/<files>``."""
    entries = os.listdir(tmp)
    if len(entries) != 1:
        return False
    inner = os.path.join(tmp, entries[0])
    if not os.path.isdir(inner) or os.path.islink(inner):
        return False
    holding = tmp + ".inner"
    os.rename(inner, holding)
    os.rmdir(tmp)
    os.rename(holding, tmp)
    return True


def measure(root: str) -> "tuple[int, int]":
    files = size = 0
    for base, _dirs, names in os.walk(root):
        for n in names:
            files += 1
            try:
                size += os.path.getsize(os.path.join(base, n))
            except OSError:
                pass
    return files, size


def run(args: argparse.Namespace) -> dict:
    archive, dest = os.path.abspath(args.archive), os.path.abspath(args.dest)
    tmp = dest + ".unpacking"
    if os.path.isdir(dest) and not os.path.exists(tmp):
        # A previous run finished the rename: report it instead of unpacking over a complete result.
        files, size = measure(dest)
        removed = _remove_archive(archive, args.keep_archive)
        return {"status": "ok", "path": dest, "files": files, "bytes": size, "resumed": True,
                "archive_removed": removed}
    if not os.path.isfile(archive):
        return {"status": "error", "reason": f"the archive is not there: {archive}"}
    shutil.rmtree(tmp, ignore_errors=True)             # what an interrupted run left behind
    shutil.rmtree(tmp + ".inner", ignore_errors=True)
    budget =Budget(os.path.getsize(archive), args.max_bytes, args.max_files, args.max_ratio)
    try:
        os.makedirs(tmp)
        if zipfile.is_zipfile(archive):
            unpack_zip(archive, tmp, budget)
        elif tarfile.is_tarfile(archive):
            unpack_tar(archive, tmp, budget)
        else:
            raise Refused("this is not a zip or tar archive")
        if budget.files == 0:
            raise Refused("the archive holds no files")
        flattened = flatten_single_root(tmp)
        os.rename(tmp, dest)
    except Refused as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"status": "refused", "reason": str(exc)}
    except (zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"status": "refused", "reason": f"the archive is damaged ({type(exc).__name__}: {exc})"}
    except OSError as exc:                             # disk full, quota, a name too long, …
        shutil.rmtree(tmp, ignore_errors=True)
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    return {"status": "ok", "path": dest, "files": budget.files, "bytes": budget.bytes,
            "skipped_links": budget.skipped_links, "skipped_junk": budget.skipped_junk,
            "flattened": flattened, "archive_removed": _remove_archive(archive, args.keep_archive)}


def _remove_archive(archive: str, keep: bool) -> bool:
    if keep:
        return False
    try:
        os.remove(archive)
        return True
    except OSError:
        return False


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--archive", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--max-bytes", type=int, default=500 * 1024 ** 3)
    ap.add_argument("--max-files", type=int, default=200_000)
    ap.add_argument("--max-ratio", type=float, default=200.0)
    ap.add_argument("--keep-archive", action="store_true")
    args = ap.parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:  # noqa: BLE001 - one result line whatever happens
        result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    print(f"{MARK} {json.dumps(result)}", flush=True)
    return {"ok": 0, "refused": 3}.get(result["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
