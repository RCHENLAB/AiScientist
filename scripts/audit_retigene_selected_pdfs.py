#!/usr/bin/env python3
"""Audit every selected RetiGene PDF for completeness, identity, and provenance."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

from pypdf import PdfReader


DEFAULT_BASE = Path("output/retigene_papers/journal_priority")
PMC_SOURCES = {
    "europepmc_fulltext",
    "pmc_html_reconstructed",
    "pmc_oa_fallback",
    "pmc_scan_reconstructed+ocr",
}
REPOSITORY_SOURCES = {
    "ncbi_bookshelf",
    "openalex_open_access",
    "repository_open_access",
    "semantic_scholar_open_access",
}
TITLE_STOPWORDS = {
    "and",
    "are",
    "for",
    "from",
    "into",
    "the",
    "their",
    "through",
    "using",
    "with",
}
AUTHOR_MANUSCRIPT_PATTERNS = (
    "author manuscript",
    "accepted manuscript",
    "nih public access",
    "publisher's disclaimer",
    "publishers disclaimer",
    "manuscript accepted for publication",
)
SUPPLEMENT_PATTERNS = (
    "supp. figure",
    "supp. table",
    "supp figure",
    "supp table",
    "supplementary material",
    "supplemental material",
    "supplementary information",
    "supporting information",
    "online supplementary",
)
SEVERE_FLAGS = {
    "BAD_HEADER",
    "EMPTY_PDF",
    "FIGURE_OR_TABLE_ONLY",
    "MISSING",
    "NOT_VERSION_OF_RECORD",
    "SUPPLEMENT_ONLY",
    "TITLE_MISMATCH",
    "UNREADABLE",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse rows already saved in the report when PMID and selected path still match.",
    )
    return parser.parse_args()


def resolve_manifest_path(value: str, cwd: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (cwd / path).resolve()


def normalize_words(value: str) -> set[str]:
    words = {
        word
        for word in re.findall(r"[a-z0-9]+", value.lower())
        if len(word) >= 3 and word not in TITLE_STOPWORDS
    }
    return words


def title_overlap(title: str, text: str) -> float | None:
    expected = normalize_words(title)
    if not expected:
        return None
    observed = normalize_words(text)
    return round(len(expected & observed) / len(expected), 3)


def normalize_doi(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return re.sub(r"[^a-z0-9]", "", value)


def source_class(source: str) -> str:
    if source in PMC_SOURCES:
        return "pmc_or_europepmc"
    if source in REPOSITORY_SOURCES:
        return "repository_or_bookshelf"
    return "publisher_or_journal"


def extract_pdf(path: Path) -> tuple[int, str, str, int]:
    reader = PdfReader(path)
    page_text: list[str] = []
    identity_text: list[str] = []
    extraction_errors = 0
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - malformed font metadata can break text only.
            extraction_errors += 1
            text = ""
        page_text.append(text)
        if index < 3:
            identity_text.append(text)
    return (
        len(reader.pages),
        "\n".join(page_text),
        "\n".join(identity_text),
        extraction_errors,
    )


def audit_row(row: dict[str, str], cwd: Path) -> dict[str, str]:
    path = resolve_manifest_path(row.get("selected_pdf", ""), cwd)
    source = row.get("selected_source", "")
    result = {
        "pmid": row.get("pmid", ""),
        "title": row.get("title", ""),
        "doi": row.get("doi", ""),
        "journal": row.get("journalTitle", ""),
        "selected_source": source,
        "source_class": source_class(source),
        "file": path.name,
        "path": str(path),
        "size_bytes": "",
        "pages": "",
        "text_chars": "",
        "title_overlap": "",
        "doi_in_text": "",
        "author_manuscript_marker": "",
        "text_extraction_errors": "",
        "flags": "",
        "status": "",
    }
    flags: list[str] = []
    if not path.exists():
        flags.append("MISSING")
        result["flags"] = "|".join(flags)
        result["status"] = "fail"
        return result

    result["size_bytes"] = str(path.stat().st_size)
    try:
        if path.read_bytes()[:4] != b"%PDF":
            flags.append("BAD_HEADER")
        pages, full_text, identity_text, extraction_errors = extract_pdf(path)
    except Exception as exc:  # noqa: BLE001 - record malformed PDFs without stopping the audit.
        flags.append("UNREADABLE")
        result["flags"] = "|".join(flags)
        result["status"] = "fail"
        result["text_chars"] = f"{type(exc).__name__}: {exc}"
        return result

    compact_text = re.sub(r"\s+", " ", full_text).strip()
    compact_identity = re.sub(r"\s+", " ", identity_text).strip()
    lowered_identity = compact_identity.lower()
    overlap = title_overlap(row.get("title", ""), compact_identity)
    doi = normalize_doi(row.get("doi", ""))
    doi_in_text = bool(doi and doi in normalize_doi(compact_identity))
    author_marker = any(pattern in lowered_identity for pattern in AUTHOR_MANUSCRIPT_PATTERNS)
    supplement_marker = any(pattern in lowered_identity[:3000] for pattern in SUPPLEMENT_PATTERNS)
    starts_as_figure = bool(
        re.match(
            r"^(?:(?:supp\.?|supplement(?:ary|al)?)\s+)?"
            r"(?:figure|fig\.?|table)\s*(?:s?\d+|one)\b",
            lowered_identity,
        )
    )

    result["pages"] = str(pages)
    result["text_chars"] = str(len(compact_text))
    result["title_overlap"] = "" if overlap is None else str(overlap)
    result["doi_in_text"] = str(doi_in_text).lower()
    result["author_manuscript_marker"] = str(author_marker).lower()
    result["text_extraction_errors"] = str(extraction_errors)

    if pages == 0:
        flags.append("EMPTY_PDF")
    if extraction_errors:
        flags.append("TEXT_EXTRACTION_WARNING")
    if pages == 1:
        flags.append("ONE_PAGE")
    if len(compact_text) < 1000:
        flags.append("LOW_TEXT")
    if pages > 1 and len(compact_text) < pages * 100:
        flags.append("IMAGE_SCAN_OR_WEAK_OCR")
    if author_marker:
        flags.append("NOT_VERSION_OF_RECORD")

    identity_mismatch = overlap is not None and overlap < 0.35 and not doi_in_text
    if starts_as_figure and pages <= 2 and identity_mismatch:
        flags.append("FIGURE_OR_TABLE_ONLY")
    if supplement_marker and identity_mismatch:
        flags.append("SUPPLEMENT_ONLY")
    elif identity_mismatch and len(compact_text) >= 1000:
        flags.append("TITLE_MISMATCH")

    result["flags"] = "|".join(flags)
    result["status"] = "fail" if SEVERE_FLAGS & set(flags) else ("review" if flags else "pass")
    return result


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    cwd = Path.cwd()
    base = args.base.resolve()
    manifest = (args.manifest or base / "retigene_priority_manifest.csv").resolve()
    report = (args.report or base / "corpus_sanity_check_latest.csv").resolve()
    summary_path = (args.summary or base / "corpus_sanity_check_summary.json").resolve()

    with manifest.open(newline="", encoding="utf-8") as handle:
        selected = [
            row for row in csv.DictReader(handle) if row.get("selected_status") == "selected"
        ]

    existing: dict[tuple[str, str], dict[str, str]] = {}
    if args.resume and report.exists():
        with report.open(newline="", encoding="utf-8") as handle:
            for saved in csv.DictReader(handle):
                existing[(saved.get("pmid", ""), saved.get("path", ""))] = saved

    results: list[dict[str, str]] = []
    for index, row in enumerate(selected, start=1):
        selected_path = str(resolve_manifest_path(row.get("selected_pdf", ""), cwd))
        saved = existing.get((row.get("pmid", ""), selected_path))
        results.append(saved if saved is not None else audit_row(row, cwd))
        if index % 100 == 0 or index == len(selected):
            print(f"[audit] {index}/{len(selected)}", flush=True)
            write_csv(report, results)

    flag_counts: Counter[str] = Counter()
    for result in results:
        flag_counts.update(flag for flag in result["flags"].split("|") if flag)

    summary = {
        "manifest": str(manifest),
        "selected_count": len(selected),
        "unique_selected_paths": len({result["path"] for result in results}),
        "status_counts": dict(sorted(Counter(result["status"] for result in results).items())),
        "source_class_counts": dict(
            sorted(Counter(result["source_class"] for result in results).items())
        ),
        "selected_source_counts": dict(
            sorted(Counter(result["selected_source"] for result in results).items())
        ),
        "flag_counts": dict(sorted(flag_counts.items())),
        "possible_author_manuscripts": [
            {
                "pmid": result["pmid"],
                "source": result["selected_source"],
                "file": result["file"],
            }
            for result in results
            if result["author_manuscript_marker"] == "true"
        ],
        "failed": [
            {
                "pmid": result["pmid"],
                "flags": result["flags"],
                "file": result["file"],
            }
            for result in results
            if result["status"] == "fail"
        ],
        "needs_review": [
            {
                "pmid": result["pmid"],
                "flags": result["flags"],
                "file": result["file"],
            }
            for result in results
            if result["status"] == "review"
        ],
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary["status_counts"], indent=2))
    print(f"[output] {report}")
    print(f"[output] {summary_path}")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
