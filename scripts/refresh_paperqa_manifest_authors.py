"""Refresh PaperQA manifest citations with authors from the full RetiGene metadata."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add author strings to PaperQA manifest citations.")
    parser.add_argument(
        "--paperqa-manifest",
        default="output/retigene_papers/paperqa_pilot_50/paperqa_manifest.csv",
        help="PaperQA manifest to update in place.",
    )
    parser.add_argument(
        "--metadata",
        default="output/retigene_papers/journal_priority/retigene_priority_manifest.csv",
        help="Full metadata CSV containing authorString by PMID.",
    )
    return parser.parse_args()


def first_author(author_string: str) -> str:
    authors = [a.strip() for a in (author_string or "").split(",") if a.strip()]
    if not authors:
        return ""
    if len(authors) == 1:
        return authors[0]
    return f"{authors[0]} et al."


def citation(row: dict[str, str], metadata: dict[str, dict[str, str]]) -> str:
    pmid = str(row.get("doc_id") or "").strip()
    meta = metadata.get(pmid, {})
    author = first_author(meta.get("authorString", ""))
    title = row.get("title") or meta.get("title") or ""
    journal = row.get("journal") or meta.get("journalTitle") or ""
    year = row.get("year") or meta.get("pubYear") or ""
    doi = row.get("doi") or meta.get("doi") or ""
    parts = []
    if author:
        parts.append(author)
    if title:
        parts.append(title.rstrip(".") + ".")
    if journal or year:
        parts.append(f"{journal} ({year}).".strip())
    if pmid:
        parts.append(f"PMID:{pmid}")
    if doi:
        parts.append(f"DOI:{doi}")
    return " ".join(part for part in parts if part).replace("..", ".")


def main() -> None:
    args = parse_args()
    paperqa_manifest = Path(args.paperqa_manifest)
    metadata_path = Path(args.metadata)

    with metadata_path.open(newline="", encoding="utf-8") as f:
        metadata = {row["pmid"]: row for row in csv.DictReader(f)}

    with paperqa_manifest.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    for row in rows:
        row["citation"] = citation(row, metadata)

    with paperqa_manifest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Updated {len(rows)} citations in {paperqa_manifest}")


if __name__ == "__main__":
    main()
