"""File formats: a file name (and what the upload peek saw) -> its media type, its EDAM format and
the EDAM data type it usually holds.

``encodingFormat`` in Croissant is a media type. Most bioinformatics formats have no registered one,
so the conventional ``x-`` names are used, and the EDAM format IRI says precisely what the file is.
Longest suffix wins (``.vcf.gz`` before ``.gz``). IDs checked against OLS4, like :mod:`.vocab`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .vocab import EDAM


@dataclass(frozen=True)
class Format:
    key: str
    label: str
    media_type: str
    edam_format: str = ""     # "format_1930"
    edam_data: str = ""       # "data_3917"
    delimiter: str = ""       # "," / "\t" for a table mlcroissant can read
    compression: str = ""     # "gzip" when the suffix says so

    @property
    def format_iri(self) -> str:
        return EDAM + self.edam_format if self.edam_format else ""

    @property
    def data_iri(self) -> str:
        return EDAM + self.edam_data if self.edam_data else ""


_TABLE = "data_3112"          # gene expression matrix
_COUNTS = "data_3917"         # count matrix
_READS = "data_2044"          # sequence
_ALIGN = "data_0863"          # sequence alignment
_VARIANTS = "data_3498"       # sequence variations

# (suffixes, Format), longest suffixes first within the scan below.
_FORMATS: tuple[tuple[tuple[str, ...], Format], ...] = (
    ((".h5ad",), Format("h5ad", "AnnData (h5ad)", "application/x-hdf5", "format_3590", _TABLE)),
    ((".loom",), Format("loom", "Loom", "application/x-hdf5", "format_3913", _TABLE)),
    ((".h5", ".hdf5"), Format("hdf5", "HDF5", "application/x-hdf5", "format_3590", "")),
    ((".mtx", ".mtx.gz"), Format("mtx", "Matrix Market", "text/plain", "format_3916", _COUNTS)),
    ((".csv",), Format("csv", "CSV", "text/csv", "format_3752", "", ",")),
    ((".csv.gz",), Format("csv.gz", "CSV (gzip)", "text/csv", "format_3752", "", ",", "gzip")),
    ((".tsv", ".tab"), Format("tsv", "TSV", "text/tab-separated-values", "format_3475", "", "\t")),
    ((".tsv.gz", ".tab.gz"), Format("tsv.gz", "TSV (gzip)", "text/tab-separated-values", "format_3475",
                                    "", "\t", "gzip")),
    ((".txt",), Format("txt", "Plain text", "text/plain", "format_2330", "")),
    ((".txt.gz",), Format("txt.gz", "Plain text (gzip)", "text/plain", "format_2330", "", "", "gzip")),
    ((".vcf",), Format("vcf", "VCF", "text/x-vcf", "format_3016", _VARIANTS)),
    ((".vcf.gz", ".vcf.bgz"), Format("vcf.gz", "VCF (bgzip)", "text/x-vcf", "format_3016", _VARIANTS,
                                     "", "gzip")),
    ((".bam",), Format("bam", "BAM", "application/x-bam", "format_2572", _ALIGN)),
    ((".cram",), Format("cram", "CRAM", "application/x-cram", "format_3462", _ALIGN)),
    ((".sam",), Format("sam", "SAM", "text/x-sam", "format_2573", _ALIGN)),
    ((".fastq", ".fq", ".fastq.gz", ".fq.gz"), Format("fastq", "FASTQ", "text/x-fastq", "format_1930", _READS)),
    ((".fasta", ".fa", ".fna", ".fasta.gz", ".fa.gz"), Format("fasta", "FASTA", "text/x-fasta", "format_1929", _READS)),
    ((".bed", ".bed.gz"), Format("bed", "BED", "text/x-bed", "format_3003", "")),
    ((".gtf", ".gtf.gz"), Format("gtf", "GTF", "text/x-gtf", "format_2306", "")),
    ((".gff", ".gff3", ".gff.gz", ".gff3.gz"), Format("gff3", "GFF3", "text/x-gff3", "format_1975", "")),
    ((".json",), Format("json", "JSON", "application/json", "format_3464", "")),
    ((".pdf",), Format("pdf", "PDF", "application/pdf", "format_3508", "")),
    ((".png",), Format("png", "PNG", "image/png", "format_3603", "")),
    ((".svg",), Format("svg", "SVG", "image/svg+xml", "format_3604", "")),
    ((".html", ".htm"), Format("html", "HTML", "text/html", "format_2331", "")),
    ((".md",), Format("markdown", "Markdown", "text/markdown", "", "")),
    ((".docx",), Format("docx", "Word document",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "", "")),
    ((".xlsx",), Format("xlsx", "Excel workbook",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "format_3620", "")),
    ((".rds",), Format("rds", "R data (RDS)", "application/octet-stream", "", "")),
    ((".zip",), Format("zip", "ZIP archive", "application/zip", "format_3987", "")),
    ((".tar.gz", ".tgz"), Format("tar.gz", "tar archive (gzip)", "application/gzip", "format_3981", "", "", "gzip")),
    ((".tar",), Format("tar", "tar archive", "application/x-tar", "format_3981", "")),
    ((".gz",), Format("gz", "gzip", "application/gzip", "format_3989", "", "", "gzip")),
)

_BY_SUFFIX: list[tuple[str, Format]] = sorted(
    ((suf, fmt) for sufs, fmt in _FORMATS for suf in sufs), key=lambda x: -len(x[0]))

UNKNOWN = Format("other", "File", "application/octet-stream")


def for_name(name: str) -> Format:
    low = (name or "").lower()
    for suf, fmt in _BY_SUFFIX:
        if low.endswith(suf):
            return fmt
    return UNKNOWN


def refine(fmt: Format, peek: "dict | None") -> Format:
    """What the content says, where the name said nothing useful: a ``.txt`` or unnamed file whose
    head is a comma/tab table becomes a table, so a record set can be drafted for it."""
    tab = (peek or {}).get("tabular") or {}
    if fmt.delimiter or not tab:
        return fmt
    delim = {"comma": ",", "tab": "\t"}.get(str(tab.get("delimiter") or ""))
    if not delim or fmt.key not in ("txt", "txt.gz", "other", "gz"):
        return fmt
    gz = fmt.compression == "gzip"
    if delim == ",":
        return _FORMATS_BY_KEY["csv.gz" if gz else "csv"]
    return _FORMATS_BY_KEY["tsv.gz" if gz else "tsv"]


_FORMATS_BY_KEY = {fmt.key: fmt for _, fmt in _FORMATS}


def all_formats() -> list[Format]:
    return [fmt for _, fmt in _FORMATS]
