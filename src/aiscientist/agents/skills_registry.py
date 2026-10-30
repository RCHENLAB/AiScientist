"""Offline, pinned skill releases. Installed releases are immutable and fail closed."""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import zipfile
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path, PurePosixPath

from . import run_manifest

ROOT = Path(__file__).resolve().parents[3]
# Where the pinned releases are published (configs/skills_registry.json pins the tag).
REGISTRY_REPO = "lijinbio/aiscientist-skills"
POLICY: ContextVar[dict | None] = ContextVar("skill_policy", default=None)
# The registry skill the code of the CURRENT PLAN STEP runs under. A ContextVar, not
# ``ctx.decisions``: a plan's independent steps run in parallel threads that share one ctx and each
# copy the context, so one step's skill never reaches another step's code. Outside a step (the chat
# path) it is unset and ``ctx.decisions`` keeps the last skill read, as before.
_UNSET = object()
ACTIVE: ContextVar = ContextVar("active_registry_skill", default=_UNSET)
# What the code of the current plan step ran under each registry skill, counted once when the step
# ends (``step_scope``). Unset outside a step, where each call is counted as it ends.
_STEP_CALLS: ContextVar["dict | None"] = ContextVar("registry_skill_step_calls", default=None)
LOG = logging.getLogger(__name__)
_COUNT_LOCK = threading.Lock()
_SCOPES = itertools.count()


# What the last load found: the release tag it read and how many curated skills it gave, or the
# error that refused it. /health reports it, so deploy/deploy_skills.sh can see the switch take.
STATUS: dict = {"tag": None, "skills": 0, "error": None}
_VERSION_RE = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?"


def config() -> dict:
    """The registry settings. ``release_tag`` is the release the curated skills come from: the one
    ``current`` points at when that link exists (``skills/current``, switched by
    ``deploy/deploy_skills.sh``), else the tag the configuration pins (``pinned_tag``)."""
    path = Path(os.environ.get("AISCIENTIST_SKILLS_REGISTRY_CONFIG",
                               ROOT / "configs/skills_registry.json"))
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for key in ("release_tag", "releases_root", "allowed_tiers", "current"):
        value = os.environ.get("AISCIENTIST_SKILLS_REGISTRY_" + key.upper())
        if value is not None:
            cfg[key] = ([v.strip() for v in value.split(",") if v.strip()] if key == "allowed_tiers"
                        else value)
    if not re.fullmatch("v" + _VERSION_RE, cfg["release_tag"]):
        raise ValueError("skills registry release_tag must be a pinned v<major>.<minor>.<patch> tag")
    tiers = cfg["allowed_tiers"]
    if not isinstance(tiers, list) or not tiers or set(tiers) - {"core", "verified", "community"}:
        raise ValueError("invalid skills registry allowed_tiers")
    root = Path(cfg["releases_root"])
    cfg["releases_root"] = root if root.is_absolute() else ROOT / root
    cfg["pinned_tag"] = cfg["release_tag"]
    cfg["release_path"] = None
    if cfg.get("current"):
        link = Path(cfg["current"])
        link = link if link.is_absolute() else ROOT / link
        cfg["current"] = link
        if os.path.lexists(link):
            target = link.resolve()
            if not target.is_dir():
                raise ValueError(f"{link} points at {target}, which is not an installed skills release")
            if not re.fullmatch(_VERSION_RE, target.name):
                raise ValueError(f"{link} points at {target.name!r}, which is not a release version")
            cfg["release_tag"], cfg["release_path"] = "v" + target.name, target
    return cfg


def release_dir(cfg: dict) -> Path:
    return cfg.get("release_path") or cfg["releases_root"] / cfg["release_tag"][1:]


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


def network_allowed(policy: dict | None, platform_allows: bool) -> bool:
    """Whether the code of a call may reach the network. Without a registry skill, the platform's
    switch decides (``AISCIENTIST_SANDBOX_NETWORK``). A registry skill that declares any network
    use gets the same network as every other snippet: ``install`` included, because a skill's own
    steps install its software (``mamba create`` in the registry template). ``none`` runs offline.
    The platform switch, when off, wins over any declaration."""
    if policy is None:
        return platform_allows
    return platform_allows and policy.get("network", "none") != "none"


def _extract(archive: Path, dest: Path) -> None:
    """Unpack a verified zip into ``dest``, refusing unsafe paths and symlinks."""
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            safe_path(dest, info.filename)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("symlinks are not allowed in skill bundles")
        zf.extractall(dest)


def install_command(cfg: dict) -> str:
    """The one line that installs the pinned release where the loader reads it."""
    return f"gh release download {cfg['release_tag']} -R {REGISTRY_REPO} -D {release_dir(cfg)}"


def load(load_from) -> dict:
    """The curated skills of the pinned release. An absent release is an empty library, said in the
    log with the command that installs it; an installed release that fails a check raises."""
    STATUS.update(tag=None, skills=0, error=None)
    cfg = config()
    root = release_dir(cfg)
    if not root.exists():
        LOG.warning("Pinned skills release %s is not installed at %s: no curated skills are loaded. "
                    "Install it with: %s", cfg["release_tag"], root, install_command(cfg))
        STATUS["error"] = f"release {cfg['release_tag']} is not installed"
        return {}
    checked = check_release(root, cfg["release_tag"])
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
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
    STATUS.update(tag=cfg["release_tag"], skills=len(out))
    return out


def check_release(root: Path, tag: str) -> set[str]:
    """Check an installed release before anything of it is read: every file ``SHA256SUMS`` lists
    matches its checksum, and ``catalog.json`` is the catalog of ``tag``. Returns the checked files.
    ``deploy/deploy_skills.sh`` runs it (through :func:`_main`) before it switches ``skills/current``."""
    checked = verify(root)
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    if (catalog.get("schema_version") != 1 or catalog.get("registry") != "aiscientist-skills"
            or catalog.get("registry_version") != tag[1:]):
        raise ValueError("skill catalog does not match pinned registry release")
    return checked


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
    """Start the run's manifest.json (agents/run_manifest.py) with the skills release it reads."""
    if ctx is None or ctx.workspace is None:
        return
    try:
        release = config()["release_tag"]
    except Exception:  # noqa: BLE001 - a bad registry config leaves the run without curated skills, not without a manifest
        release = None
    run_manifest.init(ctx, release)


@contextmanager
def step_scope(skill=None, step: str = ""):
    """Run one plan step: ``skill`` (the one the plan assigned, if any) is the step's active registry
    skill, a skill read during the step replaces it, and nothing outlives the step.

    When the scope ends, each registry skill whose method the step's code ran is counted once
    (``count_step``). ``step`` names the plan step, so that the attempts of one step (a revision the
    Critic asked for) count as one use; without it each scope is a step of its own."""
    token = ACTIVE.set(skill if skill is not None and skill.registry_release else None)
    calls: dict = {}
    calls_token = _STEP_CALLS.set(calls)
    try:
        yield
    finally:
        _STEP_CALLS.reset(calls_token)
        ACTIVE.reset(token)
        try:
            count_step(step or f"\x00scope-{next(_SCOPES)}", calls)
        except Exception as exc:  # noqa: BLE001 - the counter is advisory; it never fails a step
            LOG.warning("could not count the skills of step %r: %s", step[:80], exc)


def count_step(step: str, calls: dict) -> None:
    """Count once, per registry skill, the step whose code ran under it.

    ``calls`` is ``{(release, name, version): (skill, ctx, [(ok, code, tool), ...])}``, as
    ``count_call`` records it. A call runs the skill's method when it is a call of a tool that runs
    the skill (``run_cellqc``) or a ``run_in_environment`` command (both run only the skill), and a
    ``run_code`` call when its code calls one of the skill's own functions
    (``skill_code.skill_functions``) or no such function can be found in the skill's code. A step
    none of whose calls ran the method is not counted: it inspected files or
    read another step's output, and said nothing about the version. The step is a success when one
    of those calls succeeded, an error when none did. A later attempt of the same step that
    succeeds turns that step's error into a success; nothing is counted twice."""
    from .skill_code import calls_to, skill_functions
    for skill, ctx, ran in calls.values():
        functions = skill_functions(skill)
        method = [ok for ok, code, tool in ran
                  if tool != "run_code" or not functions or calls_to(code, functions)]
        if method:
            _count_once(ctx, step, skill, "success" if any(method) else "error")


def _count_once(ctx, step: str, skill, state: str) -> None:
    key = "\x1f".join((step, skill.registry_release, skill.name, skill.version))
    with _COUNT_LOCK:
        decisions = getattr(ctx, "decisions", None)
        seen = decisions.setdefault("registry_skill_step_counts", {}) if isinstance(decisions, dict) else {}
        before = seen.get(key)
        if before == state or before == "success":
            return
        seen[key] = state
    if before is None:
        _count(skill, state, ctx)
    else:                                   # the step's earlier attempt failed; this one ran it
        _adjust(skill, ctx, success=1, error=-1)


def record_use(ctx, skill) -> None:
    if ctx is None:
        return
    active = skill if skill.registry_release else None
    ctx.decisions["active_registry_skill"] = active
    if ACTIVE.get() is not _UNSET:
        ACTIVE.set(active)
    record_in_manifest(ctx, skill)


def record_in_manifest(ctx, skill) -> None:
    """Add ``skill`` (name, version, release) to the run's manifest.json, without making it the
    active skill: a tool that runs a skill's method (``skill:`` in its TOOL.md) records it here, and
    must not change how later run_code calls are sandboxed."""
    if ctx is None:
        return
    init_manifest(ctx)
    if ctx.workspace is None:
        return
    item = {"name": skill.name, "version": skill.version or "local",
            "registry_release": skill.registry_release or None}
    if getattr(skill, "image", ""):
        item["image"] = skill.image          # the environment the skill pins (its SKILL.md)

    def add(doc: dict) -> bool:
        if item in doc.setdefault("skills", []):
            return False
        doc["skills"].append(item)
        return True
    run_manifest.update(ctx, add)


def active_skill(ctx, name: str = ""):
    if name:
        from .skills import get_skill, refresh_skills
        refresh_skills()
        skill = get_skill(name)
        if skill is None:
            raise ValueError(f"unknown skill {name!r}")
        return skill
    current = ACTIVE.get()
    if current is not _UNSET:
        return current
    return ctx.decisions.get("active_registry_skill") if ctx is not None else None


def execute(skill, ctx, executor, call, tool: str, code: str = ""):
    """Apply immutable per-call policy; never mutate a shared executor or process environment.

    ``code`` is what the call runs (the snippet, the command). The call is counted by
    ``count_call``."""
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
    count_call(skill, ctx, result, tool, code)
    return result


def count_call(skill, ctx, result, tool: str, code: str = "") -> None:
    """Count one call that ran under registry skill ``skill``, by its ``result``: a ``run_code`` or
    ``run_in_environment`` call (``execute``), or a call of a tool that runs the skill's method
    (``skill:`` in its TOOL.md, ``tool_skills.attach_skill``). Inside a plan step the call is
    recorded for the step's count (``count_step``); outside one it is counted as it ends. A call
    the user stopped, or one whose result was reused rather than run again, is not counted."""
    if not skill.registry_release:
        return
    status = result.get("status") if isinstance(result, dict) else None
    if status == "cancelled":   # the user pressed Stop: not a verdict on the skill version
        return
    if isinstance(result, dict) and result.get("reused_existing"):   # an earlier call's result
        return
    ok = status in {"ok", "success"}
    step = _STEP_CALLS.get()
    if step is None:
        _count(skill, "success" if ok else "error", ctx)
    else:
        key = (skill.registry_release, skill.name, skill.version)
        step.setdefault(key, (skill, ctx, []))[2].append((ok, str(code or ""), tool))


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
    _adjust(skill, ctx, **{state: 1})


def _adjust(skill, ctx, success: int = 0, error: int = 0) -> None:
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
                db.execute("UPDATE skill_versions SET success=MAX(0, success+?), error=MAX(0, error+?) "
                           "WHERE release=? AND name=? AND version=?",
                           (success, error, skill.registry_release, skill.name, skill.version))
        finally:
            db.close()
    except (OSError, sqlite3.Error) as exc:
        # The counter is for rollback decisions; it must never fail the run itself.
        LOG.warning("could not record skill version outcome in %s: %s", path, exc)


def _main(argv: list[str] | None = None) -> int:
    """``python -m aiscientist.agents.skills_registry check <release dir> <tag>``: the deploy's check
    of a downloaded release, the same one the loader makes. Exit 0 when it passes."""
    import sys
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] != "check":
        print("usage: python -m aiscientist.agents.skills_registry check <release dir> <tag>", file=sys.stderr)
        return 2
    try:
        files = check_release(Path(args[1]), args[2])
    except (OSError, ValueError, KeyError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"{args[2]}: {len(files)} files match SHA256SUMS, catalog.json is the catalog of {args[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
