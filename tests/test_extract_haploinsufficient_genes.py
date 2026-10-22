"""extract_haploinsufficient_genes without HPC3: a fake model, a toy embedder, a small text corpus,
and a PDF drawn with PyMuPDF for the extractor. What the tests pin down is the contract the curator
relies on — the passages come from the model's own search requests; a gene is listed only from an
explicit, verified, heterozygous-context statement; the other mechanism classes stay in hits.csv;
round two reads the other papers about a gene round one found; the review flags fire; a rerun
resumes without asking the model twice."""
from __future__ import annotations

import csv
import io
import json
import re
import types
import urllib.error
from pathlib import Path

import numpy as np
import pytest

from aiscientist.tools.extract_haploinsufficient_genes import tool as hi

PRPF31_SENT = ("Our data indicate that the disease mechanism in these families is haploinsufficiency "
               "of PRPF31, since the heterozygous null allele produces no stable transcript.")
RHO_SENT = ("The P23H allele of RHO acts through a dominant-negative effect on rhodopsin folding rather "
            "than by haploinsufficiency.")
ABCA4_SENT = ("Nonsense-mediated decay of the ABCA4 transcript was observed in the compound heterozygous "
              "patient, consistent with recessive Stargardt disease.")
REF_TITLE = ("Haploinsufficiency of the splicing factor gene PRP31 underlies autosomal dominant retinitis "
             "pigmentosa on chromosome 19q13.4 (RP11)")
REF_SENT = "Vithana EN, Abu-Safieh L, Allen MJ, Carey A, Papaioannou M. " + REF_TITLE + "."
# A paper that never says "haploinsufficiency": only the gene's own request in round two finds it.
DOSAGE_SENT = ("Fibroblasts from heterozygous PRPF31 carriers expressed half the normal amount of PRPF31 "
               "protein, so one functional copy is not enough in adRP.")

PAPERS = {
    "11111111": ["Retinitis pigmentosa is a heterogeneous disease with many causal genes.", PRPF31_SENT,
                 "Patients carried heterozygous deletions in PRPF31 and had typical adRP."],
    "22222222": ["Rhodopsin mutations are the commonest cause of adRP in most populations.", RHO_SENT,
                 "Transgenic mice confirm this finding in photoreceptors."],
    "33333333": ["Stargardt disease is recessive in almost every reported family.", ABCA4_SENT,
                 "Neuron 2001;29:125-132.", REF_SENT, "Nat Genet 1999;22:111-112."],
}
EXTRA = {"44444444": ["Splicing factor genes are expressed in every tissue of the eye.", DOSAGE_SENT,
                      "The retina appears to be the tissue most sensitive to this reduction."]}

REQUESTS = ["Heterozygous null alleles cause dominant retinitis pigmentosa through haploinsufficiency.",
            "One functional copy of the gene is not enough, so haploinsufficiency causes the retinal disease.",
            "Haploinsufficiency rather than a dominant-negative effect explains the dominant retinal dystrophy."]

ANSWERS = {
    PRPF31_SENT: {"gene": "PRPF31", "mechanism_class": "haploinsufficiency", "explicit": True, "negated": False,
                  "human_retinal": True, "phenotype": "autosomal dominant retinitis pigmentosa"},
    RHO_SENT: {"gene": "RHO", "mechanism_class": "dominant_negative", "explicit": True, "negated": True,
               "human_retinal": True, "phenotype": "adRP"},
    ABCA4_SENT: {"gene": "ABCA4", "mechanism_class": "recessive_lof", "explicit": True, "negated": False,
                 "human_retinal": True, "phenotype": "Stargardt disease"},
    REF_SENT: {"gene": "PRP31", "mechanism_class": "haploinsufficiency", "explicit": True, "negated": False,
               "human_retinal": True, "phenotype": "adRP"},
    DOSAGE_SENT: {"gene": "PRPF31", "mechanism_class": "haploinsufficiency", "explicit": True, "negated": False,
                  "human_retinal": True, "phenotype": "adRP"},
}


def _corpus(tmp_path: Path, extra: bool = False) -> Path:
    """A pre-built text cache: one chunk per paper."""
    root = tmp_path / "retigene"
    cache = root / "mechanism_cache"
    cache.mkdir(parents=True)
    papers = {**PAPERS, **(EXTRA if extra else {})}
    with (cache / "sentences.jsonl").open("w") as sf, (cache / "chunks.jsonl").open("w") as cf:
        for pmid, sents in papers.items():
            cf.write(json.dumps({"pmid": pmid, "chunk": 0, "page": 1, "text": " ".join(sents)}) + "\n")
            for k, s in enumerate(sents):
                sf.write(json.dumps({"sid": f"{pmid}:0:{k}", "pmid": pmid, "chunk": 0, "sent": k, "page": 1,
                                     "text": s, "prev": sents[k - 1] if k else "",
                                     "next": sents[k + 1] if k + 1 < len(sents) else ""}) + "\n")
    (root / "papers").mkdir()
    with (root / "paperqa_manifest.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file_location", "title", "doc_id"])
        w.writerow(["11111111.pdf", "PRPF31 haploinsufficiency in adRP families", "11111111"])
        w.writerow(["22222222.pdf", "Dominant-negative rhodopsin", "22222222"])
        w.writerow(["33333333.pdf", REF_TITLE, "33333333"])
        w.writerow(["44444444.pdf", "PRPF31 protein levels in carrier fibroblasts", "44444444"])
    with (root / "hgnc_complete_set.txt").open("w") as fh:
        fh.write("hgnc_id\tsymbol\talias_symbol\tprev_symbol\n")
        fh.write("HGNC:1\tPRPF31\tPRP31\tRP11\n")
        fh.write("HGNC:2\tRHO\t\tRP4\n")
        fh.write("HGNC:3\tABCA4\tABCR\tSTGD1\n")
    return root


def _embed(texts):
    """A toy PubMedBERT: one axis for the mechanism's word, one for PRPF31, a little of everything."""
    return np.array([[1.0 if "haploinsufficiency" in t.lower() else 0.0, 1.0 if "PRPF31" in t else 0.0, 0.1]
                     for t in texts], dtype="float32")


def _system(messages) -> str:
    return messages[0]["content"]


def _passage(messages) -> tuple[str, list[str]]:
    """The PMID and the numbered sentences of a passage prompt."""
    user = messages[1]["content"]
    pmid = re.search(r"PMID (\d+)", user).group(1)
    return pmid, [m.group(1) for m in re.finditer(r"^\[\d+\] (.*)$", user, re.M)]


# What the fake model writes as its search words: one in the corpus, one too common (in every chunk
# of the fixture), one absent and a gene name.
MODEL_TERMS = ["one copy is not enough", "retinitis", "zzzunusedword", "PRPF31"]


def _fake_model(calls: list[str] | None = None, written: list[str] | None = None):
    """Writes the requests, and reads a passage like the served Qwen would: one item per mechanism
    sentence it knows, nothing for the rest."""
    def model(messages):
        system = _system(messages)
        if system == hi.GENE_REQUESTS_SYSTEM:
            genes = [line.split(" — ")[0] for line in messages[1]["content"].splitlines()]
            if written is not None:
                written.extend(genes)
            return json.dumps({"requests": {g: f"Heterozygous loss of {g} causes adRP through haploinsufficiency."
                                            for g in genes}})
        if system.startswith(hi.TERMS_SYSTEM.split("{N}")[0][:80]):
            if written is not None:
                written.append("terms")
            return json.dumps({"terms": MODEL_TERMS})
        if system != hi.JUDGE_SYSTEM:
            if written is not None:
                written.append("round1")
            return json.dumps({"requests": REQUESTS})
        pmid, sents = _passage(messages)
        if calls is not None:
            calls.append(pmid)
        items = [{**ANSWERS[s], "sentence": k + 1, "quote": s} for k, s in enumerate(sents) if s in ANSWERS]
        return json.dumps({"items": items})
    return model


def _ctx(tmp_path: Path, monkeypatch, root: Path):
    monkeypatch.setenv("AISCIENTIST_PAPERQA_PAPERS", str(root / "papers"))
    monkeypatch.setenv("AISCIENTIST_PAPERQA_MANIFEST", str(root / "paperqa_manifest.csv"))
    ws = tmp_path / "run"
    ws.mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(model="qwen", llm_base_url="http://unused:1/v1", tunnel_port=None,
                                 workspace=ws, decisions={}, llm_is_remote=False)


def _read(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _run(ctx, model, args=None):
    return hi.run_extract(args or {}, ctx, judge_fn=model, embed_fn=_embed, log=lambda s: None)


def test_only_explicit_verified_heterozygous_statements_make_genes_csv(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    res = _run(ctx, _fake_model())
    assert res["status"] == "ok", res
    genes = _read(ctx.workspace / res["files"]["genes_csv"])
    hits = _read(ctx.workspace / res["files"]["hits_csv"])
    assert [g["gene"] for g in genes] == ["PRPF31"]
    g = genes[0]
    assert g["pmid_1"] == "11111111" and g["quote_1"] == PRPF31_SENT
    assert g["phenotype"] == "autosomal dominant retinitis pigmentosa"
    assert g["mechanism_statement"].lower().startswith("haploinsufficiency")
    # The reference-list entry (PRP31, an alias; cite-like) counts as a second paper but ranks after
    # the body sentence; the row's gene_status is that of the best quote, the hit row says "alias".
    assert g["n_papers"] == "2" and g["pmid_2"] == "33333333"
    assert g["gene_status"] == "ok" and g["flag_gene_alias_or_unknown"] == "False"
    assert g["flag_only_cited_titles"] == "False" and g["flag_no_dominance_marker"] == "False"
    ref = [h for h in hits if h["sentence_id"] == "33333333:0:3"][0]
    assert ref["cite_like"] == "True" and ref["gene_status"] == "alias" and ref["gene"] == "PRPF31"
    # The other classes are in hits.csv, not in genes.csv.
    by_gene = {h["gene"]: h for h in hits}
    assert by_gene["RHO"]["mechanism_class"] == "dominant_negative" and by_gene["RHO"]["eligible"] == "False"
    assert by_gene["ABCA4"]["mechanism_class"] == "recessive_lof" and by_gene["ABCA4"]["eligible"] == "False"
    assert res["n_genes"] == 1 and res["genes"][0]["gene"] == "PRPF31"
    # Every hit says which request found its passage; the requests are a table of their own.
    assert all(h["found_by"] for h in hits)
    reqs = _read(ctx.workspace / res["files"]["requests_csv"])
    assert [r["request"] for r in reqs if r["round"] == "1" and r["id"] not in ("K", "T")] == REQUESTS
    # The keyword round is a row of its own: the three chunks that say "haploinsufficiency".
    kw = [r for r in reqs if r["id"] == "K"][0]
    assert kw["round"] == "1" and kw["request"] == hi.KEYWORD_REQUEST and kw["n_passages"] == "3"
    assert [r["gene"] for r in reqs if r["round"] == "2"] == ["PRPF31"]
    assert res["n_requests"] == 3 and res["n_gene_requests"] == 1 and res["requests"] == REQUESTS
    assert res["n_keyword_passages"] == 3 and res["n_keyword_only_passages"] == 0
    assert res["n_passages"] == 3 and res["n_judged"] == 3 and not res["warnings"]


def test_each_passage_is_read_once_and_the_requests_name_no_gene(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    calls: list[str] = []
    res = _run(ctx, _fake_model(calls))
    assert res["status"] == "ok"
    assert sorted(calls) == ["11111111", "22222222", "33333333"]     # one call per passage, never per sentence
    assert "Do NOT name any specific gene" in hi.REQUESTS_SYSTEM


def test_round_two_reads_the_papers_round_one_missed(tmp_path, monkeypatch):
    """Round one retrieves the passages that read like the mechanism; the paper that states PRPF31's
    dosage without the word is found by PRPF31's own request, among the chunks naming PRPF31.
    The vectors are cached next to the text. (The keyword round is off here: it would read that
    paper in round one, for its "one functional copy".)"""
    root = _corpus(tmp_path, extra=True)
    ctx = _ctx(tmp_path, monkeypatch, root)
    calls: list[str] = []
    res = _run(ctx, _fake_model(calls), {"k_per_request": 3, "keyword_round": 0})
    assert res["status"] == "ok", res
    assert (root / "mechanism_cache" / "chunk_vecs.npy").exists()
    assert calls[-1] == "44444444" and calls.count("44444444") == 1
    genes = _read(ctx.workspace / res["files"]["genes_csv"])
    assert genes[0]["gene"] == "PRPF31" and genes[0]["n_papers"] == "3"
    dosage = [h for h in _read(ctx.workspace / res["files"]["hits_csv"]) if h["pmid"] == "44444444"][0]
    assert dosage["found_by"] == "G:PRPF31" and dosage["eligible"] == "True"
    # Without round two, the paper is never read.
    ctx2 = _ctx(tmp_path / "b", monkeypatch, root)
    res2 = _run(ctx2, _fake_model(), {"k_per_request": 3, "k_per_gene": 0, "keyword_round": 0})
    assert res2["n_gene_requests"] == 0
    assert _read(ctx2.workspace / res2["files"]["genes_csv"])[0]["n_papers"] == "2"


# A paper whose PDF writes the word with the "ﬁ" ligature (62% of the corpus' sentences that use
# it do). The toy embedder, like PubMedBERT's tokens, does not see "haploinsufficiency" in it.
LIGATURE_SENT = ("We conclude that haploinsufﬁciency of CRX causes the dominant cone-rod dystrophy in "
                 "this family, as the heterozygous frameshift allele is degraded.")
LIGATURE = {"55555555": ["Cone-rod dystrophy has dominant and recessive forms in the clinic.", LIGATURE_SENT,
                         "The proband's mother carries the same allele and is mildly affected."]}


def test_the_keyword_round_reads_the_worded_passages_no_request_found(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    cache = root / "mechanism_cache"
    with (cache / "sentences.jsonl").open("a") as sf, (cache / "chunks.jsonl").open("a") as cf:
        for pmid, sents in LIGATURE.items():
            cf.write(json.dumps({"pmid": pmid, "chunk": 0, "page": 2, "text": " ".join(sents)}) + "\n")
            for k, s in enumerate(sents):
                sf.write(json.dumps({"sid": f"{pmid}:0:{k}", "pmid": pmid, "chunk": 0, "sent": k, "page": 2,
                                     "text": s, "prev": sents[k - 1] if k else "",
                                     "next": sents[k + 1] if k + 1 < len(sents) else ""}) + "\n")
    with (root / "hgnc_complete_set.txt").open("a") as fh:
        fh.write("HGNC:4\tCRX\tCORD2\t\n")
    base = _fake_model()

    def model(messages):
        if _system(messages) == hi.JUDGE_SYSTEM and _passage(messages)[0] == "55555555":
            # The model types the ligature out as "fi": the quote must still verify.
            return json.dumps({"items": [{"gene": "CRX", "mechanism_class": "haploinsufficiency",
                                          "explicit": True, "negated": False, "human_retinal": True,
                                          "phenotype": "dominant cone-rod dystrophy", "sentence": 2,
                                          "quote": LIGATURE_SENT.replace("ﬁ", "fi")}]})
        return base(messages)

    # The requests alone (k=3 of 4 chunks) never reach it.
    ctx0 = _ctx(tmp_path / "a", monkeypatch, root)
    res0 = _run(ctx0, model, {"k_per_request": 3, "keyword_round": 0, "k_per_gene": 0})
    assert "CRX" not in {g["gene"] for g in res0["genes"]}
    # The keyword round reads it, and only it is new.
    ctx = _ctx(tmp_path / "b", monkeypatch, root)
    res = _run(ctx, model, {"k_per_request": 3, "k_per_gene": 0})
    assert res["status"] == "ok", res
    assert res["n_keyword_passages"] == 4 and res["n_keyword_only_passages"] == 1
    genes = {g["gene"]: g for g in _read(ctx.workspace / res["files"]["genes_csv"])}
    assert genes["CRX"]["pmid_1"] == "55555555"
    hit = [h for h in _read(ctx.workspace / res["files"]["hits_csv"]) if h["pmid"] == "55555555"][0]
    assert hit["found_by"] == "K" and hit["quote_verified"] == "True" and hit["eligible"] == "True"


def test_keyword_wording_folds_ligatures_and_skips_ordinary_dosage():
    chunks = [{"text": "Haploinsufﬁciency of PAX6 causes aniridia."},
              {"text": "One functional copy of the gene is not sufficient for normal vision."},
              {"text": "The drug dosage was reduced after the first week."},
              {"text": "Nonsense-mediated decay removed the mutant transcript in the recessive case."}]
    assert hi.keyword_chunks(chunks) == [0, 1]
    assert hi.nz("haploinsufﬁciency") == hi.nz("haploinsufficiency")
    # A PDF that extracted the en dash as a control character still verifies the model's quote.
    assert hi.nz("maintain 60\x0180% of the normal level") == hi.nz("maintain 60–80% of the normal level")


def test_a_quote_the_paper_does_not_contain_is_rejected(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    base = _fake_model()

    def invented(messages):
        if _system(messages) == hi.JUDGE_SYSTEM and _passage(messages)[0] == "11111111":
            return json.dumps({"items": [{"gene": "PRPF31", "mechanism_class": "haploinsufficiency",
                                          "explicit": True, "negated": False, "human_retinal": True,
                                          "phenotype": "adRP", "sentence": 2,
                                          "quote": "PRPF31 haploinsufficiency causes adRP in every family."}]})
        if _system(messages) == hi.JUDGE_SYSTEM:
            return json.dumps({"items": []})
        return base(messages)

    res = _run(ctx, invented)
    assert res["status"] == "ok" and res["n_genes"] == 0
    hits = _read(ctx.workspace / res["files"]["hits_csv"])
    assert hits and hits[0]["quote_verified"] == "False" and hits[0]["eligible"] == "False"
    assert hits[0]["sentence_id"] == "11111111:0:1"          # filed under the sentence number it gave


def test_rerun_resumes_and_retries_only_failed_passages(tmp_path, monkeypatch):
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    calls: list[str] = []
    written: list[str] = []
    base = _fake_model(calls, written)

    def flaky(messages):
        if _system(messages) == hi.JUDGE_SYSTEM and _passage(messages)[0] == "11111111":
            raise OSError("connection reset")
        return base(messages)

    res1 = _run(ctx, flaky, {"concurrency": 2})
    assert res1["status"] == "ok"
    assert any("could not be read" in w for w in res1["warnings"])
    # Without the body sentence, PRPF31 rests on the reference-list entry alone (and is flagged so).
    genes1 = _read(ctx.workspace / res1["files"]["genes_csv"])
    assert [(g["gene"], g["n_papers"], g["flag_only_cited_titles"]) for g in genes1] == [("PRPF31", "1", "True")]
    calls.clear()
    written.clear()
    res2 = _run(ctx, base, {"concurrency": 2})
    assert res2["status"] == "ok" and res2["n_genes"] == 1
    assert calls == ["11111111"], "only the failed passage is read again"
    assert written == [], "the kept requests are reused, not written again"
    genes2 = _read(ctx.workspace / res2["files"]["genes_csv"])
    assert genes2[0]["n_papers"] == "2" and genes2[0]["flag_only_cited_titles"] == "False"


def test_a_reply_the_parser_rejected_is_parsed_again_without_a_model_call(tmp_path, monkeypatch):
    """The whole reply is kept: after a parser fix, the rerun reads it from disk instead of paying
    for the passage again."""
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    base = _fake_model()
    real_parse = hi.parse_items

    def chatty(messages):
        reply = base(messages)
        return "Here is the JSON you asked for:\n" + reply if _system(messages) == hi.JUDGE_SYSTEM else reply

    def strict(text):                          # the old parser: bare JSON only
        return None if not text.lstrip().startswith("{") else real_parse(text)

    monkeypatch.setattr(hi, "parse_items", strict)
    res1 = _run(ctx, chatty)
    assert res1["status"] == "failed" and res1["n_judged"] == 0
    monkeypatch.setattr(hi, "parse_items", real_parse)

    def must_not_read(messages):
        assert _system(messages) != hi.JUDGE_SYSTEM, "a kept reply was sent to the model again"
        return base(messages)

    res2 = _run(ctx, must_not_read)
    assert res2["status"] == "ok" and res2["n_genes"] == 1


def test_max_passages_caps_a_smoke_test(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    calls: list[str] = []
    res = _run(ctx, _fake_model(calls), {"max_passages": 1})
    assert res["status"] == "ok" and res["n_passages"] == 1 and len(calls) == 1


def test_without_corpus_model_or_embedder_the_tool_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv("AISCIENTIST_PAPERQA_PAPERS", raising=False)
    ctx = types.SimpleNamespace(model="qwen", llm_base_url="http://x/v1", tunnel_port=None,
                                workspace=tmp_path, decisions={}, llm_is_remote=False)
    assert hi.run_extract({}, ctx, log=lambda s: None)["status"] == "not_enabled"
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    ctx.model = None
    assert hi.run_extract({}, ctx, log=lambda s: None)["status"] == "not_enabled"
    ctx.model = "qwen"
    monkeypatch.setattr(hi, "sentence_transformer_embed", lambda hf: None)
    res = hi.run_extract({}, ctx, judge_fn=_fake_model(), log=lambda s: None)
    assert res["status"] == "failed" and "PubMedBERT" in res["error"]


def test_unusable_requests_fail_the_scan_plainly(tmp_path, monkeypatch):
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    res = _run(ctx, lambda messages: json.dumps({"requests": ["too short"]}))
    assert res["status"] == "failed" and "search requests" in res["error"] and not res.get("model_unreachable")


def test_pdf_extraction_reads_columns_in_order_and_stops_at_references(tmp_path):
    fitz = pytest.importorskip("pymupdf")
    pdf = tmp_path / "12345678.pdf"
    doc = fitz.open()
    for pno in range(3):
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 20), f"Running header of the journal {pno}", fontsize=8)      # header strip
        page.insert_text((40, 100), "Left column first sentence here.", fontsize=10)
        page.insert_text((40, 130), "Left column second sentence here.", fontsize=10)
        page.insert_text((320, 100), "Right column first sentence here.", fontsize=10)
        page.insert_text((320, 130), "Right column second sentence here.", fontsize=10)
        if pno == 2:
            page.insert_text((40, 300), "References\nSmith J, Doe A. A paper title. J Something 2001;1:1-2.",
                             fontsize=10)
        page.insert_text((40, 790), f"Page {pno + 1} of the article", fontsize=8)            # footer strip
    doc.save(str(pdf))
    chunks = hi.paper_chunks(pdf)
    assert [p for p, _ in chunks] == [1, 2, 3]
    text = chunks[0][1]
    assert "Running header" not in text and "Page 1" not in text
    assert text.index("Left column first") < text.index("Left column second") < text.index("Right column first")
    assert "Smith J" not in chunks[2][1] and "References" not in chunks[2][1]
    sents = hi.chunk_sentences("12345678", 0, 1, text)
    assert [s.text for s in sents][:2] == ["Left column first sentence here.", "Left column second sentence here."]
    assert sents[1].prev == sents[0].text and sents[0].next == sents[1].text and sents[0].sid == "12345678:0:0"
    assert hi.corpus_files(tmp_path, []) == [("12345678", pdf)]
    assert hi.corpus_files(tmp_path, [{"file_location": "12345678.pdf", "doc_id": "99"}]) == [("99", pdf)]


def test_replies_are_parsed_as_given():
    txt = '```json\n{"items":[{"gene":"CRX","mechanism_class":"haploinsufficiency","explicit":true,' \
          '"negated":false,"human_retinal":true,"phenotype":"LCA","quote":"q"},"junk"]}\n```'
    assert hi.parse_items(txt) == [{"gene": "CRX", "mechanism_class": "haploinsufficiency", "explicit": True,
                                    "negated": False, "human_retinal": True, "phenotype": "LCA", "quote": "q"}]
    assert hi.parse_items('{"items": []}') == []
    assert hi.parse_items('Sure, here it is:\n{"items": []}') == []    # a line before the JSON
    assert hi.parse_items("not json") is None
    assert hi.parse_items('{"items": "x"}') is None
    reqs = ["Heterozygous null alleles cause adRP.", "one copy is not enough here", "Dosage explains the disease."]
    assert hi.parse_requests(json.dumps({"requests": reqs + [reqs[0], "short", 7]}), 16) == reqs
    assert hi.parse_requests(json.dumps({"requests": reqs}), 2) == reqs[:2]
    assert hi.parse_requests(json.dumps({"requests": reqs[:2]}), 16) is None
    assert hi.parse_gene_requests('{"requests": {"prpf31": "PRPF31 loss causes adRP here.", "XYZ": "a b c d"}}',
                                  ["PRPF31"]) == {"PRPF31": "PRPF31 loss causes adRP here."}
    assert hi.parse_gene_requests('{"requests": {}}', ["PRPF31"]) is None


def test_a_gene_the_writer_left_out_gets_the_template_and_a_warning(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    base = _fake_model()

    def forgetful(messages):
        if _system(messages) == hi.GENE_REQUESTS_SYSTEM:
            return json.dumps({"requests": {"RHO": "RHO is dominant-negative in adRP."}})
        return base(messages)

    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    res = _run(ctx, forgetful)
    assert res["status"] == "ok"
    reqs = _read(ctx.workspace / res["files"]["requests_csv"])
    # The phenotype is the first one read for the gene (the reference entry's, in retrieval order).
    assert [r["request"] for r in reqs if r["gene"] == "PRPF31"] == [
        hi.GENE_REQUEST_TEMPLATE.format(gene="PRPF31", phenotype="adRP")]
    assert any("fixed template" in w for w in res["warnings"])


def test_hgnc_normalisation_prefers_approved_symbols(tmp_path):
    p = tmp_path / "hgnc.txt"
    p.write_text("hgnc_id\tsymbol\talias_symbol\tprev_symbol\n"
                 "HGNC:1\tRP1\t\t\nHGNC:2\tMAPRE2\tRP1\t\nHGNC:3\tNRG4\tHRG4\t\nHGNC:4\tUNC119\tHRG4\t\n"
                 "HGNC:5\tPDE6B\tPDEB\t\n")
    h = hi.Hgnc(p)
    assert h.norm("RP1") == ("RP1", "ok")            # the approved symbol wins over MAPRE2's alias
    assert h.norm("HRG4") == ("HRG4", "ambiguous:NRG4|UNC119")
    assert h.norm("PDEB") == ("PDE6B", "alias")
    assert h.norm("mapre2") == ("MAPRE2", "ok")
    assert h.norm("XYZ123") == ("XYZ123", "unknown")
    assert h.norm(None) == (None, "missing")
    assert hi.Hgnc(None).norm("Rho") == ("RHO", "unchecked")


def test_reference_entries_are_recognised():
    is_cite = hi.make_is_cite(["A human homolog of yeast pre-mRNA splicing gene underlies adRP on 19q13.4"])
    assert is_cite("Vithana EN, Abu-Safieh L, Allen MJ. A human homolog of yeast pre-mRNA splicing gene underlies "
                   "adRP on 19q13.4.")
    assert is_cite("Haploinsufficiency of MITF causes Waardenburg syndrome type 2.",
                   "Hum Mol Genet 1998;7:123-130.", "Nat Genet 1994;8:251-255.")
    assert not is_cite("We conclude that haploinsufficiency of PRPF31 causes adRP.",
                       "Hum Mol Genet 1998;7:123-130.", "")
    assert not is_cite("Our data indicate that the mechanism is haploinsufficiency of PRPF31.")


def test_round_two_searches_only_for_symbols_hgnc_knows(tmp_path):
    hgnc = tmp_path / "hgnc.txt"
    hgnc.write_text("hgnc_id\tsymbol\talias_symbol\tprev_symbol\nHGNC:1\tPRPF31\tPRP31\t\n")
    items = [{"gene": g, "mechanism_class": "haploinsufficiency", "negated": False, "phenotype": "adRP"}
             for g in ("PRP31", "the", "NOTAGENE")]
    items.append({"gene": "PRPF31", "mechanism_class": "haploinsufficiency", "negated": True})
    p = hi.Passage("1:0", "1", 0, 1, [])
    got = hi.round_two_genes([p], {"1:0": hi.Judgment("1:0", items)}, hi.Hgnc(hgnc))
    assert got == {"PRPF31": {"terms": {"PRPF31", "PRP31"}, "phenotypes": ["adRP"]}}


def test_retrieval_keeps_a_few_chunks_per_paper_and_finds_genes_as_words():
    chunks = [{"pmid": "1", "chunk": i, "text": t} for i, t in enumerate(["PRPF31 a", "b", "c", "d"])]
    chunks.append({"pmid": "2", "chunk": 0, "text": "xPRPF31 and prpf31 and PRPF310"})
    vecs = hi._l2(np.array([[1.0, 0.0], [0.9, 0.1], [0.8, 0.2], [0.7, 0.3], [0.1, 0.9]], dtype="float32"))
    q = hi._l2(np.array([1.0, 0.0], dtype="float32"))
    assert hi.nearest_chunks(q, vecs, chunks, 4, max_per_paper=3) == [0, 1, 2, 4]
    assert hi.nearest_chunks(q, vecs, chunks, 2, candidates=[3, 4]) == [3, 4]
    assert hi.chunks_naming({"PRPF31": {"PRPF31"}}, chunks) == {"PRPF31": {0}}


def test_paperqa_cli_dispatches_the_tool(tmp_path, monkeypatch):
    from aiscientist.tools import paperqa_cli
    root = _corpus(tmp_path)
    seen = {}

    def fake_run(args, ctx):
        seen["args"], seen["ctx"] = args, ctx
        return {"status": "ok", "n_genes": 0}

    monkeypatch.setattr(hi, "run_extract", fake_run)
    # run_tool exports the injected deployment args to the environment (its contract with the
    # tools); register them so the teardown removes what this call sets.
    for key in ("AISCIENTIST_PAPERQA_LLM_BASE_URL", "AISCIENTIST_PAPERQA_PAPERS", "AISCIENTIST_PAPERQA_MANIFEST"):
        monkeypatch.setenv(key, "")
    res = paperqa_cli.run_tool("extract_haploinsufficient_genes", str(tmp_path / "ws"),
                               {"model": "qwen", "llm_base_url": "http://gpu:1/v1", "papers": str(root / "papers"),
                                "manifest": str(root / "paperqa_manifest.csv"), "max_passages": 5})
    assert res["status"] == "ok"
    assert seen["args"]["max_passages"] == 5 and "model" not in seen["args"]
    assert seen["ctx"].llm_base_url == "http://gpu:1/v1" and seen["ctx"].workspace == str(tmp_path / "ws")


def test_a_dead_endpoint_stops_the_pass_at_once(tmp_path, monkeypatch):
    """Every passage refused (the job's model died): the pass gives up after a short streak,
    reports model_unreachable instead of a hollow 'ok' with zero genes, writes no judgment lines
    for the passages it never reached, and a rerun with a live model reads them all."""
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    base = _fake_model()

    def refused(messages):
        if _system(messages) == hi.JUDGE_SYSTEM:
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        return base(messages)

    monkeypatch.setattr(hi, "_DEAD_AFTER", 3)
    res = _run(ctx, refused, {"concurrency": 2})
    assert res["status"] == "failed" and res["model_unreachable"] is True
    assert "stopped answering" in res["error"] and res["n_judged"] == 0
    jl = ctx.workspace / "work" / "haploinsufficiency" / "judgments.jsonl"
    assert not jl.exists() or all(json.loads(line)["error"] != "model unreachable" for line in jl.open())
    # Back with a live model: everything is read, nothing was lost.
    res2 = _run(ctx, base, {"concurrency": 2})
    assert res2["status"] == "ok" and res2["n_genes"] == 1 and not res2["warnings"]


def test_a_model_gone_before_the_requests_is_unreachable_too(tmp_path, monkeypatch):
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)

    def refused(messages):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    res = _run(ctx, refused)
    assert res["status"] == "failed" and res["model_unreachable"] is True and res["n_judged"] == 0


def test_an_http_error_is_not_a_dead_endpoint(tmp_path, monkeypatch):
    """A 4xx/5xx the server answered with is the server's problem with that request, not a dead
    port: the pass continues, the passage is recorded as failed and read again next run."""
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    base = _fake_model()

    def bad_request(messages):
        if _system(messages) == hi.JUDGE_SYSTEM and _passage(messages)[0] == "22222222":
            raise urllib.error.HTTPError("http://x", 400, "Bad Request", {}, io.BytesIO(b""))
        return base(messages)

    monkeypatch.setattr(hi, "_DEAD_AFTER", 1)
    res = _run(ctx, bad_request, {"concurrency": 2})
    assert res["status"] == "ok" and res["n_genes"] == 1
    assert any("1 of" in w and "could not be read" in w for w in res["warnings"])


def test_a_reply_that_is_not_a_completion_fails_its_passage_only(tmp_path, monkeypatch):
    """A 200 with no choices (IndexError inside the client) used to escape the worker thread and
    end the whole pass. It is one failed passage: the rest are read and the rerun retries it."""
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    base = _fake_model()

    def no_choices(messages):
        if _system(messages) == hi.JUDGE_SYSTEM and _passage(messages)[0] == "22222222":
            return str({"choices": []}["choices"][0])
        return base(messages)

    res = _run(ctx, no_choices, {"concurrency": 2})
    assert res["status"] == "ok" and res["n_genes"] == 1
    assert any("1 of" in w and "could not be read" in w for w in res["warnings"])


def test_the_models_words_are_checked_against_the_corpus_before_any_passage_is_added():
    chunks = ([{"pmid": str(i), "chunk": 0, "text": f"Retinal dystrophy family {i} with a heterozygous variant."}
               for i in range(300)]
              + [{"pmid": "a", "chunk": 0, "text": "Reduced dosage: one functional copy is not enough."},
                 {"pmid": "b", "chunk": 0, "text": "PRPF31 haplo-insufficiency in adRP."}])
    index = hi.TextIndex.of_chunks(chunks)
    terms, added = hi.model_term_chunks(
        ["one functional copy", "haploinsufficient", "heterozygous", "zzzabsent", "PRPF31"], index, cap=10)
    use = {t.text: t.use for t in terms}
    assert use == {"one functional copy": "filter", "haploinsufficient": "filter",   # hyphenated spelling too
                   "heterozygous": "rank",          # in 300 of 302 chunks: too common to read every one
                   "zzzabsent": "absent", "PRPF31": "rank"}                          # a gene selects no mechanism
    assert added == {300, 301}
    # The cap counts only chunks not read anyway: rarest words first, and a word that would pass it
    # is left out.
    capped, few = hi.model_term_chunks(["one functional copy", "haploinsufficient"], index, cap=1)
    assert len(few) == 1 and sorted(t.use for t in capped) == ["filter", "over_cap"]
    both, held = hi.model_term_chunks(["one functional copy", "haploinsufficient"], index, cap=1, already={301})
    assert held == {300, 301} and [t.use for t in both] == ["filter", "filter"]


def test_a_reply_with_words_is_parsed_and_a_bad_one_is_not():
    assert hi.parse_model_terms(json.dumps({"terms": ["gene dosage", "a b c d e", 3, "one functional copy"]})) == [
        "gene dosage", "one functional copy"]
    assert hi.parse_model_terms("no json") is None
