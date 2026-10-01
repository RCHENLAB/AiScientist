"""Lab-role calls on the cluster GPU: no wall-clock limit, only an idle (no-token) limit; effort per role.

The fake server streams SSE like vLLM with a reasoning parser: reasoning deltas first, then content.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bioagent.gateway import vllm_client
from bioagent.gateway.errors import VLLMNetworkError


def _serve(chunks, gap: float, stall_after: int | None = None):
    """Start a fake /v1/chat/completions that streams ``chunks`` with ``gap`` seconds between them;
    with ``stall_after`` it goes silent after that many chunks. Returns (port, seen_payloads)."""
    seen: list[dict] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401 - silence
            pass

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i, c in enumerate(chunks):
                if stall_after is not None and i >= stall_after:
                    time.sleep(30)
                    return
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                self.wfile.flush()
                time.sleep(gap)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], seen


def _chunks(n_think: int):
    think = [{"model": "m", "choices": [{"delta": {"reasoning_content": "hmm "}}]} for _ in range(n_think)]
    answer = [{"model": "m", "choices": [{"delta": {"content": "PLAN"}}]},
              {"model": "m", "choices": [{"delta": {}, "finish_reason": "stop"}]},
              {"model": "m", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 9}}]
    return think + answer


def test_long_thinking_outlives_the_idle_timeout(monkeypatch):
    # ~2.4 s of streamed thinking against a 1 s idle limit: never silent for 1 s, so it completes.
    monkeypatch.setattr(vllm_client, "_base", lambda port: f"http://127.0.0.1:{port}/v1", raising=False)
    port, seen = _serve(_chunks(12), gap=0.2)
    t = time.time()
    text, usage = vllm_client.complete_ex(port, "m", [{"role": "user", "content": "plan"}],
                                          reasoning_effort="high", idle_timeout=1.0)
    assert time.time() - t > 2.0                      # longer than the idle limit, and still fine
    assert text == "PLAN" and usage["finish_reason"] == "stop" and usage["completion_tokens"] == 9
    assert seen[0]["stream"] is True and seen[0]["reasoning_effort"] == "high"


def test_silence_is_what_ends_a_call(monkeypatch):
    monkeypatch.setattr(vllm_client, "_base", lambda port: f"http://127.0.0.1:{port}/v1", raising=False)
    port, _ = _serve(_chunks(5), gap=0.05, stall_after=3)
    with pytest.raises(VLLMNetworkError):
        vllm_client.complete_ex(port, "m", [{"role": "user", "content": "x"}], idle_timeout=0.8)


def test_role_effort_env(monkeypatch):
    from bioagent.gateway import app as gw
    monkeypatch.setenv("BIOAGENT_VLLM_REASONING_EFFORT_PLAN", "high")
    monkeypatch.setenv("BIOAGENT_VLLM_REASONING_EFFORT_CLASSIFY", "low")
    monkeypatch.delenv("BIOAGENT_VLLM_REASONING_EFFORT_CRITIC", raising=False)
    assert gw._role_effort("plan") == "high" and gw._role_effort("classify") == "low"
    assert gw._role_effort("critic") is None          # falls back to the global effort inside complete_ex
    monkeypatch.setenv("BIOAGENT_VLLM_REASONING_EFFORT_WRITER", "bogus")
    assert gw._role_effort("writer") is None


def test_lab_roles_reach_the_cluster_call(monkeypatch):
    """The PI plan call carries role=plan all the way to complete_ex, with the idle (not total) limit."""
    from bioagent.gateway import app as gw
    monkeypatch.setenv("BIOAGENT_VLLM_REASONING_EFFORT_PLAN", "high")
    calls = []
    monkeypatch.setattr(vllm_client, "complete_ex",
                        lambda port, model, messages, **kw: calls.append(kw) or ("ok", {}))
    conn = type("C", (), {"mock": False, "tunnel_port": 1234, "llm_choice": {}, "owner": "alice",
                          "selected_model": "m", "settings": None, "emit": lambda *a, **k: None,
                          "run_id": "r", "app_user_id": None})()
    monkeypatch.setattr(gw, "_record_lab_usage", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_session_credential", lambda c: None)
    llm = gw._lab_llm(conn)
    complete_fn = llm[0] if isinstance(llm, tuple) else llm.complete_fn
    complete_fn([{"role": "user", "content": "plan"}], role="plan")
    assert calls and calls[0]["reasoning_effort"] == "high" and calls[0]["idle_timeout"] == gw._LAB_IDLE_TIMEOUT


def test_an_effort_the_model_rejects_falls_back_to_its_default():
    """Qwen3.8 answers reasoning_effort="high" with HTTP 400 (it takes low/medium/xhigh only)."""
    seen: list[dict] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            if body.get("reasoning_effort") == "high":
                msg = json.dumps({"error": {"message": "Unexpected reasoning effort high. Supported types "
                                            "are xhigh (default), medium, and low.", "code": 400}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for c in _chunks(2):
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    text, _ = vllm_client.complete_ex(srv.server_address[1], "m", [{"role": "user", "content": "x"}],
                                      reasoning_effort="high", idle_timeout=5.0)
    assert text == "PLAN" and seen[0]["reasoning_effort"] == "high" and "reasoning_effort" not in seen[1]


def _scientist_chat(monkeypatch, replies):
    """The session's bound Scientist chat, with chat_tools scripted to return ``replies`` in order."""
    from bioagent.gateway import app as gw
    calls: list[dict] = []
    queue = list(replies)

    def fake_chat_tools(port, model, messages, tools, **kw):
        calls.append(kw)
        return queue.pop(0)

    monkeypatch.setattr(vllm_client, "chat_tools", fake_chat_tools)
    warned: list[str] = []
    conn = type("C", (), {"mock": False, "tunnel_port": 1234, "llm_choice": {}, "owner": "alice",
                          "selected_model": "m", "settings": None,
                          "emit": lambda *a, **k: warned.append(a[-1] if a else ""),
                          "run_id": "r", "app_user_id": None})()
    monkeypatch.setattr(gw, "_session_credential", lambda c: None)
    return gw._lab_llm(conn).scientist_chat, calls, warned


def test_the_scientist_turn_carries_its_role_effort(monkeypatch):
    monkeypatch.setenv("BIOAGENT_VLLM_REASONING_EFFORT_SCIENTIST", "medium")
    chat, calls, _ = _scientist_chat(monkeypatch, [{"content": "", "tool_calls": [{"x": 1}],
                                                    "finish_reason": "tool_calls"}])
    out = chat([{"role": "user", "content": "step"}], [])
    assert out["tool_calls"] and len(calls) == 1 and calls[0]["reasoning_effort"] == "medium"


def test_a_turn_cut_off_while_reasoning_is_asked_again_at_low_effort(monkeypatch):
    monkeypatch.delenv("BIOAGENT_VLLM_REASONING_EFFORT_SCIENTIST", raising=False)
    chat, calls, warned = _scientist_chat(monkeypatch, [
        {"content": "", "tool_calls": [], "finish_reason": "length"},
        {"content": "", "tool_calls": [{"x": 1}], "finish_reason": "tool_calls"}])
    out = chat([{"role": "user", "content": "step"}], [])
    assert out["tool_calls"] and len(calls) == 2
    assert calls[1]["reasoning_effort"] == "low"
    assert any("out of output room" in w for w in warned)


def test_a_turn_that_simply_answers_is_not_retried(monkeypatch):
    chat, calls, _ = _scientist_chat(monkeypatch, [{"content": "done", "tool_calls": [],
                                                    "finish_reason": "stop"}])
    assert chat([{"role": "user", "content": "step"}], [])["content"] == "done"
    assert len(calls) == 1


def test_live_llm_args_follow_the_current_allocation():
    from bioagent.gateway import app as gw
    conn = type("C", (), {"alloc": type("A", (), {"node": "hpc3-gpu-m54-01", "port": 39783})(),
                          "selected_model": "RedHatAI/Qwen3.8-27B-INT4"})()
    assert gw._live_llm_args(conn) == {"llm_base_url": "http://hpc3-gpu-m54-01:39783/v1",
                                       "model": "RedHatAI/Qwen3.8-27B-INT4"}
    conn.alloc = None                      # mid-reprovision: say nothing, keep the build-time value
    assert gw._live_llm_args(conn) == {}
