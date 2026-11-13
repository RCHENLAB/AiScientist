"""MCP model jobs follow the chosen session and never bypass the saved-key guard."""
from types import SimpleNamespace

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


@pytest.mark.parametrize('credential', [{'id': 'saved-key'}, {'builtin': 'shared'}])
def test_other_chosen_endpoint_cannot_fall_back_to_old_gpu(monkeypatch, credential):
    monkeypatch.delenv('AISCIENTIST_LLM_BASE_URL', raising=False)
    monkeypatch.setattr(app, '_bound_credential', lambda conn: credential)
    conn = SimpleNamespace(alloc=SimpleNamespace(node='old-gpu', port=8001), selected_model='old-model')
    with pytest.raises(ValueError, match='existing tools'):
        app._mcp_session_model(conn)


def test_configured_external_endpoint_cannot_be_replaced_with_gpu(monkeypatch):
    monkeypatch.setenv('AISCIENTIST_LLM_BASE_URL', 'https://example.test/v1')
    monkeypatch.setattr(app, '_bound_credential', lambda conn: None)
    with pytest.raises(ValueError, match='existing tools'):
        app._mcp_session_model(SimpleNamespace())
