"""CellQC cutoffs named in a chat message: "raise the mitochondrial limit to 20".

``run_cellqc`` already takes ``min_counts``, ``min_features`` and ``max_pct_mt``; what was missing is
reading a plain sentence as one of those settings, saying so on the plan card, and refusing a
request that cannot be read before anything runs. This module is the reading and the checking —
pure, no model call, no I/O — so the same sentence gives the same answer every time:

* :func:`read_cutoffs` finds each cutoff the reply names together with the number it is set to,
  and says whether the reply asks for anything else. Only a certain reading counts: the cutoff's
  name, at most a few words such as "per cell" or "threshold", an optional joining word ("to",
  "=", "at") and the number. "Cluster at resolution 0.8 and regress mito" names no cutoff, so it
  is left to the other ways a plan is revised; "raise the mitochondrial limit by 5" names one
  without the value to use, which is explained back.
* :func:`cutoff_rows` is what the plan card shows for a step: every cutoff the step's tool takes,
  with its value, unit, and whether it is the default or has been changed.

Adaptive and per-library cutoff methods are not read here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class Cutoff:
    name: str            # the tool's setting
    label: str           # how the researcher says it
    unit: str
    integer: bool
    high: float          # the largest value that can be used
    fallback: Any        # the default when the tool's schema does not state one
    strong: str          # a name that is always this cutoff ("mito limit"): without a value it is asked for
    weak: str            # a name that is this cutoff only with a value right after it ("mito content 20")


_MT = r"(?:mitochondri(?:al|a)|mito|mt)"
_MT_QTY = r"(?:reads?|genes?|counts?|rna|content|percent(?:age)?|fraction|proportion|%)"
_LIMIT = r"(?:limit|cut-?off|threshold|cap|ceiling|maximum|max|filter)"
_FLOOR = r"(?:cut-?off|threshold|limit|minimum|min|floor|filter)"
_PER = r"(?:\s+per\s+(?:cell|barcode|nucleus|droplet))"
_COUNTS = r"(?:umi\s+counts?|counts?|umis?)"

CUTOFFS: "dict[str, Cutoff]" = {
    "max_pct_mt": Cutoff(
        "max_pct_mt", "mitochondrial limit", "%", False, 100.0, 10.0,
        rf"\bmax[_ ]?pct[_ ]?mt\b|\b(?:pct|percent)[_. ]mt\b|\b{_MT}(?:\s+{_MT_QTY})?\s+{_LIMIT}\b"
        rf"|\bmax(?:imum)?\s+{_MT}\b(?:\s+{_MT_QTY}(?!\w))?",
        rf"\b{_MT}\s+{_MT_QTY}(?!\w)|(?:\bpercent(?:age)?|%)\s+(?:of\s+)?{_MT}\b(?:\s+(?:reads?|counts?|genes?|content|rna)\b)?"),
    "min_counts": Cutoff(
        "min_counts", "minimum counts per barcode", "counts", True, 1_000_000.0, 500,
        rf"\bmin[_ ]?counts?\b|\bmin(?:imum)?\s+(?:number\s+of\s+)?(?:total\s+)?{_COUNTS}{_PER}?(?!\w)"
        rf"|\b(?:total\s+)?{_COUNTS}{_PER}?\s+{_FLOOR}\b",
        rf"\b(?:total\s+)?{_COUNTS}{_PER}(?!\w)"),
    "min_features": Cutoff(
        "min_features", "minimum genes per barcode", "genes", True, 100_000.0, 300,
        rf"\bmin[_ ]?features?\b|\bmin(?:imum)?\s+(?:number\s+of\s+)?(?:detected\s+)?(?:features?|genes)(?:\s+detected)?{_PER}?(?!\w)"
        rf"|\b(?:features?|genes){_PER}\s+{_FLOOR}\b",
        rf"\b(?:detected\s+)?(?:features?|genes){_PER}(?!\w)"),
}

# A bare "mito" is the mitochondrial limit only when a joining word gives it a value: "set mito to 5".
_BARE_MT = re.compile(r"\b(?:mitochondri(?:al|a)|mito)\b", re.I)
_NUMBER = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_VALUE = rf"(?<![\w.])(?P<value>{_NUMBER})(?!\w)\s*(?P<unit>%|percent\b)?"
# Words that may stand between a cutoff's name and its value: "minimum genes per cell threshold to 200".
_GAP = (r"(?:\s+(?:per|cells?|barcodes?|nucleus|nuclei|droplets?|cut-?off|threshold|limit|cap|filter|"
        r"minimum|maximum|value|setting|in|for|the|qc|step|cellqc|run_cellqc)\b){0,4}")
_FROM = rf"(?:\s+from\s+{_NUMBER}\s*(?:%|percent\b)?)?"
_TAIL = re.compile(rf"{_GAP}{_FROM}\s*(?:(?:to|at|of|as|is|be)\b|=|:|->|→)?\s*{_VALUE}", re.I)
_BARE_TAIL = re.compile(rf"\s*(?:(?:to|at)\b|=|:)\s*{_VALUE}", re.I)
_RELATIVE = re.compile(rf"{_GAP}\s+by\s+{_NUMBER}", re.I)
# "25% mitochondrial": the value before the name, with its percent sign.
_VALUE_FIRST = re.compile(
    rf"(?<![\w.])(?P<value>{_NUMBER})\s*(?:%|percent\b)\s+(?:of\s+)?{_MT}\b"
    rf"(?:\s+(?:reads?|counts?|genes?|content|rna|limit|cut-?off|threshold|cap)\b)*", re.I)
# A cutoff asked for without saying which: "change the QC cutoff to 20".
_GENERIC = re.compile(r"\b(?:qc|quality[- ]control|quality|filtering|filter|cellqc)\s+(?:cut-?offs?|thresholds?|limits?)\b"
                      r"|\b(?:cut-?offs?|thresholds?)\s+(?:for|of)\s+(?:qc|quality[- ]control|filtering)\b", re.I)
_UP = re.compile(r"\b(?:raise|raising|increase|increasing|higher|bump)\b", re.I)
_DOWN = re.compile(r"\b(?:lower|lowering|decrease|decreasing|reduce|reducing|drop)\b", re.I)
_CLAUSE_BREAK = re.compile(r"(?<!\d)[.,]|[.,](?!\d)|[;\n]|\band\b|\bthen\b", re.I)
# What a reply that only sets cutoffs is made of besides them: verbs, articles, joining words.
_FILLER = frozenset("""
    set sets raise lower increase decrease reduce drop bump change make use adjust update put move bring
    keep relax loosen tighten allow please can could would you we i let lets s want like to the a an and
    also then plus both it its in for of from at on instead ok okay thanks thank now just cutoff cutoffs
    cut off threshold thresholds limit limits cap filter filters filtering qc cellqc run_cellqc quality
    control step value values setting settings plan this that cell cells barcode barcodes per percent up
    down so be is
""".split())


@dataclass
class CutoffReading:
    """What a reply says about CellQC cutoffs.

    ``changes`` maps each setting read for certain to its value. ``problems`` are ``(setting,
    words)`` for a cutoff named in a way that cannot be used (``setting`` is ``None`` for "which
    cutoff?"). ``only`` is true when the reply asks for nothing besides those settings.
    ``directions`` holds "up" or "down" when the reply says "raise" or "lower" before a setting."""
    changes: "dict[str, Any]" = field(default_factory=dict)
    problems: "list[tuple[str | None, str]]" = field(default_factory=list)
    only: bool = False
    directions: "dict[str, str]" = field(default_factory=dict)


def _number(text: str) -> float:
    return float(text.replace(",", ""))


def _range_problem(spec: Cutoff, value: float, given: str) -> str:
    if spec.integer and value != int(value):
        return f"{spec.name} counts whole {spec.unit}; {given} is not a whole number."
    if spec.name == "max_pct_mt":
        if value <= 0:
            return f"{spec.name} must be above 0: {given}% would remove every barcode."
        if value > 100:
            return f"{spec.name} is a percent and cannot be above 100; {given}% cannot be used."
        return ""
    if value < 0 or value > spec.high:
        return f"{spec.name} must be between 0 and {int(spec.high):,}; {given} cannot be used."
    return ""


def _clause(text: str, start: int, end: int) -> str:
    """The clause around ``text[start:end]``: from the break before it to the break after it."""
    breaks = [m.end() for m in _CLAUSE_BREAK.finditer(text, 0, start)]
    after = _CLAUSE_BREAK.search(text, end)
    return text[breaks[-1] if breaks else 0:after.start() if after else len(text)]


def _direction(text: str, start: int) -> "str | None":
    """"up" or "down" from the verb in the clause before ``start``, if it says one."""
    clause = text[:start]
    breaks = [m.end() for m in _CLAUSE_BREAK.finditer(clause)]
    clause = clause[breaks[-1]:] if breaks else clause
    up, down = _UP.search(clause), _DOWN.search(clause)
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return None


def _mentions(text: str) -> "list[tuple[int, int, str, str]]":
    """``(start, end, setting, kind)`` for every name of a cutoff in the text, in order; ``kind``
    is "first" (value before the name), "strong", "weak" or "bare". Where two overlap, the earlier
    and then the longer one is kept."""
    found: list[tuple[int, int, str, str]] = []
    for m in _VALUE_FIRST.finditer(text):
        found.append((m.start(), m.end(), "max_pct_mt", "first"))
    for name, spec in CUTOFFS.items():
        for kind, pattern in (("strong", spec.strong), ("weak", spec.weak)):
            for m in re.finditer(pattern, text, re.I):
                found.append((m.start(), m.end(), name, kind))
    for m in _BARE_MT.finditer(text):
        found.append((m.start(), m.end(), "max_pct_mt", "bare"))
    found.sort(key=lambda f: (f[0], -f[1]))
    out: list[tuple[int, int, str, str]] = []
    for item in found:
        if out and item[0] < out[-1][1]:
            continue
        out.append(item)
    return out


def read_cutoffs(text: str) -> CutoffReading:
    """What ``text`` asks of the CellQC cutoffs (see :class:`CutoffReading`)."""
    text = text or ""
    reading = CutoffReading()
    spans: list[tuple[int, int]] = []
    for start, end, name, kind in _mentions(text):
        spec = CUTOFFS[name]
        if kind == "first":
            value_text, close = _VALUE_FIRST.match(text, start).group("value"), end
        else:
            tail = (_BARE_TAIL if kind == "bare" else _TAIL).match(text, end)
            if tail is None:
                if kind == "strong" and _RELATIVE.match(text, end):
                    reading.problems.append((name, (
                        f"Say the value {spec.name} ({spec.label}) should have, not how much to change it: "
                        f"for example “set {spec.name} to {spec.fallback}”.")))
                elif kind == "strong":
                    reading.problems.append((name, (
                        f"You named {spec.name} ({spec.label}) but not the value to use. Say it as a "
                        f"number, for example “set {spec.name} to {spec.fallback}”.")))
                continue    # a weak or bare name with no value is not about the cutoff
            value_text, close = tail.group("value"), tail.end()
        # "min count 10" said of genes ("filter genes with min count 10", "min counts 10 per gene")
        # is a gene filter, not the per-barcode count cutoff.
        if name == "min_counts" and re.search(r"\b(?:genes?|features?)\b", _clause(text, start, close), re.I):
            continue
        spans.append((start, close))
        value = _number(value_text)
        named = text[start:end].lower()
        if name == "max_pct_mt" and value <= 1 and re.search(r"fraction|proportion", named):
            reading.problems.append((name, (
                f"{spec.name} is a percent; {value_text} reads as a fraction. Say the percent, for "
                f"example “set {spec.name} to {value * 100:g}”.")))
            continue
        why = _range_problem(spec, value, value_text)
        if why:
            reading.problems.append((name, why))
            continue
        new = int(value) if spec.integer else value
        if name in reading.changes and reading.changes[name] != new:
            reading.problems.append((name, (
                f"{spec.name} was given two different values ({reading.changes[name]} and {new}). "
                "Say which one to use.")))
            continue
        reading.changes[name] = new
        direction = _direction(text, start)
        if direction:
            reading.directions[name] = direction
    if not spans and not reading.problems and _GENERIC.search(text) and re.search(_VALUE, text):
        reading.problems.append((None, (
            "Which cutoff? Name the one to change with its value, for example “raise the "
            "mitochondrial limit to 20”.")))
    if reading.changes and not reading.problems:
        rest = text
        for start, close in sorted(spans, reverse=True):
            rest = rest[:start] + " " + rest[close:]
        words = re.findall(r"[a-z_]+|\d", rest.lower().replace("'", ""))
        reading.only = all(w in _FILLER for w in words)
    return reading


def read_cutoff_request(text: str) -> "tuple[dict[str, Any], list[str]]":
    """``(changes, problems)`` of :func:`read_cutoffs`: the settings read with their values, and why
    part of the reply could not be used, in words for the researcher. Both empty means the reply is
    not about a cutoff."""
    reading = read_cutoffs(text)
    return dict(reading.changes), [why for _setting, why in reading.problems]


def cutoff_rows(settings: "Mapping[str, Any]", schema: "Mapping[str, Any]") -> "list[dict[str, Any]]":
    """The cutoffs a step's tool takes, for the plan card: ``{param, label, value, unit, default,
    changed}``. ``schema`` is the tool's parameter properties; a cutoff it does not declare is left
    out. ``changed`` is true when the plan's value differs from the tool's default."""
    rows: list[dict[str, Any]] = []
    for name, spec in CUTOFFS.items():
        prop = schema.get(name) if isinstance(schema, Mapping) else None
        if not isinstance(prop, Mapping):
            continue
        default = prop.get("default", spec.fallback)
        value = settings.get(name, default)
        rows.append({"param": name, "label": spec.label, "value": value, "unit": spec.unit,
                     "default": default, "changed": name in settings and value != default})
    return rows
