"""Reports reuse accepted registered MCP citations without rewriting call provenance."""
import copy
from types import SimpleNamespace

import pytest

from aiscientist.agents import mcp_bridge
from aiscientist.gateway import app as gw


@pytest.fixture
def server(monkeypatch):
    server = SimpleNamespace(name="literature", adapter="analysis", tools=[
        {"name": n} for n in ("literature_search", "deep_literature", "search_corpus")],
        planner_name=lambda name: "mcp_reviewed_" + name)
    monkeypatch.setattr(mcp_bridge, "configured_servers", lambda: (server,))
    return server


def _round(name, *, accepted=True, ok=True):
    return {"verdict": {"verdict": "accept" if accepted else "revise"}, "scientist_result": {
        "final_answer": "The selected evidence is PMID:12345678.", "steps": [{
            "tool": name, "ok": ok, "args": {"query": "retinal evidence", "question": "Evidence?"},
            "result": {"status": "ok", "results": [
                {"title": "Selected evidence", "pmid": "12345678", "citation": "Selected. PMID:12345678"},
                {"title": "Unused evidence", "pmid": "87654321", "citation": "Unused. PMID:87654321"}],
                "contexts": [{"citation": "Selected. PMID:12345678"}], "answer": "Grounded answer."}}]}}


@pytest.mark.parametrize("wire,function", [
    ("literature_search", gw._references_from_accepted_literature_search),
    ("deep_literature", gw._references_from_accepted_deep_literature),
    ("search_corpus", gw._references_from_accepted_deep_literature)])
def test_registered_results_have_same_report_citations_as_original_calls(server, wire, function):
    legacy = {"rounds": [_round(wire)]}
    native = {"rounds": [_round(server.planner_name(wire))]}
    before = copy.deepcopy(native)
    assert function(native) == function(legacy)
    assert native == before  # actual execution name remains in the audit trail
    assert function(native)["citations"]


@pytest.mark.parametrize("accepted,ok", [(False, True), (True, False)])
def test_failed_or_unaccepted_native_results_are_not_cited(server, accepted, ok):
    assert gw._references_from_accepted_literature_search({"rounds": [
        _round(server.planner_name("literature_search"), accepted=accepted, ok=ok)]}) is None
    assert gw._references_from_accepted_deep_literature({"rounds": [
        _round(server.planner_name("deep_literature"), accepted=accepted, ok=ok)]}) is None


def test_foreign_and_unregistered_names_do_not_gain_literature_identity(server):
    assert gw._references_from_accepted_literature_search({"rounds": [_round("mcp_literature_search")]}) is None
    server.name = "foreign"
    assert gw._references_from_accepted_literature_search({"rounds": [
        _round(server.planner_name("literature_search"))]}) is None


def test_public_citations_keep_selected_identifier_gate_and_limit(server):
    result = {"rounds": [_round(server.planner_name("literature_search"))]}
    selected = gw._references_from_accepted_literature_search(result, limit=1)
    assert len(selected["citations"]) == 1 and selected["citations"][0]["pmid"] == "12345678"
    result["rounds"][0]["scientist_result"]["final_answer"] = "No explicitly cited paper."
    assert gw._references_from_accepted_literature_search(result) is None
