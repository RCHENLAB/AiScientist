#!/usr/bin/env python3
"""Capture complete PMC article HTML for remaining manifest rows."""

from __future__ import annotations

import csv
import json
import re
import time
from html import unescape
from pathlib import Path

import requests


BASE = Path("output/retigene_papers/journal_priority")
MANIFEST = BASE / "retigene_priority_manifest.csv"
CAPTURE_DIR = BASE / "pmc_browser_capture" / "europepmc_html"
METADATA = BASE / "pmc_browser_capture" / "http_pmc_capture_metadata.json"
USER_AGENT = "BioAgentPrototype/1.0 (PMC literature recovery; contact: local research use)"


def extract_article(page: str) -> str:
    main_match = re.search(r"<main\b", page, flags=re.IGNORECASE)
    start_at = main_match.start() if main_match else 0
    start = re.search(r"<article\b", page[start_at:], flags=re.IGNORECASE)
    if not start:
        return ""
    article_start = start_at + start.start()
    depth = 0
    token_re = re.compile(r"</?article\b[^>]*>", flags=re.IGNORECASE)
    for token in token_re.finditer(page, article_start):
        if token.group(0).lower().startswith("</article"):
            depth -= 1
            if depth == 0:
                return page[article_start : token.end()]
        else:
            depth += 1
    return ""


def text_length(article: str) -> int:
    without_tags = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", article, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", without_tags)
    return len(" ".join(unescape(text).split()))


def main() -> int:
    with MANIFEST.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rows = [
        row
        for row in rows
        if row.get("selected_status") != "selected" and row.get("pmcid", "").strip()
    ]
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html"})
    captures: list[dict[str, object]] = []

    for index, row in enumerate(rows, start=1):
        pmid = row["pmid"]
        pmcid = row["pmcid"].strip()
        url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
        record: dict[str, object] = {
            "pmid": pmid,
            "pmcid": pmcid,
            "title": row.get("title", ""),
            "doi": row.get("doi", ""),
            "journalTitle": row.get("journalTitle", ""),
            "pubYear": row.get("pubYear", ""),
            "url": url,
            "scanPages": [],
        }
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
            article = extract_article(response.text)
            chars = text_length(article)
            if len(article.encode("utf-8")) < 5_000 or chars < 5_000:
                raise ValueError(
                    f"article HTML is incomplete: bytes={len(article.encode('utf-8'))}; chars={chars}"
                )
            path = CAPTURE_DIR / f"PMID{pmid}_{pmcid}_ncbi_http.html"
            path.write_text(article, encoding="utf-8")
            record.update(
                {
                    "status": "captured",
                    "htmlPath": str(path),
                    "htmlBytes": path.stat().st_size,
                    "textChars": chars,
                }
            )
            print(f"[{index}/{len(rows)}] PMID {pmid}: captured {chars} chars", flush=True)
        except (requests.RequestException, OSError, ValueError) as exc:
            record.update({"status": "error", "note": str(exc)[:500]})
            print(f"[{index}/{len(rows)}] PMID {pmid}: error: {exc}", flush=True)
        captures.append(record)
        time.sleep(0.15)

    METADATA.write_text(json.dumps(captures, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "processed": len(captures),
                "captured": sum(record.get("status") == "captured" for record in captures),
                "metadata": str(METADATA),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
