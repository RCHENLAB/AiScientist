"""Presentation policy shared by final-result writers; analysis and audit data stay complete."""

import re

QC_SUMMARY_POLICY = (
    "Default QC presentation: report the overall QC outcome and a concise count of doublets removed. "
    "Keep per-cell doublet scores and doublet caller comparisons (their agreement, kappa and "
    "disagreement counts) out "
    "of the default final result, including its prose, tables and figure discussion. Do not repeat "
    "these details from step narratives, tool warnings or table previews. Keep them in the "
    "downloadable QC files or optional technical details; include a technical-details section in "
    "the final result only when the user explicitly asks for those details. A request for a QC "
    "report alone does not ask for caller comparisons. Preserve material QC warnings in concise "
    "plain language: if doublet removal needs review, say so and point to available technical "
    "details without naming or quantifying the caller comparison. Do not hide failed QC steps, "
    "excluded libraries or other QC problems. Name only detail files actually available in the "
    "run (for example tables/cellqc_metrics.csv or CellQC's result/ files); never invent a file. "
)

# Match run_cellqc's warning threshold; the underlying metric stays in the audit/downloads.
_DOUBLET_KAPPA_LOW = 0.2


def doublet_count_lines(lib: dict) -> list[str]:
    """Default display: only the number removed, never caller scores or comparisons."""
    dbl, filt = lib.get("doublets_removed"), lib.get("cells_after_filter")
    if not isinstance(dbl, (int, float)):
        return []
    return [f"Doublets removed: {int(dbl):,}"
            + (f" of the {int(filt):,} filtered cells ({100 * dbl / filt:.1f}%)"
               if isinstance(filt, (int, float)) and filt else "") + "."]


def qc_warnings(result: dict) -> list[str]:
    """Summarize CellQC's known comparison warning; preserve every other warning intact."""
    def review(sample: str) -> str:
        return (f"{sample}: Doublet filtering needs review; see the technical details "
                "in the Downloads panel.")

    warnings = []
    for raw in result.get("warnings") or []:
        warning = re.sub(r"\s*Report it\.?\s*$", "", str(raw)).strip()
        if not warning:
            continue
        match = re.fullmatch(
            r"(.+?): the two doublet callers agree poorly \(kappa [^)]+\)"
            r"(?:, so the choice of decider drives which cells are removed)?\.?", warning)
        warnings.append(review(match.group(1)) if match else warning)
    # Some saved results carry the metric but no warning. Still flag the QC concern once.
    for lib in result.get("libraries") or []:
        if not isinstance(lib, dict):
            continue
        kappa = lib.get("doublet_kappa")
        if isinstance(kappa, (int, float)) and kappa < _DOUBLET_KAPPA_LOW:
            note = review(str(lib.get("sample") or "Library"))
            if note not in warnings:
                warnings.append(note)
    return warnings


def qc_result_summary(result: dict) -> str:
    """A concise result from QC records when a stopped run must not call a writer model."""
    parts = []
    before, after = result.get("cells_before"), result.get("cells_after")
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        parts.append(f"{int(after):,} of {int(before):,} cells kept.")
    elif isinstance(after, (int, float)):
        parts.append(f"{int(after):,} cells kept.")
    if result.get("skipped"):
        parts.append("QC step skipped; no cells removed here.")
    libs = [lib for lib in result.get("libraries") or [] if isinstance(lib, dict)]
    if libs:
        for lib in libs:
            parts.extend(f"{lib.get('sample') or 'Library'}: {line}" for line in doublet_count_lines(lib))
    else:
        parts.extend(doublet_count_lines(result))
        # The standalone doublet tool reports predictions separately from actual filtering.
        if result.get("step") == "doublets" and result.get("filtered") is not None \
                and isinstance(before, (int, float)) and isinstance(after, (int, float)):
            parts.append(f"Doublets removed: {int(before - after):,}.")
    excluded = result.get("excluded_libraries") or []
    if excluded:
        parts.append("Excluded libraries: " + ", ".join(str(sample) for sample in excluded) + ".")
    parts.extend(qc_warnings(result))
    if result.get("warning"):
        parts.append(str(result["warning"]))
    return " ".join(parts) or "QC results are recorded in the run's technical details."
