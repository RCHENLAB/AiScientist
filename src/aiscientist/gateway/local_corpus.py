"""Offline CPU retrieval from an Eye Server copy of the corpus index.

The copy is written by ``corpus_index`` (``scripts/update_corpus_index.py``): ``<root>/current`` names
the version to search, ``<root>/hf_cache`` holds the embedding model. A copy laid out flat
(``mechanism_cache/`` directly under the root) is read too.

A copy is searched only while it is the index HPC3 has. When the session can reach HPC3, the files
of the copy are compared with that index (``corpus_scan.corpus_fingerprint``: their sizes and
modification times, which rsync keeps, and the manifest's checksum), at most once every few minutes;
a copy that differs is stale and the search runs on HPC3 instead.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any

from . import dataset_storage

CORPUS_DEFAULT_EMBEDDING = "st-NeuML/pubmedbert-base-embeddings"







_LOG = logging.getLogger(__name__)
# Where the copy lives under the data root: reference data, beside users/, analysis_results/ and
# corpus_scans/ (issue 28's layout).
DEFAULT_DIR = ("reference", "retigene")
# How long the HPC3 index's fingerprint is trusted before it is read again.
FINGERPRINT_TTL_S = 300
_FINGERPRINTS: dict[str, tuple[float, "dict[str, str] | None"]] = {}
_MANIFEST_SHA: dict[tuple[str, int, int], str] = {}


def local_root() -> Path:
    return Path(os.environ.get("AISCIENTIST_LOCAL_CORPUS_ROOT")
                or dataset_storage.data_root().joinpath(*DEFAULT_DIR))


def index_dir(root: Path | None = None) -> Path:
    """The version a search reads now: ``<root>/current`` resolved once, so a sync that switches
    it during the search cannot mix two versions; ``<root>`` itself for a flat copy."""
    root = root if root is not None else local_root()
    link = root / "current"
    return link.resolve() if link.exists() else root


def has_local_index() -> bool:
    cache = index_dir() / "mechanism_cache"
    return all((cache / name).is_file() for name in ("chunks.jsonl", "chunk_vecs.npy"))


def vectors_match_chunks(cache: Path) -> bool:
    """Whether ``chunk_vecs.npy`` has one row per line of ``chunks.jsonl``. When it does not (a copy
    caught between two index builds), ``search_corpus`` would embed the whole corpus again, which on
    the gateway's CPU takes hours inside one request: such a copy is not used."""
    import ast
    import math
    import re
    import struct

    # Inspect the bounded NPY metadata without importing an embedding/numeric stack.
    # The native server reads the matrix; this is only the transfer completeness check.
    path = cache / "chunk_vecs.npy"
    with path.open("rb") as fh:
        if fh.read(6) != b"\x93NUMPY":
            raise ValueError("The corpus vector file is not an NPY array")
        version = fh.read(2)
        width = 2 if version == b"\x01\x00" else 4 if version in (b"\x02\x00", b"\x03\x00") else 0
        if not width:
            raise ValueError("Unsupported corpus vector metadata version")
        raw = fh.read(width)
        if len(raw) != width:
            raise ValueError("Truncated corpus vector metadata")
        size = struct.unpack("<H" if width == 2 else "<I", raw)[0]
        if size > 65536:
            raise ValueError("Corpus vector metadata exceeds its limit")
        header = fh.read(size)
        if len(header) != size:
            raise ValueError("Truncated corpus vector metadata")
        metadata = ast.literal_eval(header.decode("utf-8" if version == b"\x03\x00" else "latin1"))
        shape = metadata.get("shape") if isinstance(metadata, dict) else None
        dtype = re.fullmatch(r"[<>=|][biufc](\d+)", str(metadata.get("descr", ""))) if isinstance(metadata, dict) else None
        if (not isinstance(shape, tuple) or len(shape) != 2
                or any(type(n) is not int or n < 0 for n in shape)
                or not dtype or not 0 < int(dtype[1]) <= 32
                or type(metadata.get("fortran_order")) is not bool):
            raise ValueError("Invalid corpus vector matrix metadata")
        if path.stat().st_size < fh.tell() + math.prod(shape) * int(dtype[1]):
            raise ValueError("Truncated corpus vector matrix")
        rows = shape[0]
    with (cache / "chunks.jsonl").open("rb") as fh:
        lines = sum(1 for line in fh if line.strip())
    return rows == lines


def _manifest_sha256(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _MANIFEST_SHA:
        _MANIFEST_SHA.clear()
        _MANIFEST_SHA[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _MANIFEST_SHA[key]


def differences(fingerprint: "dict[str, str]", index: Path | None = None) -> list[str]:
    """The files of the copy that are not the HPC3 index ``fingerprint`` describes (the output of
    ``corpus_scan.corpus_fingerprint``); empty when the copy is that index. The HGNC table counts
    only when the copy has one: the search runs without it."""
    from .corpus_scan import _CORPUS_FILES

    index = index if index is not None else index_dir()
    out = []
    for name in _CORPUS_FILES:
        want = fingerprint.get(name)
        if want is None:
            continue
        local = index / name
        if name == "hgnc_complete_set.txt" and not local.is_file():
            continue
        try:
            st = local.stat()
        except OSError:
            out.append(name)
            continue
        if f"{st.st_size} {int(st.st_mtime)}" != want:
            out.append(name)
    want_sha = fingerprint.get("manifest_sha256")
    manifest = index / "manifest.csv"
    if want_sha and manifest.is_file() and _manifest_sha256(manifest) != want_sha:
        out.append("manifest.csv")
    return out


def hpc3_fingerprint(executor: Any) -> "dict[str, str] | None":
    """The HPC3 index's fingerprint, read through ``executor`` (one ``stat`` on the login node) at
    most once every ``FINGERPRINT_TTL_S``. None without a session on HPC3, a configured corpus or an
    answer: the copy is then searched as it is."""
    papers = os.environ.get("AISCIENTIST_PAPERQA_PAPERS", "")
    if executor is None or not papers:
        return None
    ttl = float(os.environ.get("AISCIENTIST_LOCAL_CORPUS_CHECK_S", FINGERPRINT_TTL_S) or FINGERPRINT_TTL_S)
    now = time.monotonic()
    hit = _FINGERPRINTS.get(papers)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    from .corpus_scan import corpus_fingerprint
    fp = corpus_fingerprint(executor, papers, os.environ.get("AISCIENTIST_PAPERQA_MANIFEST", ""))
    _FINGERPRINTS[papers] = (now, fp)
    return fp


def stale(executor: Any) -> list[str]:
    """The files that make the copy stale for a session on ``executor``; empty when the copy may be
    searched (it is the HPC3 index, or HPC3 cannot say)."""
    fp = hpc3_fingerprint(executor)
    if not fp:
        return []
    diff = differences(fp)
    if diff:
        _LOG.warning("The RetiGene index copy at %s is not the HPC3 index (%s differ); searching on "
                     "HPC3. Update it with scripts/update_corpus_index.py.", index_dir(), ", ".join(diff))
    return diff














def make_local_tool() -> Any:
    """Use the opt-in external MCP image; no embedding model loads in the gateway."""
    from . import local_literature_mcp
    return local_literature_mcp.make_tool()
