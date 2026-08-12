#!/usr/bin/env python3
"""Build a journal-first RetiGene paper corpus.

Source priority:
  1. Official journal/publisher PDF when a direct OA publisher PDF is available,
     or when a PDF was manually obtained through UCI/publisher access.
  2. Existing PMC / NCBI OA PDF fallback.
  3. Mark as needing UCI/publisher/manual retrieval.

This script does not mass-scrape paywalled publisher websites. It only downloads
direct open-access publisher PDF URLs discovered through Unpaywall, then falls
back to the existing PMC OA corpus.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


BASE = Path("output/retigene_papers")
MANIFEST = BASE / "retigene_paper_manifest.csv"
OUT_DIR = BASE / "journal_priority"
PRIORITY_PDF_DIR = OUT_DIR / "papers_priority"
UNPAYWALL_CACHE = OUT_DIR / "unpaywall_cache.jsonl"
PRIORITY_MANIFEST = OUT_DIR / "retigene_priority_manifest.csv"
NEEDS_MANUAL = OUT_DIR / "needs_journal_or_uci_access.csv"
SUMMARY = OUT_DIR / "journal_priority_summary.json"
EMAIL = "<contact@your-institution.edu>"
USER_AGENT = f"BioAgentPrototype RetiGene journal-priority corpus ({EMAIL})"


EXTRA_FIELDS = [
    "unpaywall_is_oa",
    "unpaywall_best_host",
    "journal_landing_url",
    "journal_pdf_url",
    "selected_source",
    "selected_pdf",
    "selected_status",
    "priority_note",
]


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("_")[:80] or "paper"


def read_manifest() -> tuple[list[dict[str, str]], list[str]]:
    with MANIFEST.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    return rows, fields


def read_cache() -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    if not UNPAYWALL_CACHE.exists():
        return cache
    with UNPAYWALL_CACHE.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            doi = item.get("doi")
            if doi:
                cache[doi.lower()] = item
    return cache


def append_cache(doi: str, data: dict[str, Any]) -> None:
    UNPAYWALL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    record = {"doi": doi.lower(), "data": data}
    with UNPAYWALL_CACHE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def fetch_unpaywall(doi: str) -> dict[str, Any]:
    url = "https://api.unpaywall.org/v2/" + urllib.parse.quote(doi, safe="") + "?" + urllib.parse.urlencode(
        {"email": EMAIL}
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_unpaywall(doi: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    key = doi.lower()
    if key in cache:
        return cache[key]["data"]
    try:
        data = fetch_unpaywall(doi)
    except Exception as exc:  # noqa: BLE001 - network/API failures become manifest notes.
        data = {"error": str(exc)}
    cache[key] = {"doi": key, "data": data}
    append_cache(doi, data)
    time.sleep(0.15)
    return data


def find_publisher_urls(data: dict[str, Any]) -> tuple[str, str, str, str]:
    if data.get("error"):
        return "", "", "", ""
    best = data.get("best_oa_location") or {}
    best_host = best.get("host_type") or ""
    landing = ""
    pdf = ""
    for loc in data.get("oa_locations") or []:
        if loc.get("host_type") != "publisher":
            continue
        if not landing:
            landing = loc.get("url") or ""
        if loc.get("url_for_pdf"):
            pdf = loc["url_for_pdf"]
            break
    if not landing and best.get("host_type") == "publisher":
        landing = best.get("url") or ""
        pdf = best.get("url_for_pdf") or ""
    return str(data.get("is_oa", "")), best_host, landing, pdf


def request_bytes(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read(), resp.headers.get("content-type", "")


def looks_like_pdf(data: bytes, content_type: str) -> bool:
    return data.startswith(b"%PDF") or "pdf" in content_type.lower()


def download_journal_pdf(row: dict[str, str], pdf_url: str) -> tuple[str, str]:
    pmid = row["pmid"]
    doi = safe_name(row.get("doi") or "journal")
    target = PRIORITY_PDF_DIR / f"PMID{pmid}_journal_{doi}.pdf"
    if target.exists() and target.stat().st_size > 1024:
        return str(target), "journal official PDF already exists"
    try:
        data, content_type = request_bytes(pdf_url)
        if not looks_like_pdf(data[:2048], content_type):
            return "", f"journal PDF URL did not return a PDF; content-type={content_type}; bytes={len(data)}"
        tmp = target.with_suffix(".pdf.part")
        tmp.write_bytes(data)
        tmp.replace(target)
        return str(target), "downloaded direct publisher/journal OA PDF"
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        return "", f"journal PDF download failed: {exc}"


def copy_existing_pdf(row: dict[str, str], source_label: str) -> tuple[str, str]:
    src = Path(row.get("local_pdf") or "")
    if not src.exists():
        return "", "existing local_pdf path is missing"
    pmid = row["pmid"]
    target = PRIORITY_PDF_DIR / f"PMID{pmid}_{source_label}_{safe_name(src.name)}"
    if target.exists() and target.stat().st_size > 1024:
        return str(target), f"{source_label} PDF already exists in priority corpus"
    shutil.copy2(src, target)
    return str(target), f"copied {source_label} PDF into priority corpus"


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    rows, original_fields = read_manifest()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PRIORITY_PDF_DIR.mkdir(parents=True, exist_ok=True)
    cache = read_cache()

    output_rows: list[dict[str, str]] = []
    for idx, row in enumerate(rows, start=1):
        row = dict(row)
        for field in EXTRA_FIELDS:
            row[field] = ""

        doi = row.get("doi", "").strip()
        if doi:
            data = get_unpaywall(doi, cache)
            is_oa, best_host, landing, pdf_url = find_publisher_urls(data)
            row["unpaywall_is_oa"] = is_oa
            row["unpaywall_best_host"] = best_host
            row["journal_landing_url"] = landing
            row["journal_pdf_url"] = pdf_url

        # Manual UCI/publisher PDFs are already the best source.
        if row.get("status") == "downloaded_institutional_access":
            selected, note = copy_existing_pdf(row, "journal_uci")
            row["selected_source"] = "journal_uci_manual"
            row["selected_pdf"] = selected
            row["selected_status"] = "selected" if selected else "needs_recheck"
            row["priority_note"] = note
        elif row.get("journal_pdf_url"):
            selected, note = download_journal_pdf(row, row["journal_pdf_url"])
            if selected:
                row["selected_source"] = "journal_publisher_oa"
                row["selected_pdf"] = selected
                row["selected_status"] = "selected"
                row["priority_note"] = note
            elif row.get("downloaded") == "Yes":
                selected, fallback_note = copy_existing_pdf(row, "pmc_fallback")
                row["selected_source"] = "pmc_oa_fallback"
                row["selected_pdf"] = selected
                row["selected_status"] = "selected" if selected else "needs_recheck"
                row["priority_note"] = f"{note}; fallback: {fallback_note}"
            else:
                row["selected_source"] = "none"
                row["selected_status"] = "needs_uci_or_manual"
                row["priority_note"] = note
        elif row.get("downloaded") == "Yes":
            selected, note = copy_existing_pdf(row, "pmc_fallback")
            row["selected_source"] = "pmc_oa_fallback"
            row["selected_pdf"] = selected
            row["selected_status"] = "selected" if selected else "needs_recheck"
            row["priority_note"] = note
        else:
            row["selected_source"] = "none"
            row["selected_status"] = "needs_uci_or_manual"
            if doi:
                row["priority_note"] = "No direct publisher OA PDF found; try journal website through UCI/publisher access."
            else:
                row["priority_note"] = "No DOI available in metadata; needs manual source lookup."

        output_rows.append(row)
        if idx % 100 == 0:
            print(f"[priority] processed {idx}/{len(rows)}", flush=True)

    fields = original_fields + [f for f in EXTRA_FIELDS if f not in original_fields]
    write_csv(PRIORITY_MANIFEST, output_rows, fields)
    write_csv(NEEDS_MANUAL, [r for r in output_rows if r["selected_status"] != "selected"], fields)

    counts = Counter(r["selected_source"] for r in output_rows)
    counts.update({f"selected_status:{k}": v for k, v in Counter(r["selected_status"] for r in output_rows).items()})
    counts["total_pmids"] = len(output_rows)
    counts["priority_pdf_files"] = len(list(PRIORITY_PDF_DIR.glob("*.pdf")))
    SUMMARY.write_text(json.dumps(dict(sorted(counts.items())), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[output] {PRIORITY_MANIFEST}", flush=True)
    print(f"[output] {NEEDS_MANUAL}", flush=True)
    print(f"[output] {SUMMARY}", flush=True)
    print(SUMMARY.read_text(), flush=True)


if __name__ == "__main__":
    main()
