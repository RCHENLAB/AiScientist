"""Offline, pinned skill releases. Installed releases are immutable and fail closed."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import zipfile
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[3]
POLICY: ContextVar[dict | None] = ContextVar("skill_policy", default=None)
LOG = logging.getLogger(__name__)
_MANIFEST_LOCK = threading.RLock()


def config() -> dict:
    path = Path(os.environ.get("AISCIENTIST_SKILLS_REGISTRY_CONFIG",
                               ROOT / "configs/skills_registry.json"))
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for key in ("release_tag", "releases_root", "allowed_tiers", "transitional_fallback"):
        value = os.environ.get("AISCIENTIST_SKILLS_REGISTRY_" + key.upper())
        if value is not None:
            cfg[key] = ([v.strip() for v in value.split(",") if v.strip()] if key == "allowed_tiers" else
                        value.lower() in {"1", "true", "yes"} if key == "transitional_fallback"
                        else value)
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", cfg["release_tag"]):
        raise ValueError("skills registry release_tag must be a pinned v<major>.<minor>.<patch> tag")
    tiers = cfg["allowed_tiers"]
    if not isinstance(tiers, list) or not tiers or set(tiers) - {"core", "verified", "community"}:
        raise ValueError("invalid skills registry allowed_tiers")
    root = Path(cfg["releases_root"])
    cfg["releases_root"] = root if root.is_absolute() else ROOT / root
    return cfg


def release_dir(cfg: dict) -> Path:
    return cfg["releases_root"] / cfg["release_tag"][1:]


def safe_path(root: Path, name: str) -> Path:
    rel = PurePosixPath(name)
    if not name or "\\" in name or ":" in name or rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"unsafe registry path: {name!r}")
    path = root.joinpath(*rel.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"registry path escapes release: {name!r}")
    return path


def verify(root: Path) -> set[str]:
    checked = set()
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(.+)", line)
        if not match:
            raise ValueError("invalid SHA256SUMS line")
        digest, name = match.groups()
        path = safe_path(root, name)
        if name in checked or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest.lower():
            raise ValueError(f"registry checksum mismatch or missing file: {name}")
        checked.add(name)
    if "catalog.json" not in checked:
        raise ValueError("catalog.json is not covered by SHA256SUMS")
    return checked


def requirements(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("skill requirements must be an object")
    req = {"compute": "", "gpu": False, "scheduler": "none", "network": "none",
           "secrets": [], "allowed_tools": "", **value}
    if (type(req["gpu"]) is not bool or req["scheduler"] not in {"none", "optional", "required"}
            or req["network"] not in {"none", "install", "required", "optional", "runtime"}
            or not isinstance(req["secrets"], list)
            or any(not isinstance(s, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s)
                   for s in req["secrets"])):
        raise ValueError("invalid skill requirements")
    return req


def _extract(archive: Path, dest: Path) -> None:
    """Unpack a verified zip into ``dest``, refusing unsafe paths and symlinks."""
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            safe_path(dest, info.filename)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("symlinks are not allowed in skill bundles")
        zf.extractall(dest)


def load(load_from) -> dict | None:
    cfg = config()
    root = release_dir(cfg)
    if not root.exists():
        if not cfg.get("transitional_fallback", False):
            raise FileNotFoundError(f"pinned skills release is not installed: {root}")
        LOG.warning("Pinned skills release %s not installed at %s; using transitional local skills fallback",
                    cfg["release_tag"], root)
        return None
    checked = verify(root)
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    if (catalog.get("schema_version") != 1 or catalog.get("registry") != "aiscientist-skills"
            or catalog.get("registry_version") != cfg["release_tag"][1:]):
        raise ValueError("skill catalog does not match pinned registry release")
    out = {}
    # The registry's release workflow publishes one aiscientist-skills-<version>.zip holding each
    # skill folder at its catalog path (aiscientist_skills/package.py); per-skill zips are still read.
    # Extraction is temporary; text is read into Skill, so no writable cache can bypass hashes.
    bundle_name = f"aiscientist-skills-{cfg['release_tag'][1:]}.zip"
    with ExitStack() as stack:
        bundle = None
        if bundle_name in checked:
            bundle = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="aiscientist-skills-")))
            _extract(safe_path(root, bundle_name), bundle)
        for row in catalog["skills"]:
            _load_row(row, cfg, root, checked, bundle, load_from, out)
    return out


def _load_row(row: dict, cfg: dict, root: Path, checked: set[str], bundle: Path | None, load_from,
              out: dict) -> None:
    """Add one catalog row's skill to ``out`` if its tier is allowed."""
    name = row["name"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name in out:
        raise ValueError("invalid or duplicate catalog skill name")
    if row["status"] not in {"core", "verified", "community"}:
        raise ValueError("invalid catalog skill status")
    if row["status"] not in cfg["allowed_tiers"]:
        return
    if not row.get("version"):
        raise ValueError(f"missing skill version: {name}")
    req = requirements(row["requirements"])
    folder = safe_path(root, row["path"])
    if folder.is_dir():
        # Never load a file which was not covered by the release checksums.
        for path in folder.rglob("*"):
            if path.is_file() and path.relative_to(root).as_posix() not in checked:
                raise ValueError(f"unverified skill file: {path}")
        loaded = load_from(folder)
    elif bundle is not None and safe_path(bundle, row["path"]).is_dir():
        loaded = load_from(safe_path(bundle, row["path"]))
    else:
        # Older releases: one zip per skill, <name>-<version>.zip; two other names are accepted.
        candidates = [f"{name}-{row['version']}.zip", f"{name}.zip", row["path"] + ".zip"]
        archive = next((p for p in candidates if p in checked), None)
        if archive is None:
            raise ValueError(f"missing verified bundle for skill {name}")
        with tempfile.TemporaryDirectory(prefix="aiscientist-skill-") as temp:
            _extract(safe_path(root, archive), Path(temp))
            loaded = load_from(Path(temp))
    if len(loaded) != 1:
        raise ValueError(f"bundle must contain exactly one skill: {name}")
    skill = next(iter(loaded.values()))
    out[name] = replace(skill, name=name, summary=row["description"], category=row["category"],
                        version=row["version"], status=row["status"], tags=tuple(row.get("tags", [])),
                        requirements=req, registry_release=cfg["release_tag"], folder="",
                        induced=False, review="",
                        metadata={**skill.metadata, "compute": req["compute"]})


def init_manifest(ctx) -> None:
    if ctx is None or ctx.workspace is None:
        return
    path = Path(ctx.workspace) / "manifest.json"
    try:
        release = config()["release_tag"]
    except Exception:  # noqa: BLE001 - a bad registry config leaves the run without curated skills, not without a manifest
        release = None
    with _MANIFEST_LOCK:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            _write(path, {"registry_release": release, "skills": []})


def _write(path: Path, value: dict) -> None:
    # Unique temporary files also support concurrent run workspaces.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False) as f:
        json.dump(value, f, indent=2)
    os.replace(f.name, path)


def record_use(ctx, skill) -> None:
    if ctx is None:
        return
    ctx.decisions["active_registry_skill"] = skill if skill.registry_release else None
    init_manifest(ctx)
    if ctx.workspace is None:
        return
    path = Path(ctx.workspace) / "manifest.json"
    with _MANIFEST_LOCK:
        doc = json.loads(path.read_text(encoding="utf-8"))
        item = {"name": skill.name, "version": skill.version or "local",
                "registry_release": skill.registry_release or None}
        if item not in doc.setdefault("skills", []):
            doc["skills"].append(item)
            _write(path, doc)


def active_skill(ctx, name: str = ""):
    if name:
        from .skills import get_skill, refresh_skills
        refresh_skills()
        skill = get_skill(name)
        if skill is None:
            raise ValueError(f"unknown skill {name!r}")
        return skill
    return ctx.decisions.get("active_registry_skill") if ctx is not None else None


def execute(skill, ctx, executor, call, tool: str):
    """Apply immutable per-call policy; never mutate a shared executor or process environment."""
    if skill is None:
        return call()
    record_use(ctx, skill)
    if not skill.registry_release:
        return call()
    req = skill.requirements
    secrets = {}
    token = None
    try:
        if not getattr(executor, "supports_skill_policy", False):
            raise ValueError("executor cannot enforce registry skill requirements")
        allowed = req.get("allowed_tools")
        # Agent Skills writes allowed-tools space-separated; commas are accepted too.
        allowed = [v for v in re.split(r"[\s,]+", allowed) if v] if isinstance(allowed, str) else allowed
        if allowed and tool not in [str(v).strip() for v in allowed]:
            raise ValueError(f"skill does not declare tool {tool}")
        for name in req["secrets"]:
            # A skill can name a credential, but cannot choose its source or receive its value.
            value = os.environ.get("AISCIENTIST_SKILL_SECRET_" + name)
            if value is None or "\n" in value or "\r" in value:
                raise ValueError(f"required server-side secret is unavailable: {name}")
            secrets[name] = value
        token = POLICY.set({**req, "secret_values": secrets})
        result = call()
    except Exception as exc:
        result = {"status": "error", "error": str(exc)}
    finally:
        if token is not None:
            POLICY.reset(token)
    # A command may echo its environment. Values must never reach the model or event stream.
    def redact(value):
        if isinstance(value, str):
            for secret in secrets.values():
                if secret:
                    value = value.replace(secret, "[REDACTED]")
        elif isinstance(value, dict):
            value = {k: redact(v) for k, v in value.items()}
        elif isinstance(value, list):
            value = [redact(v) for v in value]
        return value
    result = redact(result)
    status = result.get("status") if isinstance(result, dict) else None
    if status != "cancelled":   # the user pressed Stop: not a verdict on the skill version
        _count(skill, "success" if status in {"ok", "success"} else "error", ctx)
    return result


def stats_path(ctx=None) -> Path:
    """One file for the whole deployment, so error rates per version are counted across every
    account: ``AISCIENTIST_SKILLS_REGISTRY_STATS``, else ``<AISCIENTIST_STATE_DIR>``, else next to
    the run folders (a console without a state dir)."""
    explicit = os.environ.get("AISCIENTIST_SKILLS_REGISTRY_STATS", "").strip()
    if explicit:
        return Path(explicit)
    state = os.environ.get("AISCIENTIST_STATE_DIR", "").strip()
    if state:
        return Path(state) / "skill_versions.sqlite3"
    if ctx is not None and getattr(ctx, "workspace", None):
        return Path(ctx.workspace).parent / "skill_versions.sqlite3"
    return ROOT / ".skill_versions.sqlite3"


def _count(skill, state: str, ctx) -> None:
    path = stats_path(ctx)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=10)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS skill_versions (release TEXT, name TEXT, version TEXT, "
                           "success INTEGER DEFAULT 0, error INTEGER DEFAULT 0, PRIMARY KEY(release,name,version))")
                db.execute("INSERT OR IGNORE INTO skill_versions(release,name,version) VALUES(?,?,?)",
                           (skill.registry_release, skill.name, skill.version))
                db.execute(f"UPDATE skill_versions SET {state}={state}+1 WHERE release=? AND name=? AND version=?",
                           (skill.registry_release, skill.name, skill.version))
        finally:
            db.close()
    except (OSError, sqlite3.Error) as exc:
        # The counter is for rollback decisions; it must never fail the run itself.
        LOG.warning("could not record skill version outcome in %s: %s", path, exc)
