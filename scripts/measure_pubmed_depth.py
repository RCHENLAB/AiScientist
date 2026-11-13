#!/usr/bin/env python3
"""Compare the former eight-hit search with local ranking of 100 hits.

Run from any directory, for example:
    python scripts/measure_pubmed_depth.py "PRPF31 haploinsufficiency" "retinitis pigmentosa penetrance"

Each JSON line reports wall time and reference-PMID presence among the papers the answer
would read (at most 12), separately for the old and new searches. This makes real network
requests, in old/new order; timings include request latency and local processing, not an LLM.
With AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME=mcp, run inside the published literature
image with the read-only operator bundle documented in literature-maintenance.md.
new_100 uses actual MCP tools/list + call, including process/transport startup.
Legacy is retained until the deployment passes its native acceptance gate.
The old path deliberately preserves the former metadata-only filter and scoring rule.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from functools import partial
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _literature_runtime import platform_helpers, runtime, search_publications  # noqa: E402

collect_papers, _contract = platform_helpers()
focus_literature_query = _contract.focus_literature_query
_QUERY_STOPWORDS, _WORD, _clean = _contract.QUERY_STOPWORDS, _contract._WORD, _contract._clean

# Frozen eight-hit baseline: these filters deliberately remain independent of
# the production MCP search being measured.
_EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

_GENERIC_CITATION_PATTERNS = (
    r"^abstracts?\b",
    r"\babstract book\b",
    r"\bannual meeting\b",
    r"\bconference\b",
    r"\bcongress\b",
    r"\be-?posters?\b",
    r"\bmeeting abstracts?\b",
    r"^publication only$",
    r"\bsupplement\b",
)

def _is_generic_citation(citation: dict[str, Any]) -> bool:
    title = str(citation.get("title") or "").strip().lower()
    if not title:
        return True
    return any(re.search(pattern, title) for pattern in _GENERIC_CITATION_PATTERNS)

REFERENCE_PMIDS = ("21529959", "12906118", "35433063")


def old_search(query: str, _limit: int, *, timeout_s: float) -> dict:
    """The pre-fix request/filter/ranking, frozen here as the measurement baseline."""
    import httpx

    query = focus_literature_query(query)
    try:
        resp = httpx.get(
            _EUROPEPMC,
            params={"query": f"({query}) AND SRC:MED", "format": "json", "pageSize": 8, "resultType": "core"},
            timeout=timeout_s, headers={"User-Agent": "AiScientist/literature_search"},
        )
        resp.raise_for_status()
        raw = resp.json().get("resultList", {}).get("result", []) or []
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    terms = {t.lower() for t in _WORD.findall(query) if len(t) > 3 and t.lower() not in _QUERY_STOPWORDS}
    ranked = []
    for r in raw[:8]:
        p = {"title": _clean(r.get("title") or ""), "authors": r.get("authorString") or "",
             "journal": r.get("journalTitle") or "", "year": str(r.get("pubYear") or ""),
             "doi": r.get("doi") or "", "pmid": str(r.get("pmid") or "")}
        title = p["title"].lower()
        metadata = " ".join(p[k] for k in ("title", "journal", "authors")).lower()
        if _is_generic_citation(p) or (terms and not any(t in metadata for t in terms)):
            continue
        score = 2 if p["doi"] or p["pmid"] else 0
        score += sum(3 if t in title else 1 if t in metadata else 0 for t in terms)
        score += 2 * ("review" in title) + 2 * ("germline" in title) + 4 * ("ddx41" in title)
        try:
            year = int(p["year"][:4])
        except ValueError:
            year = 0
        ranked.append(((score, year), p))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return {"status": "ok", "results": [p for _, p in ranked]}


def measure(query: str, search) -> dict:
    started = time.perf_counter()
    collected = collect_papers(search, [query])
    elapsed = time.perf_counter() - started
    pmids = {p.pmid for p in collected.papers}
    return {"seconds": round(elapsed, 3), "kept": len(collected.papers),
            "reference_pmids": {p: p in pmids for p in REFERENCE_PMIDS},
            **({"errors": collected.errors} if collected.errors else {})}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queries", nargs="+", help="quote each PubMed keyword query")
    parser.add_argument("--timeout", type=float, default=20.0, help="baseline HTTP timeout; native MCP allows another 60s for startup (default: 20)")
    args = parser.parse_args()
    failed = False
    for query in args.queries:
        old = measure(query, partial(old_search, timeout_s=args.timeout))
        new = measure(query, partial(search_publications, timeout_s=args.timeout, abstracts=True, pubmed_only=True))
        print(json.dumps({"query": query, "old_8": old, "new_100": new, "runtime": runtime()}), flush=True)
        failed |= bool(old.get("errors") or new.get("errors"))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
