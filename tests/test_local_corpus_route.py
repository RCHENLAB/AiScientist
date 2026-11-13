"""Local native retrieval selection and fallback, without network or model calls."""
from types import SimpleNamespace
import threading

import pytest

from aiscientist.agents.research_harness import HarnessTool
from aiscientist.gateway import app as gw
from aiscientist.gateway import local_corpus as lc, literature_mcp


def tool(execute):
    return HarnessTool(name="mcp_search_corpus", description="Native corpus fixture",
                       parameters={"type": "object"}, executor=execute)


def connection(tmp_path, remote=None, *, connected=False):
    run = SimpleNamespace(literature_mcp=remote, chat_stop=threading.Event(), literature_mcp_context=None)
    return SimpleNamespace(workspace=tmp_path, selected_model="selected-model", tunnel_port=0,
                           alloc=None, executor=object() if connected else None, mock=not connected, active_run=run)


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    monkeypatch.setattr(literature_mcp, "registered_server", lambda:
        SimpleNamespace(name="literature", settings_env=()))
    monkeypatch.setattr(gw, "_tool_read_roots", lambda conn: [])


def test_local_literature_runs_without_hpc_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("AISCIENTIST_PAPERQA_ON_HPC", raising=False)
    result = {"status": "ok", "contexts": [{"pmid": "12345", "page": 6, "text": "a passage"}]}
    monkeypatch.setattr(lc, "make_local_tool", lambda: tool(lambda a, c: result))
    conn = connection(tmp_path)
    assert gw._literature_corpus_available(conn)
    selected, ctx = gw._literature_corpus_tool(conn)
    assert selected.executor({"queries": ["a question"]}, ctx) == result


def test_failed_local_retrieval_falls_back_to_registered_hpc_route(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "make_local_tool", lambda: tool(lambda a, c: None))
    calls = []
    remote = SimpleNamespace(run_tool=lambda name, args, ctx:
        calls.append((name, args)) or {"status": "ok", "contexts": []})
    selected, ctx = gw._literature_corpus_tool(connection(tmp_path, remote))
    assert selected.executor({"queries": ["question"]}, ctx)["status"] == "ok"
    assert calls == [("search_corpus", {"analysis_id": "corpus", "queries": ["question"]})]


def test_native_console_keeps_registered_name_and_selected_model_context(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "make_local_tool", lambda: tool(lambda a, c: {"status": "ok"}))
    selected, ctx = gw._literature_corpus_tool(connection(tmp_path))
    assert selected.name == "mcp_search_corpus" and ctx.model == "selected-model"


def test_missing_local_and_remote_paths_do_not_invent_an_inprocess_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "make_local_tool", lambda: None)
    assert gw._literature_corpus_tool(connection(tmp_path)) is None


def test_a_stale_copy_is_skipped_and_hpc_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "make_local_tool", lambda: tool(lambda a, c: pytest.fail("stale copy searched")))
    asked = []
    monkeypatch.setattr(lc, "stale", lambda executor: asked.append(executor) or ["mechanism_cache/chunks.jsonl"])
    calls = []
    remote = SimpleNamespace(run_tool=lambda name, args, ctx:
        calls.append(name) or {"status": "ok", "contexts": []})
    conn = connection(tmp_path, remote, connected=True)
    selected, ctx = gw._literature_corpus_tool(conn)
    assert selected.executor({"queries": ["question"]}, ctx)["status"] == "ok"
    assert calls == ["search_corpus"] and asked == [conn.executor]


def test_a_fresh_copy_is_searched_here(tmp_path, monkeypatch):
    result = {"status": "ok", "contexts": [{"pmid": "1"}]}
    monkeypatch.setattr(lc, "make_local_tool", lambda: tool(lambda a, c: result))
    monkeypatch.setattr(lc, "stale", lambda executor: [])
    remote = SimpleNamespace(run_tool=lambda *a: pytest.fail("fresh local copy did not need HPC"))
    selected, ctx = gw._literature_corpus_tool(connection(tmp_path, remote, connected=True))
    assert selected.executor({"queries": ["question"]}, ctx) == result
