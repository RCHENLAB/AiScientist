#!/usr/bin/env python3
"""Prepare a RetiGene PMID/PDF corpus for PaperQA.

Input: RetiGene Genes table TSV with a "References (PMIDs)" column.
Output:
  - retigene_paper_manifest.csv: all unique PMIDs with Europe PMC status.
  - non_open_access_or_failed.csv: rows not downloaded and why.
  - papers_open_access/: downloaded open-access PDFs.

The downloader is conservative: it only downloads PDFs Europe PMC marks as open
access and PDF-available. Other papers are recorded for follow-up.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


DEFAULT_GENE_TABLE = Path("/Users/maziyao/Downloads/RetiGene_gene-table_v1.12.tsv")
DEFAULT_OUT_DIR = Path("output/retigene_papers")
EUROPE_PMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
NCBI_PMC_OA_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}"
USER_AGENT = "BioAgentPrototype RetiGene corpus preparation (contact: <contact@your-institution.edu>)"


MANIFEST_FIELDS = [
    "pmid",
    "genes",
    "gene_count",
    "broad_phenotype_categories",
    "inheritance_modes",
    "title",
    "authorString",
    "journalTitle",
    "pubYear",
    "doi",
    "pmcid",
    "pubType",
    "isOpenAccess",
    "inPMC",
    "hasPDF",
    "hasReferences",
    "citedByCount",
    "firstPublicationDate",
    "pubmed_url",
    "europepmc_url",
    "oa_license",
    "oa_pdf_url",
    "oa_package_url",
    "download_method",
    "downloaded",
    "local_pdf",
    "status",
    "note",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gene-table", type=Path, default=DEFAULT_GENE_TABLE)
    parser.add_argument(
        "--pmid-csv",
        type=Path,
        default=None,
        help="Optional CSV with PMIDs and Gene columns; used for teacher-provided full PMID lists.",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--sleep", type=float, default=0.2, help="Delay between API/PDF requests.")
    parser.add_argument("--download", action="store_true", help="Download open-access PDFs.")
    parser.add_argument("--max-downloads", type=int, default=None, help="Optional cap for testing.")
    return parser.parse_args()


def split_pmids(value: str) -> list[str]:
    return re.findall(r"\d+", value or "")


def read_gene_table(path: Path) -> dict[str, dict[str, set[str]]]:
    by_pmid: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"genes": set(), "broad_phenotype_categories": set(), "inheritance_modes": set()}
    )
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter="\t")
        required = {"Gene name", "References (PMIDs)"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"Missing required columns in {path}: {sorted(missing)}")
        for row in reader:
            gene = (row.get("Gene name") or "").strip()
            phenotype = (row.get("Broad phenotype category") or "").strip()
            inheritance = (row.get("Inheritance mode") or "").strip()
            for pmid in split_pmids(row.get("References (PMIDs)", "")):
                if gene:
                    by_pmid[pmid]["genes"].add(gene)
                if phenotype:
                    by_pmid[pmid]["broad_phenotype_categories"].add(phenotype)
                if inheritance:
                    by_pmid[pmid]["inheritance_modes"].add(inheritance)
    return by_pmid


def read_pmid_csv(path: Path) -> dict[str, dict[str, set[str]]]:
    by_pmid: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"genes": set(), "broad_phenotype_categories": set(), "inheritance_modes": set()}
    )
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = set(reader.fieldnames or [])
        pmid_col = "PMIDs" if "PMIDs" in fieldnames else "pmid" if "pmid" in fieldnames else None
        gene_col = "Gene" if "Gene" in fieldnames else "gene" if "gene" in fieldnames else None
        if pmid_col is None:
            raise SystemExit(f"Missing PMIDs/pmid column in {path}")
        for row in reader:
            gene = (row.get(gene_col) or "").strip() if gene_col else ""
            for pmid in split_pmids(row.get(pmid_col, "")):
                if gene:
                    by_pmid[pmid]["genes"].add(gene)
    return by_pmid


def request_json(url: str, timeout: int = 45) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def query_europe_pmc_batch(pmids: list[str]) -> dict[str, dict[str, Any]]:
    query = " OR ".join(f"EXT_ID:{pmid}" for pmid in pmids)
    params = urllib.parse.urlencode(
        {
            "query": query,
            "format": "json",
            "pageSize": len(pmids),
            "resultType": "lite",
        }
    )
    url = f"{EUROPE_PMC_SEARCH_URL}?{params}"
    data = request_json(url)
    found: dict[str, dict[str, Any]] = {}
    for result in data.get("resultList", {}).get("result", []):
        pmid = result.get("pmid") or result.get("id")
        if pmid:
            found[str(pmid)] = result
    return found


def query_all_metadata(pmids: list[str], batch_size: int, sleep: float) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    total = len(pmids)
    for start in range(0, total, batch_size):
        batch = pmids[start : start + batch_size]
        batch_result: dict[str, dict[str, Any]] = {}
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                batch_result = query_europe_pmc_batch(batch)
                break
            except Exception as exc:  # noqa: BLE001 - keep going across network hiccups.
                last_error = exc
                time.sleep(1.5 * attempt)
        if not batch_result and last_error:
            print(
                f"[metadata] failed batch {start + 1}-{start + len(batch)}: {last_error}",
                file=sys.stderr,
            )
        metadata.update(batch_result)
        print(f"[metadata] checked {min(start + len(batch), total)}/{total}; found {len(metadata)}", flush=True)
        time.sleep(sleep)
    return metadata


def classify_row(row: dict[str, str]) -> None:
    pmcid = row.get("pmcid", "")
    is_oa = row.get("isOpenAccess", "")

    if row.get("title", "") == "":
        row["status"] = "metadata_missing"
        row["note"] = "Europe PMC returned no metadata for this PMID."
    elif is_oa == "Y" and pmcid:
        row["status"] = "open_access_pmc_candidate"
        row["note"] = "Europe PMC marks this paper as open access with a PMCID; download will use NCBI PMC OA links."
    elif is_oa == "Y":
        row["status"] = "open_access_no_pmcid"
        row["note"] = "Open access metadata present, but no PMCID for PDF retrieval."
    else:
        row["status"] = "not_open_access"
        row["note"] = "Not marked open access by Europe PMC; mark for alternative sources."


def metadata_to_rows(
    by_pmid: dict[str, dict[str, set[str]]],
    metadata: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for pmid in sorted(by_pmid, key=int):
        meta = metadata.get(pmid, {})
        pmcid = str(meta.get("pmcid") or "")
        row: dict[str, str] = {
            "pmid": pmid,
            "genes": ";".join(sorted(by_pmid[pmid]["genes"])),
            "gene_count": str(len(by_pmid[pmid]["genes"])),
            "broad_phenotype_categories": ";".join(sorted(by_pmid[pmid]["broad_phenotype_categories"])),
            "inheritance_modes": ";".join(sorted(by_pmid[pmid]["inheritance_modes"])),
            "title": str(meta.get("title") or ""),
            "authorString": str(meta.get("authorString") or ""),
            "journalTitle": str(meta.get("journalTitle") or ""),
            "pubYear": str(meta.get("pubYear") or ""),
            "doi": str(meta.get("doi") or ""),
            "pmcid": pmcid,
            "pubType": str(meta.get("pubType") or ""),
            "isOpenAccess": str(meta.get("isOpenAccess") or ""),
            "inPMC": str(meta.get("inPMC") or ""),
            "hasPDF": str(meta.get("hasPDF") or ""),
            "hasReferences": str(meta.get("hasReferences") or ""),
            "citedByCount": str(meta.get("citedByCount") or ""),
            "firstPublicationDate": str(meta.get("firstPublicationDate") or ""),
            "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "europepmc_url": f"https://europepmc.org/article/MED/{pmid}",
            "oa_license": "",
            "oa_pdf_url": "",
            "oa_package_url": "",
            "download_method": "",
            "downloaded": "No",
            "local_pdf": "",
            "status": "",
            "note": "",
        }
        classify_row(row)
        rows.append(row)
    return rows


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str] = MANIFEST_FIELDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def is_pdf_bytes(data: bytes, content_type: str) -> bool:
    return data.startswith(b"%PDF") or "pdf" in content_type.lower()


def request_bytes(url: str, timeout: int = 90) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("content-type", "")


def ncbi_ftp_to_https(url: str) -> str:
    """Turn PMC OA ftp links into the currently browsable HTTPS archive path.

    The NCBI OA API still returns links under ftp://ftp.ncbi.nlm.nih.gov/pub/pmc,
    while the downloadable tree is now exposed under /pub/pmc/deprecated/.
    """
    if url.startswith("ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/"):
        suffix = url.removeprefix("ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/")
        return f"https://ftp.ncbi.nlm.nih.gov/pub/pmc/deprecated/{suffix}"
    if url.startswith("https://ftp.ncbi.nlm.nih.gov/pub/pmc/"):
        suffix = url.removeprefix("https://ftp.ncbi.nlm.nih.gov/pub/pmc/")
        return f"https://ftp.ncbi.nlm.nih.gov/pub/pmc/deprecated/{suffix}"
    return url


def fetch_pmc_oa_links(pmcid: str) -> dict[str, str]:
    url = NCBI_PMC_OA_URL.format(pmcid=pmcid)
    data, _content_type = request_bytes(url, timeout=45)
    root = ElementTree.fromstring(data)
    record = root.find(".//record")
    if record is None:
        return {}
    links: dict[str, str] = {
        "oa_license": record.attrib.get("license", ""),
        "oa_retracted": record.attrib.get("retracted", ""),
    }
    for link in record.findall("link"):
        fmt = link.attrib.get("format", "")
        href = link.attrib.get("href", "")
        if fmt == "pdf" and href:
            links["oa_pdf_url"] = ncbi_ftp_to_https(href)
        elif fmt == "tgz" and href:
            links["oa_package_url"] = ncbi_ftp_to_https(href)
    return links


def write_pdf_from_package(package_bytes: bytes, target: Path, pmcid: str) -> str:
    with tempfile.TemporaryDirectory() as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        package_path = tmp_dir / f"{pmcid}.tar.gz"
        package_path.write_bytes(package_bytes)
        with tarfile.open(package_path, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.isfile() and m.name.lower().endswith(".pdf")]
            if not members:
                raise RuntimeError("OA package did not contain a PDF")
            # Prefer the largest PDF if there are multiple supplement PDFs.
            member = max(members, key=lambda m: m.size)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"could not extract {member.name} from OA package")
            data = extracted.read()
        if not data.startswith(b"%PDF"):
            raise RuntimeError("extracted package member was not a valid PDF")
        target.write_bytes(data)
        return member.name


def download_pdf(row: dict[str, str], pdf_dir: Path, sleep: float) -> None:
    if row["status"] != "open_access_pmc_candidate":
        return
    pmid = row["pmid"]
    pmcid = row["pmcid"]
    target = pdf_dir / f"PMID{pmid}_{pmcid}.pdf"
    tmp = target.with_suffix(".pdf.part")
    if target.exists() and target.stat().st_size > 1024:
        row["downloaded"] = "Yes"
        row["local_pdf"] = str(target)
        row["status"] = "downloaded"
        row["note"] = "PDF already exists locally."
        return

    last_error = ""
    for attempt in range(1, 4):
        try:
            oa_links = fetch_pmc_oa_links(pmcid)
            row["oa_license"] = oa_links.get("oa_license", "")
            row["oa_pdf_url"] = oa_links.get("oa_pdf_url", "")
            row["oa_package_url"] = oa_links.get("oa_package_url", "")
            if oa_links.get("oa_retracted") == "yes":
                raise RuntimeError("PMC OA record is retracted")

            if row["oa_pdf_url"]:
                data, content_type = request_bytes(row["oa_pdf_url"], timeout=120)
                if not is_pdf_bytes(data[:2048], content_type):
                    raise RuntimeError(f"OA PDF response is not a PDF; content-type={content_type}; bytes={len(data)}")
                tmp.write_bytes(data)
                tmp.replace(target)
                row["download_method"] = "pmc_oa_pdf"
            elif row["oa_package_url"]:
                package, content_type = request_bytes(row["oa_package_url"], timeout=180)
                if "gzip" not in content_type.lower() and not package[:2] == b"\x1f\x8b":
                    raise RuntimeError(
                        f"OA package response is not gzip; content-type={content_type}; bytes={len(package)}"
                    )
                member = write_pdf_from_package(package, tmp, pmcid)
                tmp.replace(target)
                row["download_method"] = f"pmc_oa_package:{member}"
            else:
                raise RuntimeError("PMC OA API returned no PDF or package link")

            row["downloaded"] = "Yes"
            row["local_pdf"] = str(target)
            row["status"] = "downloaded"
            row["note"] = "Downloaded open-access PDF from NCBI PMC OA."
            time.sleep(sleep)
            return
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError, ElementTree.ParseError, tarfile.TarError) as exc:
            last_error = str(exc)
            time.sleep(1.5 * attempt)

    row["downloaded"] = "No"
    row["local_pdf"] = ""
    row["status"] = "pdf_download_failed"
    row["note"] = last_error[:500]


def download_candidates(rows: list[dict[str, str]], pdf_dir: Path, sleep: float, max_downloads: int | None) -> None:
    pdf_dir.mkdir(parents=True, exist_ok=True)
    candidates = [r for r in rows if r["status"] == "open_access_pmc_candidate"]
    total = len(candidates) if max_downloads is None else min(len(candidates), max_downloads)
    done = 0
    for row in candidates:
        if max_downloads is not None and done >= max_downloads:
            break
        download_pdf(row, pdf_dir, sleep)
        done += 1
        if done % 10 == 0 or row["status"] != "downloaded":
            print(f"[download] processed {done}/{total}; latest PMID {row['pmid']} => {row['status']}", flush=True)


def summarize(rows: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["status"]] += 1
    counts["total_pmids"] = len(rows)
    counts["downloaded_pdfs"] = sum(1 for row in rows if row["downloaded"] == "Yes")
    return dict(sorted(counts.items()))


def main() -> None:
    args = parse_args()
    out_dir = args.out_dir
    pdf_dir = out_dir / "papers_open_access"
    out_dir.mkdir(parents=True, exist_ok=True)

    by_pmid = read_pmid_csv(args.pmid_csv) if args.pmid_csv else read_gene_table(args.gene_table)
    pmids = sorted(by_pmid, key=int)
    print(f"[input] PMID CSV: {args.pmid_csv}" if args.pmid_csv else f"[input] genes table: {args.gene_table}", flush=True)
    print(f"[input] unique PMIDs: {len(pmids)}", flush=True)

    metadata = query_all_metadata(pmids, args.batch_size, args.sleep)
    (out_dir / "europepmc_metadata.jsonl").write_text(
        "\n".join(json.dumps(metadata.get(pmid, {"pmid": pmid}), ensure_ascii=False) for pmid in pmids) + "\n",
        encoding="utf-8",
    )
    rows = metadata_to_rows(by_pmid, metadata)

    if args.download:
        download_candidates(rows, pdf_dir, args.sleep, args.max_downloads)

    manifest = out_dir / "retigene_paper_manifest.csv"
    failed = out_dir / "non_open_access_or_failed.csv"
    write_csv(manifest, rows)
    write_csv(failed, [r for r in rows if r["downloaded"] != "Yes"])

    counts = summarize(rows)
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(counts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("[output]", manifest, flush=True)
    print("[output]", failed, flush=True)
    print("[output]", summary_path, flush=True)
    print("[summary]", flush=True)
    for key, value in counts.items():
        print(f"  {key}: {value}", flush=True)


if __name__ == "__main__":
    main()
