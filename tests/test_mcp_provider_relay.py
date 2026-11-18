"""Full-chat forwarding keeps tool semantics and does not forward bearer redirects."""
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aiscientist.gateway import vllm_client
from aiscientist.gateway.errors import GatewayError


@contextmanager
def server(handler):
    http = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{http.server_port}'
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def test_openai_relay_preserves_tool_choice_token_limit_and_usage():
    received = []
    call = {'id': 'c94', 'type': 'function', 'function': {'name': 'collect', 'arguments': '{"v":2}'}}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers.get('Authorization'),
                             json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'model': 'selected', 'choices': [{'message': {'content': None,
                'tool_calls': [call]}, 'finish_reason': 'tool_calls'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 3}}).encode())
        def log_message(self, *args):
            pass
    request = {'model': 'selected', 'messages': [{'role': 'user', 'content': 'Synthetic check'}],
               'max_tokens': 65, 'tools': [{'type': 'function', 'function': {'name': 'collect', 'parameters': {}}}],
               'tool_choice': 'required', 'parallel_tool_calls': False, 'temperature': 0}
    with server(Handler) as endpoint:
        out = vllm_client.relay_chat('selected', request, base_url=endpoint+'/v1', api_key='example-bearer')
    assert received[0][:2] == ('/v1/chat/completions', 'Bearer example-bearer')
    assert received[0][2] == {**request, 'stream': False}
    assert out['tool_calls'] == [call] and out['finish_reason'] == 'tool_calls'
    assert out['usage']['completion_tokens'] == 3


def test_openai_relay_does_not_forward_bearer_to_redirect_destination():
    reached = []
    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            reached.append(self.headers.get('Authorization'))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass
    with server(Destination) as target:
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(302)
                self.send_header('Location', target+'/redirected')
                self.end_headers()
            def log_message(self, *args):
                pass
        with server(Redirect) as endpoint:
            with pytest.raises(GatewayError):
                vllm_client.relay_chat('selected', {'messages': [{'role': 'user', 'content': 'Test'}],
                    'max_tokens': 10}, base_url=endpoint, api_key='example-bearer')
    assert reached == []
