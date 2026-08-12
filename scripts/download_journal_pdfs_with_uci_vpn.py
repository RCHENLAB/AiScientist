#!/usr/bin/env python3
"""Download journal and legal repository PDFs using the current network/VPN session.

This script is intentionally conservative:
- It only tries official publisher/journal URLs from the existing manifest, DOI
  resolver pages, well-known official publisher PDF URL patterns, or repository
  locations recorded by Unpaywall.
- It validates that the response is a real PDF before saving it.
- It records every attempt, including login/HTML responses.
- It updates the journal-priority manifest when a better journal PDF is found.

It does not use unofficial mirrors and does not handle passwords or 2FA. If a
publisher needs an interactive UCI login page, the row is marked with the HTML
response details instead of being forced.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html.parser
import json
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests


BASE = Path("output/retigene_papers/journal_priority")
MANIFEST = BASE / "retigene_priority_manifest.csv"
NEEDS = BASE / "needs_journal_or_uci_access.csv"
PDF_DIR = BASE / "papers_priority"
ATTEMPTS = BASE / "journal_uci_vpn_download_attempts.csv"
SUMMARY = BASE / "journal_uci_vpn_download_summary.json"
PRIORITY_SUMMARY = BASE / "journal_priority_summary.json"
UNPAYWALL_CACHE = BASE / "unpaywall_cache.jsonl"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

JOURNAL_SOURCES = {
    "journal_browser_uci",
    "journal_direct_uci_or_publisher",
    "journal_publisher_oa",
    "journal_uci_manual",
    "journal_uci_vpn_script",
    "repository_open_access",
}

ATTEMPT_FIELDS = [
    "pmid",
    "doi",
    "journalTitle",
    "candidate_kind",
    "url",
    "final_url",
    "http_status",
    "content_type",
    "result",
    "note",
    "local_pdf",
]


@dataclass(frozen=True)
class Candidate:
    kind: str
    url: str
    referer: str = ""


class LinkExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attr_dict = {k.lower(): v or "" for k, v in attrs}
        href = attr_dict.get("href", "")
        if not href:
            return
        self._current = {
            "href": href,
            "aria": attr_dict.get("aria-label", ""),
            "title": attr_dict.get("title", ""),
            "class": attr_dict.get("class", ""),
            "data_test": attr_dict.get("data-test", ""),
        }
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current is None:
            return
        item = dict(self._current)
        item["text"] = " ".join(" ".join(self._text_parts).split())
        self.links.append(item)
        self._current = None
        self._text_parts = []


def safe_name(value: str, limit: int = 90) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("_")[:limit] or "paper"


def normalize_doi(doi: str) -> str:
    return doi.strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "")


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
    ATTEMPTS.parent.mkdir(parents=True, exist_ok=True)
    with ATTEMPTS.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ATTEMPT_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def load_unpaywall_cache() -> dict[str, dict]:
    cache: dict[str, dict] = {}
    if not UNPAYWALL_CACHE.exists():
        return cache
    with UNPAYWALL_CACHE.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            doi = normalize_doi(str(item.get("doi") or ""))
            if doi:
                cache[doi] = item.get("data") or {}
    return cache


def write_summaries(rows: list[dict[str, str]], run_counts: Counter[str]) -> None:
    needs_rows = [r for r in rows if r.get("selected_status") != "selected"]
    fields = list(rows[0].keys()) if rows else []
    write_csv(NEEDS, needs_rows, fields)

    source_counts = Counter(r.get("selected_source", "") for r in rows)
    status_counts = Counter(r.get("selected_status", "") for r in rows)
    summary = {
        "total_pmids": len(rows),
        "needs_journal_or_uci_access": len(needs_rows),
        "priority_pdf_files": len(list(PDF_DIR.glob("*.pdf"))),
        "selected_source_counts": dict(sorted(source_counts.items())),
        "selected_status_counts": dict(sorted(status_counts.items())),
        "this_run_counts": dict(sorted(run_counts.items())),
    }
    SUMMARY.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    PRIORITY_SUMMARY.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sciencedirect_pii_from_doi(doi: str) -> str:
    suffix = doi.split("/", 1)[1] if "/" in doi else ""
    if not suffix.lower().startswith("s"):
        return ""
    pii = re.sub(r"[^A-Za-z0-9]", "", suffix).upper()
    return pii if len(pii) >= 12 else ""


def bmj_urls_from_doi(doi: str) -> list[Candidate]:
    suffix = doi.split("/", 1)[1] if "/" in doi else ""
    match = re.fullmatch(r"([a-z]+)\.(\d+)\.(\d+)\.([A-Za-z0-9]+)", suffix.lower())
    if not match:
        return []
    code, volume, issue, page = match.groups()
    domain_map = {
        "adc": "adc.bmj.com",
        "bjo": "bjo.bmj.com",
        "gut": "gut.bmj.com",
        "jmg": "jmg.bmj.com",
        "jnnp": "jnnp.bmj.com",
    }
    domain = domain_map.get(code, f"{code}.bmj.com")
    return [Candidate("bmj_dot_doi", f"https://{domain}/content/{volume}/{issue}/{page}.full.pdf")]


def pattern_candidates(row: dict[str, str]) -> list[Candidate]:
    doi = normalize_doi(row.get("doi", ""))
    if not doi:
        return []
    prefix = doi.split("/", 1)[0]
    suffix = doi.split("/", 1)[1] if "/" in doi else ""
    urls: list[Candidate] = []

    if prefix == "10.1038" and suffix:
        urls.append(Candidate("nature_pdf", f"https://www.nature.com/articles/{suffix}.pdf"))
        # Several Springer Nature journals use article IDs without DOI dots, e.g.
        # 10.1038/gim.2014.138 -> /articles/gim2014138.pdf.
        dotted_id = suffix.replace(".", "")
        if dotted_id != suffix:
            urls.append(Candidate("nature_pdf_nodots", f"https://www.nature.com/articles/{dotted_id}.pdf"))
    if prefix in {"10.1002", "10.1111"}:
        urls.extend(
            [
                Candidate("wiley_pdfdirect", f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"),
                Candidate("wiley_pdf", f"https://onlinelibrary.wiley.com/doi/pdf/{doi}"),
            ]
        )
    if prefix == "10.1007":
        urls.append(Candidate("springer_pdf", f"https://link.springer.com/content/pdf/{doi}.pdf"))
    if prefix in {"10.1080", "10.3109"}:
        urls.append(Candidate("taylor_francis_pdf", f"https://www.tandfonline.com/doi/pdf/{doi}?download=true"))
    if prefix == "10.1073":
        urls.append(Candidate("pnas_pdf", f"https://www.pnas.org/doi/pdf/{doi}"))
    if prefix == "10.1056":
        urls.append(Candidate("nejm_pdf", f"https://www.nejm.org/doi/pdf/{doi}"))
    if prefix == "10.1126":
        urls.append(Candidate("science_pdf", f"https://www.science.org/doi/pdf/{doi}"))
    if prefix == "10.1212":
        urls.extend(
            [
                Candidate("neurology_pdfdirect", f"https://www.neurology.org/doi/pdfdirect/{doi}"),
                Candidate("neurology_pdf", f"https://www.neurology.org/doi/pdf/{doi}"),
            ]
        )
    if prefix == "10.1177":
        urls.append(Candidate("sage_pdf", f"https://journals.sagepub.com/doi/pdf/{doi}"))
    if prefix == "10.1089":
        urls.append(Candidate("liebert_pdf", f"https://www.liebertpub.com/doi/pdf/{doi}"))
    if prefix == "10.1021":
        urls.append(Candidate("acs_pdf", f"https://pubs.acs.org/doi/pdf/{doi}"))
    if prefix == "10.1096":
        urls.extend(
            [
                Candidate("faseb_pdfdirect", f"https://faseb.onlinelibrary.wiley.com/doi/pdfdirect/{doi}"),
                Candidate("faseb_pdf", f"https://faseb.onlinelibrary.wiley.com/doi/pdf/{doi}"),
            ]
        )
    if prefix == "10.1523":
        urls.append(Candidate("jneurosci_pdf", f"https://www.jneurosci.org/content/jneuro/doi/{doi}.full.pdf"))
    if prefix == "10.1136":
        urls.extend(bmj_urls_from_doi(doi))
    if prefix == "10.1016":
        pii = sciencedirect_pii_from_doi(doi)
        if pii:
            urls.append(Candidate("sciencedirect_pii", f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft"))

    urls.append(Candidate("doi_landing", f"https://doi.org/{doi}"))
    return urls


def repository_candidates(row: dict[str, str], unpaywall_cache: dict[str, dict]) -> list[Candidate]:
    doi = normalize_doi(row.get("doi", ""))
    data = unpaywall_cache.get(doi, {})
    candidates: list[Candidate] = []
    for location in data.get("oa_locations") or []:
        if location.get("host_type") != "repository":
            continue
        pdf_url = str(location.get("url_for_pdf") or "").strip()
        landing_url = str(location.get("url") or location.get("url_for_landing_page") or "").strip()
        if pdf_url:
            candidates.append(Candidate("unpaywall_repository_pdf", pdf_url, referer=landing_url))
        if landing_url and landing_url != pdf_url:
            candidates.append(Candidate("unpaywall_repository_landing", landing_url))
    return candidates


def row_candidates(row: dict[str, str], unpaywall_cache: dict[str, dict]) -> list[Candidate]:
    seen: set[str] = set()
    candidates: list[Candidate] = []

    for kind, url in [
        ("manifest_journal_pdf", row.get("journal_pdf_url", "")),
        ("manifest_journal_landing", row.get("journal_landing_url", "")),
    ]:
        if url and url not in seen:
            candidates.append(Candidate(kind, url))
            seen.add(url)

    for candidate in repository_candidates(row, unpaywall_cache):
        if candidate.url not in seen:
            candidates.append(candidate)
            seen.add(candidate.url)

    for candidate in pattern_candidates(row):
        if candidate.url not in seen:
            candidates.append(candidate)
            seen.add(candidate.url)
    return candidates


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/pdf,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    return session


def looks_like_pdf(data: bytes, content_type: str) -> bool:
    head = data[:4096].lower()
    return data.startswith(b"%PDF") or ("pdf" in content_type.lower() and b"<html" not in head)


def html_note(data: bytes) -> str:
    text = data[:20000].decode("utf-8", errors="ignore")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    title = " ".join((title_match.group(1) if title_match else "").split())
    lowered = text.lower()
    tags = []
    for label in ["login", "sign in", "institution", "shibboleth", "proxy", "captcha", "access denied"]:
        if label in lowered:
            tags.append(label)
    if title and tags:
        return f"HTML page title={title!r}; signals={','.join(tags)}"
    if title:
        return f"HTML page title={title!r}"
    if tags:
        return f"HTML page; signals={','.join(tags)}"
    return f"HTML/non-PDF response; bytes={len(data)}"


def extract_pdf_candidates(base_url: str, data: bytes) -> list[Candidate]:
    content = data[:500000].decode("utf-8", errors="ignore")
    parser = LinkExtractor()
    try:
        parser.feed(content)
    except html.parser.HTMLParseError:
        return []

    scored: list[tuple[int, Candidate]] = []
    for link in parser.links:
        href = link.get("href", "")
        text_blob = " ".join(
            [
                link.get("text", ""),
                link.get("aria", ""),
                link.get("title", ""),
                link.get("class", ""),
                link.get("data_test", ""),
                href,
            ]
        ).lower()
        if "pdf" not in text_blob and "download" not in text_blob and "pdfft" not in text_blob:
            continue
        if any(word in text_blob for word in ["supplement", "supporting information", "appendix", "mediaobjects"]):
            continue
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        score = 0
        if "download pdf" in text_blob:
            score += 10
        if "article-pdf" in text_blob or "c-pdf" in text_blob or "data-test download-pdf" in text_blob:
            score += 8
        if parsed.path.lower().endswith(".pdf"):
            score += 6
        if "/doi/pdf" in url.lower() or "pdfdirect" in url.lower() or "pdfft" in url.lower():
            score += 5
        scored.append((score, Candidate("discovered_pdf_link", url, referer=base_url)))

    # Some publisher pages expose the article PDF URL in inline JS/metadata rather
    # than as an <a> link. ARVO/IOVS does this for older article PDFs.
    for match in re.finditer(r"""(?P<url>(?:https?:)?//[^"'<>\s]+\.pdf(?:\?[^"'<>\s]*)?|/[^"'<>\s]+\.pdf(?:\?[^"'<>\s]*)?)""", content, flags=re.I):
        raw_url = match.group("url").replace("&amp;", "&")
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url
        url = urljoin(base_url, raw_url)
        lowered = url.lower()
        if any(word in lowered for word in ["supplement", "supporting", "appendix", "mediaobjects", "citation"]):
            continue
        score = 7
        if "content_public" in lowered or "/doi/pdf" in lowered or "pdfdirect" in lowered:
            score += 4
        scored.append((score, Candidate("discovered_raw_pdf_url", url, referer=base_url)))

    seen: set[str] = set()
    output: list[Candidate] = []
    for _, candidate in sorted(scored, key=lambda x: x[0], reverse=True):
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        output.append(candidate)
        if len(output) >= 6:
            break
    return output


def target_path(row: dict[str, str], candidate: Candidate) -> Path:
    pmid = row["pmid"]
    doi_or_hash = safe_name(normalize_doi(row.get("doi", "")), 55)
    if not doi_or_hash:
        doi_or_hash = hashlib.sha1(candidate.url.encode("utf-8")).hexdigest()[:12]
    kind = safe_name(candidate.kind, 35)
    return PDF_DIR / f"PMID{pmid}_journal_uci_script_{kind}_{doi_or_hash}.pdf"


def fetch(
    session: requests.Session,
    candidate: Candidate,
    timeout: float,
) -> tuple[requests.Response | None, bytes, str]:
    headers = {}
    if candidate.referer:
        headers["Referer"] = candidate.referer
    try:
        response = session.get(candidate.url, headers=headers, timeout=timeout, allow_redirects=True)
        return response, response.content, ""
    except requests.RequestException as exc:
        return None, b"", str(exc)


def attempt_candidate(
    session: requests.Session,
    row: dict[str, str],
    candidate: Candidate,
    timeout: float,
    dry_run: bool,
) -> tuple[dict[str, str], list[Candidate]]:
    response, data, error = fetch(session, candidate, timeout)
    content_type = response.headers.get("content-type", "") if response is not None else ""
    final_url = response.url if response is not None else ""
    status = str(response.status_code) if response is not None else ""

    attempt = {
        "pmid": row.get("pmid", ""),
        "doi": row.get("doi", ""),
        "journalTitle": row.get("journalTitle", ""),
        "candidate_kind": candidate.kind,
        "url": candidate.url,
        "final_url": final_url,
        "http_status": status,
        "content_type": content_type,
        "result": "",
        "note": "",
        "local_pdf": "",
    }

    if error:
        attempt["result"] = "error"
        attempt["note"] = error[:500]
        return attempt, []

    if response is not None and response.status_code >= 400:
        attempt["result"] = "http_error"
        attempt["note"] = f"HTTP {response.status_code}; {html_note(data)}"
        return attempt, []

    if looks_like_pdf(data, content_type):
        target = target_path(row, candidate)
        attempt["result"] = "downloaded"
        attempt["note"] = f"validated PDF; bytes={len(data)}"
        attempt["local_pdf"] = str(target)
        if not dry_run:
            PDF_DIR.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".pdf.part")
            tmp.write_bytes(data)
            tmp.replace(target)
        return attempt, []

    discovered: list[Candidate] = []
    if response is not None and "html" in content_type.lower():
        discovered = extract_pdf_candidates(final_url or candidate.url, data)
        if candidate.kind.startswith("unpaywall_repository_"):
            discovered = [
                Candidate("unpaywall_repository_discovered_pdf", item.url, item.referer)
                for item in discovered
            ]
    attempt["result"] = "not_pdf"
    attempt["note"] = html_note(data)
    return attempt, discovered


def copy_if_existing(selected_pdf: str, target: Path) -> str:
    src = Path(selected_pdf)
    if not src.exists() or target.exists():
        return ""
    shutil.copy2(src, target)
    return str(target)


def should_process(row: dict[str, str], pmid_filter: set[str], include_selected_journal: bool) -> bool:
    if pmid_filter and row.get("pmid") not in pmid_filter:
        return False
    if include_selected_journal:
        return True
    return row.get("selected_source") not in JOURNAL_SOURCES


def selected_is_pmc(row: dict[str, str]) -> bool:
    return row.get("selected_source") == "pmc_oa_fallback"


def run(args: argparse.Namespace) -> int:
    rows, fields = read_csv(args.manifest)
    pmids = set(args.pmid or [])
    session = make_session()
    unpaywall_cache = load_unpaywall_cache()
    run_counts: Counter[str] = Counter()
    processed = 0
    updated = 0

    for row in rows:
        if args.needs_only and row.get("selected_status") == "selected":
            continue
        if not should_process(row, pmids, args.include_selected_journal):
            continue
        candidates = row_candidates(row, unpaywall_cache)
        if not candidates:
            run_counts["no_candidate_url"] += 1
            continue

        processed += 1
        if args.limit and processed > args.limit:
            break

        row_attempts: list[dict[str, str]] = []
        discovered_queue: list[Candidate] = []
        success: dict[str, str] | None = None

        for candidate in candidates:
            attempt, discovered = attempt_candidate(session, row, candidate, args.timeout, args.dry_run)
            row_attempts.append(attempt)
            run_counts[f"attempt_{attempt['result']}"] += 1
            if attempt["result"] == "downloaded":
                success = attempt
                break
            discovered_queue.extend(discovered)
            time.sleep(args.delay)

        if success is None and discovered_queue:
            seen = {a["url"] for a in row_attempts}
            for candidate in discovered_queue:
                if candidate.url in seen:
                    continue
                seen.add(candidate.url)
                attempt, _ = attempt_candidate(session, row, candidate, args.timeout, args.dry_run)
                row_attempts.append(attempt)
                run_counts[f"attempt_{attempt['result']}"] += 1
                if attempt["result"] == "downloaded":
                    success = attempt
                    break
                time.sleep(args.delay)

        append_attempts(row_attempts)

        if success and not args.dry_run:
            was_pmc_fallback = selected_is_pmc(row)
            previous = row.get("selected_pdf", "")
            row["journal_pdf_url"] = success["final_url"] or success["url"]
            is_repository = success["candidate_kind"].startswith("unpaywall_repository_")
            row["selected_source"] = "repository_open_access" if is_repository else "journal_uci_vpn_script"
            row["selected_pdf"] = success["local_pdf"]
            row["selected_status"] = "selected"
            if is_repository:
                row["priority_note"] = "Downloaded a legal open-access repository PDF recorded by Unpaywall."
            elif was_pmc_fallback:
                row["priority_note"] = "Replaced PMC fallback with official journal PDF downloaded through UCI/VPN-accessible URL."
            elif previous:
                row["priority_note"] = "Updated to official journal PDF downloaded through UCI/VPN-accessible URL."
            else:
                row["priority_note"] = "Downloaded official journal PDF through UCI/VPN-accessible URL."
            updated += 1
            run_counts["rows_updated"] += 1
        elif not success:
            run_counts["rows_not_updated"] += 1

        if processed % args.save_every == 0 and not args.dry_run:
            write_csv(args.manifest, rows, fields)
            write_summaries(rows, run_counts)
            print(f"[progress] processed={processed} updated={updated}", flush=True)

    if not args.dry_run:
        write_csv(args.manifest, rows, fields)
        write_summaries(rows, run_counts)

    print(
        json.dumps(
            {
                "processed_rows": min(processed, args.limit or processed),
                "updated_rows": updated,
                "attempt_log": str(ATTEMPTS),
                "manifest": str(args.manifest),
                "summary": str(SUMMARY),
                "counts": dict(sorted(run_counts.items())),
            },
            indent=2,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--pmid", action="append", help="Only process this PMID; can be repeated.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N candidate rows.")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--delay", type=float, default=0.4)
    parser.add_argument("--save-every", type=int, default=25)
    parser.add_argument("--include-selected-journal", action="store_true")
    parser.add_argument("--needs-only", action="store_true", help="Only process rows that are not selected yet.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(list(argv))


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
