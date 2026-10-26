"""Offline passage-grounded gene-table run and empty retrieval."""
import asyncio
import csv
import json

import pytest
from types import SimpleNamespace

from aiscientist.gateway import app as gw
from aiscientist.gateway.settings import HPCSettings


def test_gene_table_run_filters_quotes_and_keeps_csv(tmp_path, monkeypatch):
    conn = gw.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace = tmp_path / "tester"
    pushed = []
    conn.push = lambda payload, **kw: pushed.append(payload)
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    monkeypatch.setattr(gw, "_literature_corpus_tool", lambda c: (object(), object()))
    quote = "PRPF31 haploinsufficiency causes retinal degeneration."
    monkeypatch.setattr(gw, "_literature_corpus_logged", lambda *a: {
        "status": "ok", "contexts": [{"pmid": "12345", "page": 6, "text": quote}]})
    finished = []
    monkeypatch.setattr(gw, "_finish_run_record", lambda *args: finished.append(args))
    calls = []
    def complete(messages, **kw):
        calls.append((messages, kw))
        return json.dumps([{"gene": "PRPF31", "evidence": quote, "passage": 1},
                           {"gene": "RHO", "evidence": "RHO is haploinsufficient.", "passage": 1}])
    monkeypatch.setattr(gw, "_lab_llm", lambda c: SimpleNamespace(complete_fn=complete))
    monkeypatch.setattr(gw, "_stamp_literature_result", lambda *a: None)
    req = gw.LabRequest(connection_id=conn.id, question="What are haploinsufficient genes?", route="chat")
    asyncio.run(gw._run_gene_table(conn, req))
    token = next(p["token"] for p in pushed if p["type"] == "chat_token")
    assert "| gene | evidence" in token and "PRPF31" in token and "RHO" not in token
    assert "PMID 12345, p. 6" in token and "not from every paper" in token
    assert "Ask about the RetiGene papers by name for the complete table" in token and calls[0][1] == {"role": "literature"}
    path = next(conn.workspace.glob("*/artifacts/literature/genes.csv"))
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1 and rows[0]["evidence"] == quote
    assert pushed[-1]["type"] == "chat_done"
    assert len(finished) == 1 and finished[0][2] == "done"
    conn.loop.close()


def test_missing_corpus_still_returns_empty_table_and_csv(tmp_path, monkeypatch):
    conn = gw.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace = tmp_path / "tester"
    pushed = []
    conn.push = lambda payload, **kw: pushed.append(payload)
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    monkeypatch.setattr(gw, "_literature_corpus_tool", lambda c: None)
    monkeypatch.setattr(gw, "_stamp_literature_result", lambda *a: None)
    req = gw.LabRequest(connection_id=conn.id, question="List haploinsufficient genes", route="chat")
    asyncio.run(gw._run_gene_table(conn, req))
    token = next(p["token"] for p in pushed if p["type"] == "chat_token")
    assert "| gene | evidence" in token and "do not answer this with verified gene evidence" in token
    assert next(conn.workspace.glob("*/artifacts/literature/genes.csv")).is_file()
    assert pushed[-1]["type"] == "chat_done"
    conn.loop.close()


@pytest.mark.parametrize("stop_during", ["retrieval", "model", "error"])
def test_gene_table_finishes_record_once_on_stop_or_error(tmp_path, monkeypatch, stop_during):
    conn = gw.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace = tmp_path / "tester"
    pushed, finished = [], []
    conn.push = lambda payload, **kw: pushed.append(payload)
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    monkeypatch.setattr(gw, "_finish_run_record", lambda *args: finished.append(args))

    def corpus(c):
        if stop_during == "error":
            raise ValueError("retrieval failed")
        return object(), object()

    def retrieve(c, run, *args):
        if stop_during == "retrieval":
            run.chat_stop.set()
        return {"status": "ok", "contexts": [{"pmid": "12345", "page": 6, "text": "PRPF31 disease"}]}

    def complete(*args, **kwargs):
        conn.active_run.chat_stop.set()
        return "[]"

    monkeypatch.setattr(gw, "_literature_corpus_tool", corpus)
    monkeypatch.setattr(gw, "_literature_corpus_logged", retrieve)
    monkeypatch.setattr(gw, "_lab_llm", lambda c: SimpleNamespace(complete_fn=complete))
    req = gw.LabRequest(connection_id=conn.id, question="List haploinsufficient genes", route="chat")
    asyncio.run(gw._run_gene_table(conn, req))
    assert len(finished) == 1
    assert finished[0][2] == ("failed" if stop_during == "error" else "cancelled")
    assert pushed[-1]["type"] == ("chat_error" if stop_during == "error" else "chat_stopped")
    assert not conn.chat_running
    conn.loop.close()
