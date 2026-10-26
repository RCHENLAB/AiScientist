"""The shared hybrid retrieval in ``tools/_lib/corpus.py``: exact words next to the embedding.

What is pinned down: words match the way papers write them (PDF ligatures, hyphens, word endings),
gene symbols match case-sensitively and field abbreviations are not genes, each term is counted in
the corpus before it is used, BM25 prefers the rare word, and reciprocal-rank fusion brings up a
passage the embedding alone ranks low."""
from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from aiscientist.tools._lib import corpus as C  # noqa: E402


def _index(texts):
    return C.TextIndex(texts)


def _count(texts, term, kind="word"):
    return C.TextIndex(texts).counts(C.Term(term, kind))


def test_words_match_ligatures_hyphens_and_endings():
    texts = ["BMP4 haploinsufﬁciency is known.",          # the PDF ligature
             "A haplo-insufficiency of PAX6.",                 # hyphenated
             "PRPF31 is haploinsufficient.",                   # adjective form
             "Insufficient data."]
    assert set(_count(texts, "haploinsufficiency")) == {0, 1, 2}
    assert set(_count(["loss-of-function alleles", "loss of function", "loss of vision"], "loss of function")) == {0, 1}
    assert set(_count(["one copy", "two copies", "a copyright notice"], "copy")) == {0, 1}
    assert set(_count(["the OCT scan", "an octamer"], "oct")) == {0}          # short words end at a boundary
    assert C.fold("gene–dosage­") == "gene-dosage"


def test_gene_symbols_are_case_sensitive_whole_words_and_abbreviations_are_not_genes():
    texts = ["RPGR is mutated.", "rpgr in lower case", "RPGRIP1 is another gene.", "xRPGR"]
    assert set(_count(texts, "RPGR", "gene")) == {0}
    known = frozenset({"RPGR", "ERG", "RPE"})
    assert [t.kind for t in C.query_terms("What does RPGR do to the ERG and the RPE?", known)][:1] == ["gene"]
    kinds = {t.text: t.kind for t in C.query_terms("What does RPGR do to the ERG and the RPE?", known)}
    assert kinds["RPGR"] == "gene" and kinds["erg"] == "word" and kinds["rpe"] == "word"
    # without HGNC every gene-shaped token counts; with it only the table's symbols do
    assert {t.text for t in C.query_terms("Is FOO1 linked to RPGR?", None) if t.kind == "gene"} == {"FOO1", "RPGR"}
    assert {t.text for t in C.query_terms("Is FOO1 linked to RPGR?", known) if t.kind == "gene"} == {"RPGR"}


def test_stop_words_are_dropped_from_a_question():
    words = [t.text for t in C.query_terms("Which genes are reported to cause cone dystrophy?")]
    assert words == ["genes", "cause", "cone", "dystrophy"]


def test_each_term_is_counted_and_a_common_one_only_ranks():
    texts = [f"retinal passage {i}" for i in range(200)] + ["haploinsufficiency of PAX6"]
    index = _index(texts)
    terms = index.check(C.parse_terms(["haploinsufficiency", "retinal", "zzzabsent", "PAX6"]))
    assert {t.text: (t.kind, t.n_passages, t.use) for t in terms} == {
        "haploinsufficiency": ("word", 1, "filter"), "retinal": ("word", 200, "rank"),
        "zzzabsent": ("word", 0, "absent"), "PAX6": ("gene", 1, "filter")}
    assert index.matching([terms[0], terms[3]]) == {200}


def test_bm25_prefers_the_rare_word_and_does_not_reward_length():
    texts = ["retinal retinal retinal dystrophy"] * 5 + ["haploinsufficiency causes dystrophy",
                                                         "retinal " * 200 + "haploinsufficiency"]
    index = _index(texts)
    scores = index.bm25(index.check(C.parse_terms(["retinal", "haploinsufficiency"])))
    best = max(scores, key=scores.get)
    assert best == 5                       # the short passage with the rare word beats the long one


def test_fusion_brings_up_a_passage_the_embedding_ranks_low():
    texts = [f"Review {i} of visual function." for i in range(50)] + ["SIX6 is a candidate; BMP4 haploinsufficiency."]
    vecs = np.asarray([[1.0, 0.01 * i] for i in range(50)] + [[0.05, 1.0]], dtype="float32")
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    index = _index(texts)
    terms = index.check(C.query_terms("Which genes cause disease through haploinsufficiency?"))
    ranked = C.hybrid_rank(np.asarray([1.0, 0.0], dtype="float32"), vecs, index, terms, depth=20)
    assert 50 in [d for d, _, _ in ranked[:2]]              # first by words ranks with the first by meaning
    dense_only = C.hybrid_rank(np.asarray([1.0, 0.0], dtype="float32"), vecs, index, [], depth=20)
    assert 50 not in [d for d, _, _ in dense_only]         # beyond the embedding's top 20


def test_a_named_gene_ranks_its_passages_first_and_allowed_limits_everything():
    texts = ["General splicing review.", "RPGR is mutated in X-linked disease.", "Another review."]
    vecs = np.asarray([[1.0, 0.0], [0.5, 0.86], [0.9, 0.43]], dtype="float32")
    index = _index(texts)
    terms = index.check(C.query_terms("What does RPGR do?"))
    q = np.asarray([1.0, 0.0], dtype="float32")
    assert [d for d, _, _ in C.hybrid_rank(q, vecs, index, terms)] == [1, 0, 2]
    assert [d for d, _, _ in C.hybrid_rank(q, vecs, index, terms, allowed={0, 2})] == [0, 2]


def test_nearest_caps_each_paper_and_keeps_corpus_order_on_ties():
    chunks = [{"pmid": "a"}, {"pmid": "a"}, {"pmid": "b"}]
    vecs = np.asarray([[1.0, 0.0], [1.0, 0.0], [0.9, 0.1]], dtype="float32")
    assert C.nearest(np.asarray([1.0, 0.0], dtype="float32"), vecs, chunks, 3, max_per_paper=1) == [0, 2]


def test_the_embeddings_best_passage_is_kept_when_words_favour_others():
    """2026-10-21: words that merely occur in other passages ("findings", "diagnosis") pushed out the
    passages the embedding ranked first; the merged list is interleaved with the embedding's own."""
    texts = ["PEHO syndrome is diagnosed by optic atrophy and an extinguished VEP."] + [
        f"Findings required for the diagnosis of syndrome {i}: findings, diagnosis, required." for i in range(20)]
    vecs = np.asarray([[1.0, 0.0]] + [[0.2, 1.0]] * 20, dtype="float32")
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    index = _index(texts)
    terms = index.check(C.query_terms("Which findings are required for the diagnosis of this syndrome?"))
    ranked = [d for d, _, _ in C.hybrid_rank(np.asarray([1.0, 0.0], dtype="float32"), vecs, index, terms)]
    assert 0 in ranked[:C.DENSE_EVERY + 1]                 # the embedding's first takes the first slot it owns


def test_interleave_takes_the_rankings_in_turn_each_chunk_once():
    assert C.interleave([1, 2, 3], [9, 1, 8]) == [1, 9, 2, 8, 3]
    assert C.interleave([1, 2, 3, 4, 5], [9, 8, 7], ratio=3) == [1, 2, 3, 9, 4, 5, 8, 7]
    assert C.interleave([], [4, 5]) == [4, 5]


def test_a_gene_only_the_model_adds_does_not_put_its_passages_first():
    texts = ["PEHO syndrome: optic atrophy and an extinguished VEP are required.", "ZNHIT3 variants were found."]
    vecs = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype="float32")
    index = _index(texts)
    terms = index.check(C.query_terms("What findings define PEHO syndrome?") + C.parse_terms(["ZNHIT3"]))
    ranked = [d for d, _, _ in C.hybrid_rank(np.asarray([1.0, 0.0], dtype="float32"), vecs, index, terms)]
    assert ranked[0] == 0
