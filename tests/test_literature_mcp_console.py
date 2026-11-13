"""Registered console routes retain selected models, spend, cancellation and tables."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from aiscientist.agents.mcp_model import ModelBinding, RelayBinding
from aiscientist.gateway import app as gw, corpus_scan, literature_mcp
from aiscientist.gateway.errors import GatewayError


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setattr(literature_mcp, "registered_server", lambda: object())
    run = SimpleNamespace(chat_stop=threading.Event(), conversation_id="conversation", monitor=None)
    conn = SimpleNamespace(workspace=tmp_path, selected_model="selected", active_run=run, events=[],
                           push=lambda event: None)
    calls, staged, kept = [], [], []
    result = {"status": "ok", "state": "COMPLETED", "analysis_id": "default", "n_genes": 1,
              "api_usage": {"cost_usd": 0.021, "calls": 7}}
    route = SimpleNamespace(run_tool=lambda *args: calls.append(args) or dict(result),
                            stage_scan_tables=lambda *args: staged.append(args))
    async def build(*args):
        run.literature_mcp, run.literature_mcp_context = route, object()
        return route
    async def keep(*args):
        kept.append(args)
    monkeypatch.setattr(gw, "_build_literature_mcp_route", build)
    monkeypatch.setattr(gw, "_keep_corpus_scan", keep)
    monkeypatch.setattr(gw, "_push_usage", lambda *a: None)
    monkeypatch.setattr(gw, "_bound_credential", lambda c: {"id": "selected", "model": "haiku"})
    monkeypatch.setattr(gw, "_corpus_scan_cost_cap", lambda c: (0.1, "test budget"))
    monkeypatch.setattr(gw, "_build_literature_executor", lambda *a, **kw: pytest.fail("legacy executor used"))
    return conn, run, route, result, calls, staged, kept


@pytest.mark.parametrize("kind", ["api", "shared", "cluster"])
def test_registered_scan_uses_selected_model_and_preserves_publication(console, monkeypatch, kind):
    conn, run, route, _, calls, staged, kept = console
    binding = ModelBinding("http://cluster:8000/v1", "selected-cluster") if kind == "cluster" else RelayBinding(
        "selected-" + kind, lambda *a: pytest.fail("adapter must own model relay"), priced=True)
    monkeypatch.setattr(gw, "_mcp_session_model", lambda c: binding)
    monkeypatch.setattr(gw, "_bound_credential", lambda c: {"builtin": "shared"} if kind == "shared" else {})
    say = []
    out = asyncio.run(gw._corpus_scan_on_own_gpu(conn, run, "run1", ("cache", {}),
                      lambda *a: say.append(a), lambda *a: None))
    assert out["api_usage"]["model"] == binding.model
    assert calls[0][0] == "extract_haploinsufficient_genes"
    assert calls[0][1] == ({"max_cost_usd": 0.1} if kind == "api" else {})
    assert staged and kept and "cache" not in gw._CORPUS_SCANS_RUNNING
    assert any(binding.model in row[0] for row in say)
    assert "GPU of its own" not in gw._curation_time_words(conn, None, None)


def test_failed_priced_pilot_does_not_publish_or_cache(console, monkeypatch):
    conn, run, _, result, _, staged, kept = console
    monkeypatch.setattr(gw, "_mcp_session_model", lambda c: RelayBinding("haiku", lambda *a: None, priced=True))
    result.update(status="failed", state="FAILED", over_budget=True, error="pilot exceeds budget")
    with pytest.raises(GatewayError, match="pilot exceeds budget.*0.0210"):
        asyncio.run(gw._corpus_scan_via_relay(conn, run, "run1", ("cache", {}), lambda *a: None,
                                            lambda *a: None, {}))
    assert not staged and not kept and "cache" not in gw._CORPUS_SCANS_RUNNING


def test_unknown_price_refuses_before_model_or_extraction(console, monkeypatch):
    conn, run, _, _, calls, staged, kept = console
    monkeypatch.setattr(gw, "_mcp_session_model", lambda c: RelayBinding("unpriced", lambda *a: None))
    with pytest.raises(GatewayError, match="no known price"):
        asyncio.run(gw._corpus_scan_via_relay(conn, run, "run1", None, lambda *a: None, lambda *a: None, {}))
    assert not calls and not staged and not kept


def test_small_remaining_budget_is_never_rounded_to_unlimited(console, monkeypatch):
    conn, run, _, _, calls, _, _ = console
    monkeypatch.setattr(gw, "_mcp_session_model", lambda c: RelayBinding("haiku", lambda *a: None, priced=True))
    monkeypatch.setattr(gw, "_corpus_scan_cost_cap", lambda c: (0.000001, "remaining credit"))
    asyncio.run(gw._corpus_scan_via_relay(conn, run, "run1", None, lambda *a: None, lambda *a: None, {}))
    assert calls[0][1]["max_cost_usd"] == 0.000001


def test_stop_cannot_publish_successful_inflight_result(console, monkeypatch):
    conn, run, _, _, _, staged, kept = console
    monkeypatch.setattr(gw, "_mcp_session_model", lambda c: ModelBinding("http://cluster/v1", "cluster"))
    run.chat_stop.set()
    assert asyncio.run(gw._corpus_scan_on_own_gpu(conn, run, "run1", None, lambda *a: None,
                                               lambda *a: None)) is None
    assert not staged and not kept


def test_pubmed_console_calls_registered_tool_with_abstracts(console, monkeypatch):
    conn, run, route, result, calls, _, _ = console
    run.literature_mcp, run.literature_mcp_context = route, object()
    result.update(results=[])
    monkeypatch.setattr(gw._tools_api, "search_literature", lambda *a, **k: pytest.fail("legacy search used"))
    gw._literature_search_logged(conn, run, ["PRPF31"], per_query=8)
    assert calls[0][0:2] == ("literature_search", {"query": "PRPF31", "limit": 8,
                                                  "abstracts": True, "pubmed_only": True, "analysis_id": "pubmed"})


def test_registered_model_label_never_uses_legacy_dedicated_gpu(console, monkeypatch):
    conn, *_ = console
    monkeypatch.setattr(gw, "_mcp_session_model", lambda c: RelayBinding("selected-haiku", lambda *a: None))
    monkeypatch.setattr(corpus_scan, "scan_model", lambda c: pytest.fail("legacy GPU model selected"))
    assert gw._corpus_scan_model(conn) == "selected-haiku"
