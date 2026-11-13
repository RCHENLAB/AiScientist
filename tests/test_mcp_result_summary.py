"""Report previews preserve negative evidence and factual results under the byte bound."""
import copy
import inspect
import json

from aiscientist.agents.mcp_result_summary import project_result


def test_diagnose_negative_evidence_survives_large_ranked_result():
    row = {"disease_name": "Synthetic retinal condition", "disease_id": "OMIM:000000", "gene": "",
           "posttest_prob": 0.4287, "rank": 1, "agreement": "lirical_only"}
    result = {"status": "ok", "state": "COMPLETED", "analysis_id": "synthetic", "n_candidates": 8621,
              "full_result": "mcp/phenotype/synthetic/diagnose.json", "top": row,
              "differential": [dict(row, rank=i + 1) for i in range(8621)],
              "literature": [{"status": "ok", "gene": "PRPF31", "association": False,
                              "clingen_tier": "NONE", "evidence_status": "unsupported",
                              "notes": ["The retrieved passages did not support this gene."],
                              "evidence": [{"source_id": "doi:10.0000/example", "doi": "10.0000/example",
                                            "quote": "unrelated paper" * 100} for _ in range(8)]}]}
    before = copy.deepcopy(result)
    out = project_result("phenotype-diagnosis", result)
    assert out["literature"][0]["association"] is False
    assert out["literature"][0]["evidence_status"] == "unsupported"
    assert out["literature"][0]["evidence_count"] == 8
    assert out["top"] == row and out["n_candidates"] == 8621
    assert out["full_result"] == result["full_result"]
    assert out["response_summary"]["complete"] is False
    assert result == before and len(json.dumps(out, ensure_ascii=False).encode()) <= 5500


def test_extraction_zero_findings_and_measured_cost_are_not_dropped():
    out = project_result("literature", {"status": "ok", "state": "COMPLETED", "n_genes": 0,
        "genes": [], "n_hits_eligible": 0, "over_budget": False, "n_passages": 4,
        "n_judged": 4, "api_usage": {"calls": 5, "cost_usd": 0.001761},
        "pilot": {"passages": 4, "read": 4, "cost_usd": 0.001635, "estimated_total_usd": 0.0033},
        "contexts": [{"text": "irrelevant" * 20000}]})
    assert out["n_genes"] == out["n_hits_eligible"] == 0
    assert out["genes"] == [] and out["over_budget"] is False
    assert out["api_usage"]["cost_usd"] == 0.001761
    assert out["pilot"]["estimated_total_usd"] == 0.0033


def test_deep_answer_and_source_ids_survive_unicode_bounded_summary():
    out = project_result("literature", {"status": "ok", "n_contexts": 8,
        "answer": "The retrieved report describes a 12 bp deletion in 19 affected relatives. " + "表型" * 3000,
        "contexts": [{"pmid": "12923864", "doi": "10.1002/ajmg.a.20224", "citation": "Wang et al. (2003)",
                      "summary": "Long passage " * 10000} for _ in range(8)]})
    assert out["answer"].startswith("The retrieved report describes a 12 bp deletion")
    assert out["sources"][0]["pmid"] == "12923864"
    assert "answer" in out["response_summary"]["shortened_fields"]
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= 5500


def test_function_embeds_without_importing_app_or_server():
    namespace = {}
    exec(inspect.getsource(project_result), namespace)
    result = namespace["project_result"]("literature", {"status": "error", "error": "pilot exceeded", "over_budget": True})
    assert result["status"] == "error" and result["over_budget"] is True


def test_explicit_trailing_source_ids_survive_long_author_lists_and_no_id_is_invented():
    out = project_result("literature", {"status": "ok", "answer": "A supported answer.",
        "contexts": [{"citation": "Author " * 100 + "DOI:10.1002/ajmg.a.20224. PMID:12923864", "summary": "Text"},
                     {"citation": "Unidentified source with PRPF31 and 12923864 in its title", "summary": "Other"}]})
    assert out["sources"][0]["doi"] == "10.1002/ajmg.a.20224"
    assert out["sources"][0]["pmid"] == "12923864"
    assert "pmid" not in out["sources"][1] and "doi" not in out["sources"][1]


def test_context_budget_keeps_a_passage_instead_of_dropping_all_contexts():
    out = project_result("literature", {"status": "ok", "n_contexts": 3, "n_papers": 3,
        "contexts": [{"pmid": str(100 + i), "citation": "Author " * 70,
                      "title": "Example " * 35, "text": "A passage " * 300} for i in range(3)]}, 4500)
    assert out["contexts"] and "A passage" in out["contexts"][0]["text"]
    assert out["n_contexts"] == 3
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= 4500
