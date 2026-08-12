"""Mid-run vLLM tunnel/serve recovery (_lab_llm + _heal_vllm_session).

A live session's LLM link dies with no user action when the SSH tunnel is reaped
while idle or the GPU serve job hits its Slurm --time limit. Before this, the next
call failed the whole run with a bare "Network error"; now the gateway heals the
session (reattach-or-resubmit + reopen tunnel) and retries once. These tests pin that
behaviour without touching HPC3.
"""

from __future__ import annotations

import threading
import types

import pytest

pytest.importorskip("fastapi")  # app.py imports fastapi; skip when the gateway extra isn't installed (CI offline)

from bioagent.gateway import app as gw  # noqa: E402
from bioagent.gateway.errors import VLLMNetworkError  # noqa: E402


def _fake_conn() -> tuple[types.SimpleNamespace, list]:
    events: list = []
    settings = types.SimpleNamespace(local_tunnel_port=0)
    conn = types.SimpleNamespace(
        mock=False,
        selected_model="qwen3.6:35b-a3b",
        tunnel_port=5001,
        settings=settings,
        gpu_lock=threading.Lock(),
        alloc=None,
        executor=types.SimpleNamespace(
            open_tunnel=lambda node, port, local_port=0: 5999,  # heal rebinds to a fresh local port
        ),
        emit_fn=lambda: (lambda *a, **k: events.append(a)),
    )
    return conn, events


def test_scientist_chat_heals_and_retries_once(monkeypatch):
    monkeypatch.delenv("BIOAGENT_LLM_BASE_URL", raising=False)
    conn, _ = _fake_conn()

    calls = {"chat": 0, "ensure": 0}

    def fake_chat_tools(port, model, messages, tools, **kw):
        calls["chat"] += 1
        if calls["chat"] == 1:
            raise VLLMNetworkError("Network error during vLLM tool-chat.", stage="vllm_chat")
        assert port == 5999, "retry must use the recovered tunnel port"
        return {"content": "ok", "tool_calls": []}

    def fake_ensure(executor, settings, emit):
        calls["ensure"] += 1
        return types.SimpleNamespace(node="hpc3-gpu-1", port=12345)

    monkeypatch.setattr(gw.vllm_client, "chat_tools", fake_chat_tools)
    monkeypatch.setattr(gw.gpu, "ensure_serve_job", fake_ensure)
    monkeypatch.setattr(gw, "_wait_for_server", lambda conn, emit, **kw: None)
    monkeypatch.setattr(gw, "_vllm_reachable", lambda conn: False)  # tunnel is dead

    _r = gw._lab_llm(conn)
    scientist_chat, label = _r.scientist_chat, _r.label
    result = scientist_chat([{"role": "user", "content": "hi"}], [])

    assert result == {"content": "ok", "tool_calls": []}
    assert calls["chat"] == 2, "must retry exactly once after healing"
    assert calls["ensure"] == 1, "must reattach/resubmit the serve job exactly once"
    assert conn.tunnel_port == 5999, "conn.tunnel_port must be refreshed to the new local port"
    assert label == "vLLM"


def test_recovery_reattaches_without_resubmit_when_tunnel_already_live(monkeypatch):
    """If the drop was transient and /v1 is reachable again by the time we hold the lock,
    heal is a no-op (no serve resubmit) and the retry just succeeds."""
    monkeypatch.delenv("BIOAGENT_LLM_BASE_URL", raising=False)
    conn, _ = _fake_conn()
    calls = {"chat": 0, "ensure": 0}

    def fake_chat_tools(port, model, messages, **kw):
        calls["chat"] += 1
        if calls["chat"] == 1:
            raise VLLMNetworkError("Network error during vLLM completion.", stage="vllm_chat")
        return "recovered"

    monkeypatch.setattr(gw.vllm_client, "complete", fake_chat_tools)
    monkeypatch.setattr(gw.gpu, "ensure_serve_job",
                        lambda *a, **k: calls.__setitem__("ensure", calls["ensure"] + 1))
    monkeypatch.setattr(gw, "_wait_for_server", lambda conn, emit, **kw: None)
    monkeypatch.setattr(gw, "_vllm_reachable", lambda conn: True)  # already back

    complete_fn = gw._lab_llm(conn).complete_fn
    assert complete_fn([{"role": "user", "content": "x"}]) == "recovered"
    assert calls["ensure"] == 0, "no resubmit when the tunnel is already reachable"


def test_openrouter_base_url_does_not_heal(monkeypatch):
    """The off-cluster test path (BIOAGENT_LLM_BASE_URL) has no tunnel to heal — a network
    error must propagate unchanged, never touching Slurm."""
    monkeypatch.setenv("BIOAGENT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("BIOAGENT_LLM_API_KEY", "sk-test")
    conn, _ = _fake_conn()
    ensure_called = {"n": 0}

    def always_fail(port, model, messages, tools, **kw):
        raise VLLMNetworkError("Network error during vLLM tool-chat.", stage="vllm_chat")

    monkeypatch.setattr(gw.vllm_client, "chat_tools", always_fail)
    monkeypatch.setattr(gw.gpu, "ensure_serve_job",
                        lambda *a, **k: ensure_called.__setitem__("n", ensure_called["n"] + 1))

    _r = gw._lab_llm(conn)
    scientist_chat, label = _r.scientist_chat, _r.label
    with pytest.raises(VLLMNetworkError):
        scientist_chat([{"role": "user", "content": "hi"}], [])
    assert ensure_called["n"] == 0
    assert label == "OpenRouter"


def test_serve_script_constraint_pins_gpu_flavour():
    """BIOAGENT_SLURM_CONSTRAINT lets an operator pin the 80GB A100 so --max-model-len
    131072 fits (a bare gpu:A100:1 can land on a 40GB card where vLLM aborts at boot)."""
    from bioagent.gateway import gpu
    from bioagent.gateway.settings import HPCSettings

    off = gpu._serve_script(HPCSettings(), "tester")
    on = gpu._serve_script(HPCSettings(constraint="a100_80gb"), "tester")
    assert "--constraint" not in off, "no constraint directive by default"
    assert "#SBATCH --constraint=a100_80gb\n" in on, "constraint directive rendered when set"
