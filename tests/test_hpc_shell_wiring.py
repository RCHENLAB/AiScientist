"""How the worker, the shell tools, the HITL channel and the package cache attach to a session.

The unit tests cover each piece in isolation; this covers the joins — the places where a wiring
mistake would silently disable a safety property rather than fail a test.
"""

from __future__ import annotations

import importlib
import threading

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app          # noqa: E402
from aiscientist.gateway.settings import HPCSettings   # noqa: E402


def _conn(tmp_path, monkeypatch, **settings_kw):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path))
    import asyncio
    st = HPCSettings(**settings_kw)
    c = gw_app.Connection(st, mock=False, loop=asyncio.new_event_loop(), username="alice")
    c.executor = type("E", (), {"username": "alice", "exec": lambda self, cmd, timeout=60.0: None})()
    c.workspace = tmp_path / "alice"
    gw_app.CONNECTIONS[c.id] = c
    return c


# --- the shell is bound only when it can actually work -----------------------


def test_no_shell_without_a_live_session(tmp_path, monkeypatch):
    """A tool roster offering something that cannot work is worse than a smaller roster."""
    c = _conn(tmp_path, monkeypatch)
    c.executor = None
    assert gw_app._build_hpc_shell(c, None) is None

    c2 = _conn(tmp_path, monkeypatch)
    c2.mock = True
    assert gw_app._build_hpc_shell(c2, None) is None


def test_reads_are_wider_than_writes(tmp_path, monkeypatch):
    """The lab account is shared, so 'my account' is not the same as 'my data': the agent may
    consult shared reference data but may only modify its own user's directories."""
    c = _conn(tmp_path, monkeypatch)
    ws = gw_app._build_hpc_shell(c, None).workspace
    shared = c.settings.shared_root.rstrip("/")

    assert ws.can_read(f"{shared}/containers/analysis.sif") is True
    assert ws.can_write(f"{shared}/containers/analysis.sif") is False
    assert ws.can_write(f"{shared}/Temp/alice/run1") is True
    assert ws.can_write(f"{shared}/Temp/bob/run1") is False, "never another member's directory"
    assert ws.can_read("/etc/shadow") is False


def test_the_worker_is_not_allocated_just_by_building_the_shell(tmp_path, monkeypatch):
    """Lazy on purpose: a run that only inspects files must never cost a CPU allocation."""
    calls = []
    monkeypatch.setattr(gw_app, "_ensure_session_worker", lambda conn: calls.append(1))
    c = _conn(tmp_path, monkeypatch, worker_enabled=True)

    shell = gw_app._build_hpc_shell(c, None)
    assert shell.worker_provider is not None and calls == []


def test_worker_tools_are_absent_when_the_worker_is_disabled(tmp_path, monkeypatch):
    c = _conn(tmp_path, monkeypatch, worker_enabled=False)
    assert gw_app._build_hpc_shell(c, None).worker_provider is None


# --- the HITL channel --------------------------------------------------------


class _Req:
    kind, summary, detail, reversible = "destructive", "rm -rf something", "rm -rf /x", False

    def as_dict(self):
        return {"kind": self.kind, "summary": self.summary, "detail": self.detail,
                "reversible": self.reversible}


def test_a_confirmation_reaches_the_client_and_its_answer_unblocks_the_worker(tmp_path, monkeypatch):
    c = _conn(tmp_path, monkeypatch)
    run = c.begin_run("conv-1")
    c.bind_run_id(run, "run-1")
    pushed: list[dict] = []
    monkeypatch.setattr(c, "push", pushed.append)

    confirm = gw_app._hitl_confirm(c, run)
    answer: list[bool] = []
    t = threading.Thread(target=lambda: answer.append(confirm(_Req())))
    t.start()

    for _ in range(200):                       # wait for the card to be pushed
        if any(p.get("type") == "confirm_request" for p in pushed):
            break
        threading.Event().wait(0.01)

    card = next(p for p in pushed if p["type"] == "confirm_request")
    assert card["detail"] == "rm -rf /x" and card["run_id"] == "run-1"
    assert card["conversation_id"] == "conv-1", "so the client shows it in the OWNING bubble"

    run.confirm_value = True
    run.confirm_event.set()
    t.join(timeout=5)
    assert answer == [True]


def test_no_answer_within_the_timeout_declines(tmp_path, monkeypatch):
    """An unattended run must not hold a worker allocation forever waiting for a click that is
    never coming."""
    monkeypatch.setenv("AISCIENTIST_CONFIRM_TIMEOUT_S", "0.1")
    c = _conn(tmp_path, monkeypatch)
    run = c.begin_run("conv-1")
    monkeypatch.setattr(c, "push", lambda *_a: None)

    assert gw_app._hitl_confirm(c, run)(_Req()) is False


def test_confirm_and_plan_review_do_not_answer_each_other(tmp_path, monkeypatch):
    """They are separate events precisely so an approval of one cannot satisfy the other."""
    c = _conn(tmp_path, monkeypatch)
    run = c.begin_run("conv-1")
    run.plan_event.set()                       # a plan approval is outstanding
    assert run.confirm_event.is_set() is False


def test_the_confirm_route_resolves_to_the_owning_run(tmp_path, monkeypatch):
    """On a shared SSH session, a click in one window must never satisfy another window's prompt."""
    from fastapi.testclient import TestClient
    pytest.importorskip("httpx")
    c = _conn(tmp_path, monkeypatch)
    mine = c.begin_run("conv-mine")
    c.bind_run_id(mine, "run-mine")
    client = TestClient(gw_app.app)

    stale = client.post("/api/confirm", json={"connection_id": c.id, "approved": True,
                                              "run_id": "run-someone-else"})
    assert stale.json()["status"] == "stale"
    assert mine.confirm_event.is_set() is False

    ok = client.post("/api/confirm", json={"connection_id": c.id, "approved": True,
                                           "run_id": "run-mine"})
    assert ok.json() == {"status": "ok", "approved": True}
    assert mine.confirm_event.is_set() and mine.confirm_value is True


# --- the package cache reaches the sandbox -----------------------------------


def test_the_sandbox_binds_the_shared_cache_read_only():
    """A snippet may IMPORT from the cache but must never write to it: publishing goes through
    install_package, which prunes image collisions and publishes atomically."""
    from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor

    ex = SlurmCodeExecutor(remote=None, container_image="/x/analysis.sif",
                           package_cache_root="/dfs3b/lab/AiScientist/pkgs")
    preamble, binds = ex._package_cache_preamble()

    assert binds == ("/dfs3b/lab/AiScientist/pkgs",)
    assert "AISCIENTIST_PKG_CACHE" in preamble
    # The key is assembled in-container as "$tag-py$ver" — the image identity is baked in, the
    # Python version is read from the image itself, so vep.sif never imports analysis.sif's wheels.
    assert "tag=analysis.sif" in preamble
    assert '"$root/$tag-py$ver"' in preamble


def test_an_unset_cache_root_changes_nothing():
    """The feature is additive: without a cache root the sandbox behaves exactly as before."""
    from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor

    ex = SlurmCodeExecutor(remote=None, container_image="/x/analysis.sif")
    assert ex._package_cache_preamble() == ("", ())


# --- catalog registration ----------------------------------------------------


def test_the_shell_tools_join_the_scientist_catalog(tmp_path, monkeypatch):
    from aiscientist.agents.registry import build_scientist_catalog

    c = _conn(tmp_path, monkeypatch, worker_enabled=True)
    names = {t.name for t in build_scientist_catalog(hpc_shell=gw_app._build_hpc_shell(c, None))}
    assert {"list_dir", "read_text", "run_shell", "fetch_url", "install_package"} <= names


def test_a_catalog_without_a_session_gains_nothing(tmp_path):
    from aiscientist.agents.registry import build_scientist_catalog

    with_none = {t.name for t in build_scientist_catalog(hpc_shell=None)}
    assert "run_shell" not in with_none and "list_dir" not in with_none


# --- connect: an API endpoint means no GPU ------------------------------------


def test_bringing_your_own_key_skips_the_gpu_entirely(tmp_path, monkeypatch):
    """The point of Yijun's call: no accelerator is held for a session that reasons on an API,
    so connect is ready in seconds instead of waiting out the GPU queue."""
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path))
    from aiscientist.gateway import llm_credentials
    importlib.reload(llm_credentials)
    cred = llm_credentials.create("alice", provider="deepseek", base_url="https://api.deepseek.com/v1",
                                  model="deepseek-chat", api_key="sk-000000000000", label="Mine")

    c = _conn(tmp_path, monkeypatch)
    c.llm_choice = {"credential_id": cred["id"]}
    gpu_calls = []
    monkeypatch.setattr(gw_app, "_ssh_connect_blocking", lambda *a: None)
    monkeypatch.setattr(gw_app, "_provision_gpu_blocking", lambda *a: gpu_calls.append(1))

    gw_app._provision_blocking(c, gw_app.ConnectRequest(ucinetid="alice"))

    assert gpu_calls == [], "no GPU job was submitted"
    assert c.status == "ready"
    assert c.selected_model == "deepseek-chat"
    assert c.alloc is None and c.tunnel_port is None


def test_without_a_credential_the_gpu_path_is_unchanged(tmp_path, monkeypatch):
    c = _conn(tmp_path, monkeypatch)
    gpu_calls = []
    monkeypatch.setattr(gw_app, "_ssh_connect_blocking", lambda *a: None)
    monkeypatch.setattr(gw_app, "_provision_gpu_blocking", lambda *a: gpu_calls.append(1))

    gw_app._provision_blocking(c, gw_app.ConnectRequest(ucinetid="alice"))
    assert gpu_calls == [1]
