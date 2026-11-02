"""Facts: what was measured about a dataset or a result, before any of it becomes metadata.

A fact is something the system established — a file's size and SHA-256 from the upload checks, the
column names of a table from its header, a run's inputs and parameters from its record. Facts are
stored beside the metadata (``MetadataRecord.facts``) so the ``croissant.json`` can be rebuilt at
any time, with the owner's review applied on top, instead of being edited.

What never becomes a fact, because it must never reach metadata: data values (a table's rows are
read only to infer each column's type, then dropped), sample or patient identifiers (a VCF's sample
names are counted, not kept), absolute server paths (files are named relative to the dataset's own
root), and credentials.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import os
import re
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from . import formats

MAX_FILES = 500                 # files described one by one; the rest are grouped by format
MAX_HASH_BYTES = 2 * 1024 ** 3  # a result file larger than this is described without a checksum
MAX_COLUMNS = 200
_TABLE_HEAD_BYTES = 128 * 1024

# --- accessions -----------------------------------------------------------------------------------------

_ACCESSIONS: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"\b(GS[EMP]\d{3,9})\b"), "GEO", "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={}"),
    (re.compile(r"\b([SED]R[RXSP]\d{5,10})\b"), "SRA", "https://www.ncbi.nlm.nih.gov/sra/{}"),
    (re.compile(r"\b(PRJ[NED][A-Z]?\d{3,9})\b"), "BioProject", "https://www.ncbi.nlm.nih.gov/bioproject/{}"),
    (re.compile(r"\b(SAM[NED][A-Z]?\d{3,10})\b"), "BioSample", "https://www.ncbi.nlm.nih.gov/biosample/{}"),
    (re.compile(r"\b(E-[A-Z]{4}-\d{1,8})\b"), "ArrayExpress",
     "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/{}"),
)


def accession_info(acc: str) -> "dict | None":
    """``{"id", "registry", "url"}`` for a GEO / SRA / ENA / BioProject / BioSample / ArrayExpress
    accession, None for anything else."""
    acc = (acc or "").strip()
    for pattern, registry, url in _ACCESSIONS:
        m = pattern.fullmatch(acc)
        if m:
            return {"id": acc, "registry": registry, "url": url.format(acc)}
    return None


def find_accessions(text: str) -> list[str]:
    """Accessions written in a file or folder name (``GSM5676874_retina.h5ad``)."""
    found: list[str] = []
    spaced = re.sub(r"[_./]+", " ", text or "")
    for pattern, _, _ in _ACCESSIONS:
        for m in pattern.finditer(spaced):
            if m.group(1) not in found:
                found.append(m.group(1))
    return found


# --- column types ----------------------------------------------------------------------------------------

_INT = re.compile(r"[+-]?\d+")
_FLOAT = re.compile(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?|[+-]?(inf|nan)", re.IGNORECASE)
_BOOL = {"true", "false"}
_MISSING = {"", "na", "nan", "null", "none", "n/a", "."}


def _is_date(v: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return False
    try:
        date.fromisoformat(v)
        return True
    except ValueError:
        return False


def infer_type(values: Iterable[str]) -> str:
    """The Croissant atomic type that fits every sampled value: ``sc:Integer``, ``sc:Float``,
    ``sc:Boolean``, ``sc:Date`` or ``sc:Text`` (also for a column whose sample is all missing)."""
    vals = [v.strip() for v in values if v is not None and v.strip().lower() not in _MISSING]
    if not vals:
        return "sc:Text"
    if all(_INT.fullmatch(v) for v in vals):
        return "sc:Integer"
    if all(_FLOAT.fullmatch(v) for v in vals):
        return "sc:Float"
    if all(v.lower() in _BOOL for v in vals):
        return "sc:Boolean"
    if all(_is_date(v) for v in vals):
        return "sc:Date"
    return "sc:Text"


def _table_from_rows(rows: list[list[str]], delimiter: str, n_columns: "int | None" = None) -> "dict | None":
    rows = [r for r in rows if r]
    if len(rows) < 1 or len(rows[0]) < 2:
        return None
    header = [c.strip() for c in rows[0]]
    body = rows[1:]
    columns = header[:MAX_COLUMNS]
    types = [infer_type(r[i] if i < len(r) else "" for r in body) for i in range(len(columns))]
    return {"delimiter": delimiter, "columns": columns, "types": types,
            "n_columns": n_columns or len(header), "sampled_rows": len(body)}


# --- the upload peek -----------------------------------------------------------------------------------


def compact_peek(peek: "dict | None") -> "dict | None":
    """The parts of an upload peek (``tools.api.peek_dataset``) that metadata may use: format, a
    table's columns with their inferred types, an HDF5/AnnData shape and group names, a VCF's
    genome build and sample COUNT. The sampled rows are used for the types and then dropped; the
    VCF sample names are dropped. None when there is nothing usable."""
    if not isinstance(peek, dict):
        return None
    out: dict[str, Any] = {}
    fmt = str(peek.get("detected_format") or "")
    if fmt:
        out["detected_format"] = fmt
    if peek.get("compression"):
        out["compression"] = str(peek["compression"])
    tab = peek.get("tabular")
    if isinstance(tab, dict) and tab.get("columns"):
        delim = {"comma": ",", "tab": "\t", "semicolon": ";", "pipe": "|"}.get(str(tab.get("delimiter") or ""), "")
        columns = [str(c) for c in tab.get("columns") or []]
        sample = [[str(x) for x in r] for r in (tab.get("rows_sample") or []) if isinstance(r, list)]
        types = [infer_type(r[i] if i < len(r) else "" for r in sample) for i in range(len(columns))]
        out["table"] = {"delimiter": delim, "columns": columns, "types": types,
                        "n_columns": int(tab.get("n_columns") or len(columns)),
                        "sampled_rows": len(sample),
                        # the peek cuts a column name at 60 characters: such a name may be a prefix
                        "maybe_truncated": [i for i, c in enumerate(columns) if len(c) >= 60]}
    h5 = peek.get("hdf5")
    if isinstance(h5, dict):
        ad = h5.get("anndata") if isinstance(h5.get("anndata"), dict) else {}
        info: dict[str, Any] = {}
        x = ad.get("X")
        if isinstance(x, dict) and isinstance(x.get("shape"), list) and len(x["shape"]) == 2:
            info["n_obs"], info["n_vars"] = int(x["shape"][0]), int(x["shape"][1])
            info["x_dtype"] = str(x.get("dtype") or "")
        for group in ("obs", "var", "layers", "obsm"):
            if isinstance(ad.get(group), list):
                info[group] = [str(k)[:80] for k in ad[group][:40]]
        attrs = h5.get("root_attrs") if isinstance(h5.get("root_attrs"), dict) else {}
        for key in ("genome", "chemistry_description", "library_ids", "filetype", "version"):
            if key in attrs and key != "library_ids":
                info[key] = str(attrs[key])[:120]
        names = [str(d.get("name") or "") for d in h5.get("datasets") or [] if isinstance(d, dict)]
        if any(n.startswith("matrix/") for n in names):
            info["tenx_matrix"] = True
            shape = next((d.get("shape") for d in h5.get("datasets") or []
                          if isinstance(d, dict) and d.get("name") == "matrix/shape"), None)
            if shape:
                info["matrix_shape_dataset"] = shape
        if info:
            out["hdf5"] = info
    vcf = peek.get("vcf")
    if isinstance(vcf, dict):
        keep = {k: vcf.get(k) for k in ("assembly", "caller", "chr_prefix", "fileformat", "has_genotypes")
                if vcf.get(k) not in (None, "", [])}
        if "n_samples" in vcf:
            keep["n_samples"] = int(vcf.get("n_samples") or 0)
        if keep:
            out["vcf"] = keep
    return out or None


def _rel(path: str) -> str:
    p = PurePosixPath(str(path or "").replace("\\", "/").lstrip("/"))
    parts = [x for x in p.parts if x not in ("", ".", "..")]
    return "/".join(parts)


def file_fact(rel_path: str, size: "int | None", sha256: "str | None" = None,
              peek: "dict | None" = None, *, verified: str = "") -> dict:
    """One file of a dataset: its path RELATIVE to the dataset's root, size, checksum (when one was
    measured) and what its content looked like. ``verified``: how the checksum was established."""
    sha = (sha256 or "").lower()
    out = {"path": _rel(rel_path), "size": int(size) if size is not None else None,
           "sha256": sha if re.fullmatch(r"[0-9a-f]{64}", sha) else None}
    if verified and out["sha256"]:
        out["verified"] = verified
    compact = compact_peek(peek) if peek and "tool" in peek else peek
    if compact:
        out["content"] = compact
    return out


def dataset_facts(*, name: str, kind: str, size_bytes: int, sha256: "str | None", uploaded_at: str,
                  owner: str, files: "list[dict] | None" = None, peek: "dict | None" = None,
                  source: str = "upload", derived_from: "dict | None" = None) -> dict:
    """The facts of one dataset row. ``files`` are :func:`file_fact` dicts (or raw dicts with
    ``path``/``size``/``sha256``/``peek``); a single-file dataset without them is described from the
    row itself. ``source``: ``upload`` (measured by the upload checks), ``server`` (a folder bound
    from the server's shared disks), ``result`` (saved from a run) or ``registry`` (only what the
    registry row holds, for rows older than this feature)."""
    is_folder = kind == "folder" or kind.startswith("server-dir") or kind.endswith("dir")
    clean: list[dict] = []
    for f in files or []:
        if not isinstance(f, dict):
            continue
        clean.append(file_fact(str(f.get("path") or ""), f.get("size"), f.get("sha256"),
                               f.get("content") or f.get("peek"), verified=str(f.get("verified") or "")))
    if not clean and not is_folder:
        clean = [file_fact(name, size_bytes, sha256, peek,
                           verified="upload" if sha256 and source == "upload" else "")]
    clean = [f for f in clean if f["path"]]
    clean.sort(key=lambda f: f["path"])
    facts: dict[str, Any] = {
        "name": name, "kind": "folder" if is_folder else "file", "registry_kind": kind,
        "size_bytes": int(size_bytes or 0), "uploaded_at": uploaded_at, "owner": owner,
        "source": source, "files": clean[:MAX_FILES],
        "file_count": len(clean) if clean else None,
        "accessions": find_accessions(name),
    }
    if len(clean) > MAX_FILES:
        rest: dict[str, int] = {}
        for f in clean[MAX_FILES:]:
            rest[formats.for_name(f["path"]).key] = rest.get(formats.for_name(f["path"]).key, 0) + 1
        facts["more_files"] = rest
    if derived_from:
        facts["derived_from"] = {k: derived_from.get(k) for k in ("uuid", "run_id", "what") if derived_from.get(k)}
    return facts


# --- results: what a run's bundle holds ------------------------------------------------------------------


def sha256_file(path: Path) -> "str | None":
    try:
        if path.stat().st_size > MAX_HASH_BYTES:
            return None
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def inspect_table(path: Path, fmt: "formats.Format | None" = None) -> "dict | None":
    """Columns and inferred types of a CSV/TSV (plain or gzip) from its first 128 KB. None for a
    file that is not such a table. Values are read for the types only and not returned."""
    fmt = fmt or formats.for_name(path.name)
    if not fmt.delimiter:
        return None
    try:
        with (gzip.open(path, "rb") if fmt.compression == "gzip" else path.open("rb")) as fh:
            head = fh.read(_TABLE_HEAD_BYTES)
    except (OSError, EOFError):
        return None
    text = head.decode("utf-8", "replace")
    lines = text.splitlines()
    if len(head) >= _TABLE_HEAD_BYTES and len(lines) > 1:
        lines = lines[:-1]                       # the last line may be cut
    try:
        rows = list(csv.reader(io.StringIO("\n".join(lines[:51])), delimiter=fmt.delimiter))
    except csv.Error:
        return None
    return _table_from_rows(rows, fmt.delimiter)


def result_files(root: Path, *, skip: Iterable[str] = ("croissant.json",)) -> list[dict]:
    """The files of a run's bundle (``artifacts/``), relative to it, each with size, SHA-256 and,
    for a CSV/TSV, its columns. Sorted; at most :data:`MAX_FILES`."""
    out: list[dict] = []
    skipped = set(skip)
    # Operational timing continues after result packaging (later saves, background completion).
    # Its mutable journal is served separately; a snapshot checksum here would immediately stale.
    skipped.add("process/timing.jsonl")
    try:
        paths = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    except OSError:
        return out
    for p in paths:
        rel = p.relative_to(root).as_posix()
        if rel in skipped or any(part.startswith(".") for part in PurePosixPath(rel).parts):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        fact = {"path": rel, "size": size, "sha256": sha256_file(p)}
        if fact["sha256"]:
            fact["verified"] = "computed"
        table = inspect_table(p)
        if table:
            fact["content"] = {"detected_format": "tabular", "table": table}
        out.append(fact)
        if len(out) >= MAX_FILES:
            break
    return out


# Values that are kept as parameters. Anything else is reduced to its type.
_PATHLIKE = re.compile(r"^(~|/|[A-Za-z]:\\)|/(data|home|dfs\d*|pub|tmp|scratch|opt|Users)/")
_SECRETLIKE = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|aisci_[A-Za-z0-9_-]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY)")
_SKIP_ARGS = {"code", "script", "source", "text", "prompt", "note", "case_note", "content", "query_text"}


def safe_value(value: Any, depth: int = 0) -> Any:
    """A tool argument as it may appear in metadata: numbers and booleans as they are; a string
    unless it is long, looks like a server path (reduced to its file name) or a credential (dropped);
    lists and dicts element by element, shallowly."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _SECRETLIKE.search(value):
            return "<redacted>"
        if _PATHLIKE.search(value):
            return PurePosixPath(value).name or "<path>"
        return value if len(value) <= 200 else value[:200] + "…"
    if depth >= 2:
        return f"<{type(value).__name__}>"
    if isinstance(value, (list, tuple)):
        return [safe_value(v, depth + 1) for v in list(value)[:20]]
    if isinstance(value, dict):
        return {str(k)[:60]: safe_value(v, depth + 1) for k, v in list(value.items())[:30]
                if str(k) not in _SKIP_ARGS}
    return f"<{type(value).__name__}>"


def run_parameters(rounds: Iterable[dict], limit: int = 60) -> list[dict]:
    """The tool calls of a run's ACCEPTED steps, in order: ``{"step", "tool", "args"}`` with each
    argument passed through :func:`safe_value`; code snippets and free text are left out."""
    out: list[dict] = []
    for r in rounds or []:
        if not isinstance(r, dict):
            continue
        verdict = r.get("verdict") if isinstance(r.get("verdict"), dict) else {}
        if str(verdict.get("verdict") or "accept") != "accept":
            continue                              # the Critic sent it back: not part of the result
        steps = ((r.get("scientist_result") or {}).get("steps") or []) if isinstance(r.get("scientist_result"), dict) else []
        for s in steps:
            if not isinstance(s, dict) or not s.get("tool") or s.get("ok") is False:
                continue
            args = s.get("args") if isinstance(s.get("args"), dict) else {}
            out.append({"step": str(r.get("step") or "")[:160], "tool": str(s["tool"])[:80],
                        "args": safe_value(args)})
            if len(out) >= limit:
                return out
    return out


def iso_now() -> str:
    from datetime import timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def human_size(n: "int | None") -> str:
    if n is None:
        return "unknown size"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{n} B"


def env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


_PATH_TOKEN = re.compile(r"(?<![\w.])(~|/)[^\s\"'<>|;,]*/[^\s\"'<>|;,]*")


def scrub_text(text: str) -> str:
    """Free text as it may appear in metadata: an absolute path in it (``/data/Users/x/run.h5ad``)
    is cut to its last part, and a credential is removed."""
    text = _SECRETLIKE.sub("<redacted>", text or "")
    return _PATH_TOKEN.sub(lambda m: PurePosixPath(m.group(0)).name or "<path>", text)
