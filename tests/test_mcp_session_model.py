"""MCP model jobs follow the chosen session and never bypass the saved-key guard."""
from types import SimpleNamespace
import threading

import pytest
from aiscientist.gateway import app


def test_binding_follows_replaced_hpc_allocation(monkeypatch):
    monkeypatch.delenv('AISCIENTIST_LLM_BASE_URL', raising=False)
    monkeypatch.setattr(app, '_bound_credential', lambda conn: None)
    conn = SimpleNamespace(alloc=SimpleNamespace(node='gpu-1', port=8001), selected_model='local-model')
    binding = app._mcp_session_model(conn)
    assert binding.endpoint == 'http://gpu-1:8001/v1' and binding.model == 'local-model'
    conn.alloc = SimpleNamespace(node='gpu-2', port=8002)
    assert app._mcp_session_model(conn).endpoint == 'http://gpu-2:8002/v1'
    conn.alloc = None
    assert app._mcp_session_model(conn) is None


@pytest.mark.parametrize('credential', [{'id': 'saved-key', 'model': 'chosen'}, {'builtin': 'shared', 'model': 'chosen'}])
def test_other_chosen_endpoint_uses_relay_not_old_gpu(monkeypatch, credential):
    monkeypatch.delenv('AISCIENTIST_LLM_BASE_URL', raising=False)
    monkeypatch.setattr(app, '_bound_credential', lambda conn: credential)
    conn = SimpleNamespace(alloc=SimpleNamespace(node='old-gpu', port=8001), selected_model='old-model',
                           chat_stop=threading.Event())
    from aiscientist.agents.mcp_model import RelayBinding
    result = app._mcp_session_model(conn)
    assert isinstance(result, RelayBinding) and result.model == 'chosen'
    assert not hasattr(result, 'endpoint') and not hasattr(result, 'api_key')


def test_configured_external_endpoint_cannot_be_replaced_with_gpu(monkeypatch):
    monkeypatch.setenv('AISCIENTIST_LLM_BASE_URL', 'https://example.test/v1')
    monkeypatch.setenv('AISCIENTIST_LLM_MODEL', 'configured')
    monkeypatch.setattr(app, '_bound_credential', lambda conn: None)
    binding = app._mcp_session_model(SimpleNamespace(chat_stop=threading.Event()))
    assert binding.model == 'configured' and not hasattr(binding, 'endpoint')


def test_native_relay_guards_and_meters_selected_model_without_exporting_key(monkeypatch, tmp_path):
    from aiscientist.gateway import anthropic_client, auth_routes, llm_credentials, llm_relay
    from aiscientist.gateway.errors import GatewayError
    cred = {'id': 'test-credential', 'model': 'claude-haiku-5-5', 'lab_model': 'different-critic',
            'provider': 'anthropic', 'base_url': 'https://api.anthropic.com', 'label': 'Test key'}
    stop = threading.Event()
    conn = SimpleNamespace(chat_stop=stop, executor=SimpleNamespace(open_sftp=lambda: None),
                           owner='test-owner', emit=lambda *args, **kw: None, last_run_id='run94',
                           app_user_id='test-user', active_run=SimpleNamespace(run_id='run94', conversation_id='chat94'))
    calls, records, guards = [], [], []
    guard = SimpleNamespace(fingerprint='test-fp', before_call=lambda: guards.append('before'),
                            after_call=lambda: guards.append('after'), close=lambda: guards.append('close'))
    monkeypatch.setattr(app, '_bound_credential', lambda c: cred)
    monkeypatch.setattr(app, '_key_use', lambda *a, **kw: guard)
    monkeypatch.setattr(app, '_guard_lab_payload', lambda messages: guards.append('egress'))
    monkeypatch.setattr(app, '_push_usage', lambda *a: None)
    monkeypatch.setattr(llm_credentials, 'call_target', lambda *a: ('http://credential-proxy/v1', 'synthetic-secret'))
    monkeypatch.setattr(llm_relay.ModelRelay, 'start', lambda self: self)
    monkeypatch.setattr(auth_routes, 'record_llm_call', lambda **kw: records.append(kw))

    def native(model, request, **kwargs):
        calls.append((model, request, kwargs))
        assert guards[-2:] == ['egress', 'before']
        return {'content': '{"observed": []}', 'tool_calls': [], 'finish_reason': 'stop',
                'usage': {'prompt_tokens': 12, 'completion_tokens': 4, 'model': model}}
    monkeypatch.setattr(anthropic_client, 'relay_chat', native)
    binding = app._mcp_session_model(conn)
    assert 'synthetic-secret' not in repr(binding)
    assert binding.priced is True
    relay = binding.start(str(tmp_path), max_cost_usd=0.5)
    assert relay._cap == 0.5
    request = {'model': 'claude-haiku-5-5', 'messages': [{'role': 'user', 'content': 'Synthetic test'}],
               'max_tokens': 64, 'response_format': {'type': 'json_object'}}
    reply = relay._reply(request)
    assert reply['turn']['content'] == '{"observed": []}'
    assert calls[0][0] == 'claude-haiku-5-5' and calls[0][1]['max_tokens'] == 64
    assert calls[0][2] == {'api_key': 'synthetic-secret', 'base_url': 'http://credential-proxy/v1'}
    assert records[0]['run_id'] == 'run94' and records[0]['conversation_id'] == 'chat94'
    assert records[0]['usage']['completion_tokens'] == 4 and records[0]['key_fp'] == 'test-fp'
    assert guards[-1] == 'after' and 'synthetic-secret' not in str(reply)
    conn.active_run = SimpleNamespace(run_id='newer-run', conversation_id='newer-chat')
    relay._reply(request)
    assert records[1]['run_id'] == 'run94' and records[1]['conversation_id'] == 'chat94'
    def refused():
        raise GatewayError('Paused', stage='llm_key_paused')
    guard.before_call = refused
    blocked = relay._reply(request)
    assert blocked['kind'] == 'stop' and len(calls) == len(records) == 2
    relay.stop()
    assert guards[-1] == 'close'


def test_relay_rejects_model_switch_before_credential_call(monkeypatch, tmp_path):
    from aiscientist.gateway import llm_relay
    monkeypatch.setattr(llm_relay.ModelRelay, 'start', lambda self: self)
    monkeypatch.setenv('AISCIENTIST_LLM_BASE_URL', 'https://example.test/v1')
    monkeypatch.setenv('AISCIENTIST_LLM_MODEL', 'chosen')
    monkeypatch.setattr(app, '_bound_credential', lambda c: None)
    conn = SimpleNamespace(chat_stop=threading.Event(), executor=SimpleNamespace(open_sftp=lambda: None))
    relay = app._mcp_session_model(conn).start(str(tmp_path))
    with pytest.raises(ValueError, match='bound session model'):
        relay._call_request({'model': 'another', 'messages': [{'role': 'user', 'content': 'Test'}]})
    relay.stop()


def test_saved_model_missing_never_uses_global_model(monkeypatch):
    monkeypatch.setenv('AISCIENTIST_LLM_MODEL', 'unrelated-global-model')
    monkeypatch.setattr(app, '_bound_credential', lambda c: {'id': 'saved'})
    with pytest.raises(ValueError, match='no configured model identifier'):
        app._mcp_session_model(SimpleNamespace())


def test_missing_credential_creates_no_guard_or_relay(monkeypatch, tmp_path):
    from aiscientist.gateway import llm_credentials
    from aiscientist.gateway.errors import GatewayError
    monkeypatch.setattr(app, '_bound_credential', lambda c: {'id': 'saved', 'model': 'chosen'})
    monkeypatch.setattr(llm_credentials, 'call_target', lambda *a: None)
    monkeypatch.setattr(app, '_key_use', lambda *a: pytest.fail('No guard before credential availability'))
    conn = SimpleNamespace(owner='test-owner', chat_stop=threading.Event(),
                           executor=SimpleNamespace(open_sftp=lambda: None))
    with pytest.raises(GatewayError, match='credential is unavailable'):
        app._mcp_session_model(conn).start(str(tmp_path))


def test_unpriced_endpoint_cannot_claim_a_cost_limit(monkeypatch, tmp_path):
    monkeypatch.setenv('AISCIENTIST_LLM_BASE_URL', 'https://example.test/v1')
    monkeypatch.setenv('AISCIENTIST_LLM_MODEL', 'unpriced-synthetic-model')
    monkeypatch.setattr(app, '_bound_credential', lambda c: None)
    conn = SimpleNamespace(chat_stop=threading.Event(), executor=SimpleNamespace(open_sftp=lambda: None))
    binding = app._mcp_session_model(conn)
    assert binding.priced is False
    with pytest.raises(ValueError, match='no configured price'):
        binding.start(str(tmp_path), max_cost_usd=1)


def test_relay_on_a_users_key_is_capped_by_the_budget_left_and_carries_its_allowance(monkeypatch, tmp_path):
    """One spending rule for every relayed job: on a user's own key the cap is what is left of their
    budget (never more than the job asked for), and that cap is the key's spike allowance."""
    from aiscientist.gateway import llm_credentials, llm_relay
    cred = {'id': 'test-credential', 'model': 'claude-haiku-5-5', 'provider': 'anthropic',
            'base_url': 'https://api.anthropic.com', 'label': 'Test key'}
    conn = SimpleNamespace(chat_stop=threading.Event(), executor=SimpleNamespace(open_sftp=lambda: None),
                           owner='test-owner', emit=lambda *args, **kw: None, app_user_id='test-user')
    seen = {}
    guard = SimpleNamespace(fingerprint='test-fp', before_call=lambda: None, after_call=lambda: None,
                            close=lambda: None)
    monkeypatch.setattr(app, '_bound_credential', lambda c: cred)
    monkeypatch.setattr(app, '_corpus_scan_cost_cap', lambda c: (1.25, 'what is left of your monthly credit'))
    monkeypatch.setattr(app, '_key_use', lambda *a, **kw: seen.update(kw) or guard)
    monkeypatch.setattr(llm_credentials, 'call_target', lambda *a: ('http://credential-proxy/v1', 'placeholder'))
    monkeypatch.setattr(llm_relay.ModelRelay, 'start', lambda self: self)
    binding = app._mcp_session_model(conn)
    assert binding.start(str(tmp_path))._cap == 1.25                       # the budget left, not the $20 ceiling
    assert seen == {'allowance_usd': 1.25, 'allowance_calls': app._CORPUS_SPIKE_CALLS}
    assert binding.start(str(tmp_path), max_cost_usd=0.5)._cap == 0.5      # a job may ask for less
    assert binding.start(str(tmp_path), max_cost_usd=9.0)._cap == 1.25     # never for more
