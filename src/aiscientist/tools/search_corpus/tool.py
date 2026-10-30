"""The ``search_corpus`` tool. Documentation: ``TOOL.md`` in this folder.

Retrieval only, over the same corpus ``deep_literature`` answers from: each query ranks the chunks of
``mechanism_cache/`` twice, by PubMedBERT similarity and by BM25 over its exact words (and the terms
the model gives), and the two rankings are merged by reciprocal rank (``_lib.corpus.hybrid_rank``);
the verbatim passages come back with their PMID, page and citation. No language model runs here, so
the job needs no GPU and holds no API key; the session's model writes the queries and the answer.

The embedder is injectable (``embed_fn``) so the tests run without sentence-transformers or HPC3.
"""

from __future__ import annotations

from typing import Any, Callable

from .._lib.corpus import (
    CorpusPaths, TextIndex, Term, chunk_vectors, corpus_paths, hybrid_rank, known_gene_symbols, l2, load_chunks,
    manifest_citations, parse_terms, query_terms, read_manifest, sentence_transformer_embed, term_report)
from ..sdk import HarnessTool

_TOOL = "search_corpus"
MAX_QUERIES = 6
DEFAULT_TOP_K = 12
MAX_TOP_K = 30
DEFAULT_MAX_PER_PAPER = 3
MAX_TERMS = 12
# How deep each of a query's two rankings goes before they are merged.
RANK_DEPTH = 200


def _schema() -> dict[str, Any]:
    return {"type": "object", "properties": {
        "queries": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_QUERIES,
                    "description": "1-6 focused questions or statements, each a full sentence; one call "
                                   "serves all of them"},
        "top_k": {"type": "integer", "default": DEFAULT_TOP_K,
                  "description": f"passages returned in total, at most {MAX_TOP_K}"},
        "max_per_paper": {"type": "integer", "default": DEFAULT_MAX_PER_PAPER,
                          "description": "at most this many passages from one paper"},
        "terms": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_TERMS,
                  "description": "optional: up to 12 exact words or short phrases a passage that answers "
                                 "would contain, with their spelling variants (gene symbols, technical "
                                 "terms, e.g. 'dominant negative', 'ellipsoid zone'). Each is counted in the "
                                 "corpus and ranks the passages that contain it; the queries' own words "
                                 "are used anyway"},
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
                  top_k: int, max_per_paper: int, allowed: set[str] | None = None,
                  terms: list[Term] | None = None, index: TextIndex | None = None,
                  known_genes: frozenset[str] | None = None) -> tuple[list[dict[str, Any]], list[Term]]:
    """The passages to return, as ``[{"idx", "score", "cosine", "query"}]``, and the terms used.

    Each query ranks the chunks with ``hybrid_rank``: by embedding and by BM25 over its own words plus
    ``terms``, merged by reciprocal rank; a query that names a gene the corpus has is answered from
    the chunks that name it. The result takes the best remaining chunk of each query in turn, so one
    broad query cannot crowd out the others, skipping a chunk already taken and a paper that has its
    ``max_per_paper``."""
    import numpy as np  # noqa: PLC0415

    index = index if index is not None else TextIndex.of_chunks(chunks)
    extra = list(terms or [])
    allowed_idx = ({i for i, c in enumerate(chunks) if str(c.get("pmid")) in allowed}
                   if allowed is not None else None)
    q_vecs = np.asarray(q_vecs, dtype="float32")
    ranked: list[list[tuple[int, float, float]]] = []
    used: dict[tuple[str, str], Term] = {}
    for qi, query in enumerate(queries):
        q_terms = query_terms(query, known_genes)
        have = {t.text.lower() for t in q_terms}
        q_terms += [t for t in extra if t.text.lower() not in have]
        index.check(q_terms)
        for t in q_terms:
            used.setdefault((t.kind, t.text.lower()), t)
        ranked.append(hybrid_rank(q_vecs[qi], vecs, index, q_terms, depth=RANK_DEPTH, allowed=allowed_idx))

    taken: set[int] = set()
    per_paper: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    cursors = [0] * len(queries)
    progressed = True
    while len(out) < top_k and progressed:
        progressed = False
        for qi in range(len(queries)):
            while cursors[qi] < len(ranked[qi]):
                i, score, cos = ranked[qi][cursors[qi]]
                cursors[qi] += 1
                pmid = str(chunks[i].get("pmid"))
                if i in taken or per_paper.get(pmid, 0) >= max_per_paper:
                    continue
                taken.add(i)
                per_paper[pmid] = per_paper.get(pmid, 0) + 1
                out.append({"idx": i, "score": score, "cosine": cos, "query": queries[qi]})
                progressed = True
                break
            if len(out) >= top_k:
                break
    out.sort(key=lambda r: -r["score"])
    return out, list(used.values())


def preload(paths: CorpusPaths) -> tuple[list[dict[str, Any]], TextIndex, frozenset[str] | None]:
    """The corpus text a search reads, ready to search: its chunks, their word index and the known
    gene symbols. Building the word index takes seconds for the 39,563 RetiGene chunks, so a process
    that searches many times (the gateway's copy) builds it once and passes it as ``preloaded``."""
    chunks = load_chunks(paths.cache / "chunks.jsonl")
    return chunks, TextIndex.of_chunks(chunks), known_gene_symbols(paths.hgnc)


def run_search(args: dict[str, Any], ctx: Any, *,
               embed_fn: Callable[[list[str]], Any] | None = None,
               paths: CorpusPaths | None = None,
               preloaded: tuple[list[dict[str, Any]], TextIndex, frozenset[str] | None] | None = None,
               ) -> dict[str, Any]:
    raw = args.get("queries")
    if isinstance(raw, str):
        raw = [raw]
    queries = [q.strip() for q in (raw or []) if isinstance(q, str) and q.strip()][:MAX_QUERIES]
    if not queries:
        return {"status": "error", "error": "search_corpus needs 'queries': 1-6 questions or statements"}
    top_k = _int(args.get("top_k"), DEFAULT_TOP_K, 1, MAX_TOP_K)
    per_paper = _int(args.get("max_per_paper"), DEFAULT_MAX_PER_PAPER, 1, MAX_TOP_K)
    allowed = {str(p).strip() for p in (args.get("pmids") or []) if str(p).strip()} or None

    paths = paths if paths is not None else corpus_paths(ctx)
    chunk_path = paths.cache / "chunks.jsonl" if paths else None
    if paths is None or not chunk_path.is_file():
        return {"status": "dependency_missing", "dependency": "corpus-cache",
                "error": "the RetiGene corpus text cache (mechanism_cache/chunks.jsonl) is not available here"}
    chunks, index, known = preloaded if preloaded is not None else (load_chunks(chunk_path), None, None)
    embed = embed_fn if embed_fn is not None else sentence_transformer_embed(paths.hf_cache)
    if embed is None:
        return {"status": "dependency_missing", "dependency": "sentence-transformers",
                "error": "the PubMedBERT embedding model could not be loaded"}
    logs: list[str] = []
    vecs = chunk_vectors(chunks, paths.cache, embed, False, logs.append)
    if not len(chunks):
        return _failed("the RetiGene corpus cache holds no chunks", queries=queries)

    if preloaded is None:
        known = known_gene_symbols(paths.hgnc)
    picked, used = rank_passages(queries, chunks, vecs, l2(embed(queries)), top_k=top_k,
                                 max_per_paper=per_paper, allowed=allowed, index=index,
                                 terms=parse_terms((args.get("terms") or [])[:MAX_TERMS], "model", known),
                                 known_genes=known)
    rows = read_manifest(paths.manifest)
    cites = manifest_citations(rows)
    # Use the same PMID fallbacks as the citation lookup, without another manifest read.
    metadata = {pmid: row for row in rows for pmid in manifest_citations([row])}
    contexts = []
    for r in picked:
        c = chunks[r["idx"]]
        pmid = str(c.get("pmid"))
        row = metadata.get(pmid, {})
        fields = {key: str(row.get(key) or row.get(alias) or "").strip()
                  for key, alias in (("title", "title"), ("authors", "authorString"),
                                     ("year", "pubYear"), ("journal", "journalTitle"), ("doi", "doi"))}
        contexts.append({
            **{key: value for key, value in fields.items() if value},
            "citation": cites.get(pmid) or f"PMID {pmid}", "pmid": pmid, "page": c.get("page"),
            "chunk": c.get("chunk"), "text": c.get("text", ""), "score": round(r["score"], 4),
            "cosine": round(r["cosine"], 4), "query": r["query"]})
    if not contexts:
        return _failed(
            "the RetiGene corpus returned 0 passages for these queries, so there is nothing to answer "
            "from. Either the PMIDs given are not in the RetiGene corpus, or it does not cover the topic: "
            "use `literature_search` (live Europe PMC) and say in the write-up that the search of the "
            "RetiGene papers returned nothing.", queries=queries, n_papers=0, contexts=[], terms=term_report(used))
    return {"status": "ok", "queries": queries, "n_papers": len({c["pmid"] for c in contexts}),
            "n_contexts": len(contexts), "contexts": contexts, "terms": term_report(used),
            "raw_data_to_llm": False}


def make_tool() -> HarnessTool:
    """The ``search_corpus`` record for the catalog (see ``TOOL.md``)."""
    return HarnessTool(
        _TOOL,
        "Search the lab's curated literature corpus and get back the matching PASSAGES, verbatim, each "
        "with its PMID, page and citation. It does not write an answer: you read the passages and "
        "write it yourself. Give 1-6 focused questions or statements as full sentences (a bare list "
        "of gene symbols retrieves poorly), e.g. 'Does loss of one copy of PRPF31 cause autosomal "
        "dominant retinitis pigmentosa?', and optionally `terms`: the exact words a passage that "
        "answers would use (e.g. 'haploinsufficiency', 'one functional copy'); passages are ranked "
        "by meaning and by these words together. Cite ONLY a PMID that appears in the returned passages and "
        "quote only sentences that appear in them; anything else is removed from your answer. "
        "Returns status failed when the corpus has nothing, then use literature_search.",
        _schema(), run_search,
        reads_private_data=False, category="literature", requires=("sentence_transformers",),
    )


__all__ = ["make_tool", "run_search", "rank_passages", "preload"]
