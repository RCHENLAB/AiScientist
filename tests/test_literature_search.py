"""literature_search returns REAL, structured citations from Europe PMC (mocked here)."""

from __future__ import annotations

import pytest

pytest.importorskip("httpx")  # gateway extra; offline CI subset doesn't install it

import httpx  # noqa: E402
from aiscientist.tools.literature_search.tool import (
    focus_literature_query,
    make_literature_search_tool,
    search_europepmc,
)

_FAKE = {
    "resultList": {"result": [
        {"title": "DDX41 in retinal photoreceptors", "authorString": "Li J, Sun Y",
         "pubYear": "2025", "journalTitle": "Cell", "doi": "10.1016/j.cell.2025.01.001",
         "pmid": "40000001", "source": "MED", "id": "40000001"},
        {"title": "DDX41 retina paper without DOI", "authorString": "Roe A", "pubYear": "2024",
         "journalTitle": "Nature", "doi": "", "pmid": "39999999", "source": "MED", "id": "39999999"},
    ]}
}


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def test_search_parses_real_citations(monkeypatch):
    seen = {}

    def fake_get(url, params=None, timeout=None, headers=None):
        seen["url"], seen["params"] = url, params
        return _Resp(_FAKE)

    monkeypatch.setattr(httpx, "get", fake_get)
    out = search_europepmc("DDX41 retina", limit=5)

    assert out["status"] == "ok" and out["count"] == 2
    by_pmid = {r["pmid"]: r for r in out["results"]}
    first = by_pmid["40000001"]
    assert first["doi"] == "10.1016/j.cell.2025.01.001"
    assert first["url"] == "https://doi.org/10.1016/j.cell.2025.01.001"
    assert "Li J et al. (2025)" in first["citation"] and "PMID" not in first["citation"]
    # a DOI-less paper falls back to a PubMed URL + PMID in the citation
    assert by_pmid["39999999"]["url"] == "https://pubmed.ncbi.nlm.nih.gov/39999999/"
    assert "PMID:39999999" in by_pmid["39999999"]["citation"]
    # the query is what we passed (only public keywords leave the host)
    assert seen["params"]["query"] == "DDX41 retina"


def test_search_degrades_gracefully_on_network_error(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", boom)
    out = search_europepmc("anything")
    assert out["status"] == "error" and "ConnectError" in out["error"]


def test_empty_query_is_an_error():
    assert search_europepmc("   ")["status"] == "error"


def test_tool_self_describes_and_executes(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(_FAKE))
    tool = make_literature_search_tool()
    assert tool.name == "literature_search" and tool.category == "literature"
    assert tool.reads_private_data is False
    out = tool.executor({"query": "DDX41", "limit": 3}, None)
    assert out["status"] == "ok" and out["results"][0]["title"]


def test_focus_literature_query_drops_file_and_metadata_terms():
    query = focus_literature_query(
        "Analyze the uploaded DDX41_DEG.h5ad single-cell RNA-seq dataset from Ddx41 "
        "conditional knockout and wild-type mouse retina. Use the existing sampleid, "
        "majorclass, and celltype annotations to compare Ddx41 KO and WT within each "
        "majorclass. Summarize shared and majorclass-specific expression changes, and "
        "include relevant literature context."
    )

    assert query == "DDX41 conditional knockout mouse retina"
    for bad in ("uploaded", "h5ad", "DEG", "sampleid", "majorclass", "celltype"):
        assert bad.lower() not in query.lower()


def test_focus_literature_query_drops_tool_instruction_terms():
    query = focus_literature_query("Search DDX41 retina Return citations evidence")

    assert query == "DDX41 retina"


def test_search_focuses_full_report_instruction_before_europepmc(monkeypatch):
    seen = {}

    def fake_get(_url, params=None, **_kwargs):
        seen["query"] = params["query"]
        return _Resp(_FAKE)

    monkeypatch.setattr(httpx, "get", fake_get)
    out = search_europepmc(
        "Write a short literature-only report about diseases caused by germline DDX41 "
        "mutations. Do not run QC, clustering, marker-gene analysis, or any single-cell "
        "analysis. Include real references.",
        limit=5,
    )

    assert out["status"] == "ok"
    assert out["query"] == seen["query"]
    assert "DDX41" in out["query"] and "germline" in out["query"]
    assert "Write" not in out["query"] and "QC" not in out["query"]
    assert "references" not in out["query"].lower()


def test_search_filters_generic_abstract_books(monkeypatch):
    payload = {"resultList": {"result": [
        {"title": "Abstracts of the 84th Annual Meeting of the Japanese Cancer Association",
         "authorString": "", "pubYear": "2026", "journalTitle": "Cancer Science",
         "doi": "10.1111/cas.70254", "pmid": "", "source": "MED", "id": "1"},
        {"title": "Germline DDX41 mutations in myeloid neoplasms: the current clinical and molecular understanding",
         "authorString": "Kida J, Makishima H", "pubYear": "2025",
         "journalTitle": "Current Opinion in Hematology",
         "doi": "10.1097/moh.0000000000000854", "pmid": "40000002",
         "source": "MED", "id": "2"},
        {"title": "Abstract Book: 25th Congress of the European Hematology Association Virtual Edition",
         "authorString": "", "pubYear": "2020", "journalTitle": "HemaSphere",
         "doi": "", "pmid": "", "source": "PMC", "id": "PMC8901205"},
    ]}}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(payload))

    out = search_europepmc("What diseases are caused by germline DDX41 mutations?", limit=5)

    assert out["status"] == "ok"
    assert out["unfiltered_count"] == 3
    assert out["filtered_count"] == 1
    assert len(out["results"]) == 1
    assert "Germline DDX41 mutations" in out["results"][0]["title"]


def test_search_filters_off_topic_hits(monkeypatch):
    payload = {"resultList": {"result": [
        {"title": "The Black Hole: CAR T Cell Therapy in AML",
         "authorString": "Atilla E", "pubYear": "2023", "journalTitle": "Cancers",
         "doi": "10.3390/cancers15102713", "pmid": "1", "source": "MED", "id": "1"},
        {"title": "Biallelic germline variants in the hematologic malignancy predisposition gene DDX41 cause retinal dystrophy through dysregulation of retinal homeostasis",
         "authorString": "Mars Z", "pubYear": "2026", "journalTitle": "medRxiv",
         "doi": "10.64898/2026.01.28.26344834", "pmid": "", "source": "PPR", "id": "2"},
    ]}}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(payload))

    out = search_europepmc("DDX41 retina innate immunity hematopoiesis", limit=5)

    assert out["filtered_count"] == 1
    assert "DDX41" in out["results"][0]["title"]
    assert "CAR T" not in "\n".join(c["citation"] for c in out["results"])


@pytest.fixture
def deep_response():
    # Newest first: the useful paper is far beyond the previous eight-hit request.
    hits = [{"title": "Unrelated cancer treatment", "pubYear": "2026", "pmid": str(i),
             "authorString": "RHO A", "journalTitle": "Retina", "abstractText": "Tumour growth."}
            for i in range(1, 101)]
    hits[79] = {"title": "Inherited visual disease", "pubYear": "2003", "pmid": "12906118",
                "abstractText": "<p>" + "Background. " * 250 + "RHO retina degeneration.</p>"}
    return {"resultList": {"result": hits}}


@pytest.mark.parametrize("abstracts", [False, True])
def test_deep_abstract_match_outranks_newer_hits_in_one_request(monkeypatch, deep_response, abstracts):
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs)
        return _Resp(deep_response)

    monkeypatch.setattr(httpx, "get", get)
    out = search_europepmc("RHO retina degeneration", limit=8, abstracts=abstracts, pubmed_only=True)
    assert len(calls) == 1
    assert calls[0]["params"] == {"query": "(RHO retina degeneration) AND SRC:MED", "format": "json",
                                  "pageSize": 100, "resultType": "core"}
    assert calls[0]["timeout"] == 20.0
    assert out["unfiltered_count"] == 100
    assert [r["pmid"] for r in out["results"]] == ["12906118"]
    assert ("abstract" in out["results"][0]) is abstracts
    if abstracts:
        assert len(out["results"][0]["abstract"]) <= 2000


def test_zero_overlap_does_not_match_authors_journal_or_substrings(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp({"resultList": {"result": [
        {"title": "Rhodopsin structure", "abstractText": "Protein folding.", "authorString": "RHO A",
         "journalTitle": "RHO", "pmid": "1"}]}}))
    out = search_europepmc("RHO", abstracts=True)
    assert out["unfiltered_count"] == 1 and out["results"] == []


def test_title_overlap_has_more_weight_than_abstract_overlap(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp({"resultList": {"result": [
        {"title": "Protein structure", "abstractText": "RHO " * 200, "pmid": "1", "pubYear": "2026"},
        {"title": "RHO structure", "pmid": "2", "pubYear": "2003"}]}}))
    assert [r["pmid"] for r in search_europepmc("RHO")["results"]] == ["2", "1"]



def test_deep_search_returns_ranked_candidates_but_answer_reads_only_twelve(monkeypatch):
    from aiscientist.agents.literature_answer import collect_papers

    calls = []
    hits = [{"title": f"RHO retina study {i}", "abstractText": "RHO degeneration.",
             "pmid": str(1000 + i), "pubYear": "2026"} for i in range(100)]

    def get(url, **kwargs):
        calls.append(kwargs)
        return _Resp({"resultList": {"result": hits}})

    monkeypatch.setattr(httpx, "get", get)
    got = collect_papers(lambda q, n: search_europepmc(q, n, abstracts=True, pubmed_only=True), ["RHO retina"])
    assert len(calls) == 1
    assert calls[0]["params"]["pageSize"] == 100          # reads 100 hits ...
    assert got.searched == [{"query": "RHO retina", "count": 12}]   # ... keeps the best 12
    assert len(got.papers) == 12


def test_a_large_limit_still_returns_at_most_twenty_five_papers(monkeypatch):
    # The search reads 100 hits, but the tool's schema promises 1-25 results to the model.
    hits = [{"title": f"RHO retina study {i}", "abstractText": "RHO degeneration.", "pmid": str(2000 + i)}
            for i in range(100)]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp({"resultList": {"result": hits}}))
    out = search_europepmc("RHO retina", limit=100)
    assert out["unfiltered_count"] == 100 and out["count"] == 25
