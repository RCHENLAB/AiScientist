"""Every research request runs with the same options (RCHENLAB/AiScientist issue 21) — offline.

The composer no longer offers Auto / Single agent / Virtual Lab, "Pipelines & skills", "Plan first",
"Bypass" or "Save results to my data": every request starts with a plan the user reviews, no review
gate is skipped, the PI chooses one scientist or a team and the pipeline, and what the run hands on
is saved to the user's data. The gateway fixes these values itself, so a page from before the change
or a script cannot start a run without a plan to review.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

FIXED = {"plan_mode": True, "autonomous": False, "mode": "auto", "save_results": True,
         "preset": None, "presets": [], "skills": [], "preset_prompt": None}
CHOSEN = {"plan_mode": False, "autonomous": True, "mode": "team", "save_results": False,
          "preset": "celltype_annotation", "presets": ["differential_expression"],
          "skills": ["condition_by_celltype"], "preset_prompt": "my own guidance"}


@pytest.fixture(autouse=True)
def _clean_conns(monkeypatch):
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    yield
    gw_app.CONNECTIONS.clear()


def _options(req) -> dict:
    return {k: getattr(req, k) for k in FIXED}


def test_the_options_a_request_carries_are_replaced_by_the_fixed_ones():
    req = gw_app.LabRequest(connection_id="c", question="q", **CHOSEN)
    assert _options(gw_app._fixed_run_options(req)) == FIXED
    plain = gw_app.LabRequest(connection_id="c", question="q")          # a client that sends none
    assert _options(gw_app._fixed_run_options(plain)) == FIXED


def test_a_run_started_through_the_api_gets_the_fixed_options(monkeypatch):
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.status = "ready"
    conn.executor = object()
    gw_app.CONNECTIONS[conn.id] = conn
    started = []

    async def fake_dispatch(c, req):
        started.append(req)

    monkeypatch.setattr(gw_app, "_dispatch_lab", fake_dispatch)
    r = TestClient(gw_app.app).post("/api/lab", json={
        "connection_id": conn.id, "conversation_id": "conv-A", "question": "QC these two samples",
        "route": "research", **CHOSEN})
    assert r.status_code == 200, r.text
    (req,) = started
    assert _options(req) == FIXED
    assert req.question == "QC these two samples" and req.route == "research"
