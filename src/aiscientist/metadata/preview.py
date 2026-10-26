"""Rows of a record set, read from the head of its file.

The caller fetches the head (a bounded number of bytes, from wherever the file is) and passes it
here. With mlcroissant installed the rows are read the way any Croissant consumer reads them: a
one-file copy of the document points at the sample (with the sample's own checksum, so the hash
check passes) and ``Dataset.records`` yields the rows. Without it, the same fields are read with
the ``csv`` module. A head that was cut mid-file loses its last, partial line either way.
"""

from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import io
import json
import logging
import tempfile
import zlib
from pathlib import Path
from typing import Any

from . import formats


def pick_record_set(doc: dict, wanted: str = "") -> tuple["dict | None", "dict | None"]:
    """The record set named ``wanted`` (by name or ``@id``; the first when empty) and the
    distribution entry its first field reads."""
    sets = doc.get("recordSet") or []
    rs = next((r for r in sets if not wanted or wanted in (r.get("name"), r.get("@id"))), None)
    if rs is None:
        return None, None
    for field in rs.get("field") or []:
        src = field.get("source") or {}
        ref = (src.get("fileObject") or src.get("fileSet") or {}).get("@id")
        if ref:
            entry = next((d for d in doc.get("distribution") or [] if d.get("@id") == ref), None)
            return rs, entry
    return rs, None


def _decompress_head(head: bytes) -> bytes:
    """As much of a gzip/bgzip head as decompresses (a cut stream yields what it has)."""
    out = bytearray()
    data = head
    while data:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            out += d.decompress(data)
        except zlib.error:
            break
        data = d.unused_data            # bgzip: the next block is a new gzip member
        if not d.eof:
            break
    return bytes(out)


def _text_lines(head: bytes, gz: bool, truncated: bool) -> list[str]:
    raw = _decompress_head(head) if gz else head
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    if (truncated or gz) and len(lines) > 1 and not text.endswith("\n"):
        lines = lines[:-1]
    return lines


def _clean(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            return str(value)
    return value


def _with_mlcroissant(doc: dict, rs: dict, entry: dict, sample: bytes, limit: int) -> list[dict]:
    import mlcroissant as mlc
    logging.getLogger("absl").setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(prefix="aiscientist-preview-") as tmp:
        fmt = formats.for_name(str(entry.get("name") or entry.get("contentUrl") or ""))
        local = "sample.csv" if fmt.delimiter == "," else "sample.tsv"
        Path(tmp, local).write_bytes(sample)
        one = {"@type": "cr:FileObject", "@id": entry["@id"], "name": local, "contentUrl": local,
               "encodingFormat": "text/csv" if fmt.delimiter == "," else "text/tab-separated-values",
               "sha256": hashlib.sha256(sample).hexdigest()}
        fields = []
        for f in rs.get("field") or []:
            f = copy.deepcopy(f)
            src = f.get("source") or {}
            if "fileSet" in src:
                src["fileObject"] = src.pop("fileSet")
            fields.append(f)
        small = {k: v for k, v in doc.items() if k not in ("distribution", "recordSet")}
        small["distribution"] = [one]
        small["recordSet"] = [{**{k: v for k, v in rs.items() if k != "field"}, "field": fields}]
        Path(tmp, "croissant.json").write_text(json.dumps(small))
        ds = mlc.Dataset(jsonld=str(Path(tmp, "croissant.json")))
        names = {f.get("@id"): f.get("name") for f in fields}
        rows = []
        for record in ds.records(record_set=rs["@id"]):
            rows.append({names.get(k) or str(k).rsplit("/", 1)[-1]: _clean(v) for k, v in record.items()})
            if len(rows) >= limit:
                break
        return rows


def _with_csv(rs: dict, lines: list[str], delimiter: str, limit: int) -> list[dict]:
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    wanted = [(f.get("name"), ((f.get("source") or {}).get("extract") or {}).get("column"))
              for f in rs.get("field") or []]
    rows = []
    for raw in reader:
        rows.append({name: raw.get(col) for name, col in wanted if name})
        if len(rows) >= limit:
            break
    return rows


def preview_rows(doc: dict, rs: dict, entry: dict, head: bytes, *, limit: int = 10,
                 truncated: bool = False) -> tuple[list[dict], str, list[str]]:
    """``(rows, how they were read, notes)`` for the first ``limit`` records of ``rs``."""
    name = str(entry.get("name") or entry.get("contentUrl") or "")
    fmt = formats.for_name(name)
    if fmt.delimiter not in (",", "\t"):
        return [], "none", [f"{name} is not a CSV/TSV table; only tables can be previewed."]
    gz = fmt.compression == "gzip" or head[:2] == b"\x1f\x8b"
    lines = _text_lines(head, gz, truncated)
    notes = ["Read from the first part of the file only."] if truncated else []
    sample = ("\n".join(lines) + "\n").encode("utf-8")
    try:
        import mlcroissant  # noqa: F401
        has_mlc = True
    except Exception:  # noqa: BLE001
        has_mlc = False
    if has_mlc:
        try:
            return _with_mlcroissant(doc, rs, entry, sample, limit), "mlcroissant", notes
        except Exception as exc:  # noqa: BLE001 - fall back to the plain reader, and say so
            notes.append(f"mlcroissant could not read it ({type(exc).__name__}); read as plain "
                         f"{'CSV' if fmt.delimiter == ',' else 'TSV'} instead.")
    return _with_csv(rs, lines, fmt.delimiter, limit), "csv", notes


def gzip_bytes(data: bytes) -> bytes:
    """For tests: gzip ``data``."""
    return gzip.compress(data)
