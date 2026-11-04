"""A list question about the RetiGene papers is a curation of the whole corpus (RCHENLAB/AiScientist
issue 46).

"What are haploinsufficient genes in RetiGene papers?" used to become a literature search over the
corpus' 12 best passages and PubMed, answered in prose. Now the console's route sends it to the corpus
curation: a plan card that says what is read and how long it takes, then the corpus scan
(``extract_haploinsufficient_genes``), two tables built from the scan's sentences (one row per gene
with its claim, one per sentence) and a chat answer written from them, with the genes the papers
argue against apart. The tables can be saved to the user's data. Offline: the model, the scan job
and the user's decisions are scripted.

The bare question, "What are haploinsufficient genes?", names no papers; it gets the same curation
(issue 20, option A), with a card that says it reads the RetiGene papers and that "search PubMed"
asks the public literature. A list question that names PubMed or the literature, and a question
about the concept, still go to the literature line.
"""
from __future__ import annotations

import asyncio
import csv
import json
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from aiscientist.agents import request_router as rr  # noqa: E402
from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway import corpus_scan  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

QUESTION = "What are haploinsufficient genes in RetiGene papers?"
BARE = "What are haploinsufficient genes?"         # names no papers (issue 20, option A)

HIT_COLUMNS = ("pmid", "page", "gene", "gene_as_written", "gene_status", "gene_in_quote", "mechanism_class",
               "explicit", "negated", "human_retinal", "phenotype", "mechanism_statement", "quote",
               "quote_verified", "cite_like", "dominance_marker", "garbled", "sentence_id", "found_by", "eligible")


def _hit(pmid, page, gene, quote, *, cls="haploinsufficiency", explicit=True, negated=False, retinal=True,
         verified=True, phenotype="retinitis pigmentosa", cite=False, status="ok"):
    eligible = cls == "haploinsufficiency" and explicit and not negated and retinal and verified and status != "missing"
    return {"pmid": pmid, "page": page, "gene": gene, "gene_as_written": gene, "gene_status": status,
            "gene_in_quote": True, "mechanism_class": cls, "explicit": explicit, "negated": negated,
            "human_retinal": retinal, "phenotype": phenotype, "mechanism_statement": "haploinsufficiency",
            "quote": quote, "quote_verified": verified, "cite_like": cite, "dominance_marker": True,
            "garbled": False, "sentence_id": f"{pmid}:{page}:{gene}", "found_by": "K", "eligible": eligible}


HITS = [
    _hit("111", 4, "PRPF31", "Haploinsufficiency of PRPF31 causes autosomal dominant retinitis pigmentosa."),
    _hit("222", 2, "PRPF31", "A heterozygous PRPF31 deletion causes RP through haploinsufficiency."),
    _hit("333", 7, "OPA1", "Most OPA1 variants cause optic atrophy by haploinsufficiency.", phenotype="ADOA"),
    _hit("444", 3, "TSPAN12", "TSPAN12 mutations in FEVR are unlikely to act by haploinsufficiency.",
         negated=True, phenotype="FEVR"),
    _hit("555", 9, "CRX", "Monoallelic CRX loss causes disease through haploinsufficiency."),
    _hit("556", 1, "CRX", "CRX haploinsufficiency is not the cause of the dominant phenotype.", negated=True),
    _hit("666", 5, "PRPH2", "Heterozygous PRPH2 variants may act through haploinsufficiency.", explicit=False),
    # not curated: a non-ocular phenotype, an unverified quote, another mechanism class
    _hit("777", 1, "GATAD2B", "GATAD2B haploinsufficiency causes intellectual disability.", retinal=False),
    _hit("888", 1, "ABCA4", "ABCA4 haploinsufficiency was invented.", verified=False),
    _hit("999", 2, "CRX", "Some CRX frameshift alleles act as dominant negatives.", cls="dominant_negative"),
]


def _write_tables(out: Path, hits=HITS) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "hits.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(HIT_COLUMNS))
        w.writeheader()
        w.writerows(hits)
    with (out / "genes.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["gene", "n_papers", "conflicting_mechanism", "flag_garbled", "flag_no_dominance_marker",
                    "pmid_1", "quote_1"])
        w.writerow(["CRX", "1", "dominant_negative", "False", "False", "555", HITS[4]["quote"]])
        w.writerow(["OPA1", "1", "", "True", "False", "333", HITS[2]["quote"]])
        w.writerow(["PRPF31", "2", "", "False", "False", "111", HITS[0]["quote"]])
    (out / "requests.csv").write_text("id,round,gene,request,n_passages\nR1,1,,x,5\n")
    return out


PAPERS = {"111": {"title": "PRPF31 and RP", "doi": "10.1/prpf31", "citation": ""},
          "444": {"title": "TSPAN12 in FEVR", "doi": "10.1/tspan12", "citation": ""}}


# --- the routing words -------------------------------------------------------------------------------

@pytest.mark.parametrize("text,named", [
    (QUESTION, True),
    ("what do retigene papers say about PRPF31", True),
    ("Which genes in the lab's corpus are haploinsufficient?", True),
    ("list the haploinsufficient genes in the corpus", True),
    ("语料库里有哪些单倍剂量不足的基因？", True),
    ("What are haploinsufficient genes?", False),
    ("Is the corpus callosum thinner in this mouse?", False),
])
def test_a_question_names_the_retigene_corpus(text, named):
    assert rr.names_corpus(text) is named


def test_pubmed_is_added_only_when_asked_for():
    assert rr.corpus_only(QUESTION)
    assert not rr.corpus_only("What do RetiGene papers and PubMed say about PRPF31?")
    assert not rr.corpus_only("Haploinsufficient genes in RetiGene papers and the published literature")
    assert not rr.corpus_only("What is known about PRPF31?")          # names no corpus at all


def test_the_curation_route_takes_list_questions_about_the_corpus_or_naming_no_source(monkeypatch):
    conn = types.SimpleNamespace()
    monkeypatch.setattr(gw_app, "_literature_corpus_on_hpc", lambda c: True)
    assert gw_app._corpus_curation(conn, QUESTION)
    assert gw_app._corpus_curation(conn, "Which RetiGene genes are haploinsufficient?")
    assert not gw_app._corpus_curation(conn, "Is PRPF31 haploinsufficient in RetiGene papers?")  # one gene
    assert not gw_app._corpus_curation(conn, "Which haploinsufficient genes from RetiGene are in my data?")
    # Issue 20, option A: the bare list question reads the RetiGene papers too.
    assert gw_app._corpus_curation(conn, BARE)
    assert gw_app._corpus_curation(conn, "Which genes are haploinsufficient in retinitis pigmentosa?")
    # ... but not one that names another source, points at the chat, or is not a list question,
    for other in ("What are haploinsufficient genes in PubMed?",
                  "Which haploinsufficient genes are reported in the published literature?",
                  "Which genes are haploinsufficient according to ClinGen?",
                  "Which of these genes are haploinsufficient?",
                  "Which haploinsufficient genes are in my data?",
                  "What is haploinsufficiency?", "How do haploinsufficient genes cause disease?"):
        assert not gw_app._corpus_curation(conn, other), other
    # ... nor in a chat with data attached or an earlier analysis, where "which genes" may mean its own.
    assert not gw_app._corpus_curation(conn, BARE, with_context=True)
    assert gw_app._corpus_curation(conn, QUESTION, with_context=True)     # naming RetiGene is enough
    monkeypatch.setattr(gw_app, "_literature_corpus_on_hpc", lambda c: False)
    assert not gw_app._corpus_curation(conn, QUESTION)               # this session cannot reach the corpus
    assert not gw_app._corpus_curation(conn, BARE)


@pytest.mark.parametrize("question", [
    BARE, "Which genes are haploinsufficient?", "What genes are haploinsufficient in retinal disease?",
    "List all haploinsufficient genes", "Haploinsufficient genes in RP?", "Give examples of haploinsufficient genes",
    "Can you list the haploinsufficient genes?", "How many genes are haploinsufficient in IRD?",
    "Are there haploinsufficient genes in inherited retinal disease?",
    "Which genes cause retinal disease through haploinsufficiency?", "What are haplo-insufficient genes?",
    "What are the known haploinsufficient retinal disease genes?", "I need a list of haploinsufficient genes",
    "哪些基因是单倍剂量不足的？", "单倍剂量不足的基因有哪些？", "列出所有单倍剂量不足的基因",
])
def test_a_plain_request_for_the_genes_is_a_list_question_when_read_strictly(question):
    assert corpus_scan.asks_for_gene_list(question, strict=True)


@pytest.mark.parametrize("question", [
    # The concept, one gene, a mechanism: prose from the literature line. The loose reading, kept for
    # questions that name the RetiGene papers, takes most of these as list questions.
    "What is haploinsufficiency?", "What is a haploinsufficient gene?", "Is PRPF31 haploinsufficient?",
    "Is PRPF31 haploinsufficient in all patients?", "Is PRPF31 one of the haploinsufficient genes?",
    "Which mechanism underlies PRPF31 haploinsufficiency?", "How do haploinsufficient genes cause disease?",
    "Why are some genes haploinsufficient?", "What makes genes haploinsufficient?",
    "What are the effects of haploinsufficient genes?", "Tell me about haploinsufficient genes",
    "How do we find haploinsufficient genes?", "Which is worse, haploinsufficiency or a dominant negative?",
    "What genes cause Stargardt disease?", "PRPF31是单倍剂量不足的基因吗？", "单倍剂量不足是什么？",
])
def test_a_question_about_the_concept_or_one_gene_is_no_list_question_when_read_strictly(question):
    assert not corpus_scan.asks_for_gene_list(question, strict=True)


@pytest.mark.parametrize("text,none", [
    (BARE, True), ("哪些基因是单倍剂量不足的？", True),
    (QUESTION, False),                                                      # the RetiGene papers
    ("What are haploinsufficient genes in PubMed?", False),
    ("Which haploinsufficient genes are reported in the literature?", False),
    ("Which haploinsufficient genes do papers report?", False),
    ("Which genes are haploinsufficient according to ClinGen?", False),
    ("文献里哪些基因是单倍剂量不足的？", False),
    ("Which haploinsufficient genes are in my data?", False),                # the user's data
    ("Which of these genes are haploinsufficient?", False),                 # something in the chat
    ("Which genes in the list above are haploinsufficient?", False),
])
def test_a_question_names_no_source(text, none):
    assert rr.names_no_source(text) is none


def test_the_eye_server_copy_alone_makes_no_curation_and_is_not_loaded_to_decide(monkeypatch):
    # The curation reads the corpus on HPC3; with only the Eye Server copy (local_corpus) the
    # question goes to the literature line, and deciding that loads no embedding model.
    from aiscientist.gateway import local_corpus
    monkeypatch.setattr(local_corpus, "make_local_tool",
                        lambda: (_ for _ in ()).throw(AssertionError("model loaded to route")))
    monkeypatch.setattr(gw_app, "_literature_corpus_on_hpc", lambda c: False)
    assert not gw_app._corpus_curation(types.SimpleNamespace(), QUESTION)


# --- the tables ----------------------------------------------------------------------------------------

def test_the_gene_table_has_one_row_per_gene_with_its_claim_strongest_first(tmp_path):
    rows, evidence = corpus_scan.curation_tables(_write_tables(tmp_path / "t"), PAPERS)
    assert [(r["gene"], r["claim"]) for r in rows] == [
        ("PRPF31", "haploinsufficient"), ("OPA1", "haploinsufficient"),     # two papers before one
        ("CRX", "contested"), ("PRPH2", "suggested"), ("TSPAN12", "argued against")]
    prpf31 = rows[0]
    assert prpf31["quote"] == HITS[0]["quote"] and prpf31["pmid"] == "111" and prpf31["page"] == "4"
    assert prpf31["title"] == "PRPF31 and RP" and prpf31["doi"] == "10.1/prpf31"
    assert prpf31["explicit"] == "yes" and prpf31["negated"] == "no" and prpf31["papers_stating"] == 2
    assert prpf31["pmids_stating"] == "111|222" and prpf31["phenotype"] == "retinitis pigmentosa"
    assert rows[1]["review_flags"] == "garbled"                     # the scan's own flag for OPA1
    crx = rows[2]
    assert (crx["papers_stating"], crx["papers_against"], crx["pmids_against"]) == (1, 1, "556")
    assert crx["conflicting_mechanism"] == "dominant_negative"
    tspan = rows[4]
    assert tspan["negated"] == "yes" and tspan["quote"] == HITS[3]["quote"] and tspan["doi"] == "10.1/tspan12"
    assert rows[3]["explicit"] == "no"
    # The non-ocular, the unverified and the other mechanism's sentences are not curated.
    assert {r["gene"] for r in rows}.isdisjoint({"GATAD2B", "ABCA4"})
    # One evidence row per sentence that bears on a claim, in the genes' order.
    assert [(e["gene"], e["statement"]) for e in evidence] == [
        ("PRPF31", "states"), ("PRPF31", "states"), ("OPA1", "states"), ("CRX", "states"),
        ("CRX", "argues against"), ("PRPH2", "hedged"), ("TSPAN12", "argues against")]
    assert all(e["quote"] and e["pmid"] and e["page"] for e in evidence)


def test_an_animal_model_against_a_stated_gene_makes_it_contested_never_argued_against(tmp_path):
    # The kept Haiku scan of 2026-10-20: one paper proposes TSPAN12 haploinsufficiency in FEVR, another
    # notes that heterozygous knockout mice vascularize normally. The mouse sentence is not about a human
    # retinal disease, so it is no gene of its own, but it is the papers arguing against a listed gene.
    hits = [
        _hit("101", 4, "TSPAN12", "Haploinsufficiency of TSPAN12 was proposed as the cause of FEVR.",
             phenotype="FEVR"),
        _hit("102", 18, "TSPAN12", "Mice with heterozygous knockout of Tspan12 exhibit normal vascularization.",
             negated=True, retinal=False),
        _hit("103", 2, "SOX2", "SOX2 haploinsufficiency may explain anophthalmia.", explicit=False),
        _hit("104", 2, "SOX2", "Mice heterozygous for a Sox2 null mutation show no eye phenotype.",
             negated=True, retinal=False),
        # Nothing but such a sentence: not curated. A reference entry: never counter evidence.
        _hit("105", 13, "ELOVL4", "Elovl4 haploinsufficiency does not cause retinal degeneration in mice.",
             negated=True, retinal=False),
        _hit("106", 9, "TSPAN12", "Smith A. Tspan12 haploinsufficiency is not enough in mice.",
             negated=True, retinal=False, cite=True),
    ]
    rows, evidence = corpus_scan.curation_tables(_write_tables(tmp_path / "t", hits=hits))
    assert [(r["gene"], r["claim"]) for r in rows] == [("TSPAN12", "contested"), ("SOX2", "suggested")]
    tspan = rows[0]
    assert tspan["quote"] == hits[0]["quote"] and (tspan["papers_stating"], tspan["papers_against"]) == (1, 1)
    assert (tspan["quote_against"], tspan["pmid_against"], tspan["page_against"]) == (hits[1]["quote"], "102", "18")
    assert tspan["against_context"] == "animal model or another disease" and tspan["pmids_against"] == "102"
    assert corpus_scan.AGAINST_ELSEWHERE_FLAG in tspan["review_flags"].split("|")
    # A hedged gene stays suggested: a heterozygous mouse without the phenotype is no human argument.
    assert rows[1]["papers_against"] == 1 and corpus_scan.AGAINST_ELSEWHERE_FLAG in rows[1]["review_flags"]
    assert [(e["gene"], e["statement"]) for e in evidence] == [
        ("TSPAN12", "states"), ("TSPAN12", "argues against elsewhere"),
        ("SOX2", "hedged"), ("SOX2", "argues against elsewhere")]
    # The chat lists TSPAN12 as contested, both sides quoted, never among the haploinsufficient genes.
    md = "\n".join(gw_app._curation_md(rows))
    assert "**Haploinsufficient" not in md and md.startswith("**Contested — 1 gene**")
    assert ("| TSPAN12 | “Haploinsufficiency of TSPAN12 was proposed as the cause of FEVR.” "
            "([PMID 101](https://pubmed.ncbi.nlm.nih.gov/101/), p. 4) | “Mice with heterozygous knockout") in md
    assert "(102/), p. 18) — in an animal model or another disease | 1 / 1 |" in md.replace(
        "[PMID 102](https://pubmed.ncbi.nlm.nih.gov/", "(")


def test_the_tables_are_written_with_their_columns(tmp_path):
    out = tmp_path / "curation"
    cur = corpus_scan.write_curation(_write_tables(tmp_path / "t"), out, PAPERS)
    assert cur["files"] == [corpus_scan.CURATION_GENES, corpus_scan.CURATION_EVIDENCE]
    assert cur["counts"] == {"haploinsufficient": 2, "contested": 1, "suggested": 1, "argued against": 1}
    with (out / corpus_scan.CURATION_GENES).open(newline="") as fh:
        genes = list(csv.DictReader(fh))
    assert tuple(genes[0]) == corpus_scan.GENE_TABLE_COLUMNS and len(genes) == 5
    with (out / corpus_scan.CURATION_EVIDENCE).open(newline="") as fh:
        assert len(list(csv.DictReader(fh))) == cur["n_sentences"] == 7
    # a scan that found nothing writes no tables
    empty = corpus_scan.write_curation(_write_tables(tmp_path / "e", hits=[]), tmp_path / "none")
    assert empty["rows"] == [] and empty["files"] == [] and not (tmp_path / "none").exists()


def test_the_papers_come_from_the_corpus_manifest():
    text = ("doc_id,docname,file_location,title,citation,doi\n"
            "111,,,PRPF31 and RP,Lee A. PRPF31 and RP. IOVS (2020).,10.1/a\n"
            ",PMID222,,Second,Kim B. Second. DOI: 10.1/b.,\n"
            ",,/dfs/papers/333.pdf,Third,,\n")
    papers = corpus_scan.papers_from_manifest(text)
    assert papers["111"] == {"title": "PRPF31 and RP", "doi": "10.1/a", "citation": "Lee A. PRPF31 and RP. IOVS (2020)."}
    assert papers["222"]["doi"] == "10.1/b" and papers["333"]["title"] == "Third"
    assert corpus_scan.papers_from_manifest("") == {}


# --- the route, end to end -----------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    yield
    gw_app.CONNECTIONS.clear()
    gw_app._CORPUS_SCANS_RUNNING.clear()


CORPUS_ASKED = rr.CORPUS_ASK_SYSTEM[:60]


class _Model:
    """The session's model: it may screen the message and say whether it asks for one fact or a
    list (``corpus_ask``), and must not be asked to route it to literature or analysis."""

    def __init__(self, verdict='{"kind": "list", "property": "haploinsufficient in retinal disease", '
                               '"known": "haploinsufficiency", "confidence": 0.95}'):
        self.asked: list[str] = []
        self.verdict = verdict

    def __call__(self, messages, *, role=None):
        system = messages[0]["content"]
        if system == gw_app._NEW_STUDY_ROUTER_SYS:
            return '{"intent": "study", "confidence": 0.95}'
        self.asked.append(system[:60])
        if system[:60] == CORPUS_ASKED:
            return self.verdict
        if system == rr.ROUTER_SYSTEM:
            raise AssertionError("a curation question must not be routed by the model")
        raise AssertionError(f"unexpected model call: {system[:80]}")


_RESULT = {"status": "ok", "n_papers": 1739, "n_passages": 1531, "n_requests": 16, "n_gene_requests": 3,
           "n_keyword_passages": 402, "n_hits_eligible": 4, "n_genes": 3, "genes": [],
           "genes_flagged_for_review": ["OPA1"], "definition": "one copy is not enough", "warnings": []}


class _ScanJob:
    """The scan's HPC3 job: writes the scan's tables into the run's artifacts and returns its result."""

    def __init__(self, local_workspace: Path):
        self.local_workspace, self.calls, self.notify = local_workspace, [], None

    def run_tool(self, tool, args, ctx):
        self.calls.append(tool)
        _write_tables(self.local_workspace / "artifacts" / corpus_scan.OUT_SUBDIR)
        return dict(_RESULT)


def _conn(tmp_path):
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = object()
    conn.selected_model = "qwen"
    conn.pushed = []
    conn.push = lambda payload, **kw: conn.pushed.append(payload)
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


def _wire(monkeypatch, decisions, *, papers=PAPERS):
    model = _Model()
    monkeypatch.setattr(gw_app, "_lab_llm",
                        lambda conn: types.SimpleNamespace(complete_fn=model, scientist_chat=None, model="qwen"))
    monkeypatch.setattr(gw_app, "_literature_corpus_available", lambda c: True)
    monkeypatch.setattr(gw_app, "_literature_corpus_on_hpc", lambda c: True)
    monkeypatch.setattr(gw_app, "_corpus_papers", lambda c: papers)
    jobs: list[_ScanJob] = []

    def build(c, run_id=None, own_gpu=False):
        jobs.append(_ScanJob(c.workspace / run_id))
        return jobs[-1]

    monkeypatch.setattr(gw_app, "_build_literature_executor", build)
    monkeypatch.setattr(gw_app._tools_api, "search_literature",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("PubMed searched")))
    shown: list[str] = []
    script = list(decisions)

    def review(conn, emit, step, stage="literature"):
        shown.append((stage, step))
        conn.push({"type": "plan_prompt", "agenda": [step]})
        return script.pop(0)

    monkeypatch.setattr(gw_app, "_literature_plan_review", review)
    return types.SimpleNamespace(model=model, jobs=jobs, shown=shown)


def _dispatch(conn, question=QUESTION):
    req = gw_app.LabRequest(connection_id=conn.id, conversation_id="conv-1", question=question, route="auto")
    asyncio.run(gw_app._dispatch_lab(conn, gw_app._fixed_run_options(req)))


def test_the_issue_question_gets_a_curation_plan_then_the_tables_and_an_answer_from_them(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "approve"}])
    _dispatch(conn)

    # The plan: one curation step over the RetiGene corpus, no PubMed, with the time it takes.
    ((stage, step),) = w.shown
    assert stage == "corpus" and step.startswith("**RetiGene curation**")
    assert "PubMed is not searched" in step and "about 2 hours on a GPU of its own" in step
    # The model only said that it asks for a list (of a curation built in), and routed nothing else.
    assert corpus_scan.CURATION_GENES in step and w.model.asked == [CORPUS_ASKED]
    assert [j.calls for j in w.jobs] == [["extract_haploinsufficient_genes"]]

    (run_dir,) = [p for p in conn.workspace.iterdir() if p.is_dir()]
    art = run_dir / "artifacts"
    assert (art / "curation" / corpus_scan.CURATION_GENES).is_file()
    assert (art / "curation" / corpus_scan.CURATION_EVIDENCE).is_file()
    answer = next(p for p in conn.pushed if p.get("type") == "chat_token")["token"]
    assert answer.startswith("**Haploinsufficient genes in the RetiGene corpus: 3**")
    assert "1 of them contested; 1 argued against; 1 only suggested" in answer
    # Strongest evidence first, every row cited with its paper and page.
    assert answer.index("| PRPF31 |") < answer.index("| OPA1 |")
    assert "[PMID 111](https://pubmed.ncbi.nlm.nih.gov/111/), p. 4 + 1 more" in answer
    # The gene the papers argue against is in its own section, after the list, never in it.
    listed, rest = answer.split("**Argued against", 1)
    assert "TSPAN12" not in listed and "| TSPAN12 | “TSPAN12 mutations in FEVR" in rest
    assert "**Only suggested — 1 gene**" in answer and "**PRPH2**" in answer
    assert "Read these first" in answer and "OPA1" in answer
    arts = next(p for p in conn.pushed if p.get("type") == "artifacts")
    names = {a["name"] for a in arts["items"]}
    assert {corpus_scan.CURATION_GENES, corpus_scan.CURATION_EVIDENCE, "genes.csv", "hits.csv"} <= names
    assert corpus_scan.CURATION_POINTER not in names                # the save record is not a result file
    pointer = json.loads((run_dir / corpus_scan.CURATION_POINTER).read_text())
    assert pointer["n_genes"] == 3 and [f["name"] for f in pointer["files"]] == [
        corpus_scan.CURATION_GENES, corpus_scan.CURATION_EVIDENCE]
    assert gw_app._TABLES_NAME_RE.fullmatch(pointer["name"])
    # The curation tables are the run's one "Save tables to my data", not also the scan's genes.csv.
    assert not (run_dir / gw_app._LITERATURE_RESULT_POINTER).exists()
    assert not (run_dir / "result" / "literature").exists()
    assert [p.get("type") for p in conn.pushed][-1] == "chat_done"


def test_the_bare_question_gets_the_curation_and_its_card_says_it_names_no_papers(tmp_path, monkeypatch):
    """Issue 20, option A: "What are haploinsufficient genes?" names no papers. It gets the RetiGene
    curation, and its plan card says that the question names no papers, that this reads the RetiGene
    papers, and that "search PubMed" asks the public literature instead."""
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "approve"}])
    _dispatch(conn, BARE)

    ((stage, step),) = w.shown
    assert stage == "corpus" and step.startswith(
        "**RetiGene curation** — The question names no papers, so this reads the lab's RetiGene papers; "
        "say “search PubMed” for an answer from the public literature instead. Read the whole RetiGene corpus")
    assert f"to answer: “{BARE}”" in step and "PubMed is not searched" in step
    assert w.model.asked == []                                       # the model routed nothing
    progress = [str(p.get("text")) for p in conn.pushed if p.get("type") == "lab_progress"]
    assert any("names no papers, so the plan reads the lab's RetiGene papers" in t and "“search PubMed”" in t
               for t in progress)
    assert any(e.get("stage") == "route" and "(the question names no papers)" in e["message"]
               for e in conn.log if e.get("type") == "event")
    # Then the same curation as for the question that names the RetiGene papers.
    assert [j.calls for j in w.jobs] == [["extract_haploinsufficient_genes"]]
    answer = next(p for p in conn.pushed if p.get("type") == "chat_token")["token"]
    assert answer.startswith("**Haploinsufficient genes in the RetiGene corpus: 3**")
    assert "| PRPF31 |" in answer and "[PMID 111](https://pubmed.ncbi.nlm.nih.gov/111/), p. 4" in answer
    assert [p.get("type") for p in conn.pushed][-1] == "chat_done"


def test_the_question_that_names_the_retigene_papers_keeps_its_card(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "cancel"}])
    _dispatch(conn, QUESTION)
    ((stage, step),) = w.shown
    assert stage == "corpus" and step == corpus_scan.curation_plan_step(
        QUESTION, gw_app._curation_time_words(conn, None, None))
    assert "names no papers" not in step and "search PubMed" not in step
    progress = [str(p.get("text")) for p in conn.pushed if p.get("type") == "lab_progress"]
    assert any(t.startswith("🧬 This asks for the genes the RetiGene papers report") for t in progress)
    assert not any("names no papers" in t for t in progress)


def _to_literature(monkeypatch):
    """The model's router, scripted to answer "literature", and the literature line, recorded."""
    seen: list[str] = []

    def route(c, req, prior=""):
        seen.append(f"routed: {req.question}")
        return rr.Route(rr.LITERATURE, 0.9, "papers", "model")

    async def literature(c, req, r=None, **kw):
        seen.append(f"literature: {req.question}")

    monkeypatch.setattr(gw_app, "_route_request", route)
    monkeypatch.setattr(gw_app, "_run_literature", literature)
    return seen


@pytest.mark.parametrize("question", [
    "What are haploinsufficient genes in PubMed?",                  # names PubMed: the public literature
    "Which haploinsufficient genes are reported in the published literature?",
    "What is haploinsufficiency?",                                   # no list question: prose
    "How do haploinsufficient genes cause disease?",
])
def test_a_list_question_naming_pubmed_or_a_question_about_the_concept_goes_to_the_literature_line(
        tmp_path, monkeypatch, question):
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [])
    seen = _to_literature(monkeypatch)
    _dispatch(conn, question)
    assert w.shown == [] and w.jobs == []                            # no curation plan, no scan
    assert seen == [f"routed: {question}", f"literature: {question}"]
    assert not any(e.get("stage") == "route" and "curation" in str(e.get("message"))
                   for e in conn.log if e.get("type") == "event")
    # The literature line searches PubMed for it: it does not name the RetiGene papers alone.
    assert not rr.corpus_only(question)


def test_the_bare_question_in_a_chat_with_data_attached_goes_to_the_literature_line(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [])
    seen = _to_literature(monkeypatch)
    req = gw_app.LabRequest(connection_id=conn.id, conversation_id="conv-1", question=BARE, route="auto",
                            dataset_path="/data/retina.h5ad")
    asyncio.run(gw_app._dispatch_lab(conn, gw_app._fixed_run_options(req)))
    assert w.shown == [] and w.jobs == [] and seen == [f"routed: {BARE}", f"literature: {BARE}"]


def test_search_pubmed_on_the_bare_questions_plan_asks_the_public_literature(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "feedback", "feedback": "search PubMed"}])
    seen = _to_literature(monkeypatch)
    _dispatch(conn, BARE)
    combined = f"{BARE}\n\nsearch PubMed"
    assert [stage for stage, _ in w.shown] == ["corpus"] and w.jobs == []
    assert seen == [f"routed: {combined}", f"literature: {combined}"]
    assert not rr.corpus_only(combined)                             # the literature line searches PubMed
    assert not gw_app._corpus_curation(conn, combined)


@pytest.mark.parametrize("question", [QUESTION, BARE])
def test_what_the_user_sees_names_retigene_never_the_corpus_alone(tmp_path, monkeypatch, question):
    """Jin (issue 46): users do not know that "the corpus" is the RetiGene papers. The plan card, the
    chat's messages, the feed and the answer say "RetiGene" wherever they say "corpus"."""
    import re
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "approve"}])
    _dispatch(conn, question)
    seen = [step for _stage, step in w.shown]
    seen += [str(p.get(k)) for p in conn.pushed for k in ("text", "token", "message") if p.get(k)]
    seen += [str(e.get("message")) for e in conn.log if e.get("type") == "event"]
    bare = [m.group(0) + " … " + text[max(0, m.start() - 60):m.end() + 20]
            for text in seen for m in re.finditer(r"(?<!RetiGene )\bcorpus\b", text, re.IGNORECASE)]
    assert bare == []
    assert any("RetiGene" in text for text in seen)


def test_a_cancelled_curation_plan_reads_nothing(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "cancel"}])
    _dispatch(conn)
    assert w.jobs == [] and not any(p.get("type") == "chat_token" for p in conn.pushed)
    assert any("Plan cancelled — nothing was read" in str(p.get("text")) for p in conn.pushed)


def test_a_note_on_the_curation_plan_is_planned_again_by_the_router(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "feedback", "feedback": "also search PubMed"}])
    routed: list[str] = []

    def route(c, req, prior=""):
        routed.append(req.question)
        return rr.Route(rr.LITERATURE, 0.9, "papers", "model")

    async def literature(c, req, r=None, **kw):
        routed.append("literature")

    monkeypatch.setattr(gw_app, "_route_request", route)
    monkeypatch.setattr(gw_app, "_run_literature", literature)
    _dispatch(conn)
    assert w.jobs == []
    assert routed == [f"{QUESTION}\n\nalso search PubMed", "literature"]


def test_a_note_asking_for_more_than_a_fixed_number_keeps_the_curation(tmp_path, monkeypatch):
    """The curation reads every RetiGene paper, so a note asking not to be limited to a number of
    papers asks for what it already does. Re-planning it through the router answered a request for
    MORE coverage with a bounded 12-passage search (RCHENLAB/AiScientist issues 59 and 60)."""
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "feedback", "feedback": "without limiting the answer to 12 papers"},
                            {"action": "approve"}])

    def never(*a, **k):
        raise AssertionError("re-planned by the router")

    monkeypatch.setattr(gw_app, "_route_request", never)
    _dispatch(conn)
    assert [j.calls for j in w.jobs] == [["extract_haploinsufficient_genes"]]
    assert [stage for stage, _ in w.shown] == ["corpus", "corpus"]
    assert any("already reads every RetiGene paper" in str(p.get("text")) for p in conn.pushed)


def test_a_note_lifting_the_limit_to_the_retigene_papers_is_planned_again(tmp_path, monkeypatch):
    """"Don't limit it to RetiGene" asks for other sources, not for more of the RetiGene papers: the
    curation must not keep its RetiGene-only plan and call the note covered."""
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "feedback", "feedback": "Don't limit it to RetiGene"}])
    routed: list[str] = []

    def route(c, req, prior=""):
        routed.append(req.question)
        return rr.Route(rr.LITERATURE, 0.9, "papers", "model")

    async def literature(c, req, r=None, **kw):
        routed.append("literature")

    monkeypatch.setattr(gw_app, "_route_request", route)
    monkeypatch.setattr(gw_app, "_run_literature", literature)
    _dispatch(conn)
    assert w.jobs == [] and routed == [f"{QUESTION}\n\nDon't limit it to RetiGene", "literature"]
    assert not rr.corpus_only(routed[0])                         # the literature line searches PubMed
    assert not any("already reads every RetiGene paper" in str(p.get("text")) for p in conn.pushed)


class _RoutesToLiterature:
    """The session's model for a question that does NOT name the RetiGene papers: it routes to the
    literature line, and is asked nothing else because the note hands the request on before the
    plan is approved."""

    def __call__(self, messages, *, role=None):
        system = messages[0]["content"]
        if system == gw_app._NEW_STUDY_ROUTER_SYS:
            return '{"intent": "study", "confidence": 0.95}'
        if system == rr.ROUTER_SYSTEM:
            return '{"route": "literature", "confidence": 0.9, "reason": "published evidence"}'
        raise AssertionError(f"unexpected model call: {system[:80]}")


def test_a_note_that_turns_a_literature_question_into_a_list_question_gets_the_curation(tmp_path, monkeypatch):
    """Run 73f1758c07de (RCHENLAB/AiScientist issue 60). The question that opened the run did not
    name the RetiGene papers, so it was routed to the literature line — correctly. A note then made
    it "according to the RetiGene papers, without limiting the answer to 12 papers", which is exactly
    what the curation is for; but the curation was only ever considered for the opening message, so
    the request stayed in the literature line and the user got 12 passages from 12 papers. The note
    must reach the curation. The opening question names the published literature, as the run's did
    ("gene evidence in published retinal disease literature"): a bare gene-list question would get
    the curation on the opening message already (issue 20)."""
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "feedback",
                             "feedback": "I mean according to the RetiGene papers, and do not limit "
                                         "the answer to 12 papers"},
                            {"action": "approve"}])
    monkeypatch.setattr(gw_app, "_lab_llm",
                        lambda conn: types.SimpleNamespace(complete_fn=_RoutesToLiterature(),
                                                           scientist_chat=None, model="qwen"))
    opening = "What are haploinsufficient genes in the published retinal disease literature?"
    assert not gw_app._corpus_curation(conn, opening)
    _dispatch(conn, opening)

    # the literature plan first, then the curation's own plan card, then the whole corpus is read
    assert [stage for stage, _ in w.shown] == ["literature", "corpus"]
    assert w.shown[1][1].startswith("**RetiGene curation**")
    assert [j.calls for j in w.jobs] == [["extract_haploinsufficient_genes"]]
    assert any("becomes a curation of all of them" in str(p.get("token")) for p in conn.pushed)
    answer = [p for p in conn.pushed if p.get("type") == "chat_token"][-1]["token"]
    assert answer.startswith("**Haploinsufficient genes in the RetiGene corpus: 3**")


def test_a_request_cannot_bounce_between_the_curation_and_the_literature_line(tmp_path, monkeypatch):
    """The curation is reconsidered once per message. A note that sends a curation request to the
    literature line must not be sent straight back, however the question then reads."""
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "feedback", "feedback": "also search PubMed"}])
    lab: list[str] = []

    def route(c, req, prior=""):
        return rr.Route(rr.LITERATURE, 0.9, "papers", "model")

    async def literature(c, req, r=None, **kw):
        return QUESTION                       # qualifies for the curation all over again

    async def fake_lab(c, req, **kw):
        lab.append(req.question)

    monkeypatch.setattr(gw_app, "_route_request", route)
    monkeypatch.setattr(gw_app, "_run_literature", literature)
    monkeypatch.setattr(gw_app, "_run_lab", fake_lab)
    _dispatch(conn)
    assert w.jobs == []                       # the curation did not run a second time
    assert lab == [QUESTION]


# --- the real literature line between the two (review of PR #135 after #137) ------------------------

PUBLISHED = "What are haploinsufficient genes in the published retinal disease literature?"


def _real_literature_line(monkeypatch):
    """The session's model routes to the literature line, its rewrite of the question keeps the
    words, and the lab is recorded: the literature line itself is NOT replaced."""
    monkeypatch.setattr(gw_app, "_lab_llm",
                        lambda conn: types.SimpleNamespace(complete_fn=_RoutesToLiterature(),
                                                           scientist_chat=None, model="qwen"))
    monkeypatch.setattr(gw_app, "_revise_literature_question", lambda conn, q, note: f"{q} {note}")
    lab: list[str] = []

    async def fake_lab(c, req, **kw):
        lab.append(req.question)

    monkeypatch.setattr(gw_app, "_run_lab", fake_lab)
    return lab


def test_a_note_on_the_literature_plan_after_the_curation_was_turned_down_stays_there(tmp_path, monkeypatch):
    """The curation is offered once per message. After the user turned it down ("also search
    PubMed"), the literature line's question still names the RetiGene papers and asks for the
    genes, so ANY later note made it qualify again: the line said "this becomes a curation" and
    handed the request back, and the dispatcher, which had already offered the curation, gave it to
    the lab as an analysis."""
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "feedback", "feedback": "also search PubMed"},
                            {"action": "feedback", "feedback": "focus on mouse studies"},
                            {"action": "cancel"}])
    lab = _real_literature_line(monkeypatch)
    _dispatch(conn, QUESTION)
    assert [stage for stage, _ in w.shown] == ["corpus", "literature", "literature"]
    assert w.jobs == [] and lab == []
    assert not any("becomes a curation" in str(p.get("token")) for p in conn.pushed)
    assert "PubMed is not searched" not in w.shown[2][1]          # the sources the user asked for


def test_a_note_on_the_literature_plan_keeps_the_rule_for_a_chat_with_data(tmp_path, monkeypatch):
    """Issue 20: the bare question goes to the literature line in a chat with data attached, where
    "which genes" may mean the user's own. A note on that plan must not take it to the curation
    after all: the note loop asked ``_corpus_curation`` without the chat's context."""
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "feedback", "feedback": "list every one of them"},
                            {"action": "cancel"}])
    lab = _real_literature_line(monkeypatch)
    req = gw_app.LabRequest(connection_id=conn.id, conversation_id="conv-1", question=BARE, route="auto",
                            dataset_path="/data/retina.h5ad")
    asyncio.run(gw_app._dispatch_lab(conn, gw_app._fixed_run_options(req)))
    assert [stage for stage, _ in w.shown] == ["literature", "literature"]
    assert w.jobs == [] and lab == []
    assert not any("becomes a curation" in str(p.get("token")) for p in conn.pushed)


def test_a_note_on_a_curation_reached_from_the_literature_plan_is_routed_again(tmp_path, monkeypatch):
    """Literature plan → a note that names the RetiGene papers → the curation's card → "also search
    PubMed". On the opening message's curation that note is routed; here it went straight to the
    lab, which planned an analysis of a literature question."""
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "feedback", "feedback": "I mean according to the RetiGene papers"},
                            {"action": "feedback", "feedback": "also search PubMed"},
                            {"action": "cancel"}])
    lab = _real_literature_line(monkeypatch)
    _dispatch(conn, PUBLISHED)
    assert [stage for stage, _ in w.shown] == ["literature", "corpus", "literature"]
    assert w.jobs == [] and lab == []
    assert "PubMed is not searched" not in w.shown[2][1]


def test_the_curation_a_literature_note_leads_to_does_not_take_another_chats_run(tmp_path, monkeypatch):
    """The literature line ends its run before it hands the request back. The curation then took
    ``conn.active_run``, which is by then the newest live run of the session: another chat's run
    waiting at its plan card, which it re-bound, ran in and ended."""
    conn = _conn(tmp_path)
    other = gw_app.RunState(conn, conversation_id="conv-other")
    conn.live_runs.append(other)                      # another chat, waiting at its plan card
    w = _wire(monkeypatch, [{"action": "feedback", "feedback": "I mean according to the RetiGene papers"},
                            {"action": "approve"}])
    _real_literature_line(monkeypatch)
    _dispatch(conn, PUBLISHED)
    assert [j.calls for j in w.jobs] == [["extract_haploinsufficient_genes"]]
    assert other in conn.live_runs and other.run_id is None and other.kind == "research"


def test_a_kept_scan_is_planned_as_instant_and_needs_no_job(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    tables = _write_tables(tmp_path / "kept" / corpus_scan.OUT_SUBDIR)
    stored = corpus_scan.store("k46", _RESULT, tables, {"model": "judge"})
    monkeypatch.setattr(gw_app, "_corpus_scan_identity", lambda c: ("k46", {}))
    w = _wire(monkeypatch, [{"action": "approve"}])
    _dispatch(conn)
    ((_, step),) = w.shown
    assert f"at once, from the scan of the RetiGene papers kept on {stored.meta['stored_at'][:10]}" in step
    assert w.jobs == []
    answer = next(p for p in conn.pushed if p.get("type") == "chat_token")["token"]
    assert "kept because the RetiGene papers have not changed since" in answer and "| PRPF31 |" in answer


def test_on_an_api_key_the_plan_says_the_model_the_time_and_the_cap(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(gw_app, "_corpus_scan_api", lambda c: {"model": "claude-haiku-5-5"})
    monkeypatch.setattr(gw_app, "_corpus_scan_cost_cap", lambda c: (20.0, "the $20.00 per-scan ceiling"))
    words = gw_app._curation_time_words(conn, None, {"model": "claude-haiku-5-5"})
    assert "with your API model claude-haiku-5-5, on your key" in words and "about 10 minutes" in words
    assert "it stops at $20.00 (the $20.00 per-scan ceiling)" in words and "larger model" not in words
    assert "a larger model takes longer" in gw_app._curation_time_words(conn, None, {"model": "claude-sonnet-5-5"})

    def spent(c):
        raise gw_app.GatewayError("Your API budget for this cycle is used up ($9.00 spent).", stage="corpus")

    monkeypatch.setattr(gw_app, "_corpus_scan_cost_cap", spent)
    assert "budget for this cycle is used up" in gw_app._curation_time_words(conn, None, {"model": "m"})


# --- saving the tables to the user's data ----------------------------------------------------------------

def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app)


def _seed_curation(root: Path, owner: str, run_id: str) -> Path:
    folder = root / owner / run_id / "artifacts" / "curation"
    folder.mkdir(parents=True)
    (folder / corpus_scan.CURATION_GENES).write_text("gene,claim\nPRPF31,haploinsufficient\n")
    (folder / corpus_scan.CURATION_EVIDENCE).write_text("gene,quote\nPRPF31,One copy is not enough.\n")
    conn = types.SimpleNamespace(workspace=root / owner)
    gw_app._point_at_curation_tables(conn, run_id, folder.parent, {
        "files": [corpus_scan.CURATION_GENES, corpus_scan.CURATION_EVIDENCE],
        "counts": {"haploinsufficient": 1, "contested": 0}})
    return folder


def test_the_tables_are_listed_and_saved_to_the_users_data_as_a_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: types.SimpleNamespace(username="yijus12", id=5))
    recorded: list = []
    monkeypatch.setattr(gw_app.auth_routes, "dataset_path_recorded", lambda uid, path: False)
    monkeypatch.setattr(gw_app.auth_routes, "record_dataset", lambda *a, **kw: recorded.append((a, kw)) or 46)
    folder = _seed_curation(tmp_path / "runs", "yijus12", "cur123")

    (row,) = _client().get("/api/result-datasets").json()["result_datasets"]
    assert row["run_id"] == "cur123" and row["tables"]["n_genes"] == 1 and row["tables"]["saved_to"] is None
    assert row["tables"]["files"] == [corpus_scan.CURATION_GENES, corpus_scan.CURATION_EVIDENCE]

    r = _client().post("/api/result-datasets/cur123/save?what=tables")
    assert r.status_code == 200 and r.json()["dataset_id"] == 46 and r.json()["kind"] == "folder"
    dest = Path(r.json()["path"])
    assert dest.is_relative_to(tmp_path / "data")                     # the managed dataset folder
    assert (dest / corpus_scan.CURATION_GENES).read_text().startswith("gene,claim")
    assert (folder / corpus_scan.CURATION_GENES).is_file()           # copied: still in the run's results
    (args, kw), = recorded
    assert args[0] == 5 and args[2] == str(dest) and args[4] == "folder" and kw["source"] == "result"
    assert {f["path"] for f in kw["files"]} == {corpus_scan.CURATION_GENES, corpus_scan.CURATION_EVIDENCE}
    assert _client().post("/api/result-datasets/cur123/save?what=tables").json()["already"] is True
    (row,) = _client().get("/api/result-datasets").json()["result_datasets"]
    assert row["tables"]["saved_to"] == str(dest)


def test_saving_tables_is_confined_to_the_callers_own_runs_and_their_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    _seed_curation(tmp_path / "runs", "victim", "run9")
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: types.SimpleNamespace(username="attacker", id=7))
    assert _client().post("/api/result-datasets/run9/save?what=tables").status_code == 404
    # a record whose path leads out of the run's results is refused, not followed
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: types.SimpleNamespace(username="victim", id=8))
    pointer = tmp_path / "runs" / "victim" / "run9" / corpus_scan.CURATION_POINTER
    rec = json.loads(pointer.read_text())
    pointer.write_text(json.dumps({**rec, "path": str(tmp_path)}))
    assert _client().post("/api/result-datasets/run9/save?what=tables").status_code == 409
