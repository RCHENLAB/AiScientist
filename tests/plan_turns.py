"""How the tests' scripted models tell the lab's model calls apart (RCHENLAB/AiScientist#122).

A call of its own says what it asks in its system prompt. A turn of the plan phase's conversation
(the review's PI round, the reading of a reply, an answer, a revision) is sent after the draft's fixed
prefix, so its system prompt is the PI's; what it asks is in its last message, which starts with that
instruction (``ResearchLab._pi_turn``).
"""

from __future__ import annotations

from aiscientist.agents.research_lab.prompts import (
    _PLAN_QA_SYSTEM,
    _PLAN_REPLY_SYSTEM,
    _PLAN_REVIEW_PI_SYSTEM,
    _PLAN_REVISE_SYSTEM,
)

TURNS = (_PLAN_QA_SYSTEM, _PLAN_REPLY_SYSTEM, _PLAN_REVISE_SYSTEM, _PLAN_REVIEW_PI_SYSTEM)


def turn(messages) -> "str | None":
    """The last message of a conversation turn (its instruction, then its body), or ``None``."""
    last = str((messages[-1] or {}).get("content") or "") if messages else ""
    if len(messages) > 2 and messages[-1].get("role") == "user" and any(last.startswith(t) for t in TURNS):
        return last
    return None


def instruction(messages) -> str:
    """What the call asks: a turn's instruction and body, else the system prompt."""
    return turn(messages) or str(messages[0].get("content") or "")


def body(messages) -> str:
    """What the call is given: a turn's last message, else the first user message."""
    return turn(messages) or str(messages[1].get("content") or "")
