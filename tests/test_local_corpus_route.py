"""Local retrieval gateway selection with no network or models."""
from types import SimpleNamespace
from aiscientist.gateway import app as gw
from aiscientist.gateway import local_corpus as lc
from aiscientist.tools.search_corpus.tool import make_tool
from dataclasses import replace


def connection(tmp_path):
    return SimpleNamespace(workspace=tmp_path, selected_model="test", tunnel_port=0,
                           alloc=None, executor=None, mock=True)


def test_local_literature_runs_without_hpc_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("AISCIENTIST_PAPERQA_ON_HPC", raising=False)
    result = {"status": "ok", "contexts": [{"pmid": "12345", "page": 6, "text": "a passage"}]}
    monkeypatch.setattr(lc, "make_local_tool", lambda: replace(make_tool(), executor=lambda a, c: result))
    monkeypatch.setattr(gw, "_tool_read_roots", lambda conn: [])
    monkeypatch.setattr(gw, "_build_literature_executor", lambda c: (_ for _ in ()).throw(AssertionError("HPC used")))
    conn = connection(tmp_path)
    assert gw._literature_corpus_available(conn)
    tool, ctx = gw._literature_corpus_tool(conn)
    assert tool.executor({"queries": ["a question"]}, ctx) == result


def test_failed_local_retrieval_falls_back_to_hpc(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "make_local_tool", lambda: replace(make_tool(), executor=lambda a, c: None))
    monkeypatch.setattr(gw, "_tool_read_roots", lambda conn: [])
    calls = []
    def remote(name, args, ctx):
        calls.append((name, args))
        return {"status": "ok", "contexts": []}
    monkeypatch.setattr(gw, "_build_literature_executor", lambda c: SimpleNamespace(run_tool=remote))
    tool, ctx = gw._literature_corpus_tool(connection(tmp_path))
    assert tool.executor({"queries": ["question"]}, ctx)["status"] == "ok"
    assert calls == [("search_corpus", {"queries": ["question"]})]


def test_local_search_is_offered_in_research_roster(tmp_path, monkeypatch):
    monkeypatch.delenv("AISCIENTIST_PAPERQA_ON_HPC", raising=False)
    monkeypatch.setattr(lc, "has_local_index", lambda: True)
    tool = replace(make_tool(), executor=lambda a, c: {"status": "ok"})
    monkeypatch.setattr(gw, "_literature_corpus_tool", lambda c: (tool, None))
    roster = gw._session_roster(connection(tmp_path), [], None, lambda *a: None)
    assert [t.name for t in roster] == ["search_corpus"]


def test_research_roster_loads_model_off_the_event_loop(tmp_path, monkeypatch):
    import asyncio
    import threading

    loop_thread = threading.get_ident()
    loaded = []
    monkeypatch.setattr(lc, "has_local_index", lambda: True)
    monkeypatch.setattr(gw, "_tool_read_roots", lambda conn: [])

    def load(root):
        loaded.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return lambda texts: []

    monkeypatch.setattr(lc, "_embedder", load)
    roster = asyncio.run(gw._prepare_session_roster(connection(tmp_path), [], None, lambda *a: None))
    assert [t.name for t in roster] == ["search_corpus"]
    assert len(loaded) == 1
