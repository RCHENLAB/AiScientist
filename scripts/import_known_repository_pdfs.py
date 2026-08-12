#!/usr/bin/env python3
"""Import individually verified legal repository PDFs into the priority corpus."""

from __future__ import annotations

import csv
import json
import ssl
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pypdf import PdfReader


BASE = Path("output/retigene_papers/journal_priority")
MANIFEST = BASE / "retigene_priority_manifest.csv"
NEEDS = BASE / "needs_journal_or_uci_access.csv"
PDF_DIR = BASE / "papers_priority"
SUMMARY = BASE / "known_repository_recovery_summary.json"
PRIORITY_SUMMARY = BASE / "journal_priority_summary.json"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
SYSTEM_CA_BUNDLE = (
    Path.home() / "Library/Python/3.9/lib/python/site-packages/certifi/cacert.pem"
)
TLS_CONTEXT = ssl.create_default_context(
    cafile=str(SYSTEM_CA_BUNDLE) if SYSTEM_CA_BUNDLE.exists() else None
)

REPOSITORIES = {
    "15286153": {
        "label": "pmc_official_pdf",
        "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1735855/pdf/v041p00591.pdf",
    },
    "11527955": {
        "label": "citeseerx_public_fulltext",
        "url": (
            "https://citeseerx.ist.psu.edu/document?"
            "doi=6268f1d5d317bc8a6ea87338534ef9ee61085013&repid=rep1&type=pdf"
        ),
    },
    "36325687": {
        "label": "sage_publisher_open_access",
        "url": "https://journals.sagepub.com/doi/pdf/10.1177/11206721221136324",
    },
    "16912710": {
        "label": "author_publication_page",
        "url": "https://fatihozaltin.com/uploads/fck/file/61.pdf",
    },
    "20637498": {
        "label": "hal_open_archive",
        "url": "https://hal.science/hal-04982593/document",
    },
    "31077665": {
        "label": "ucl_discovery",
        "url": (
            "https://discovery.ucl.ac.uk/id/eprint/10074496/1/"
            "1-s2.0-S000293941930193X-main.pdf"
        ),
    },
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


def download_pdf(pmid: str, record: dict[str, str]) -> tuple[Path, int, int]:
    request = Request(
        record["url"],
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/pdf,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urlopen(request, timeout=90, context=TLS_CONTEXT) as response:
        data = response.read()
        content_type = response.headers.get("content-type", "")
    if not data.startswith(b"%PDF") or len(data) < 30_000:
        raise ValueError(f"invalid PDF response: {content_type}; bytes={len(data)}")

    target = PDF_DIR / f"PMID{pmid}_repository_{record['label']}.pdf"
    tmp = target.with_suffix(".pdf.part")
    tmp.write_bytes(data)
    tmp.replace(target)
    reader = PdfReader(target)
    if not reader.pages:
        target.unlink(missing_ok=True)
        raise ValueError("PDF contains no readable pages")
    text_chars = sum(len(page.extract_text() or "") for page in reader.pages)
    if text_chars < 3_000:
        target.unlink(missing_ok=True)
        raise ValueError(f"PDF has too little extractable text: {text_chars}")
    return target, len(reader.pages), text_chars


def main() -> int:
    rows, fields = read_csv(MANIFEST)
    by_pmid = {row.get("pmid", ""): row for row in rows}
    results: list[dict[str, object]] = []
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    for pmid, record in REPOSITORIES.items():
        row = by_pmid.get(pmid)
        if not row:
            results.append({"pmid": pmid, "status": "not_in_manifest"})
            continue
        if row.get("selected_status") == "selected":
            results.append({"pmid": pmid, "status": "already_selected"})
            continue
        try:
            target, pages, text_chars = download_pdf(pmid, record)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            results.append({"pmid": pmid, "status": "error", "note": str(exc)[:500]})
            print(f"PMID {pmid}: error: {exc}", flush=True)
            continue

        row["selected_source"] = "repository_open_access"
        row["selected_pdf"] = str(target)
        row["selected_status"] = "selected"
        row["priority_note"] = (
            f"Downloaded a verified legal repository PDF from {record['label']}; "
            f"pages={pages}; extracted text characters={text_chars}."
        )
        results.append(
            {
                "pmid": pmid,
                "status": "downloaded",
                "repository": record["label"],
                "url": record["url"],
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
        "known_repository_results": results,
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
