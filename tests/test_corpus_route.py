"""The corpus-scan line (``route == "corpus"``): a third engine next to Research and Chat that runs
``extract_haploinsufficient_genes`` as one HPC3 literature job and hands back its CSVs. It must
bypass the follow-up router (never "amend the previous report"), share the chat_start/chat_done WS
protocol, publish the synced-back files to the downloads panel, and say so when the literature
line is not available."""
from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_conns():
    yield
    gw_app.CONNECTIONS.clear()


def _conn(tmp_path, **attrs):
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = object()
    conn.selected_model = "qwen"
    for k, v in attrs.items():
        setattr(conn, k, v)
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


def _dispatch(conn, question, **req_kw):
    req = gw_app.LabRequest(connection_id=conn.id, question=question, **req_kw)
    asyncio.run(gw_app._dispatch_lab(conn, req))


def test_corpus_route_bypasses_the_followup_router(tmp_path, monkeypatch):
    conn = _conn(tmp_path, last_run_id="run123")
    art = conn.workspace / "run123" / "artifacts" / "process"
    art.mkdir(parents=True)
    (art / "run_state.json").write_text('{"question": "q", "agenda": ["a"], "rounds": []}')
    cap: dict = {}

    async def fake_corpus(c, req):
        cap["route"] = req.route

    def never(*a, **k):  # the lab and the classifier must not be touched
        raise AssertionError("research path used")

    monkeypatch.setattr(gw_app, "_run_corpus_scan", fake_corpus)
    monkeypatch.setattr(gw_app, "_run_lab", never)
    monkeypatch.setattr(gw_app, "_lab_llm", never)
    _dispatch(conn, "scan", route="corpus")
    assert cap["route"] == "corpus"
    assert gw_app._corpus_path(gw_app.LabRequest(connection_id="x", question="q", route="Corpus "))
    assert not gw_app._corpus_path(gw_app.LabRequest(connection_id="x", question="q", route="chat"))


class _FakeExecutor:
    """Stands in for the Slurm literature executor: writes the two CSVs into the run's local
    artifacts (what _sync_artifacts_back does) and returns the tool's result."""

    def __init__(self, local_workspace: Path, result: dict):
        self.local_workspace = local_workspace
        self.result = result
        self.calls: list = []
        self.notify = None

    def run_tool(self, tool, args, ctx):
        self.calls.append((tool, args, ctx))
        out = self.local_workspace / "artifacts" / "haploinsufficiency"
        out.mkdir(parents=True)
        with (out / "genes.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["gene", "n_papers"])
            w.writerow(["PRPF31", "3"])
        (out / "hits.csv").write_text("pmid,gene\n1,PRPF31\n")
        return self.result


def test_corpus_scan_runs_the_tool_and_publishes_the_csvs(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append
    built: dict = {}
    result = {"status": "ok", "n_papers": 1739, "n_sentences": 426706, "n_candidates": 14535,
              "n_hits_eligible": 120, "n_genes": 1,
              "genes": [{"gene": "PRPF31", "n_papers": 3, "phenotype": "adRP", "flags": []}],
              "genes_flagged_for_review": [], "files": {"genes_csv": "artifacts/haploinsufficiency/genes.csv"},
              "definition": "one copy is not enough", "warnings": ["w1"]}

    def fake_build(c, run_id=None):
        built["run_id"] = run_id
        built["executor"] = _FakeExecutor(c.workspace / run_id, result)
        return built["executor"]

    monkeypatch.setattr(gw_app, "_build_literature_executor", fake_build)
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus", conversation_id="conv1")
    conn.begin_run("conv1")
    asyncio.run(gw_app._run_corpus_scan(conn, req))

    run_id = built["run_id"]
    assert run_id and (conn.workspace / run_id / "artifacts" / "haploinsufficiency" / "genes.csv").is_file()
    ex = built["executor"]
    assert ex.calls and ex.calls[0][0] == "extract_haploinsufficient_genes" and ex.calls[0][1] == {}
    assert str(ex.calls[0][2].workspace) == str(conn.workspace / run_id)
    assert ex.calls[0][2].model == "qwen"
    types = [p.get("type") for p in pushed]
    assert types[0] == "chat_start" and types[-1] == "chat_done"
    token = next(p for p in pushed if p.get("type") == "chat_token")["token"]
    assert "PRPF31" in token and "1739" in token and "⚠ w1" in token
    arts = next(p for p in pushed if p.get("type") == "artifacts")
    # the two tables, and the result's Croissant metadata describing them (issue 9)
    assert {a["name"] for a in arts["items"]} == {"genes.csv", "hits.csv", "croissant.json"}
    meta = json.loads((conn.workspace / run_id / "artifacts" / "croissant.json").read_text())
    assert meta["prov:wasGeneratedBy"]["@id"] == f"urn:aiscientist:run:{run_id}"
    assert {d["contentUrl"] for d in meta["distribution"]} == {"haploinsufficiency/genes.csv",
                                                              "haploinsufficiency/hits.csv"}
    assert all(a["url"].startswith(f"/api/file/{conn.owner}/{run_id}/") for a in arts["items"])
    assert arts["bundle_url"] == f"/api/bundle/{conn.owner}/{run_id}"
    assert conn.chat_running is False
    # No run_state.json: a later research message must not see this as a study to amend.
    assert not (conn.workspace / run_id / "artifacts" / "process" / "run_state.json").exists()


def test_corpus_scan_without_the_literature_line_reports_and_stops(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append
    monkeypatch.setattr(gw_app, "_build_literature_executor", lambda c, run_id=None: None)
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus")
    conn.begin_run(None)
    asyncio.run(gw_app._run_corpus_scan(conn, req))
    err = next(p for p in pushed if p.get("type") == "chat_error")
    assert "literature line" in err["message"]
    assert not any(p.get("type") == "chat_done" for p in pushed)
    assert conn.chat_running is False


def test_corpus_scan_tool_failure_is_a_chat_error(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append

    class Failing:
        notify = None

        def run_tool(self, tool, args, ctx):
            return {"status": "error", "error": "HPC3 analysis job failed: boom"}

    monkeypatch.setattr(gw_app, "_build_literature_executor", lambda c, run_id=None: Failing())
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus")
    conn.begin_run(None)
    asyncio.run(gw_app._run_corpus_scan(conn, req))
    err = next(p for p in pushed if p.get("type") == "chat_error")
    assert "boom" in err["message"]


def test_literature_executor_gets_a_per_run_workspace(tmp_path, monkeypatch):
    """With a run_id the literature job writes under the run (so the files come back under it);
    without one (the chat path) the shared paperqa dir is used as before."""
    conn = _conn(tmp_path, mock=False)
    conn.alloc = type("A", (), {"node": "gpu-1", "port": 1234})()
    monkeypatch.setenv("AISCIENTIST_PAPERQA_ON_HPC", "1")
    monkeypatch.setenv("AISCIENTIST_PAPERQA_IMAGE", "/x/paperqa.sif")
    monkeypatch.setattr(gw_app, "_sync_aiscientist_source_to_hpc", lambda c: "/dfs/pysrc/tester")
    monkeypatch.setattr(gw_app, "_temp_base", lambda c: "/dfs/Temp/tester")
    ex = gw_app._build_literature_executor(conn, run_id="abc123")
    assert ex.remote_workspace == "/dfs/Temp/tester/corpus/abc123"
    assert ex.local_workspace == conn.workspace / "abc123"
    ex2 = gw_app._build_literature_executor(conn)
    assert ex2.remote_workspace == "/dfs/Temp/tester/paperqa" and ex2.local_workspace == conn.workspace


def test_corpus_scan_heals_the_model_and_resumes(tmp_path, monkeypatch):
    """The job reports model_unreachable (the serve job was preempted): the gateway recovers the
    session's model and resubmits; the second submission's result is the one reported."""
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append
    healed = []
    monkeypatch.setattr(gw_app, "_heal_vllm_session", lambda c: healed.append(c))
    ok = {"status": "ok", "n_papers": 3, "n_sentences": 30, "n_candidates": 4, "n_hits_eligible": 1,
          "n_genes": 1, "genes": [{"gene": "PRPF31", "n_papers": 1, "phenotype": "adRP", "flags": []}],
          "genes_flagged_for_review": [], "definition": "d", "warnings": []}

    class Flaky(_FakeExecutor):
        def __init__(self, ws):
            super().__init__(ws, ok)
            self.n = 0

        def run_tool(self, tool, args, ctx):
            self.n += 1
            if self.n == 1:
                return {"status": "failed", "model_unreachable": True, "n_judged": 120, "n_candidates": 4000,
                        "error": "The served model stopped answering"}
            return super().run_tool(tool, args, ctx)

    built = {}
    monkeypatch.setattr(gw_app, "_build_literature_executor",
                        lambda c, run_id=None: built.setdefault("ex", Flaky(c.workspace / run_id)))
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus")
    conn.begin_run(None)
    asyncio.run(gw_app._run_corpus_scan(conn, req))
    assert len(healed) == 1 and built["ex"].n == 2
    assert any("went away mid-scan" in p.get("text", "") for p in pushed if p.get("type") == "lab_progress")
    assert [p.get("type") for p in pushed][-1] == "chat_done"
    assert any("PRPF31" in p.get("token", "") for p in pushed if p.get("type") == "chat_token")


def test_corpus_scan_gives_up_after_the_last_attempt(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append
    monkeypatch.setattr(gw_app, "_heal_vllm_session", lambda c: None)

    class Dead:
        notify = None
        n = 0

        def run_tool(self, tool, args, ctx):
            self.n += 1
            return {"status": "failed", "model_unreachable": True, "n_judged": 0, "n_candidates": 4000,
                    "error": "The served model stopped answering"}

    dead = Dead()
    monkeypatch.setattr(gw_app, "_build_literature_executor", lambda c, run_id=None: dead)
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus")
    conn.begin_run(None)
    asyncio.run(gw_app._run_corpus_scan(conn, req))
    assert dead.n == gw_app.CORPUS_MAX_ATTEMPTS
    err = next(p for p in pushed if p.get("type") == "chat_error")
    assert "stopped answering" in err["message"]


def test_the_corpus_tool_is_run_by_this_line_only():
    """The scan is an hour of the session's model and writes into a workspace the Research line's
    literature job does not have, so no model may call it: it is absent from the Scientist's
    catalog, while the Slurm executor still finds its manifest (job resources) by name."""
    from aiscientist.agents.registry import build_scientist_catalog
    from aiscientist.tools import catalog

    m = catalog.manifest(gw_app.CORPUS_TOOL)
    assert m is not None and m.research is False and m.chat is False
    assert m.runs_on == "hpc:literature" and m.time_limit
    assert gw_app.CORPUS_TOOL not in {t.name for t in build_scientist_catalog(literature_executor=object())}
