"""extract_haploinsufficient_genes without HPC3: a fake model, a three-paper text corpus, and a PDF
drawn with PyMuPDF for the extractor. What the tests pin down is the contract the curator relies
on — a gene is listed only from an explicit, verified, heterozygous-context statement; the other
mechanism classes stay in hits.csv; the review flags fire; a rerun resumes."""
from __future__ import annotations

import csv
import json
import types
from pathlib import Path

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


def _corpus(tmp_path: Path) -> Path:
    """A pre-built text cache: three papers, one chunk each."""
    root = tmp_path / "retigene"
    cache = root / "mechanism_cache"
    cache.mkdir(parents=True)
    papers = {
        "11111111": ["Retinitis pigmentosa is a heterogeneous disease with many causal genes.", PRPF31_SENT,
                     "Patients carried heterozygous deletions in PRPF31 and had typical adRP."],
        "22222222": ["Rhodopsin mutations are the commonest cause of adRP in most populations.", RHO_SENT,
                     "Transgenic mice confirm this finding in photoreceptors."],
        "33333333": ["Stargardt disease is recessive in almost every reported family.", ABCA4_SENT,
                     "Neuron 2001;29:125-132.", REF_SENT, "Nat Genet 1999;22:111-112."],
    }
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
    with (root / "hgnc_complete_set.txt").open("w") as fh:
        fh.write("hgnc_id\tsymbol\talias_symbol\tprev_symbol\n")
        fh.write("HGNC:1\tPRPF31\tPRP31\tRP11\n")
        fh.write("HGNC:2\tRHO\t\tRP4\n")
        fh.write("HGNC:3\tABCA4\tABCR\tSTGD1\n")
    return root


def _target(messages) -> str:
    return messages[1]["content"].split("TARGET: ", 1)[1].split("\nCONTEXT AFTER:")[0]


def _fake_model(calls: list[str] | None = None):
    """Answers like the served Qwen would for the four mechanism sentences; nothing for the rest."""
    def judge(messages):
        target = _target(messages)
        if calls is not None:
            calls.append(target)
        if target == PRPF31_SENT:
            items = [{"gene": "PRPF31", "mechanism_class": "haploinsufficiency", "explicit": True,
                      "negated": False, "human_retinal": True, "phenotype": "autosomal dominant retinitis pigmentosa",
                      "quote": PRPF31_SENT}]
        elif target == RHO_SENT:
            items = [{"gene": "RHO", "mechanism_class": "dominant_negative", "explicit": True, "negated": True,
                      "human_retinal": True, "phenotype": "adRP", "quote": RHO_SENT}]
        elif target == ABCA4_SENT:
            items = [{"gene": "ABCA4", "mechanism_class": "recessive_lof", "explicit": True, "negated": False,
                      "human_retinal": True, "phenotype": "Stargardt disease", "quote": ABCA4_SENT}]
        elif target == REF_SENT:
            items = [{"gene": "PRP31", "mechanism_class": "haploinsufficiency", "explicit": True, "negated": False,
                      "human_retinal": True, "phenotype": "adRP", "quote": REF_SENT}]
        else:
            items = []
        return json.dumps({"items": items})
    return judge


def _ctx(tmp_path: Path, monkeypatch, root: Path):
    monkeypatch.setenv("AISCIENTIST_PAPERQA_PAPERS", str(root / "papers"))
    monkeypatch.setenv("AISCIENTIST_PAPERQA_MANIFEST", str(root / "paperqa_manifest.csv"))
    ws = tmp_path / "run"
    ws.mkdir(exist_ok=True)
    return types.SimpleNamespace(model="qwen", llm_base_url="http://unused:1/v1", tunnel_port=None,
                                 workspace=ws, decisions={}, llm_is_remote=False)


def _read(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_only_explicit_verified_heterozygous_statements_make_genes_csv(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    res = hi.run_extract({}, ctx, judge_fn=_fake_model(), embed_fn=None, log=lambda s: None)
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
    assert any("keyword only" in w for w in res["warnings"])   # no embedder in the test


def test_a_quote_the_paper_does_not_contain_is_rejected(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)

    def invented(messages):
        if _target(messages) == PRPF31_SENT:
            return json.dumps({"items": [{"gene": "PRPF31", "mechanism_class": "haploinsufficiency",
                                          "explicit": True, "negated": False, "human_retinal": True,
                                          "phenotype": "adRP",
                                          "quote": "PRPF31 haploinsufficiency causes adRP in every family."}]})
        return json.dumps({"items": []})

    res = hi.run_extract({}, ctx, judge_fn=invented, embed_fn=None, log=lambda s: None)
    assert res["status"] == "ok" and res["n_genes"] == 0
    hits = _read(ctx.workspace / res["files"]["hits_csv"])
    assert hits and hits[0]["quote_verified"] == "False" and hits[0]["eligible"] == "False"


def test_rerun_resumes_and_retries_only_failed_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    calls: list[str] = []
    base = _fake_model(calls)

    def flaky(messages):
        if _target(messages) == PRPF31_SENT:
            raise OSError("connection reset")
        return base(messages)

    res1 = hi.run_extract({"concurrency": 2}, ctx, judge_fn=flaky, embed_fn=None, log=lambda s: None)
    assert res1["status"] == "ok"
    assert any("could not be judged" in w for w in res1["warnings"])
    # Without the body sentence, PRPF31 rests on the reference-list entry alone (and is flagged so).
    genes1 = _read(ctx.workspace / res1["files"]["genes_csv"])
    assert [(g["gene"], g["n_papers"], g["flag_only_cited_titles"]) for g in genes1] == [("PRPF31", "1", "True")]
    n_first = len(calls)
    calls.clear()
    res2 = hi.run_extract({"concurrency": 2}, ctx, judge_fn=base, embed_fn=None, log=lambda s: None)
    assert res2["status"] == "ok" and res2["n_genes"] == 1
    assert calls == [PRPF31_SENT], "only the failed sentence is re-judged"
    assert n_first > 1
    genes2 = _read(ctx.workspace / res2["files"]["genes_csv"])
    assert genes2[0]["n_papers"] == "2" and genes2[0]["flag_only_cited_titles"] == "False"


def test_semantic_candidates_use_the_chunk_vectors(tmp_path, monkeypatch):
    """With an embedder, a chunk near a mechanism query brings in its soft-marker sentences even
    without a keyword hit; the vectors are cached next to the text."""
    import numpy as np
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)

    def embed(texts):
        return np.array([[1.0, 0.0] if ("PRPF31" in t or "haploinsufficiency" in t) else [0.0, 1.0] for t in texts],
                        dtype="float32")

    res = hi.run_extract({}, ctx, judge_fn=_fake_model(), embed_fn=embed, log=lambda s: None)
    assert res["status"] == "ok"
    assert (root / "mechanism_cache" / "chunk_vecs.npy").exists()
    cands = {c["sid"]: c for c in map(json.loads, (ctx.workspace / "work" / "haploinsufficiency" / "candidates.jsonl").open())}
    # "Patients carried heterozygous deletions ..." has no keyword tag but sits in the hot chunk.
    assert cands["11111111:0:2"]["kw"] == [] and cands["11111111:0:2"]["sem"] is True
    assert "11111111:0:0" not in cands                       # no soft marker: not a candidate
    assert not any("keyword only" in w for w in res["warnings"])


def test_without_corpus_or_model_the_tool_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv("AISCIENTIST_PAPERQA_PAPERS", raising=False)
    ctx = types.SimpleNamespace(model="qwen", llm_base_url="http://x/v1", tunnel_port=None,
                                workspace=tmp_path, decisions={}, llm_is_remote=False)
    assert hi.run_extract({}, ctx, log=lambda s: None)["status"] == "not_enabled"
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    ctx.model = None
    assert hi.run_extract({}, ctx, log=lambda s: None)["status"] == "not_enabled"


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


def test_judge_json_is_parsed_as_given():
    txt = '```json\n{"items":[{"gene":"CRX","mechanism_class":"haploinsufficiency","explicit":true,' \
          '"negated":false,"human_retinal":true,"phenotype":"LCA","quote":"q"},"junk"]}\n```'
    assert hi.parse_items(txt) == [{"gene": "CRX", "mechanism_class": "haploinsufficiency", "explicit": True,
                                    "negated": False, "human_retinal": True, "phenotype": "LCA", "quote": "q"}]
    assert hi.parse_items('{"items": []}') == []
    assert hi.parse_items("not json") is None
    assert hi.parse_items('{"items": "x"}') is None


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
                                "manifest": str(root / "paperqa_manifest.csv"), "max_candidates": 5})
    assert res["status"] == "ok"
    assert seen["args"]["max_candidates"] == 5 and "model" not in seen["args"]
    assert seen["ctx"].llm_base_url == "http://gpu:1/v1" and seen["ctx"].workspace == str(tmp_path / "ws")


def test_a_dead_endpoint_stops_the_pass_at_once(tmp_path, monkeypatch):
    """Every call refused (the serve job was preempted): the pass gives up after a short streak,
    reports model_unreachable instead of a hollow 'ok' with zero genes, writes no judgment lines
    for the sentences it never reached, and a rerun with a live model judges them all."""
    import urllib.error
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    calls = []

    def refused(messages):
        calls.append(1)
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(hi, "_DEAD_AFTER", 3)
    res = hi.run_extract({"concurrency": 2}, ctx, judge_fn=refused, embed_fn=None, log=lambda s: None,)
    assert res["status"] == "failed" and res["model_unreachable"] is True
    assert "stopped answering" in res["error"] and res["n_judged"] == 0
    jl = ctx.workspace / "work" / "haploinsufficiency" / "judgments.jsonl"
    assert not jl.exists() or all(json.loads(line)["error"] != "model unreachable" for line in jl.open())
    # Back with a live model: everything is judged, nothing was lost.
    res2 = hi.run_extract({"concurrency": 2}, ctx, judge_fn=_fake_model(), embed_fn=None, log=lambda s: None)
    assert res2["status"] == "ok" and res2["n_genes"] == 1 and not res2["warnings"][1:]


def test_an_http_error_is_not_a_dead_endpoint(tmp_path, monkeypatch):
    """A 4xx/5xx the server answered with is the server's problem with that request, not a dead
    port: the pass continues, the sentence is recorded as failed and retried next run."""
    import io
    import urllib.error
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    base = _fake_model()

    def bad_request(messages):
        if _target(messages) == RHO_SENT:
            raise urllib.error.HTTPError("http://x", 400, "Bad Request", {}, io.BytesIO(b""))
        return base(messages)

    monkeypatch.setattr(hi, "_DEAD_AFTER", 1)
    res = hi.run_extract({"concurrency": 2}, ctx, judge_fn=bad_request, embed_fn=None, log=lambda s: None)
    assert res["status"] == "ok" and res["n_genes"] == 1
    assert any("1 of" in w and "could not be judged" in w for w in res["warnings"])


def test_a_reply_that_is_not_a_completion_fails_its_sentence_only(tmp_path, monkeypatch):
    """A 200 with no choices (IndexError inside the client) used to escape the worker thread and
    end the whole pass. It is one failed sentence: the rest are judged and the rerun retries it."""
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    root = _corpus(tmp_path)
    ctx = _ctx(tmp_path, monkeypatch, root)
    base = _fake_model()

    def no_choices(messages):
        if _target(messages) == RHO_SENT:
            return str({"choices": []}["choices"][0])
        return base(messages)

    res = hi.run_extract({"concurrency": 2}, ctx, judge_fn=no_choices, embed_fn=None, log=lambda s: None)
    assert res["status"] == "ok" and res["n_genes"] == 1
    assert any("1 of" in w and "could not be judged" in w for w in res["warnings"])
