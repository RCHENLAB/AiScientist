"""The idle GPU watchdog (``_monitor_gpu``) on sessions with and without a GPU.

A session on the user's own API key holds no allocation (``conn.alloc is None``). The watchdog used
to probe it anyway: ``srun --jobid={alloc.job_id}`` raised AttributeError on every poll, and each one
landed in the run's event log as "GPU monitor error." (prod run 6275df1213ab).
"""

from __future__ import annotations

import asyncio

import pytest

from aiscientist.gateway import app as gw
from aiscientist.gateway import gpu
from aiscientist.gateway.gpu import GPUAllocation, GPUHealth
from aiscientist.gateway.settings import HPCSettings


class _Executor:
    def __init__(self):
        self.commands: list[str] = []

    def exec(self, cmd, timeout=None):
        self.commands.append(cmd)
        raise AssertionError(f"unexpected command on HPC3: {cmd}")


@pytest.fixture
def conn():
    c = gw.Connection(HPCSettings(), mock=False, loop=asyncio.new_event_loop(), username="alice")
    c.status = "ready"
    c.executor = _Executor()        # a real (non-mock) session's SSH line
    gw.CONNECTIONS[c.id] = c
    yield c
    gw.CONNECTIONS.pop(c.id, None)


def _run_polls(monkeypatch, conn, polls: int) -> None:
    """Run the watchdog for ``polls`` iterations, then end the session so the loop exits."""
    slept = 0

    async def _sleep(_seconds):
        nonlocal slept
        slept += 1
        if slept > polls and conn.status == "ready":
            conn.status = "disconnected"

    monkeypatch.setattr(gw.asyncio, "sleep", _sleep)
    asyncio.run(gw._monitor_gpu(conn))


def test_a_session_without_a_gpu_is_not_probed_or_marked_lost(monkeypatch, conn):
    # The real check_health stays in place: reaching it is the prod AttributeError on alloc.job_id.
    looked = []
    monkeypatch.setattr(gpu, "find_running_job", lambda *a, **k: looked.append("squeue"))
    assert conn.alloc is None
    _run_polls(monkeypatch, conn, polls=3)
    assert looked == [] and conn.executor.commands == []
    assert not [e for e in conn.log if e["level"] == "error"]
    assert conn.status == "disconnected"         # ended by the test, never flipped to "error"


def test_a_gpu_session_whose_job_vanished_is_still_marked_lost(monkeypatch, conn):
    conn.alloc = GPUAllocation(job_id="555", node="hpc3-gpu-m54-00", port=30000)
    monkeypatch.setattr(gpu, "check_health",
                        lambda *a, **k: GPUHealth(True, 3, 1000, 81920, "NVIDIA A100"))
    monkeypatch.setattr(gpu, "find_running_job", lambda *a, **k: None)
    _run_polls(monkeypatch, conn, polls=3)
    assert conn.status == "error"
    assert [e["stage"] for e in conn.log if e["level"] == "error"] == ["gpu_alloc"]
