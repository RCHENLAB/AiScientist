"""Offline retrieval comparisons on a hand-graded corpus; no embedding model is loaded."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from aiscientist.tools._lib import corpus as C  # noqa: E402
from aiscientist.tools.search_corpus import tool as sc  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "eval_corpus_retrieval", Path(__file__).resolve().parents[1] / "scripts" / "eval_corpus_retrieval.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


@pytest.fixture
def tiny():
    chunks = [{"pmid": pmid, "chunk": chunk, "text": text} for pmid, chunk, text in [
        ("a", 0, "RPGR marker"), ("a", 1, "RPGR marker marker"),
        ("b", 0, "rescue pathway"), ("c", 0, "rescue rescue pathway"), ("d", 0, "unrelated")]]
    vecs = C.l2([[0, 1], [0.1, 1], [1, 0], [0.8, 0.6], [0.6, 0.8]])
    return chunks, vecs


def embed(queries):
    return np.asarray([[1, 0]] * len(queries), dtype="float32")


def test_default_hybrid_is_unchanged(tiny):
    chunks, vecs = tiny
    queries = ["RPGR rescue", "pathway"]
    kwargs = {"top_k": 5, "max_per_paper": 2}
    assert sc.rank_passages(queries, chunks, vecs, embed(queries), **kwargs) == sc.rank_passages(
        queries, chunks, vecs, embed(queries), method="hybrid", **kwargs)
    index = C.TextIndex.of_chunks(chunks)
    terms = index.check(C.query_terms(queries[0]))
    assert C.hybrid_rank(embed(queries)[0], vecs, index, terms) == C.hybrid_rank(
        embed(queries)[0], vecs, index, terms, method="hybrid")
    assert "method" not in sc._schema()["properties"]


def test_baseline_orderings(tiny):
    chunks, vecs = tiny
    for method, expected in [("dense", [2, 3, 4, 1, 0]), ("bm25", [3, 2])]:
        picked, _ = sc.rank_passages(["rescue"], chunks, vecs, embed([""]),
                                     top_k=5, max_per_paper=2, method=method)
        assert [r["idx"] for r in picked] == expected
    with pytest.raises(ValueError, match="Unknown retrieval"):
        sc.rank_passages(["rescue"], chunks, vecs, embed([""]), top_k=1, max_per_paper=1, method="bad")


@pytest.mark.parametrize("method", ev.METHODS)
def test_shared_dedupe_caps_and_scope(tiny, method):
    chunks, vecs = tiny
    queries = ["RPGR rescue", "RPGR rescue"]
    picked, _ = sc.rank_passages(queries, chunks, vecs, embed(queries), top_k=10,
                                 max_per_paper=1, method=method, allowed={"a", "b", "c"})
    assert len(picked) == 3
    assert len({r["idx"] for r in picked}) == 3
    assert {chunks[r["idx"]]["pmid"] for r in picked} == {"a", "b", "c"}


def questions():
    return [{"id": "gene", "category": "gene-specific", "queries": ["RPGR"], "terms": [],
             "relevant_passages": [{"pmid": "b", "chunk": 0}], "expected_pmids": ["b"]},
            {"id": "list", "category": "exhaustive-list", "queries": ["rescue pathway"], "terms": [],
             "relevant_passages": [{"pmid": "b", "chunk": 0}, {"pmid": "c", "chunk": 0}],
             "expected_pmids": ["b", "c", "d"]}]


def test_hand_computed_metrics_enumeration_and_regressions(tiny):
    chunks, vecs = tiny
    result = ev.evaluate(questions(), chunks, vecs, embed, top_k=1, max_per_paper=1, enumeration_budget=4)
    rows = {(r["question_id"], r["method"]): r for r in result["results"]}
    assert rows["gene", "hybrid"]["passage_recall"] == 0
    assert rows["gene", "hybrid"]["answer_support"] is False
    assert rows["gene", "dense"]["passage_recall"] == 1
    assert rows["gene", "dense"]["supporting_paper_recall"] == 1
    assert rows["gene", "dense"]["answer_support"] is True
    for method in ev.METHODS:
        row = rows["list", method]
        assert row["passage_precision"] == 1
        assert row["passage_recall"] == 0.5
        assert row["supporting_paper_recall"] == pytest.approx(1 / 3)
        assert row["answer_support"] is True
        assert row["queries"] == ["rescue pathway"]
        assert row["ranking_seconds"] >= 0
    assert rows["list", "dense"]["enumeration_coverage"] == 1
    assert rows["list", "bm25"]["enumeration_coverage"] == pytest.approx(2 / 3)
    assert rows["list", "hybrid"]["enumeration_coverage"] == 1
    assert rows["gene", "dense"]["enumeration_coverage"] is None
    assert rows["gene", "bm25"]["query_embedding_seconds"] == 0
    regression, = result["gene_query_regressions"]
    assert regression["question_id"] == "gene" and regression["baseline"] == "dense"
    assert regression["metrics"]["passage_recall"] == {"hybrid": 0, "dense": 1}
    assert result["by_category"]["gene-specific"]["dense"]["metrics"]["passage_recall"]["mean"] == 1
    assert result["summary"]["dense"]["metrics"]["passage_recall"] == {"mean": 0.75, "n": 2}


def test_unlabeled_and_supporting_passage_metrics():
    assert all(v is None for v in ev.metrics([], {}).values())
    assert ev.metrics([], {"relevant_passages": [{"pmid": "a", "chunk": 0}]})["passage_precision"] == 0
    assert ev.metrics([{"pmid": "a", "chunk": 0}], {
        "supporting_passages": [{"pmid": "a", "chunk": 0}]})["answer_support"] is True
    assert ev.metrics([{"pmid": "a", "chunk": 1}], {
        "supporting_passages": [{"pmid": "a", "chunk": 0}]})["answer_support"] is False


def test_fingerprint_and_main_end_to_end(tiny, tmp_path, monkeypatch):
    chunks, vecs = tiny
    chunk_path = tmp_path / "chunks.jsonl"
    vector_path = tmp_path / "vectors.npy"
    chunk_path.write_text("".join(json.dumps(c) + "\n" for c in chunks), encoding="utf-8")
    np.save(vector_path, vecs)
    original = ev.fingerprint(chunk_path, vector_path, len(chunks), "fake")
    assert original == ev.fingerprint(chunk_path, vector_path, len(chunks), "fake")
    chunk_path.write_text(chunk_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert original != ev.fingerprint(chunk_path, vector_path, len(chunks), "fake")
    chunk_path.write_text("".join(json.dumps(c) + "\n" for c in chunks), encoding="utf-8")
    question_path = tmp_path / "questions.json"
    question_path.write_text(json.dumps({"questions": questions()}), encoding="utf-8")
    genes = tmp_path / "genes.tsv"
    genes.write_text("symbol\talias_symbol\tprev_symbol\nRPGR\t\t\n", encoding="utf-8")
    monkeypatch.setattr(ev, "sentence_transformer_embed", lambda *_: pytest.fail("Must use injected embedder"))
    assert ev.main(["--questions", str(question_path), "--chunks", str(chunk_path),
                    "--vectors", str(vector_path), "--genes", str(genes), "--embedding-model", "fake",
                    "--top-k", "1", "--max-per-paper", "1", "--enumeration-budget", "4",
                    "--output-dir", str(tmp_path / "out")], embed_fn=embed) == 0
    result = json.loads((tmp_path / "out" / "results.json").read_text(encoding="utf-8"))
    assert result["provenance"]["corpus"] == original
    assert result["provenance"]["question_set_sha256"] == ev.sha256(question_path)
    assert result["provenance"]["genes_sha256"] == ev.sha256(genes)
    assert result["provenance"]["default_method"] == "hybrid"
    assert result["provenance"]["git_revision"]
    assert len(result["results"]) == 6
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "Gene-query regressions" in report and "Enumeration coverage" in report


def test_template_requires_lab_labels():
    path = Path(__file__).resolve().parents[1] / "examples/retrieval_eval/questions.template.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {q["category"] for q in data["questions"]} == set(ev.CATEGORIES)
    with pytest.raises(ValueError, match="placeholder"):
        ev.validate_questions(data)
