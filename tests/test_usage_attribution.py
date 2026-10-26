"""Which chat, run and pipeline step each model call is recorded against, and failed calls
(RCHENLAB/AiScientist issue 8, Part B). Offline: the recorder is replaced by a list."""

from __future__ import annotations

import contextvars

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway import vllm_client  # noqa: E402
from aiscientist.gateway.errors import GatewayError  # noqa: E402


class _Run:
    run_id = "run-1"
    conversation_id = "17"


class _Conn:
    selected_model = "m"
    tunnel_port = 1
    app_user_id = 3
    last_run_id = None
    mock = False
    active_run = _Run()


@pytest.fixture()
def rows(monkeypatch):
    from aiscientist.gateway import auth_routes
    got: list = []
    monkeypatch.setattr(auth_routes, "record_llm_call", lambda **kw: got.append(kw))
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    return got


def test_a_call_is_recorded_against_its_chat_run_and_step(rows):
    flags: dict = {}

    def in_run():
        gw_app._note_usage_step({"type": "planning"}, flags)
        gw_app._record_lab_usage(_Conn(), role="plan", model="m", endpoint="e", remote=True,
                                 usage={}, max_tokens=None, duration_ms=1)
        gw_app._note_usage_step({"type": "pi_agenda", "agenda": ["Run QC", "Cluster  the cells"]}, flags)
        gw_app._note_usage_step({"type": "scientist_start", "step": "Cluster the cells"}, flags)
        gw_app._record_lab_usage(_Conn(), role="scientist", model="m", endpoint="e", remote=True,
                                 usage={}, max_tokens=None, duration_ms=1)
        gw_app._note_usage_step({"type": "synthesize"}, flags)
        gw_app._record_lab_usage(_Conn(), role="writer", model="m", endpoint="e", remote=True,
                                 usage={}, max_tokens=None, duration_ms=1)

    contextvars.copy_context().run(in_run)
    assert [r["step"] for r in rows] == ["Planning", "Step 2: Cluster the cells", "Write-up"]
    assert all(r["conversation_id"] == "17" and r["run_id"] == "run-1" and r["user_id"] == 3 for r in rows)


def test_parallel_dag_nodes_each_keep_their_own_step(rows):
    from aiscientist.agents.loop_utils import ContextThreadPoolExecutor
    flags = {"usage_agenda": ["A", "B"]}

    def node(step):
        gw_app._note_usage_step({"type": "scientist_start", "step": step}, flags)
        import time
        time.sleep(0.05)                       # both nodes are mid-step at once
        gw_app._record_lab_usage(_Conn(), role="scientist", model="m", endpoint="e", remote=True,
                                 usage={}, max_tokens=None, duration_ms=1)
        return step

    def run():
        with ContextThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(node, ["A", "B"]))

    contextvars.copy_context().run(run)
    assert sorted(r["step"] for r in rows) == ["Step 1: A", "Step 2: B"]


def test_a_failed_lab_call_is_recorded_with_its_cause_then_raised(rows, monkeypatch):
    for v in ("AISCIENTIST_LAB_LLM_BASE_URL", "AISCIENTIST_LAB_LLM_MODEL", "AISCIENTIST_LAB_LLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("AISCIENTIST_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("AISCIENTIST_LLM_API_KEY", "sk-or-example")

    def boom(*a, **k):
        raise GatewayError('vLLM completion error: {"error":{"code":402,"message":"Insufficient credits"}}',
                           stage="vllm_chat")

    monkeypatch.setattr(vllm_client, "complete_ex", boom)
    llm = gw_app._lab_llm(_Conn())
    with pytest.raises(GatewayError):
        llm.complete_fn([{"role": "user", "content": "q"}], role="plan")
    assert rows[-1]["error_type"] == "credit" and rows[-1]["usage"] is None
    assert rows[-1]["provider"] == "openai" and rows[-1]["remote"] is True


@pytest.mark.parametrize("message,stage,cause", [
    ("Anthropic is rate-limiting this key", "anthropic_rate", "rate"),
    ("The key is valid but the account has no credit left.", "anthropic_credit", "credit"),
    ('vLLM tool-chat error: {"error":{"code":401,"message":"User not found."}}', "vllm_chat", "auth"),
    ("vLLM chat error: 429 Too Many Requests", "vllm_chat", "rate"),
    ("This model's maximum context length is 262144 tokens", "vllm_chat", "context"),
    ("vLLM chat error: something odd", "vllm_chat", "endpoint"),
])
def test_the_recorded_cause_is_a_category_not_the_message(message, stage, cause):
    assert gw_app._usage_error_type(GatewayError(message, stage=stage)) == cause


def test_the_openai_scientist_turn_reports_its_usage_and_is_recorded(rows, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("AISCIENTIST_LLM_API_KEY", "sk-or-example")

    class _Resp:
        def __init__(self):
            import json
            self._b = json.dumps({"model": "anthropic/claude-opus-4.8",
                                  "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 50, "completion_tokens": 5}}).encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(vllm_client.urllib.request, "urlopen", lambda req, timeout=0: _Resp())
    out = gw_app._lab_llm(_Conn()).scientist_chat([{"role": "user", "content": "q"}], [])
    assert out == {"content": "hi", "tool_calls": [], "finish_reason": "stop"}   # nothing of ours added
    assert rows[-1]["role"] == "scientist" and rows[-1]["usage"]["prompt_tokens"] == 50
    assert rows[-1]["usage"]["model"] == "anthropic/claude-opus-4.8" and rows[-1]["remote"] is True


def test_the_budget_gate_only_applies_to_a_session_on_an_api_key(monkeypatch):
    from aiscientist.gateway import llm_usage
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(llm_usage, "budget_gate",
                        lambda uid, tz=None: {"mode": "confirm", "message": "limit reached",
                                              "spent_usd": 2.0, "hard_limit_usd": 1.0})
    conn = _Conn()
    monkeypatch.setattr(gw_app, "_session_credential", lambda c: None)
    assert gw_app._budget_refusal(conn, False, None) is None          # cluster GPU: free
    monkeypatch.setattr(gw_app, "_session_credential", lambda c: {"id": "x"})
    res = gw_app._budget_refusal(conn, False, None)
    assert res.status_code == 409 and b"needs_budget_confirm" in res.body
    assert gw_app._budget_refusal(conn, True, None) is None            # the user said go ahead
    monkeypatch.setattr(llm_usage, "budget_gate",
                        lambda uid, tz=None: {"mode": "block", "message": "blocked",
                                              "spent_usd": 2.0, "hard_limit_usd": 1.0})
    res = gw_app._budget_refusal(conn, True, None)
    assert res.status_code == 409 and b"budget_blocked" in res.body
