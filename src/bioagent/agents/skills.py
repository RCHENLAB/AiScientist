"""Atomic skill library — small, model-rewritable code capabilities surfaced on demand.

A *skill* is a folder ``skills/<name>/`` (repo root) in the Anthropic Agent-Skills shape:

- ``SKILL.md`` — YAML-ish frontmatter (``name`` + one-line ``description``) plus a markdown body
  (``## When to use`` / ``## Details & adaptation`` / ``## Run``). The description is the manifest
  label; the body is the human-readable *when-to-use + how-to-adapt* guidance.
- ``reference.py`` (and any other bundled file) — the adaptable CodeAct *demonstration* the
  Scientist fetches, rewrites, and runs via ``run_code``.

It sits between the two other layers (see ``docs/skills_and_pipelines_architecture.md``):

- unlike the fixed **registry** tools (``agents/registry.py``), a skill is meant to be READ,
  ADAPTED, and run via ``run_code`` — and grown by induction;
- unlike a **preset pipeline** (``agents/preset_pipelines.py``), a skill is atomic and composable,
  not a whole end-to-end workflow.

**Progressive disclosure — three levels, so a template only costs context when it is used:**

1. the Scientist's per-step brief lists only the skill MANIFEST (``name`` + one-line description);
2. ``read_skill_reference(name)`` returns the SKILL.md *guidance* (when-to-use / how-to-adapt) plus
   the list of bundled files — NOT the code;
3. ``read_skill_reference(name, file="reference.py")`` returns one bundled file's *code* on demand.

The registry stays the small always-on core; this library can grow without bloating every brief. As
it grows past ``MANIFEST_MAX``, the brief points the agent at ``search_skills(query)`` so even the
name+description list stays small.

Override the location with ``$BIOAGENT_SKILLS_DIR`` (repo-root ``skills/`` by default).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .research_harness import HarnessContext, HarnessTool

# Above this many skills, the Scientist's brief stops dumping the full manifest and points the agent
# at ``search_skills(query)`` instead — so even the name+description list can't bloat every step.
MANIFEST_MAX = int(os.environ.get("BIOAGENT_SKILL_MANIFEST_MAX", "12"))


@dataclass(frozen=True)
class Skill:
    """One atomic, adaptable capability. ``summary`` (SKILL.md ``description``) is what the manifest
    advertises; ``doc`` (the SKILL.md body — when-to-use / how-to-adapt) is fetched on demand; the
    ``files`` (``reference.py`` + any bundle) hold the code, fetched one level deeper still — so a
    large template only enters context when a step actually uses it."""
    name: str                                  # folder / frontmatter name, e.g. "perturbation_edistance"
    summary: str = ""                          # frontmatter description — the one-line manifest label
    doc: str = ""                              # SKILL.md body: when-to-use + how-to-adapt guidance
    files: "dict[str, str]" = field(default_factory=dict)  # bundled file name -> source (reference.py, ...)
    induced: bool = False                      # machine-written (skill_induction) rather than curated
    supersedes: str = ""                       # an older skill this one is a better version of


def _skills_dir() -> Path:
    """The atomic-skill library: ``$BIOAGENT_SKILLS_DIR`` or repo-root ``skills/``."""
    env = os.environ.get("BIOAGENT_SKILLS_DIR")
    if env:
        return Path(env)
    # skills.py -> agents -> bioagent -> src -> <repo root>
    return Path(__file__).resolve().parents[3] / "skills"


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into (frontmatter dict, body). Frontmatter is the leading ``---``-delimited
    block of simple ``key: value`` lines; body is the rest. (Same convention as preset pipelines.)"""
    meta: dict[str, str] = {}
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
                meta[key] = value
    return meta, body.strip()


def _first_line(text: str) -> str:
    """First non-empty line of ``text`` (the manifest-label fallback when no frontmatter description)."""
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")[:200]


def _induced_dir() -> "Path | None":
    """The INDUCED-skill root (``$BIOAGENT_INDUCED_SKILLS_DIR``), or None when induction is not
    configured. Kept separate from the curated library on purpose — see ``skill_induction.py``:
    machine-written templates live in the gateway workspace, never in the git-tracked ``skills/``."""
    env = os.environ.get("BIOAGENT_INDUCED_SKILLS_DIR")
    return Path(env) if env else None


def _load_skills() -> dict[str, Skill]:
    """Load every ``skills/<name>/SKILL.md`` into the library (sorted by name; skips ``_``-prefixed
    folders and folders without a readable SKILL.md). The skill name is its frontmatter ``name`` or,
    failing that, the folder name; bundled files are every non-SKILL.md file in the folder."""
    out = _load_from(_skills_dir())
    induced = _induced_dir()
    if induced is not None:
        # Curated wins: an induced skill can never shadow a hand-authored one of the same name.
        for name, skill in _load_from(induced).items():
            out.setdefault(name, skill)
    return out


def _load_from(root: Path) -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    if not root.is_dir():
        return out
    for skill_md in sorted(root.glob("*/SKILL.md")):
        folder = skill_md.parent
        if folder.name.startswith("_"):
            continue
        try:
            meta, body = _parse_front_matter(skill_md.read_text(encoding="utf-8"))
        except OSError:
            continue
        name = meta.get("name") or folder.name
        summary = meta.get("description") or _first_line(body)
        files: dict[str, str] = {}
        for f in sorted(folder.iterdir()):
            if f.name == "SKILL.md" or not f.is_file():
                continue
            try:
                files[f.name] = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        if name and (body or files):
            out[name] = Skill(name=name, summary=summary, doc=body, files=files,
                              induced=str(meta.get("induced", "")).strip().lower() in ("1", "true", "yes"),
                              supersedes=meta.get("supersedes", "").strip())
    return out


SKILLS: dict[str, Skill] = _load_skills()


def register_skill(skill: Skill) -> bool:
    """Add a newly INDUCED skill to the in-process library so the NEXT run in this process can find
    it, without a restart. Additive only — an existing name (curated or already induced) is never
    replaced, so a concurrent run reading the manifest can never see a skill change under it, only
    a new one appear. Returns True if it was added."""
    if not skill.name or skill.name in SKILLS:
        return False
    SKILLS[skill.name] = skill
    return True


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
    """All atomic skills as plain dicts (name + one-line summary) for the console's skill picker."""
    return [{"name": s.name, "summary": s.summary} for s in SKILLS.values()]


def superseded_names(lib: "dict[str, Skill]") -> "set[str]":
    """Names that some OTHER skill in the library declares itself a better version of. Induction
    versions rather than overwrites, so both live on disk; this is what makes the newer one the
    default without deleting the older one — it stays loadable by name if the newer one misbehaves."""
    return {s.supersedes for s in lib.values() if s.supersedes and s.supersedes in lib}


def skill_manifest(skills: "dict[str, Skill] | None" = None) -> str:
    """The progressive-disclosure MANIFEST for the Scientist's brief: one ``- name — summary`` line
    per skill, bodies and code withheld. ``None`` → the loaded global library. Empty library → ''.

    A skill that a NEWER version supersedes is omitted here — the newest version is what the brief
    advertises. The old one is not deleted and ``read_skill_reference`` still resolves it by name."""
    lib = SKILLS if skills is None else skills
    hidden = superseded_names(lib)
    return "\n".join(
        f"- {s.name}" + (f" — {s.summary}" if s.summary else "")
        for s in lib.values() if s.name not in hidden
    )


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
    scored = [(s, _score(q, s)) for s in lib.values()]
    scored = [(s, sc) for s, sc in scored if sc > 0]
    scored.sort(key=lambda pair: (pair[1], pair[0].name), reverse=True)
    return [s for s, _ in scored[:max(1, k)]]


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
        "one-line summary (not the guidance or code). Use this when the step needs analysis the "
        "purpose-built tools do not cover and the skill list is too long to scan: search, then "
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

    - ``read_skill_reference(name)`` → the SKILL.md GUIDANCE (when-to-use / how-to-adapt) + the list
      of bundled files, so the agent can confirm the skill fits before paying for the code;
    - ``read_skill_reference(name, file="reference.py")`` → that bundled file's CODE, to adapt & run.

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
        # No file requested: return the guidance + the file list (fetch a file next to get the code).
        files = sorted(hit.files)
        default = "reference.py" if "reference.py" in files else (files[0] if files else None)
        out = {"name": hit.name, "summary": hit.summary, "doc": hit.doc, "files": files}
        if default is not None:
            out["next"] = (f"call read_skill_reference(name={hit.name!r}, file={default!r}) to get the "
                           "runnable template, then adapt it and run via run_code")
        return out

    return HarnessTool(
        "read_skill_reference",
        "Read a named atomic SKILL by progressive disclosure (skills are listed by name + one-line "
        "description in your step brief). Call with just `name` to get the skill's GUIDANCE "
        "(when-to-use + how-to-adapt) and its file list; then call again with `file` (e.g. "
        "\"reference.py\") to get that file's CODE to adapt and run via run_code. Use this ONLY when "
        "THIS step needs analysis the purpose-built tools do not cover — if a tool covers the step, "
        "use the tool instead.",
        {"type": "object",
         "properties": {
             "name": {"type": "string", "description": "skill name from the brief's manifest"},
             "file": {"type": "string",
                      "description": "optional bundled file to fetch the code of, e.g. 'reference.py'"}},
         "required": ["name"]},
        _exec,
        category="codeact",
    )
