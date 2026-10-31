"""Compare experimental retrieval methods against the unchanged hybrid default, offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiscientist.tools._lib.corpus import (  # noqa: E402
    DEFAULT_EMBEDDING, TextIndex, corpus_paths, known_gene_symbols, l2, load_chunks, parse_terms,
    sentence_transformer_embed, term_report,
)
from aiscientist.tools.search_corpus.tool import RANK_DEPTH, rank_passages  # noqa: E402

METHODS = ("hybrid", "dense", "bm25")
CATEGORIES = ("gene-specific", "mechanism", "paraphrase", "exhaustive-list")
METRICS = ("passage_precision", "passage_recall", "supporting_paper_recall", "answer_support",
           "query_embedding_seconds", "ranking_seconds", "enumeration_coverage")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(chunks_path: Path, vectors_path: Path, count: int, model: str) -> dict[str, Any]:
    return {"chunks_sha256": sha256(chunks_path), "vectors_sha256": sha256(vectors_path),
            "chunk_count": count, "embedding_model": model}


def passage_key(passage: dict[str, Any]) -> tuple[str, str]:
    return str(passage["pmid"]), str(passage["chunk"])


def metrics(retrieved: list[dict[str, Any]], question: dict[str, Any]) -> dict[str, Any]:
    """Missing or empty reference sets are unscored, rather than counted as failures."""
    got = {passage_key(p) for p in retrieved}
    relevant = {passage_key(p) for p in question.get("relevant_passages", [])}
    papers = {str(p) for p in question.get("expected_pmids", [])}
    supporting = {passage_key(p) for p in question.get("supporting_passages",
                                                         question.get("relevant_passages", []))}
    got_papers = {p[0] for p in got}
    return {
        "passage_precision": len(got & relevant) / len(got) if relevant and got else (0.0 if relevant else None),
        "passage_recall": len(got & relevant) / len(relevant) if relevant else None,
        "supporting_paper_recall": len(got_papers & papers) / len(papers) if papers else None,
        "answer_support": bool(got & supporting or got_papers & papers) if supporting or papers else None,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for method in METHODS:
        selected = [r for r in rows if r["method"] == method]
        values = {}
        for key in METRICS:
            observed = [r[key] for r in selected if r.get(key) is not None]
            values[key] = {"mean": sum(observed) / len(observed) if observed else None, "n": len(observed)}
        out[method] = {"questions": len(selected), "metrics": values}
    return out


def gene_regressions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for hybrid in rows:
        if hybrid["category"] != "gene-specific" or hybrid["method"] != "hybrid":
            continue
        for baseline in rows:
            if baseline["question_id"] != hybrid["question_id"] or baseline["method"] == "hybrid":
                continue
            worse = {key: {"hybrid": hybrid[key], baseline["method"]: baseline[key]}
                     for key in METRICS[:4] if hybrid.get(key) is not None and baseline.get(key) is not None
                     and hybrid[key] < baseline[key]}
            if worse:
                out.append({"question_id": hybrid["question_id"], "baseline": baseline["method"], "metrics": worse})
    return out


def validate_questions(data: dict[str, Any]) -> list[dict[str, Any]]:
    questions = data["questions"]
    if not questions or data.get("placeholder"):
        raise ValueError("Replace the placeholder question set with lab labels before evaluation")
    seen = set()
    for q in questions:
        if q.get("placeholder") or q["id"] in seen or q["category"] not in CATEGORIES:
            raise ValueError("Question IDs must be unique, categories valid, and placeholders replaced")
        seen.add(q["id"])
        if not q["queries"] or any(not isinstance(s, str) or not s.strip() for s in q["queries"]):
            raise ValueError("Each question needs nonempty query strings")
        if not isinstance(q.get("terms", []), list):
            raise ValueError("terms must be a list of strings")
        for key in ("relevant_passages", "supporting_passages"):
            for passage in q.get(key, []):
                passage_key(passage)
    return questions


def evaluate(questions: list[dict[str, Any]], chunks: list[dict[str, Any]], vecs: Any,
             embed_fn: Callable[[list[str]], Any], *, known_genes: frozenset[str] | None = None,
             top_k: int = 12, max_per_paper: int = 3, enumeration_budget: int = 200,
             rank_depth: int = RANK_DEPTH) -> dict[str, Any]:
    """Embed each question once and reuse its vectors for dense and hybrid. BM25 needs no embedding."""
    import numpy as np  # noqa: PLC0415

    if min(top_k, max_per_paper, rank_depth) < 1 or enumeration_budget <= top_k:
        raise ValueError("Budgets must be positive and enumeration_budget must exceed top_k")
    if vecs.ndim != 2 or vecs.shape[0] != len(chunks):
        raise ValueError("Vector rows must match corpus chunks")
    keys = [passage_key(c) for c in chunks]
    if len(set(keys)) != len(keys):
        raise ValueError("Corpus (pmid, chunk) identifiers must be unique")
    index = TextIndex.of_chunks(chunks)
    rows = []
    for q in questions:
        started = perf_counter()
        q_vecs = l2(embed_fn(q["queries"]))
        embedding_seconds = perf_counter() - started
        if q_vecs.shape != (len(q["queries"]), vecs.shape[1]) or not np.isfinite(q_vecs).all():
            raise ValueError("Query embedding dimensions must match corpus vectors and be finite")
        for method in METHODS:
            def retrieve(budget: int, depth: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
                started = perf_counter()
                picked, used = rank_passages(
                    q["queries"], chunks, vecs, q_vecs, top_k=budget, max_per_paper=max_per_paper,
                    terms=parse_terms(q.get("terms", []), known=known_genes), index=index,
                    known_genes=known_genes, method=method, rank_depth=depth)
                elapsed = perf_counter() - started
                passages = [{"pmid": str(chunks[r["idx"]]["pmid"]), "chunk": chunks[r["idx"]]["chunk"],
                             "corpus_index": r["idx"], "rank": rank, "score": r["score"], "query": r["query"]}
                            for rank, r in enumerate(picked, 1)]
                return passages, term_report(used), elapsed

            passages, terms, elapsed = retrieve(top_k, rank_depth)
            row = {"question_id": q["id"], "category": q["category"], "method": method,
                   "queries": q["queries"], "input_terms": q.get("terms", []), "terms": terms,
                   "retrieved": passages, **metrics(passages, q), "ranking_seconds": elapsed,
                   "query_embedding_seconds": embedding_seconds if method != "bm25" else 0.0,
                   "enumeration_coverage": None}
            if q["category"] == "exhaustive-list":
                enumerated, _, enum_seconds = retrieve(enumeration_budget, max(rank_depth, enumeration_budget))
                row.update(enumeration_retrieved=enumerated, enumeration_ranking_seconds=enum_seconds,
                           enumeration_coverage=metrics(enumerated, q)["supporting_paper_recall"])
            rows.append(row)
    return {"results": rows, "summary": summarize(rows),
            "by_category": {category: summarize([r for r in rows if r["category"] == category])
                            for category in CATEGORIES}, "gene_query_regressions": gene_regressions(rows)}


def markdown_report(result: dict[str, Any]) -> str:
    lines = ["# Corpus retrieval evaluation", "", "Default remains hybrid. No default change is proposed.", "",
             "Answer support is a retrieval hit proxy; no generated answer is graded.", "",
             "Means exclude unscored labels; JSON includes per-metric sample counts and all passages.", ""]
    for title, summary in [("All questions", result["summary"]), *result["by_category"].items()]:
        header = "| Method | Precision | Passage recall | Paper recall | Support | Embed s | Rank s |"
        lines += [f"## {title}", "", header,
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for method, item in summary.items():
            values = [item["metrics"][key]["mean"] for key in METRICS[:-1]]
            cells = " | ".join("unscored" if v is None else f"{v:.4f}" for v in values)
            lines.append(f"| {method} | {cells} |")
        lines.append("")
    lines += ["## Enumeration coverage (separate larger budget)", "", "| Question | Method | Coverage | Rank s |",
              "| --- | --- | --- | --- |"]
    for row in result["results"]:
        if row["category"] == "exhaustive-list":
            value = row["enumeration_coverage"]
            coverage = "unscored" if value is None else f"{value:.4f}"
            lines.append(f"| {row['question_id']} | {row['method']} | {coverage} | "
                         f"{row['enumeration_ranking_seconds']:.4f} |")
    lines += ["", "## Gene-query regressions", "", "```json",
              json.dumps(result["gene_query_regressions"], indent=2), "```", "", "## Provenance", "", "```json",
              json.dumps(result["provenance"], indent=2), "```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, embed_fn: Callable[[list[str]], Any] | None = None) -> int:
    import numpy as np  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    for name in ("chunks", "vectors", "genes", "hf-cache"):
        parser.add_argument(f"--{name}", type=Path)
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING)
    parser.add_argument("--deployment-revision", help="Lab-supplied deployed revision, if different from this checkout")
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--max-per-paper", type=int, default=3)
    parser.add_argument("--enumeration-budget", type=int, default=200)
    parser.add_argument("--rank-depth", type=int, default=RANK_DEPTH)
    args = parser.parse_args(argv)
    paths = corpus_paths(None)
    chunks_path = args.chunks or (paths.cache / "chunks.jsonl" if paths else None)
    vectors_path = args.vectors or (chunks_path.parent / "chunk_vecs.npy" if chunks_path else None)
    if chunks_path is None or vectors_path is None:
        parser.error("Provide --chunks/--vectors or configure AISCIENTIST_PAPERQA_PAPERS")
    questions = validate_questions(json.loads(args.questions.read_text(encoding="utf-8")))
    chunks = load_chunks(chunks_path)
    vecs = np.load(vectors_path, mmap_mode="r")
    genes_path = args.genes or (paths.hgnc if paths else None)
    if genes_path is not None and not genes_path.is_file():
        parser.error("The specified gene table does not exist")
    if embed_fn is None:
        # Never download a model during evaluation, even with explicit corpus paths.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from aiscientist.tools._lib import corpus  # noqa: PLC0415
        previous_model = corpus.DEFAULT_EMBEDDING
        try:
            corpus.DEFAULT_EMBEDDING = args.embedding_model
            embed_fn = sentence_transformer_embed(args.hf_cache or (paths.hf_cache if paths else None))
        finally:
            corpus.DEFAULT_EMBEDDING = previous_model
    if embed_fn is None:
        parser.error("Embedding model unavailable locally; supply --hf-cache with the indexed model")
    result = evaluate(questions, chunks, vecs, embed_fn, known_genes=known_gene_symbols(genes_path),
                      top_k=args.top_k, max_per_paper=args.max_per_paper,
                      enumeration_budget=args.enumeration_budget, rank_depth=args.rank_depth)
    repo = Path(__file__).resolve().parents[1]
    revision = subprocess.run(["git", "-c", f"safe.directory={repo.as_posix()}", "rev-parse", "HEAD"],
                              cwd=repo, capture_output=True, text=True, check=False)
    dirty = subprocess.run(["git", "-c", f"safe.directory={repo.as_posix()}", "status", "--porcelain"],
                           cwd=repo, capture_output=True, text=True, check=False)
    result["provenance"] = {
        "corpus": fingerprint(chunks_path, vectors_path, len(chunks), args.embedding_model),
        "genes_sha256": sha256(genes_path) if genes_path else None,
        "git_revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "git_dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
        "deployment_revision": args.deployment_revision,
        "question_set_sha256": sha256(args.questions), "question_set": questions,
        "timestamp": datetime.now(timezone.utc).isoformat(), "methods": METHODS, "default_method": "hybrid",
        "top_k": args.top_k, "max_per_paper": args.max_per_paper, "enumeration_budget": args.enumeration_budget,
        "rank_depth": args.rank_depth, "enumeration_rank_depth": max(args.rank_depth, args.enumeration_budget),
        "method_details": {"hybrid": "Unchanged gene priority, separate BM25 groups, RRF and dense interleave",
                           "dense": "Cosine only; no gene priority",
                           "bm25": "BM25 over deduplicated query and model terms together; no gene priority"},
        "timing": "One shared query embedding per question; attributed to hybrid/dense, zero for BM25. "
                  "Ranking includes term checks, excludes index/model load. Shared term cache is warm after first use. "
                  "One trial per method, fixed order hybrid/dense/bm25; enumeration timed separately.",
        "default_change": None,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "report.md").write_text(markdown_report(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
