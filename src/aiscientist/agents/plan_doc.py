"""The plan as a document: one YAML file the researcher reads, edits and approves, and that the run
is then held to.

A plan has been ``agenda: list[str]`` — prose steps. Prose is the right thing to READ, but two facts
cannot be stated in it: which of a step's numbers are its tool's arguments, and what exactly was
approved. So nothing bound the tool to the number on the plan card ("resolution 1.0" approved,
0.5 run), and nothing could compare a finished run with the plan it was approved as.

This module adds the written form that carries both, without replacing the agenda: every step keeps
its prose (``does``) and gains the structured facts beside it::

    plan: 1
    request: Compare DDX41 with WT in the rods
    steps:
      - id: s1
        title: Clustering & UMAP
        tool: run_clustering
        settings:
          resolution: 1.0
        does: >-
          Group the cells into transcriptionally distinct populations with `run_clustering` ...

``skill`` names the vetted skill a code step uses (from the pinned registry release): matched by
AiScientist when the plan is drafted, shown on the card, removable, and handed to the Scientist
with its guidance and code when the step runs.

``settings`` are the tool arguments the plan fixes. They are passed to the tool as written
(``pinned`` in the harness), so an approved number is the number that runs; ``check_adherence``
reads a finished run back against the approved document.

Pure data and helpers — no model call, no I/O — like :mod:`aiscientist.agents.dag`.
"""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

PLAN_VERSION = 1

#: Tools that run code the model writes. A plan step that names one has no fixed operation to
#: describe, so the plan says so instead of naming a pipeline tool.
CODE_TOOLS = frozenset({"run_code", "run_shell", "run_in_environment"})

_HEADER = (
    "# AiScientist plan. Run it as it is, or edit it and choose \"Apply edits\".\n"
    "# `settings` are passed to the step's tool exactly as written here.\n"
    "# `skill` is the vetted skill a step uses; delete the line to let the step go without it.\n"
    "# `request` and `runs_on` are filled in by AiScientist; changes to them are ignored.\n"
)


class PlanError(ValueError):
    """A plan document that cannot be used. ``problems`` holds one sentence per defect."""

    def __init__(self, problems: "Iterable[str]") -> None:
        self.problems = [str(p) for p in problems if str(p).strip()]
        super().__init__("; ".join(self.problems) or "invalid plan")


# --- one step ------------------------------------------------------------------------------------

_TITLED_RE = re.compile(r"^\*\*(.+?)\*\*\s*[—–:-]+\s*(.*)$", re.S)
_TITLE_ONLY_RE = re.compile(r"^\*\*(.+?)\*\*\s*$", re.S)


def split_step(text: str) -> "tuple[str, str]":
    """``(title, prose)`` of an agenda step written ``**Title** — prose``; ``("", text)`` otherwise."""
    s = (text or "").strip()
    m = _TITLED_RE.match(s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = _TITLE_ONLY_RE.match(s)
    if m:
        return m.group(1).strip(), ""
    return "", s


def join_step(title: str, does: str) -> str:
    """The agenda step for a title and its prose — the inverse of :func:`split_step`."""
    title, does = (title or "").strip(), (does or "").strip()
    if title and does:
        return f"**{title}** — {does}"
    return f"**{title}**" if title else does


def _flat(text: str) -> str:
    return " ".join((text or "").split())


@dataclass(frozen=True)
class PlanStep:
    """One step: the agenda text the Scientist is handed, and the structured facts beside it."""

    id: str
    text: str
    tool: "str | None" = None
    settings: "dict[str, Any]" = field(default_factory=dict)
    runs_on: str = ""
    skill: "str | None" = None

    @property
    def title(self) -> str:
        return split_step(self.text)[0]

    @property
    def does(self) -> str:
        return split_step(self.text)[1]

    def to_public(self) -> "dict[str, Any]":
        out: dict[str, Any] = {"id": self.id, "title": self.title, "does": self.does}
        if self.tool:
            out["tool"] = self.tool
        if self.settings:
            out["settings"] = dict(self.settings)
        if self.runs_on:
            out["runs_on"] = self.runs_on
        if self.skill:
            out["skill"] = self.skill
        return out


@dataclass(frozen=True)
class PlanDoc:
    request: str
    steps: "tuple[PlanStep, ...]"

    def agenda(self) -> "list[str]":
        return [s.text for s in self.steps]

    def step_for(self, text: str) -> "PlanStep | None":
        """The step whose agenda text is ``text`` (the loop identifies a step by its text)."""
        for s in self.steps:
            if s.text == text:
                return s
        return None

    def to_public(self) -> "list[dict[str, Any]]":
        return [s.to_public() for s in self.steps]

    def to_yaml(self) -> str:
        return dump_plan_yaml(self)


# --- YAML: writing ---------------------------------------------------------------------------------

def _loader() -> Any:
    """A safe loader that reads only ``true``/``false`` as booleans and no dates.

    YAML 1.1 turns ``no``, ``on`` and ``off`` into booleans and ``2026-10-12`` into a date. A column
    named ``NO`` or a group called ``off`` is a string here, and must still be one after an edit."""
    import yaml  # lazy: the core stays importable without PyYAML

    class _PlanLoader(yaml.SafeLoader):
        pass

    _PlanLoader.yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers
                if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp")]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    _PlanLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
    return _PlanLoader


def _load(text: str) -> Any:
    import yaml

    return yaml.load(text, Loader=_loader())  # noqa: S506 - a SafeLoader subclass


#: Words another YAML reader may take for a boolean or null; quoted so every reader sees a string.
_AMBIGUOUS_WORDS = frozenset({"y", "n", "yes", "no", "on", "off", "true", "false", "null", "~", ""})


def _plain_ok(s: str, *, in_list: bool = False) -> bool:
    """Whether ``s`` can be written unquoted and read back as the same string."""
    if s != s.strip() or "\n" in s or s.lower() in _AMBIGUOUS_WORDS:
        return False
    if in_list and any(c in s for c in ",[]{}"):
        return False
    try:
        probe = f"[{s}]" if in_list else f"k: {s}"
        back = _load(probe)
    except Exception:  # noqa: BLE001 - PyYAML missing or the probe does not parse: quote it
        return False
    return back == ([s] if in_list else {"k": s})


def _float(value: float) -> str:
    """A float as YAML 1.1 reads one: ``1e-05`` is a STRING to PyYAML, ``1.0e-05`` is a number."""
    if value != value or value in (float("inf"), float("-inf")):
        return json.dumps(str(value))
    text = repr(value)
    if "e" in text:
        mantissa, exponent = text.split("e")
        mantissa = mantissa if "." in mantissa else mantissa + ".0"
        exponent = exponent if exponent[0] in "+-" else "+" + exponent
        text = f"{mantissa}e{exponent}"
    return text


def _scalar(value: Any, *, in_list: bool = False) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return repr(value)
    if isinstance(value, float):
        return _float(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(v, in_list=True) for v in value) + "]"
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, default=str)   # JSON is valid YAML flow syntax
    s = str(value)
    return s if _plain_ok(s, in_list=in_list) else json.dumps(s, ensure_ascii=False)


def _block(text: str, indent: str) -> str:
    """Prose as a block scalar: folded and wrapped for one paragraph, literal when it has lines."""
    text = (text or "").strip()
    if not text:
        return '""'
    if "\n" in text:
        return "|-\n" + "\n".join((indent + line.rstrip()) if line.strip() else ""
                                  for line in text.split("\n"))
    lines = textwrap.wrap(_flat(text), width=88, break_long_words=False, break_on_hyphens=False)
    return ">-\n" + "\n".join(indent + line for line in lines)


def dump_plan_yaml(doc: PlanDoc) -> str:
    """The document as YAML text. Written by hand so the prose stays readable (a wrapped block)
    and the field order is the reading order; :func:`parse_plan_yaml` reads it back."""
    out = [_HEADER + f"plan: {PLAN_VERSION}", f"request: {_scalar(_flat(doc.request))}", "steps:"]
    if not doc.steps:
        out[-1] = "steps: []"
    for step in doc.steps:
        out.append(f"  - id: {_scalar(step.id)}")
        out.append(f"    title: {_scalar(step.title)}")
        if step.tool:
            out.append(f"    tool: {_scalar(step.tool)}")
        if step.runs_on:
            out.append(f"    runs_on: {_scalar(step.runs_on)}")
        if step.skill:
            out.append(f"    skill: {_scalar(step.skill)}")
        if step.settings:
            out.append("    settings:")
            out.extend(f"      {_scalar(str(k))}: {_scalar(v)}" for k, v in step.settings.items())
        out.append("    does: " + _block(step.does, "      "))
    return "\n".join(out) + "\n"


# --- YAML: reading ---------------------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
_SETTING_VALUE_TYPES = (str, int, float, bool, type(None))


def _where(exc: Exception) -> str:
    mark = getattr(exc, "problem_mark", None)
    return f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""


def parse_plan_yaml(text: str, *, request: str = "") -> PlanDoc:
    """Read a plan document a person edited. Raises :class:`PlanError` listing every defect found,
    so one round trip tells the editor everything that is wrong.

    Lenient where nothing is lost: a missing ``id`` is assigned, ``text`` is accepted for ``does``,
    and the fields AiScientist fills in itself (``request``, ``runs_on``) are ignored on the way in.
    Strict where a guess would run something the editor did not write: no step list, a step with no
    text, a duplicate id, or settings that are not a ``name: value`` mapping are errors."""
    try:
        data = _load(text or "")
    except ImportError as exc:
        raise PlanError(["This server cannot read YAML (PyYAML is not installed)."]) from exc
    except Exception as exc:  # noqa: BLE001 - yaml.YAMLError; the message goes back to the editor
        problem = getattr(exc, "problem", None) or str(exc).split("\n", 1)[0]
        raise PlanError([f"The YAML does not parse: {problem}{_where(exc)}."]) from exc
    if not isinstance(data, Mapping):
        raise PlanError(["The plan must be a mapping with a `steps:` list."])
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanError(["The plan has no steps. To run nothing, cancel the plan instead."])

    problems: list[str] = []
    seen: set[str] = set()
    parsed: list[dict[str, Any]] = []
    for n, raw in enumerate(raw_steps, 1):
        if isinstance(raw, str):
            raw = {"does": raw}
        if not isinstance(raw, Mapping):
            problems.append(f"Step {n} must be a mapping (id, title, tool, settings, does).")
            continue
        title = _flat(str(raw.get("title") or ""))
        does = raw.get("does", raw.get("text", raw.get("goal", "")))
        does = "" if does is None else str(does).strip()
        if "\n" not in does:
            does = _flat(does)
        if not title and not does:
            problems.append(f"Step {n} has neither a title nor a `does` text.")
            continue
        sid = str(raw.get("id") or "").strip()
        if sid and not _ID_RE.match(sid):
            problems.append(f"Step {n}: id {sid!r} must be letters, digits, `_` or `-`.")
            sid = ""
        if sid in seen:
            problems.append(f"Step {n}: id {sid!r} is used by an earlier step.")
            sid = ""
        if sid:
            seen.add(sid)
        tool = raw.get("tool")
        tool = str(tool).strip() if tool not in (None, "") else None
        skill = raw.get("skill")
        skill = str(skill).strip() if skill not in (None, "", False) else None
        settings_raw = raw.get("settings")
        settings: dict[str, Any] = {}
        if settings_raw not in (None, {}, []):
            if not isinstance(settings_raw, Mapping):
                problems.append(f"Step {n}: `settings` must be `name: value` lines.")
            else:
                for key, value in settings_raw.items():
                    ok = isinstance(value, _SETTING_VALUE_TYPES) or (
                        isinstance(value, list)
                        and all(isinstance(v, _SETTING_VALUE_TYPES) for v in value))
                    if not ok:
                        problems.append(f"Step {n}: setting `{key}` must be a value or a list of values.")
                    else:
                        settings[str(key)] = value
        if settings and not tool:
            problems.append(f"Step {n} has settings but names no `tool` to pass them to.")
        parsed.append({"id": sid, "title": title, "does": does, "tool": tool, "settings": settings,
                       "skill": skill})
    if problems:
        raise PlanError(problems)

    steps: list[PlanStep] = []
    for item in parsed:
        sid = item["id"] or _fresh_id(seen)
        seen.add(sid)
        steps.append(PlanStep(id=sid, text=join_step(item["title"], item["does"]),
                              tool=item["tool"], settings=item["settings"], skill=item["skill"]))
    return PlanDoc(request=request, steps=tuple(steps))


def _fresh_id(used: "set[str]") -> str:
    i = 1
    while f"s{i}" in used:
        i += 1
    return f"s{i}"


# --- from an agenda ----------------------------------------------------------------------------------

def sync_plan(prev: "PlanDoc | None", agenda: "list[str]", *, request: str,
              tool_of: "Callable[[str], str | None]",
              runs_on_of: "Callable[[str], str] | None" = None) -> "tuple[PlanDoc, list[int]]":
    """The document for ``agenda``, carrying over what ``prev`` already knew.

    A step whose text is unchanged keeps its id AND its settings — including settings the researcher
    typed — so revising one step never resets the others. A step whose text is new is returned in
    ``fresh`` (0-based positions) with no settings yet: the caller binds those. A rewritten step
    keeps the id of the step it replaced when it sits in the same position."""
    prev_steps = list(prev.steps) if prev is not None else []
    by_text: dict[str, list[PlanStep]] = {}
    for s in prev_steps:
        by_text.setdefault(s.text, []).append(s)
    used = {s.id for s in prev_steps}
    kept_ids: set[str] = set()
    slots: "list[PlanStep | None]" = []
    for text in agenda:
        same = by_text.get(text) or []
        step = same.pop(0) if same else None
        if step is not None:
            kept_ids.add(step.id)
        slots.append(step)
    steps: list[PlanStep] = []
    fresh: list[int] = []
    for i, (text, step) in enumerate(zip(agenda, slots)):
        if step is None:
            inherited = prev_steps[i].id if i < len(prev_steps) and prev_steps[i].id not in kept_ids else ""
            sid = inherited or _fresh_id(used)
            used.add(sid)
            kept_ids.add(sid)
            step = PlanStep(id=sid, text=text, tool=tool_of(text))
            fresh.append(i)
        if runs_on_of is not None and step.tool:
            step = PlanStep(id=step.id, text=step.text, tool=step.tool,
                            settings=step.settings, runs_on=runs_on_of(step.tool) or "",
                            skill=step.skill)
        steps.append(step)
    return PlanDoc(request=request, steps=tuple(steps)), fresh


# --- settings: what a step's text states, as the tool's own arguments ----------------------------------

_NUMBER_RE = re.compile(
    r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\w)|(?<![\w.])\d+(?:\.\d+)?(?!\w|\.\d)")
_CALL_RE = r"\b{name}\s*\(([^)]*)\)"
_KWARG_RE = re.compile(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([A-Za-z0-9_.\-]+))""")


def _number_spans(text: str) -> "list[tuple[int, int, float]]":
    """``(start, end, value)`` of every number the prose states.

    A leading ``-`` is a sign only where it cannot be a hyphen: "log2FC below -1" is minus one,
    "the top-50 markers" and "steps 1-2" are fifty and two."""
    out: list[tuple[int, int, float]] = []
    for m in _NUMBER_RE.finditer(text or ""):
        try:
            value = float(m.group(0).replace(",", ""))
        except ValueError:
            continue
        start = m.start()
        if start >= 1 and text[start - 1] in "-\u2212" and (
                start == 1 or not (text[start - 2].isalnum() or text[start - 2] in ")]`")):
            start, value = start - 1, -value
        out.append((start, m.end(), value))
    return out


def _numbers_in(text: str) -> "list[float]":
    return [value for _start, _end, value in _number_spans(text)]


def _states_number(text: str, value: float) -> bool:
    """Whether the prose states ``value`` — as written, or as a percentage of it (0.05 ↔ "5%")."""
    numbers = _numbers_in(text)
    return any(abs(n - value) < 1e-9 or abs(n - value * 100) < 1e-9 for n in numbers)


def _coerce(value: Any, spec: "Mapping[str, Any]") -> "tuple[Any, bool]":
    """``(value in the schema's declared type, ok)``."""
    kind = spec.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    try:
        if kind == "integer":
            if isinstance(value, bool):
                return value, False
            f = float(value)
            return (int(f), True) if f == int(f) else (value, False)
        if kind == "number":
            if isinstance(value, bool):
                return value, False
            return float(value), True
        if kind == "boolean":
            if isinstance(value, bool):
                return value, True
            if str(value).strip().lower() in ("true", "false"):
                return str(value).strip().lower() == "true", True
            return value, False
        if kind == "string":
            return (value, True) if isinstance(value, str) else (str(value), not isinstance(value, (list, dict)))
        if kind == "array":
            return (list(value), True) if isinstance(value, (list, tuple)) else ([value], True)
    except (TypeError, ValueError):
        return value, False
    return value, True


def call_settings(text: str, tool: str) -> "dict[str, Any]":
    """Arguments a step writes as a call — ``run_de(groupby="sampleid", padj=0.05)`` — untyped."""
    out: dict[str, Any] = {}
    for call in re.finditer(_CALL_RE.format(name=re.escape(tool)), text or ""):
        for kw in _KWARG_RE.finditer(call.group(1)):
            quoted = kw.group(2) if kw.group(2) is not None else kw.group(3)
            out[kw.group(1)] = quoted if quoted is not None else kw.group(4)
    return out


def bind_settings(text: str, properties: "Mapping[str, Any]",
                  proposed: "Mapping[str, Any]") -> "tuple[dict[str, Any], list[dict[str, Any]]]":
    """``(settings, dropped)`` — the proposed arguments this step's own text supports.

    The proposal comes from a model (or from a call written in the step). It is admitted one
    argument at a time, and only when three things hold: the tool declares the argument, the value
    has the declared type and is inside a declared closed set, and the STEP TEXT states the value —
    a number the prose does not contain, or a name it never mentions, is the binder's invention and
    would pin the tool to something nobody approved. Booleans cannot be checked against prose and
    are admitted on type alone; they are shown in the document like every other setting."""
    settings: dict[str, Any] = {}
    dropped: list[dict[str, Any]] = []
    lowered = (text or "").lower()
    for key, value in (proposed or {}).items():
        spec = (properties or {}).get(key)
        if not isinstance(spec, Mapping):
            dropped.append({"param": key, "value": value, "why": "unknown_param"})
            continue
        value, ok = _coerce(value, spec)
        if not ok:
            dropped.append({"param": key, "value": value, "why": "wrong_type"})
            continue
        allowed = spec.get("enum")
        if allowed and value not in allowed:
            dropped.append({"param": key, "value": value, "why": "not_allowed"})
            continue
        if isinstance(value, bool):
            stated = True
        elif isinstance(value, (int, float)):
            stated = _states_number(text, float(value))
        elif isinstance(value, str):
            stated = bool(value.strip()) and value.strip().lower() in lowered
        elif isinstance(value, list):
            stated = bool(value) and all(str(v).strip().lower() in lowered for v in value)
        else:
            stated = False
        if not stated:
            dropped.append({"param": key, "value": value, "why": "not_in_text"})
            continue
        settings[key] = value
    return settings, dropped


def check_plan(doc: PlanDoc, schemas: "Mapping[str, Mapping[str, Any]]",
               is_known: "Callable[[str], bool] | None" = None,
               skill_name: "Callable[[str], str | None] | None" = None,
               ) -> "tuple[PlanDoc, list[dict[str, Any]]]":
    """``(doc with settings in their declared types, findings)`` for a document a person edited.

    Findings are ``{kind, step, id, tool, param, detail}``. ``unknown_tool``, ``unknown_setting``,
    ``bad_value`` and ``unknown_skill`` are errors — the run would fail on them, or run with a value
    the tool rejects — and the caller refuses the edit. An empty ``schemas`` yields no tool
    findings: with no catalog there is nothing to judge against.

    ``skill_name`` resolves a step's ``skill`` to the library's own name (an old name of a migrated
    skill resolves too), or ``None`` when no such skill is loaded."""
    if skill_name is not None:
        resolved: list[PlanStep] = []
        skill_findings: list[dict[str, Any]] = []
        for n, step in enumerate(doc.steps, 1):
            if step.skill:
                name = skill_name(step.skill)
                if name is None:
                    skill_findings.append({"step": n, "id": step.id, "tool": step.tool or "",
                                           "kind": "unknown_skill", "param": step.skill, "detail": ""})
                elif name != step.skill:
                    step = PlanStep(id=step.id, text=step.text, tool=step.tool, settings=step.settings,
                                    runs_on=step.runs_on, skill=name)
            resolved.append(step)
        doc = PlanDoc(request=doc.request, steps=tuple(resolved))
        if skill_findings:
            later_doc, later = check_plan(doc, schemas, is_known)
            return later_doc, skill_findings + later
    if not schemas:
        return doc, []
    findings: list[dict[str, Any]] = []
    steps: list[PlanStep] = []
    for n, step in enumerate(doc.steps, 1):
        base = {"step": n, "id": step.id, "tool": step.tool or ""}
        if not step.tool:
            steps.append(step)
            continue
        props = schemas.get(step.tool)
        if props is None:
            if not (is_known and is_known(step.tool)):
                findings.append({**base, "kind": "unknown_tool", "param": "", "detail": ""})
            steps.append(step)
            continue
        typed: dict[str, Any] = {}
        for key, value in step.settings.items():
            spec = props.get(key)
            if not isinstance(spec, Mapping):
                findings.append({**base, "kind": "unknown_setting", "param": key,
                                 "detail": ", ".join(sorted(props)) or "(the tool takes no settings)"})
                continue
            coerced, ok = _coerce(value, spec)
            allowed = spec.get("enum")
            if not ok:
                findings.append({**base, "kind": "bad_value", "param": key,
                                 "detail": f"{value!r} is not a {spec.get('type')}"})
            elif allowed and coerced not in allowed:
                findings.append({**base, "kind": "bad_value", "param": key,
                                 "detail": f"{value!r}; allowed: {', '.join(map(str, allowed))}"})
            else:
                typed[key] = coerced
        steps.append(PlanStep(id=step.id, text=step.text, tool=step.tool,
                              settings=typed, runs_on=step.runs_on, skill=step.skill))
    return PlanDoc(request=doc.request, steps=tuple(steps)), findings


# --- an edit ---------------------------------------------------------------------------------------

_ABSENT: Any = object()   # "this setting is not there", distinct from a setting whose value is null

def _same_value(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fmt(value: float) -> str:
    return repr(int(value)) if float(value) == int(value) else repr(float(value))


def _replace_number(text: str, old: float, new: float) -> "str | None":
    """``text`` with its ONE statement of ``old`` rewritten to ``new``; ``None`` when the text
    states it nowhere or more than once (which occurrence is the setting cannot be told)."""
    hits = [(start, end) for start, end, value in _number_spans(text) if abs(value - old) < 1e-9]
    if len(hits) != 1:
        return None
    start, end = hits[0]
    return text[:start] + _fmt(new) + text[end:]


def apply_edit(before: PlanDoc, edited: PlanDoc) -> "tuple[PlanDoc, list[dict[str, Any]], list[dict[str, Any]]]":
    """``(document to adopt, changes, notes)`` for an edit of ``before``.

    Steps are matched by id. A step the editor did not touch comes back as the SAME object, so
    nothing downstream sees it as changed. ``changes`` lists what the edit did (``removed``,
    ``added``, ``moved``, ``title``, ``text``, ``tool``, ``skill``, ``setting``) with before/after
    values.

    A setting and the sentence that states it are two copies of one number. When the editor changes
    the setting and the prose states the old value exactly once, the prose is rewritten to match
    (the change is listed). Where that cannot be done safely — or the editor changed the prose and
    left the setting — a ``note`` says the two disagree and that the setting is what runs. Changing
    a step's ``tool`` renames it in the step's sentence the same way."""
    old_by_id = {s.id: s for s in before.steps}
    old_pos = {s.id: i for i, s in enumerate(before.steps)}
    changes: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    steps: list[PlanStep] = []
    for n, step in enumerate(edited.steps, 1):
        old = old_by_id.get(step.id)
        if old is None:
            changes.append({"kind": "added", "step": n, "id": step.id, "after": step.text})
            steps.append(step)
            continue
        text = step.text
        same_prose = (_flat(step.title) == _flat(old.title) and _flat(step.does) == _flat(old.does))
        if same_prose:
            text = old.text                       # the very string the rest of the run already holds
        elif _flat(step.title) != _flat(old.title) and _flat(step.does) == _flat(old.does):
            changes.append({"kind": "title", "step": n, "id": step.id,
                            "before": old.title, "after": step.title})
        else:
            changes.append({"kind": "text", "step": n, "id": step.id,
                            "before": old.text, "after": step.text})
        if (step.tool or None) != (old.tool or None):
            changes.append({"kind": "tool", "step": n, "id": step.id,
                            "before": old.tool or "", "after": step.tool or ""})
            # The step's sentence names its tool, and that sentence is what the Scientist is handed.
            # Left alone it would ask for the old tool while the settings went to the new one.
            if old.tool and step.tool and same_prose:
                text = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(old.tool)}(?![A-Za-z0-9_])", step.tool, text)
        if (step.skill or None) != (old.skill or None):
            changes.append({"kind": "skill", "step": n, "id": step.id,
                            "before": old.skill or "", "after": step.skill or ""})
        for key in [*old.settings, *(k for k in step.settings if k not in old.settings)]:
            was, now = old.settings.get(key, _ABSENT), step.settings.get(key, _ABSENT)
            if was is not _ABSENT and now is not _ABSENT and _same_value(was, now):
                if _is_number(now) and not same_prose and _states_number(old.text, float(was)) \
                        and not _states_number(text, float(now)):
                    notes.append({"kind": "text_dropped_setting", "step": n, "id": step.id,
                                  "param": key, "value": now})
                continue
            changes.append({"kind": "setting", "step": n, "id": step.id, "param": key,
                            "before": None if was is _ABSENT else was,
                            "after": None if now is _ABSENT else now})
            if _is_number(was) and _is_number(now) and _states_number(text, float(was)) \
                    and not _states_number(text, float(now)):
                rewritten = _replace_number(text, float(was), float(now))
                if rewritten is None:
                    notes.append({"kind": "text_states_old_value", "step": n, "id": step.id,
                                  "param": key, "before": was, "after": now})
                else:
                    text = rewritten
        if old_pos[step.id] != n - 1 and len(edited.steps) == len(before.steps):
            changes.append({"kind": "moved", "step": n, "id": step.id, "before": old_pos[step.id] + 1})
        unchanged = (text == old.text and (step.tool or None) == (old.tool or None)
                     and (step.skill or None) == (old.skill or None)
                     and _settings_equal(step.settings, old.settings))
        steps.append(old if unchanged else PlanStep(
            id=step.id, text=text, tool=step.tool, settings=dict(step.settings),
            runs_on=old.runs_on if (step.tool or None) == (old.tool or None) else "",
            skill=step.skill or None))
    kept = {s.id for s in edited.steps}
    for s in before.steps:
        if s.id not in kept:
            changes.append({"kind": "removed", "step": old_pos[s.id] + 1, "id": s.id, "before": s.text})
    return PlanDoc(request=before.request, steps=tuple(steps)), changes, notes


def _settings_equal(a: "Mapping[str, Any]", b: "Mapping[str, Any]") -> bool:
    return set(a) == set(b) and all(_same_value(a[k], b[k]) for k in a)


# --- a revision, as operations on the document ---------------------------------------------------------
#
# A change the researcher asks for in words is made on the SAME document they would edit by hand: the
# model is shown the YAML and names what to change (``change`` / ``remove`` / ``add`` / ``move``, by
# step id), and this code applies it. The result goes through ``check_plan`` and ``apply_edit`` like a
# hand edit, so a step no operation names is the same object afterwards — also when the request adds,
# removes or reorders steps, which used to cost a redraft of the whole plan.

PLAN_OPS = ("change", "remove", "add", "move")
_START = ("", "start", "first", "top", "beginning")


def parse_plan_ops(raw: str) -> "list[dict[str, Any]] | None":
    """The operations in a model's reply — ``{"ops": [...]}`` or a bare list — or ``None`` when the
    reply is not one (unparseable, or the model declined with ``{"ops": []}`` / ``{"cannot": ...}``).
    ``None`` means "revise the plan another way", never "do nothing"."""
    text = (raw or "").strip()
    obj: Any = None
    for candidate in (text, *(m.group(0) for m in [re.search(r"\{.*\}", text, re.S),
                                                    re.search(r"\[.*\]", text, re.S)] if m)):
        try:
            obj = json.loads(candidate)
            break
        except (ValueError, TypeError):
            continue
    if isinstance(obj, Mapping):
        obj = obj.get("ops")
    if not isinstance(obj, list) or not obj:
        return None
    ops = [dict(o) for o in obj if isinstance(o, Mapping)]
    return ops if len(ops) == len(obj) else None


def apply_ops(doc: PlanDoc, ops: "Iterable[Mapping[str, Any]]") -> PlanDoc:
    """``doc`` with ``ops`` applied in order. Raises :class:`PlanError` naming every operation that
    cannot be applied (an unknown step, an unknown operation, a step with no text); nothing is
    applied in that case.

    A step is named by its ``id``; its 1-based number in ``doc`` is accepted too. ``change`` sets
    any of ``title``, ``does``, ``tool`` and ``settings`` (merged into the step's settings; a
    ``null`` value removes one). ``add`` and ``move`` place a step ``after`` another, or first with
    ``after: "start"``; ``add`` without ``after`` appends."""
    steps = list(doc.steps)
    numbered = {str(i): s.id for i, s in enumerate(doc.steps, 1)}
    used = {s.id for s in steps}
    problems: list[str] = []

    def find(ref: Any) -> "int | None":
        key = str(ref if ref is not None else "").strip()
        ids = [s.id for s in steps]
        if key in ids:
            return ids.index(key)
        key = numbered.get(key.lstrip("#"), "")
        return ids.index(key) if key in ids else None

    def place(ref: Any, n: int) -> "int | None":
        """Where a step goes for ``after: ref`` — an index into ``steps`` — or None (reported)."""
        if isinstance(ref, str) and ref.strip().lower() in _START:
            return 0
        at = find(ref)
        if at is None:
            problems.append(f"Operation {n}: there is no step {ref!r} to put it after.")
            return None
        return at + 1

    for n, op in enumerate(ops, 1):
        kind = str(op.get("op") or "").strip().lower()
        if kind not in PLAN_OPS:
            problems.append(f"Operation {n}: {kind or '(none)'!r} is not one of {', '.join(PLAN_OPS)}.")
            continue
        if kind == "add":
            title = _flat(str(op.get("title") or ""))
            does = str(op.get("does") or op.get("text") or "").strip()
            if not title and not does:
                problems.append(f"Operation {n}: the step to add has neither a title nor a `does` text.")
                continue
            settings = op.get("settings")
            if settings not in (None, {}) and not isinstance(settings, Mapping):
                problems.append(f"Operation {n}: `settings` must be a mapping of name to value.")
                continue
            at = len(steps) if "after" not in op or op.get("after") is None else place(op.get("after"), n)
            if at is None:
                continue
            sid = _fresh_id(used)
            used.add(sid)
            tool = str(op.get("tool") or "").strip() or None
            skill = str(op.get("skill") or "").strip() or None
            steps.insert(at, PlanStep(id=sid, text=join_step(title, does), tool=tool,
                                      settings={str(k): v for k, v in (settings or {}).items() if v is not None},
                                      skill=skill))
            continue
        at = find(op.get("step"))
        if at is None:
            problems.append(f"Operation {n}: there is no step {op.get('step')!r} in the plan.")
            continue
        step = steps[at]
        if kind == "remove":
            del steps[at]
        elif kind == "move":
            del steps[at]
            to = place(op.get("after", "start"), n)
            steps.insert(at if to is None else to, step)
        else:   # change
            title = _flat(str(op["title"])) if op.get("title") is not None else step.title
            does = str(op["does"]).strip() if op.get("does") is not None else step.does
            if not title and not does:
                problems.append(f"Operation {n}: step {step.id} would be left with no text.")
                continue
            new = op.get("settings")
            if new is not None and not isinstance(new, Mapping):
                problems.append(f"Operation {n}: `settings` must be a mapping of name to value.")
                continue
            settings = dict(step.settings)
            for key, value in (new or {}).items():
                if value is None:
                    settings.pop(str(key), None)
                else:
                    settings[str(key)] = value
            tool = (str(op["tool"]).strip() or None) if "tool" in op and op["tool"] is not None else step.tool
            # ``"skill": null`` takes the step's skill away; no ``skill`` key leaves it as it is.
            skill = (str(op["skill"] or "").strip() or None) if "skill" in op else step.skill
            prose_same = title == step.title and does == step.does
            steps[at] = PlanStep(id=step.id, text=step.text if prose_same else join_step(title, does),
                                 tool=tool, settings=settings, runs_on=step.runs_on, skill=skill)
    if not problems and not steps:
        problems.append("The operations remove every step. To run nothing, cancel the plan instead.")
    if problems:
        raise PlanError(problems)
    return PlanDoc(request=doc.request, steps=tuple(steps))


# --- pinning ---------------------------------------------------------------------------------------

def pin_args(args: "Mapping[str, Any]", pinned: "Mapping[str, Any]") -> "tuple[dict[str, Any], list[dict[str, Any]]]":
    """``(args with the approved settings applied, what that overrode)``.

    The approved value wins over the value the model sent; an argument the plan does not fix is the
    model's to choose. ``overrode`` lists only real disagreements — an argument the model left out
    is filled in silently, since leaving it out states no other choice."""
    out = dict(args or {})
    overrode: list[dict[str, Any]] = []
    for key, value in (pinned or {}).items():
        if key in out and not _same_value(out[key], value):
            overrode.append({"param": key, "approved": value, "model": out[key]})
        out[key] = value
    return out, overrode


# --- reading a finished run back against the approved plan --------------------------------------------

def check_adherence(doc: PlanDoc, executed: "list[dict[str, Any]]",
                    approved_additions: "Iterable[str]" = ()) -> "dict[str, Any]":
    """What the run did with each approved step.

    ``executed`` is the run's rounds in order: ``{"step": text, "accepted": bool, "calls":
    [{"tool", "args", "ok"}]}``. Each approved step gets one status:

    * ``as_planned``       — it ran, its tool was called and every approved setting was used
    * ``not_run``          — no round executed it (pruned, skipped, or the run stopped first)
    * ``tool_not_called``  — it ran, but the tool the plan names was never called
    * ``tool_failed``      — the named tool was called and never succeeded
    * ``settings_differ``  — a call used a different value than the one approved
    * ``not_accepted``     — it ran as planned, and the reviewer did not accept the result

    A step that ran without being in the approved plan is listed in ``added`` when the researcher
    approved it during the run (``approved_additions``: its text), and in ``unplanned`` otherwise.
    Only the second makes the run differ from what was approved. Deterministic — it compares
    records, it does not judge the science."""
    by_text: dict[str, list[dict[str, Any]]] = {}
    for item in executed or []:
        by_text.setdefault(str(item.get("step", "")), []).append(item)
    rows: list[dict[str, Any]] = []
    for n, step in enumerate(doc.steps, 1):
        row: dict[str, Any] = {"step": n, "id": step.id, "title": step.title or step.does[:60],
                               "tool": step.tool or "", "status": "as_planned", "detail": []}
        runs = by_text.get(step.text) or []
        if not runs:
            row["status"] = "not_run"
            rows.append(row)
            continue
        calls = [c for r in runs for c in (r.get("calls") or []) if isinstance(c, Mapping)]
        if step.tool:
            mine = [c for c in calls if c.get("tool") == step.tool]
            if not mine:
                row["status"] = "tool_not_called"
                row["detail"] = sorted({str(c.get("tool")) for c in calls if c.get("tool") != "finish"})
                rows.append(row)
                continue
            good = [c for c in mine if c.get("ok")]
            if not good:
                row["status"] = "tool_failed"
                rows.append(row)
                continue
            differ: list[dict[str, Any]] = []
            for key, approved in step.settings.items():
                used = [(c.get("args") or {}).get(key, _ABSENT) for c in good]
                if not any(u is not _ABSENT and _same_value(u, approved) for u in used):
                    seen = next((u for u in used if u is not _ABSENT), None)
                    differ.append({"param": key, "approved": approved, "used": seen})
            if differ:
                row["status"] = "settings_differ"
                row["detail"] = differ
                rows.append(row)
                continue
        if not runs[-1].get("accepted", False):
            row["status"] = "not_accepted"
        rows.append(row)
    planned = {s.text for s in doc.steps}
    allowed = {str(t) for t in approved_additions or ()}
    added: list[str] = []
    unplanned: list[str] = []
    for item in executed or []:
        text = str(item.get("step", ""))
        if not text or text in planned:
            continue
        bucket = added if text in allowed else unplanned
        if text not in bucket:
            bucket.append(text)
    return {"steps": rows, "added": added, "unplanned": unplanned,
            "as_planned": sum(1 for r in rows if r["status"] == "as_planned"),
            "total": len(rows),
            "ok": not unplanned and all(r["status"] == "as_planned" for r in rows)}


_STATUS_WORDS = {
    "as_planned": "ran as approved",
    "not_run": "did not run",
    "tool_not_called": "ran, but its tool was never called",
    "tool_failed": "its tool never succeeded",
    "settings_differ": "ran with a different setting than approved",
    "not_accepted": "ran as approved; the result was not accepted",
}


def describe_status(row: "Mapping[str, Any]") -> str:
    """One row of :func:`check_adherence` as a sentence fragment."""
    words = _STATUS_WORDS.get(str(row.get("status")), str(row.get("status")))
    detail = row.get("detail") or []
    if row.get("status") == "settings_differ":
        words += " (" + "; ".join(
            f"{d['param']}: approved {d['approved']!r}, used {d['used']!r}" for d in detail) + ")"
    elif row.get("status") == "tool_not_called" and detail:
        words += " (called: " + ", ".join(f"`{t}`" for t in detail) + ")"
    return words


def adherence_markdown(report: "Mapping[str, Any]") -> str:
    """The adherence check as a short Markdown record (``process/plan_adherence.md``)."""
    lines = ["# The run against its approved plan", "",
             f"{report.get('as_planned', 0)} of {report.get('total', 0)} approved steps ran as approved.", "",
             "| Step | Tool | Result |", "|---|---|---|"]
    for row in report.get("steps") or []:
        tool = f"`{row['tool']}`" if row.get("tool") else "—"
        lines.append(f"| {row['step']}. {row['title']} | {tool} | {describe_status(row)} |")
    added = report.get("added") or []
    if added:
        lines += ["", "## Steps added during the run, with the researcher's approval", ""]
        lines += [f"- {split_step(t)[0] or t[:120]}" for t in added]
    unplanned = report.get("unplanned") or []
    if unplanned:
        lines += ["", "## Steps that ran without being in the approved plan", ""]
        lines += [f"- {split_step(t)[0] or t[:120]}" for t in unplanned]
    return "\n".join(lines) + "\n"
