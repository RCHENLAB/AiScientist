"""Real loopback/file-queue roundtrips, with a fake guarded provider callback."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiscientist.agents.mcp_relay_worker import RelayServer, validate_request
from aiscientist.gateway.errors import GatewayError
from aiscientist.gateway.llm_relay import LocalFS, ModelRelay

MODEL = "synthetic-session-model"
TOOL = {"type": "function", "function": {"name": "collect", "parameters": {"type": "object"}}}
CALL = {"id": "call-1", "type": "function", "function": {"name": "collect", "arguments": '{"k": 2}'}}


@contextmanager
def serving(tmp_path, callback, **options):
    directory = tmp_path.resolve() / "relay"
    server = RelayServer(directory, MODEL, timeout=3)
    relay = ModelRelay(LocalFS, str(directory), call_request=callback, poll_s=0.005,
                       sleep=lambda _: None, concurrency=1, **options).start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, relay
    finally:
        server.shutdown()
        server.server_close()
        relay.stop()
        thread.join(timeout=3)


def request(server, body, token=True):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + server.token
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                                 data=json.dumps(body).encode(), headers=headers)
    return urllib.request.urlopen(req, timeout=5)


def body(**kwargs):
    return {"model": MODEL, "messages": [{"role": "user", "content": "synthetic public question"}], **kwargs}


def test_text_json_roundtrip_preserves_model_options_usage_and_private_files(tmp_path):
    seen, records = [], []
    def guarded(req):
        seen.append(validate_request(req, MODEL))
        return {"content": '{"ok": true}', "finish_reason": "stop"}, {"prompt_tokens": 12, "completion_tokens": 4}
    with serving(tmp_path, guarded, price=lambda _: 0.02, record=lambda *args: records.append(args)) as (server, relay):
        assert server.server_address[0] == "127.0.0.1"
        assert stat.S_IMODE(server.directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((server.directory / "token").stat().st_mode) == 0o600
        with request(server, body(max_tokens=100, temperature=0, response_format={"type": "json_object"},
                                  chat_template_kwargs={"enable_thinking": False})) as response:
            result = json.load(response)
        assert json.loads(result["choices"][0]["message"]["content"]) == {"ok": True}
        assert result["usage"]["completion_tokens"] == 4
        assert result["aiscientist_usage"] == {"version": 1, "billed": True, "cost_usd": 0.02}
        assert seen[0]["model"] == MODEL and seen[0]["max_tokens"] == 100
        assert seen[0]["reasoning_effort"] == "none" and seen[0]["temperature"] == 0
        assert "chat_template_kwargs" not in seen[0]
        assert relay.stats.calls == 1 and relay.stats.cost_usd == 0.02 and len(records) == 1
        assert not list((server.directory / "req").iterdir())
        assert not list((server.directory / "resp").iterdir())
    assert not (server.directory / "token").exists()


@pytest.mark.parametrize("stream", [False, True])
def test_function_call_choice_and_replay_cross_full_queue(tmp_path, stream):
    seen = []
    def guarded(req):
        seen.append(validate_request(req, MODEL))
        return {"content": "", "tool_calls": [CALL], "finish_reason": "tool_calls"}, {"prompt_tokens": 10}
    with serving(tmp_path, guarded) as (server, _):
        with request(server, body(tools=[TOOL], tool_choice={"type": "function", "function": {"name": "collect"}},
                                  stream=stream, stream_options={"include_usage": True})) as response:
            raw = response.read().decode()
        if stream:
            chunks = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: {")]
            assert chunks[0]["choices"][0]["delta"]["tool_calls"][0] == {"index": 0, **CALL}
            assert chunks[1]["choices"][0]["finish_reason"] == "tool_calls"
            assert chunks[-1]["usage"]["prompt_tokens"] == 10 and raw.endswith("data: [DONE]\n\n")
        else:
            result = json.loads(raw)
            assert result["choices"][0]["message"]["tool_calls"] == [CALL]
        replay = body()
        replay["messages"] += [{"role": "assistant", "content": None, "tool_calls": [CALL]},
                               {"role": "tool", "tool_call_id": "call-1", "content": "two documents"}]
        with request(server, replay):
            pass
        assert seen[-1]["messages"][-1]["tool_call_id"] == "call-1"


@pytest.mark.parametrize("patch", [{"model": "another-model"}, {"unknown": True}, {"max_tokens": True},
                                  {"max_tokens": 999999}, {"tools": ["bad"]},
                                  {"response_format": {"type": "json_schema"}}, {"n": 2},
                                  {"chat_template_kwargs": {"arbitrary": True}}])
def test_invalid_requests_never_reach_guarded_provider(tmp_path, patch):
    def forbidden(_):
        raise AssertionError("invalid request reached provider")
    with serving(tmp_path, forbidden) as (server, relay):
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, body(**patch))
        assert exc.value.code == 400 and relay.stats.calls == 0


def test_authentication_and_budget_refusal_make_no_extra_provider_call(tmp_path):
    calls = []
    def guarded(req):
        calls.append(req)
        return {"content": "ok"}, {"prompt_tokens": 1}
    with serving(tmp_path, guarded, price=lambda _: 1.0, max_cost_usd=1.0) as (server, relay):
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, body(), token=False)
        assert exc.value.code == 401 and not calls
        with request(server, body()):
            pass
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, body())
        assert exc.value.code == 403 and len(calls) == 1 and relay.stats.calls == 1


def test_gateway_key_guard_refusal_stays_fatal(tmp_path):
    def refused(_):
        raise GatewayError("synthetic budget refused", stage="budget")
    records = []
    with serving(tmp_path, refused, record=lambda *args: records.append(args)) as (server, relay):
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, body())
        assert exc.value.code == 403
        assert relay.stats.calls == 0 and relay.stats.failed == 1
        assert not records


def test_run_cancellation_prevents_provider_request(tmp_path):
    calls = []
    with serving(tmp_path, lambda req: calls.append(req), should_stop=lambda: True) as (server, relay):
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, body())
        assert exc.value.code == 403 and not calls and relay.stats.calls == 0


def test_invalid_json_model_reply_records_real_usage_once(tmp_path):
    records = []
    def guarded(_):
        return {"relay_error": "model did not produce JSON"}, {"prompt_tokens": 9, "completion_tokens": 3}
    with serving(tmp_path, guarded, price=lambda _: 0.02, record=lambda *args: records.append(args)) as (server, relay):
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, body(response_format={"type": "json_object"}))
        assert exc.value.code == 502
        error = json.load(exc.value)
        assert error["aiscientist_usage"] == {"version": 1, "billed": True, "cost_usd": 0.02}
        assert error["usage"]["completion_tokens"] == 3
        assert relay.stats.calls == relay.stats.failed == 1
        assert relay.stats.cost_usd == 0.02 and len(records) == 1
        assert records[0][0]["completion_tokens"] == 3 and records[0][2] == "model"


def test_relay_rejects_symlink_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink"):
        RelayServer(link / "child", MODEL)


def test_actual_staged_worker_setup_and_gateway_lifecycle(tmp_path):
    from aiscientist.agents.mcp_model import RelayBinding, model_files, prepare_model, run_with_model
    from aiscientist.agents.skills_registry import POLICY
    root = tmp_path.resolve()
    skill = root / "skill"
    skill.mkdir()
    relays = []
    def start(directory):
        def guarded(req):
            validate_request(req, MODEL)
            return {"content": '{"hpo": ["HP:0000662"]}'}, {"prompt_tokens": 10, "completion_tokens": 5}
        relay = ModelRelay(LocalFS, directory, call_request=guarded, poll_s=0.01, concurrency=1).start()
        relays.append(relay)
        return relay
    executor = SimpleNamespace(work_dir=str(root), supports_skill_policy=True,
                               mcp_model_binding=lambda: RelayBinding(MODEL, start))
    bound, setup = prepare_model("phenotype-diagnosis", "map_phenotype_to_hpo", executor, {})
    for name, source in model_files(bound).items():
        shutil.copyfile(source, skill / name)
    code = '''
import json, os, pathlib, urllib.request
key = pathlib.Path(os.environ['PHENOTYPE_MODEL_API_KEY_FILE']).read_text()
body = {'model': os.environ['PHENOTYPE_MODEL'], 'messages': [{'role': 'user', 'content': 'synthetic'}]}
req = urllib.request.Request(os.environ['PHENOTYPE_MODEL_ENDPOINT']+'/chat/completions', data=json.dumps(body).encode(), headers={'Authorization': 'Bearer '+key})
with urllib.request.urlopen(req, timeout=10) as result:
    print(json.load(result)['choices'][0]['message']['content'])
'''
    command = "set -eu\n" + setup + "python - <<'CHECK'\n" + code + "CHECK\n"
    env = {**os.environ, "AISCIENTIST_WORK": str(root), "AISCIENTIST_SKILL_DIR": str(skill),
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    token = POLICY.set({"network": "runtime", "secret_values": {}})
    try:
        result = run_with_model(bound, executor,
                                lambda: subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True, timeout=20))
    finally:
        POLICY.reset(token)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"hpo": ["HP:0000662"]}
    assert relays[0].stats.calls == 1 and not relays[0]._thread.is_alive()
    assert not (Path(bound.relay_directory) / "token").exists()
