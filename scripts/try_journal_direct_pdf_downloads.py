#!/usr/bin/env python3
"""Try direct official journal PDF URLs for the RetiGene priority corpus.

This is a conservative downloader:
- It only uses official publisher/journal URL patterns.
- It validates that the response is a real PDF.
- It records every attempt in a CSV.
- It updates the journal-priority manifest when a better journal PDF is found.

It does not use unofficial mirrors and does not bypass interactive login pages.
"""

from __future__ import annotations

import csv
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path


BASE = Path("output/retigene_papers/journal_priority")
MANIFEST = BASE / "retigene_priority_manifest.csv"
NEEDS = BASE / "needs_journal_or_uci_access.csv"
PDF_DIR = BASE / "papers_priority"
ATTEMPTS = BASE / "journal_direct_download_attempts.csv"
SUMMARY = BASE / "journal_direct_download_summary.json"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


ATTEMPT_FIELDS = [
    "pmid",
    "doi",
    "journalTitle",
    "publisher_pattern",
    "url",
    "result",
    "note",
    "local_pdf",
]


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("_")[:90] or "paper"


def sciencedirect_pii_from_doi(doi: str) -> str:
    suffix = doi.split("/", 1)[1] if "/" in doi else ""
    if not suffix.lower().startswith("s"):
        return ""
    # 10.1016/s0021-9258(18)42199-0 -> S0021925818421990
    pii = re.sub(r"[^A-Za-z0-9]", "", suffix).upper()
    return pii if len(pii) >= 12 else ""


def bmj_urls_from_doi(doi: str) -> list[tuple[str, str]]:
    suffix = doi.split("/", 1)[1] if "/" in doi else ""
    match = re.fullmatch(r"([a-z]+)\.(\d+)\.(\d+)\.([A-Za-z0-9]+)", suffix.lower())
    if not match:
        return []
    code, volume, issue, page = match.groups()
    domain_map = {
        "bjo": "bjo.bmj.com",
        "jmg": "jmg.bmj.com",
        "jnnp": "jnnp.bmj.com",
        "adc": "adc.bmj.com",
        "gut": "gut.bmj.com",
    }
    domain = domain_map.get(code, f"{code}.bmj.com")
    return [("bmj_dot_doi", f"https://{domain}/content/{volume}/{issue}/{page}.full.pdf")]


def candidate_urls(row: dict[str, str]) -> list[tuple[str, str]]:
    doi = (row.get("doi") or "").strip().lower()
    if not doi:
        return []
    prefix = doi.split("/", 1)[0]
    urls: list[tuple[str, str]] = []
    if prefix == "10.1002":
        urls.append(("wiley", f"https://onlinelibrary.wiley.com/doi/pdf/{doi}"))
    elif prefix == "10.1007":
        urls.append(("springer", f"https://link.springer.com/content/pdf/{doi}.pdf"))
    elif prefix == "10.1080" or prefix == "10.3109":
        urls.append(("taylor_francis", f"https://www.tandfonline.com/doi/pdf/{doi}?download=true"))
    elif prefix == "10.1073":
        urls.append(("pnas", f"https://www.pnas.org/doi/pdf/{doi}"))
    elif prefix == "10.1056":
        urls.append(("nejm", f"https://www.nejm.org/doi/pdf/{doi}"))
    elif prefix == "10.1126":
        urls.append(("science", f"https://www.science.org/doi/pdf/{doi}"))
    elif prefix == "10.1136":
        urls.extend(bmj_urls_from_doi(doi))
    elif prefix == "10.1016":
        pii = sciencedirect_pii_from_doi(doi)
        if pii:
            urls.append(("sciencedirect_pii", f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft"))
    return urls


def download_pdf(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=75) as resp:
        return resp.read(), resp.headers.get("content-type", "")


def looks_like_pdf(data: bytes, content_type: str) -> bool:
    return data.startswith(b"%PDF") or ("pdf" in content_type.lower() and b"<html" not in data[:2048].lower())


def try_row(row: dict[str, str]) -> list[dict[str, str]]:
    attempts: list[dict[str, str]] = []
    urls = candidate_urls(row)
    if not urls:
        return attempts
    pmid = row["pmid"]
    doi = row.get("doi", "")
    for pattern, url in urls:
        attempt = {
            "pmid": pmid,
            "doi": doi,
            "journalTitle": row.get("journalTitle", ""),
            "publisher_pattern": pattern,
            "url": url,
            "result": "",
            "note": "",
            "local_pdf": "",
        }
        target = PDF_DIR / f"PMID{pmid}_journal_direct_{pattern}_{safe_name(doi)}.pdf"
        if target.exists() and target.stat().st_size > 1024:
            attempt["result"] = "downloaded"
            attempt["note"] = "already exists"
            attempt["local_pdf"] = str(target)
            attempts.append(attempt)
            break
        try:
            data, content_type = download_pdf(url)
            if not looks_like_pdf(data[:4096], content_type):
                attempt["result"] = "not_pdf"
                attempt["note"] = f"content-type={content_type}; bytes={len(data)}"
                attempts.append(attempt)
                continue
            tmp = target.with_suffix(".pdf.part")
            tmp.write_bytes(data)
            tmp.replace(target)
            attempt["result"] = "downloaded"
            attempt["note"] = f"official publisher PDF; content-type={content_type}; bytes={len(data)}"
            attempt["local_pdf"] = str(target)
            attempts.append(attempt)
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            attempt["result"] = "error"
            attempt["note"] = str(exc)[:500]
            attempts.append(attempt)
        time.sleep(0.25)
    return attempts


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    rows, fields = read_csv(MANIFEST)
    all_attempts: list[dict[str, str]] = []
    updated = 0
    for idx, row in enumerate(rows, start=1):
        # Skip rows already backed by manual/journal publisher PDF.
        if row.get("selected_source") in {"journal_uci_manual", "journal_publisher_oa", "journal_direct_uci_or_publisher"}:
            continue
        attempts = try_row(row)
        all_attempts.extend(attempts)
        success = next((a for a in attempts if a["result"] == "downloaded"), None)
        if success:
            row["selected_source"] = "journal_direct_uci_or_publisher"
            row["selected_pdf"] = success["local_pdf"]
            row["selected_status"] = "selected"
            row["priority_note"] = f"Downloaded official journal PDF using {success['publisher_pattern']} URL pattern."
            row["journal_pdf_url"] = success["url"]
            updated += 1
        if idx % 100 == 0:
            print(f"[journal-direct] scanned {idx}/{len(rows)}; new/updated journal PDFs {updated}", flush=True)

    write_csv(MANIFEST, rows, fields)
    write_csv(NEEDS, [r for r in rows if r.get("selected_status") != "selected"], fields)
    write_csv(ATTEMPTS, all_attempts, ATTEMPT_FIELDS)

    counts = Counter(r.get("selected_source", "") for r in rows)
    counts.update({f"selected_status:{k}": v for k, v in Counter(r.get("selected_status", "") for r in rows).items()})
    counts.update({f"attempt:{k}": v for k, v in Counter(a["result"] for a in all_attempts).items()})
    counts["total_pmids"] = len(rows)
    counts["new_or_updated_journal_direct"] = updated
    counts["priority_pdf_files"] = len(list(PDF_DIR.glob("*.pdf")))
    SUMMARY.write_text(json.dumps(dict(sorted(counts.items())), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[output] {MANIFEST}", flush=True)
    print(f"[output] {NEEDS}", flush=True)
    print(f"[output] {ATTEMPTS}", flush=True)
    print(SUMMARY.read_text(), flush=True)


if __name__ == "__main__":
    main()
