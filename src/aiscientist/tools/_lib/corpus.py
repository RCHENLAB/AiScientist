"""The literature corpus on disk, shared by the tools that read it.

The RetiGene corpus lives on dfs3b next to its PDFs: ``mechanism_cache/chunks.jsonl`` (clean text
chunks), ``mechanism_cache/chunk_vecs.npy`` (their L2-normalised PubMedBERT vectors), the manifest
CSV and the offline model cache. ``extract_haploinsufficient_genes`` and ``search_corpus`` both read
them; this module is the one place that knows the layout. Standard library at import; numpy and
sentence-transformers are imported when a function needs them (they exist inside ``paperqa.sif``).
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

CACHE_DIRNAME = "mechanism_cache"
DEFAULT_EMBEDDING = os.environ.get("AISCIENTIST_PAPERQA_EMBEDDING", "st-NeuML/pubmedbert-base-embeddings")


@dataclass
class CorpusPaths:
    papers: Path
    manifest: Path | None
    cache: Path
    cache_writable: bool
    hgnc: Path | None
    hf_cache: Path | None
    notes: list[str] = field(default_factory=list)


def read_manifest(path: Path | None) -> list[dict[str, str]]:
    """The corpus manifest rows (``file_location``, ``title``, ``doc_id`` = PMID, ``citation``, ...)."""
    if not path or not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def manifest_citations(rows: list[dict[str, str]]) -> dict[str, str]:
    """PMID -> citation string, from manifest rows. A row without a ``citation`` column falls back to
    its title, so a passage is never returned without something to cite."""
    out: dict[str, str] = {}
    for row in rows:
        pmid = str(row.get("doc_id") or "").strip()
        if not pmid and row.get("docname"):
            pmid = str(row["docname"]).removeprefix("PMID").strip()
        if not pmid and row.get("file_location"):
            pmid = Path(str(row["file_location"])).stem
        if pmid:
            out[pmid] = (row.get("citation") or row.get("title") or "").strip()
    return out


def load_chunks(chunk_path: Path) -> list[dict[str, Any]]:
    with chunk_path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def l2(v: Any) -> Any:
    import numpy as np  # noqa: PLC0415
    v = np.asarray(v, dtype="float32")
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return v / n


def chunk_vectors(chunks: list[dict[str, Any]], cache_dir: Path, embed_fn: Callable[[list[str]], Any],
                  writable: bool, log: Callable[[str], None]) -> Any:
    """PubMedBERT vectors for every chunk (cached as ``chunk_vecs.npy`` beside the text cache)."""
    import numpy as np  # noqa: PLC0415
    path = cache_dir / "chunk_vecs.npy"
    if path.exists():
        vecs = np.load(path, mmap_mode="r")
        if vecs.shape[0] == len(chunks):
            return vecs
        log(f"chunk_vecs.npy has {vecs.shape[0]} rows for {len(chunks)} chunks; re-embedding")
    t0 = time.time()
    parts = []
    for i in range(0, len(chunks), 256):
        parts.append(l2(embed_fn([c["text"] for c in chunks[i:i + 256]])))
        if (i // 256) % 20 == 0:
            log(f"embedded {min(i + 256, len(chunks))}/{len(chunks)} chunks")
    vecs = np.concatenate(parts) if parts else np.zeros((0, 1), dtype="float32")
    log(f"embedding done in {time.time() - t0:.0f}s")
    if writable:
        try:
            np.save(path, vecs)
        except OSError as exc:
            log(f"could not cache vectors: {exc}")
    return vecs


def corpus_paths(ctx: Any, *, work_subdir: str = "") -> CorpusPaths | None:
    """Where the corpus is, from ``AISCIENTIST_PAPERQA_*``. ``work_subdir`` names the folder under the
    run's ``work/`` to build a text cache in when the shared one is missing (a tool that cannot build
    one leaves it empty and gets the shared path back, unreadable)."""
    papers = os.environ.get("AISCIENTIST_PAPERQA_PAPERS", "")
    if not papers or not Path(papers).is_dir():
        return None
    papers_p = Path(papers)
    root = papers_p.parent
    manifest = os.environ.get("AISCIENTIST_PAPERQA_MANIFEST", "")
    manifest_p = Path(manifest) if manifest and Path(manifest).is_file() else None
    notes: list[str] = []
    shared = Path(os.environ.get("AISCIENTIST_MECHANISM_CACHE", "") or root / CACHE_DIRNAME)
    ws = getattr(ctx, "workspace", None)
    if (shared / "chunks.jsonl").is_file():
        cache, writable = shared, os.access(shared, os.W_OK)
    elif ws and work_subdir:
        cache, writable = Path(ws) / "work" / work_subdir / CACHE_DIRNAME, True
        notes.append(f"No corpus cache at {shared}; extracting the PDFs into the run's work/ "
                     "(build it once with scripts/build_mechanism_cache.py to make this fast).")
    else:
        cache, writable = shared, os.access(root, os.W_OK)
    hgnc = os.environ.get("AISCIENTIST_HGNC_TABLE", "") or str(root / "hgnc_complete_set.txt")
    hgnc_p = Path(hgnc) if Path(hgnc).is_file() else None
    if hgnc_p is None:
        notes.append("No HGNC table (hgnc_complete_set.txt next to the papers, or AISCIENTIST_HGNC_TABLE): "
                     "gene symbols are reported as written and gene_status is 'unchecked'.")
    hf = root / "hf_cache"
    return CorpusPaths(papers_p, manifest_p, cache, writable, hgnc_p, hf if hf.is_dir() else None, notes)


def sentence_transformer_embed(hf_cache: Path | None) -> Callable[[list[str]], Any] | None:
    """The corpus embedding model (PubMedBERT, offline from the corpus' hf_cache), or None."""
    try:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
    except ImportError:
        return None
    name = DEFAULT_EMBEDDING.split("st-", 1)[-1]
    if hf_cache:
        os.environ.setdefault("HF_HOME", str(hf_cache))
        os.environ.setdefault("HF_HUB_CACHE", str(hf_cache / "hub"))
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        model = SentenceTransformer(name)
    except Exception:  # noqa: BLE001 - offline cache missing: no semantic search
        return None
    return lambda texts: model.encode(texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True)


# A bare gene symbol: 2-8 characters, capitals and digits, at least one capital, optionally a
# hyphen part (HLA-DRB1). Used to boost a chunk that contains the symbol as a word.
_GENE_RX = re.compile(r"\b[A-Z][A-Z0-9]{1,7}(?:-[A-Z0-9]{1,4})?\b")
_NOT_GENES = frozenset({"DNA", "RNA", "THE", "AND", "FOR", "WITH", "WHAT", "WHICH", "ARE", "WAS", "NOT",
                        "PCR", "MRNA", "SNP", "CNV", "OR", "IN", "OF", "TO", "IS", "AS", "AT", "BY"})


def gene_symbols(text: str) -> list[str]:
    """Tokens of ``text`` that look like gene symbols (RPGR, PRPF31), in order, without repeats."""
    seen: dict[str, None] = {}
    for tok in _GENE_RX.findall(text):
        if tok not in _NOT_GENES and any(c.isalpha() for c in tok) and tok not in seen:
            seen[tok] = None
    return list(seen)
