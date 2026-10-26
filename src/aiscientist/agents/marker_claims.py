"""A gene the report attributes to the wrong cell lineage, caught against the curated marker reference.

Run c57071e7dc94's manuscript (2026-10-04) said "Notably, RLBP1 is a canonical rod photoreceptor marker,
but in this dataset it discriminates the Müller glia clusters…". RLBP1 (CRALBP) marks Müller glia and
RPE. The cluster labels were right, because ``run_marker_annotation`` took its markers from the curated
retina reference. The phrase came from model memory: it first appears in a claim-audit candidate
("RLBP1—a canonical rod photoreceptor marker—"), and the manuscript writer kept it. Closed-set grounding
pins labels and numbers, not what a gene marks, so nothing caught it.

This check reads the reference the annotation used. A sentence that says a gene IS a marker of a
lineage ("X is a canonical <lineage> marker", "X, a <lineage> marker", "X—a <lineage> marker—", "X is
a marker of <lineage>") or MARKS one ("X marks <lineage>") is removed when X is a specific marker
(discriminator) of another lineage and the claimed lineage's panel does not list X. The sentence
argues from the false premise, so swapping the lineage name would leave nonsense, and rewriting it
would put prose in the manuscript that no model wrote. Deliberately narrow: only discriminators are
judged (panel genes are shared), a lineage the reference does not name ("photoreceptor", "glial") is
not judged, nor is a negated claim or a cell state ("GFAP marks reactive Müller glia" is gliosis, not
identity).
"""

from __future__ import annotations

import re
from typing import Any

_WORD = r"[\w'’/-]+"
# Words that make "X is … marker" something other than an attribution: a negation, or a preposition
# ("X is absent from the rod marker panel").
_NOT_A_CLAIM = re.compile(
    r"\b(?:not|no|never|neither|nor|in|from|of|by|with|within|among|across|than|to|on|at|into|as|"
    r"unlike|rather|absent|expressed|enriched|detected|used|included|listed|missing)\b|n't",
    re.IGNORECASE)
# A lineage phrase ends where a contrast or a qualifier starts: "a marker of glia rather than rods".
_PHRASE_END = re.compile(r"\b(?:rather|not|than|but|unlike|instead|whereas|while|versus|vs|nor|in|"
                         r"across|within|among)\b", re.IGNORECASE)
# The reference defines lineages, not states.
_STATE = re.compile(r"\b(?:reactiv|gliot|gliosis|activat|stress|injur|inflam|degenerat|upregulat|induc)",
                    re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?][\"'’”)\]*_]*(?=\s|$)")
_ABBREVIATION = re.compile(r"\b(?:e\.g|i\.e|vs|cf|figs?|approx|al)\.$", re.IGNORECASE)
_LINE_MARKER = re.compile(r"[ \t]*(?:(?:[-*+]|\d+[.)]|>|#{1,6})[ \t]+)*")


def _norm(name: str) -> str:
    """The reference's own normalisation ("Müller glia" -> "mullerglia"), so its aliases apply."""
    return re.sub(r"[^a-z]", "", str(name).lower().replace("ü", "u"))


def _sentence_span(md: str, pos: int) -> "tuple[int, int]":
    """The sentence around ``pos``, bounded by sentence ends and by its line (after any list marker)."""
    ls = md.rfind("\n", 0, pos) + 1
    le = md.find("\n", pos)
    le = len(md) if le == -1 else le
    start = _LINE_MARKER.match(md, ls, le).end()
    end = le
    for m in _SENTENCE_END.finditer(md, start, le):
        if _ABBREVIATION.search(md[start:m.start() + 1]):
            continue
        if m.end() <= pos:
            start = m.end()
        else:
            end = m.end()
            break
    while start < end and md[start] in " \t":
        start += 1
    return start, end


def _cut(md: str, start: int, end: int) -> str:
    """Remove ``md[start:end]`` with its spacing; a line left with only a list/quote marker goes too."""
    ls = md.rfind("\n", 0, start) + 1
    le = md.find("\n", end)
    le = len(md) if le == -1 else le
    tail = end
    while tail < le and md[tail] in " \t":
        tail += 1
    line = md[ls:start] + md[tail:le]
    if not line[_LINE_MARKER.match(line).end():].strip():
        return md[:ls] + md[le + 1:]
    if tail == le:                      # the line's last sentence: take the space before it instead
        while start > ls and md[start - 1] in " \t":
            start -= 1
    return md[:start] + md[tail:]


def check_marker_claims(md: str, reference: "dict[str, Any] | None") -> "tuple[str, list[str]]":
    """Remove each sentence that calls a gene a marker of a lineage the curated ``reference`` (a
    ``run_marker_annotation`` tissue JSON) gives to a different lineage. Returns ``(md, issues)``."""
    if not md or not isinstance(reference, dict) or not isinstance(reference.get("species"), dict):
        return md, []
    names: dict[str, list[str]] = {}        # lineage -> its normalised name and aliases
    listed: dict[str, set[str]] = {}        # GENE -> lineages whose panel lists it
    specific: dict[str, set[str]] = {}      # GENE -> lineages it is a discriminator of
    symbols: set[str] = set()
    for types in reference["species"].values():
        for lineage, spec in types.items():
            names.setdefault(lineage, [_norm(lineage), *spec.get("aliases", [])])
            for g in [*spec.get("panel", []), *spec.get("discriminators", [])]:
                symbols.add(g)
                listed.setdefault(g.upper(), set()).add(lineage)
            for g in spec.get("discriminators", []):
                specific.setdefault(g.upper(), set()).add(lineage)
    for g, more in (reference.get("also_marks") or {}).items():
        listed.setdefault(str(g).upper(), set()).update(more)
    if not symbols:
        return md, []

    alt = "|".join(re.escape(s) for s in sorted(symbols, key=len, reverse=True))
    symbol = re.compile(rf"(?<![A-Za-z0-9-])(?:{alt})(?![A-Za-z0-9-])")
    gene = rf"[*_]{{0,2}}{symbol.pattern}[*_]{{0,2}}"
    genes = rf"(?P<genes>{gene}(?:(?:\s*,\s*(?:(?:and|or)\s+)?|\s*/\s*|\s+(?:and|or)\s+){gene})*)"
    is_marker = re.compile(
        genes + r"(?:\s+(?:is|are|was|were|remains?|serves?\s+as)\s+(?:(?:also|often|widely|commonly|"
        r"generally|usually|typically)\s+)?(?:(?:a|an|the|one\s+of\s+the)\s+)?|\s*[,(—–]\s*(?:a|an|the)\s+)"
        rf"(?P<pre>(?:{_WORD}\s+){{0,5}}?)markers?\b(?:\s+(?:of|for)\s+(?P<post>{_WORD}(?:\s+{_WORD}){{0,3}}))?")
    marks = re.compile(genes + rf"\s+(?:\w+ly\s+)?(?:marks|mark|labels|label)\s+(?P<post>{_WORD}(?:\s+{_WORD}){{0,3}})")

    found: dict[tuple[int, int], str] = {}
    for pattern in (is_marker, marks):
        for m in pattern.finditer(md):
            pre = m.groupdict().get("pre") or ""
            if _NOT_A_CLAIM.search(pre):
                continue
            phrases = [_PHRASE_END.split(p)[0] for p in (pre, m.group("post") or "")]
            if any(_STATE.search(p) for p in phrases):
                continue
            keys = [_norm(p) for p in phrases if p.strip()]
            claimed = {lin for lin, ns in names.items() for k in keys if any(n and n in k for n in ns)}
            if not claimed:
                continue
            wrong = [g for g in dict.fromkeys(symbol.findall(m.group("genes")))
                     if g.upper() in specific and not claimed & listed.get(g.upper(), set())]
            if not wrong:
                continue
            span = _sentence_span(md, m.start())
            found.setdefault(span, (
                f"marker claim: the report called {', '.join(wrong)} a {' / '.join(sorted(claimed))} "
                f"marker, but the curated {reference.get('tissue') or 'tissue'} reference lists "
                + "; ".join(f"{g} under {', '.join(sorted(specific[g.upper()]))}" for g in wrong)
                + f" — sentence removed: \"{md[span[0]:span[1]]}\""))
    for start, end in sorted(found, reverse=True):
        md = _cut(md, start, end)
    return md, list(found.values())
