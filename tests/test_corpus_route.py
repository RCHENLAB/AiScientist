"""The corpus-scan line (``route == "corpus"``): a third engine next to Research and Chat that runs
``extract_haploinsufficient_genes`` as one HPC3 job with a GPU and a model of its own and hands back
its CSVs. It must bypass the follow-up router (never "amend the previous report"), share the
chat_start/chat_done WS protocol, publish the synced-back files to the downloads panel, say so when
the literature line is not available, keep a finished scan, and answer from a kept scan of the same
corpus without a job — from Corpus scan or from a chat message asking for the gene list."""
from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway import corpus_scan  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_conns(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    yield
    gw_app.CONNECTIONS.clear()
    gw_app._CORPUS_SCANS_RUNNING.clear()


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
    result = {"status": "ok", "n_papers": 1739, "n_sentences": 426706, "n_passages": 1200, "n_requests": 16, "n_gene_requests": 40,
              "n_hits_eligible": 120, "n_genes": 1,
              "genes": [{"gene": "PRPF31", "n_papers": 3, "phenotype": "adRP", "flags": []}],
              "genes_flagged_for_review": [], "files": {"genes_csv": "artifacts/haploinsufficiency/genes.csv"},
              "definition": "one copy is not enough", "warnings": ["w1"]}

    def fake_build(c, run_id=None, own_gpu=False):
        built["run_id"], built["own_gpu"] = run_id, own_gpu
        built["executor"] = _FakeExecutor(c.workspace / run_id, result)
        return built["executor"]

    monkeypatch.setattr(gw_app, "_build_literature_executor", fake_build)
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus", conversation_id="conv1")
    conn.begin_run("conv1")
    asyncio.run(gw_app._run_corpus_scan(conn, req))

    run_id = built["run_id"]
    assert run_id and (conn.workspace / run_id / "artifacts" / "haploinsufficiency" / "genes.csv").is_file()
    ex = built["executor"]
    assert built["own_gpu"] is True                       # its own GPU and model, not the session's
    assert ex.calls and ex.calls[0][0] == "extract_haploinsufficient_genes" and ex.calls[0][1] == {}
    assert str(ex.calls[0][2].workspace) == str(conn.workspace / run_id)
    assert ex.calls[0][2].model == corpus_scan.scan_model(conn.settings)
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
    monkeypatch.setattr(gw_app, "_build_literature_executor", lambda c, run_id=None, own_gpu=False: None)
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

    monkeypatch.setattr(gw_app, "_build_literature_executor",
                        lambda c, run_id=None, own_gpu=False: Failing())
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


def test_corpus_scan_resubmits_when_its_model_died(tmp_path, monkeypatch):
    """The job reports model_unreachable (its own model died mid-scan): the job is submitted again
    and resumes; the session's model is not touched, and the second result is the one reported."""
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append

    def no_heal(c):
        raise AssertionError("the session's model is not the scan's")

    monkeypatch.setattr(gw_app, "_heal_vllm_session", no_heal)
    ok = {"status": "ok", "n_papers": 3, "n_sentences": 30, "n_passages": 4, "n_hits_eligible": 1,
          "n_genes": 1, "genes": [{"gene": "PRPF31", "n_papers": 1, "phenotype": "adRP", "flags": []}],
          "genes_flagged_for_review": [], "definition": "d", "warnings": []}

    class Flaky(_FakeExecutor):
        def __init__(self, ws):
            super().__init__(ws, ok)
            self.n = 0

        def run_tool(self, tool, args, ctx):
            self.n += 1
            if self.n == 1:
                return {"status": "failed", "model_unreachable": True, "n_judged": 120, "n_passages": 4000,
                        "error": "The served model stopped answering"}
            return super().run_tool(tool, args, ctx)

    built = {}
    monkeypatch.setattr(gw_app, "_build_literature_executor",
                        lambda c, run_id=None, own_gpu=False: built.setdefault("ex", Flaky(c.workspace / run_id)))
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus")
    conn.begin_run(None)
    asyncio.run(gw_app._run_corpus_scan(conn, req))
    assert built["ex"].n == 2
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
            return {"status": "failed", "model_unreachable": True, "n_judged": 0, "n_passages": 4000,
                    "error": "The served model stopped answering"}

    dead = Dead()
    monkeypatch.setattr(gw_app, "_build_literature_executor", lambda c, run_id=None, own_gpu=False: dead)
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus")
    conn.begin_run(None)
    asyncio.run(gw_app._run_corpus_scan(conn, req))
    assert dead.n == gw_app.CORPUS_MAX_ATTEMPTS
    err = next(p for p in pushed if p.get("type") == "chat_error")
    assert "stopped answering" in err["message"]


_QUEUED = {"status": "error", "execution_mode": "hpc_slurm", "queue_timeout": True, "slurm_reason": "Priority",
           "error": "HPC3 analysis job failed: bioagent_corpusscan_x did not get a node within 7200s"}


def test_corpus_scan_submits_again_when_no_card_came_free(tmp_path, monkeypatch):
    """Zian's 2026-10-19 21:13 scan waited out the paid queue (Slurm: Priority) and the whole run
    failed. Now the job is submitted again, the run goes on, and the second job's result is shown."""
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append
    ok = {"status": "ok", "n_papers": 3, "n_sentences": 30, "n_passages": 4, "n_hits_eligible": 1,
          "n_genes": 1, "genes": [{"gene": "PRPF31", "n_papers": 1, "phenotype": "adRP", "flags": []}],
          "genes_flagged_for_review": [], "definition": "d", "warnings": []}

    class Busy(_FakeExecutor):
        startup_timeout_s, queue_windows = 1800, 4

        def __init__(self, ws):
            super().__init__(ws, ok)
            self.n = 0

        def run_tool(self, tool, args, ctx):
            self.n += 1
            return dict(_QUEUED) if self.n == 1 else super().run_tool(tool, args, ctx)

    built = {}
    monkeypatch.setattr(gw_app, "_build_literature_executor",
                        lambda c, run_id=None, own_gpu=False: built.setdefault("ex", Busy(c.workspace / run_id)))
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus")
    conn.begin_run(None)
    asyncio.run(gw_app._run_corpus_scan(conn, req))
    assert built["ex"].n == 2
    retry = [p["text"] for p in pushed if p.get("type") == "lab_progress" and "No GPU card" in p.get("text", "")]
    assert retry and "within 2 h (Slurm: Priority)" in retry[0] and "retry 1 of 2" in retry[0]
    assert not any(p.get("type") == "chat_error" for p in pushed)
    assert [p.get("type") for p in pushed][-1] == "chat_done"


def test_corpus_scan_stops_retrying_a_full_queue_with_a_plain_message(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append

    class Full:
        notify = None
        startup_timeout_s, queue_windows = 1800, 4
        n = 0

        def run_tool(self, tool, args, ctx):
            self.n += 1
            return dict(_QUEUED)

    full = Full()
    monkeypatch.setattr(gw_app, "_build_literature_executor", lambda c, run_id=None, own_gpu=False: full)
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus")
    conn.begin_run(None)
    asyncio.run(gw_app._run_corpus_scan(conn, req))
    assert full.n == 1 + conn.settings.corpus_scan_queue_retries
    err = next(p for p in pushed if p.get("type") == "chat_error")["message"]
    assert "could not start" in err and "3 waits of 2 h" in err and "never ran" in err


def test_the_scan_executor_waits_hours_in_the_queue(tmp_path, monkeypatch):
    conn = _conn(tmp_path, mock=False)
    conn.alloc = type("A", (), {"node": "gpu-1", "port": 1234})()
    monkeypatch.setenv("AISCIENTIST_PAPERQA_ON_HPC", "1")
    monkeypatch.setenv("AISCIENTIST_PAPERQA_IMAGE", "/x/paperqa.sif")
    monkeypatch.setattr(gw_app, "_sync_aiscientist_source_to_hpc", lambda c: "/dfs/pysrc/tester")
    monkeypatch.setattr(gw_app, "_temp_base", lambda c: "/dfs/Temp/tester")
    ex = gw_app._build_literature_executor(conn, run_id="abc123", own_gpu=True)
    st = conn.settings
    assert ex.startup_timeout_s * ex.queue_windows >= st.corpus_scan_queue_wait_s >= 7200
    assert ex.queue_progress is True
    lit = gw_app._build_literature_executor(conn, run_id="abc123")
    assert (lit.startup_timeout_s, lit.queue_windows, lit.queue_progress) == (600, 3, False)


def test_waits_are_said_in_words():
    assert [gw_app._duration_words(s) for s in (1800, 3600, 5400, 7200, 59)] == [
        "30 min", "1 h", "1.5 h", "2 h", "1 min"]


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


# --- the scans kept on the Eye Server --------------------------------------------------------

_OK = {"status": "ok", "n_papers": 1739, "n_sentences": 426706, "n_passages": 1200, "n_requests": 16, "n_gene_requests": 40, "n_judged": 1200,
       "n_hits_eligible": 120, "n_genes": 1,
       "genes": [{"gene": "PRPF31", "n_papers": 3, "phenotype": "adRP", "flags": []}],
       "genes_flagged_for_review": [], "definition": "one copy is not enough", "warnings": []}


def _identity(key):
    return lambda c: (key, {"corpus": {"root": "/dfs/retigene"}, "rules": "r", "model": "m"})


def test_a_finished_scan_is_kept_and_the_next_request_needs_no_job(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append
    monkeypatch.setattr(gw_app, "_corpus_scan_identity", _identity("k1"))
    builds = []

    def fake_build(c, run_id=None, own_gpu=False):
        builds.append(run_id)
        return _FakeExecutor(c.workspace / run_id, _OK)

    monkeypatch.setattr(gw_app, "_build_literature_executor", fake_build)
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus")
    conn.begin_run(None)
    asyncio.run(gw_app._run_corpus_scan(conn, req))
    kept = corpus_scan.load("k1")
    assert kept is not None and kept.meta["run_id"] == builds[0] and kept.meta["model"] == "m"
    assert kept.result["n_genes"] == 1 and (kept.tables / "genes.csv").is_file()
    assert not gw_app._CORPUS_SCANS_RUNNING

    # the same corpus again: answered from the kept scan, no job, the tables in this run's downloads
    pushed.clear()
    conn.begin_run(None)
    asyncio.run(gw_app._run_corpus_scan(conn, req))
    assert len(builds) == 1
    token = next(p for p in pushed if p.get("type") == "chat_token")["token"]
    assert "kept because the RetiGene papers have not changed" in token and "PRPF31" in token
    arts = next(p for p in pushed if p.get("type") == "artifacts")
    assert {"genes.csv", "hits.csv"} <= {a["name"] for a in arts["items"]}
    assert [p.get("type") for p in pushed][-1] == "chat_done"


def test_the_answer_table_has_the_evidence_its_page_and_the_citations(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append
    tables = tmp_path / "t" / "haploinsufficiency"
    tables.mkdir(parents=True)
    with (tables / "genes.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["gene", "n_papers", "flag_garbled", "pmid_1", "quote_1", "pmid_2", "quote_2"])
        w.writerow(["PRPF31", "3", "False", "111", "One copy | is not enough.", "222", "x"])
    (tables / "hits.csv").write_text("pmid,page,quote\n111,4,One copy | is not enough.\n")
    stored = corpus_scan.store("k9", _OK, tables, {"model": "judge-model"})
    req = gw_app.LabRequest(connection_id=conn.id, question="What are haploinsufficient genes?", route="chat")
    conn.begin_run(None)
    asyncio.run(gw_app._run_corpus_scan(conn, req, stored=stored))
    token = next(p for p in pushed if p.get("type") == "chat_token")["token"]
    assert "| gene | evidence (the paper's words) | citations | review flags |" in token
    assert "| PRPF31 | “One copy \\| is not enough.” (p. 4) | [PMID 111](https://pubmed.ncbi.nlm.nih.gov/111/), " \
           "[PMID 222](https://pubmed.ncbi.nlm.nih.gov/222/) | — |" in token
    assert "(by judge-model)" in token


def test_one_scan_of_a_corpus_at_a_time(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append
    monkeypatch.setattr(gw_app, "_corpus_scan_identity", _identity("k3"))
    gw_app._CORPUS_SCANS_RUNNING["k3"] = "run0"

    def never(*a, **k):
        raise AssertionError("a second job for the same corpus")

    monkeypatch.setattr(gw_app, "_build_literature_executor", never)
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus")
    conn.begin_run(None)
    asyncio.run(gw_app._run_corpus_scan(conn, req))
    err = next(p for p in pushed if p.get("type") == "chat_error")
    assert "already running (run run0)" in err["message"]


def test_a_model_that_never_started_is_reported_with_its_log(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    pushed: list[dict] = []
    conn.push = pushed.append

    class NoModel:
        notify = None

        def run_tool(self, tool, args, ctx):
            return {"status": "failed", "model_unreachable": False, "vllm_log": "/s/j.vllm.log",
                    "error": "The scan's model did not start: the server exited before it answered."}

    monkeypatch.setattr(gw_app, "_build_literature_executor", lambda c, run_id=None, own_gpu=False: NoModel())
    monkeypatch.setattr(gw_app, "_vllm_log_tail", lambda c, path: f"CUDA out of memory ({path})")
    req = gw_app.LabRequest(connection_id=conn.id, question="scan", route="corpus")
    conn.begin_run(None)
    errors = []
    conn.emit = lambda level, stage, message, detail=None: errors.append((level, message, detail))
    asyncio.run(gw_app._run_corpus_scan(conn, req))
    err = next(p for p in pushed if p.get("type") == "chat_error")
    assert "did not start" in err["message"]
    assert any(d and "CUDA out of memory (/s/j.vllm.log)" in str(d) for _, _, d in errors)


# --- a chat message asking for the gene list --------------------------------------------------

def test_a_chat_question_for_the_gene_list_is_answered_from_a_kept_scan(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    stored = object()
    seen = {}

    async def fake_corpus(c, req, stored=None):
        seen["stored"] = stored

    async def never_chat(c, req):
        raise AssertionError("the chat ran")

    monkeypatch.setattr(gw_app, "_stored_corpus_scan", lambda c: stored)
    monkeypatch.setattr(gw_app, "_run_corpus_scan", fake_corpus)
    monkeypatch.setattr(gw_app, "_run_quick_chat", never_chat)
    _dispatch(conn, "What are haploinsufficient genes?", route="chat")
    assert seen["stored"] is stored


def test_without_a_kept_scan_dispatches_to_the_gene_table(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    ran = []

    async def table(c, req):
        ran.append(req.question)

    monkeypatch.setattr(gw_app, "_literature_corpus_available", lambda c: True)
    monkeypatch.setattr(gw_app, "_stored_corpus_scan", lambda c: None)
    monkeypatch.setattr(gw_app, "_run_gene_table", table)
    _dispatch(conn, "What are haploinsufficient genes?", route="chat")
    assert ran == ["What are haploinsufficient genes?"]


def test_a_question_about_one_gene_goes_to_the_chat_without_a_lookup(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    ran = []

    async def fake_chat(c, req):
        ran.append(req.question)

    def no_lookup(c):
        raise AssertionError("looked up a kept scan")

    monkeypatch.setattr(gw_app, "_stored_corpus_scan", no_lookup)
    monkeypatch.setattr(gw_app, "_run_quick_chat", fake_chat)
    _dispatch(conn, "Is PRPF31 haploinsufficient?", route="chat")
    assert ran == ["Is PRPF31 haploinsufficient?"]


# --- the job's own GPU ----------------------------------------------------------------------

def test_the_scan_executor_has_its_own_gpu_and_never_the_sessions_model(tmp_path, monkeypatch):
    conn = _conn(tmp_path, mock=False)
    conn.alloc = type("A", (), {"node": "gpu-1", "port": 1234})()      # even a session WITH a model
    monkeypatch.setenv("AISCIENTIST_PAPERQA_ON_HPC", "1")
    monkeypatch.setenv("AISCIENTIST_PAPERQA_IMAGE", "/x/paperqa.sif")
    monkeypatch.setattr(gw_app, "_sync_aiscientist_source_to_hpc", lambda c: "/dfs/pysrc/tester")
    monkeypatch.setattr(gw_app, "_temp_base", lambda c: "/dfs/Temp/tester")
    ex = gw_app._build_literature_executor(conn, run_id="abc123", own_gpu=True)
    st = conn.settings
    assert (ex.partition, ex.account, ex.gres, ex.constraint) == (
        st.corpus_scan_partition, st.corpus_scan_account, st.corpus_scan_gres, st.corpus_scan_constraint)
    assert ex.network is True and ex.live_args is None and callable(ex.job_prelude)
    assert ex.inject_args["model"] == corpus_scan.scan_model(st) and "llm_base_url" not in ex.inject_args
    assert ex.job_prefix == "bioagent_corpusscan" and ex.remote_workspace == "/dfs/Temp/tester/corpus/abc123"
    assert gw_app._classify_session_job("bioagent_corpusscan_extract_haploinsufficient_genes_1") == "gpu"
    # the other literature jobs are unchanged: CPU, the session's model
    lit = gw_app._build_literature_executor(conn, run_id="abc123")
    assert lit.gres == "" and lit.job_prelude is None and lit.inject_args["llm_base_url"] == "http://gpu-1:1234/v1"


@pytest.mark.parametrize("hpc_enabled", [False, True])
def test_gene_list_without_reachable_corpus_uses_quick_chat(tmp_path, monkeypatch, hpc_enabled):
    import threading

    conn = _conn(tmp_path)
    conn.push = lambda payload, **kw: pushed.append(payload)
    pushed, ran, checks = [], [], []
    loop_thread = threading.get_ident()
    if hpc_enabled:
        monkeypatch.setenv("AISCIENTIST_PAPERQA_ON_HPC", "1")
    else:
        monkeypatch.delenv("AISCIENTIST_PAPERQA_ON_HPC", raising=False)
    monkeypatch.setattr(gw_app, "_stored_corpus_scan", lambda c: None)

    def available(c):
        checks.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return False

    async def chat(c, req):
        ran.append(req.question)

    async def no_table(*args):
        raise AssertionError("unreachable corpus used for a table")

    monkeypatch.setattr(gw_app, "_literature_corpus_available", available)
    monkeypatch.setattr(gw_app, "_run_quick_chat", chat)
    monkeypatch.setattr(gw_app, "_run_gene_table", no_table)
    _dispatch(conn, "What are haploinsufficient genes?", route="chat")
    assert ran == ["What are haploinsufficient genes?"] and len(checks) == 1
    notices = [p for p in pushed
               if "Ask about the RetiGene papers by name for the complete table" in p.get("text", "")]
    assert bool(notices) == hpc_enabled
