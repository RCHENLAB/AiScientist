"""What the Datasets tab shows of a dataset: a bounded preview of a file and the list of a folder's files
(RCHENLAB/AiScientist issue 10).

Pure functions over bytes, a local path or a file-like object. The gateway decides where the bytes come
from (this host, or HPC3 over SFTP) and who may read them; nothing here touches the network or the
database. Every reader is bounded: a preview reads a head of a text file, and only the metadata and a few
rows of an ``.h5ad``, so a 15 GB matrix costs the same to preview as a small one. A preview never raises:
a file it cannot read is described by what could be read, with a note.
"""
from __future__ import annotations

import csv
import io
import json
import os
import posixpath
import zlib
from pathlib import Path
from typing import Any

# A text file's preview reads this much of its head (a gzip head is decompressed to at most TEXT_MAX).
HEAD_BYTES = 1 << 20
TEXT_MAX = 4 << 20
TABLE_ROWS = 100
TEXT_LINES = 200
VCF_RECORDS = 20
CELL_CHARS = 200
H5AD_HEAD_ROWS = 8
H5AD_HEAD_COLUMNS = 8
H5AD_MAX_COLUMNS = 60
H5AD_MAX_CATEGORIES = 12
H5AD_SAMPLE_VALUES = 2000
HDF5_MAX_NODES = 120
FOLDER_MAX_ENTRIES = 2000

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
# Shown inline in the page. Never SVG or HTML: a user's file must not run script in this origin.
INLINE_TYPES = {**IMAGE_TYPES, ".pdf": "application/pdf"}

_TABLE_SUFFIXES = (".csv", ".tsv", ".tab", ".txt")
_VCF_SUFFIXES = (".vcf", ".vcf.gz", ".vcf.bgz")


def _stem_suffixes(name: str) -> tuple[str, bool]:
    """``(name without a trailing .gz/.bgz, lower-cased; whether it had one)``."""
    low = name.lower()
    for comp in (".gz", ".bgz"):
        if low.endswith(comp):
            return low[: -len(comp)], True
    return low, False


def classify(name: str) -> str:
    """What kind of preview a file gets, from its name: ``h5ad``, ``hdf5``, ``vcf``, ``table``, ``json``,
    ``image``, ``pdf``, ``text`` or ``binary``. The content can still overrule a ``text`` guess (a VCF
    without its suffix is recognised by its first line)."""
    low = name.lower()
    if low.endswith(".h5ad"):
        return "h5ad"
    if low.endswith((".h5", ".hdf5", ".loom")):
        return "hdf5"
    if low.endswith(_VCF_SUFFIXES):
        return "vcf"
    ext = os.path.splitext(low)[1]
    if ext in IMAGE_TYPES:
        return "image"
    if ext == ".pdf":
        return "pdf"
    stem, _ = _stem_suffixes(low)
    if stem.endswith(_TABLE_SUFFIXES):
        return "table"
    if stem.endswith(".json"):
        return "json"
    if stem.endswith((".md", ".log", ".mtx", ".fa", ".fasta", ".fq", ".fastq", ".gtf", ".gff", ".gff3",
                      ".bed", ".sam", ".yaml", ".yml", ".r", ".py", ".sh", ".html")):
        return "text"
    if ext in ("", ".gz", ".bgz"):
        return "text"           # decided by the bytes: a binary head is reported as binary
    return "binary"


def inline_type(name: str) -> "str | None":
    """The media type a file may be shown with inside the page, or None (it can only be downloaded)."""
    return INLINE_TYPES.get(os.path.splitext(name.lower())[1])


# --- text formats ---------------------------------------------------------------------------------


def _gunzip(data: bytes, limit: int = TEXT_MAX) -> bytes:
    """Decompress the gzip members in ``data`` (a bgzip file is many), stopping at ``limit`` bytes of
    output or where the head was cut off."""
    out = bytearray()
    while data and len(out) < limit:
        d = zlib.decompressobj(31)
        try:
            out += d.decompress(data, limit - len(out))
        except zlib.error:
            break
        if not d.eof:
            break
        data = d.unused_data
    return bytes(out)


def _looks_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample[:4096]:
        return True
    text = sum(32 <= b < 127 or b in (9, 10, 13) or b >= 128 for b in sample[:4096])
    return text / min(len(sample), 4096) < 0.85


def _text_of(head: bytes, truncated: bool) -> tuple[str, bool]:
    """The head as text, without the last line when the head ends inside it. ``(text, cut)``."""
    text = head.decode("utf-8", "replace")
    if truncated and "\n" in text:
        text = text[: text.rindex("\n")]
    return text, truncated


def _cell(value: Any) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= CELL_CHARS else s[: CELL_CHARS - 1] + "…"


def _column_types(rows: list[list[str]], n: int) -> list[str]:
    types = []
    for k in range(n):
        values = [r[k] for r in rows if k < len(r) and r[k] not in ("", "NA", "nan", "NaN", ".")]
        kind = "empty" if not values else "integer"
        for v in values:
            try:
                int(v)
                continue
            except ValueError:
                pass
            try:
                float(v)
                kind = "number" if kind in ("integer", "number") else kind
            except ValueError:
                kind = "text"
                break
        types.append(kind)
    return types


def table_preview(text: str, name: str, cut: bool) -> "dict[str, Any] | None":
    """The first rows of a delimited file, or None when the text is not a table."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    stem, _ = _stem_suffixes(name)
    if stem.endswith((".tsv", ".tab")):
        delim = "\t"
    elif stem.endswith(".csv"):
        delim = ","
    else:
        sample = "\n".join(lines[:50])
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except csv.Error:
            delim = max(",\t;|", key=lambda d: lines[0].count(d))
    if lines[0].count(delim) == 0 and (len(lines) < 2 or lines[1].count(delim) == 0):
        return None
    rows = list(csv.reader(io.StringIO("\n".join(lines[: TABLE_ROWS + 1])), delimiter=delim))
    header, body = rows[0], rows[1:]
    width = max([len(header), *(len(r) for r in body)])
    return {"type": "table",
            "delimiter": {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}.get(delim, delim),
            "columns": [_cell(c) for c in header] + [""] * (width - len(header)),
            "column_types": _column_types(body, width),
            "rows": [[_cell(c) for c in r] for r in body],
            "rows_shown": len(body),
            "more_rows": len(lines) > TABLE_ROWS + 1 or cut,
            "note": "The first row is shown as the column names."}


def vcf_preview(text: str, cut: bool) -> dict[str, Any]:
    """A VCF's header facts and its first records."""
    meta, header, records = [], None, []
    for line in text.splitlines():
        if line.startswith("##"):
            meta.append(line)
        elif line.startswith("#CHROM"):
            header = line.lstrip("#").split("\t")
        elif line.strip() and header is not None:
            records.append(line.split("\t"))
            if len(records) >= VCF_RECORDS:
                break
    ids = lambda key: [m.split("ID=", 1)[1].split(",", 1)[0] for m in meta  # noqa: E731
                       if m.startswith(f"##{key}=<") and "ID=" in m]
    assembly = next((m.split("=", 1)[1] for m in meta if m.lower().startswith("##reference=")), None)
    fixed = header[:8] if header else ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO"]
    samples = header[9:] if header and len(header) > 9 else []
    return {"type": "vcf",
            "fileformat": next((m.split("=", 1)[1] for m in meta if m.startswith("##fileformat=")), None),
            "reference": _cell(assembly) if assembly else None,
            "n_contigs": len(ids("contig")),
            "info_fields": ids("INFO")[:80],
            "format_fields": ids("FORMAT")[:80],
            "filters": ids("FILTER")[:40],
            "samples": samples[:200],
            "n_samples": len(samples),
            "columns": fixed,
            "rows": [[_cell(c) for c in r[:8]] for r in records],
            "header_complete": header is not None,
            "more_rows": bool(records) and (len(records) >= VCF_RECORDS or cut)}


def text_file_preview(head: bytes, name: str, size: int) -> dict[str, Any]:
    """The preview of a file read as text from its ``head`` (``size`` = the whole file's size)."""
    truncated = len(head) < size
    if head[:2] == b"\x1f\x8b":
        raw = _gunzip(head)
        truncated = truncated or len(raw) >= TEXT_MAX
        compressed = True
    else:
        raw, compressed = head, False
    kind = classify(name)
    if _looks_binary(raw[:4096]):
        return {"type": "binary", "note": "This file is binary and has no preview. Download it to open it.",
                "compressed": compressed}
    text, cut = _text_of(raw, truncated)
    if kind == "vcf" or text.startswith("##fileformat=VCF"):
        out = vcf_preview(text, cut)
    elif kind == "json":
        out = None
        if not cut:
            try:
                out = {"type": "text", "text": json.dumps(json.loads(text), indent=2, ensure_ascii=False)[:TEXT_MAX]}
            except ValueError:
                out = None
        if out is None:
            out = {"type": "text", "text": "\n".join(text.splitlines()[:TEXT_LINES])}
    else:
        out = table_preview(text, name, cut) if kind == "table" else None
        if out is None:
            lines = text.splitlines()
            out = {"type": "text", "text": "\n".join(lines[:TEXT_LINES]),
                   "more_rows": len(lines) > TEXT_LINES or cut}
    out["compressed"] = compressed
    return out


# --- HDF5 / AnnData -------------------------------------------------------------------------------


def _str(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if hasattr(v, "item") and getattr(v, "shape", None) == ():
        return _str(v.item())
    return str(v)


def _attr(node: Any, key: str, default: Any = None) -> Any:
    try:
        return node.attrs[key] if key in node.attrs else default
    except Exception:  # noqa: BLE001 - an unreadable attribute is just absent
        return default


def _shape(node: Any) -> "list[int] | None":
    shape = getattr(node, "shape", None)
    if shape is not None:
        return [int(x) for x in shape]
    s = _attr(node, "shape")
    return [int(x) for x in s] if s is not None else None


def _frame(group: Any) -> dict[str, Any]:
    """An AnnData ``obs`` or ``var`` group (anndata >= 0.7): its length, columns and first rows."""
    import h5py
    out: dict[str, Any] = {"n": 0, "columns": [], "head": {"index": [], "columns": [], "rows": []}}
    if isinstance(group, h5py.Dataset):            # anndata < 0.7 stored a compound dataset
        names = list(group.dtype.names or [])
        out["n"] = int(group.shape[0])
        out["columns"] = [{"name": n, "dtype": str(group.dtype[n])} for n in names[:H5AD_MAX_COLUMNS]]
        rows = group[: H5AD_HEAD_ROWS]
        out["head"] = {"index": [_str(r[0]) for r in rows], "columns": names[1: 1 + H5AD_HEAD_COLUMNS],
                       "rows": [[_cell(_str(v)) for v in list(r)[1: 1 + H5AD_HEAD_COLUMNS]] for r in rows]}
        out["legacy_format"] = True
        return out
    index_key = _str(_attr(group, "_index", "_index"))
    index = group[index_key] if index_key in group else None
    out["n"] = int(index.shape[0]) if index is not None else 0
    order = [_str(c) for c in _attr(group, "column-order", [])] or [k for k in group.keys() if k != index_key]
    out["n_columns"] = len(order)
    head_cols: list[tuple[str, list[str]]] = []
    for name in order[:H5AD_MAX_COLUMNS]:
        node = group.get(name)
        if node is None:
            continue
        col: dict[str, Any] = {"name": name}
        try:
            if isinstance(node, h5py.Group) and "categories" in node and "codes" in node:
                cats = node["categories"]
                col["dtype"] = "category"
                col["n_categories"] = int(cats.shape[0])
                col["categories"] = [_cell(_str(c)) for c in cats[:H5AD_MAX_CATEGORIES]]
                if len(head_cols) < H5AD_HEAD_COLUMNS:
                    codes = node["codes"][:H5AD_HEAD_ROWS]
                    allcats = cats[: min(int(cats.shape[0]), 10_000)]
                    head_cols.append((name, [_cell(_str(allcats[c])) if 0 <= c < len(allcats) else "NA"
                                             for c in codes]))
            elif isinstance(node, h5py.Group):
                col["dtype"] = _str(_attr(node, "encoding-type", "group"))
            else:
                col["dtype"] = str(node.dtype) if node.dtype.kind not in ("O", "S") else "string"
                if len(head_cols) < H5AD_HEAD_COLUMNS:
                    head_cols.append((name, [_cell(_str(v)) for v in node[:H5AD_HEAD_ROWS]]))
        except Exception as exc:  # noqa: BLE001 - one odd column must not lose the others
            col["note"] = f"unreadable: {type(exc).__name__}"
        out["columns"].append(col)
    if index is not None:
        idx = [_cell(_str(v)) for v in index[:H5AD_HEAD_ROWS]]
        out["head"] = {"index": idx, "columns": [n for n, _ in head_cols],
                       "rows": [[vals[i] if i < len(vals) else "" for _, vals in head_cols]
                                for i in range(len(idx))]}
    return out


def _x_info(f: Any) -> dict[str, Any]:
    """The matrix: its storage, type, shape, and whether a sample of its values is all integers (raw
    counts look like that; normalized or log-transformed data does not)."""
    import h5py
    import numpy as np
    x = f.get("X")
    if x is None:
        return {"present": False}
    info: dict[str, Any] = {"present": True}
    if isinstance(x, h5py.Group):
        info["encoding"] = _str(_attr(x, "encoding-type", _attr(x, "h5sparse_format", "sparse")))
        info["shape"] = _shape(x)
        data = x.get("data")
        if data is not None:
            info["dtype"] = str(data.dtype)
            info["stored_values"] = int(data.shape[0])
            sample = np.asarray(data[:H5AD_SAMPLE_VALUES])
        else:
            sample = np.asarray([])
    else:
        info["encoding"] = "dense"
        info["shape"] = _shape(x)
        info["dtype"] = str(x.dtype)
        # enough leading rows for the sample, not 20 rows of a 30,000-gene dense matrix
        rows = min(int(x.shape[0]), H5AD_SAMPLE_VALUES // max(1, int(x.shape[1])) + 1) if x.ndim == 2 else 0
        sample = np.asarray(x[:rows]).ravel()[:H5AD_SAMPLE_VALUES] if rows else np.asarray([])
    if sample.size:
        finite = sample[np.isfinite(sample)] if sample.dtype.kind == "f" else sample
        if finite.size:
            info["sample"] = {"n": int(sample.size), "min": float(finite.min()), "max": float(finite.max()),
                              "all_integer": bool(np.all(np.mod(finite, 1) == 0)),
                              "any_negative": bool(np.any(finite < 0))}
    return info


def _keys_with_shapes(group: Any) -> list[dict[str, Any]]:
    if group is None or not hasattr(group, "keys"):
        return []
    return [{"name": k, "shape": _shape(group[k])} for k in list(group.keys())[:40]]


def h5ad_preview(source: Any) -> dict[str, Any]:
    """The structure of an AnnData file from a path or a seekable file-like object: no matrix is
    loaded, only metadata, the first rows of ``obs`` and ``var``, and a small sample of ``X``."""
    try:
        import h5py
    except Exception:  # noqa: BLE001 - h5py is a core dependency, but a preview must not crash without it
        return {"type": "h5ad", "note": "h5py is not installed here, so the file's structure cannot be read."}
    out: dict[str, Any] = {"type": "h5ad"}
    try:
        with h5py.File(source, "r") as f:
            out["encoding_version"] = _str(_attr(f, "encoding-version", "")) or None
            out["obs"] = _frame(f["obs"]) if "obs" in f else None
            out["var"] = _frame(f["var"]) if "var" in f else None
            out["X"] = _x_info(f)
            out["n_obs"] = (out["obs"] or {}).get("n") or ((out["X"].get("shape") or [0, 0])[0])
            out["n_vars"] = (out["var"] or {}).get("n") or ((out["X"].get("shape") or [0, 0])[1])
            out["layers"] = _keys_with_shapes(f.get("layers"))
            out["obsm"] = _keys_with_shapes(f.get("obsm"))
            out["varm"] = _keys_with_shapes(f.get("varm"))
            out["obsp"] = _keys_with_shapes(f.get("obsp"))
            uns = f.get("uns")
            out["uns"] = list(uns.keys())[:60] if uns is not None and hasattr(uns, "keys") else []
            raw = f.get("raw")
            out["raw"] = ({"present": True, "X": _shape(raw["X"]) if "X" in raw else None,
                           "n_vars": _frame(raw["var"])["n"] if "var" in raw else None}
                          if raw is not None else {"present": False})
    except Exception as exc:  # noqa: BLE001 - a damaged file is described, not raised
        out["note"] = f"The file could not be read as HDF5 ({type(exc).__name__}: {str(exc)[:200]})."
    return out


def hdf5_preview(source: Any) -> dict[str, Any]:
    """The group and dataset tree of any HDF5 file (a 10x ``.h5``, a ``.loom``), without reading data."""
    try:
        import h5py
    except Exception:  # noqa: BLE001
        return {"type": "hdf5", "note": "h5py is not installed here, so the file's structure cannot be read."}
    nodes: list[dict[str, Any]] = []
    out: dict[str, Any] = {"type": "hdf5", "nodes": nodes}
    try:
        with h5py.File(source, "r") as f:
            out["attrs"] = {k: _cell(_str(v)) for k, v in list(f.attrs.items())[:20]}

            def visit(name: str, obj: Any) -> "bool | None":
                if len(nodes) >= HDF5_MAX_NODES:
                    out["more_nodes"] = True
                    return True
                if isinstance(obj, h5py.Dataset):
                    nodes.append({"path": name, "kind": "dataset", "shape": _shape(obj), "dtype": str(obj.dtype)})
                else:
                    nodes.append({"path": name, "kind": "group"})
                return None

            f.visititems(visit)
            matrix = f.get("matrix")                          # Cell Ranger's feature-barcode matrix
            if matrix is not None and "shape" in matrix:
                genes, cells = (int(v) for v in matrix["shape"][:2])
                out["cellranger"] = {"features": genes, "barcodes": cells}
                ftype = matrix.get("features/feature_type")
                if ftype is not None:
                    kinds: dict[str, int] = {}
                    for v in ftype[: min(int(ftype.shape[0]), 200_000)]:
                        kinds[_str(v)] = kinds.get(_str(v), 0) + 1
                    out["cellranger"]["feature_types"] = kinds
    except Exception as exc:  # noqa: BLE001
        out["note"] = f"The file could not be read as HDF5 ({type(exc).__name__}: {str(exc)[:200]})."
    return out


# --- folders and paths ----------------------------------------------------------------------------


def safe_relpath(rel: "str | None") -> "str | None":
    """A file path inside a folder dataset, as the browser sent it, normalised; None when it could leave
    the folder (absolute, ``..``, a NUL byte) or is empty."""
    if not rel:
        return None
    rel = str(rel).replace("\\", "/")
    if "\x00" in rel or rel.startswith("/"):
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return posixpath.join(*parts)


def local_folder_entries(root: Path, limit: int = FOLDER_MAX_ENTRIES) -> dict[str, Any]:
    """The files under a local folder, as ``{entries: [{path, size}], total_files, total_bytes,
    truncated}``. Symlinks are not followed, so a link cannot list or reach files outside the folder."""
    entries: list[dict[str, Any]] = []
    total_files = total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if name.startswith(".") or full.is_symlink() or not full.is_file():
                continue
            size = full.stat().st_size
            total_files += 1
            total_bytes += size
            if len(entries) < limit:
                entries.append({"path": full.relative_to(root).as_posix(), "size": size})
    return {"entries": entries, "total_files": total_files, "total_bytes": total_bytes,
            "truncated": total_files > len(entries)}


def parse_find_listing(text: str, limit: int = FOLDER_MAX_ENTRIES) -> dict[str, Any]:
    """Parse ``find <dir> -type f -printf '%P\\t%s\\n'`` output into the same shape as
    :func:`local_folder_entries`."""
    entries: list[dict[str, Any]] = []
    total_files = total_bytes = 0
    for line in sorted(text.splitlines()):
        rel, _, size = line.rpartition("\t")
        rel = safe_relpath(rel)
        if rel is None or any(p.startswith(".") for p in rel.split("/")):
            continue
        try:
            n = int(size)
        except ValueError:
            continue
        total_files += 1
        total_bytes += n
        if len(entries) < limit:
            entries.append({"path": rel, "size": n})
    return {"entries": entries, "total_files": total_files, "total_bytes": total_bytes,
            "truncated": total_files > len(entries)}
