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


_CALLER = re.compile(r"doublet[ _-]?finder|scdbl[ _-]?finder|(?<![A-Za-z])pann(?![A-Za-z])|\bdf[._]classifications?\b", re.I)
_DOUBLET = re.compile(r"\bdoublets?\b|doublet_|双细胞", re.I)
_DIAGNOSTIC = re.compile(
    r"scores?|(?<![A-Za-z])pann(?![A-Za-z])|agree(?:ment)?|disagree|concordance|compar|(?<![A-Za-z])kappa(?![A-Za-z])|κ|cohen|"
    r"second opinion|classifications?|\bcallers?\b|\bflags?\b|\bflagged\b|\boverlap\b|"
    r"比较|评分|一致性|分歧", re.I)
# A clause that names no caller is a doublet diagnostic only through these words, even under a
# doublet heading: never the generic "compared", "score", "flagged" or "overlap" that the next
# analysis, or another QC problem ("CellQC flagged S2 for low retention"), uses.
_DOUBLET_TERMS = re.compile(
    r"(?<![A-Za-z])pann(?![A-Za-z])|agree(?:ment)?|disagree|concordance|(?<![A-Za-z])kappa(?![A-Za-z])|κ|cohen|second opinion|\bcallers?\b|"
    r"doublet[ _-]?scores?|双细胞评分|一致性|分歧", re.I)
# What still refers to the doublet finding in a later sentence that does not mention doublets: the
# comparison's statistics and the callers. A bare "agreed" there usually starts another result.
_COMPARISON = re.compile(
    r"(?<![A-Za-z])pann(?![A-Za-z])|concordance|(?<![A-Za-z])kappa(?![A-Za-z])|κ|cohen|second opinion|\bcallers?\b|doublet[ _-]?scores?", re.I)
_UNRELATED = re.compile(
    r"(?<![A-Za-z])ISG(?![A-Za-z])|"
    r"\b(?:signature|annotation|annotator|inter.?rater|cell.?type|cluster|clustering)\b", re.I)
_REVIEW = re.compile(
    r"\bdisagree(?:ment|d|s)?\b|\bagree(?:ment)?\s+(?:(?:is|was)\s+)?poorly\b|"
    r"\b(?:poor|low|unreliable|unstable)\s+(?:caller\s+)?(?:agreement|concordance)\b|"
    r"\b(?:agreement|concordance)\s+(?:(?:is|was)\s+)?(?:poor|low|unreliable|unstable)\b", re.I)
_FAILURE = re.compile(r"\b(?:failed|failure|could not|did not run)\b", re.I)
_NUMBER = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?")
_QUALITATIVE_VALUE = re.compile(
    r"\bagree(?:d|s)?\s+(?:poorly|well)\b|\bdisagree(?:d|s)?\b|"
    r"\bdoublet[ _-]?scores?\s+(?:(?:is|are|was|were)\s+)?(?:higher|lower)\b", re.I)
_METRIC_VALUE = re.compile(
    r"(?<![A-Za-z])(?:scores?|scored|pann|kappa|κ|agreement|concordance|disagreements?|"
    r"agree(?:d|s)?|disagree(?:d|s)?|flags?|flagged|overlap|评分|一致性|分歧)(?![A-Za-z])"
    r"\s*(?:(?:[:=(\[]|of\b|on\b|at\b|was\b|were\b|is\b|are\b|mean\b|median\b|"
    r"standard error\b|ranged? from\b|increased to\b|decreased to\b|为|是)\s*)*"
    r"[-+]?\d+(?:[,.]\d+)*(?:e[-+]?\d+)?", re.I)
_COUNT = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
_REMOVED = re.compile(
    rf"(?:\b(?:doublets? removed|removed doublets?)\s*:\s*(?P<label>{_COUNT})"
    rf"|\b(?P<before>{_COUNT})\s+doublets?\s+(?:were\s+|was\s+)?removed"
    rf"|\bremoved\s+(?P<after>{_COUNT})\s+doublets?\b"
    rf"|移除(?:了)?\s*(?P<chinese>{_COUNT})\s*个?双细胞)", re.I)
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")
_IDENTIFIER = re.compile(r"^(?:sample(?:id)?|library|cell|barcode|dataset|index|id)$", re.I)
_GENERIC_REVIEW = "Doublet filtering needs review; see optional technical details."
_GENERIC_FAILURE = "Doublet filtering failed; see optional technical details for the failure."


def _without_links(text: str) -> str:
    # A download filename is a pointer, not a displayed result. Keep the link label available
    # for classification (a figure caption can itself discuss a diagnostic).
    text = re.sub(r"\]\([^)]*\)", "]", text)
    return re.sub(r"`[^`\n]*[/\\][^`\n]*`", "", text)


def _diagnostic(text: str, doublet_context: bool = False, nearby: bool = False) -> bool:
    if re.fullmatch(r"\s*(?:[-*+]\s+)?\[[^\]]+\]\([^)]*\)\s*\.?", text):
        return False  # A download link offers opt-in access; it does not show the values.
    images = re.findall(r"!\[([^\]]*)\]\(([^)]*)\)", text)
    for caption, path in images:
        # A normal QC figure can mention a doublet panel without being a caller comparison.
        # Only a dedicated diagnostic filename or a caption with diagnostic values is opt-in.
        filename = path.rsplit("/", 1)[-1]
        if (_CALLER.search(filename) or (_DOUBLET.search(filename) and _DOUBLET_TERMS.search(filename))
                or ((_CALLER.search(caption) or _DOUBLET.search(caption)) and _METRIC_VALUE.search(caption))):
            return True
    if images:
        return False
    text = _without_links(text)
    # Names, methods, removal counts and execution failures are not comparison results. A
    # diagnostic must actually state a value (including "agree poorly" / "scores were higher").
    valued = _METRIC_VALUE.search(text) or _QUALITATIVE_VALUE.search(text) or _REVIEW.search(text)
    if not valued:
        return False
    if _CALLER.search(text):
        return True
    if _DOUBLET.search(text) and _DOUBLET_TERMS.search(text):
        return True
    # A bare agreement/kappa belongs to doublets only under an explicit doublet heading or
    # within that sentence. Named unrelated results stay visible even in that section.
    if doublet_context and _DOUBLET_TERMS.search(text) and not _UNRELATED.search(text):
        return True
    return bool(nearby and _COMPARISON.search(text) and not _UNRELATED.search(text))


def asks_for_doublet_details(question: str) -> bool:
    """Does the request itself ask about the doublet calls (their scores, agreement or comparison)?
    Then they are the answer, not optional detail. Naming a caller as the decider does not ask."""
    return any((_DOUBLET.search(clause) or _CALLER.search(clause)) and _DIAGNOSTIC.search(clause)
               for clause in re.split(r"[.;,!?\n。！？；，]+|\bthen\b", question or ""))


def _review_needed(text: str) -> bool:
    if _REVIEW.search(text):
        return True
    text = text.replace("|", " ")  # Metric/Value Markdown rows: ``| Kappa | 0.10 |``.
    match = re.search(r"(?:kappa|κ)\s*(?:[=:]|of|was|is|为)?\s*(-?\d+(?:\.\d+)?)", text, re.I)
    return bool(match and float(match.group(1)) < 0.2)


def _counts(text: str) -> list[str]:
    """Salvage only explicitly stated removals, never infer them from caller flags/scores."""
    prefix = re.match(r"^\s*[-*+]?\s*([\w.-]+):\s+", text)
    counts = []
    for match in _REMOVED.finditer(text):
        count = next(v for v in match.groups() if v is not None)
        tail = text[match.end():]
        sample = re.match(r"\s+(?:in|from)\s+([\w.-]+)", tail, re.I)
        label = sample.group(1).rstrip(".") if sample else prefix.group(1) if prefix else ""
        counts.append((f"{label}: " if label else "") + f"Doublets removed: {count}.")
        if not sample:
            continue
        tail = tail[sample.end():]
        # Only the coordinated removal list belongs to this count. Stop before a new clause
        # such as "leaving 880 in S1"; those are retained cells, not further removals.
        while more := re.match(
                rf"\s*(?:,\s*(?:and\s+)?|and\s+)({_COUNT})\s+(?:doublets?\s+)?"
                r"(?:in|from)\s+([\w.-]+)", tail, re.I):
            counts.append(f"{more.group(2).rstrip('.')}: Doublets removed: {more.group(1)}.")
            tail = tail[more.end():]
    return list(dict.fromkeys(counts))


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _metric_field(text: str, context: bool, *, caller_table: bool = False) -> bool:
    """A table's field names have no values themselves; scope them by the table's callers."""
    if _FAILURE.search(text) or re.search(r"\bstatus\b", text, re.I):
        return False
    if _CALLER.search(text):
        return True
    if _UNRELATED.search(text):
        return False
    return bool((_DOUBLET.search(text) and _DOUBLET_TERMS.search(text))
                or (context and _DOUBLET_TERMS.search(text))
                or (caller_table and re.fullmatch(r"scores?", text, re.I)))


def _low_kappa(value: str) -> bool:
    try:
        return float(value.strip()) < 0.2
    except ValueError:
        return False


def _table(lines: list[str], context: bool) -> tuple[list[str], bool, bool]:
    """Filter diagnostic columns/metric rows, keeping other measurements in a mixed table."""
    header = _cells(lines[0])
    caller_table = any(_CALLER.search(name) for name in header)
    context = context or caller_table
    keep = [i for i, name in enumerate(header) if not _metric_field(name, context, caller_table=caller_table)]
    changed = len(keep) != len(header)
    kappa_columns = [i for i, name in enumerate(header) if i not in keep
                     and re.search(r"kappa|κ", name, re.I)]
    needs_review = any(_low_kappa(values[i]) for line in lines[2:] if (values := _cells(line))
                       for i in kappa_columns if i < len(values))
    if not keep or (changed and all(_IDENTIFIER.fullmatch(header[i]) for i in keep)):
        return [], True, needs_review
    rows = []
    metric_rows = bool(header and re.fullmatch(r"metric|measure(?:ment)?|statistic", header[0], re.I))
    for line in lines[2:]:
        values = _cells(line)
        remaining = [values[i] if i < len(values) else "" for i in keep]
        if remaining and (_FAILURE.search(remaining[0]) or re.search(r"\bstatus\b", remaining[0], re.I)):
            rows.append(remaining)
            continue
        # A Metric/Value table carries diagnostic names in rows instead of columns.
        if ((metric_rows and remaining and _metric_field(remaining[0], context))
                or _diagnostic(" ".join(remaining), context)):
            changed = True
            needs_review |= _review_needed(" ".join(remaining))
            continue
        rows.append(remaining)
    if not changed:
        return lines, False, False
    if not rows:
        return [], True, needs_review
    display = [[header[i] for i in keep], ["---"] * len(keep), *rows]
    return ["| " + " | ".join(row) + " |" for row in display], True, needs_review


def _clauses(line: str) -> list[str]:
    """Split prose clauses, keeping an image or download link structurally intact."""
    chunks = re.split(r"(!?\[[^\]]*\]\([^)]*\))", line)
    return [clause for chunk in chunks for clause in (
        [chunk] if re.fullmatch(r"!?\[[^\]]*\]\([^)]*\)", chunk) else
        re.split(r"(?<=[.;!?])\s+|(?<=[。！？；])\s*|,\s+(?=(?:whereas|while|but|and)\s)", chunk))]


def _fenced_metric_values(lines: list[str], context: bool) -> tuple[bool, bool]:
    """Associate fenced CSV/TSV values with their field, not with arbitrary later digits."""
    import csv

    language = lines[0].lstrip()[3:].strip().lower()
    if language not in {"csv", "tsv"}:
        return False, False
    try:
        rows = list(csv.reader(lines[1:-1], delimiter="\t" if language == "tsv" else ","))
    except csv.Error:
        return False, False
    if len(rows) < 2:
        return False, False
    caller_table = any(_CALLER.search(name) for name in rows[0])
    context = context or caller_table
    fields = [(i, name) for i, name in enumerate(rows[0])
              if _metric_field(name, context, caller_table=caller_table)]
    values = [(name, row[i]) for row in rows[1:] for i, name in fields
              if i < len(row) and _NUMBER.fullmatch(row[i].strip())]
    return bool(values), any(re.search(r"kappa|κ", name, re.I) and _low_kappa(value)
                             for name, value in values)


def default_qc_text(text: str, *, salvage_counts: bool = True,
                    keep: "re.Pattern[str] | None" = None) -> QCDisplayResult:
    """Return the default narrative and any doublet-specific draft details moved out of it.

    Known caller names and explicit doublet diagnostic language establish scope. A bare score
    or kappa outside that scope is never removed. Markdown tables are filtered by column/row;
    prose is filtered by sentence/clause so independent scientific findings survive. This does
    not parse or alter the analysis files. Consumers should keep ``details`` in an optional
    artifact and may append authoritative QC counts from the tool record; a consumer that has them
    passes ``salvage_counts=False``, since a count in a removed clause may be another caller's.
    A section whose heading matches ``keep`` is left as written, down to the next heading of its
    level or above: code writes it from the run's records.
    """
    if not text:
        return QCDisplayResult(text)
    lines = text.splitlines()
    output: list[str] = []
    details: list[str] = []
    needs_review = False
    failed = False
    doublet_level: int | None = None
    keep_level: int | None = None
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
            if keep_level is not None and level <= keep_level:
                keep_level = None
            if keep is not None and keep.search(title):
                keep_level = level
            elif _DOUBLET.search(title) or _CALLER.search(title):
                doublet_level = level
        if keep_level is not None or re.match(r"^\s*>?\s*\*\*Untested code\.\*\*", line):
            output.append(line)
            i += 1
            continue
        context = doublet_level is not None
        if not line.strip():
            paragraph_context = False

        if i + 1 < len(lines) and "|" in line and _SEPARATOR.fullmatch(lines[i + 1]):
            end = i + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            block = lines[i:end]
            filtered, changed, table_review = _table(block, context)
            output.extend(filtered)
            if changed:
                original = "\n".join(block)
                details.append(original)
                needs_review |= table_review
            i = end
            continue

        if re.match(r"^\s*(```|~~~)", line):
            fence = line.lstrip()[:3]
            end = i + 1
            while end < len(lines) and not lines[end].lstrip().startswith(fence):
                end += 1
            if end == len(lines):
                # A truncated opener must not turn every later section into one diagnostic block.
                # Drop the unmatched marker and process the remaining lines as ordinary prose.
                details.append(line)
                i += 1
                continue
            end += 1
            block = "\n".join(lines[i:end])
            fenced_values, fenced_review = _fenced_metric_values(lines[i:end], context)
            if fenced_values or _diagnostic(block, context):
                details.append(block)
                needs_review |= fenced_review or _review_needed(block)
                failed |= bool(_FAILURE.search(block))
            else:
                output.extend(lines[i:end])
            i = end
            continue

        # Keep independent clauses in mixed prose. Decimal points are not followed by a space.
        clauses = _clauses(line)
        kept: list[str] = []
        sentence_context = paragraph_context
        removed = False
        for clause in clauses:
            if not clause:
                continue
            diagnostic = _diagnostic(clause, context, sentence_context)
            # With a QC record, caller removal claims are replaced by its authoritative counts.
            # Without one, keep the original sentence with all sample names and counts intact.
            if not salvage_counts and _CALLER.search(clause) and (
                    _REMOVED.search(clause) or re.search(rf"\bremoved\s*:?\s*{_COUNT}\b", clause, re.I)):
                diagnostic = True
            if diagnostic:
                removed = True
                needs_review |= _review_needed(clause)
                failed |= bool(_FAILURE.search(clause))
                if salvage_counts:
                    kept.extend(_counts(clause))
                # A following bare "kappa was 0.10" still refers to this finding, including
                # across sentence/line boundaries in the same paragraph.
                sentence_context = True
            else:
                kept.append(clause)
                if _DOUBLET.search(clause):
                    sentence_context = True
                elif _UNRELATED.search(clause):
                    sentence_context = False
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
