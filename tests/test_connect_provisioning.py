"""One connection lifecycle: /api/connect brings SSH and the GPU/vLLM up TOGETHER.

The gateway used to be able to defer GPU provisioning to the first run (``BIOAGENT_LAZY_GPU``),
which left a half-connected session — SSH up, no model — that both the run endpoints and the
console had to special-case. That path is gone: ``_provision_blocking`` is the only way a session
comes up, status walks connecting → provisioning → ready, and a run may only start from "ready".

These cover the wiring (SSH phase, then GPU phase, both inside one call) with the GPU half stubbed,
so nothing here depends on the full vLLM mock chain.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway import app as gw_app  # noqa: E402
from bioagent.gateway.settings import HPCSettings  # noqa: E402


def _conn():
    return gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")


def _req():
    return gw_app.ConnectRequest(ucinetid="tester", mock=True, campus_network_confirmed=True)


def test_ssh_phase_never_publishes_a_usable_half_connected_session():
    """The SSH phase logs in but must NOT declare the session usable: no GPU, and the status
    stays 'connecting' so no client can treat SSH-only as a state it can run from."""
    conn = _conn()
    gw_app._ssh_connect_blocking(conn, _req())
    assert conn.executor is not None          # SSH session exists (uploads/storage use it)
    assert conn.alloc is None                 # the SSH phase must NOT allocate a GPU
    assert conn.status == "connecting"        # ... and must NOT advertise readiness


def test_connect_provisions_ssh_and_gpu_together(monkeypatch):
    """_provision_blocking is the ONE path: SSH first, GPU immediately after, in one call."""
    conn = _conn()
    calls: list = []

    def fake_gpu(c):
        assert c.executor is not None         # GPU phase runs AFTER the SSH login
        calls.append(c)
        c.alloc = object()
        c.status = "ready"

    monkeypatch.setattr(gw_app, "_provision_gpu_blocking", fake_gpu)
    gw_app._provision_blocking(conn, _req())
    assert conn.executor is not None and conn.alloc is not None
    assert conn.status == "ready"
    assert calls == [conn]                    # GPU phase invoked exactly once, after SSH


def test_no_deferred_gpu_provisioning_entry_points():
    """Regression guard for the removal: nothing may reintroduce a deferred-provisioning entry
    point (the old ``_ensure_gpu_ready_blocking`` / ``POST /api/connect/gpu`` / ``lazy_gpu``)."""
    assert not hasattr(gw_app, "_ensure_gpu_ready_blocking")
    assert not hasattr(gw_app, "connect_gpu")
    paths = {getattr(r, "path", None) for r in gw_app.app.routes}
    assert "/api/connect/gpu" not in paths
    assert not hasattr(HPCSettings(), "lazy_gpu")


def test_lab_endpoint_starts_a_run_on_a_ready_session(monkeypatch):
    conn = _conn()
    gw_app._ssh_connect_blocking(conn, _req())
    conn.alloc = object()
    conn.status = "ready"
    gw_app.CONNECTIONS[conn.id] = conn

    started: list = []

    async def fake_run_lab(c, r):
        started.append(c.id)

    monkeypatch.setattr(gw_app, "_run_lab", fake_run_lab)
    req = gw_app.LabRequest(connection_id=conn.id, question="hello")

    async def call():
        resp = await gw_app.lab(req)
        await asyncio.sleep(0)          # let the created _run_lab task run
        return resp

    try:
        resp = asyncio.run(call())
        assert resp.status_code == 200
        assert started == [conn.id]     # the run actually kicked off (didn't 409)
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)


def test_lab_endpoint_rejects_a_session_still_coming_up():
    """A session that logged in over SSH but hasn't finished provisioning has no model to run
    against — 409 instead of silently blocking the caller on a ~10-min A100 spin-up."""
    conn = _conn()
    gw_app._ssh_connect_blocking(conn, _req())
    assert conn.executor is not None and conn.alloc is None
    gw_app.CONNECTIONS[conn.id] = conn
    req = gw_app.LabRequest(connection_id=conn.id, question="hello")
    try:
        resp = asyncio.run(gw_app.lab(req))
        assert resp.status_code == 409
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)


def test_lab_endpoint_rejects_session_without_ssh():
    """A session whose SSH isn't up yet (no executor) still 409s."""
    conn = _conn()                      # fresh: status 'connecting', executor None
    gw_app.CONNECTIONS[conn.id] = conn
    req = gw_app.LabRequest(connection_id=conn.id, question="hello")
    try:
        resp = asyncio.run(gw_app.lab(req))
        assert resp.status_code == 409
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)


def test_no_lazy_gpu_env_knob(monkeypatch):
    """BIOAGENT_LAZY_GPU is dead: setting it must not resurrect a setting or change anything."""
    monkeypatch.setenv("BIOAGENT_LAZY_GPU", "1")
    assert not hasattr(HPCSettings.from_env(), "lazy_gpu")
