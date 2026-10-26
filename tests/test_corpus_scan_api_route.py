"""Which way a console Corpus scan goes, and what its chat reply says about cost.

A session on the user's own API key scans with that model through the gateway relay (the key stays
on the Eye Server); a session on the cluster GPU, or on the lab's shared model, keeps the scan's own
GPU job. The reply states the pilot and what the model calls cost."""
from __future__ import annotations

import types

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway import corpus_scan  # noqa: E402


def _conn(credential):
    return types.SimpleNamespace(llm_choice={"credential_id": credential["id"]} if credential else {},
                                 owner="BioAdmin", settings=types.SimpleNamespace(corpus_scan_model="qwen-int4",
                                                                                   vllm_model="qwen-int4"))


@pytest.mark.parametrize("credential, expected", [
    (None, None),
    ({"id": "shared", "builtin": "shared", "provider": "openai", "model": "dsv4"}, None),
    ({"id": "c1", "provider": "anthropic", "model": "claude-haiku-5-5", "base_url": ""},
     {"model": "claude-haiku-5-5", "native_anthropic": True}),
    ({"id": "c2", "provider": "openrouter", "model": "qwen/qwen3", "base_url": "https://openrouter.ai/api/v1"},
     {"model": "qwen/qwen3", "native_anthropic": False}),
])
def test_only_a_session_on_its_own_key_scans_with_its_api_model(monkeypatch, credential, expected):
    monkeypatch.setattr(gw_app, "_session_credential", lambda conn: credential)
    api = gw_app._corpus_scan_api(_conn(credential))
    if expected is None:
        assert api is None
        assert gw_app._corpus_scan_model(_conn(credential)) == corpus_scan.scan_model(_conn(None).settings)
    else:
        assert {k: api[k] for k in expected} == expected and api["cred"] is credential
        # The kept scan is keyed by the model, so a Haiku scan never answers for the GPU's model.
        assert gw_app._corpus_scan_model(_conn(credential)) == expected["model"]


def test_the_reply_states_the_pilot_and_the_cost():
    result = {"n_papers": 1736, "n_requests": 16, "n_gene_requests": 90, "n_keyword_passages": 402,
              "n_passages": 1500, "n_hits_eligible": 110, "n_genes": 44, "definition": "D.",
              "pilot": {"passages": 9, "read": 9, "calls": 9, "cost_usd": 0.0021, "estimated_total_usd": 0.7},
              "api_usage": {"model": "claude-haiku-5-5", "calls": 1620, "input_tokens": 2_300_000,
                            "output_tokens": 410_000, "cost_usd": 0.4355}}
    md = gw_app._corpus_summary_md(result, [], model="claude-haiku-5-5")
    assert "Pilot first: 9 of 9 passages read, $0.0021, projecting about $0.70" in md
    assert "Model cost on your API key: **$0.44** (1620 calls, 2,300,000 input / 410,000 output tokens)" in md
    assert "every passage that states a dosage mechanism in words: 402" in md
    assert "**How a gene qualifies**" in md and "Definition used" not in md
    kept = gw_app._corpus_summary_md(result, [], model="claude-haiku-5-5", kept_since="2026-10-21")
    assert "answering from it costs nothing" in kept
    # A scan on the cluster GPU has no API cost line.
    gpu = gw_app._corpus_summary_md({k: v for k, v in result.items() if k != "api_usage"}, [], model="qwen")
    assert "Model cost" not in gpu
