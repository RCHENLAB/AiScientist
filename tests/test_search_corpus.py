"""search_corpus without HPC3: a fake embedder over a three-paper chunk cache. What the tests pin down
is the contract the platform relies on: passages come back verbatim with a citation in the
`contexts` shape deep_literature uses; nothing matching is `failed`, never `ok`; the job path imports
no paperqa and needs no model; and an answer written from the passages is cut back to what they say."""
from __future__ import annotations

import csv
import json
import sys
import types

import pytest

np = pytest.importorskip("numpy")

from aiscientist.agents import corpus_answer as ca  # noqa: E402
from aiscientist.tools import paperqa_cli  # noqa: E402
from aiscientist.tools.search_corpus import tool as sc  # noqa: E402

VOCAB = ["prpf31", "haploinsufficiency", "rhodopsin", "dominant", "stargardt", "recessive", "splicing",
         "photoreceptor", "retinitis", "abca4"]
TEXTS = {
    "11111111": ["Haploinsufficiency of PRPF31 causes retinitis pigmentosa through reduced splicing.",
                 "PRPF31 splicing factor levels decide the penetrance in the family."],
    "22222222": ["Rhodopsin mutations act in a dominant way and harm the photoreceptor."],
    "33333333": ["Stargardt disease from ABCA4 is recessive."],
}


def embed(texts: list[str]):
    out = []
    for t in texts:
        words = t.lower().replace(".", " ").replace("?", " ").split()
        out.append([float(words.count(v)) for v in VOCAB])
    return np.asarray(out, dtype="float32")


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "retigene"
    cache = root / "mechanism_cache"
    cache.mkdir(parents=True)
    (root / "papers").mkdir()
    with (cache / "chunks.jsonl").open("w") as fh:
        for pmid, texts in TEXTS.items():
            for k, text in enumerate(texts):
                fh.write(json.dumps({"pmid": pmid, "chunk": k, "page": k + 1, "text": text}) + "\n")
    with (root / "paperqa_manifest.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file_location", "title", "doc_id", "citation"])
        w.writerow(["11111111.pdf", "PRPF31 haploinsufficiency", "11111111", "Smith J. PRPF31 in adRP. 2001."])
        w.writerow(["22222222.pdf", "Rhodopsin", "22222222", ""])
        w.writerow(["33333333.pdf", "Stargardt", "33333333", "Lee K. ABCA4 and Stargardt. 1999."])
    monkeypatch.setenv("AISCIENTIST_PAPERQA_PAPERS", str(root / "papers"))
    monkeypatch.setenv("AISCIENTIST_PAPERQA_MANIFEST", str(root / "paperqa_manifest.csv"))
    monkeypatch.delenv("AISCIENTIST_MECHANISM_CACHE", raising=False)
    return root


def run(args, **kw):
    return sc.run_search(args, types.SimpleNamespace(workspace=None), embed_fn=embed, **kw)


def test_returns_verbatim_passages_in_the_deep_literature_shape(corpus):
    out = run({"queries": ["Does haploinsufficiency of PRPF31 cause retinitis pigmentosa?"], "top_k": 3})
    assert out["status"] == "ok" and out["n_contexts"] == len(out["contexts"]) == 3
    first = out["contexts"][0]
    assert first["pmid"] == "11111111" and first["text"] == TEXTS["11111111"][0]
    assert first["citation"] == "Smith J. PRPF31 in adRP. 2001."
    assert {"citation", "pmid", "page", "chunk", "text", "score", "query"} <= set(first)
    # no citation column value: the title stands in, and a paper missing from the manifest is cited by PMID
    assert next(c for c in out["contexts"] if c["pmid"] == "22222222")["citation"] == "Rhodopsin"


def test_each_query_gets_its_best_passage_and_a_paper_is_capped(corpus):
    out = run({"queries": ["PRPF31 haploinsufficiency splicing", "dominant rhodopsin photoreceptor"],
               "top_k": 2, "max_per_paper": 1})
    assert [c["pmid"] for c in out["contexts"]].count("11111111") == 1
    assert {c["pmid"] for c in out["contexts"]} == {"11111111", "22222222"}
    capped = run({"queries": ["PRPF31 splicing"], "top_k": 5, "max_per_paper": 1})
    assert len({c["pmid"] for c in capped["contexts"]}) == len(capped["contexts"])


def test_pmids_restrict_the_search_and_an_empty_result_is_failed(corpus):
    only = run({"queries": ["PRPF31 splicing"], "pmids": ["33333333"]})
    assert {c["pmid"] for c in only["contexts"]} == {"33333333"}
    none = run({"queries": ["PRPF31 splicing"], "pmids": ["99999999"]})
    assert none["status"] == "failed" and none["contexts"] == [] and "literature_search" in none["error"]


def test_a_query_that_names_a_gene_is_answered_first_from_the_passages_that_name_it(corpus):
    chunks = [{"pmid": "a", "text": "A general review of retinal splicing."},
              {"pmid": "b", "text": "RPGR is mutated in X-linked disease."}]
    vecs = np.asarray([[1.0, 0.0], [0.6, 0.8]], dtype="float32")
    q = np.asarray([[1.0, 0.0]], dtype="float32")
    order, _ = sc.rank_passages(["What does RPGR do?"], chunks, vecs, q, top_k=2, max_per_paper=2)
    # the review is nearer by meaning, but the passage naming RPGR comes first; the other still follows
    assert [chunks[r["idx"]]["pmid"] for r in order] == ["b", "a"]
    plain, _ = sc.rank_passages(["What does the gene do?"], chunks, vecs, q, top_k=2, max_per_paper=2)
    assert [chunks[r["idx"]]["pmid"] for r in plain] == ["a", "b"]


def test_exact_words_find_a_passage_the_embedding_ranks_low(corpus):
    """The 2026-10-20 failure: the passage that states the fact is about something else, so its
    vector is far from the question; its words are not."""
    chunks = [{"pmid": str(i), "text": f"Review {i} of retinal imaging and visual function."} for i in range(30)]
    chunks.append({"pmid": "x", "text": "SIX6 is a candidate; BMP4 haploinsuf\ufb01ciency is known to cause microphthalmia."})
    vecs = np.asarray([[1.0, 0.02 * i] for i in range(30)] + [[0.1, 1.0]], dtype="float32")
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    q = np.asarray([[1.0, 0.0]], dtype="float32")
    order, used = sc.rank_passages(["Which genes cause microphthalmia through haploinsufficiency?"], chunks, vecs, q,
                                   top_k=3, max_per_paper=1)
    assert chunks[order[0]["idx"]]["pmid"] == "x"            # the ligature spelling matched too
    assert {t.text: t.use for t in used}["haploinsufficiency"] == "filter"


def test_model_terms_are_counted_and_reported(corpus):
    out = run({"queries": ["Which splicing factor genes cause retinitis pigmentosa?"],
               "terms": ["haploinsufficiency", "nonexistentword"], "top_k": 2})
    rep = {t["term"]: t for t in out["terms"]}
    assert rep["haploinsufficiency"]["n_passages"] == 1 and rep["haploinsufficiency"]["source"] == "model"
    assert rep["nonexistentword"]["use"] == "absent"
    assert out["contexts"][0]["pmid"] == "11111111"


def test_missing_cache_and_bad_arguments_say_so(tmp_path, monkeypatch):
    monkeypatch.delenv("AISCIENTIST_PAPERQA_PAPERS", raising=False)
    assert run({"queries": ["x y z w"]})["status"] == "dependency_missing"
    assert sc.run_search({"queries": []}, types.SimpleNamespace(workspace=None))["status"] == "error"


def test_the_job_entry_point_runs_it_without_a_model_or_paperqa(corpus, monkeypatch):
    monkeypatch.setitem(sys.modules, "paperqa", None)          # importing it would raise
    monkeypatch.setattr(sc, "sentence_transformer_embed", lambda _hf: embed)
    out = paperqa_cli.run_tool("search_corpus", str(corpus), {
        "queries": ["PRPF31 haploinsufficiency splicing"], "papers": str(corpus / "papers"),
        "manifest": str(corpus / "paperqa_manifest.csv"), "model": "must-be-ignored"})
    assert out["status"] == "ok" and out["contexts"][0]["pmid"] == "11111111"


# --- the check on an answer written from the passages -------------------------------------------

CONTEXTS = [{"pmid": "11111111", "text": TEXTS["11111111"][0]},
            {"pmid": "22222222", "text": TEXTS["22222222"][0]}]


def test_a_fabricated_pmid_is_removed_and_reported():
    v = ca.verify_answer("PRPF31 haploinsufficiency causes adRP [PMID:11111111]. "
                         "RHO is dominant [PMID:22222222, PMID:55555555].", CONTEXTS)
    assert "55555555" not in v.text and "PMID:22222222" in v.text
    assert v.cited == ["11111111", "22222222"]
    assert any("55555555" in r for r in v.removed)


def test_a_quotation_that_is_in_no_passage_removes_its_sentence():
    good = 'The paper says "Haploinsufficiency of PRPF31 causes retinitis pigmentosa" [PMID:11111111].'
    bad = 'It also says "PRPF31 null alleles are fully penetrant in every family" [PMID:11111111].'
    v = ca.verify_answer(good + " " + bad, CONTEXTS)
    assert "causes retinitis pigmentosa" in v.text and "fully penetrant" not in v.text
    assert len(v.removed) == 1 and "fully penetrant" in v.removed[0]


def test_an_answer_citing_only_fabricated_pmids_is_left_with_no_citation():
    v = ca.verify_answer("PRPF31 matters [PMID:99999999].", CONTEXTS)
    assert v.cited == [] and v.removed


def test_parse_questions_keeps_full_sentences_only():
    raw = '```json\n["Does PRPF31 loss cause adRP?", "RHO", "Does PRPF31 loss cause adRP?"]\n```'
    assert ca.parse_questions(raw) == ["Does PRPF31 loss cause adRP?"]
    assert ca.parse_questions("no json here") == []


# --- the research step ---------------------------------------------------------------------------

def _lab(answer: str, corpus_out: dict):
    from aiscientist.agents.research_lab import ResearchLab

    replies = iter(['["Does PRPF31 loss cause adRP?"]', answer])
    lab = types.SimpleNamespace(
        _complete=lambda _msgs: next(replies), ctx=None,
        _deep_literature_question=lambda *_a: "fallback question about PRPF31 and adRP")
    tool = types.SimpleNamespace(executor=lambda args, ctx: corpus_out)
    spec = types.SimpleNamespace(name="Geneticist")
    return lambda: ResearchLab._run_search_corpus_step(lab, "q", "step", [], tool, spec, lambda _e: None)


OK_OUT = {"status": "ok", "contexts": [
    {"pmid": "11111111", "citation": "Smith J. PRPF31 in adRP. 2001.", "text": TEXTS["11111111"][0]},
    {"pmid": "33333333", "citation": "Lee K. ABCA4 and Stargardt. 1999.", "text": TEXTS["33333333"][0]}]}


def test_step_accepts_a_checked_answer_and_keeps_only_cited_papers():
    res = _lab("PRPF31 haploinsufficiency causes adRP [PMID:11111111] [PMID:12121212].", OK_OUT)()
    assert res.status == "ok"
    assert "Smith J." in res.final_answer and "Lee K." not in res.final_answer
    assert "12121212" not in res.final_answer.split("Citations")[0]
    assert [c["pmid"] for c in res.steps[0]["result"]["contexts"]] == ["11111111"]


def test_step_is_incomplete_when_no_citation_survives_so_the_fallback_runs():
    res = _lab("PRPF31 matters [PMID:99999999].", OK_OUT)()
    assert res.status == "incomplete"


def test_step_is_incomplete_when_the_corpus_has_nothing():
    res = _lab("unused", {"status": "failed", "error": "0 passages", "contexts": []})()
    assert res.status == "incomplete" and res.stop_reason == "no_literature_citations"


def test_approved_queries_go_to_the_call_as_written():
    """Prod run BioAdmin/0dd83b85d736 (2026-10-18, OpenRouter): the plan pinned four queries, the
    step had the model write five others, and the plan check reported "0 of 1 approved steps ran as
    approved". Approved settings are applied whatever the model would send, as for every tool."""
    from aiscientist.agents.research_lab import ResearchLab

    prompts: list[str] = []
    sent: list[dict] = []

    def complete(msgs):
        prompts.append(msgs[0]["content"])
        return "PRPF31 haploinsufficiency causes adRP [PMID:11111111]."

    lab = types.SimpleNamespace(_complete=complete, ctx=None, _deep_literature_question=lambda *_a: "unused")
    tool = types.SimpleNamespace(executor=lambda args, ctx: sent.append(args) or OK_OUT)
    approved = {"queries": ["Is PRPF31 haploinsufficient in adRP?", "Does CNOT3 modify PRPF31 penetrance?"],
                "top_k": 8}
    res = ResearchLab._run_search_corpus_step(lab, "q", "step", [], tool, types.SimpleNamespace(name="G"),
                                              lambda _e: None, pinned=approved)
    assert res.status == "ok"
    assert sent == [approved]
    assert prompts == [ca.ANSWER_SYSTEM]          # no request-writing call: the plan already wrote them


def test_the_literature_branch_passes_the_plans_search_corpus_settings():
    from aiscientist.agents.research_lab import ResearchLab

    seen: dict = {}

    def run_step(*_a, pinned=None):
        seen["pinned"] = pinned
        return types.SimpleNamespace(status="ok")

    step = "Search the literature corpus with `search_corpus` for PRPF31 penetrance."
    lab = types.SimpleNamespace(
        scientist=types.SimpleNamespace(catalog=[types.SimpleNamespace(name="search_corpus")]),
        _pinned_settings=lambda s: {"search_corpus": {"queries": ["Is PRPF31 haploinsufficient?"]}},
        _run_search_corpus_step=run_step)
    ResearchLab._scientist(lab, "q", step, types.SimpleNamespace(name="G"), "", [], lambda _e: None)
    assert seen["pinned"] == {"queries": ["Is PRPF31 haploinsufficient?"]}
