"""Keep doublet diagnostics out of the default narrative without changing the run record.

This is a presentation boundary, not a fact checker. Removed draft text is returned so a caller
can preserve it as optional technical detail. The tool results and downloadable data stay intact.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class QCDisplayResult:
    text: str
    details: tuple[str, ...] = ()
    needs_review: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.details)


_CALLER = re.compile(r"doublet[ _-]?finder|scdbl[ _-]?finder|\bpann\b|\bdf[._]classifications?\b", re.I)
_DOUBLET = re.compile(r"\bdoublets?\b|doublet_|双细胞", re.I)
_DIAGNOSTIC = re.compile(
    r"scores?|\bpann\b|agree(?:ment)?|disagree|concordance|compar|\bkappa\b|κ|cohen|"
    r"second opinion|classifications?|\bcallers?\b|\bflags?\b|\bflagged\b|\boverlap\b|"
    r"比较|评分|一致性|分歧", re.I)
_UNRELATED = re.compile(
    r"(?<![A-Za-z])ISG(?![A-Za-z])|"
    r"\b(?:signature|annotation|annotator|inter.?rater|cell.?type|cluster|clustering)\b", re.I)
_REVIEW = re.compile(r"poor|\blow\b|disagree|unreliable|unstable|uncertain|failed|failure|error", re.I)
_FAILURE = re.compile(r"\b(?:failed|failure|error|could not|did not run)\b", re.I)
_COUNT = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
_REMOVED = re.compile(
    rf"(?:\b(?:doublets? removed|removed doublets?)\s*:\s*(?P<label>{_COUNT})"
    rf"|\b(?P<before>{_COUNT})\s+doublets?\s+(?:were\s+|was\s+)?removed"
    rf"|\bremoved\s+(?P<after>{_COUNT})\s+doublets?\b)", re.I)
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")
_IDENTIFIER = re.compile(r"^(?:sample(?:id)?|library|cell|barcode|dataset|index|id)$", re.I)
_GENERIC_REVIEW = "Doublet filtering needs review; see optional technical details."
_GENERIC_FAILURE = "Doublet filtering failed; see optional technical details for the failure."


def _without_links(text: str) -> str:
    # A download filename is a pointer, not a displayed result. Keep the link label available
    # for classification (a figure caption can itself discuss a diagnostic).
    text = re.sub(r"\]\([^)]*\)", "]", text)
    return re.sub(r"`[^`\n]*[/\\][^`\n]*`", "", text)


def _diagnostic(text: str, doublet_context: bool = False) -> bool:
    if re.fullmatch(r"\s*(?:[-*+]\s+)?\[[^\]]+\]\([^)]*\)\s*\.?", text):
        return False  # A download link offers opt-in access; it does not show the values.
    for image in re.findall(r"!\[[^\]]*\]\([^)]*\)", text):
        # Unlike a download link, an image puts the data directly in the default result.
        if _CALLER.search(image) or (_DOUBLET.search(image) and _DIAGNOSTIC.search(image)):
            return True
    text = _without_links(text)
    if _CALLER.search(text):
        # A caller's failure is an execution warning, not a comparison to hide. Preserve the
        # method and failure reason unless this clause also expands scores/agreement.
        if _FAILURE.search(text) and not _DIAGNOSTIC.search(text):
            return False
        return True
    if _DOUBLET.search(text) and _DIAGNOSTIC.search(text):
        return True
    # A bare agreement/score belongs to doublets only under an explicit doublet heading or
    # within that sentence. Named unrelated results stay visible even in that section.
    return bool(doublet_context and _DIAGNOSTIC.search(text) and not _UNRELATED.search(text))


def _review_needed(text: str) -> bool:
    if _REVIEW.search(text):
        return True
    text = text.replace("|", " ")  # Metric/Value Markdown rows: ``| Kappa | 0.10 |``.
    match = re.search(r"(?:kappa|κ)\s*(?:[=:]|of|was|is)?\s*(-?\d+(?:\.\d+)?)", text, re.I)
    return bool(match and float(match.group(1)) < 0.2)


def _counts(text: str) -> list[str]:
    """Salvage only explicitly stated removals, never infer them from caller flags/scores."""
    return list(dict.fromkeys(
        f"Doublets removed: {next(v for v in match.groups() if v is not None)}."
        for match in _REMOVED.finditer(text)))


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _table(lines: list[str], context: bool) -> tuple[list[str], bool]:
    """Filter diagnostic columns/metric rows, keeping other measurements in a mixed table."""
    header = _cells(lines[0])
    keep = [i for i, name in enumerate(header) if not _diagnostic(name, context)]
    changed = len(keep) != len(header)
    if not keep or (changed and all(_IDENTIFIER.fullmatch(header[i]) for i in keep)):
        return [], True
    rows = []
    for line in lines[2:]:
        values = _cells(line)
        remaining = [values[i] if i < len(values) else "" for i in keep]
        # A Metric/Value table carries diagnostic names in rows instead of columns.
        if any(_diagnostic(value, context) for value in remaining):
            changed = True
            continue
        rows.append(remaining)
    if not changed:
        return lines, False
    if not rows:
        return [], True
    display = [[header[i] for i in keep], ["---"] * len(keep), *rows]
    return ["| " + " | ".join(row) + " |" for row in display], True


def default_qc_text(text: str) -> QCDisplayResult:
    """Return the default narrative and any doublet-specific draft details moved out of it.

    Known caller names and explicit doublet diagnostic language establish scope. A bare score
    or kappa outside that scope is never removed. Markdown tables are filtered by column/row;
    prose is filtered by sentence/clause so independent scientific findings survive. This does
    not parse or alter the analysis files. Consumers should keep ``details`` in an optional
    artifact and may append authoritative QC counts from the tool record.
    """
    if not text:
        return QCDisplayResult(text)
    lines = text.splitlines()
    output: list[str] = []
    details: list[str] = []
    needs_review = False
    failed = False
    doublet_level: int | None = None
    paragraph_context = False
    i = 0
    while i < len(lines):
        line = lines[i]
        heading = re.match(r"^\s*(#{1,6})\s+(.+)$", line)
        if heading:
            paragraph_context = False
            level, title = len(heading.group(1)), heading.group(2)
            if doublet_level is not None and level <= doublet_level:
                doublet_level = None
            if _DOUBLET.search(title) or _CALLER.search(title):
                doublet_level = level
        context = doublet_level is not None
        if not line.strip():
            paragraph_context = False

        if i + 1 < len(lines) and "|" in line and _SEPARATOR.fullmatch(lines[i + 1]):
            end = i + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            block = lines[i:end]
            filtered, changed = _table(block, context)
            output.extend(filtered)
            if changed:
                original = "\n".join(block)
                details.append(original)
                needs_review |= _review_needed(original)
            i = end
            continue

        if re.match(r"^\s*(```|~~~)", line):
            fence = line.lstrip()[:3]
            end = i + 1
            while end < len(lines) and not lines[end].lstrip().startswith(fence):
                end += 1
            end = min(end + 1, len(lines))
            block = "\n".join(lines[i:end])
            if _diagnostic(block, context):
                details.append(block)
                needs_review |= _review_needed(block)
                failed |= bool(_FAILURE.search(block))
            else:
                output.extend(lines[i:end])
            i = end
            continue

        # Keep independent clauses in mixed prose. Decimal points are not followed by a space.
        clauses = re.split(r"(?<=[.;!?])\s+|(?<=[。！？；])\s*|,\s+(?=(?:whereas|while|but|and)\s)", line)
        kept: list[str] = []
        sentence_context = context or paragraph_context
        removed = False
        for clause in clauses:
            if not clause:
                continue
            diagnostic = _diagnostic(clause, sentence_context)
            if diagnostic:
                removed = True
                needs_review |= _review_needed(clause)
                failed |= bool(_FAILURE.search(clause))
                kept.extend(_counts(clause))
                # A following bare "kappa was 0.10" still refers to this finding, including
                # across sentence/line boundaries in the same paragraph.
                sentence_context = True
            else:
                kept.append(clause)
                if _DOUBLET.search(clause):
                    sentence_context = True
                elif _UNRELATED.search(clause):
                    sentence_context = context
        paragraph_context = sentence_context if line.strip() else False
        if removed:
            details.append(line)
            if kept:
                output.append(" ".join(kept))
        else:
            output.append(line)
        i += 1

    if not details:
        return QCDisplayResult(text)
    rendered = "\n".join(output).strip()
    if failed and _GENERIC_FAILURE not in rendered:
        rendered += "\n\n" + _GENERIC_FAILURE
    elif needs_review and "Doublet filtering needs review" not in rendered:
        rendered += "\n\n" + _GENERIC_REVIEW
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    if text.endswith("\n"):
        rendered += "\n"
    return QCDisplayResult(rendered, tuple(dict.fromkeys(details)), needs_review)
