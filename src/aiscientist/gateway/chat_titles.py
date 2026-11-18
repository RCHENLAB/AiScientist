"""A chat's title from what its first request asks for.

The page titles a new chat with the opening words of its first message. Requests that open alike
("Run VEP and ClinVar on the variants in the …") then read alike in the sidebar. Here the session's
model names the request in a few words; the gateway stores that title and tells the page.

A title the user typed is never replaced, and a chat is named this way once. Any failure leaves the
page's title in place.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from sqlalchemy import func, select

from .db import session_scope
from .models import Conversation, Message

MAX_WORDS = 8
MAX_CHARS = 60

_SYSTEM = (
    "You name a chat in a research console. Read the user's request and reply with a title of at "
    f"most {MAX_WORDS} words that says what the user wants done: the analysis or the question, not the "
    "file names. Write it as a short noun phrase in the language of the request, with no quotation "
    "marks, no trailing period and nothing else."
)


def intent_title(complete: "Callable[[list[dict]], str] | None", question: str) -> str:
    """The model's title for ``question``, cleaned; "" when it cannot be had or is not usable."""
    text = " ".join(str(question or "").split())
    if complete is None or not text:
        return ""
    try:
        raw = complete([{"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": text[:2000]}])
    except Exception as exc:  # noqa: BLE001 - the page's title stays
        print(f"[title] the model did not name the chat: {type(exc).__name__}: {exc}")
        return ""
    return clean_title(raw)


def clean_title(raw: str) -> str:
    """One line, no wrapping quotes or markdown, within the limits; "" for an empty or runaway reply
    (a model that answered the request instead of naming it)."""
    lines = [ln.strip() for ln in str(raw or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    title = re.sub(r"^(title|标题)\s*[:：]\s*", "", lines[0], flags=re.IGNORECASE)
    title = re.sub(r"[*_`#]", "", title).strip().strip("\"'“”‘’「」").rstrip(".。").strip()
    if not title or len(title.split()) > MAX_WORDS * 2 or len(title) > MAX_CHARS * 2:
        return ""
    words = title.split()
    if len(words) > MAX_WORDS:
        title = " ".join(words[:MAX_WORDS])
    return title[:MAX_CHARS].strip()


def dataset_stem(names: "list[str]") -> str:
    """The attached data as the page writes it in a title: "Ddx41_DEG1", or "Ddx41_DEG1 +2"."""
    names = [n for n in (str(x or "").strip() for x in names) if n]
    if not names:
        return ""
    name = names[0].rstrip("/").split("/")[-1]
    stem = re.sub(r"\.[A-Za-z0-9]{1,6}$", "", re.sub(r"\.(gz|bgz|zip)$", "", name, flags=re.IGNORECASE))[:22]
    return stem + (f" +{len(names) - 1}" if len(names) > 1 else "")


def wants_title(user_id: int, conversation_id: int) -> bool:
    """True for this user's chat that still carries the page's own title (or none) and is at its
    first request: a later message ("yes, go on") does not say what the chat is about."""
    with session_scope() as s:
        conv = s.get(Conversation, conversation_id)
        if conv is None or conv.user_id != user_id or (conv.title_source or "prompt") != "prompt":
            return False
        asked = s.scalar(select(func.count()).select_from(Message).where(
            Message.conversation_id == conversation_id, Message.role == "user"))
        return (asked or 0) <= 1


def store(user_id: int, conversation_id: int, intent: str, data: "list[str]") -> str:
    """Give the chat its intent title. Returns the stored title, or "" when nothing changed: no such
    chat of this user, a title the user typed, or a chat already named this way."""
    if not intent:
        return ""
    stem = dataset_stem(data)
    title = (f"{stem} · {intent}" if stem else intent)[:255]
    with session_scope() as s:
        conv = s.get(Conversation, conversation_id)
        if conv is None or conv.user_id != user_id or (conv.title_source or "prompt") != "prompt":
            return ""
        taken = set(s.scalars(select(Conversation.title).where(
            Conversation.user_id == user_id, Conversation.id != conversation_id)).all())
        if title in taken and conv.created_at is not None:
            title = f"{title} · {conv.created_at.strftime('%m-%d %H:%M')}"[:255]
        conv.title = title
        conv.title_source = "intent"
        s.commit()
        return title
