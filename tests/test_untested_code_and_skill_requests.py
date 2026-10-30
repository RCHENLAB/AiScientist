"""Untested code is labelled, and a run that needed it is recorded as a skill request (issue 13).

Issue 13 asked for requests that no tested skill covers to be refused and recorded. The measured
problem was different: a "matched" pipeline still runs code the model writes (every preset pipeline
lists ``run_code``), so matching says nothing about whether tested code ran. What is pinned here:
the label comes from the calls that actually ran (a curated tool is tested, ``run_code`` /
``run_shell`` / ``run_in_environment`` are not), the reply and the reports open with a notice naming
the untested steps, and the run leaves an admin-only record with the code, its outcome and the
Critic's verdict — scrubbed, grouped, with a status and a retention period.
"""
from __future__ import annotations

import importlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aiscientist.agents.code_provenance import (
    NONE, TESTED, UNTESTED, has_untested_code, run_provenance, step_status, untested_notice,
)


def _round(step: str, calls: list[dict], verdict: str = "accept", critique: str = "ok") -> dict:
    """A round as run_state.json stores it (production passes LabRound dataclasses)."""
    return {"step": step, "scientist_result": {"steps": calls},
            "verdict": {"verdict": verdict, "score": 0.8, "critique": critique}}


QC = "**QC** — Run `run_cellqc` on the libraries."
SCORE = "**Score the signature** — adapt `score_signature` via `run_code`."
LOOK = "**Look at the matrix** — read the skill."
# Key-shaped, built here so no scanner of the repository takes the test for a leaked key.
FAKE_KEY = "sk-" + "abcdefghijklmnop1234"
CODE = "import scanpy as sc\nadata = sc.read_h5ad('/dfs3b/ruic20/u/alice/sample.h5ad')\nprint(adata)\n"

ROUNDS = [
    _round(QC, [{"tool": "read_skill_reference", "ok": True, "args": {"name": "x"}},
                {"tool": "run_cellqc", "ok": True, "args": {}, "summary": "4 libraries"}]),
    # a revise, then the accepted attempt: both attempts' code is kept, the last one labels the step
    _round(SCORE, [{"tool": "run_code", "ok": False, "args": {"code": "1/0"},
                    "error": "ZeroDivisionError"}], verdict="revise", critique="it crashed"),
    _round(SCORE, [{"tool": "run_code", "ok": True, "args": {"code": CODE}, "summary": "scored 900 cells",
                    "result": {"artifacts": ["tables/scores.csv"]}},
                   {"tool": "finish", "ok": True, "args": {}}]),
    _round(LOOK, [{"tool": "read_skill_reference", "ok": True, "args": {}}]),
]


def _code(code: str, **result) -> dict:
    return {"tool": "run_code", "ok": True, "args": {"code": code}, **({"result": result} if result else {})}


def test_a_step_is_labelled_by_the_calls_that_ran_not_by_what_it_matched():
    assert step_status([{"tool": "run_de", "ok": True}]) == TESTED
    # code alone produced the step's result
    assert step_status([_code("print(adata.shape)")]) == UNTESTED
    assert step_status([{"tool": "run_in_environment", "ok": True, "args": {"command": "cellqc"}}]) == UNTESTED
    # code that wrote a table after a tool: part of the result came from the code
    assert step_status([{"tool": "run_de", "ok": True}, _code("df.to_csv('x.csv')")]) == UNTESTED
    assert step_status([{"tool": "run_de", "ok": True},
                        _code("x", execution_mode="hpc_slurm", tables=["tables/x.csv"])]) == UNTESTED
    # a failed code attempt that a tool then replaced: the result is the tool's
    assert step_status([{"tool": "run_code", "ok": False}, {"tool": "run_de", "ok": True}]) == TESTED
    assert step_status([{"tool": "list_dir", "ok": True}, {"tool": "finish", "ok": True}]) == NONE


def test_code_that_only_checks_a_tested_tools_result_keeps_the_step_tested():
    """Production, 2026-10-19: every CellQC step read the tool's output once more — `cat
    qc_status.csv` with run_shell, or the QC checkpoint's shape with run_code. Those runs used only
    the tested tool, so they get no notice and are no skill request."""
    cellqc = [{"tool": "inspect_dataset", "ok": True}, {"tool": "run_cellqc", "ok": True},
              {"tool": "run_shell", "ok": True, "args": {"command": "cd out/result && cat qc_status.csv"},
               "result": {"execution_mode": "hpc_slurm", "status": "ok"}},
              _code("import scanpy as sc\na = sc.read_h5ad(os.path.join(os.environ['AISCIENTIST_WORK'], "
                    "'adata_qc.h5ad'))\nprint(a.shape)")]
    assert step_status(cellqc) == TESTED
    prov = run_provenance([_round("**CellQC**", cellqc)])
    assert prov["tested_accepted"] == ["**CellQC**"] and prov["code_attempts"] == 2
    assert not has_untested_code(prov) and untested_notice(prov, recorded=True) == ""
    assert [c["wrote"] for c in prov["steps"][0]["code_calls"]] == [False, False]


def test_the_run_record_keeps_every_attempt_and_labels_each_step_by_its_last():
    prov = run_provenance(ROUNDS)
    steps = {s["step"]: s for s in prov["steps"]}
    assert list(steps) == [QC, SCORE, LOOK]
    assert steps[QC]["status"] == TESTED and steps[QC]["tools"] == ["run_cellqc"]
    score = steps[SCORE]
    assert score["status"] == UNTESTED and score["accepted"] and score["attempts"] == 2
    assert [c["ok"] for c in score["code_calls"]] == [False, True]
    assert score["code_calls"][0]["error"] == "ZeroDivisionError"
    assert score["code_calls"][1]["files"] == ["tables/scores.csv"]
    assert steps[LOOK]["status"] == NONE
    assert prov["untested_accepted"] == [SCORE] and prov["tested_accepted"] == [QC]
    assert prov["accepted_steps"] == 3 and prov["code_attempts"] == 2 and has_untested_code(prov)


def test_the_notice_names_the_untested_steps_and_is_empty_when_tools_did_everything():
    prov = run_provenance(ROUNDS)
    note = untested_notice(prov, recorded=True)
    assert note.startswith("> **Untested code.** 1 of the 3 completed steps")
    assert "*Score the signature*" in note and "**" not in note.split("**Untested code.**")[1]
    assert "process/untested_code.md" in note
    assert "recorded" in note
    assert "recorded" not in untested_notice(prov, recorded=False)
    only_tools = run_provenance([ROUNDS[0]])
    assert untested_notice(only_tools, recorded=True) == "" and not has_untested_code(only_tools)
    # a failed attempt alone puts nothing in the reply, but the run is still a skill request
    failed = run_provenance([ROUNDS[1]])
    assert untested_notice(failed, recorded=True) == "" and has_untested_code(failed)


# --- the store -----------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AISCIENTIST_SKILL_REQUESTS", raising=False)
    monkeypatch.delenv("AISCIENTIST_SKILL_REQUEST_RETENTION_DAYS", raising=False)
    from aiscientist.gateway import skill_requests
    return skill_requests


def _record(store, run_id="run000000001", account="alice", question=None):
    return store.build_record(
        run_id=run_id, account=account,
        question=question or ("Score my retina cells for the phototransduction signature. "
                              f"Key {FAKE_KEY}, mail alice@uci.edu, MRN 12345678."),
        provenance=run_provenance(ROUNDS), conversation_id="chat-1",
        earlier_requests=["Run CellQC on /data/aiscientist/data/users/alice/datasets/x/original"],
        datasets=[{"path": "/dfs3b/ruic20/u/alice/sample.h5ad", "kind": "h5ad", "size_bytes": 10}],
        pipelines=["gene_signature_scoring"], converged=True,
        claim_audit={"claims": [{"text": "a"}, {"text": "b"}]}, induced_skills=["score_phototransduction"])


def test_a_record_keeps_what_the_team_needs_and_scrubs_what_it_must_not():
    store = pytest.importorskip("aiscientist.gateway.skill_requests")
    rec = _record(store)
    flat = json.dumps(rec)
    assert FAKE_KEY not in flat and "alice@uci.edu" not in flat
    assert "12345678" not in flat
    assert "/dfs3b/" not in flat and "/data/aiscientist" not in flat, "server paths cut to their last part"
    assert rec["datasets"] == [{"name": "sample.h5ad", "kind": "h5ad", "size_bytes": 10}]
    assert rec["pipelines"] == ["gene_signature_scoring"]
    assert rec["outcome"] == {"converged": True, "accepted_steps": 3, "untested_accepted": 1,
                              "tested_accepted": 1, "code_attempts": 2, "code_failures": 1,
                              "claims_checked": 2}
    score = [s for s in rec["steps"] if s["step"] == SCORE][0]
    assert "sc.read_h5ad('sample.h5ad')" in score["code_calls"][1]["code"]
    assert score["critique"] == "ok" and rec["induced_skills"] == ["score_phototransduction"]
    assert rec["status"] == "new" and rec["id"] == "run000000001"


def test_records_are_private_files_listed_grouped_and_moved_along(store):
    for i, (account, pipe) in enumerate([("alice", "gene_signature_scoring"),
                                         ("bob", "gene_signature_scoring"), ("alice", "")]):
        rec = _record(store, run_id=f"run{i:09d}", account=account)
        rec["pipelines"] = [pipe] if pipe else []
        rec["summary"] = {"one_line": f"request {i}", "category": "gene signature scoring",
                          "closest_skill": pipe}
        path = store.save(rec)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    rows = store.listing()
    assert len(rows) == 3 and "code_calls" not in json.dumps(rows), "the listing carries no code"
    groups = store.groups(rows)
    assert (groups[0]["closest_skill"], groups[0]["requests"], groups[0]["accounts"]) == \
        ("gene_signature_scoring", 2, 2), "the group more accounts asked for comes first"
    rec = store.set_status("run000000001", "planned", by="root", note="next sprint")
    assert rec["status"] == "planned" and rec["status_history"][0]["by"] == "root"
    assert [r["id"] for r in store.listing(status="planned")] == ["run000000001"]
    assert store.listing(q="request 2")[0]["id"] == "run000000002"
    with pytest.raises(ValueError):
        store.set_status("run000000001", "maybe", by="root")
    with pytest.raises(KeyError):
        store.set_status("../etc/passwd", "planned", by="root")


def test_past_the_retention_period_the_raw_parts_go_and_the_summary_stays(store):
    rec = _record(store)
    rec["summary"] = {"one_line": "signature scoring", "category": "gene signature scoring"}
    rec["created_at"] = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    store.save(rec)
    kept = store.load(rec["id"])
    assert kept["raw_expired"] and kept["request"] == "" and kept["earlier_requests"] == []
    assert all("code" not in c for s in kept["steps"] for c in s["code_calls"])
    assert kept["summary"]["one_line"] == "signature scoring" and kept["outcome"]["code_attempts"] == 2
    assert store.listing()[0]["raw_expired"] is True


def test_the_summary_comes_from_the_cheap_model_and_falls_back_to_code(store):
    rec = _record(store)
    choices = ["gene_signature_scoring", "run_de"]
    seen = {}

    def model(messages, *, role=None):
        seen["role"] = role
        return ('Here it is: {"one_line": "Score cells", "goal": "g", "inputs": ["h5ad"], '
                '"outputs": ["table"], "methods": "AUCell", "category": "Gene Signature Scoring", '
                '"closest_skill": "gene_signature_scoring", "missing": "AUCell scoring"}')

    summary, by = store.summarize(rec, model, choices)
    assert by == "model" and seen["role"] == "classify"
    assert summary["category"] == "gene signature scoring" and summary["methods"] == ["AUCell"]
    assert summary["closest_skill"] == "gene_signature_scoring"

    summary, _ = store.summarize(rec, lambda m, role=None: '{"category": "telepathy", '
                                 '"closest_skill": "made_up_tool"}', choices)
    assert summary["category"] == "other" and summary["closest_skill"] == "", \
        "a category or skill outside the lists is not invented"
    summary, by = store.summarize(rec, lambda m, role=None: "not json", choices)
    assert by == "code" and summary["closest_skill"] == "gene_signature_scoring"
    summary, by = store.summarize(rec, None, choices)
    assert by == "code"


def test_recording_can_be_turned_off_and_needs_a_private_place(store, monkeypatch):
    assert store.enabled()
    monkeypatch.setenv("AISCIENTIST_SKILL_REQUESTS", "0")
    assert not store.enabled()
    monkeypatch.delenv("AISCIENTIST_SKILL_REQUESTS")
    monkeypatch.delenv("AISCIENTIST_STATE_DIR")
    assert not store.enabled(), "no state directory: never a skill_requests/ in the working directory"


# --- the gateway's part ----------------------------------------------------------------------------


def test_the_gateway_writes_the_code_file_and_the_record(store):
    app = pytest.importorskip("aiscientist.gateway.app")
    prov = run_provenance(ROUNDS)
    md = app._untested_code_md(prov)
    assert md.startswith("# Code written during this run")
    assert "**Attempt 1** · `run_code` · failed" in md and "ZeroDivisionError" in md
    assert CODE.strip().splitlines()[1] in md, "the user's own bundle keeps the code as it ran"
    conn = SimpleNamespace(owner="alice")
    req = SimpleNamespace(question="Score my cells", conversation_id="chat-9")
    result = SimpleNamespace(converged=True, claim_audit={})
    lab = SimpleNamespace(protocols=[{"key": "gene_signature_scoring", "label": "x"}],
                          induced_skills=["score_x"],
                          scientist=SimpleNamespace(catalog=[SimpleNamespace(name="run_de"),
                                                             SimpleNamespace(name="run_code")]))
    decisions = {"datasets": [{"path": "/dfs3b/x/sample.h5ad", "kind": "h5ad"}],
                 "content_modality": "scrna"}
    rec = app._save_skill_request(conn, "abc123def456", req, result, lab, decisions,
                                  {"session": [{"question": "Run CellQC"}]}, prov)
    saved = store.load("abc123def456")
    assert saved == rec and saved["account"] == "alice" and saved["conversation_id"] == "chat-9"
    assert saved["earlier_requests"] == ["Run CellQC"] and saved["induced_skills"] == ["score_x"]
    assert saved["datasets"][0]["content_modality"] == "scrna"
    app._summarize_skill_request(rec, lambda m, role=None: '{"one_line": "Score cells", '
                                 '"category": "gene signature scoring", "closest_skill": "run_de"}', lab)
    filled = store.load("abc123def456")
    assert filled["summary_by"] == "model" and filled["summary"]["closest_skill"] == "run_de"


def test_the_bundles_code_file_carries_no_server_path_and_no_key(store, monkeypatch):
    """process/untested_code.md is in the bundle the user downloads and shares: cleaned like the
    event log (issues 16 and 37). An HPC3 path is the user's own and stays."""
    app = pytest.importorskip("aiscientist.gateway.app")
    from pathlib import Path
    home = str(Path.home())
    leaky = (f"df = pd.read_csv('{home}/secret/run_state.json')\nkey = '{FAKE_KEY}'\n"
             "adata = sc.read_h5ad('/dfs3b/ruic20/u/alice/sample.h5ad')\n")
    prov = run_provenance([_round(SCORE, [
        {"tool": "run_code", "ok": True, "args": {"code": leaky}, "result": {"artifacts": ["tables/x.csv"]}},
        {"tool": "run_code", "ok": False, "args": {"code": "1/0"},
         "error": f'Traceback:\n  File "{home}/app/x.py", line 1\nZeroDivisionError'}])])
    md = app._untested_code_md(prov)
    assert home not in md and FAKE_KEY not in md
    assert "/dfs3b/ruic20/u/alice/sample.h5ad" in md and "run_state.json" in md


def test_the_summary_is_the_runs_own_classify_call_and_never_asks_about_the_budget(store, monkeypatch):
    """The summary goes through the run's complete_fn on the classify role, is labelled in the usage
    record, offers the pinned release's vetted skills as the closest match, and a reached budget is
    refused for it rather than asked about: nobody is waiting for a team-only summary."""
    app = pytest.importorskip("aiscientist.gateway.app")
    from aiscientist.agents import skills as skills_mod
    monkeypatch.setitem(skills_mod.SKILLS, "score_signature", SimpleNamespace(name="score_signature"))
    rec = _record(store, run_id="sum000000001")
    store.save(rec)
    seen: dict = {}

    def model(messages, *, role=None):
        seen.update(role=role, step=app._USAGE_STEP.get(), ask_off=app._BUDGET_ASK_OFF.get(),
                    payload=json.loads(messages[1]["content"]))
        return '{"one_line": "Score cells", "category": "gene signature scoring", "closest_skill": "score_signature"}'

    lab = SimpleNamespace(scientist=SimpleNamespace(catalog=[]))
    app._summarize_skill_request(rec, model, lab)
    assert seen["role"] == "classify" and seen["step"] == "Skill request summary" and seen["ask_off"]
    assert "score_signature" in seen["payload"]["existing_pipelines_skills_and_tools"]
    assert store.load("sum000000001")["summary"]["closest_skill"] == "score_signature"
    assert app._USAGE_STEP.get() is None and app._BUDGET_ASK_OFF.get() is False, "nothing leaks out"

    # the budget question is not put to the user for it
    asked = []
    monkeypatch.setattr(app, "_hitl_confirm", lambda conn, run: (lambda req: asked.append(req) or True))
    conn = SimpleNamespace(owner="alice", app_user_id=1, active_run=SimpleNamespace(kind="research"))
    use = app._key_use(conn, {"id": "k1", "key_fingerprint": "fp", "label": "key"})
    token = app._BUDGET_ASK_OFF.set(True)
    try:
        assert use.confirm("over the limit", "x") is None and asked == []
    finally:
        app._BUDGET_ASK_OFF.reset(token)
    assert use.confirm("over the limit", "x") is True and len(asked) == 1, "a run's own calls still ask"


def test_a_record_names_the_vetted_skills_behind_its_code(store):
    """A request that needed code although the plan gave the step a vetted skill says that skill
    needs work: the record keeps the skills, from the approved plan and from the code calls."""
    app = pytest.importorskip("aiscientist.gateway.app")
    from aiscientist.agents.plan_doc import PlanDoc, PlanStep
    doc = PlanDoc(request="Score my cells", steps=(
        PlanStep(id="s1", text="**QC** — Run CellQC.", tool="run_cellqc"),
        PlanStep(id="s2", text="**Score the signature** — Adapt the skill.", tool="run_code",
                 skill="score_signature")))
    prov = run_provenance([_round(SCORE, [
        {"tool": "run_in_environment", "ok": True, "args": {"command": "cellqc --x"},
         "result": {"status": "ok", "skill": "cellqc_env", "tables": ["tables/q.csv"]}}])])
    lab = SimpleNamespace(protocols=[], induced_skills=[], approved_plan_yaml=doc.to_yaml())
    assert app._skill_request_skills(lab, prov) == ["score_signature", "cellqc_env"]
    rec = store.build_record(run_id="sk0000000001", account="a", question="q", provenance=prov,
                             skills=app._skill_request_skills(lab, prov))
    assert rec["skills"] == ["score_signature", "cellqc_env"]
    assert store.fallback_summary(rec)["closest_skill"] == "score_signature"
    store.save(rec)
    assert store.listing()[0]["skills"] == ["score_signature", "cellqc_env"]


# --- the admin API -------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch, store):
    for mod in ("bcrypt", "itsdangerous", "sqlalchemy", "httpx"):
        pytest.importorskip(mod)
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("AISCIENTIST_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AISCIENTIST_ADMIN_USER", "root")
    monkeypatch.setenv("AISCIENTIST_ADMIN_PASSWORD", "rootpass1")
    from aiscientist.gateway import auth, auth_routes, db, models  # noqa: F401
    for mod in (db, models, auth, auth_routes):
        importlib.reload(mod)
    db.reset(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    db.init_db()
    auth_routes.ensure_bootstrap_admin()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(auth_routes.router)
    return TestClient(app)


def test_only_an_admin_can_read_or_move_skill_requests(client, store):
    rec = _record(store)
    rec["summary"] = {"one_line": "Score cells", "category": "gene signature scoring",
                      "closest_skill": "gene_signature_scoring"}
    store.save(rec)
    assert client.get("/api/admin/skill-requests").status_code == 401
    assert client.post("/api/auth/login", json={"username": "root", "password": "rootpass1"}).status_code == 200
    client.post("/api/admin/users", json={"username": "bob", "password": "bobpass1", "role": "user"})
    j = client.get("/api/admin/skill-requests").json()
    assert [r["id"] for r in j["requests"]] == [rec["id"]] and j["counts"]["new"] == 1
    assert j["groups"][0]["requests"] == 1 and j["recording"] is True and j["retention_days"] == 365
    detail = client.get(f"/api/admin/skill-requests/{rec['id']}").json()
    assert detail["steps"][1]["code_calls"][1]["code"].startswith("import scanpy")
    assert client.get("/api/admin/skill-requests/nope").status_code == 404
    r = client.post(f"/api/admin/skill-requests/{rec['id']}/status", json={"status": "planned"})
    assert r.status_code == 200 and r.json()["request_status"] == "planned"
    assert client.post(f"/api/admin/skill-requests/{rec['id']}/status",
                       json={"status": "maybe"}).status_code == 400
    assert client.get("/api/admin/skill-requests?status=new").json()["requests"] == []

    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "bob", "password": "bobpass1"})
    assert client.get("/api/admin/skill-requests").status_code == 403
    assert client.get(f"/api/admin/skill-requests/{rec['id']}").status_code == 403


# --- a whole run through the gateway ---------------------------------------------------------------


def test_a_run_whose_code_wrote_results_says_so_and_leaves_a_request(tmp_path, monkeypatch):
    """Prod run 1e887ed2bf8e's shape: CellQC, then an "ambient rod-leak audit" the AI wrote with
    run_code, which wrote a table. The reply and the short report open with the notice, the code is
    in process/untested_code.md, and the run is a skill request with its summary."""
    pytest.importorskip("fastapi")
    import asyncio

    import test_qc_only_chat_reply as qc
    from aiscientist.agents import research_lab as rl
    from aiscientist.agents.research_lab import CriticVerdict, LabResult, LabRound
    from aiscientist.gateway import app as gw_app
    from aiscientist.reporting import report as reporting
    from aiscientist.tools import api as tools_api

    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AISCIENTIST_SKILL_REQUESTS", raising=False)
    monkeypatch.delenv("AISCIENTIST_PLAN_YAML", raising=False)
    audit = "**Ambient rod-leak audit** — With `run_code`, measure rod transcripts in non-rod cells."
    leak_code = "df = rod_leak(adata)\ndf.to_csv(os.path.join(os.environ['AISCIENTIST_ARTIFACTS'], 'tables/rod_leak.csv'))\n"

    class _Lab:
        def __init__(self, ctx, config, **_k):
            self.guidance, self.protocols, self.self_sourced = None, [], ""
            self.execution_profile, self.later, self.proposed_steps = {}, [], []
            self.request_breadth, self.induced_skills = "specific", []
            self.scientist = type("S", (), {"catalog": []})()

        def run(self, question, on_event=None, plan_review=None, **_kw):
            r1 = LabRound(1, 0, qc._STEP, "Scientist", {"final_answer": "QC done", "steps": [
                {"tool": "run_cellqc", "ok": True, "args": {}, "result": qc._CELLQC}]},
                CriticVerdict("accept", 0.8, ""))
            r2 = LabRound(2, 1, audit, "Scientist", {"final_answer": "3% rod reads", "steps": [
                {"tool": "run_code", "ok": True, "args": {"code": leak_code}, "summary": "3% rod reads",
                 "result": {"status": "ok", "tables": ["tables/rod_leak.csv"]}}]},
                CriticVerdict("accept", 0.7, "plausible"))
            return LabResult(question, [qc._STEP, audit], [r1, r2], True, 2, "QC and the audit are done.")

    rendered: list[str] = []

    def _render(md, out_dir, **_k):
        rendered.append(md)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.md").write_text(md, encoding="utf-8")
        return {"status": "markdown_only", "md_path": str(out_dir / "report.md")}

    def _schematic(agenda, out_dir, **_k):
        out_dir.mkdir(parents=True, exist_ok=True)
        return {"status": "dependency_missing"}

    def _not_this(*_a, **_k):
        raise AssertionError("a short report only")

    monkeypatch.setattr(rl, "ResearchLab", _Lab)
    monkeypatch.setattr(gw_app, "_build_brief_report", lambda *a, **k: "# Short report\n\nQC kept 14,553 cells.\n")
    for name in ("_build_report", "_review_and_finalize_report", "_build_technical_report"):
        monkeypatch.setattr(gw_app, name, _not_this)
    monkeypatch.setattr(reporting, "build_pdf_report", _render)
    monkeypatch.setattr(tools_api, "render_workflow_schematic", _schematic)
    conn = qc._conn(tmp_path)
    payloads: list[dict] = []
    monkeypatch.setattr(conn, "_publish", lambda payload: payloads.append(payload))
    req = gw_app.LabRequest(connection_id=conn.id, conversation_id="conv-1",
                            question="QC this sample and check for ambient rod leakage.")
    conn.begin_run("conv-1")
    try:
        asyncio.run(gw_app._run_lab(conn, req))
    finally:
        # the fake connection must not outlive the test: later tests would find it and use it
        gw_app.CONNECTIONS.pop(conn.id, None)
    run_id = conn.last_run_by_conversation["conv-1"]
    art = conn.workspace / run_id / "artifacts"

    reply = qc._reply(payloads)
    assert reply.startswith("> **Untested code.** 1 of the 2 completed steps"), reply[:300]
    assert "*Ambient rod-leak audit*" in reply and "recorded" in reply
    assert rendered and "> **Untested code.**" in rendered[0], "the report opens with it too"
    code_md = (art / "process" / "untested_code.md").read_text(encoding="utf-8")
    assert "## 1. Ambient rod-leak audit" in code_md and "rod_leak.csv" in code_md
    from aiscientist.gateway import skill_requests
    rec = skill_requests.load(run_id)
    assert rec is not None and rec["account"] == "tester" and rec["conversation_id"] == "conv-1"
    assert rec["outcome"]["untested_accepted"] == 1 and rec["outcome"]["tested_accepted"] == 1
    assert rec["summary"] and rec["summary_by"] in ("model", "code")
