"""Offline CPU retrieval from an Eye Server copy of the corpus index."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import dataset_storage
from ..tools.api import (CorpusPaths, CORPUS_DEFAULT_EMBEDDING,
                         make_search_corpus_tool, run_search_corpus)

_LOG = logging.getLogger(__name__)
_LOCK = threading.Lock()
_EMBEDDERS: dict[tuple[str, str], Any] = {}
_LOAD_FAILURES: dict[tuple[str, str], float] = {}
_LOAD_RETRY_S = 180


def local_root() -> Path:
    return Path(os.environ.get("AISCIENTIST_LOCAL_CORPUS_ROOT") or dataset_storage.data_root() / "literature")


def has_local_index() -> bool:
    cache = local_root() / "mechanism_cache"
    return all((cache / name).is_file() for name in ("chunks.jsonl", "chunk_vecs.npy"))


def _vectors_match_chunks(cache: Path) -> bool:
    """Whether ``chunk_vecs.npy`` has one row per line of ``chunks.jsonl``. When it does not (a copy
    caught between two index builds), ``search_corpus`` would embed the whole corpus again, which on
    the gateway's CPU takes hours inside one request: such a copy is not used."""
    import numpy as np

    rows = np.load(cache / "chunk_vecs.npy", mmap_mode="r").shape[0]
    with (cache / "chunks.jsonl").open("rb") as fh:
        lines = sum(1 for line in fh if line.strip())
    return rows == lines


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
    try:
        if not _vectors_match_chunks(root / "mechanism_cache"):
            _LOG.warning("Local corpus copy has vectors for a different index; using HPC3 fallback")
            return None
        embed = _embedder(root)
        manifest = root / "manifest.csv"
        hgnc = root / "hgnc_complete_set.txt"
        paths = CorpusPaths(root, manifest if manifest.is_file() else None,
                            root / "mechanism_cache", False,
                            hgnc if hgnc.is_file() else None, root / "hf_cache")
        return run_search_corpus(args, ctx, embed_fn=embed, paths=paths)
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
