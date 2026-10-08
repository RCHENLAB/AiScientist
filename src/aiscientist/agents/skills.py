"""Atomic skill library — small, model-rewritable code capabilities surfaced on demand.

A *skill* is any folder holding a ``SKILL.md``, at ANY depth under ``skills/`` (repo root), in the
Anthropic Agent-Skills shape — so ``skills/<name>/`` and a category layout such as
``skills/single-cell/qc-snrna-with-cellqc-standalone/`` both work, and a skill written for another
Agent-Skills catalog can be dropped in unchanged:

- ``SKILL.md`` — the ONLY required file: frontmatter (``name`` + ``description``, plus optional
  Agent-Skills fields such as ``license``, ``compatibility`` and a nested ``metadata:`` block) and a
  markdown body. The description is the manifest label; the body is the guidance — and for a short
  skill it can carry the code or shell commands inline.
- any other file in the folder or its subfolders (``reference.py``, ``scripts/stage.sh``, ...) — a
  bundled file the Scientist fetches on demand, adapts, and runs.

A folder dropped in is picked up without a restart: :func:`refresh_skills` re-scans whenever the
files on disk change, and the lab calls it before every step.

It sits between the two other layers (see ``docs/skills_and_pipelines_architecture.md``):

- unlike the fixed **registry** tools (``agents/registry.py``), a skill is meant to be READ,
  ADAPTED, and run via ``run_code`` — and grown by induction;
- unlike a **preset pipeline** (``agents/preset_pipelines.py``), a skill is atomic and composable,
  not a whole end-to-end workflow.

**Progressive disclosure — three levels, so a template only costs context when it is used:**

1. the Scientist's per-step brief lists the skill MANIFEST (``name`` + description, grouped by
   category) — every skill, with no cap, so the agent sees the whole library before it chooses;
2. ``read_skill_reference(name)`` returns the SKILL.md body plus the list of bundled files;
3. ``read_skill_reference(name, file="reference.py")`` returns one bundled file on demand.

``search_skills(query)`` ranks the library by keyword for when the list is long.

Override the location with ``$AISCIENTIST_SKILLS_DIR`` (repo-root ``skills/`` by default).

**Induced skills are reviewed before any model sees them.** :data:`ALL_SKILLS` is everything on disk;
:data:`SKILLS` — what the PI plans with and the Scientist lists, searches and reads — holds the
curated skills plus only the induced skills an admin has APPROVED (``_review.json`` in the induced
root, written by :func:`set_review` from the console's skill-review tab).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .research_harness import HarnessContext, HarnessTool

# Folders whose name starts with one of these are never scanned: ``_drafts/``, ``.git/`` of a cloned
# skills repository, editor and OS litter.
_SKIP_PREFIXES = ("_", ".")
# A bundled file larger than this is not read into memory (a skill may ship test data next to its
# code; the agent needs the code, not a matrix).
_MAX_BUNDLED_BYTES = 1_000_000
# A planning-prompt line keeps at most this much of a skill's description: the Agent-Skills maximum,
# so a well-formed description is never cut. It was 200 (2026-10-01), which cut off exactly the
# sentence that routes a skill — "Inside an AiScientist analysis, ... is the run_cellqc tool" ends
# the CellQC skill's description. There is no limit on how many skills are listed (Yijun, 2026-10-02).
_PLAN_SUMMARY_CHARS = 1024


@dataclass(frozen=True)
class Skill:
    """One atomic, adaptable capability. ``summary`` (SKILL.md ``description``) is what the manifest
    advertises; ``doc`` (the SKILL.md body) is fetched on demand; ``files`` (``reference.py``,
    ``scripts/...``, any bundle) are fetched one level deeper still — so a large template only enters
    context when a step actually uses it. A skill may have no files at all: its code is in ``doc``."""
    name: str                                  # folder / frontmatter name, e.g. "perturbation_edistance"
    summary: str = ""                          # frontmatter description — the manifest label
    doc: str = ""                              # SKILL.md body: guidance, and possibly inline code
    files: "dict[str, str]" = field(default_factory=dict)  # bundled path -> source ("scripts/x.sh")
    induced: bool = False                      # machine-written (skill_induction) rather than curated
    supersedes: str = ""                       # an older skill this one is a better version of
    category: str = ""                         # metadata.category, else the folder it sits in
    metadata: "dict[str, Any]" = field(default_factory=dict)  # the SKILL.md ``metadata:`` block
    folder: str = ""                           # where it was loaded from (diagnostics)
    review: str = ""                           # induced only: "pending" | "approved" | "retired"
    # The software environment the skill's commands run in, declared like a TOOL.md's: ``image``
    # (``bioconda:<pkg>=<ver> ...`` or a pinned ``docker://``) plus optional Slurm resources. The
    # platform provisions it on first use and ``run_in_environment`` runs commands inside it, so a
    # skill that needs, say, an R pipeline works by being dropped in — no wrapper tool. "" = none.
    image: str = ""
    cpus: int = 0
    mem_gb: int = 0
    time_limit: str = ""
    env_problem: str = ""                      # why a declared environment was not accepted


def _skills_dir() -> Path:
    """The atomic-skill library: ``$AISCIENTIST_SKILLS_DIR`` or repo-root ``skills/``."""
    env = os.environ.get("AISCIENTIST_SKILLS_DIR")
    if env:
        return Path(env)
    # skills.py -> agents -> aiscientist -> src -> <repo root>
    return Path(__file__).resolve().parents[3] / "skills"


def _unquote(value: str) -> str:
    """``"1.0.0"`` / ``'x'`` -> the bare string (Agent-Skills frontmatter often quotes values)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Split a SKILL.md into (frontmatter dict, body). Frontmatter is the leading ``---``-delimited
    block of ``key: value`` lines; body is the rest. (Same convention as preset pipelines.)

    Beyond flat keys it reads what the Agent-Skills standard uses: quoted values, YAML block scalars
    (``description: >-``), and ONE level of nesting — a key with no value followed by indented
    ``key: value`` lines becomes a dict (``metadata:``), or indented ``- item`` lines a list."""
    meta: dict[str, Any] = {}
    body = text
    if text.lstrip().startswith("---"):
        rest = text.lstrip()[3:]
        end = rest.find("\n---")
        if end != -1:
            front = rest[:end]
            body = rest[end + 4:]
            lines = front.splitlines()
            i = 0
            while i < len(lines):
                raw, line = lines[i], lines[i].strip()
                i += 1
                if not line or line.startswith("#") or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                key, value = k.strip(), v.strip()
                # YAML block scalars (``description: >-`` / ``|``): the value is the INDENTED
                # block that follows, not the marker. Without this the description parses as the
                # literal ">-" and the skill advertises itself in the manifest as punctuation —
                # which is exactly what `literature-corpus-recovery` was doing.
                if value in (">", ">-", ">+", "|", "|-", "|+"):
                    indent = len(raw) - len(raw.lstrip())
                    block: list[str] = []
                    while i < len(lines):
                        nxt = lines[i]
                        if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                            break            # dedented back to a sibling key
                        block.append(nxt.strip())
                        i += 1
                    joiner = "\n" if value.startswith("|") else " "   # literal keeps line breaks
                    value = joiner.join(b for b in block if b).strip()
                elif value == "":
                    # A nested block (``metadata:`` + indented lines). Without this, its children
                    # were read as TOP-LEVEL keys, so a skill's ``metadata.version`` silently became
                    # its ``version`` and the block itself an empty string.
                    indent = len(raw) - len(raw.lstrip())
                    nested: dict[str, str] = {}
                    items: list[str] = []
                    while i < len(lines):
                        nxt = lines[i]
                        if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                            break
                        child = nxt.strip()
                        i += 1
                        if not child or child.startswith("#"):
                            continue
                        if child.startswith("- "):
                            items.append(_unquote(child[2:].strip()))
                        elif ":" in child:
                            ck, _, cv = child.partition(":")
                            nested[ck.strip()] = _unquote(cv.strip())
                    if nested or items:
                        meta[key] = nested if nested else items
                        continue
                meta[key] = _unquote(value)
    return meta, body.strip()


def _first_line(text: str) -> str:
    """First non-empty line of ``text`` (the manifest-label fallback when no frontmatter description)."""
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")[:200]


def _induced_dir() -> "Path | None":
    """The INDUCED-skill root (``$AISCIENTIST_INDUCED_SKILLS_DIR``), or None when induction is not
    configured. Kept separate from the curated library on purpose — see ``skill_induction.py``:
    machine-written templates live in the gateway workspace, never in the git-tracked ``skills/``."""
    env = os.environ.get("AISCIENTIST_INDUCED_SKILLS_DIR")
    return Path(env) if env else None


def _roots() -> "list[Path]":
    """Where skills are loaded from, curated first: ``skills/`` and, if configured, the induced root."""
    induced = _induced_dir()
    return [_skills_dir(), *([induced] if induced is not None else [])]


def _load_skills() -> dict[str, Skill]:
    """Load every ``SKILL.md`` under the skill roots, at any depth, into the library. The skill name
    is its frontmatter ``name`` or, failing that, its folder name. Everything in the induced root is
    induced — whatever its frontmatter says — and carries its review status (``pending`` until an
    admin decides), so a folder dropped there cannot skip the review."""
    out = _load_from(_skills_dir())
    induced = _induced_dir()
    if induced is not None:
        reviews = _load_reviews()
        # Curated wins: an induced skill can never shadow a hand-authored one of the same name.
        for name, skill in _load_from(induced).items():
            if name not in out:
                status = str((reviews.get(name) or {}).get("status") or "pending")
                out[name] = replace(skill, induced=True,
                                    review=status if status in REVIEW_STATUSES else "pending")
    return out


# --- review gate for induced skills ------------------------------------------
# An induced skill is one run's run_code frozen into a template, with that dataset's file format and
# column names baked in. Offered to the PI on every later run, it got planned onto data it could not
# read: run f3b8268c4fd4 put an h5ad provenance audit (read_h5ad + DDX41's nCount_RNA / sampleid /
# majorclass) in front of a 10x .h5 and failed three times. So an induced skill reaches the models
# only once an admin approves it (Yijun, 2026-10-07); until then it is on disk, listed for review,
# and invisible to planning and to the Scientist. Curated skills are unaffected.
REVIEW_STATUSES = ("pending", "approved", "retired")
_REVIEW_FILE = "_review.json"
_REVIEW_HISTORY = 20


def _review_path() -> "Path | None":
    root = _induced_dir()
    return root / _REVIEW_FILE if root is not None else None


def _load_reviews() -> "dict[str, dict]":
    """``{skill name: {status, by, at, note, history}}`` from the induced root, ``{}`` when absent or
    unreadable (an unreadable file leaves every induced skill pending, never approved)."""
    path = _review_path()
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def visible_to_models(skill: Skill) -> bool:
    """Curated skills always; induced ones only once approved."""
    return not skill.induced or skill.review == "approved"


def _skill_md_files(root: Path) -> "list[Path]":
    """Every ``SKILL.md`` under ``root`` at any depth, in path order, skipping ``_``/``.`` folders."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(_SKIP_PREFIXES))
        if "SKILL.md" in filenames:
            found.append(Path(dirpath) / "SKILL.md")
    return found


def _bundled_files(folder: Path) -> "dict[str, str]":
    """Every readable text file in a skill folder and its subfolders except the SKILL.md, keyed by
    its path relative to the folder (``reference.py``, ``scripts/stage.sh``). A subfolder that holds
    its own SKILL.md is a separate skill and is not bundled into this one."""
    files: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        here = Path(dirpath)
        if here != folder and (here / "SKILL.md").is_file():
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(_SKIP_PREFIXES))
        for fn in sorted(filenames):
            if fn.startswith(".") or (here == folder and fn == "SKILL.md"):
                continue
            path = here / fn
            try:
                if path.stat().st_size > _MAX_BUNDLED_BYTES:
                    continue
                files[path.relative_to(folder).as_posix()] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue                  # binary or unreadable: not something the agent can adapt
    return files


_ENV_KEYS = ("image", "cpus", "mem_gb", "time_limit")


def _environment(meta: "dict[str, Any]", metadata: "dict[str, Any]") -> "tuple[dict[str, Any], str]":
    """``({image, cpus, mem_gb, time_limit}, problem)`` from a SKILL.md. The keys may sit at the top
    level or inside ``metadata:`` (where the Agent Skills format keeps custom fields; the top level
    wins). Validated exactly like a TOOL.md; an invalid declaration is dropped, with the reason."""
    def pick(key: str) -> Any:
        value = meta.get(key)
        return metadata.get(key) if value in (None, "") else value

    env: dict[str, Any] = {"image": str(pick("image") or "").strip(), "cpus": 0, "mem_gb": 0,
                           "time_limit": str(pick("time_limit") or "").strip()}
    for key in ("cpus", "mem_gb"):
        value = pick(key)
        if value in (None, ""):
            continue
        try:
            env[key] = int(str(value).strip())
        except ValueError:
            return {}, f"{key} {value!r} is not an integer"
    if not env["image"]:
        return ({}, "resources are set but no image") if any(env[k] for k in _ENV_KEYS) else ({}, "")
    from ..tools.catalog import image_problem, resources_problem
    problem = image_problem(env["image"]) or resources_problem(env["cpus"], env["mem_gb"],
                                                               env["time_limit"])
    return ({}, problem) if problem else (env, "")


def _load_from(root: Path) -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    if not root.is_dir():
        return out
    for skill_md in _skill_md_files(root):
        folder = skill_md.parent
        try:
            meta, body = _parse_front_matter(skill_md.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        name = str(meta.get("name") or folder.name).strip()
        if name in out:
            # Two folders claiming one name: the first in path order is kept, and the clash is said
            # out loud rather than letting whichever loads last silently win.
            print(f"[skills] {folder} is also named {name!r}; keeping {out[name].folder}")
            continue
        description = meta.get("description")
        summary = description if isinstance(description, str) and description else _first_line(body)
        metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
        rel_parent = folder.parent.relative_to(root).as_posix() if folder != root else "."
        category = str(metadata.get("category") or ("" if rel_parent == "." else rel_parent))
        files = _bundled_files(folder)
        env, env_problem = _environment(meta, metadata)
        if env_problem:
            print(f"[skills] {folder}: environment not used: {env_problem}")
        if name and (body or files):
            out[name] = Skill(name=name, summary=summary, doc=body, files=files,
                              induced=str(meta.get("induced", "")).strip().lower() in ("1", "true", "yes"),
                              supersedes=str(meta.get("supersedes", "")).strip(),
                              category=category, metadata=dict(metadata), folder=str(folder),
                              image=env.get("image", ""), cpus=env.get("cpus", 0),
                              mem_gb=env.get("mem_gb", 0), time_limit=env.get("time_limit", ""),
                              env_problem=env_problem)
    return out


def _signature() -> tuple:
    """What the skill roots look like on disk: every file's path, size and mtime. Cheap — a stat per
    file — and it changes whenever a skill folder is added, removed or edited."""
    sig: list[tuple] = []
    for root in _roots():
        if not root.is_dir():
            sig.append((str(root), None))
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(_SKIP_PREFIXES)]
            for fn in filenames:
                try:
                    st = os.stat(os.path.join(dirpath, fn))
                except OSError:
                    continue
                sig.append((dirpath, fn, st.st_size, st.st_mtime_ns))
    return tuple(sorted(sig, key=repr))


# Everything on disk (plus in-process registrations): the review tab and induction's name checks.
ALL_SKILLS: dict[str, Skill] = _load_skills()
# What the models see: curated + APPROVED induced. Other modules hold this dict; it is updated in place.
SKILLS: dict[str, Skill] = {n: s for n, s in ALL_SKILLS.items() if visible_to_models(s)}
_SIGNATURE: tuple = _signature()
_LOCK = threading.Lock()
# Skills added in-process by register_skill (induction), kept across re-scans while they are loaded.
_REGISTERED: dict[str, Skill] = {}


def refresh_skills() -> bool:
    """Bring :data:`SKILLS` up to date with the folders on disk, so a skill dropped into ``skills/``
    (or edited, or removed) is seen by the next step without a restart. A no-op unless a file under
    the skill roots changed since the last scan. Updates the dict IN PLACE, because other modules
    hold a reference to it. Returns True when the library changed."""
    global _SIGNATURE
    sig = _signature()
    if sig == _SIGNATURE:
        return False
    with _LOCK:
        fresh = _load_skills()
        for name, skill in list(_REGISTERED.items()):
            # still loaded (visible, or held back for review): keep it; disk wins on a clash
            if name in SKILLS or (name in ALL_SKILLS and not visible_to_models(ALL_SKILLS[name])):
                fresh.setdefault(name, skill)
            else:
                _REGISTERED.pop(name, None)       # removed since: forget it
        visible = {n: s for n, s in fresh.items() if visible_to_models(s)}
        for target, wanted in ((ALL_SKILLS, fresh), (SKILLS, visible)):
            for name in [n for n in list(target) if n not in wanted]:
                target.pop(name, None)
            for name, skill in wanted.items():
                if target.get(name) != skill:
                    target[name] = skill
        _SIGNATURE = sig
    return True


def register_skill(skill: Skill) -> bool:
    """Add a newly INDUCED skill to the in-process library without a restart. It enters
    :data:`ALL_SKILLS` as ``pending`` and reaches :data:`SKILLS` (the models) only once approved.
    Additive only — an existing name (curated or already induced) is never replaced, so a concurrent
    run reading the manifest can never see a skill change under it, only a new one appear. Returns
    True if it was added."""
    if not skill.name or skill.name in ALL_SKILLS or skill.name in SKILLS:
        return False
    if skill.induced and not skill.review:
        skill = replace(skill, review="pending")
    ALL_SKILLS[skill.name] = skill
    if visible_to_models(skill):
        SKILLS[skill.name] = skill
    _REGISTERED[skill.name] = skill
    return True


def set_review(name: str, status: str, *, by: str = "", note: str = "") -> dict:
    """Record an admin's decision on an induced skill — ``approved`` (the models may use it),
    ``retired`` (kept on disk, never offered) or back to ``pending`` — and apply it to the library.
    Raises ``ValueError`` for an unknown status, ``KeyError`` for a name that is not an induced skill,
    ``RuntimeError`` when no induced root is configured."""
    if status not in REVIEW_STATUSES:
        raise ValueError(f"status must be one of {REVIEW_STATUSES}")
    path = _review_path()
    if path is None:
        raise RuntimeError("no induced-skill directory is configured")
    refresh_skills()
    skill = ALL_SKILLS.get(name)
    if skill is None or not skill.induced:
        raise KeyError(name)
    with _LOCK:
        reviews = _load_reviews()
        prev = reviews.get(name) or {}
        history = list(prev.get("history") or [])
        if prev.get("status"):
            history.append({k: prev.get(k) for k in ("status", "by", "at", "note")})
        rec = {"status": status, "by": by, "note": (note or "")[:500],
               "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
               "history": history[-_REVIEW_HISTORY:]}
        reviews[name] = rec
        tmp = path.with_name("." + path.name + ".tmp")
        tmp.write_text(json.dumps(reviews, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    # A registered (not yet re-scanned) skill carries its status on the object, not on disk.
    if name in _REGISTERED:
        _REGISTERED[name] = replace(_REGISTERED[name], review=status)
        ALL_SKILLS[name] = _REGISTERED[name]
        if visible_to_models(_REGISTERED[name]):
            SKILLS[name] = _REGISTERED[name]
        else:
            SKILLS.pop(name, None)
    refresh_skills()
    return rec


def review_listing() -> "list[dict]":
    """Every induced skill for the review tab: status, provenance and who decided, newest first."""
    refresh_skills()
    reviews = _load_reviews()
    rows = []
    for s in list(ALL_SKILLS.values()):
        if not s.induced:
            continue
        meta: dict = {}
        mtime = 0.0
        if s.folder:
            md = Path(s.folder) / "SKILL.md"
            try:
                meta, _ = _parse_front_matter(md.read_text(encoding="utf-8"))
                mtime = md.stat().st_mtime
            except (OSError, UnicodeDecodeError):
                pass
        r = reviews.get(s.name) or {}
        rows.append({"name": s.name, "summary": s.summary, "status": s.review or "pending",
                     "supersedes": s.supersedes, "files": sorted(s.files),
                     "origin_step": str(meta.get("origin_step", ""))[:600],
                     "origin_run": str(meta.get("origin_run", "")),
                     "created_at": (_dt.datetime.fromtimestamp(mtime, _dt.timezone.utc)
                                    .isoformat(timespec="seconds") if mtime else None),
                     "reviewed_by": r.get("by"), "reviewed_at": r.get("at"), "note": r.get("note", "")})
    rows.sort(key=lambda row: row["created_at"] or "", reverse=True)
    return rows


def review_detail(name: str) -> "dict | None":
    """One induced skill in full for the reviewer: the SKILL.md and every bundled file."""
    refresh_skills()
    s = ALL_SKILLS.get(name)
    if s is None or not s.induced:
        return None
    try:
        skill_md = (Path(s.folder) / "SKILL.md").read_text(encoding="utf-8") if s.folder else s.doc
    except (OSError, UnicodeDecodeError):
        skill_md = s.doc
    return {"name": s.name, "status": s.review or "pending", "skill_md": skill_md,
            "files": dict(s.files)}


def _resolve(lib: "dict[str, Skill]", name: str) -> "Skill | None":
    """Look up a skill tolerantly: exact name, else with a legacy ``.py`` suffix stripped (older
    configs / preset prose refer to skills as ``<name>.py``)."""
    hit = lib.get(name)
    if hit is None and name.endswith(".py"):
        hit = lib.get(name[:-3])
    return hit


def get_skill(name: str, skills: "dict[str, Skill] | None" = None) -> "Skill | None":
    """A skill by name, tolerating a legacy ``.py`` suffix (older configs / preset prose refer to a
    skill as ``<name>.py``). ``None`` → the loaded global library. Returns None if unknown."""
    return _resolve(SKILLS if skills is None else skills, name)


def list_skills() -> list[dict]:
    """All atomic skills as plain dicts (name, summary, category) for the console's skill picker.
    Re-scans first, so a folder dropped in since the last call is listed."""
    refresh_skills()
    return [{"name": s.name, "summary": s.summary, "category": s.category, "image": s.image}
            for s in list(SKILLS.values())]


def superseded_names(lib: "dict[str, Skill]") -> "set[str]":
    """Names that some OTHER skill in the library declares itself a better version of. Induction
    versions rather than overwrites, so both live on disk; this is what makes the newer one the
    default without deleting the older one — it stays loadable by name if the newer one misbehaves."""
    return {s.supersedes for s in list(lib.values()) if s.supersedes and s.supersedes in lib}


def skill_manifest(skills: "dict[str, Skill] | None" = None) -> str:
    """The progressive-disclosure MANIFEST for the Scientist's brief: one ``- name — summary`` line
    per skill, bodies and code withheld; skills with a category are grouped under ``[category]``.
    Every skill is listed — there is no cap. ``None`` → the loaded global library. Empty → ''.

    A skill that a NEWER version supersedes is omitted here — the newest version is what the brief
    advertises. The old one is not deleted and ``read_skill_reference`` still resolves it by name."""
    lib = SKILLS if skills is None else skills
    hidden = superseded_names(lib)
    shown = [s for s in list(lib.values()) if s.name not in hidden]

    def line(s: Skill) -> str:
        env = " [own environment: run its commands with run_in_environment]" if s.image else ""
        return f"- {s.name}" + (f" — {s.summary}" if s.summary else "") + env

    out = [line(s) for s in shown if not s.category]
    for category in sorted({s.category for s in shown if s.category}):
        out.append(f"[{category}]")
        out.extend(line(s) for s in shown if s.category == category)
    return "\n".join(out)


# --- retrieval (search_skills) -----------------------------------------------

def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric word tokens (length ≥ 2) — the unit of overlap scoring."""
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) >= 2}


def _score(query_tokens: set[str], skill: Skill) -> float:
    """Relevance of a skill to the query by token overlap, weighted name > summary > doc/code.
    Deterministic and offline — no embedding model needed. 0 = no overlap."""
    if not query_tokens:
        return 0.0
    name = _tokens(skill.name)
    summ = _tokens(skill.summary)
    body = _tokens(skill.doc[:2000]) | _tokens("".join(skill.files.values())[:2000])
    score = 0.0
    for tok in query_tokens:
        if tok in name:
            score += 3.0
        elif tok in summ:
            score += 2.0
        elif tok in body:
            score += 1.0
    return score


def search_skills(query: str, k: int = 5, skills: "dict[str, Skill] | None" = None) -> list[Skill]:
    """The ``k`` atomic skills most relevant to ``query`` by token overlap (name > summary > body).
    Returns only positive matches, best first — empty if nothing overlaps. ``None`` → global library."""
    lib = SKILLS if skills is None else skills
    q = _tokens(query)
    scored = [(s, _score(q, s)) for s in list(lib.values())]
    scored = [(s, sc) for s, sc in scored if sc > 0]
    scored.sort(key=lambda pair: (pair[1], pair[0].name), reverse=True)
    return [s for s, _ in scored[:max(1, k)]]


def plan_skill_lines(query: str, skills: "dict[str, Skill] | None" = None,
                     limit: "int | None" = None) -> "tuple[list[str], int]":
    """The skills the PI sees while drafting a plan, as ``- name — summary`` lines, and how many
    were left out. Superseded versions are hidden, as in :func:`skill_manifest`. By default EVERY
    skill is listed. A caller that passes ``limit`` gets the skills ``query`` (the question plus the
    dataset profile) matches best first and the rest filling the remaining places, so a request the
    keyword match misses, such as one not written in English, still sees most of them.
    ``None`` → the loaded global library."""
    lib = SKILLS if skills is None else skills
    hidden = superseded_names(lib)
    visible = [s for s in list(lib.values()) if s.name not in hidden]
    limit = len(visible) if limit is None else max(0, limit)
    if len(visible) > limit:
        ranked = search_skills(query, k=limit, skills={s.name: s for s in visible})
        ranked_names = {s.name for s in ranked}
        visible = (ranked + [s for s in visible if s.name not in ranked_names])
    chosen = visible[:limit]
    lines = []
    for s in chosen:
        summary = " ".join(s.summary.split())
        if len(summary) > _PLAN_SUMMARY_CHARS:
            summary = summary[:_PLAN_SUMMARY_CHARS].rsplit(" ", 1)[0] + " …"
        lines.append(f"- {s.name}" + (f" — {summary}" if summary else ""))
    return lines, len(visible) - len(chosen)


def skills_named_in(text: str, skills: "dict[str, Skill] | None" = None) -> list[str]:
    """Skill names that ``text`` (a plan step) mentions as whole names, in library order — so
    ``annotate_clusters_by_markers_v2`` does not also count as ``annotate_clusters_by_markers``.
    ``None`` → the loaded global library."""
    lib = SKILLS if skills is None else skills
    low = (text or "").lower()
    return [name for name in list(lib)
            if re.search(rf"(?<![\w-]){re.escape(name.lower())}(?![\w-])", low)]


def make_search_skills_tool(get_skills: "Callable[[], dict[str, Skill]] | None" = None) -> HarnessTool:
    """Scientist tool: find the atomic skills relevant to a capability by keyword, returning only
    their name + one-line summary (NOT the guidance or code) — so a large library never has to be
    listed in full. ``get_skills`` defaults to the loaded global library (override for tests)."""
    _get = get_skills if get_skills is not None else (lambda: SKILLS)

    def _exec(args: dict[str, Any], _ctx: HarnessContext) -> dict[str, Any]:
        lib = _get()
        if not lib:
            return {"results": [], "hint": "no skills are available for this run"}
        query = str(args.get("query", "")).strip()
        try:
            k = int(args.get("k", 5))
        except (TypeError, ValueError):
            k = 5
        hits = search_skills(query, k=k, skills=lib)
        if not hits:
            return {"results": [], "available_count": len(lib),
                    "hint": "no skill matched — try broader capability terms, or a tool may cover this step"}
        return {"results": [{"name": s.name, "summary": s.summary} for s in hits]}

    return HarnessTool(
        "search_skills",
        "Find atomic SKILLS relevant to a capability, by keyword — returns matching skills' name + "
        "summary (not the guidance or code). Use this when the step needs analysis the "
        "purpose-built tools do not cover and the skill list in your brief is long: search, then "
        "`read_skill_reference(name)` to read the best match's guidance.",
        {"type": "object",
         "properties": {
             "query": {"type": "string",
                       "description": "the capability you need, e.g. 'rank perturbations by distance to control'"},
             "k": {"type": "integer", "description": "max results (default 5)"}},
         "required": ["query"]},
        _exec,
        category="codeact",
    )


def make_skill_reference_tool(get_skills: "Callable[[], dict[str, Skill]] | None" = None) -> HarnessTool:
    """The progressive-disclosure fetch, as a Scientist tool. The per-step brief advertises only the
    manifest (name + one-line description). This tool reveals one more level per call:

    - ``read_skill_reference(name)`` → the SKILL.md body (guidance, and for a short skill the code or
      commands themselves) + the list of bundled files, so the agent can confirm the skill fits;
    - ``read_skill_reference(name, file="reference.py")`` → that bundled file, to adapt & run.

    ``get_skills`` defaults to the loaded global library (override for tests)."""
    _get = get_skills if get_skills is not None else (lambda: SKILLS)

    def _exec(args: dict[str, Any], _ctx: HarnessContext) -> dict[str, Any]:
        lib = _get()
        if not lib:
            return {"error": "no skills are available for this run"}
        name = str(args.get("name", "")).strip()
        hit = _resolve(lib, name)
        if hit is None:
            return {"error": f"unknown skill {name!r}", "available": sorted(lib)}
        requested = str(args.get("file", "")).strip()
        if requested:
            code = hit.files.get(requested)
            if code is None:
                return {"error": f"skill {hit.name!r} has no file {requested!r}",
                        "files": sorted(hit.files)}
            return {"name": hit.name, "file": requested, "code": code}
        # No file requested: return the SKILL.md body + the file list (fetch a file next for its code).
        files = sorted(hit.files)
        default = "reference.py" if "reference.py" in files else (files[0] if files else None)
        out = {"name": hit.name, "summary": hit.summary, "doc": hit.doc, "files": files}
        if hit.metadata:
            out["metadata"] = hit.metadata   # e.g. compute, requires-gpu, requires-network, tested-with
        if default is not None:
            out["next"] = (f"call read_skill_reference(name={hit.name!r}, file={default!r}) to get the "
                           "runnable file, then adapt it and run it (Python via run_code; shell "
                           "commands via run_shell, or via subprocess inside run_code)")
        else:
            out["next"] = ("this skill has no bundled files: its code or commands are in `doc` above. "
                           "Adapt them to this dataset, then run Python via run_code and shell "
                           "commands via run_shell (or via subprocess inside run_code)")
        return out

    return HarnessTool(
        "read_skill_reference",
        "Read a named atomic SKILL by progressive disclosure (skills are listed by name + "
        "description in your step brief). Call with just `name` to get the skill's SKILL.md — its "
        "guidance, which for a short skill also holds the code or shell commands — and the list of "
        "its bundled files; then call again with `file` (e.g. \"reference.py\") to get one of those "
        "files to adapt and run. Use this ONLY when THIS step needs analysis the purpose-built tools "
        "do not cover — if a tool covers the step, use the tool instead.",
        {"type": "object",
         "properties": {
             "name": {"type": "string", "description": "skill name from the brief's manifest"},
             "file": {"type": "string",
                      "description": "optional bundled file to fetch the code of, e.g. 'reference.py'"}},
         "required": ["name"]},
        _exec,
        category="codeact",
    )
