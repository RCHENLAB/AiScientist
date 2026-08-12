"""Skill induction — turn a procedure a run actually executed into a reusable atomic skill.

``agents/skills.py`` has claimed since it was written that the library is "grown by induction", but
nothing ever wrote a skill: every skill in ``skills/`` was hand-authored. This module is that
missing half. When a step solved something with ``run_code`` that no curated tool covered, and the
Critic accepted the result, the concrete snippet is generalized into a template
(``SKILL.md`` + ``reference.py``) that later runs can find, read, adapt, and run.

WHERE INDUCED SKILLS GO, AND WHY IT IS NOT ``skills/``
-----------------------------------------------------
Induced skills are written to a SEPARATE root (``BIOAGENT_INDUCED_SKILLS_DIR``, in practice under
the gateway workspace) — never into the repo's git-tracked ``skills/``. A model silently editing
source that ships to every user is a different and much worse thing than a model leaving a template
in its own workspace. A repo skill of the same name always wins, so an induced skill can never
shadow a curated one; the frontmatter carries ``induced: true`` and the originating step, so a
human reading the library can always tell which is which and promote (or delete) it deliberately.

WHAT INDUCTION DOES AND DOES NOT ADD
------------------------------------
The code in an induced skill is code the Scientist already wrote and already executed in that run.
Induction REMEMBERS it; it grants no execution capability that did not exist. What it changes is
that the next run does not have to re-derive it. That is also why every guard here is
deterministic — ``_validate`` rejects a bad name, uncompilable code, an oversized body, or a
collision, regardless of what the model claims about its own output.

Pure functions + one filesystem write, LLM injected as ``complete_fn`` — so the whole thing is
offline-testable without a GPU or a real run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .loop_utils import safe_json_loads

#: Skill names must be a plain snake_case identifier: it becomes a DIRECTORY NAME, so anything
#: exotic is a path-traversal or clobber risk, not merely untidy.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
#: A template far larger than this is not a reusable skill, it is the whole analysis pasted in.
MAX_CODE_CHARS = 12_000
MIN_CODE_CHARS = 120

INDUCE_SYSTEM = (
    "You are a research engineer curating a lab's library of reusable analysis templates. A step in "
    "a just-finished run solved something by writing and running a custom Python snippet, and the "
    "reviewer accepted the result. Decide whether that snippet is worth KEEPING as a reusable "
    "template, and if so, generalize it into one.\n"
    "KEEP IT ONLY IF it is a genuinely reusable PROCEDURE that would apply to a different dataset "
    "and a different run: a named analysis or transformation with a clear input and output. DO NOT "
    "keep: one-off inspection or debugging, printing a few numbers, a snippet that only re-does "
    "what an existing curated tool or an existing skill already does (both lists are given — check "
    "them), anything that hard-codes THIS dataset's file paths, sample names, or cluster numbers as "
    "its whole purpose, or anything that writes/renders/packages a report. The default answer is "
    "NO — a library full of near-duplicate one-offs is worse than a small curated one.\n"
    "If you keep it, GENERALIZE: lift every dataset-specific value (paths, column names, gene "
    "lists, thresholds, group names) into a clearly-marked CONFIG block of named constants at the "
    "TOP of the template, so the next run adapts those and nothing else. Keep reading inputs from "
    "the environment variables the snippet used (BIOAGENT_DATASET / BIOAGENT_WORK / "
    "BIOAGENT_ARTIFACTS) rather than absolute paths. The template must be complete, runnable "
    "Python, and must not import anything the original snippet did not.\n"
    "Reply with ONLY a JSON object: "
    '{"keep": true|false, "reason": "<one sentence>", "name": "<snake_case, 3-40 chars, describes '
    'the PROCEDURE not this dataset>", "description": "<one line for the library index — what it '
    'does and when it applies>", "when_to_use": "<2-4 sentences: the situation it fits, and what '
    'to adapt>", "code": "<the generalized template>", '
    '"supersedes": "<the name of an EXISTING skill this is a better version of, or empty>"}. '
    'With "keep": false, the other fields may be empty.\n'
    'Use "supersedes" ONLY when the existing skill covers the SAME procedure and this run showed a '
    'genuinely better way to do it — a bug it hits, a step it misses, a materially cleaner method. '
    'Do NOT use it to re-file a skill that already works: an unchanged re-statement is not an '
    'improvement, and the old version is not replaced, it is kept alongside.'
)


@dataclass(frozen=True)
class InducedSkill:
    """A validated, ready-to-write skill. Constructed only via :func:`induce`."""

    name: str
    description: str
    when_to_use: str
    code: str
    origin_step: str = ""
    reason: str = ""
    # An existing skill this is a better version of ('' = brand new). VERSIONING, never overwrite:
    # the superseded skill stays on disk and stays loadable, so a worse "improvement" is always
    # recoverable. Only the manifest prefers the newer one.
    supersedes: str = ""

    def skill_md(self, *, origin_run: str = "") -> str:
        """The SKILL.md, in the same shape ``skills.py`` parses for hand-authored skills, plus the
        provenance frontmatter that marks it as machine-written."""
        origin = f"origin_run: {origin_run}\n" if origin_run else ""
        step = f"origin_step: {self.origin_step}\n" if self.origin_step else ""
        sup = f"supersedes: {self.supersedes}\n" if self.supersedes else ""
        return (
            "---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            "induced: true\n"
            f"{origin}{step}{sup}"
            "---\n\n"
            "## When to use\n\n"
            f"{self.when_to_use}\n\n"
            + (f"> Supersedes `{self.supersedes}`, which is kept alongside this one rather than "
               "overwritten — if this version misbehaves, the older one is still there.\n\n"
               if self.supersedes else "")
            +
            "> Induced automatically from a step that ran successfully in an earlier study, not "
            "hand-curated. Treat the template as a starting point and check it against your data "
            "before trusting the result.\n\n"
            "## Run\n"
            f'Fetch the template with `read_skill_reference("{self.name}", file="reference.py")`, '
            "adapt the CONFIG values at the top to THIS dataset, then execute it via `run_code` "
            "(reads checkpoints from `BIOAGENT_WORK`, writes under `BIOAGENT_ARTIFACTS`). If a "
            "purpose-built tool already covers the step, use the tool instead.\n"
        )


def candidates(rounds: "list[Any]") -> list[dict[str, Any]]:
    """The inducible steps of a finished run: every ACCEPTED round's successful ``run_code`` call,
    newest last. Returns ``[{"step", "code", "summary"}]``. A step whose code is trivially short is
    dropped here rather than spending an LLM call on it."""
    out: list[dict[str, Any]] = []
    for r in rounds:
        verdict = getattr(r, "verdict", None)
        if getattr(verdict, "verdict", "") != "accept":
            continue
        for st in (getattr(r, "scientist_result", {}) or {}).get("steps", []):
            if st.get("tool") != "run_code" or not st.get("ok"):
                continue
            code = str((st.get("args") or {}).get("code") or "")
            if len(code) < MIN_CODE_CHARS:
                continue
            out.append({"step": getattr(r, "step", ""), "code": code,
                        "summary": str(st.get("summary") or "")[:400]})
    return out


def _validate(obj: Any, taken: "set[str]") -> "tuple[InducedSkill | None, str]":
    """Turn a model reply into an :class:`InducedSkill`, or explain why not. Every check here is
    deterministic and independent of what the model asserts about its own output."""
    if not isinstance(obj, dict):
        return None, "unparseable reply"
    if not bool(obj.get("keep", False)):
        return None, str(obj.get("reason", "")) or "declined"
    name = str(obj.get("name", "")).strip().lower()
    if not _NAME_RE.match(name):
        return None, f"invalid skill name {name!r}"
    supersedes = str(obj.get("supersedes", "")).strip().lower()
    if supersedes and supersedes not in taken:
        return None, f"claims to supersede unknown skill {supersedes!r}"
    # A name clash is normally a re-proposal of something we already have — refuse it. It is only
    # legitimate when the model explicitly declared this an improvement of that same skill, which
    # is what turns "overwrite" into "write the next version alongside".
    if name in taken and supersedes != name:
        return None, f"name {name!r} already exists"
    code = str(obj.get("code", "")).strip()
    if len(code) < MIN_CODE_CHARS:
        return None, "template too short to be reusable"
    if len(code) > MAX_CODE_CHARS:
        return None, f"template too large ({len(code)} chars)"
    try:
        compile(code, f"<induced:{name}>", "exec")
    except SyntaxError as exc:
        return None, f"template does not compile: {exc}"
    description = str(obj.get("description", "")).strip()
    if not description:
        return None, "no description for the library index"
    return InducedSkill(
        name=name, description=description[:200],
        when_to_use=str(obj.get("when_to_use", "")).strip() or description,
        code=code, reason=str(obj.get("reason", "")).strip(), supersedes=supersedes,
    ), ""


def induce(cands: "list[dict[str, Any]]", complete_fn: "Callable[[list[dict[str, Any]]], str]", *,
           existing_manifest: str, tool_names: str, taken: "set[str]",
           max_new: int = 2) -> "tuple[list[InducedSkill], list[str]]":
    """Ask the model to generalize each candidate, keeping the ones that pass :func:`_validate`.

    Returns ``(kept, rejections)`` — the rejection strings are for the event log, so a run can show
    *why* nothing was learned instead of silently learning nothing. Never raises: an LLM or parse
    failure is recorded as a rejection.
    """
    kept: list[InducedSkill] = []
    rejected: list[str] = []
    claimed = set(taken)
    for cand in cands:
        if len(kept) >= max_new:
            break
        payload = {
            "step_it_performed": cand["step"],
            "code_that_ran": cand["code"][:MAX_CODE_CHARS],
            "result_summary": cand["summary"],
            "existing_skills": existing_manifest or "(none)",
            "curated_tools_that_already_exist": tool_names,
        }
        try:
            raw = complete_fn([
                {"role": "system", "content": INDUCE_SYSTEM},
                {"role": "user", "content": json.dumps(payload)},
            ])
        except Exception as exc:  # noqa: BLE001 - induction is best-effort, never fatal
            rejected.append(f"{type(exc).__name__}: {exc}")
            continue
        skill, why = _validate(safe_json_loads(raw), claimed)
        if skill is None:
            rejected.append(why)
            continue
        skill = InducedSkill(skill.name, skill.description, skill.when_to_use, skill.code,
                             origin_step=str(cand["step"])[:200], reason=skill.reason,
                             supersedes=skill.supersedes)
        claimed.add(skill.name)
        kept.append(skill)
    return kept, rejected


#: Version suffixes tried when a skill supersedes an existing one. A cap, because a model that
#: "improves" the same skill every run would otherwise grow an unbounded chain.
MAX_VERSIONS = 9


def write_skill(root: "str | Path", skill: InducedSkill, *,
                origin_run: str = "") -> "tuple[Path, str] | None":
    """Write ``<root>/<name>/{SKILL.md,reference.py}``. Returns ``(folder, written_name)``, or
    ``None`` on any filesystem error (induction must never break a run).

    NEVER OVERWRITES. A brand-new skill whose folder already exists is abandoned — that folder is
    someone else's. A skill that declares ``supersedes`` is written as the next free ``<name>_vN``
    instead, so the improvement lands *alongside* the version it replaces rather than on top of it.
    That is the whole point: a model's "better version" that turns out to be worse costs you one
    unused directory, not a working template that silently stopped working.
    """
    if not _NAME_RE.match(skill.name):     # belt-and-braces: never build a path from a bad name
        return None
    base = Path(root)
    try:
        target, name = base / skill.name, skill.name
        if target.exists():
            if not skill.supersedes:
                return None
            for v in range(2, MAX_VERSIONS + 1):
                name = f"{skill.name}_v{v}"
                if not _NAME_RE.match(name):
                    return None
                target = base / name
                if not target.exists():
                    break
            else:
                return None                # the version chain is full; stop rather than churn
        target.mkdir(parents=True)
        versioned = replace(skill, name=name) if name != skill.name else skill
        (target / "SKILL.md").write_text(versioned.skill_md(origin_run=origin_run), encoding="utf-8")
        (target / "reference.py").write_text(versioned.code, encoding="utf-8")
    except OSError:
        return None
    return target, name
