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
import threading
import time
from pathlib import Path
from typing import Any

from . import dataset_storage
from ..tools.api import (CorpusPaths, CORPUS_DEFAULT_EMBEDDING,
                         make_search_corpus_tool, preload_search_corpus, run_search_corpus)

_LOG = logging.getLogger(__name__)
_LOCK = threading.Lock()
_EMBEDDERS: dict[tuple[str, str], Any] = {}
_LOAD_FAILURES: dict[tuple[str, str], float] = {}
_LOAD_RETRY_S = 180
# Where the copy lives under the data root: reference data, beside users/, analysis_results/ and
# corpus_scans/ (issue 28's layout).
DEFAULT_DIR = ("reference", "retigene")
# How long the HPC3 index's fingerprint is trusted before it is read again.
FINGERPRINT_TTL_S = 300
_FINGERPRINTS: dict[str, tuple[float, "dict[str, str] | None"]] = {}
# The copy's text, ready to search (``preload_search_corpus``): one version at a time, kept for the
# life of the process. Building it takes ~7 s of CPU for the 39,563 chunks; a search then takes ~2 s.
_TEXT_LOCK = threading.Lock()
_TEXT: dict[tuple[str, int, int], Any] = {}
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
    import numpy as np

    rows = np.load(cache / "chunk_vecs.npy", mmap_mode="r").shape[0]
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


def _preloaded(paths: Any) -> Any:
    st = (paths.cache / "chunks.jsonl").stat()
    key = (str(paths.cache), st.st_size, st.st_mtime_ns)
    with _TEXT_LOCK:
        if key not in _TEXT:
            _TEXT.clear()            # the previous version's text goes once no search holds it
            _TEXT[key] = preload_search_corpus(paths)
        return _TEXT[key]


def _embedder(root: Path) -> Any:
    name = os.environ.get("AISCIENTIST_PAPERQA_EMBEDDING", CORPUS_DEFAULT_EMBEDDING).removeprefix("st-")
    hf = root / "hf_cache" / "hub"
    key = (name, str(hf))
    with _LOCK:
        if key not in _EMBEDDERS:
            if time.monotonic() < _LOAD_FAILURES.get(key, 0):
                raise RuntimeError("The offline embedding model is temporarily unavailable")
            try:
                from sentence_transformers import SentenceTransformer

                model = SentenceTransformer(name, device="cpu", local_files_only=True,
                                            cache_folder=str(hf) if hf.is_dir() else None)
            except Exception:
                _LOAD_FAILURES[key] = time.monotonic() + _LOAD_RETRY_S
                raise
            _LOAD_FAILURES.pop(key, None)
            _EMBEDDERS[key] = lambda texts: model.encode(
                texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True)
        return _EMBEDDERS[key]


def run_local_search(args: dict[str, Any], ctx: Any = None) -> dict[str, Any] | None:
    """Call in a worker thread. None means use the existing HPC3 fallback.

    Loading never downloads a model or changes process environment. Failed loads are cached for
    three minutes before retrying, allowing model installation without restarting the gateway.
    """
    if not has_local_index():
        return None
    root = local_root()
    index = index_dir(root)
    try:
        if not vectors_match_chunks(index / "mechanism_cache"):
            _LOG.warning("Local corpus copy has vectors for a different index; using HPC3 fallback")
            return None
        embed = _embedder(root)
        manifest = index / "manifest.csv"
        hgnc = index / "hgnc_complete_set.txt"
        paths = CorpusPaths(index, manifest if manifest.is_file() else None,
                            index / "mechanism_cache", False,
                            hgnc if hgnc.is_file() else None, root / "hf_cache")
        return run_search_corpus(args, ctx, embed_fn=embed, paths=paths, preloaded=_preloaded(paths))
    except Exception:
        _LOG.warning("Local corpus retrieval unavailable; using HPC3 fallback", exc_info=True)
        return None


def make_local_tool() -> Any:
    """Return a ready local tool, or None. Model loading belongs in a worker thread."""
    if not has_local_index():
        return None
    try:
        _embedder(local_root())
    except Exception:
        return None
    from dataclasses import replace
    return replace(make_search_corpus_tool(), executor=run_local_search)
