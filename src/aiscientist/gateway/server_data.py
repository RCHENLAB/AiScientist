"""Datasets that already live on the gateway host, bound by path instead of uploaded.

A lab's shared data sits on the server (``/data/Users/shared/...`` on the eyeserver). Uploading it
through the browser would copy it out of the server and back in. Instead a user binds the PATH:

1. the path must resolve (symlinks followed) inside one of the folders listed in
   ``AISCIENTIST_SERVER_DATA_ROOTS``: the allowlist is checked here, in code, never by a model;
2. :func:`scan` lists it once (metadata only) and decides what an analysis needs from it;
3. when analysis runs on HPC3, whose nodes cannot see the server's disks, :func:`sync_to_hpc`
   copies those files to the user's dfs3b uploads area at run start. The copy is incremental (a file
   already there at the same size is skipped, so a second run copies nothing) and resumable (a large
   file is written as ``.part`` and continued where an interrupted copy stopped).

Nothing here imports the rest of the gateway: the executor is passed in.
"""

from __future__ import annotations

import difflib
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

ROOTS_ENV = "AISCIENTIST_SERVER_DATA_ROOTS"
CONFIRM_GB_ENV = "AISCIENTIST_SERVER_DATA_CONFIRM_GB"
MAX_GB_ENV = "AISCIENTIST_SERVER_DATA_MAX_GB"
DEFAULT_CONFIRM_GB = 10.0
DEFAULT_MAX_GB = 500.0       # dfs3b is shared and near its quota: a whole-share bind is a mistake
MAX_SCAN_FILES = 50_000      # more than this is a share, not a dataset: bind a subfolder
_GB = 1024 ** 3

# Never copied: a viewer format no tool reads, and desktop litter.
_SKIP_SUFFIXES = (".cloupe",)
_SKIP_NAMES = {".DS_Store", "Thumbs.db"}
# Not copied from a Cell Ranger delivery: per-molecule records only `cellranger aggr` reads.
_SKIP_IN_CELLRANGER = {"molecule_info.h5"}


class ServerPathError(ValueError):
    """A path that cannot be bound, with a message the user can act on."""


def _configured(env: "dict[str, str] | None" = None) -> "list[tuple[str, Path]]":
    """``(as written, resolved)`` for each allowlisted folder that exists. Separated by ``:`` or
    ``,``; a missing folder or ``/`` itself is dropped."""
    raw = (env if env is not None else os.environ).get(ROOTS_ENV, "")
    out: list[tuple[str, Path]] = []
    for part in re.split(r"[:,]", raw):
        part = os.path.normpath(part.strip()) if part.strip() else ""
        if not part.startswith("/"):
            continue
        try:
            root = Path(part).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if root.is_dir() and root != Path("/") and all(root != r for _, r in out):
            out.append((part, root))
    return out


def server_data_roots(env: "dict[str, str] | None" = None) -> "tuple[Path, ...]":
    """The allowlisted folders, resolved. Empty (the default) means binding server paths is off."""
    return tuple(root for _, root in _configured(env))


def display_roots(env: "dict[str, str] | None" = None) -> "list[str]":
    """The allowlisted folders as a user would type them: as configured, plus the resolved form
    when a symlink makes it different."""
    shown: list[str] = []
    for written, root in _configured(env):
        for form in (written, str(root)):
            if form not in shown:
                shown.append(form)
    return shown


def _gb_env(name: str, default: float, env: "dict[str, str] | None" = None) -> float:
    try:
        return float((env if env is not None else os.environ).get(name, "") or default)
    except ValueError:
        return default


def confirm_bytes(env: "dict[str, str] | None" = None) -> int:
    """Above this many bytes to copy, the console asks before binding."""
    return int(_gb_env(CONFIRM_GB_ENV, DEFAULT_CONFIRM_GB, env) * _GB)


def max_bytes(env: "dict[str, str] | None" = None) -> int:
    """Above this many bytes to copy, a bind is refused."""
    return int(_gb_env(MAX_GB_ENV, DEFAULT_MAX_GB, env) * _GB)


def root_of(path: "str | Path", roots: Iterable[Path]) -> "Path | None":
    """The allowlisted root that contains ``path`` once resolved, or None. Never raises."""
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    for root in roots:
        if resolved == root or resolved.is_relative_to(root):
            return root
    return None


def _did_you_mean(lexical: str, roots: "tuple[Path, ...]") -> str:
    """A hint for a mistyped path (``Samplel_WT`` for ``Sample1_WT``): the names closest to the
    first missing part, in the deepest folder that exists. Only a folder that resolves inside the
    allowlist is listed, so the hint reveals nothing a bind there could not."""
    parts = Path(lexical).parts
    for depth in range(len(parts) - 1, 0, -1):
        parent = Path(*parts[:depth])
        try:
            if not parent.is_dir() or root_of(parent, roots) is None:
                continue
            names = sorted(os.listdir(parent))
        except OSError:
            return ""
        close = difflib.get_close_matches(parts[depth], names, n=3, cutoff=0.6)
        if not close:
            return ""
        return " Did you mean " + " or ".join(str(parent / n) for n in close) + "?"
    return ""


def resolve(raw: str, roots: "tuple[Path, ...]") -> "tuple[Path, Path]":
    """``(resolved path, its root)`` for a path a user typed, or :class:`ServerPathError`."""
    text = (raw or "").strip().strip("'\"").rstrip("/") or "/"
    if not roots:
        raise ServerPathError("Binding server folders is not enabled on this deployment "
                              f"({ROOTS_ENV} is empty).")
    if not text.startswith("/"):
        raise ServerPathError("Give an absolute path, starting with /.")
    allowed = ", ".join(str(r) for r in roots)
    # Refuse a path outside the allowlist BEFORE touching the filesystem, so the answer never
    # tells anyone whether a path elsewhere on the server exists.
    lexical = os.path.normpath(text)
    prefixes = {str(r) for r in roots} | {w for w, _ in _configured()}
    if not any(lexical == pre or lexical.startswith(pre + "/") for pre in prefixes):
        raise ServerPathError(f"{text} is outside the folders that can be bound ({allowed}).")
    try:
        resolved = Path(text).resolve(strict=True)
    except (OSError, RuntimeError):
        # Copied from wrapped text, a path arrives split at a slash ("…/jinl14/ CellQC_testdata/…").
        joined = re.sub(r"\s*/\s*", "/", text)
        if joined != text:
            return resolve(joined, roots)
        raise ServerPathError(f"{text} does not exist on the server."
                              + _did_you_mean(lexical, roots)) from None
    root = root_of(resolved, roots)
    if root is None:                               # a link inside the allowlist that leads out
        raise ServerPathError(f"{text} is outside the folders that can be bound ({allowed}).")
    if not os.access(resolved, os.R_OK):
        raise ServerPathError(f"{text} is not readable by the server.")
    return resolved, root


def find_paths(text: str, roots: "tuple[Path, ...]") -> list[str]:
    """Absolute paths in free text that start inside an allowlisted root (not yet resolved: the
    console offers each one to bind, and binding resolves it). Order kept, duplicates dropped."""
    found: list[str] = []
    for m in re.finditer(r"(?<![\w/])(/[^\s'\"`<>|;,()\[\]{}]+)", text or ""):
        candidate = m.group(1).rstrip(".:")
        if any(candidate == str(r) or candidate.startswith(str(r) + "/") for r in roots):
            if candidate not in found:
                found.append(candidate)
    return found


@dataclass
class Scan:
    path: Path
    root: Path
    is_dir: bool
    files: list[tuple[str, int]] = field(default_factory=list)      # (rel, size), everything
    copy: list[tuple[str, int]] = field(default_factory=list)       # what an analysis needs
    skipped: list[tuple[str, int, str]] = field(default_factory=list)
    cellranger: "dict[str, Any] | None" = None

    @property
    def size_bytes(self) -> int:
        return sum(s for _, s in self.files)

    @property
    def copy_bytes(self) -> int:
        return sum(s for _, s in self.copy)

    def summary(self) -> dict:
        skipped_by: dict[str, dict] = {}
        for rel, size, why in self.skipped:
            row = skipped_by.setdefault(why, {"reason": why, "files": 0, "bytes": 0, "examples": []})
            row["files"] += 1
            row["bytes"] += size
            if len(row["examples"]) < 3:
                row["examples"].append(rel)
        cr = self.cellranger
        return {
            "path": str(self.path), "root": str(self.root), "name": self.path.name,
            "kind": "folder" if self.is_dir else "file",
            "n_files": len(self.files), "size_bytes": self.size_bytes,
            "copy_files": len(self.copy), "copy_bytes": self.copy_bytes,
            "skipped": list(skipped_by.values()),
            "cellranger": ({"n_libraries": cr["n_libraries"],
                            "samples": [lib["sample"] for lib in cr["libraries"]],
                            "all_have_bam": cr["all_have_bam"]} if cr else None),
        }


def _walk(path: Path, roots: "tuple[Path, ...]") -> "tuple[list[tuple[str, int]], list[tuple[str, int, str]], list[str]]":
    """Every file under ``path`` as (rel, size), files whose link leaves the allowlist, and the
    directories (for the Cell Ranger layout, which also recognises unpacked matrix folders)."""
    files: list[tuple[str, int]] = []
    outside: list[tuple[str, int, str]] = []
    dirs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        rel_dir = os.path.relpath(dirpath, path)
        dirnames.sort()
        for d in dirnames:
            dirs.append(d if rel_dir == "." else f"{rel_dir}/{d}")
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            try:
                size = os.stat(full).st_size
            except OSError:
                continue                                  # a dangling link
            if os.path.islink(full) and root_of(full, roots) is None:
                outside.append((rel, size, "a link to outside the allowed folders"))
                continue
            files.append((rel, size))
            if len(files) > MAX_SCAN_FILES:
                raise ServerPathError(
                    f"{path} holds more than {MAX_SCAN_FILES:,} files: that is a whole share, not "
                    "a dataset. Bind the dataset's own folder.")
    return files, outside, dirs


def _pipestance_internal(rel: str, parent: str, outs: str) -> bool:
    """Cell Ranger's own bookkeeping beside ``outs/``: its working tree (``SC_RNA_COUNTER_CS/``),
    the ``_log``/``_perf``/... files and the ``.mri.tgz`` archive. A user's own file there is kept."""
    if not rel.startswith(parent) or rel.startswith(outs):
        return False
    rest = rel[len(parent):]
    return "/" in rest or rest.startswith("_") or rest.endswith(".mri.tgz")


def scan(path: Path, root: Path, roots: "tuple[Path, ...]",
         layout_fn: "Callable[[list[str], str], dict | None] | None" = None) -> Scan:
    """List ``path`` and decide what to copy. ``layout_fn`` is
    ``tools.api.describe_cellranger_layout`` (passed in so this module needs no tools import)."""
    if not path.is_dir():
        size = path.stat().st_size
        return Scan(path=path, root=root, is_dir=False, files=[(path.name, size)],
                    copy=[(path.name, size)])
    files, outside, dirs = _walk(path, roots)
    layout = None
    if layout_fn is not None:
        try:
            layout = layout_fn([rel for rel, _ in files] + dirs, str(path))
        except Exception:  # noqa: BLE001 - recognising the layout is a hint, never a blocker
            layout = None
    # A pipestance is <sample>/outs/ plus Cell Ranger's own working tree beside it (tens of GB of
    # intermediates): only outs/ is the delivery.
    pipestance: list[tuple[str, str]] = []
    for lib in (layout or {}).get("libraries", []):
        rel = lib.get("rel") or ""
        if rel == "outs" or rel.endswith("/outs"):
            pipestance.append((rel[: -len("outs")], rel + "/"))
    out = Scan(path=path, root=root, is_dir=True, files=files + [(r, s) for r, s, _ in outside],
               skipped=list(outside), cellranger=layout)
    for rel, size in files:
        name = rel.rsplit("/", 1)[-1]
        if name in _SKIP_NAMES or name.startswith("._"):
            out.skipped.append((rel, size, "desktop metadata"))
        elif name.lower().endswith(_SKIP_SUFFIXES):
            out.skipped.append((rel, size, "Loupe Browser file (no tool reads it)"))
        elif layout and name in _SKIP_IN_CELLRANGER:
            out.skipped.append((rel, size, "Cell Ranger per-molecule file (only cellranger aggr "
                                           "reads it)"))
        elif any(_pipestance_internal(rel, parent, outs) for parent, outs in pipestance):
            out.skipped.append((rel, size, "Cell Ranger working files outside outs/"))
        else:
            out.copy.append((rel, size))
    return out


def mirror_dir(uploads_dir: str, root: Path, path: Path) -> str:
    """Where ``path`` is mirrored on HPC3: ``<uploads>/server-data/<root as one name>/<rel>``. The
    same server path always maps to the same place, which is what makes the copy incremental."""
    tag = "_".join(p for p in root.parts if p not in ("/", "")) or "root"
    rel = path.relative_to(root).as_posix()
    base = f"{uploads_dir.rstrip('/')}/server-data/{tag}"
    return base if rel in ("", ".") else f"{base}/{rel}"


class TransferCancelled(RuntimeError):
    """The run was stopped during the copy; the partial file stays and the next run resumes it."""


def _remote_sizes(executor: Any, remote_dir: str) -> "dict[str, int]":
    """Files already in ``remote_dir`` as {rel: size} — one metadata-only ``find`` (control plane,
    allowed on a login node; the bytes themselves move over the transfer host)."""
    res = executor.exec(f"find {shlex.quote(remote_dir)} -type f -printf '%s\\t%P\\n' 2>/dev/null")
    sizes: dict[str, int] = {}
    for line in (getattr(res, "stdout", "") or "").splitlines():
        try:
            size_s, rel = line.split("\t", 1)
            sizes[rel] = int(size_s)
        except ValueError:
            continue
    return sizes


def sync_to_hpc(executor: Any, scan_result: Scan, remote: str,
                progress: "Callable[[str], None] | None" = None,
                should_cancel: "Callable[[], bool] | None" = None) -> dict:
    """Copy ``scan_result.copy`` to ``remote`` (a directory for a folder, the file path for a file).

    Returns ``{"remote", "copied", "copied_bytes", "kept", "kept_bytes", "seconds"}``. Raises
    :class:`TransferCancelled` when ``should_cancel`` turns true, and the executor's error on a
    failed transfer."""
    say = progress or (lambda _msg: None)
    start = time.monotonic()
    if scan_result.is_dir:
        have = _remote_sizes(executor, remote)
        targets = [(rel, size, f"{remote}/{rel}") for rel, size in scan_result.copy]
    else:
        rel, size = scan_result.copy[0]
        have = {rel: executor.remote_size(remote)} if hasattr(executor, "remote_size") else {}
        targets = [(rel, size, remote)]
    todo = [(rel, size, dst) for rel, size, dst in targets if have.get(rel) != size]
    kept = len(targets) - len(todo)
    kept_bytes = sum(size for rel, size, _ in targets if have.get(rel) == size)
    total = sum(size for _, size, _ in todo)
    if todo:
        say(f"⤴ Copying {len(todo)} file(s), {total / _GB:.1f} GB, from the server to your HPC3 "
            f"storage" + (f" ({kept} already there)" if kept else "") + "…")
    put_resumable = getattr(executor, "put_file_resumable", None)
    if put_resumable is not None and todo:
        # Every parent directory in one command, instead of one mkdir per file.
        parents = sorted({dst.rsplit("/", 1)[0] for _, _, dst in todo})
        for i in range(0, len(parents), 200):
            executor.exec("mkdir -p " + " ".join(shlex.quote(d) for d in parents[i:i + 200]))
    done_bytes = 0
    for i, (rel, size, dst) in enumerate(todo, 1):
        if should_cancel and should_cancel():
            raise TransferCancelled(f"stopped before {rel}")
        src = str(scan_result.path / rel) if scan_result.is_dir else str(scan_result.path)
        if size >= _GB:
            say(f"⤴ [{i}/{len(todo)}] {rel} ({size / _GB:.1f} GB)…")
        if put_resumable is not None:
            last = [time.monotonic()]

            def _tick(sent: int, of: int, _rel: str = rel) -> None:
                if should_cancel and should_cancel():
                    raise TransferCancelled(f"stopped during {_rel}")
                now = time.monotonic()
                if of >= _GB and now - last[0] >= 60:
                    last[0] = now
                    say(f"⤴ {_rel}: {sent / _GB:.1f} of {of / _GB:.1f} GB")

            put_resumable(src, dst, progress=_tick, make_parent=False)
        else:
            executor.put_file(src, dst)
        done_bytes += size
    return {"remote": remote, "copied": len(todo), "copied_bytes": done_bytes, "kept": kept,
            "kept_bytes": kept_bytes, "seconds": round(time.monotonic() - start, 1)}
