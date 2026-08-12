#!/usr/bin/env python3
"""Download and import signed Europe PMC PDF URLs captured in the browser."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pypdf import PdfReader


BASE = Path("output/retigene_papers/journal_priority")
URLS = BASE / "pmc_browser_capture" / "europepmc_pdf_urls.json"
MANIFEST = BASE / "retigene_priority_manifest.csv"
NEEDS = BASE / "needs_journal_or_uci_access.csv"
PDF_DIR = BASE / "papers_priority"
SUMMARY = BASE / "europepmc_pdf_recovery_summary.json"
PRIORITY_SUMMARY = BASE / "journal_priority_summary.json"
USER_AGENT = "BioAgentPrototype/1.0 (Europe PMC literature recovery)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urls", type=Path, default=URLS)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--pmid", action="append")
    return parser.parse_args()


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def download_pdf(record: dict, timeout: float) -> tuple[Path, int, int]:
    url = str(record.get("pdfUrl") or "")
    if "/fulltextRepo?" not in url:
        raise ValueError("record does not contain a signed Europe PMC PDF URL")
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/pdf"},
    )
    with urlopen(request, timeout=timeout) as response:
        data = response.read()
        content_type = response.headers.get("content-type", "")
    if not data.startswith(b"%PDF") or len(data) < 30_000:
        raise ValueError(f"invalid PDF response: {content_type}; bytes={len(data)}")

    pmid = str(record["pmid"])
    pmcid = str(record["pmcid"])
    target = PDF_DIR / f"PMID{pmid}_europepmc_fulltext_{pmcid}.pdf"
    tmp = target.with_suffix(".pdf.part")
    tmp.write_bytes(data)
    tmp.replace(target)
    reader = PdfReader(target)
    if not reader.pages:
        target.unlink(missing_ok=True)
        raise ValueError("PDF contains no readable pages")
    text_chars = sum(len(page.extract_text() or "") for page in reader.pages)
    return target, len(reader.pages), text_chars


def write_summaries(rows: list[dict[str, str]], results: list[dict[str, object]]) -> None:
    needs_rows = [row for row in rows if row.get("selected_status") != "selected"]
    fields = list(rows[0].keys()) if rows else []
    write_csv(NEEDS, needs_rows, fields)
    summary = {
        "total_pmids": len(rows),
        "selected_count": len(rows) - len(needs_rows),
        "remaining_count": len(needs_rows),
        "priority_pdf_files": len(list(PDF_DIR.glob("*.pdf"))),
        "selected_source_counts": dict(
            sorted(Counter(row.get("selected_source", "") for row in rows).items())
        ),
        "europepmc_results": results,
    }
    SUMMARY.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    PRIORITY_SUMMARY.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    args = parse_args()
    records = json.loads(args.urls.read_text(encoding="utf-8"))
    records = [record for record in records if record.get("status") == "pdf_url"]
    if args.pmid:
        requested_pmids = set(args.pmid)
        records = [record for record in records if str(record.get("pmid")) in requested_pmids]
    rows, fields = read_csv(args.manifest)
    by_pmid = {row.get("pmid", ""): row for row in rows}
    results: list[dict[str, object]] = []
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    for index, record in enumerate(records, start=1):
        pmid = str(record.get("pmid") or "")
        row = by_pmid.get(pmid)
        if not row or row.get("pmcid") != record.get("pmcid"):
            results.append({"pmid": pmid, "status": "metadata_mismatch"})
            continue
        if row.get("selected_status") == "selected":
            results.append({"pmid": pmid, "status": "already_selected"})
            continue
        try:
            target, pages, text_chars = download_pdf(record, args.timeout)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            results.append({"pmid": pmid, "status": "error", "note": str(exc)[:500]})
            print(f"[{index}/{len(records)}] PMID {pmid}: error: {exc}", flush=True)
            continue

        row["selected_source"] = "europepmc_fulltext"
        row["selected_pdf"] = str(target)
        row["selected_status"] = "selected"
        row["priority_note"] = (
            f"Downloaded from Europe PMC's signed full-text repository; "
            f"pages={pages}; extracted text characters={text_chars}."
        )
        results.append(
            {
                "pmid": pmid,
                "pmcid": record["pmcid"],
                "status": "downloaded",
                "pages": pages,
                "text_characters": text_chars,
                "pdf": str(target),
            }
        )
        print(f"[{index}/{len(records)}] PMID {pmid}: {pages} pages", flush=True)

    write_csv(args.manifest, rows, fields)
    write_summaries(rows, results)
    print(
        json.dumps(
            {
                "records": len(records),
                "downloaded": sum(result["status"] == "downloaded" for result in results),
                "remaining": sum(row.get("selected_status") != "selected" for row in rows),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
