"""Gateway model-queue guarantees; scientific pilot/cache tests live in the native package."""
import json
import urllib.error

import pytest
from test_mcp_relay import body, request, serving

from aiscientist.gateway.errors import GatewayError


def test_priced_queue_records_every_paid_call_and_blocks_calls_after_cap(tmp_path):
    calls, records = [], []
    def provider(req):
        calls.append(req)
        return {"content": '{"items": []}'}, {"prompt_tokens": 1000, "completion_tokens": 20}
    with serving(tmp_path, provider, price=lambda _: 0.01, max_cost_usd=0.02,
                 record=lambda *args: records.append(args)) as (server, relay):
        for _ in range(2):
            with request(server, body()) as response:
                out = json.load(response)
            assert out["aiscientist_usage"]["cost_usd"] == 0.01
            assert out["usage"]["prompt_tokens"] == 1000
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, body())
        assert exc.value.code == 403
        assert len(calls) == len(records) == relay.stats.calls == 2
        assert relay.stats.cost_usd == 0.02
        assert relay.stats.input_tokens == 2000
        assert all(r[2] is None for r in records)
        assert not list((server.directory / "req").iterdir())
        assert not list((server.directory / "resp").iterdir())


def test_a_refused_provider_key_stops_the_queue_without_paid_retries(tmp_path):
    calls, records = [], []
    def provider(req):
        calls.append(req)
        raise GatewayError("Synthetic provider rejected the key", stage="anthropic_auth")
    with serving(tmp_path, provider, record=lambda *args: records.append(args)) as (server, relay):
        for _ in range(2):
            with pytest.raises(urllib.error.HTTPError) as exc:
                request(server, body())
            assert exc.value.code == 403
        assert len(calls) == 1
        assert relay.stats.calls == 0 and relay.stats.failed == 1
        assert len(records) == 1 and records[0][0] is None and records[0][2] == "auth"


def test_a_rate_limit_is_waited_out_by_the_gateway(tmp_path):
    calls, records = [], []
    def provider(req):
        calls.append(req)
        if len(calls) == 1:
            raise GatewayError("Synthetic provider rate limit", stage="anthropic_rate")
        return {"content": '{"items": []}'}, {"prompt_tokens": 8, "completion_tokens": 3}
    with serving(tmp_path, provider, price=lambda _: 0.01,
                 record=lambda *args: records.append(args)) as (server, relay):
        with request(server, body()) as response:
            assert json.load(response)["choices"][0]["message"]["content"] == '{"items": []}'
        assert len(calls) == 2 and relay.stats.calls == 1 and relay.stats.failed == 0
        assert records[0][0] is None and records[0][2] == "rate"
        assert records[1][0]["prompt_tokens"] == 8 and records[1][2] is None
