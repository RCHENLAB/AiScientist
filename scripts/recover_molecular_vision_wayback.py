#!/usr/bin/env python3
"""Recover Molecular Vision PDFs from archived copies of the public journal site."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pypdf import PdfReader


BASE = Path("output/retigene_papers/journal_priority")
MANIFEST = BASE / "retigene_priority_manifest.csv"
NEEDS = BASE / "needs_journal_or_uci_access.csv"
PDF_DIR = BASE / "papers_priority"
SUMMARY = BASE / "molecular_vision_wayback_recovery_summary.json"
PRIORITY_SUMMARY = BASE / "journal_priority_summary.json"
USER_AGENT = "BioAgentPrototype/1.0 (Molecular Vision archive recovery)"

ARTICLES = {
    "16163268": "v11/a83",
    "16280978": "v11/a110",
    "16636652": "v12/a39",
    "17110911": "v12/a145",
    "17167404": "v12/a168",
}

FALLBACK_ORIGINALS = {
    "16636652": (
        "https://www.zora.uzh.ch/id/eprint/38712/1/"
        "v12a39-kloeckener-gruissem.pdf"
    ),
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


def fetch(url: str, accept: str, timeout: float = 90) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers.get("content-type", "")


def query_archive(original_pattern: str) -> list[dict[str, str]]:
    params = urlencode(
        [
            ("url", original_pattern),
            ("output", "json"),
            ("filter", "statuscode:200"),
            ("filter", "mimetype:application/pdf"),
            ("collapse", "digest"),
        ]
    )
    data, _ = fetch(f"https://web.archive.org/cdx/search/cdx?{params}", "application/json")
    records = json.loads(data)
    if len(records) < 2:
        return []
    fields = records[0]
    return [dict(zip(fields, record)) for record in records[1:]]


def find_archived_pdf(pmid: str, article_path: str) -> tuple[str, str, str]:
    candidates = query_archive(f"www.molvis.org/molvis/{article_path}/*")
    if not candidates and pmid in FALLBACK_ORIGINALS:
        candidates = query_archive(FALLBACK_ORIGINALS[pmid])
    if not candidates:
        raise ValueError("Internet Archive has no archived PDF for this article")
    candidate = max(candidates, key=lambda record: record["timestamp"])
    timestamp = candidate["timestamp"]
    original = candidate["original"]
    archive_url = f"https://web.archive.org/web/{timestamp}id_/{original}"
    return archive_url, timestamp, original


def download_pdf(pmid: str, article_path: str) -> tuple[Path, int, int, str, str]:
    archive_url, timestamp, original = find_archived_pdf(pmid, article_path)
    data, content_type = fetch(archive_url, "application/pdf")
    if not data.startswith(b"%PDF") or len(data) < 30_000:
        raise ValueError(f"invalid archived PDF: {content_type}; bytes={len(data)}")

    article_id = article_path.replace("/", "_")
    target = PDF_DIR / f"PMID{pmid}_molecular_vision_wayback_{article_id}.pdf"
    tmp = target.with_suffix(".pdf.part")
    tmp.write_bytes(data)
    tmp.replace(target)
    reader = PdfReader(target)
    if not reader.pages:
        target.unlink(missing_ok=True)
        raise ValueError("archived PDF contains no readable pages")
    text_chars = sum(len(page.extract_text() or "") for page in reader.pages)
    if text_chars < 3_000:
        target.unlink(missing_ok=True)
        raise ValueError(f"archived PDF has too little extractable text: {text_chars}")
    return target, len(reader.pages), text_chars, timestamp, original


def main() -> int:
    rows, fields = read_csv(MANIFEST)
    by_pmid = {row.get("pmid", ""): row for row in rows}
    results: list[dict[str, object]] = []
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    for pmid, article_path in ARTICLES.items():
        row = by_pmid.get(pmid)
        if not row:
            results.append({"pmid": pmid, "status": "not_in_manifest"})
            continue
        if row.get("selected_status") == "selected":
            results.append({"pmid": pmid, "status": "already_selected"})
            continue
        try:
            target, pages, text_chars, timestamp, original = download_pdf(pmid, article_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            results.append({"pmid": pmid, "status": "error", "note": str(exc)[:500]})
            print(f"PMID {pmid}: error: {exc}", flush=True)
            continue

        row["selected_source"] = "molecular_vision_wayback"
        row["selected_pdf"] = str(target)
        row["selected_status"] = "selected"
        row["priority_note"] = (
            "Recovered from an Internet Archive snapshot of the original public Molecular "
            f"Vision PDF; article={article_path}; snapshot={timestamp}; pages={pages}; "
            f"extracted text characters={text_chars}."
        )
        results.append(
            {
                "pmid": pmid,
                "article": article_path,
                "status": "downloaded",
                "snapshot": timestamp,
                "original_url": original,
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
        "molecular_vision_wayback_results": results,
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
