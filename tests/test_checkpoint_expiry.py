"""Offline tests for the A2 checkpoint auto-expiry: old run checkpoints (work/adata_*.h5ad) are
swept after checkpoint_ttl_days, while artifacts/ (the deliverables) are never touched. Also covers
the /api/lab/continue guard that fails clearly once a run's checkpoints have expired.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway import app as gw_app  # noqa: E402
from bioagent.gateway.settings import HPCSettings  # noqa: E402


def _seed_run(root: Path, owner: str, run_id: str, *, age_days: float) -> Path:
    run = root / owner / run_id
    (run / "work").mkdir(parents=True)
    (run / "artifacts" / "report").mkdir(parents=True)
    ckpt = run / "work" / "adata_clustered.h5ad"
    ckpt.write_bytes(b"x")
    report = run / "artifacts" / "report" / "report.pdf"
    report.write_bytes(b"%PDF")
    old = time.time() - age_days * 86400
    for p in (ckpt, run / "work", report):
        os.utime(p, (old, old))
    return run


def test_expire_removes_old_checkpoints_keeps_recent(tmp_path):
    old = _seed_run(tmp_path, "u", "old", age_days=10)
    recent = _seed_run(tmp_path, "u", "recent", age_days=1)

    removed = gw_app._expire_old_checkpoints(tmp_path, ttl_days=7)

    assert removed == 1
    assert not (old / "work").exists()            # expired checkpoints gone
    assert (recent / "work").exists()             # within TTL — kept
    # artifacts (deliverables) are NEVER touched, even for the expired run
    assert (old / "artifacts" / "report" / "report.pdf").exists()


def test_expire_disabled_when_ttl_zero(tmp_path):
    old = _seed_run(tmp_path, "u", "old", age_days=30)
    assert gw_app._expire_old_checkpoints(tmp_path, ttl_days=0) == 0
    assert (old / "work").exists()


def test_expire_no_root_is_noop(tmp_path):
    assert gw_app._expire_old_checkpoints(tmp_path / "nope", ttl_days=7) == 0


def test_ttl_reads_from_env(monkeypatch):
    monkeypatch.setenv("BIOAGENT_CHECKPOINT_TTL_DAYS", "3")
    assert HPCSettings.from_env().checkpoint_ttl_days == 3


# --- /api/lab/continue guard when checkpoints have expired --------------------

@pytest.fixture(autouse=True)
def _clean_conns():
    yield
    gw_app.CONNECTIONS.clear()


def _conn(tmp_path):
    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    conn.last_run_id = "run123"
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


def _seed_state(conn, run_id="run123", *, with_checkpoint: bool):
    art = conn.workspace / run_id / "artifacts"
    (art / "process").mkdir(parents=True)
    state = {"question": "q", "agenda": ["Run QC", "Cluster", "DE"],
             "rounds": [{"round_no": i, "step_index": i, "step": s, "specialist": "S",
                         "scientist_result": {}, "verdict": {"verdict": "accept", "score": 1, "critique": ""}}
                        for i, s in enumerate(["Run QC", "Cluster", "DE"], 1)],
             "converged": True, "accepted_steps": 3, "final_answer": "r"}
    (art / "process" / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    if with_checkpoint:
        work = conn.workspace / run_id / "work"
        work.mkdir(parents=True)
        (work / "adata_clustered.h5ad").write_bytes(b"x")


def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app)


def test_continue_409_when_checkpoints_expired(tmp_path):
    conn = _conn(tmp_path)
    _seed_state(conn, with_checkpoint=False)          # checkpoints swept away
    r = _client().post("/api/lab/continue", json={"connection_id": conn.id, "from_step_index": 1})
    assert r.status_code == 409
    assert "expired" in r.json()["error"]


def test_continue_from_step_0_ok_without_checkpoints(tmp_path, monkeypatch):
    # Step 0 reads the raw dataset, not a checkpoint — resumable even after expiry.
    conn = _conn(tmp_path)
    _seed_state(conn, with_checkpoint=False)
    def fake(c, req, *, resume=None, resume_run_id=None, resume_decisions=None):
        async def _n():
            return None
        return _n()
    monkeypatch.setattr(gw_app, "_run_lab", fake)
    r = _client().post("/api/lab/continue", json={"connection_id": conn.id, "from_step_index": 0})
    assert r.status_code == 200
