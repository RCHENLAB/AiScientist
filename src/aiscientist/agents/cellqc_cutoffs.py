"""CellQC cutoffs named in a chat message: "raise the mitochondrial limit to 20".

``run_cellqc`` already takes ``min_counts``, ``min_features`` and ``max_pct_mt``; what was missing is
reading a plain sentence as one of those settings, saying so on the plan card, and refusing a
request that cannot be read before anything runs. This module is the reading and the checking —
pure, no model call, no I/O — so the same sentence gives the same answer every time:

* :func:`read_cutoff_request` turns a reply into ``{setting: value}`` and a list of problems (an
  out-of-range value, a cutoff named without a value, a value with no cutoff). A reply with any
  problem changes nothing: the researcher is told what was wrong and the plan stays as it is.
* :func:`cutoff_rows` is what the plan card shows for a step: every cutoff the step's tool takes,
  with its value, unit, and whether it is the default or has been changed.

Adaptive and per-library cutoff methods are not read here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Cutoff:
    name: str            # the tool's setting
    label: str           # how the researcher says it
    unit: str
    integer: bool
    low: float           # exclusive lower bound for a percent, inclusive for a count (see ``problem``)
    high: float
    pattern: str         # how a message names it
    fallback: Any        # the default when the tool's schema does not state one


CUTOFFS: "dict[str, Cutoff]" = {
    "max_pct_mt": Cutoff(
        "max_pct_mt", "mitochondrial limit", "%", False, 0.0, 100.0,
        r"max[_ ]pct[_ ]mt|mitochondri\w*|\bmito\w*|\bpercent[_ ]mt\b|\bpct[_ ]mt\b|\bmt[- ]?(?:percent|pct|%|fraction|content|reads)\b",
        10.0),
    "min_counts": Cutoff(
        "min_counts", "minimum counts per barcode", "counts", True, 0.0, 1_000_000.0,
        r"min[_ ]counts|min(?:imum)?\s+(?:total\s+)?(?:counts?|umis?)|(?:total\s+|umi\s+)?(?:counts?|umis?)\s+(?:cut-?off|threshold|limit|filter|minimum)",
        500),
    "min_features": Cutoff(
        "min_features", "minimum genes per barcode", "genes", True, 0.0, 100_000.0,
        r"min[_ ]features|min(?:imum)?\s+(?:detected\s+)?(?:features?|genes)|(?:features?|genes)(?:\s+per\s+(?:cell|barcode))?\s+(?:cut-?off|threshold|limit|filter|minimum)|nfeatures?",
        300),
}

_NUMBER = r"-?\d+(?:\.\d+)?"
_NUM_RE = re.compile(_NUMBER)
# "to 20", "at 20", "= 20", ": 20", "of 20" — the words that join a cutoff to its value.
_VALUE_AFTER = re.compile(rf"(?:\bto\b|\bat\b|\bof\b|\bas\b|=|:|\bfrom\s+{_NUMBER}\s+to\b)\s*({_NUMBER})\s*(%|percent)?", re.I)
_VALUE_PLAIN = re.compile(rf"({_NUMBER})\s*(%|percent)?")
# A cutoff word with no name for which one: "the cutoff", "the threshold".
_GENERIC = re.compile(r"\b(?:cut-?offs?|thresholds?|limits?)\b", re.I)
_QC_WORD = re.compile(r"\b(?:qc|quality|filter(?:s|ing)?|cut-?offs?|thresholds?|limits?)\b", re.I)
_WINDOW = 48     # how far after a cutoff's name its value may stand


def _mentions(text: str) -> "list[tuple[int, int, str]]":
    """``(start, end, setting)`` for every cutoff the text names, in order."""
    found: list[tuple[int, int, str]] = []
    for name, spec in CUTOFFS.items():
        for m in re.finditer(spec.pattern, text, re.I):
            found.append((m.start(), m.end(), name))
    found.sort()
    out: list[tuple[int, int, str]] = []
    for item in found:   # two patterns on one stretch of text ("min_counts" inside "min counts")
        if out and item[0] < out[-1][1]:
            continue
        out.append(item)
    return out


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


def read_cutoff_request(text: str) -> "tuple[dict[str, Any], list[str]]":
    """``(changes, problems)`` for a reply that may set CellQC cutoffs.

    ``changes`` maps a setting name to its new value; ``problems`` explain, in words for the
    researcher, why part of the reply could not be used. Both empty means the reply is not about a
    cutoff and is left to the other ways a plan is revised. A reply with problems is to be answered,
    not applied — ``changes`` is then only what WOULD have been set."""
    text = text or ""
    mentions = _mentions(text)
    changes: dict[str, Any] = {}
    problems: list[str] = []
    for i, (_start, end, name) in enumerate(mentions):
        spec = CUTOFFS[name]
        limit = mentions[i + 1][0] if i + 1 < len(mentions) else len(text)
        window = text[end:min(limit, end + _WINDOW)]
        # The clause ends at a sentence break: "set mito to 20. Then cluster at 0.8" is 20, not 0.8.
        window = re.split(r"[.;\n](?:\s|$)|\bthen\b|\band\s+(?:then|also)\b", window, maxsplit=1)[0]
        m = _VALUE_AFTER.search(window) or _VALUE_PLAIN.search(window)
        if m is None:
            # "20% mitochondrial": the value stands just before the name.
            before = text[max(0, mentions[i - 1][1] if i else 0):_start]
            m = None
            for cand in _VALUE_PLAIN.finditer(before[-16:]):
                m = cand
            if m is None:
                problems.append(f"You named {spec.name} ({spec.label}) but not the value to use. "
                                f"Say it as a number, for example “set {spec.name} to "
                                f"{CUTOFFS[name].fallback}”.")
                continue
        given = m.group(1)
        value = float(given)
        why = _range_problem(spec, value, given)
        if why:
            problems.append(why)
            continue
        new = int(value) if spec.integer else value
        if name in changes and changes[name] != new:
            problems.append(f"{spec.name} was given two different values ({changes[name]} and {new}). "
                            "Say which one to use.")
            continue
        changes[name] = new
    if not mentions and _GENERIC.search(text) and _QC_WORD.search(text) and _NUM_RE.search(text) \
            and re.search(r"\b(?:raise|lower|increase|decrease|set|change|use|make|relax|tighten)\b", text, re.I) \
            and re.search(r"\b(?:qc|quality|filter\w*|barcode|cell)\b", text, re.I):
        problems.append("Which cutoff? CellQC has three: min_counts (total counts per barcode), "
                        "min_features (genes detected per barcode) and max_pct_mt (mitochondrial "
                        "percent). Name one with the value, for example “raise the mitochondrial "
                        "limit to 20”.")
    return changes, problems


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


def describe_change(name: str, before: Any, after: Any) -> str:
    spec = CUTOFFS[name]
    unit = "%" if spec.unit == "%" else f" {spec.unit}"
    return f"{name} {before}{unit} → {after}{unit}"
