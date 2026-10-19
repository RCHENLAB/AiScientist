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

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402


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


def _seed_stalled(root: Path, run_id: str, *, copy_age_days: float, run_age_days: float) -> Path:
    """A run that never published: a staged input copy, a loop log, a stall note, no report."""
    run = root / "u" / run_id
    (run / "staged").mkdir(parents=True)
    matrix = run / "staged" / "input.h5ad"
    matrix.write_bytes(b"x" * 64)
    log = run / "loop.log"
    log.write_text("step 1\n", encoding="utf-8")
    note = run / "checkpoint_stalled.md"
    note.write_text("stalled\n", encoding="utf-8")
    # the copy keeps its source's time (as a transfer that preserves times would leave it)
    source_time = time.time() - 400 * 86400
    os.utime(matrix, (source_time, source_time))
    copied = time.time() - copy_age_days * 86400
    os.utime(run / "staged", (copied, copied))
    last = time.time() - run_age_days * 86400
    for p in (log, note):
        os.utime(p, (last, last))
    return run


def test_a_stalled_runs_staged_copy_expires(tmp_path):
    run = _seed_stalled(tmp_path, "stalled", copy_age_days=12, run_age_days=10)
    assert gw_app._expire_old_checkpoints(tmp_path, ttl_days=7) == 1
    assert not (run / "staged").exists()
    assert (run / "loop.log").exists() and (run / "checkpoint_stalled.md").exists()


def test_a_staged_copy_with_an_old_file_time_stays_while_its_run_is_recent(tmp_path):
    # file time 400 days old (the source's), but the run wrote its log yesterday
    active = _seed_stalled(tmp_path, "active", copy_age_days=12, run_age_days=1)
    # and a copy that arrived an hour ago, in a run whose other files are old
    fresh = _seed_stalled(tmp_path, "fresh", copy_age_days=0.04, run_age_days=30)
    assert gw_app._expire_old_checkpoints(tmp_path, ttl_days=7) == 0
    assert (active / "staged" / "input.h5ad").exists()
    assert (fresh / "staged" / "input.h5ad").exists()


def test_a_live_run_is_never_swept(tmp_path):
    old = _seed_run(tmp_path, "u", "running", age_days=30)
    _seed_stalled(tmp_path, "running2", copy_age_days=30, run_age_days=30)
    assert gw_app._expire_old_checkpoints(tmp_path, ttl_days=7, live={"running", "running2"}) == 0
    assert (old / "work").exists() and (tmp_path / "u" / "running2" / "staged").exists()


def test_the_sweep_skips_live_runs_of_every_connection(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    monkeypatch.setattr(gw_app, "_expire_old_checkpoints",
                        lambda root, ttl, *, live=(): seen.update(root=root, live=set(live)) or 0)
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="u")
    conn.live_runs.append(gw_app.RunState(conn, run_id="abc123"))
    gw_app.CONNECTIONS[conn.id] = conn

    async def once():
        task = asyncio.create_task(gw_app._checkpoint_gc_loop())
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.01)
        task.cancel()

    asyncio.run(once())
    assert seen == {"root": tmp_path, "live": {"abc123"}}


def test_ttl_reads_from_env(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_CHECKPOINT_TTL_DAYS", "3")
    assert HPCSettings.from_env().checkpoint_ttl_days == 3


# --- /api/lab/continue guard when checkpoints have expired --------------------

@pytest.fixture(autouse=True)
def _clean_conns(monkeypatch):
    # The single-user console (accounts off): who may call these routes with accounts on is
    # pinned in tests/test_connection_ownership.py.
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
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
