"""General, LLM-driven file INGEST/TRIAGE — "skim any uploaded file and get the gist".

THE PROBLEM: today a freshly uploaded file is identified by its SUFFIX only
(``gateway/app.py`` ``_primary_suffix`` / ``_PREVIEW_KIND``). That is enough to route the
common cases (an ``.h5ad`` IS a matrix, a ``.vcf`` IS a callset) but it is blind to
everything the suffix does not say — which GENOME BUILD a VCF is on, which SAMPLE(s) it
carries, which caller wrote it, whether it is bgzipped — and it says NOTHING at all about a
file with a wrong, missing, or unknown extension. Rui's requirement is the opposite posture:
"no matter what the file is, the agent should FIRST skim it and get the gist." That is content
understanding, not regex-on-the-filename.

THE DESIGN — the same split the HPO mapper uses, code owns facts, the LLM does language:

  1. PEEK (code)      :func:`peek_dataset` reads a BOUNDED head of the file and returns a
                      deterministic evidence dict — magic bytes, size, and format-aware samples
                      where cheap (VCF header → assembly / sample ids / caller / chr-prefix /
                      bgzip?; HDF5 group tree via h5py WITHOUT loading matrices; tabular delimiter
                      + column names + a few rows; a ``.gz`` head decompressed just enough;
                      otherwise first N text lines or a hexdump snippet). It NEVER raises: a
                      corrupt / truncated / binary / unknown file degrades to a best-effort
                      descriptor with a note, never an exception. No model is involved, so it runs
                      the instant a byte lands — before any GPU exists.
  2. DESCRIBE (LLM)   :func:`describe_dataset` hands that evidence (never the raw file) to the
                      served model and gets back structured JSON: file_kind / format / assembly? /
                      sample_ids? / likely_modality / key_facts / one_line_summary / confidence.
                      The model may only NAME and SUMMARISE what the evidence shows; it is told to
                      answer "unknown" rather than invent specifics. Deterministic facts the peek
                      already established (assembly, sample ids, format, compression) are stamped
                      back OVER the model's answer, so the model can never contradict the bytes.
                      With no model reachable, describe returns a deterministic fallback built from
                      the peek — lower fidelity, still structured, clearly labelled.

WHERE THIS RUNS (the upload-time / no-GPU decision) — the two halves run at DIFFERENT times on
purpose. An upload must never depend on a served model: allocating one takes ~10 min and costs an
A100, and files are routinely uploaded outside a live session. Therefore:

  * PEEK runs synchronously at upload (``/api/upload`` + the chunked-upload finalize), on the
    still-local file BEFORE it is staged to dfs3b, for instant deterministic feedback.
  * DESCRIBE runs only when a model already exists — surfaced through the on-demand
    ``POST /api/dataset/describe`` endpoint, which checks vLLM is reachable and, if not, returns
    the peek plus a "bring the model up" note WITHOUT provisioning anything.
  * The in-process :func:`make_inspect_dataset_tool` (``inspect_dataset``) does both in one call
    for the orchestrator, using this session's tunnel — so a research run can skim its bound
    dataset mid-plan.

WHAT THIS DELIBERATELY DOES NOT DO: it does not replace the suffix-based ``_primary_suffix``
routing (that still picks a folder's primary file); it AUGMENTS it. And it does not do the
multi-file / bind-set work (that is a separate feature). This module is import-clean without
``paramiko`` or the gateway (mirrors ``tools/hpo_terms/mapper.py``), so it unit-tests on a bare
checkout; ``h5py`` is an OPTIONAL import and its absence degrades gracefully.
"""
from __future__ import annotations

import binascii
import csv
import io
import json
import os
import re
import zlib
from typing import Any, Callable

ChatFn = Callable[[list[dict]], str]

# How much of the file HEAD to read. Generous enough for a real VCF header (many contigs + INFO/
# FORMAT/FILTER defs can run to a few hundred KB, and bgzip must be decompressed block-by-block to
# reach the ``#CHROM`` line) while staying a cheap bounded read — this is a peek, not a load.
_DEFAULT_MAX_BYTES = 262_144
# What actually goes to the model: the peek's samples are trimmed to this before serialisation, so a
# huge header cannot blow the prompt budget (and only a bounded head ever leaves the gateway).
_LLM_SAMPLE_CHARS = 4_000
_LLM_SAMPLE_LINES = 40
_MAX_HDF5_KEYS = 200          # cap the HDF5 tree walk so a pathological file can't spin


# --- magic bytes -------------------------------------------------------------

# (predicate over the leading bytes) -> a short label. Order matters: bgzip is a SPECIALisation of
# gzip and must be tested first.
def _magic_label(head: bytes) -> str:
    if head[:4] == b"\x1f\x8b\x08\x04" and b"BC" in head[10:18]:
        return "bgzip"                                 # gzip w/ the BAM/BGZF "BC" extra subfield
    if head[:2] == b"\x1f\x8b":
        return "gzip"
    if head[:8] == b"\x89HDF\r\n\x1a\n":
        return "hdf5"
    if head[:4] == b"PK\x03\x04" or head[:4] == b"PK\x05\x06":
        return "zip"
    if head[:3] == b"BZh":
        return "bzip2"
    if head[:4] == b"\xfd7zXZ"[:4]:
        return "xz"
    if head[:4] == b"BAM\x01":
        return "bam"
    if head[:4] == b"CRAM":
        return "cram"
    if head[:5] == b"%PDF-":
        return "pdf"
    if head[:4] == b"\x93NUMPY":
        return "npy"
    if head[:6] == b"SQLite":
        return "sqlite"
    return ""


# chr1 length -> genome build. VCF ``##contig`` lines carry the length; when there is no explicit
# ``assembly=`` / ``##reference`` token this pins the build deterministically (same trick the
# variant line uses for header auto-detection).
_CHR1_LEN_TO_BUILD = {
    "248956422": "GRCh38",
    "249250621": "GRCh37",
    "247249719": "NCBI36",       # hg18
}

# Caller signatures that appear in a VCF header (``##source=`` or a tool-specific meta line).
# MOST-SPECIFIC FIRST — a bare "GATK" must not pre-empt "GATK HaplotypeCaller".
_CALLER_SIGNS = (
    ("HaplotypeCaller", "GATK HaplotypeCaller"), ("Mutect", "GATK Mutect2"),
    ("DeepVariant", "DeepVariant"), ("freeBayes", "freebayes"), ("freebayes", "freebayes"),
    ("bcftools", "bcftools"), ("VarScan", "VarScan"), ("strelka", "Strelka"), ("Strelka", "Strelka"),
    ("Platypus", "Platypus"), ("Dragen", "DRAGEN"), ("DRAGEN", "DRAGEN"), ("Sentieon", "Sentieon"),
    ("GATK", "GATK"),
)


def _looks_binary(sample: bytes) -> bool:
    """Heuristic: a NUL byte, or many non-text bytes, means this is not text to sample as lines."""
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    text_bytes = bytes(range(0x20, 0x7F)) + b"\t\n\r\f\v\b"
    nontext = sum(1 for b in sample if b not in text_bytes)
    return nontext / max(1, len(sample)) > 0.30


def _hexdump(head: bytes, n: int = 64) -> str:
    """A compact hex/ascii snippet of the leading bytes for an otherwise-opaque binary file."""
    chunk = head[:n]
    hexs = binascii.hexlify(chunk, " ").decode("ascii")
    ascii_ = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
    return f"{hexs}  |{ascii_}|"


def _gunzip_head(data: bytes, max_out: int) -> bytes:
    """Decompress up to ``max_out`` bytes from the head of a (possibly MULTI-member, possibly
    TRUNCATED) gzip/bgzip stream. bgzip is a run of concatenated gzip members, and we only hold a
    bounded head, so the last member is almost always truncated — every zlib error is swallowed and
    whatever decompressed so far is returned. Never raises."""
    out = bytearray()
    remaining = data
    while remaining and len(out) < max_out:
        dobj = zlib.decompressobj(wbits=31)            # 31 = gzip wrapper
        try:
            out += dobj.decompress(remaining, max_out - len(out))
        except (zlib.error, OSError):
            break
        if not dobj.eof:                               # member unfinished (truncated head) → stop
            break
        remaining = dobj.unused_data                   # advance to the next concatenated member
    return bytes(out)


# --- VCF ---------------------------------------------------------------------


def _parse_vcf_header(text: str) -> "dict[str, Any] | None":
    """Pull the useful facts out of a VCF header sample. Returns None if the text is not a VCF.

    Deterministic and defensive: everything is best-effort and a missing field is simply omitted."""
    lines = text.splitlines()
    if not lines:
        return None
    # A VCF starts with ``##fileformat=VCF``; but a sample that begins mid-stream (a truncated bgzip
    # member) may miss it, so also accept a ``#CHROM`` column line or a run of ``##`` meta lines.
    meta = [ln for ln in lines if ln.startswith("##")]
    chrom_line = next((ln for ln in lines if ln.startswith("#CHROM")), "")
    is_vcf = any(ln.startswith("##fileformat=VCF") for ln in lines) or bool(chrom_line) or len(meta) >= 3
    if not is_vcf:
        return None

    info: dict[str, Any] = {"n_meta_lines": len(meta)}
    joined = "\n".join(meta)

    for ln in meta:
        if ln.startswith("##fileformat="):
            info["fileformat"] = ln.split("=", 1)[1].strip()
            break

    # assembly: explicit tokens first (##reference, ##contig ...assembly=..., or any hgNN/GRChNN),
    # then fall back to the chr1 contig length.
    assembly = ""
    m = re.search(r"assembly=([A-Za-z0-9_.]+)", joined)
    if m:
        assembly = m.group(1)
    if not assembly:
        m = re.search(r"\b(GRCh38|GRCh37|hg38|hg19|hg18|b37|b38|NCBI36|NCBI37)\b", joined)
        if m:
            assembly = m.group(1)
    if not assembly:
        for ln in meta:
            cm = re.search(r"##contig=<[^>]*\bID=(?:chr)?1\b[^>]*\blength=(\d+)", ln)
            if cm and cm.group(1) in _CHR1_LEN_TO_BUILD:
                assembly = _CHR1_LEN_TO_BUILD[cm.group(1)] + " (from chr1 length)"
                break
    if assembly:
        info["assembly"] = assembly

    # caller / source
    src = next((ln.split("=", 1)[1].strip() for ln in meta if ln.startswith("##source=")), "")
    caller = ""
    for needle, label in _CALLER_SIGNS:
        if needle in joined:
            caller = label
            break
    if src:
        info["source"] = src[:200]
    if caller:
        info["caller"] = caller

    # chr-prefix: from the first contig ID, or the first data line's CHROM
    contig_ids = re.findall(r"##contig=<[^>]*\bID=([^,>]+)", joined)
    if contig_ids:
        info["contigs_sample"] = contig_ids[:12]
        info["chr_prefix"] = contig_ids[0].startswith("chr")
    data_line = next((ln for ln in lines if ln and not ln.startswith("#")), "")
    if data_line:
        chrom = data_line.split("\t", 1)[0]
        info.setdefault("chr_prefix", chrom.startswith("chr"))
        info["first_record_chrom"] = chrom

    # sample ids: columns 10+ of the #CHROM line (after the 9 fixed VCF columns)
    if chrom_line:
        cols = chrom_line.lstrip("#").split("\t")
        samples = cols[9:] if len(cols) > 9 else []
        info["sample_ids"] = [s.strip() for s in samples if s.strip()]
        info["n_samples"] = len(info["sample_ids"])
        info["has_genotypes"] = len(cols) > 8            # a FORMAT column implies per-sample GTs
    return info


# --- HDF5 (.h5ad / .h5 / .loom) ----------------------------------------------


def _peek_hdf5(path: str) -> dict[str, Any]:
    """The HDF5 group/dataset TREE via h5py — names, shapes, dtypes, WITHOUT reading any matrix.
    Degrades to a note (never raises) when h5py is missing or the file will not open."""
    try:
        import h5py  # optional dep — absence must degrade, not crash
    except Exception:  # noqa: BLE001
        return {"h5py": False, "note": "h5py not installed — HDF5 structure not inspected "
                                       "(magic bytes still identify it as HDF5)."}
    out: dict[str, Any] = {"h5py": True, "root_keys": [], "datasets": []}
    try:
        with h5py.File(path, "r") as f:
            out["root_keys"] = list(f.keys())[:64]
            out["root_attrs"] = {k: _scalar(f.attrs[k]) for k in list(f.attrs.keys())[:32]}
            count = 0

            def _visit(name: str, obj: Any) -> "Any | None":
                nonlocal count
                if count >= _MAX_HDF5_KEYS:
                    return True                          # stop the walk
                try:
                    import h5py as _h
                    if isinstance(obj, _h.Dataset):
                        out["datasets"].append(
                            {"name": name, "shape": list(obj.shape), "dtype": str(obj.dtype)})
                        count += 1
                except Exception:  # noqa: BLE001
                    pass
                return None

            f.visititems(_visit)
            # AnnData (.h5ad) convention: obs/var/X/layers/obsm — surface if present.
            anndata = {g: list(f[g].keys())[:40] for g in ("obs", "var", "layers", "obsm", "uns")
                       if g in f and hasattr(f[g], "keys")}
            if "X" in f:
                x = f["X"]
                anndata["X"] = ({"shape": list(x.shape), "dtype": str(x.dtype)}
                                if hasattr(x, "shape") else list(x.keys())[:8])
            if anndata:
                out["anndata"] = anndata
    except Exception as exc:  # noqa: BLE001 - a malformed HDF5 must not crash the peek
        out["note"] = f"HDF5 open/inspect failed: {type(exc).__name__}: {exc}"[:300]
    return out


def _scalar(v: Any) -> Any:
    """Make an HDF5 attribute JSON-safe (bytes -> str, numpy -> python)."""
    try:
        if isinstance(v, bytes):
            return v.decode("utf-8", "replace")
        if hasattr(v, "tolist"):
            return v.tolist()
        return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
    except Exception:  # noqa: BLE001
        return str(v)


# --- tabular (.csv / .tsv / delimited text) ----------------------------------


def _peek_tabular(text: str, *, prefer: str = "") -> "dict[str, Any] | None":
    """Sniff a delimited table from a text sample: delimiter, column names, a few rows. Returns None
    when the text does not look tabular. Best-effort — a ragged file still yields what it can."""
    lines = [ln for ln in text.splitlines() if ln != ""]
    if not lines:
        return None
    sample = "\n".join(lines[:50])
    delim = prefer
    if not delim:
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except csv.Error:
            # fall back to whichever common delimiter is most frequent in the first line
            first = lines[0]
            delim = max(",\t;|", key=lambda d: first.count(d))
            if first.count(delim) == 0:
                return None
    try:
        rows = list(csv.reader(io.StringIO(sample), delimiter=delim))
    except csv.Error:
        return None
    rows = [r for r in rows if r]
    if not rows:
        return None
    header = rows[0]
    # a single column with no delimiter present is probably not a real table
    if len(header) < 2 and delim not in lines[0]:
        return None
    return {
        "delimiter": {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}.get(delim, delim),
        "n_columns": len(header),
        "columns": [c.strip()[:60] for c in header][:60],
        "rows_sample": [[c[:40] for c in r][:20] for r in rows[1:6]],
    }


# --- the peek ----------------------------------------------------------------


def _known_suffix(name: str) -> str:
    """Longest-match known dataset suffix (``.vcf.gz`` beats ``.gz``); '' if none recognised."""
    low = (name or "").lower()
    for suf in (".vcf.gz", ".vcf.bgz", ".h5ad", ".h5ad.gz", ".bcf", ".vcf", ".h5", ".hdf5",
                ".loom", ".csv.gz", ".tsv.gz", ".txt.gz", ".csv", ".tsv", ".txt", ".json",
                ".gz", ".bgz", ".bam", ".cram", ".pdf"):
        if low.endswith(suf):
            return suf
    ext = os.path.splitext(low)[1]
    return ext or ""


def peek_dataset(path: str, *, max_bytes: int = _DEFAULT_MAX_BYTES,
                 size_hint: "int | None" = None, name: "str | None" = None) -> dict[str, Any]:
    """Robust, DETERMINISTIC peek at an uploaded file. NEVER raises.

    Reads at most ``max_bytes`` from the file head and returns a compact evidence dict describing
    what the file appears to be — magic bytes, size, and format-aware samples for the cheap-to-parse
    formats (VCF header, HDF5 tree, delimited table, gz head). An unknown / corrupt / binary file
    still returns a best-effort descriptor (first text lines or a hexdump), with the failure noted
    rather than thrown.

    ``size_hint`` — when the file on disk is only a bounded SAMPLE of a larger original (e.g. a
    ``head`` of a remote dfs3b VCF), pass the true size here so ``size_bytes`` is honest and
    ``sample_truncated`` is set. ``name`` overrides the basename used for suffix detection (useful
    when the sample was written to a temp file)."""
    ev: dict[str, Any] = {"tool": "peek_dataset"}
    basename = name or os.path.basename(path or "")
    ev["name"] = basename
    ev["suffix"] = _known_suffix(basename)
    ev["notes"] = []

    # size + existence
    try:
        disk_size = os.path.getsize(path)
        ev["exists"] = True
    except OSError as exc:
        ev["exists"] = False
        ev["readable"] = False
        ev["size_bytes"] = size_hint if size_hint is not None else 0
        ev["error"] = f"cannot stat file: {type(exc).__name__}: {exc}"[:200]
        ev["detected_format"] = "unknown"
        return ev
    ev["size_bytes"] = size_hint if size_hint is not None else disk_size
    if size_hint is not None and size_hint > disk_size:
        ev["sample_truncated"] = True

    # read the bounded head
    try:
        with open(path, "rb") as fh:
            head = fh.read(max_bytes)
        ev["readable"] = True
    except OSError as exc:
        ev["readable"] = False
        ev["error"] = f"cannot read file: {type(exc).__name__}: {exc}"[:200]
        ev["detected_format"] = "unknown"
        return ev

    ev["head_bytes_read"] = len(head)
    magic = _magic_label(head)
    if magic:
        ev["magic"] = magic
    ev["magic_hex"] = binascii.hexlify(head[:16]).decode("ascii")

    # every format probe below is individually guarded so one failure never sinks the peek
    try:
        _classify(path, basename, head, magic, ev, max_bytes)
    except Exception as exc:  # noqa: BLE001 - the whole point is to never raise on weird input
        ev.setdefault("detected_format", "unknown")
        ev["notes"].append(f"classification error: {type(exc).__name__}: {exc}"[:200])
    return ev


def _classify(path: str, basename: str, head: bytes, magic: str, ev: dict[str, Any],
              max_bytes: int) -> None:
    """Populate ``ev`` with a detected_format and format-specific evidence. Split out so
    :func:`peek_dataset` can wrap it in one guard."""
    low = basename.lower()

    # 1. HDF5 family (.h5ad / .h5 / .loom) — identified by magic, inspected via h5py
    if magic == "hdf5" or low.endswith((".h5ad", ".h5", ".hdf5", ".loom")):
        ev["detected_format"] = "hdf5"
        ev["hdf5"] = _peek_hdf5(path)
        if low.endswith(".h5ad") or ev["hdf5"].get("anndata"):
            ev["detected_format"] = "h5ad"
        elif low.endswith(".loom"):
            ev["detected_format"] = "loom"
        return

    # 2. gzip / bgzip — decompress a head and re-classify the plaintext (usually a VCF)
    if magic in ("gzip", "bgzip"):
        ev["compression"] = magic
        plain = _gunzip_head(head, max_bytes)
        text = plain.decode("utf-8", "replace")
        vcf = _parse_vcf_header(text)
        if vcf is not None:
            ev["detected_format"] = "vcf"
            ev["bgzip"] = magic == "bgzip"
            ev["vcf"] = vcf
            return
        tab = _peek_tabular(text, prefer="\t" if ".tsv" in low else ("," if ".csv" in low else ""))
        if tab is not None:
            ev["detected_format"] = "tabular"
            ev["tabular"] = tab
            return
        ev["detected_format"] = "gzip"
        ev["text_sample"] = _trim_text(text)
        if not text.strip():
            ev["notes"].append("gzip head did not decompress to readable text (opaque/binary payload).")
        return

    # 3. other compressed / known-binary containers we can name but not sample as text
    if magic in ("zip", "bzip2", "xz", "bam", "cram", "sqlite", "npy", "pdf"):
        ev["detected_format"] = magic
        ev["hexdump"] = _hexdump(head)
        return

    # 4. binary with no known magic → opaque
    if _looks_binary(head):
        ev["detected_format"] = "binary"
        ev["hexdump"] = _hexdump(head)
        ev["notes"].append("unrecognised binary file — described by magic bytes only.")
        return

    # 5. plain text → try VCF, then tabular, else raw text lines
    text = head.decode("utf-8", "replace")
    vcf = _parse_vcf_header(text)
    if vcf is not None:
        ev["detected_format"] = "vcf"
        ev["bgzip"] = False
        ev["vcf"] = vcf
        return
    if low.endswith(".json") or text.lstrip()[:1] in ("{", "["):
        ev["detected_format"] = "json"
        ev["text_sample"] = _trim_text(text)
        return
    tab = _peek_tabular(text, prefer="\t" if low.endswith(".tsv") else ("," if low.endswith(".csv") else ""))
    if tab is not None:
        ev["detected_format"] = "tabular"
        ev["tabular"] = tab
        return
    ev["detected_format"] = "text"
    ev["text_sample"] = _trim_text(text)
    ev["line_sample"] = text.splitlines()[:_LLM_SAMPLE_LINES]


def _trim_text(text: str) -> str:
    return text[:_LLM_SAMPLE_CHARS]


# --- the LLM describe --------------------------------------------------------


_DESCRIBE_SYSTEM = (
    "You are a bioinformatics data-triage assistant. You are given DETERMINISTIC evidence about an "
    "uploaded file (magic bytes, size, and format-aware samples already extracted by code). Describe "
    "what the file is. Return STRICT JSON only — one object, no prose, no code fence:\n"
    '{"file_kind": "<short human phrase>", "format": "<VCF|HDF5/AnnData|CSV|TSV|text|... or unknown>", '
    '"assembly": "<GRCh38|GRCh37|... or null>", "sample_ids": ["..."] , '
    '"likely_modality": "<variants|scrna|bulk_rna|tabular|text|image|unknown|...>", '
    '"key_facts": ["<short fact grounded in the evidence>"], '
    '"one_line_summary": "<one sentence a scientist would recognise>", '
    '"confidence": "<high|medium|low>"}\n'
    "Rules:\n"
    "- Use ONLY the evidence given. NEVER invent an assembly, sample id, gene, caller, row count, or "
    "any specific the evidence does not contain. If a field is not supported, use null (or [] for "
    "lists) and say so in one key_fact.\n"
    "- An UNKNOWN or unrecognised file is still describable: say what the bytes DO show (e.g. 'binary "
    "file, no recognised magic, ~4 MB') and set likely_modality='unknown', confidence='low'. Never "
    "refuse.\n"
    "- key_facts are short, each grounded in the evidence. one_line_summary is what you would tell a "
    "scientist glancing at their upload.\n"
    "- confidence reflects how strongly the EVIDENCE (not your priors) pins the answer."
)


def _loads(raw: str) -> Any:
    """Tolerant JSON parse of a model reply (strips a code fence / surrounding prose); None on fail."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _evidence_for_llm(peek: dict[str, Any]) -> dict[str, Any]:
    """A compact, trimmed copy of the peek to hand the model — big samples are already bounded by
    the peek, but re-trim defensively so a huge header never dominates the prompt."""
    ev = dict(peek)
    if isinstance(ev.get("text_sample"), str):
        ev["text_sample"] = ev["text_sample"][:_LLM_SAMPLE_CHARS]
    if isinstance(ev.get("line_sample"), list):
        ev["line_sample"] = ev["line_sample"][:_LLM_SAMPLE_LINES]
    hdf5 = ev.get("hdf5")
    if isinstance(hdf5, dict) and isinstance(hdf5.get("datasets"), list):
        hdf5 = dict(hdf5)
        hdf5["datasets"] = hdf5["datasets"][:60]
        ev["hdf5"] = hdf5
    return ev


def _deterministic_summary(peek: dict[str, Any]) -> dict[str, Any]:
    """A structured description built from the peek ALONE — the no-LLM fallback, and the skeleton the
    LLM answer is grounded against. Honest and specific-free beyond what the bytes show."""
    fmt = peek.get("detected_format", "unknown")
    vcf = peek.get("vcf") or {}
    hdf5 = peek.get("hdf5") or {}
    modality = {
        "vcf": "variants", "h5ad": "scrna", "loom": "scrna", "tabular": "tabular",
        "text": "text", "json": "text",
    }.get(fmt, "unknown")
    size = int(peek.get("size_bytes") or 0)
    size_h = f"{size / 1e9:.2f} GB" if size >= 1e9 else (f"{size / 1e6:.1f} MB" if size >= 1e6
                                                          else f"{size / 1e3:.1f} KB")
    facts: list[str] = [f"size ~{size_h}", f"detected format: {fmt}"]
    assembly = vcf.get("assembly")
    samples = vcf.get("sample_ids")
    if fmt == "vcf":
        kind = "VCF variant callset" + (" (bgzipped)" if peek.get("bgzip") else "")
        if assembly:
            facts.append(f"assembly {assembly}")
        if vcf.get("caller"):
            facts.append(f"caller {vcf['caller']}")
        if samples is not None:
            facts.append(f"{vcf.get('n_samples', len(samples))} sample(s)")
        one = f"A {kind}" + (f" on {assembly}" if assembly else "") + (
            f" with {vcf.get('n_samples')} sample(s)" if vcf.get("n_samples") else "") + "."
    elif fmt in ("h5ad", "loom"):
        kind = "AnnData single-cell matrix (.h5ad)" if fmt == "h5ad" else "Loom single-cell matrix"
        if isinstance(hdf5.get("anndata"), dict) and isinstance(hdf5["anndata"].get("X"), dict):
            facts.append(f"X shape {hdf5['anndata']['X'].get('shape')}")
        one = f"An HDF5 {kind}."
    elif fmt == "tabular":
        tab = peek.get("tabular") or {}
        kind = f"{tab.get('delimiter', 'delimited')}-delimited table"
        facts.append(f"{tab.get('n_columns', '?')} columns")
        one = f"A {kind} with {tab.get('n_columns', '?')} columns."
    elif fmt in ("binary", "gzip", "zip", "bzip2", "xz", "bam", "cram", "sqlite", "npy", "pdf"):
        kind = f"{fmt} file"
        one = f"A {kind} identified by its magic bytes (~{size_h})."
    else:
        kind = f"{fmt} file"
        one = f"A {fmt} file (~{size_h})."
    return {
        "file_kind": kind,
        "format": fmt,
        "assembly": assembly,
        "sample_ids": samples,
        "likely_modality": modality,
        "key_facts": facts,
        "one_line_summary": one,
        "confidence": "medium" if fmt not in ("unknown", "binary") else "low",
        "source": "deterministic",
    }


# fields the CODE owns — if the peek established them, the model's answer is overwritten with the
# deterministic value so a description can never contradict the bytes.
def _ground(desc: dict[str, Any], peek: dict[str, Any]) -> dict[str, Any]:
    vcf = peek.get("vcf") or {}
    fmt = peek.get("detected_format")
    if fmt and fmt != "unknown":
        # map internal format to a display value but keep the model's if it is compatible
        desc.setdefault("format", fmt)
    if vcf.get("assembly"):
        desc["assembly"] = vcf["assembly"]
    if vcf.get("sample_ids") is not None:
        desc["sample_ids"] = vcf["sample_ids"]
    return desc


def describe_dataset(peek: dict[str, Any], *, chat_fn: "ChatFn | None") -> dict[str, Any]:
    """Turn a :func:`peek_dataset` evidence dict into a structured "what is this" via the LLM.

    Returns ``{file_kind, format, assembly?, sample_ids?, likely_modality, key_facts[],
    one_line_summary, confidence, source}``. ``source`` is ``"llm"`` when the model produced a usable
    answer, else ``"deterministic"`` (no model reachable, or an unusable reply — the peek-only
    fallback). Deterministic facts from the peek are always stamped back over the answer so the model
    cannot contradict the evidence. Never raises."""
    fallback = _deterministic_summary(peek)
    if chat_fn is None:
        return fallback
    try:
        reply = chat_fn([
            {"role": "system", "content": _DESCRIBE_SYSTEM},
            {"role": "user", "content": "File evidence (JSON):\n"
             + json.dumps(_evidence_for_llm(peek), ensure_ascii=False)[:12_000]},
        ])
    except Exception:  # noqa: BLE001 - an LLM outage degrades to the deterministic summary
        fallback["note"] = "LLM unreachable — deterministic description."
        return fallback
    data = _loads(reply)
    if not isinstance(data, dict):
        fallback["note"] = "LLM reply was not usable JSON — deterministic description."
        return fallback

    out: dict[str, Any] = {
        "file_kind": str(data.get("file_kind") or fallback["file_kind"])[:200],
        "format": str(data.get("format") or fallback["format"])[:80],
        "assembly": data.get("assembly") if data.get("assembly") not in ("", "null") else None,
        "sample_ids": data.get("sample_ids") if isinstance(data.get("sample_ids"), list) else None,
        "likely_modality": str(data.get("likely_modality") or fallback["likely_modality"])[:40],
        "key_facts": [str(x)[:200] for x in data.get("key_facts", [])][:12]
        if isinstance(data.get("key_facts"), list) else fallback["key_facts"],
        "one_line_summary": str(data.get("one_line_summary") or fallback["one_line_summary"])[:400],
        "confidence": str(data.get("confidence") or "low")[:20],
        "source": "llm",
    }
    return _ground(out, peek)


# --- combined + the Scientist tool -------------------------------------------


def inspect_dataset(path: str, *, chat_fn: "ChatFn | None" = None,
                    max_bytes: int = _DEFAULT_MAX_BYTES,
                    size_hint: "int | None" = None, name: "str | None" = None) -> dict[str, Any]:
    """Peek a file then describe it — the whole "skim any file and get the gist" in one call.

    ``chat_fn=None`` returns peek + a deterministic description (no model needed). Returns
    ``{status, peek, description}`` and never raises."""
    peek = peek_dataset(path, max_bytes=max_bytes, size_hint=size_hint, name=name)
    description = describe_dataset(peek, chat_fn=chat_fn)
    return {
        "status": "ok" if peek.get("readable") else "unreadable",
        "tool": "inspect_dataset",
        "peek": peek,
        "description": description,
        "raw_data_to_llm": chat_fn is not None,   # a bounded head sample reaches the served model
    }


def make_inspect_dataset_tool() -> Any:
    """The ``inspect_dataset`` tool: skim ANY uploaded file (known type or not) and report a
    structured "what is this". Runs IN-PROCESS on the gateway (no HPC3): reads a bounded head and,
    when this session has a served model, describes it via ``ctx.tunnel_port`` (``think=False``,
    load-bearing — the served Qwen3.6 is a reasoning model and a bounded call with thinking ON can
    return empty content). Follows the ``make_hpo_mapping_tool`` template."""
    from ..agents.research_harness import HarnessTool

    def _exec(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        decisions = getattr(ctx, "decisions", None) or {}
        # the run's bound dataset is authoritative; an explicit `path` arg is a fallback
        path = str(args.get("path") or decisions.get("dataset_path") or "").strip()
        if not path:
            return {"status": "error", "tool": "inspect_dataset",
                    "error": "inspect_dataset needs a file path — pass `path`, or bind a dataset to "
                             "the run. It does not guess."}

        chat_fn: "ChatFn | None" = None
        port = getattr(ctx, "tunnel_port", None)
        if port is not None:
            from ..gateway import vllm_client       # deferred: keep this module gateway-decoupled

            def chat_fn(messages: list[dict]) -> str:      # noqa: F811 - bound only when served
                # think=False is LOAD-BEARING (same reason as map_phenotype_to_hpo): a bounded call to
                # the served REASONING model can spend its whole budget on the thinking trace and
                # return empty content. Triage is a structured-JSON task needing no chain-of-thought.
                return vllm_client.complete(port, getattr(ctx, "model", ""), messages,
                                            max_tokens=1200, timeout=90.0, think=False)

        return inspect_dataset(path, chat_fn=chat_fn)

    return HarnessTool(
        "inspect_dataset",
        "Skim an uploaded data file — ANY type, known extension or not — and return a structured "
        "'what is this': format, genome assembly and sample id(s) for a VCF, the group tree for an "
        ".h5ad/HDF5 matrix, columns for a table, or a best-effort descriptor for an unknown/binary "
        "file. Use this FIRST when you are unsure what a bound dataset actually is, instead of "
        "assuming from the filename. Reads only a bounded head; never loads the whole file. Returns "
        "`peek` (deterministic evidence) and `description` (file_kind, likely_modality, "
        "one_line_summary, confidence) — do not invent facts beyond what it reports.",
        {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "server-side path of the file to skim. Omit to use the dataset "
                                    "already bound to this run."}}},
        _exec,
        reads_private_data=True, category="qc",
    )
