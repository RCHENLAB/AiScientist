"""The plan phase as one append-only conversation with the PI (RCHENLAB/AiScientist#122).

The plan phase was many short, unrelated model calls: the draft, the review's PI turn, each answer to
a question, each revision, each reading of a reply. Each began with a system prompt of its own and
then re-sent the same large context (the research-path guidance, the dataset profile, the tools), so
no call could reuse the prompt prefix of another: neither a served model's prefix cache nor an API's
prompt cache was ever read.

Here the PI's calls of a plan phase are turns of ONE conversation. It starts with a fixed prefix (the
PI's system prompt, then the request, the guidance, the dataset profile, the tools and skills, and
the instruction to draft), and messages are only ever added after it: the draft, the review, every
reply and its answer or revision. Every turn after the first sends the prefix unchanged, so it is read
from the cache. The conversation is saved with a waiting plan (``to_dict``), because each reply to a
saved plan is handled by a lab built anew.

Pure data, no model call and no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: The most turns (a user message and the PI's reply) a conversation keeps after its prefix. A review
#: longer than that keeps its first and newest turns; the prefix is never dropped.
MAX_TURNS = 40


@dataclass
class PlanConversation:
    """``messages``: the system prompt and the first user message (the fixed prefix), then the turns."""

    messages: "list[dict[str, str]]" = field(default_factory=list)

    @classmethod
    def start(cls, system: str, first: str) -> "PlanConversation":
        return cls([{"role": "system", "content": system}, {"role": "user", "content": first}])

    @property
    def started(self) -> bool:
        return len(self.messages) >= 2

    @property
    def answered(self) -> bool:
        """Whether the PI has answered the first message (the draft is in the conversation)."""
        return len(self.messages) >= 3 and self.messages[-1]["role"] == "assistant"

    def prefix(self) -> "list[dict[str, str]]":
        """The messages the next turn is sent after: the whole conversation so far."""
        return [dict(m) for m in self.messages]

    def ask(self, content: str) -> "list[dict[str, str]]":
        """The messages for the next turn: the conversation, then ``content`` as the user's turn."""
        return [*self.prefix(), {"role": "user", "content": content}]

    def answer(self, reply: str) -> None:
        """The PI's reply to the first message, which ends the fixed prefix."""
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.append({"role": "assistant", "content": reply or ""})

    def add(self, content: str, reply: str) -> None:
        """One turn: what was asked and the PI's reply, appended."""
        self.messages += [{"role": "user", "content": content},
                          {"role": "assistant", "content": reply or ""}]
        self._trim()

    def _trim(self) -> None:
        head, turns = self.messages[:3], self.messages[3:]
        if len(turns) > 2 * MAX_TURNS:
            keep = 2 * (MAX_TURNS // 2)
            self.messages = head + turns[:keep] + turns[-keep:]

    def to_dict(self) -> "dict[str, Any]":
        return {"messages": [dict(m) for m in self.messages]}

    @classmethod
    def from_dict(cls, data: "Mapping[str, Any] | None") -> "PlanConversation | None":
        """The saved conversation, or ``None`` for none (a plan saved before it existed)."""
        rows = (data or {}).get("messages") if isinstance(data, Mapping) else None
        if not isinstance(rows, list):
            return None
        messages = [{"role": str(m["role"]), "content": str(m.get("content") or "")}
                    for m in rows if isinstance(m, Mapping) and m.get("role") in ("system", "user", "assistant")]
        convo = cls(messages)
        return convo if convo.started and messages[0]["role"] == "system" else None


def shared_prefix(calls: "Iterable[list[Mapping[str, Any]]]") -> "list[int]":
    """For each call's messages, how many leading characters it shares with the call before it: the part
    of its prompt a prefix cache could have served. The measure of #122 that needs no model."""
    out: list[int] = []
    previous = ""
    for messages in calls:
        text = "\x00".join(f"{m.get('role')}\x01{m.get('content')}" for m in messages)
        n = 0
        for a, b in zip(previous, text):
            if a != b:
                break
            n += 1
        out.append(n)
        previous = text
    return out
