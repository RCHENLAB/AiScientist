"""Offline tests for in-flight run replay on WS reconnect — no cluster/SSH/network.

A client that refreshes or navigates back loses its socket, but the server-side
``Connection`` and the asyncio run task survive. ``Connection`` captures the in-flight
assistant turn from pushed messages (``_track_stream``) and rebuilds it as an ordered
list of WS payloads (``stream_replay_payloads``) so the reconnecting client restores the
live centre bubble (thinking + key progress + text) and the run keeps streaming.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway.app import Connection  # noqa: E402
from bioagent.gateway.settings import HPCSettings  # noqa: E402


def _conn():
    # No subscribers → push() never touches the loop; a throwaway loop is enough.
    return Connection(HPCSettings.from_env(), mock=True, loop=asyncio.new_event_loop(), username="tester")


def _types(payloads):
    return [p["type"] for p in payloads]


def test_no_stream_before_any_run():
    assert _conn().stream_replay_payloads() == []


def test_running_lab_replays_start_thinking_progress():
    c = _conn()
    c.push({"type": "chat_start"})
    c.push({"type": "chat_thinking", "token": "planning…\n"})
    c.push({"type": "lab_progress", "text": "📋 Plan ready — 3 steps", "level": "info"})
    c.push({"type": "lab_progress", "text": "✅ Step accepted", "level": "success"})
    c.push({"type": "chat_thinking", "token": "→ run_scanpy_qc\n"})

    out = c.stream_replay_payloads()
    assert _types(out) == ["chat_start", "chat_thinking", "lab_progress", "lab_progress"]
    # thinking is consolidated into a single token blob…
    assert out[1]["token"] == "planning…\n→ run_scanpy_qc\n"
    # …and the key-progress lines preserve order + level
    assert out[2]["text"].startswith("📋 Plan ready")
    assert out[3]["level"] == "success"
    # no terminal yet — the run is still live, so the bubble stays open for live tokens
    assert "chat_done" not in _types(out)


def test_finished_run_replays_text_and_terminal_and_artifacts():
    c = _conn()
    c.push({"type": "chat_start"})
    c.push({"type": "chat_token", "token": "# Title\n\nbody"})
    arts = {"type": "artifacts", "items": [{"name": "report.pdf"}], "bundle_url": "/api/bundle/x/y"}
    c.push(arts)
    c.push({"type": "chat_done"})

    out = c.stream_replay_payloads()
    assert _types(out) == ["chat_start", "chat_token", "chat_done", "artifacts"]
    assert out[1]["token"] == "# Title\n\nbody"
    assert out[3]["items"][0]["name"] == "report.pdf"


def test_chat_start_resets_previous_turn():
    c = _conn()
    c.push({"type": "chat_start"})
    c.push({"type": "chat_token", "token": "stale"})
    c.push({"type": "chat_done"})
    c.push({"type": "chat_start"})  # a NEW run must not carry the old text/terminal
    c.push({"type": "chat_thinking", "token": "fresh\n"})

    out = c.stream_replay_payloads()
    assert _types(out) == ["chat_start", "chat_thinking"]
    assert all("stale" not in p.get("token", "") for p in out)


def test_error_terminal_carries_message():
    c = _conn()
    c.push({"type": "chat_start"})
    c.push({"type": "chat_error", "message": "vLLM dropped"})
    out = c.stream_replay_payloads()
    assert out[-1]["type"] == "chat_error" and out[-1]["message"] == "vLLM dropped"


def test_summary_exposes_chat_running():
    c = _conn()
    assert c.summary()["chat_running"] is False
    c.chat_running = True
    assert c.summary()["chat_running"] is True


def test_token_before_start_is_ignored():
    c = _conn()
    c.push({"type": "chat_token", "token": "orphan"})  # no chat_start yet
    assert c.stream_replay_payloads() == []


def test_finished_run_replay_carries_run_id_for_client_dedup():
    # A reconnecting client that missed the live completion recovers the run; it must
    # be able to tell WHICH run this is (by run_id) so it can dedupe against what it
    # already has. run_id is captured from the artifacts bundle_url.
    c = _conn()
    c.push({"type": "chat_start"})
    c.push({"type": "chat_token", "token": "# Report"})
    c.push({"type": "artifacts", "items": [{"name": "report.pdf"}],
            "bundle_url": "/api/bundle/BioAdmin/407300d229da"})
    c.push({"type": "run_complete", "run_id": "407300d229da", "agenda": ["qc", "cluster"]})
    c.push({"type": "chat_done"})

    out = c.stream_replay_payloads()
    assert out[0]["type"] == "chat_start"
    assert out[0]["run_id"] == "407300d229da"
    # run_complete (with run_id + agenda) is replayed so the client can rebind
    # regenerate/re-run state after recovering a missed run.
    rc = [p for p in out if p["type"] == "run_complete"]
    assert rc and rc[0]["run_id"] == "407300d229da" and rc[0]["agenda"] == ["qc", "cluster"]


def test_run_id_from_run_complete_when_no_artifacts():
    c = _conn()
    c.push({"type": "chat_start"})
    c.push({"type": "chat_token", "token": "done"})
    c.push({"type": "run_complete", "run_id": "abc123", "agenda": []})
    c.push({"type": "chat_done"})
    out = c.stream_replay_payloads()
    assert out[0].get("run_id") == "abc123"
