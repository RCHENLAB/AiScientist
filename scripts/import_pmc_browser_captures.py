#!/usr/bin/env python3
"""Build PDFs from PMC scan pages captured through the interactive browser."""

from __future__ import annotations

import argparse
import csv
import io
import json
import time
from collections import Counter
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image
from lxml import html
from pypdf import PdfReader
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer


BASE = Path("output/retigene_papers/journal_priority")
CAPTURE = BASE / "pmc_browser_capture" / "capture_metadata.json"
MANIFEST = BASE / "retigene_priority_manifest.csv"
NEEDS = BASE / "needs_journal_or_uci_access.csv"
PDF_DIR = BASE / "papers_priority"
PAGE_DIR = BASE / "pmc_browser_capture" / "scan_pages"
SUMMARY = BASE / "pmc_browser_recovery_summary.json"
PRIORITY_SUMMARY = BASE / "journal_priority_summary.json"
USER_AGENT = "BioAgentPrototype/1.0 (PMC literature recovery)"
FONT_DIR = Path(
    "/Users/maziyao/.cache/codex-runtimes/codex-primary-runtime/dependencies/"
    "native/libreoffice-headless/libreoffice/LibreOfficeDev.app/Contents/Resources/"
    "fonts/truetype"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=CAPTURE)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--delay", type=float, default=0.08)
    parser.add_argument("--limit", type=int, default=0)
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


def fetch_image(url: str, timeout: float) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
    with urlopen(request, timeout=timeout) as response:
        data = response.read()
        content_type = response.headers.get("content-type", "")
    if len(data) < 10_000 or not content_type.lower().startswith("image/"):
        raise ValueError(f"not a full scan image: {content_type}; bytes={len(data)}")
    return data


def build_scan_pdf(capture: dict, timeout: float, delay: float) -> tuple[Path, int, int]:
    pmid = str(capture["pmid"])
    pmcid = str(capture["pmcid"])
    pages = list(capture.get("scanPages") or [])
    if not pages:
        raise ValueError("capture has no scan pages")

    local_page_dir = PAGE_DIR / f"PMID{pmid}_{pmcid}"
    local_page_dir.mkdir(parents=True, exist_ok=True)
    images: list[Image.Image] = []
    total_bytes = 0
    for number, page in enumerate(pages, start=1):
        suffix = Path(str(page["url"]).split("?", 1)[0]).suffix or ".png"
        page_path = local_page_dir / f"page_{number:03d}{suffix}"
        if page_path.exists() and page_path.stat().st_size >= 10_000:
            data = page_path.read_bytes()
        else:
            data = fetch_image(str(page["url"]), timeout)
            page_path.write_bytes(data)
            time.sleep(delay)
        total_bytes += len(data)
        with Image.open(io.BytesIO(data)) as image:
            images.append(image.convert("RGB"))

    target = PDF_DIR / f"PMID{pmid}_pmc_scan_reconstructed_{pmcid}.pdf"
    tmp = target.with_suffix(".pdf.part")
    images[0].save(
        tmp,
        "PDF",
        save_all=True,
        append_images=images[1:],
        resolution=150.0,
        quality=95,
    )
    for image in images:
        image.close()
    tmp.replace(target)

    reader = PdfReader(target)
    if len(reader.pages) != len(pages) or target.stat().st_size < 30_000:
        target.unlink(missing_ok=True)
        raise ValueError(
            f"PDF validation failed: expected {len(pages)} pages, got {len(reader.pages)}"
        )
    return target, len(pages), total_bytes


def extract_article_blocks(path: Path) -> list[tuple[str, str]]:
    document = html.fromstring(path.read_text(encoding="utf-8"))
    for element in document.xpath("//script|//style|//nav|//button|//aside|//form"):
        element.drop_tree()

    blocks: list[tuple[str, str]] = []
    previous = ""
    xpath = "//h1|//h2|//h3|//h4|//p|//li[not(ancestor::li)]|//tr"
    for element in document.xpath(xpath):
        text = " ".join(" ".join(element.itertext()).split())
        if not text or text == previous:
            continue
        previous = text
        tag = str(element.tag).lower()
        if tag == "h1":
            kind = "title"
        elif tag in {"h2", "h3", "h4"}:
            kind = "heading"
        elif tag == "li":
            kind = "bullet"
        elif tag == "tr":
            kind = "table"
        else:
            kind = "body"
        blocks.append((kind, text))
    return blocks


def build_html_pdf(capture: dict) -> tuple[Path, int, int]:
    pmid = str(capture["pmid"])
    pmcid = str(capture.get("pmcid") or "")
    identifier = str(capture.get("identifier") or pmcid or f"PMID{pmid}")
    source_label = str(capture.get("sourceLabel") or f"PubMed Central {pmcid}").strip()
    selected_source = str(capture.get("selectedSource") or "pmc_html_reconstructed")
    file_tag = str(capture.get("fileTag") or selected_source)
    html_path = Path(str(capture.get("htmlPath") or ""))
    if not html_path.exists() or html_path.stat().st_size < 5_000:
        raise ValueError("capture does not contain a complete PMC article")
    blocks = extract_article_blocks(html_path)
    text_chars = sum(len(text) for _, text in blocks)
    if len(blocks) < 10 or text_chars < 5_000:
        raise ValueError(f"captured article text is incomplete: blocks={len(blocks)} chars={text_chars}")

    pdfmetrics.registerFont(TTFont("NotoSans", FONT_DIR / "NotoSans-Regular.ttf"))
    pdfmetrics.registerFont(TTFont("NotoSans-Bold", FONT_DIR / "NotoSans-Bold.ttf"))
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ArticleTitle",
        parent=styles["Title"],
        fontName="NotoSans-Bold",
        fontSize=17,
        leading=21,
        alignment=TA_CENTER,
        spaceAfter=14,
        splitLongWords=True,
    )
    heading_style = ParagraphStyle(
        "ArticleHeading",
        parent=styles["Heading2"],
        fontName="NotoSans-Bold",
        fontSize=12,
        leading=15,
        spaceBefore=10,
        spaceAfter=5,
        splitLongWords=True,
    )
    body_style = ParagraphStyle(
        "ArticleBody",
        parent=styles["BodyText"],
        fontName="NotoSans",
        fontSize=9.5,
        leading=13,
        spaceAfter=6,
        splitLongWords=True,
    )
    table_style = ParagraphStyle(
        "ArticleTable",
        parent=body_style,
        fontSize=8,
        leading=10,
        leftIndent=8,
        borderColor="#BBBBBB",
        borderWidth=0.5,
        borderPadding=4,
    )
    source_style = ParagraphStyle(
        "ArticleSource",
        parent=body_style,
        fontSize=8,
        textColor="#444444",
        spaceAfter=12,
    )

    story = [
        Paragraph(escape(str(capture.get("title") or f"Article {identifier}")), title_style),
        Paragraph(
            escape(f"Source: {source_label}; PMID {pmid}; captured from {capture.get('url', '')}"),
            source_style,
        ),
    ]
    for kind, text in blocks:
        if kind == "title":
            continue
        if kind == "heading":
            style = heading_style
        elif kind == "table":
            style = table_style
        else:
            style = body_style
            if kind == "bullet":
                text = "- " + text
        story.append(Paragraph(escape(text), style))

    target = PDF_DIR / f"PMID{pmid}_{file_tag}_{identifier}.pdf"
    tmp = target.with_suffix(".pdf.part")
    document = SimpleDocTemplate(
        str(tmp),
        pagesize=letter,
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        title=str(capture.get("title") or ""),
        author=f"{source_label} browser recovery",
    )
    document.build(story)
    tmp.replace(target)
    reader = PdfReader(target)
    extracted_chars = sum(len(page.extract_text() or "") for page in reader.pages)
    if len(reader.pages) < 2 or extracted_chars < 5_000:
        target.unlink(missing_ok=True)
        raise ValueError(
            f"rendered PDF validation failed: pages={len(reader.pages)} text={extracted_chars}"
        )
    return target, len(reader.pages), extracted_chars


def write_summaries(
    rows: list[dict[str, str]],
    results: list[dict[str, object]],
) -> None:
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
        "pmc_browser_results": results,
    }
    SUMMARY.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    PRIORITY_SUMMARY.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    args = parse_args()
    captures = json.loads(args.capture.read_text(encoding="utf-8"))
    captures = [
        capture
        for capture in captures
        if capture.get("status") == "captured"
        and (capture.get("scanPages") or int(capture.get("htmlBytes") or 0) >= 5_000)
    ]
    if args.limit:
        captures = captures[: args.limit]
    rows, fields = read_csv(args.manifest)
    by_pmid = {row.get("pmid", ""): row for row in rows}
    results: list[dict[str, object]] = []

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    for index, capture in enumerate(captures, start=1):
        pmid = str(capture["pmid"])
        row = by_pmid.get(pmid)
        if not row or (
            row.get("pmcid")
            and capture.get("pmcid")
            and row.get("pmcid") != capture.get("pmcid")
        ):
            results.append({"pmid": pmid, "status": "metadata_mismatch"})
            continue
        if not row.get("pmcid"):
            for field in [
                "title",
                "doi",
                "pmcid",
                "journalTitle",
                "pubYear",
                "isOpenAccess",
                "inPMC",
            ]:
                if capture.get(field) and not row.get(field):
                    row[field] = str(capture[field])
        if row.get("selected_status") == "selected" and row.get("selected_source", "").startswith(
            "pmc_"
        ):
            results.append({"pmid": pmid, "status": "already_selected"})
            continue
        try:
            if capture.get("scanPages"):
                target, page_count, source_bytes = build_scan_pdf(
                    capture, args.timeout, args.delay
                )
                source = "pmc_scan_reconstructed"
                note = (
                    f"Reconstructed from {page_count} original PMC scan-page images captured "
                    f"from {capture['pmcid']}; source image bytes={source_bytes}."
                )
            else:
                target, page_count, source_bytes = build_html_pdf(capture)
                source = str(capture.get("selectedSource") or "pmc_html_reconstructed")
                source_label = str(
                    capture.get("sourceLabel") or f"PubMed Central {capture.get('pmcid', '')}"
                ).strip()
                note = (
                    f"Rendered from complete article HTML captured from {source_label}; "
                    f"pages={page_count}; extracted text characters={source_bytes}."
                )
        except (HTTPError, URLError, OSError, ValueError) as exc:
            results.append({"pmid": pmid, "status": "error", "note": str(exc)[:500]})
            print(f"[{index}/{len(captures)}] PMID {pmid}: error: {exc}", flush=True)
            continue

        row["selected_source"] = source
        row["selected_pdf"] = str(target)
        row["selected_status"] = "selected"
        row["priority_note"] = note
        results.append(
            {
                "pmid": pmid,
                "pmcid": capture.get("pmcid", ""),
                "identifier": capture.get("identifier", ""),
                "status": "downloaded",
                "pages": page_count,
                "pdf": str(target),
            }
        )
        print(f"[{index}/{len(captures)}] PMID {pmid}: {page_count} pages", flush=True)

    write_csv(args.manifest, rows, fields)
    write_summaries(rows, results)
    print(
        json.dumps(
            {
                "processed": len(captures),
                "downloaded": sum(r["status"] == "downloaded" for r in results),
                "remaining": sum(row.get("selected_status") != "selected" for row in rows),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
