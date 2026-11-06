"""Shared datasets: a dataset the lab maintains for every account (RCHENLAB/AiScientist issue 92).

A shared dataset has no owner. Its maintainers publish it in versions, and a published version
never changes: every account reads the same bytes, and a run can name exactly what it read.

The store (``AISCIENTIST_SHARED_DATASETS_ROOT`` on the gateway host; unset = the feature is off)::

    <root>/<slug>/
    ├── latest -> v2026.11            moved only after a version is complete
    ├── v2026.11/                     files 0444, folders 0555
    │   ├── <the published files>
    │   ├── manifest.json             name, size, sha256 and facets of every file
    │   ├── SHA256SUMS                the same checksums, for ``sha256sum -c``
    │   └── CHANGELOG.md
    └── v2026.12/                     a file unchanged since the previous version is a hard link to it

HPC3 holds one copy of each version under ``<AISCIENTIST_HPC_SHARED_ROOT>/datasets/<slug>/<label>/``
(``hpc_layout.datasets_dir``), made by ``deploy/hpc3/sync_shared_dataset.sh`` and recorded here with
:func:`mark_hpc_verified` once its checksum job passes. Runs read these copies in place; nothing is
copied per account or per session, nothing counts toward an account's storage, nothing is removed
by age.

Publishing reads a folder the maintainers prepared inside an allowlisted server folder
(``AISCIENTIST_SERVER_DATA_ROOTS``), hashes each file while copying it, refuses a file that changes
meanwhile, and only then makes the version visible. A failed publish leaves the version in
``publishing``; publishing the same label again starts it over. A label is never reused.

Nothing here returns a server path to an account: :func:`dataset_views` and :func:`version_view`
name files relative to their version.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

ROOT_ENV = "AISCIENTIST_SHARED_DATASETS_ROOT"
MANIFEST = "manifest.json"
CHECKSUMS = "SHA256SUMS"
CHANGELOG = "CHANGELOG.md"
FACETS_FILE = "facets.json"          # optional, in the source folder: per-file facets the maintainers set
LATEST = "latest"
STATUSES = ("publishing", "published", "withdrawn")

_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
# scRNA, snRNA, scATAC, snMultiome, scRNAseq ...: the modality token of names like Iris_snRNA_allcells.h5ad
_MODALITY_RE = re.compile(r"(?i)^(sc|sn)(rna|atac|multiome|cite)(-?seq)?$")
_CHUNK = 8 * 1024 * 1024
_RESERVED = {MANIFEST, CHECKSUMS, CHANGELOG, FACETS_FILE}


class SharedDatasetError(ValueError):
    """A request that cannot be done, with a message the operator or user can act on."""


# --- configuration and names ---------------------------------------------------------------------


def store_root(env: "dict[str, str] | None" = None) -> "Path | None":
    """The store's folder, or None when the feature is off."""
    raw = ((env if env is not None else os.environ).get(ROOT_ENV) or "").strip()
    return Path(raw) if raw else None


def _root(store: "Path | str | None") -> Path:
    root = Path(store) if store else store_root()
    if root is None:
        raise SharedDatasetError(f"Shared datasets are off: set {ROOT_ENV} to the store's folder.")
    return root


def check_slug(slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise SharedDatasetError(f"Invalid dataset name {slug!r}: use 1-40 lower-case letters, digits "
                                 "and hyphens, starting and ending with a letter or digit (e.g. heca).")
    return slug


def check_label(label: str) -> str:
    if (not isinstance(label, str) or not _LABEL_RE.fullmatch(label) or label == LATEST
            or ".." in label):
        raise SharedDatasetError(f"Invalid version {label!r}: use 1-40 letters, digits, '.', '_' or '-', "
                                 f"starting with a letter or digit, not {LATEST!r} (e.g. v2026.11).")
    return label


def version_dir(slug: str, label: str, store: "Path | str | None" = None) -> Path:
    """Where a version's files are on the gateway host. For the gateway's own use only."""
    return _root(store) / check_slug(slug) / check_label(label)


def hpc_version_dir(shared_root: str, slug: str, label: str) -> str:
    """Where a version's HPC3 copy is: ``<shared_root>/datasets/<slug>/<label>``."""
    from . import hpc_layout
    return f"{hpc_layout.datasets_dir(shared_root)}/{check_slug(slug)}/{check_label(label)}"


def infer_facets(name: str) -> dict:
    """Tissue and modality from a name such as ``Iris_snRNA_allcells.h5ad``: the token that names a
    modality, and everything before it. Empty when the name has no such token."""
    tokens = PurePosixPath(name).name.split(".")[0].split("_")
    for i, token in enumerate(tokens):
        if i and _MODALITY_RE.match(token):
            return {"tissue": "_".join(tokens[:i]), "modality": token}
    return {}


def _h5ad_facets(path: Path) -> dict:
    """n_obs, n_vars, the X dtype and the obs column names of an .h5ad, read from its HDF5 tree
    only (the matrix is never loaded). Empty when the file cannot be read that way."""
    try:
        from ..metadata.facts import compact_peek
        from ..tools.api import peek_dataset
        info = (compact_peek(peek_dataset(str(path))) or {}).get("hdf5") or {}
    except Exception:  # noqa: BLE001 - facets are a description, never a reason to refuse
        return {}
    out: dict[str, Any] = {}
    for key in ("n_obs", "n_vars", "x_dtype"):
        if info.get(key) not in (None, ""):
            out[key] = info[key]
    obs = [c for c in info.get("obs") or [] if not str(c).startswith("_")]
    if obs:
        out["obs_columns"] = obs
    return out


# --- files ---------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_hashing(src: Path, dest: Path) -> str:
    """Copy ``src`` to ``dest`` and return the SHA-256 of what was written, in one read."""
    digest = hashlib.sha256()
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        for chunk in iter(lambda: fin.read(_CHUNK), b""):
            digest.update(chunk)
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    return digest.hexdigest()


def _scan(source: Path) -> list[tuple[str, Path, int, int]]:
    """``(relative name, path, size, mtime_ns)`` of every file to publish, sorted by name. Hidden
    files and the maintainers' ``facets.json`` are left out; a symbolic link is refused, so nothing
    outside the folder can be published by accident."""
    found: list[tuple[str, Path, int, int]] = []
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        here = Path(dirpath)
        for d in list(dirnames):
            if d.startswith("."):
                dirnames.remove(d)
            elif (here / d).is_symlink():
                raise SharedDatasetError(f"{(here / d).relative_to(source)} is a symbolic link: copy the "
                                         "real folder into the source folder instead.")
        for f in filenames:
            path = here / f
            rel = path.relative_to(source).as_posix()
            if f.startswith(".") or rel in _RESERVED:
                continue
            if path.is_symlink():
                raise SharedDatasetError(f"{rel} is a symbolic link: copy the real file into the source "
                                         "folder instead.")
            st = path.stat()
            if not stat.S_ISREG(st.st_mode):
                continue
            found.append((rel, path, st.st_size, st.st_mtime_ns))
    return sorted(found)


def _make_read_only(folder: Path) -> None:
    """Files 0444, then folders 0555, deepest first."""
    dirs: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(folder):
        dirs.append(Path(dirpath))
        for f in filenames:
            os.chmod(Path(dirpath) / f, 0o444)
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        os.chmod(d, 0o555)


def _remove_partial(folder: Path) -> None:
    """Remove a partial version folder of ours, read-only parts included."""
    if not folder.exists():
        return
    for dirpath, _dirnames, _filenames in os.walk(folder):
        os.chmod(dirpath, 0o755)
    shutil.rmtree(folder)


def _point_latest(dataset_dir: Path, label: "str | None") -> None:
    """Point ``latest`` at ``label`` atomically (a relative link), or remove it when None."""
    link = dataset_dir / LATEST
    if label is None:
        if link.is_symlink():
            link.unlink()
        return
    tmp = dataset_dir / f".{LATEST}.{os.getpid()}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(label, tmp)
    os.replace(tmp, link)


# --- database helpers ----------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: "dt.datetime | None") -> "str | None":
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.isoformat()


def _dataset(session, slug: str):
    from .models import SharedDataset
    row = session.query(SharedDataset).filter_by(slug=check_slug(slug)).one_or_none()
    if row is None:
        raise SharedDatasetError(f"No shared dataset {slug!r}. An admin creates it first: "
                                 f"aiscientist-admin shared-dataset create {slug} --name '...'.")
    return row


def _user(session, username: str):
    from .models import User
    user = session.query(User).filter_by(username=username).one_or_none()
    if user is None or not user.is_active:
        raise SharedDatasetError(f"No active account {username!r}.")
    return user


def _is_maintainer(session, dataset_id: int, user_id: int) -> bool:
    from .models import SharedDatasetMaintainer
    return session.get(SharedDatasetMaintainer, (dataset_id, user_id)) is not None


def _published(session, dataset_id: int) -> list:
    """The dataset's published versions, newest first."""
    from .models import SharedDatasetVersion
    rows = (session.query(SharedDatasetVersion)
            .filter_by(dataset_id=dataset_id, status="published").all())
    return sorted(rows, key=lambda v: (_iso(v.published_at) or "", v.id), reverse=True)


def can_publish(user: Any, slug: str) -> bool:
    """An admin, or a maintainer of this dataset."""
    if user is None:
        return False
    if getattr(user, "role", "") == "admin":
        return True
    from .db import session_scope
    with session_scope() as s:
        try:
            ds = _dataset(s, slug)
        except SharedDatasetError:
            return False
        return _is_maintainer(s, ds.id, user.id)


# --- admin: datasets and maintainers -------------------------------------------------------------


def create_dataset(slug: str, name: str, *, description: str = "", citation: str = "") -> dict:
    from .db import session_scope
    from .models import SharedDataset
    check_slug(slug)
    if not (name or "").strip():
        raise SharedDatasetError("A shared dataset needs a name, e.g. 'Human Eye Cell Atlas (HECA)'.")
    with session_scope() as s:
        if s.query(SharedDataset).filter_by(slug=slug).one_or_none() is not None:
            raise SharedDatasetError(f"Shared dataset {slug!r} already exists.")
        row = SharedDataset(slug=slug, name=name.strip(), description=description.strip(),
                            citation=citation.strip())
        s.add(row)
        s.commit()
        return {"slug": row.slug, "uuid": row.uuid, "name": row.name}


def update_dataset(slug: str, *, name: "str | None" = None, description: "str | None" = None,
                   citation: "str | None" = None) -> dict:
    """Edit the dataset's own description; a version's content never changes."""
    from .db import session_scope
    with session_scope() as s:
        row = _dataset(s, slug)
        if name is not None and name.strip():
            row.name = name.strip()
        if description is not None:
            row.description = description.strip()
        if citation is not None:
            row.citation = citation.strip()
        s.commit()
        return {"slug": row.slug, "name": row.name}


def set_maintainer(slug: str, username: str, *, add: bool = True) -> list[str]:
    """Add or remove a maintainer; returns the maintainers' usernames."""
    from .db import session_scope
    from .models import SharedDatasetMaintainer
    with session_scope() as s:
        ds = _dataset(s, slug)
        user = _user(s, username)
        row = s.get(SharedDatasetMaintainer, (ds.id, user.id))
        if add and row is None:
            s.add(SharedDatasetMaintainer(dataset_id=ds.id, user_id=user.id))
        elif not add and row is not None:
            s.delete(row)
        s.commit()
    return maintainers(slug)


def maintainers(slug: str) -> list[str]:
    from .db import session_scope
    from .models import SharedDatasetMaintainer, User
    with session_scope() as s:
        ds = _dataset(s, slug)
        rows = (s.query(User.username).join(SharedDatasetMaintainer, SharedDatasetMaintainer.user_id == User.id)
                .filter(SharedDatasetMaintainer.dataset_id == ds.id).all())
        return sorted(r[0] for r in rows)


# --- publishing ----------------------------------------------------------------------------------


def _source_folder(source: "str | Path", roots: "Iterable[Path] | None") -> Path:
    """The prepared folder, which must sit inside an allowlisted server folder."""
    from . import server_data as sd
    allowed = tuple(roots) if roots is not None else sd.server_data_roots()
    if not allowed:
        raise SharedDatasetError(f"No server folder is allowlisted ({sd.ROOTS_ENV}): the source of a "
                                 "shared dataset must sit inside one.")
    try:
        path, _root_found = sd.resolve(str(source), allowed)
    except sd.ServerPathError as exc:
        raise SharedDatasetError(str(exc)) from exc
    if not path.is_dir():
        raise SharedDatasetError(f"{source} is not a folder: put the version's files in one folder.")
    return path


def _load_facets(source: Path, extra: "dict | None") -> dict:
    facets: dict = {}
    f = source / FACETS_FILE
    if f.is_file():
        try:
            loaded = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SharedDatasetError(f"{FACETS_FILE} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise SharedDatasetError(f"{FACETS_FILE} must map file names to objects.")
        facets.update(loaded)
    for name, value in (extra or {}).items():
        facets[name] = {**(facets.get(name) or {}), **(value or {})}
    return facets


def _manifest_bytes(manifest: dict) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def publish(slug: str, label: str, source: "str | Path", *, by: str, changelog: str,
            facets: "dict | None" = None, store: "Path | str | None" = None,
            roots: "Iterable[Path] | None" = None,
            log: "Callable[[str], None] | None" = None) -> dict:
    """Publish ``source`` as version ``label`` of ``slug``, for account ``by`` (an admin or one of
    the dataset's maintainers). Returns the version's summary. See the module docstring."""
    from .db import session_scope
    from .models import SharedDatasetFile, SharedDatasetVersion
    say = log or (lambda _msg: None)
    check_slug(slug)
    check_label(label)
    if not (changelog or "").strip():
        raise SharedDatasetError("A version needs a changelog: what changed (files, annotation "
                                 "columns, cells) and why.")
    root = _root(store)
    src = _source_folder(source, roots)
    files = _scan(src)
    if not files:
        raise SharedDatasetError(f"{source} has no files to publish.")
    wanted_facets = _load_facets(src, facets)
    unknown = sorted(set(wanted_facets) - {rel for rel, *_ in files})
    if unknown:
        raise SharedDatasetError(f"Facets name files that are not in the folder: {', '.join(unknown)}.")

    dataset_dir = root / slug
    final = dataset_dir / label
    with session_scope() as s:
        ds = _dataset(s, slug)
        user = _user(s, by)
        if user.role != "admin" and not _is_maintainer(s, ds.id, user.id):
            raise SharedDatasetError(f"{by} is not a maintainer of {slug}: an admin adds maintainers "
                                     f"with aiscientist-admin shared-dataset maintainer {slug} add <account>.")
        row = s.query(SharedDatasetVersion).filter_by(dataset_id=ds.id, label=label).one_or_none()
        if row is not None and row.status != "publishing":
            raise SharedDatasetError(f"{slug} {label} is already {row.status}: a version is never "
                                     "replaced. Publish the change under a new label.")
        if row is None:
            row = SharedDatasetVersion(dataset_id=ds.id, label=label, status="publishing", changelog="",
                                       size_bytes=0, n_files=0)
            s.add(row)
            s.commit()
        version_id, version_uuid, dataset_uuid, dataset_name = row.id, row.uuid, ds.uuid, ds.name
        user_id = user.id
        previous = next((v for v in _published(s, ds.id) if v.id != version_id), None)
        prev_label = previous.label if previous else None
        prev_files = ({f.name: (f.size_bytes, f.sha256) for f in
                       s.query(SharedDatasetFile).filter_by(version_id=previous.id)} if previous else {})

    if final.exists():
        raise SharedDatasetError(f"{slug} {label} already has a folder in the store but is not published. "
                                 "Check it by hand, then remove it or publish under a new label.")
    stage = dataset_dir / f".{label}.partial"
    _remove_partial(stage)
    stage.mkdir(parents=True)
    os.chmod(dataset_dir, 0o755)

    entries: list[dict] = []
    linked = 0
    for rel, path, size, mtime in files:
        dest = stage / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        prev = prev_files.get(rel)
        sha = None
        if prev and prev[0] == size:
            sha = _sha256(path)
            if sha == prev[1]:
                try:
                    os.link(dataset_dir / str(prev_label) / rel, dest)
                    linked += 1
                    say(f"  {rel}: unchanged since {prev_label}, linked")
                except OSError:
                    sha = None          # another file system: copy it below
            else:
                sha = None
        if not dest.exists():
            sha = _copy_hashing(path, dest)
            say(f"  {rel}: {size / 1e9:.2f} GB copied")
        st = path.stat()
        if (st.st_size, st.st_mtime_ns) != (size, mtime):
            _remove_partial(stage)
            raise SharedDatasetError(f"{rel} changed while it was being published. Publish again once "
                                     "the source folder is final.")
        entry: dict[str, Any] = {"name": rel, "size_bytes": size, "sha256": sha}
        file_facets = {**infer_facets(rel)}
        if rel.lower().endswith(".h5ad"):
            file_facets.update(_h5ad_facets(dest))
        file_facets.update(wanted_facets.get(rel) or {})
        if file_facets:
            entry["facets"] = file_facets
        entries.append(entry)

    published_at = _now()
    manifest = {
        "dataset": slug, "name": dataset_name, "dataset_uuid": dataset_uuid,
        "version": label, "version_uuid": version_uuid, "previous_version": prev_label,
        "published_by": by, "published_at": _iso(published_at),
        "size_bytes": sum(e["size_bytes"] for e in entries), "n_files": len(entries),
        "files": entries,
    }
    data = _manifest_bytes(manifest)
    (stage / MANIFEST).write_bytes(data)
    (stage / CHECKSUMS).write_text("".join(f"{e['sha256']}  {e['name']}\n" for e in entries),
                                   encoding="utf-8")
    (stage / CHANGELOG).write_text(changelog.strip() + "\n", encoding="utf-8")
    manifest_sha = hashlib.sha256(data).hexdigest()
    for dirpath, _dirnames, filenames in os.walk(stage):
        for f in filenames:
            os.chmod(Path(dirpath) / f, 0o444)
    os.rename(stage, final)
    _make_read_only(final)

    with session_scope() as s:
        row = s.get(SharedDatasetVersion, version_id)
        row.status = "published"
        row.manifest_sha256 = manifest_sha
        row.changelog = changelog.strip()
        row.size_bytes = manifest["size_bytes"]
        row.n_files = len(entries)
        row.published_by = user_id
        row.published_at = published_at
        for e in entries:
            s.add(SharedDatasetFile(version_id=version_id, name=e["name"], size_bytes=e["size_bytes"],
                                    sha256=e["sha256"], facets=json.dumps(e.get("facets") or {},
                                                                          sort_keys=True)))
        s.commit()
        newest = _published(s, _dataset(s, slug).id)
        latest = newest[0].label if newest else None
    _point_latest(dataset_dir, latest)
    say(f"published {slug} {label}: {len(entries)} files, {manifest['size_bytes'] / 1e9:.2f} GB "
        f"({linked} linked from {prev_label})" if linked else
        f"published {slug} {label}: {len(entries)} files, {manifest['size_bytes'] / 1e9:.2f} GB")
    return {"slug": slug, "label": label, "uuid": version_uuid, "n_files": len(entries),
            "size_bytes": manifest["size_bytes"], "manifest_sha256": manifest_sha, "linked": linked,
            "previous_version": prev_label, "latest": latest}


def withdraw(slug: str, label: str, *, by: str, store: "Path | str | None" = None) -> dict:
    """Stop offering a version. Its files stay, so chats pinned to it and earlier runs still read
    it; ``latest`` moves to the newest version still published (or goes when there is none)."""
    from .db import session_scope
    from .models import SharedDatasetVersion
    root = _root(store)
    with session_scope() as s:
        ds = _dataset(s, slug)
        user = _user(s, by)
        if user.role != "admin" and not _is_maintainer(s, ds.id, user.id):
            raise SharedDatasetError(f"{by} is not a maintainer of {slug}.")
        row = s.query(SharedDatasetVersion).filter_by(dataset_id=ds.id, label=check_label(label)).one_or_none()
        if row is None or row.status != "published":
            raise SharedDatasetError(f"{slug} {label} is not a published version.")
        row.status = "withdrawn"
        row.withdrawn_at = _now()
        s.commit()
        newest = _published(s, ds.id)
        latest = newest[0].label if newest else None
    _point_latest(root / slug, latest)
    return {"slug": slug, "label": label, "status": "withdrawn", "latest": latest}


def mark_hpc_verified(slug: str, label: str) -> dict:
    """Record that the version's HPC3 copy passed its checksum job."""
    from .db import session_scope
    from .models import SharedDatasetVersion
    with session_scope() as s:
        ds = _dataset(s, slug)
        row = s.query(SharedDatasetVersion).filter_by(dataset_id=ds.id, label=check_label(label)).one_or_none()
        if row is None or row.status == "publishing":
            raise SharedDatasetError(f"{slug} {label} is not published.")
        row.hpc_verified_at = _now()
        s.commit()
        return {"slug": slug, "label": label, "hpc_verified_at": _iso(row.hpc_verified_at)}


# --- reading -------------------------------------------------------------------------------------


def _version_public(v, *, files: "list | None" = None, published_by: "str | None" = None) -> dict:
    out = {"label": v.label, "uuid": v.uuid, "status": v.status, "published_at": _iso(v.published_at),
           "withdrawn_at": _iso(v.withdrawn_at), "size_bytes": int(v.size_bytes or 0),
           "n_files": int(v.n_files or 0), "manifest_sha256": v.manifest_sha256,
           "on_hpc3": v.hpc_verified_at is not None}
    if files is not None:
        out["changelog"] = v.changelog
        out["published_by"] = published_by
        out["files"] = [{"name": f.name, "size_bytes": int(f.size_bytes), "sha256": f.sha256,
                         "facets": json.loads(f.facets or "{}")} for f in sorted(files, key=lambda f: f.name)]
    return out


def _pick(session, ds, version: str, *, allow_withdrawn: bool):
    from .models import SharedDatasetVersion
    if version in (None, "", LATEST):
        newest = _published(session, ds.id)
        if not newest:
            raise SharedDatasetError(f"{ds.slug} has no published version yet.")
        return newest[0]
    row = (session.query(SharedDatasetVersion)
           .filter_by(dataset_id=ds.id, label=check_label(version)).one_or_none())
    if row is None or row.status == "publishing" or (row.status == "withdrawn" and not allow_withdrawn):
        raise SharedDatasetError(f"{ds.slug} has no version {version!r}"
                                 + (" (it was withdrawn)" if row is not None and row.status == "withdrawn" else "")
                                 + ".")
    return row


def dataset_views(user: Any = None) -> list[dict]:
    """Every shared dataset as an account sees it: no server path; versions still being published
    are left out; ``can_publish`` says whether this account may publish."""
    from .db import session_scope
    from .models import SharedDataset, SharedDatasetMaintainer, SharedDatasetVersion, User
    out = []
    with session_scope() as s:
        for ds in s.query(SharedDataset).order_by(SharedDataset.name).all():
            versions = [v for v in s.query(SharedDatasetVersion).filter_by(dataset_id=ds.id).all()
                        if v.status != "publishing"]
            versions.sort(key=lambda v: (_iso(v.published_at) or "", v.id), reverse=True)
            newest = next((v for v in versions if v.status == "published"), None)
            names = [r[0] for r in s.query(User.username).join(
                SharedDatasetMaintainer, SharedDatasetMaintainer.user_id == User.id)
                .filter(SharedDatasetMaintainer.dataset_id == ds.id).all()]
            is_admin = getattr(user, "role", "") == "admin"
            out.append({"slug": ds.slug, "uuid": ds.uuid, "name": ds.name, "description": ds.description,
                        "citation": ds.citation, "maintainers": sorted(names),
                        "latest": newest.label if newest else None,
                        "can_publish": bool(user is not None and (
                            is_admin or getattr(user, "username", None) in names)),
                        "versions": [_version_public(v) for v in versions]})
    return out


def version_view(slug: str, version: str = LATEST) -> dict:
    """One version with its files, facets and changelog. ``version`` is a label or ``latest``; a
    withdrawn version can still be read by its label."""
    from .db import session_scope
    from .models import SharedDatasetFile, User
    with session_scope() as s:
        ds = _dataset(s, slug)
        v = _pick(s, ds, version, allow_withdrawn=True)
        files = s.query(SharedDatasetFile).filter_by(version_id=v.id).all()
        by = s.get(User, v.published_by) if v.published_by else None
        return {"slug": ds.slug, "uuid": ds.uuid, "name": ds.name, "description": ds.description,
                "citation": ds.citation, "requested": version or LATEST,
                "version": _version_public(v, files=files, published_by=by.username if by else None)}


def resolve(slug: str, version: str = LATEST, *, allow_withdrawn: bool = False,
            store: "Path | str | None" = None, hpc_shared_root: "str | None" = None) -> dict:
    """What a run reads: the version ``latest`` (or a label) means now, with its gateway-host path
    and, when its HPC3 copy is verified and ``hpc_shared_root`` is given, the HPC3 path. A chat
    pinned to a withdrawn version passes ``allow_withdrawn``. For the gateway's own use: the paths
    must never reach an account."""
    from .db import session_scope
    from .models import SharedDatasetFile
    with session_scope() as s:
        ds = _dataset(s, slug)
        v = _pick(s, ds, version, allow_withdrawn=allow_withdrawn)
        files = s.query(SharedDatasetFile).filter_by(version_id=v.id).all()
        out = {"slug": ds.slug, "name": ds.name, "requested": version or LATEST, "label": v.label,
               "version_uuid": v.uuid, "status": v.status, "manifest_sha256": v.manifest_sha256,
               "server_path": str(version_dir(ds.slug, v.label, store)),
               "hpc_path": (hpc_version_dir(hpc_shared_root, ds.slug, v.label)
                            if hpc_shared_root and v.hpc_verified_at is not None else None),
               "files": [{"name": f.name, "size_bytes": int(f.size_bytes), "sha256": f.sha256,
                          "facets": json.loads(f.facets or "{}")} for f in sorted(files, key=lambda f: f.name)]}
    return out


def manifest_file(slug: str, version: str, *, store: "Path | str | None" = None) -> Path:
    """The manifest.json of a published or withdrawn version (``latest`` allowed)."""
    from .db import session_scope
    with session_scope() as s:
        ds = _dataset(s, slug)
        v = _pick(s, ds, version, allow_withdrawn=True)
        label = v.label
    path = version_dir(slug, label, store) / MANIFEST
    if not path.is_file():
        raise SharedDatasetError(f"{slug} {label} has no {MANIFEST} in the store.")
    return path


def verify(slug: str, version: str = LATEST, *, store: "Path | str | None" = None,
           log: "Callable[[str], None] | None" = None) -> list[str]:
    """Re-hash a version's files in the store against its manifest; returns the files that differ
    or are missing (empty = intact). Reads every byte: minutes for tens of GB."""
    say = log or (lambda _msg: None)
    info = resolve(slug, version, allow_withdrawn=True, store=store)
    folder = Path(info["server_path"])
    bad = []
    for f in info["files"]:
        path = folder / f["name"]
        if not path.is_file() or path.stat().st_size != f["size_bytes"] or _sha256(path) != f["sha256"]:
            bad.append(f["name"])
            say(f"  {f['name']}: DIFFERS")
        else:
            say(f"  {f['name']}: ok")
    return bad
