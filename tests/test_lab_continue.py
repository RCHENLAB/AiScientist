"""Offline tests for the A2 continuation endpoint /api/lab/continue: re-run a changed step from a
prior run using its persisted run_state.json. The heavy _run_lab pipeline is monkeypatched to a
capturing stub, so this validates the endpoint's routing + ResumeState construction only.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway import app as gw_app  # noqa: E402
from bioagent.gateway.settings import HPCSettings  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_conns():
    yield
    gw_app.CONNECTIONS.clear()


def _conn(tmp_path, **attrs):
    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()          # a live-ish session (endpoint only checks it's not None)
    for k, v in attrs.items():
        setattr(conn, k, v)
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


def _seed_state(conn, run_id="run123"):
    art = conn.workspace / run_id / "artifacts"
    (art / "process").mkdir(parents=True)
    def _round(i, step):
        return {"round_no": i, "step_index": i, "step": step, "specialist": "S",
                "scientist_result": {"final_answer": f"did {step}"},
                "verdict": {"verdict": "accept", "score": 0.9, "critique": "ok"}}
    state = {
        "question": "characterize the dataset",
        "agenda": ["Run QC", "Cluster the cells", "Differential expression"],
        "rounds": [_round(1, "Run QC"), _round(2, "Cluster the cells"), _round(3, "Differential expression")],
        "converged": True, "accepted_steps": 3, "final_answer": "prior report",
        "guidance": "SKILL_GUIDANCE", "dataset_path": "/data/a.h5ad",
    }
    (art / "process" / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    # Analysis checkpoint the resumed step reads (present = not expired), so a from_step_index>0
    # continue clears the expiry guard.
    work = conn.workspace / run_id / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "adata_clustered.h5ad").write_bytes(b"x")
    return art


def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app)


# --- validation --------------------------------------------------------------

def test_continue_unknown_connection():
    assert _client().post("/api/lab/continue", json={"connection_id": "nope"}).status_code == 404


def test_continue_409_when_not_ready(tmp_path):
    conn = _conn(tmp_path, last_run_id="run123", status="connecting", executor=None)
    _seed_state(conn)
    assert _client().post("/api/lab/continue", json={"connection_id": conn.id}).status_code == 409


def test_continue_409_when_running(tmp_path):
    conn = _conn(tmp_path, last_run_id="run123", chat_running=True)
    _seed_state(conn)
    assert _client().post("/api/lab/continue", json={"connection_id": conn.id}).status_code == 409


def test_continue_404_when_no_run_state(tmp_path):
    conn = _conn(tmp_path, last_run_id="ghost")
    assert _client().post("/api/lab/continue", json={"connection_id": conn.id}).status_code == 404


# --- dispatch ----------------------------------------------------------------

def _capture_run_lab(captured):
    def fake(c, req, *, resume=None, resume_run_id=None, resume_decisions=None):
        captured.update(question=req.question, resume=resume,
                        run_id=resume_run_id, decisions=resume_decisions)
        async def _noop():
            return None
        return _noop()
    return fake


def test_continue_builds_resume_and_dispatches(tmp_path, monkeypatch):
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_state(conn)
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_run_lab", _capture_run_lab(cap))

    r = _client().post("/api/lab/continue", json={
        "connection_id": conn.id, "run_id": "run123",
        "from_step_index": 1, "modify_note": "re-cluster at resolution 1.0"})

    assert r.status_code == 200, r.text
    assert r.json()["from_step"] == 2                      # 1-based echo
    assert cap["run_id"] == "run123"                      # reuses the same run
    assert cap["question"] == "characterize the dataset"  # from state, not the client
    assert cap["decisions"]["dataset_path"] == "/data/a.h5ad"
    resume = cap["resume"]
    assert resume.from_step_index == 1
    assert resume.modify_note == "re-cluster at resolution 1.0"
    assert resume.guidance == "SKILL_GUIDANCE"
    # all accepted rounds are carried; the lab decides per step whether to reuse or re-run
    assert [rd.step for rd in resume.prior_rounds] == ["Run QC", "Cluster the cells", "Differential expression"]


def test_continue_edited_step_replaces_agenda_text(tmp_path, monkeypatch):
    conn = _conn(tmp_path, last_run_id="run123")
    _seed_state(conn)
    cap: dict = {}
    monkeypatch.setattr(gw_app, "_run_lab", _capture_run_lab(cap))

    _client().post("/api/lab/continue", json={
        "connection_id": conn.id, "from_step_index": 1,
        "edited_step": "Cluster the cells at resolution 1.0"})

    resume = cap["resume"]
    assert resume.agenda[1] == "Cluster the cells at resolution 1.0"   # step text replaced
    assert resume.agenda[0] == "Run QC" and resume.agenda[2] == "Differential expression"
