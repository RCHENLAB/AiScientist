#!/usr/bin/env python3
"""Download and import official NCBI Bookshelf PDFs for GeneReviews rows."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pypdf import PdfReader


BASE = Path("output/retigene_papers/journal_priority")
MANIFEST = BASE / "retigene_priority_manifest.csv"
NEEDS = BASE / "needs_journal_or_uci_access.csv"
PDF_DIR = BASE / "papers_priority"
SUMMARY = BASE / "ncbi_bookshelf_recovery_summary.json"
PRIORITY_SUMMARY = BASE / "journal_priority_summary.json"
USER_AGENT = "BioAgentPrototype/1.0 (NCBI Bookshelf literature recovery)"

BOOKS = {
    "20301292": "NBK1113",
    "20301500": "NBK1325",
    "20301537": "NBK1363",
    "20301590": "NBK1417",
    "27336129": "NBK368475",
}


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def download_pdf(pmid: str, book_id: str) -> tuple[Path, int, int]:
    url = f"https://www.ncbi.nlm.nih.gov/books/{book_id}/pdf/Bookshelf_{book_id}.pdf"
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf"})
    with urlopen(request, timeout=90) as response:
        data = response.read()
        content_type = response.headers.get("content-type", "")
    if not data.startswith(b"%PDF") or len(data) < 30_000:
        raise ValueError(f"invalid PDF response: {content_type}; bytes={len(data)}")

    target = PDF_DIR / f"PMID{pmid}_ncbi_bookshelf_{book_id}.pdf"
    tmp = target.with_suffix(".pdf.part")
    tmp.write_bytes(data)
    tmp.replace(target)
    reader = PdfReader(target)
    if not reader.pages:
        target.unlink(missing_ok=True)
        raise ValueError("PDF contains no readable pages")
    text_chars = sum(len(page.extract_text() or "") for page in reader.pages)
    if text_chars < 1_000:
        target.unlink(missing_ok=True)
        raise ValueError(f"PDF has too little extractable text: {text_chars} characters")
    return target, len(reader.pages), text_chars


def main() -> int:
    rows, fields = read_csv(MANIFEST)
    by_pmid = {row.get("pmid", ""): row for row in rows}
    results: list[dict[str, object]] = []
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    for pmid, book_id in BOOKS.items():
        row = by_pmid.get(pmid)
        if not row:
            results.append({"pmid": pmid, "status": "not_in_manifest"})
            continue
        if row.get("selected_status") == "selected":
            results.append({"pmid": pmid, "status": "already_selected"})
            continue
        try:
            target, pages, text_chars = download_pdf(pmid, book_id)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            results.append({"pmid": pmid, "status": "error", "note": str(exc)[:500]})
            print(f"PMID {pmid}: error: {exc}", flush=True)
            continue

        row["selected_source"] = "ncbi_bookshelf"
        row["selected_pdf"] = str(target)
        row["selected_status"] = "selected"
        row["priority_note"] = (
            f"Downloaded from the official NCBI Bookshelf GeneReviews PDF; "
            f"book={book_id}; pages={pages}; extracted text characters={text_chars}."
        )
        results.append(
            {
                "pmid": pmid,
                "book_id": book_id,
                "status": "downloaded",
                "pages": pages,
                "text_characters": text_chars,
                "pdf": str(target),
            }
        )
        print(f"PMID {pmid}: {pages} pages", flush=True)

    write_csv(MANIFEST, rows, fields)
    needs_rows = [row for row in rows if row.get("selected_status") != "selected"]
    write_csv(NEEDS, needs_rows, fields)
    summary = {
        "total_pmids": len(rows),
        "selected_count": len(rows) - len(needs_rows),
        "remaining_count": len(needs_rows),
        "priority_pdf_files": len(list(PDF_DIR.glob("*.pdf"))),
        "selected_source_counts": dict(
            sorted(Counter(row.get("selected_source", "") for row in rows).items())
        ),
        "ncbi_bookshelf_results": results,
    }
    SUMMARY.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    PRIORITY_SUMMARY.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "downloaded": sum(result["status"] == "downloaded" for result in results),
                "remaining": len(needs_rows),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
