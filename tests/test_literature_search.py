"""literature_search returns REAL, structured citations from Europe PMC (mocked here)."""

from __future__ import annotations

import pytest

pytest.importorskip("httpx")  # gateway extra; offline CI subset doesn't install it

import httpx  # noqa: E402
from bioagent.tools.literature_search import (  # noqa: E402
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
    first = out["results"][0]
    assert first["doi"] == "10.1016/j.cell.2025.01.001"
    assert first["url"] == "https://doi.org/10.1016/j.cell.2025.01.001"
    assert "Li J et al. (2025)" in first["citation"] and "PMID" not in first["citation"]
    # a DOI-less paper falls back to a PubMed URL + PMID in the citation
    assert out["results"][1]["url"] == "https://pubmed.ncbi.nlm.nih.gov/39999999/"
    assert "PMID:39999999" in out["results"][1]["citation"]
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
