"""The gateway side of "a step outside the plan runs only if the user approves it".

The lab asks through the decision card (tests/test_hypothesis_exploration.py covers what it does
with the answer). Here: the card for a proposed step waits its own time, an unanswered one is closed
and reported as NOT added — an ordinary decision point "proceeds with the agent's judgment" on a
timeout, which for a proposed step would be the opposite of asking — and the chat says what was
proposed and left out.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_conns():
    yield
    gw_app.CONNECTIONS.clear()


def _conn():
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.status = "ready"
    conn.begin_run("conv-1")
    pushed: list[dict] = []
    conn.push = pushed.append
    return conn, pushed


def _proposal(timeout_s: float):
    return SimpleNamespace(id="add-step-1", kind="add_step", decision=True, timeout_s=timeout_s,
                           options=("Add this step", "Do not add it"),
                           goal="The result of “CellQC quality control” prompted a step that is NOT "
                                "in your plan: **Ambient rod-leak audit** — … It runs only if you "
                                "approve it; without a reply it is left out and listed at the end.")


def test_an_unanswered_proposal_is_closed_and_reported_as_not_added():
    conn, pushed = _conn()
    said: list[str] = []
    out = gw_app._decision_review(conn, lambda _lvl, _stage, msg: said.append(msg), _proposal(0.05))
    assert out == {"action": "proceed", "timed_out": True}
    assert [p["type"] for p in pushed] == ["decision_prompt", "plan_done"]     # the card is closed
    assert pushed[0]["options"] == ["Add this step", "Do not add it"]
    assert "runs only if you approve it" in said[0]
    assert any("was NOT added" in m for m in said)
    assert not any("agent's judgment" in m for m in said)


def test_the_users_answer_to_a_proposal_comes_back_as_their_choice():
    conn, _pushed = _conn()

    def answer():
        for _ in range(400):
            if conn.pending_plan is not None:
                break
            time.sleep(0.005)
        conn.plan_value = {"action": "revise", "feedback": "Add this step"}
        conn.plan_event.set()

    t = threading.Thread(target=answer)
    t.start()
    out = gw_app._decision_review(conn, lambda *_a: None, _proposal(5))
    t.join()
    assert out == {"action": "proceed", "choice": "Add this step"}


def test_an_ordinary_decision_point_keeps_its_own_timeout_meaning():
    conn, pushed = _conn()
    said: list[str] = []
    node = SimpleNamespace(id="s2", goal="Use the labels or re-cluster?", options=("a", "b"))
    out = gw_app._decision_review(conn, lambda _l, _s, msg: said.append(msg), node, timeout=0.05)
    assert out == {"action": "proceed", "timed_out": True}
    assert any("agent's judgment" in m for m in said) and [p["type"] for p in pushed] == ["decision_prompt"]


@pytest.mark.parametrize("decision,words", [
    ("declined", "you declined it"), ("unanswered", "no reply to the approval request"),
    ("not_asked", "nobody to ask"),
])
def test_the_chat_says_what_was_proposed_and_why_it_was_left_out(decision, words):
    lines = gw_app._lab_event_to_chat({
        "type": "step_proposed", "decision": decision, "after_step": "CellQC quality control",
        "step": "**Ambient rod-leak audit** — Using the normalised checkpoint and the raw matrix …",
        "why": "SoupX under-corrects ambient rod-photoreceptor transcripts"})
    text = " ".join(str(p.get("text") or "") for p in lines)
    assert "Proposed, NOT added" in text and words in text
    assert "Ambient rod-leak audit" in text and "SoupX under-corrects" in text
