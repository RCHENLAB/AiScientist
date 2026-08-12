#!/usr/bin/env python3
"""Build a full-corpus PaperQA manifest from the RetiGene priority manifest.

The RetiGene recovery pipeline tracks papers in
``retigene_priority_manifest.csv`` (columns: pmid, title, authorString,
journalTitle, pubYear, doi, selected_status, selected_pdf, ...). PaperQA's
directory index, however, wants a small metadata manifest keyed by
``file_location`` so it does not have to infer citations with an LLM.

This script converts the *selected* rows of the priority manifest into the
9-column PaperQA manifest used by ``build_paperqa_directory_index.py`` (the same
format as ``paperqa_pilot_50/paperqa_manifest.csv``):

    file_location, citation, docname, title, doi, year, journal, doc_id, url

``file_location`` is the PDF basename (PaperQA resolves it against
``--papers``). Only rows whose ``selected_status == selected`` and whose PDF is
present on disk are emitted, so the manifest always matches the corpus.

Usage:
    python scripts/build_retigene_paperqa_manifest.py \
      --priority-manifest output/retigene_papers/journal_priority/retigene_priority_manifest.csv \
      --papers            output/retigene_papers/journal_priority/papers_priority \
      --out               output/retigene_papers/journal_priority/paperqa_manifest.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path

PAPERQA_COLUMNS = [
    "file_location",
    "citation",
    "docname",
    "title",
    "doi",
    "year",
    "journal",
    "doc_id",
    "url",
]


def _first_author(author_string: str) -> str:
    """'Barker DF, Hostikka SL, Zhou J' -> 'Barker DF et al.' (single author kept as-is)."""
    author_string = (author_string or "").strip().rstrip(".")
    if not author_string:
        return ""
    authors = [a.strip() for a in author_string.split(",") if a.strip()]
    if not authors:
        return ""
    if len(authors) == 1:
        return authors[0]
    return f"{authors[0]} et al."


def _citation(row: dict) -> str:
    """Human-readable citation string, mirroring the pilot manifest format."""
    author = _first_author(row.get("authorString", ""))
    title = (row.get("title", "") or "").strip()
    journal = (row.get("journalTitle", "") or "").strip()
    year = (row.get("pubYear", "") or "").strip()
    pmid = (row.get("pmid", "") or "").strip()
    doi = (row.get("doi", "") or "").strip()

    head = f"{author} " if author else ""
    year_part = f" ({year})" if year else ""
    journal_part = f" {journal}" if journal else ""
    cite = f"{head}{title}{journal_part}{year_part}.".strip()
    if pmid:
        cite += f" PMID:{pmid}"
    if doi:
        cite += f" DOI:{doi}"
    return cite


def build(priority_manifest: Path, papers: Path, out: Path) -> dict:
    rows = list(csv.DictReader(open(priority_manifest, newline="", encoding="utf-8")))
    out_rows = []
    skipped_not_selected = 0
    skipped_missing_pdf = 0

    for r in rows:
        if r.get("selected_status") != "selected":
            skipped_not_selected += 1
            continue
        sel_pdf = (r.get("selected_pdf", "") or "").strip()
        basename = os.path.basename(sel_pdf)
        if not basename or not (papers / basename).exists():
            skipped_missing_pdf += 1
            continue
        pmid = (r.get("pmid", "") or "").strip()
        out_rows.append(
            {
                "file_location": basename,
                "citation": _citation(r),
                "docname": f"PMID{pmid}" if pmid else basename,
                "title": (r.get("title", "") or "").strip(),
                "doi": (r.get("doi", "") or "").strip(),
                "year": (r.get("pubYear", "") or "").strip(),
                "journal": (r.get("journalTitle", "") or "").strip(),
                "doc_id": pmid,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
            }
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PAPERQA_COLUMNS)
        writer.writeheader()
        writer.writerows(out_rows)

    return {
        "priority_manifest": str(priority_manifest),
        "papers": str(papers),
        "out": str(out),
        "selected_rows_written": len(out_rows),
        "skipped_not_selected": skipped_not_selected,
        "skipped_missing_pdf": skipped_missing_pdf,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a full-corpus PaperQA manifest.")
    p.add_argument("--priority-manifest", required=True, type=Path)
    p.add_argument("--papers", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    report = build(args.priority_manifest.resolve(), args.papers.resolve(), args.out.resolve())
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
