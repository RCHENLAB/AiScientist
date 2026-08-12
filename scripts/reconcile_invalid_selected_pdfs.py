#!/usr/bin/env python3
"""Remove unreadable PDFs from the selected corpus and repair resolvable paths."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from pypdf import PdfReader


BASE = Path("output/retigene_papers/journal_priority")
MANIFEST = BASE / "retigene_priority_manifest.csv"
NEEDS = BASE / "needs_journal_or_uci_access.csv"
VALIDATION = BASE / "corpus_validation_summary.json"
SUMMARY = BASE / "invalid_selected_reconciliation_summary.json"
PRIORITY_SUMMARY = BASE / "journal_priority_summary.json"


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def valid_pdf(path: Path) -> bool:
    try:
        return path.exists() and path.read_bytes()[:4] == b"%PDF" and bool(PdfReader(path).pages)
    except (OSError, ValueError):
        return False


def main() -> int:
    rows, fields = read_csv(MANIFEST)
    by_pmid = {row.get("pmid", ""): row for row in rows}
    validation = json.loads(VALIDATION.read_text(encoding="utf-8"))
    repaired_paths: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []

    for pmid, recorded_path in validation.get("missing", []):
        row = by_pmid.get(str(pmid))
        if not row:
            continue
        candidate = BASE / "papers_priority" / Path(recorded_path).name
        if valid_pdf(candidate):
            row["selected_pdf"] = str(candidate)
            repaired_paths.append({"pmid": str(pmid), "pdf": str(candidate)})
            continue
        rejected.append({"pmid": str(pmid), "reason": "selected PDF is missing"})

    invalid_by_pmid = {
        str(pmid): error for pmid, _path, error in validation.get("invalid", [])
    }
    for pmid, error in invalid_by_pmid.items():
        row = by_pmid.get(pmid)
        if not row or row.get("selected_status") != "selected":
            continue
        rejected.append({"pmid": pmid, "reason": error})

    for item in rejected:
        row = by_pmid[item["pmid"]]
        old_note = row.get("priority_note", "").strip()
        validation_note = (
            "Rejected during corpus validation because the selected PDF is unreadable or missing; "
            f"reason={item['reason']}. The original file was retained for diagnosis."
        )
        row["selected_status"] = "needs_journal_or_uci_access"
        row["priority_note"] = f"{old_note} {validation_note}".strip()

    write_csv(MANIFEST, rows, fields)
    needs_rows = [row for row in rows if row.get("selected_status") != "selected"]
    write_csv(NEEDS, needs_rows, fields)
    selected_rows = [row for row in rows if row.get("selected_status") == "selected"]
    summary = {
        "total_pmids": len(rows),
        "selected_count": len(selected_rows),
        "remaining_count": len(needs_rows),
        "repaired_path_count": len(repaired_paths),
        "rejected_count": len(rejected),
        "repaired_paths": repaired_paths,
        "rejected": rejected,
        "selected_source_counts": dict(
            sorted(Counter(row.get("selected_source", "") for row in selected_rows).items())
        ),
    }
    SUMMARY.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    PRIORITY_SUMMARY.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: summary[key] for key in [
        "selected_count", "remaining_count", "repaired_path_count", "rejected_count"
    ]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
