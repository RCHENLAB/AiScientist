"""The ``search_corpus`` tool. Documentation: ``TOOL.md`` in this folder.

Retrieval only, over the same corpus ``deep_literature`` answers from: embed the queries with
PubMedBERT on CPU, take the nearest chunks of ``mechanism_cache/`` by one matrix product, return
the verbatim passages with their PMID, page and citation. No language model runs here, so the job
needs no GPU and holds no API key; the session's model writes the queries and the answer.

The embedder is injectable (``embed_fn``) so the tests run without sentence-transformers or HPC3.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .._lib.corpus import (
    chunk_vectors, corpus_paths, gene_symbols, l2, load_chunks, manifest_citations, read_manifest,
    sentence_transformer_embed)
from ..sdk import HarnessTool

_TOOL = "search_corpus"
MAX_QUERIES = 6
DEFAULT_TOP_K = 12
MAX_TOP_K = 30
DEFAULT_MAX_PER_PAPER = 3
# Added to the cosine score of a chunk that contains a gene symbol written in the query as a word.
# A semantic retriever is weak on bare identifiers (RPGR, PRPF31); the boost lifts a chunk that names
# the gene over one that is merely about the same disease.
GENE_BOOST = 0.05
GENE_BOOST_MAX = 0.10


def _schema() -> dict[str, Any]:
    return {"type": "object", "properties": {
        "queries": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_QUERIES,
                    "description": "1-6 focused questions or statements, each a full sentence; one call "
                                   "serves all of them"},
        "top_k": {"type": "integer", "default": DEFAULT_TOP_K,
                  "description": f"passages returned in total, at most {MAX_TOP_K}"},
        "max_per_paper": {"type": "integer", "default": DEFAULT_MAX_PER_PAPER,
                          "description": "at most this many passages from one paper"},
        "pmids": {"type": "array", "items": {"type": "string"},
                  "description": "restrict the search to these PMIDs (empty = the whole corpus)"},
    }, "required": ["queries"]}


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _failed(error: str, **extra: Any) -> dict[str, Any]:
    return {"status": "failed", "error": error, **extra}


def rank_passages(queries: list[str], chunks: list[dict[str, Any]], vecs: Any, q_vecs: Any, *,
                  top_k: int, max_per_paper: int, allowed: set[str] | None = None) -> list[dict[str, Any]]:
    """The passages to return, as ``[{"idx", "score", "query"}]``.

    Each query ranks every chunk by cosine similarity (plus the gene-symbol boost). The result takes
    the best remaining chunk of each query in turn, so one broad query cannot crowd out the others,
    skipping a chunk already taken and a paper that has its ``max_per_paper``."""
    import numpy as np  # noqa: PLC0415

    sims = np.asarray(q_vecs, dtype="float32") @ np.asarray(vecs, dtype="float32").T
    lowered: list[str] | None = None
    ranked: list[list[tuple[float, int]]] = []
    for qi, query in enumerate(queries):
        scores = sims[qi].copy()
        symbols = gene_symbols(query)
        if symbols:
            if lowered is None:
                lowered = [c["text"] for c in chunks]
            for sym in symbols:
                rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(sym) + r"(?![A-Za-z0-9])")
                for i, text in enumerate(lowered):
                    if rx.search(text):
                        scores[i] += GENE_BOOST
            scores = np.minimum(scores, sims[qi] + GENE_BOOST_MAX)
        if allowed is not None:
            scores = scores.copy()
            for i, c in enumerate(chunks):
                if str(c.get("pmid")) not in allowed:
                    scores[i] = -np.inf
        depth = min(len(scores), top_k * max_per_paper + top_k)
        order = np.argsort(-scores)[:depth]
        ranked.append([(float(scores[i]), int(i)) for i in order if np.isfinite(scores[i])])

    taken: set[int] = set()
    per_paper: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    cursors = [0] * len(queries)
    progressed = True
    while len(out) < top_k and progressed:
        progressed = False
        for qi in range(len(queries)):
            while cursors[qi] < len(ranked[qi]):
                score, i = ranked[qi][cursors[qi]]
                cursors[qi] += 1
                pmid = str(chunks[i].get("pmid"))
                if i in taken or per_paper.get(pmid, 0) >= max_per_paper:
                    continue
                taken.add(i)
                per_paper[pmid] = per_paper.get(pmid, 0) + 1
                out.append({"idx": i, "score": score, "query": queries[qi]})
                progressed = True
                break
            if len(out) >= top_k:
                break
    out.sort(key=lambda r: -r["score"])
    return out


def run_search(args: dict[str, Any], ctx: Any, *,
               embed_fn: Callable[[list[str]], Any] | None = None) -> dict[str, Any]:
    raw = args.get("queries")
    if isinstance(raw, str):
        raw = [raw]
    queries = [q.strip() for q in (raw or []) if isinstance(q, str) and q.strip()][:MAX_QUERIES]
    if not queries:
        return {"status": "error", "error": "search_corpus needs 'queries': 1-6 questions or statements"}
    top_k = _int(args.get("top_k"), DEFAULT_TOP_K, 1, MAX_TOP_K)
    per_paper = _int(args.get("max_per_paper"), DEFAULT_MAX_PER_PAPER, 1, MAX_TOP_K)
    allowed = {str(p).strip() for p in (args.get("pmids") or []) if str(p).strip()} or None

    paths = corpus_paths(ctx)
    chunk_path = paths.cache / "chunks.jsonl" if paths else None
    if paths is None or not chunk_path.is_file():
        return {"status": "dependency_missing", "dependency": "corpus-cache",
                "error": "the corpus text cache (mechanism_cache/chunks.jsonl) is not available here"}
    chunks = load_chunks(chunk_path)
    embed = embed_fn if embed_fn is not None else sentence_transformer_embed(paths.hf_cache)
    if embed is None:
        return {"status": "dependency_missing", "dependency": "sentence-transformers",
                "error": "the PubMedBERT embedding model could not be loaded"}
    logs: list[str] = []
    vecs = chunk_vectors(chunks, paths.cache, embed, False, logs.append)
    if not len(chunks):
        return _failed("the corpus cache holds no chunks", queries=queries)

    picked = rank_passages(queries, chunks, vecs, l2(embed(queries)), top_k=top_k, max_per_paper=per_paper,
                           allowed=allowed)
    cites = manifest_citations(read_manifest(paths.manifest))
    contexts = []
    for r in picked:
        c = chunks[r["idx"]]
        pmid = str(c.get("pmid"))
        contexts.append({
            "citation": cites.get(pmid) or f"PMID {pmid}", "pmid": pmid, "page": c.get("page"),
            "chunk": c.get("chunk"), "text": c.get("text", ""), "score": round(r["score"], 4),
            "query": r["query"]})
    if not contexts:
        return _failed(
            "the indexed corpus returned 0 passages for these queries, so there is nothing to answer "
            "from. Either the PMIDs given are not in the corpus, or it does not cover the topic: "
            "use `literature_search` (live Europe PMC) and say in the write-up that the corpus "
            "search returned nothing.", queries=queries, n_papers=0, contexts=[])
    return {"status": "ok", "queries": queries, "n_papers": len({c["pmid"] for c in contexts}),
            "n_contexts": len(contexts), "contexts": contexts, "raw_data_to_llm": False}


def make_tool() -> HarnessTool:
    """The ``search_corpus`` record for the catalog (see ``TOOL.md``)."""
    return HarnessTool(
        _TOOL,
        "Search the lab's curated literature corpus and get back the matching PASSAGES, verbatim, each "
        "with its PMID, page and citation. It does not write an answer: you read the passages and "
        "write it yourself. Give 1-6 focused questions or statements as full sentences (a bare list "
        "of gene symbols retrieves poorly), e.g. 'Does loss of one copy of PRPF31 cause autosomal "
        "dominant retinitis pigmentosa?'. Cite ONLY a PMID that appears in the returned passages and "
        "quote only sentences that appear in them; anything else is removed from your answer. "
        "Returns status failed when the corpus has nothing, then use literature_search.",
        _schema(), run_search,
        reads_private_data=False, category="literature", requires=("sentence_transformers",),
    )


__all__ = ["make_tool", "run_search", "rank_passages"]
