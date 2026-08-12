#!/usr/bin/env python3
"""Recover missing RetiGene PDFs from legal open-access API records.

The script queries OpenAlex and Semantic Scholar by DOI, attempts only PDF URLs
those services identify as open access, validates every response, and updates
the existing journal-priority manifest without replacing selected papers.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests


BASE = Path("output/retigene_papers/journal_priority")
MANIFEST = BASE / "retigene_priority_manifest.csv"
NEEDS = BASE / "needs_journal_or_uci_access.csv"
PDF_DIR = BASE / "papers_priority"
ATTEMPTS = BASE / "open_access_api_download_attempts.csv"
OPENALEX_CACHE = BASE / "openalex_cache.jsonl"
S2_CACHE = BASE / "semantic_scholar_cache.jsonl"
SUMMARY = BASE / "open_access_api_download_summary.json"
PRIORITY_SUMMARY = BASE / "journal_priority_summary.json"

USER_AGENT = "BioAgentPrototype/1.0 (legal open-access literature recovery)"
ATTEMPT_FIELDS = [
    "pmid",
    "doi",
    "source",
    "url",
    "final_url",
    "http_status",
    "content_type",
    "result",
    "note",
    "local_pdf",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--pmid", action="append")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def normalize_doi(value: str) -> str:
    return value.strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "")


def safe_name(value: str, limit: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return cleaned[:limit] or "paper"


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def append_attempts(rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    exists = ATTEMPTS.exists()
    with ATTEMPTS.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ATTEMPT_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def load_jsonl_cache(path: Path) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return cache
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            identifier = str(record.get("id") or "").strip().lower()
            if not identifier:
                doi = normalize_doi(str(record.get("doi") or ""))
                identifier = f"doi:{doi}" if doi else ""
            if identifier:
                cache[identifier] = record.get("data") or {}
    return cache


def append_jsonl(path: Path, identifier: str, data: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        record = {"id": identifier, "data": data}
        if identifier.startswith("doi:"):
            record["doi"] = identifier.removeprefix("doi:")
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def fetch_openalex(
    session: requests.Session,
    identifier: str,
    timeout: float,
) -> dict[str, Any]:
    if identifier.startswith("doi:"):
        external_id = f"https://doi.org/{quote(identifier.removeprefix('doi:'), safe='/')}"
    else:
        external_id = identifier
    url = f"https://api.openalex.org/works/{external_id}"
    response = session.get(url, params={"mailto": "maziyao@example.com"}, timeout=timeout)
    if response.status_code == 404:
        return {"not_found": True}
    response.raise_for_status()
    return response.json()


def fetch_semantic_scholar(
    session: requests.Session,
    identifiers: list[str],
    timeout: float,
) -> dict[str, dict[str, Any]]:
    if not identifiers:
        return {}
    url = "https://api.semanticscholar.org/graph/v1/paper/batch"
    response = session.post(
        url,
        params={"fields": "paperId,title,externalIds,isOpenAccess,openAccessPdf"},
        json={"ids": [identifier.upper() for identifier in identifiers]},
        timeout=timeout,
    )
    response.raise_for_status()
    output: dict[str, dict[str, Any]] = {}
    for identifier, item in zip(identifiers, response.json()):
        output[identifier] = item or {"not_found": True}
    return output


def openalex_pdf_urls(data: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    locations = list(data.get("locations") or [])
    best = data.get("best_oa_location")
    if isinstance(best, dict):
        locations.insert(0, best)
    for location in locations:
        url = str(location.get("pdf_url") or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def semantic_scholar_pdf_urls(data: dict[str, Any]) -> list[str]:
    location = data.get("openAccessPdf") or {}
    url = str(location.get("url") or "").strip()
    return [url] if url else []


def candidate_urls(
    identifier: str,
    openalex: dict[str, dict[str, Any]],
    semantic_scholar: dict[str, dict[str, Any]],
) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, urls in [
        ("openalex_open_access", openalex_pdf_urls(openalex.get(identifier, {}))),
        (
            "semantic_scholar_open_access",
            semantic_scholar_pdf_urls(semantic_scholar.get(identifier, {})),
        ),
    ]:
        for url in urls:
            if url not in seen:
                output.append((source, url))
                seen.add(url)
    return output


def download_candidate(
    session: requests.Session,
    row: dict[str, str],
    source: str,
    url: str,
    timeout: float,
    dry_run: bool,
) -> dict[str, str]:
    attempt = {
        "pmid": row.get("pmid", ""),
        "doi": row.get("doi", ""),
        "source": source,
        "url": url,
        "final_url": "",
        "http_status": "",
        "content_type": "",
        "result": "",
        "note": "",
        "local_pdf": "",
    }
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        attempt["result"] = "error"
        attempt["note"] = str(exc)[:500]
        return attempt

    data = response.content
    content_type = response.headers.get("content-type", "")
    attempt["final_url"] = response.url
    attempt["http_status"] = str(response.status_code)
    attempt["content_type"] = content_type
    if response.status_code >= 400:
        attempt["result"] = "http_error"
        attempt["note"] = f"HTTP {response.status_code}; bytes={len(data)}"
        return attempt
    # Tiny repository PDFs are commonly placeholder notices saying that the
    # full text is unavailable, not the paper itself.
    if not data.startswith(b"%PDF") or len(data) < 30000:
        attempt["result"] = "not_pdf"
        attempt["note"] = f"content-type={content_type}; bytes={len(data)}"
        return attempt

    target = PDF_DIR / (
        f"PMID{row['pmid']}_{safe_name(source)}_{safe_name(normalize_doi(row.get('doi', '')))}.pdf"
    )
    if not dry_run:
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".pdf.part")
        tmp.write_bytes(data)
        tmp.replace(target)
    attempt["result"] = "downloaded"
    attempt["note"] = f"validated open-access PDF; bytes={len(data)}"
    attempt["local_pdf"] = str(target)
    return attempt


def row_identifier(row: dict[str, str]) -> str:
    doi = normalize_doi(row.get("doi", ""))
    if doi:
        return f"doi:{doi}"
    pmid = row.get("pmid", "").strip()
    return f"pmid:{pmid}" if pmid else ""


def write_summaries(rows: list[dict[str, str]], run_counts: Counter[str]) -> None:
    needs_rows = [row for row in rows if row.get("selected_status") != "selected"]
    fields = list(rows[0].keys()) if rows else []
    write_csv(NEEDS, needs_rows, fields)
    summary = {
        "total_pmids": len(rows),
        "selected_count": len(rows) - len(needs_rows),
        "remaining_count": len(needs_rows),
        "priority_pdf_files": len(list(PDF_DIR.glob("*.pdf"))),
        "selected_source_counts": dict(sorted(Counter(row.get("selected_source", "") for row in rows).items())),
        "this_run_counts": dict(sorted(run_counts.items())),
    }
    SUMMARY.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    PRIORITY_SUMMARY.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def main() -> int:
    args = parse_args()
    rows, fields = read_csv(args.manifest)
    pmids = set(args.pmid or [])
    candidates = [
        row
        for row in rows
        if row.get("selected_status") != "selected"
        and row_identifier(row)
        and (not pmids or row.get("pmid") in pmids)
    ]
    if args.limit:
        candidates = candidates[: args.limit]

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/pdf,application/json,text/html"})
    identifiers = [row_identifier(row) for row in candidates]
    openalex_cache = load_jsonl_cache(OPENALEX_CACHE)
    s2_cache = load_jsonl_cache(S2_CACHE)
    run_counts: Counter[str] = Counter()

    missing_s2 = [identifier for identifier in identifiers if identifier not in s2_cache]
    for group in chunks(missing_s2, 100):
        try:
            fetched = fetch_semantic_scholar(session, group, args.timeout)
        except requests.RequestException as exc:
            run_counts["semantic_scholar_api_error"] += len(group)
            print(f"[semantic-scholar] {type(exc).__name__}: {exc}", flush=True)
            break
        for identifier, data in fetched.items():
            s2_cache[identifier] = data
            append_jsonl(S2_CACHE, identifier, data)
        time.sleep(args.delay)

    missing_openalex = [identifier for identifier in identifiers if identifier not in openalex_cache]
    for index, identifier in enumerate(missing_openalex, start=1):
        try:
            data = fetch_openalex(session, identifier, args.timeout)
        except requests.RequestException as exc:
            run_counts["openalex_api_error"] += 1
            print(f"[openalex] {identifier}: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(args.delay)
            continue
        openalex_cache[identifier] = data
        append_jsonl(OPENALEX_CACHE, identifier, data)
        if index % 25 == 0:
            print(f"[openalex] fetched {index}/{len(missing_openalex)}", flush=True)
        time.sleep(args.delay)

    updated = 0
    for index, row in enumerate(candidates, start=1):
        identifier = row_identifier(row)
        urls = candidate_urls(identifier, openalex_cache, s2_cache)
        if not urls:
            run_counts["no_open_pdf_url"] += 1
            continue
        success: dict[str, str] | None = None
        row_attempts: list[dict[str, str]] = []
        for source, url in urls:
            attempt = download_candidate(session, row, source, url, args.timeout, args.dry_run)
            row_attempts.append(attempt)
            run_counts[f"attempt_{attempt['result']}"] += 1
            if attempt["result"] == "downloaded":
                success = attempt
                break
            time.sleep(args.delay)
        append_attempts(row_attempts)
        if success and not args.dry_run:
            row["selected_source"] = success["source"]
            row["selected_pdf"] = success["local_pdf"]
            row["selected_status"] = "selected"
            row["priority_note"] = (
                f"Downloaded a validated legal open-access PDF listed by {success['source']}."
            )
            updated += 1
            run_counts["rows_updated"] += 1
        if index % 25 == 0:
            print(f"[download] processed {index}/{len(candidates)}; updated={updated}", flush=True)

    if not args.dry_run:
        write_csv(args.manifest, rows, fields)
        write_summaries(rows, run_counts)
    print(json.dumps({"processed": len(candidates), "updated": updated, "counts": run_counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
