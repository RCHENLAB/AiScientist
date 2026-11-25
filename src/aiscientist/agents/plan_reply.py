"""What a reply to a plan under review asks for, read by the model (RCHENLAB/AiScientist#120).

A reply to a pending plan used to be sorted by fixed word lists (``classify_plan_reply``) into ONE of
"question" or "change". A reply goes one way only, so "Which datasets does step 2 use? Use X
instead" was answered and its change dropped, and a request to look something up first ("look for
the datasets again and write them into the plan") was not a kind the rules knew: it became a change
with nothing to change, and the reviser wrote the reply into the step.

Now one model call reads the reply and returns each part separately: a ``question`` (answered,
changes nothing), a ``change`` (made on the plan document) and a ``lookup`` (the planner looks it up
before it revises: today the dataset catalogue, ``plan_datasets``). The word lists stay as the
fallback when that call fails or its reply cannot be read, with look-up words added so the fallback
still knows that kind.

``copies_reply`` is the check that a revision did not just paste the reply into a step.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping



REPLY_KINDS = ("question", "change", "lookup")

#: How many earlier exchanges of the same review go into the next call.
HISTORY_TURNS = 6


@dataclass(frozen=True)
class ReplyPart:
    kind: str
    text: str


@dataclass(frozen=True)
class ReplyReading:
    """The parts of one reply, in the order the researcher wrote them. ``source`` is ``"model"`` when
    the model read it, ``"words"`` when the word lists did (no model, or its reply was unusable)."""

    parts: "tuple[ReplyPart, ...]"
    source: str

    def texts(self, kind: str) -> "list[str]":
        return [p.text for p in self.parts if p.kind == kind]

    @property
    def questions(self) -> "list[str]":
        return self.texts("question")

    @property
    def changes(self) -> "list[str]":
        return self.texts("change")

    @property
    def lookups(self) -> "list[str]":
        return self.texts("lookup")

    def kinds(self) -> "list[str]":
        return list(dict.fromkeys(p.kind for p in self.parts))

    def to_dict(self) -> "dict[str, Any]":
        return {"source": self.source, "parts": [{"kind": p.kind, "text": p.text} for p in self.parts]}


# --- the fallback: the word lists ----------------------------------------------------------------

# A request to look DATASETS up first: a word for a dataset (never bare "data" or "file": "find the DE
# genes in this data" asks for an analysis) together with a word that asks what exists or to look.
DATASET_NOUNS = ("dataset", "data set", "atlas", "h5ad", "数据集", "图谱", "共享数据")
_LOOKUP_WORDS = (
    "look for", "look up", "search", "find", "which", "what", "available", "do i have", "do we have",
    "list", "找", "查", "搜", "哪些", "哪个", "有什么", "可用", "能用", "列出",
)
# A question about the plan itself ("Which datasets does step 2 use?") is a question, not a look-up.
_ABOUT_THE_PLAN = re.compile(r"\bstep\s*\d|\bplan (?:use|read|name)s?\b|第\s*\d+\s*步|计划(?:里|中)(?:用|读)")
# "…and write them into the plan": the look-up feeds a change.
_INTO_PLAN = (
    "write", "put", "add", "into the plan", "in the plan", "to the plan", "update the plan", "use it",
    "use them", "use that", "fill", "写进", "写入", "写到", "加到", "加进", "放进", "放到", "填", "更新计划",
    "用它", "用这些", "改",
)


def _has(text: str, words: "Iterable[str]") -> bool:
    low = text.lower()
    return any(w in low for w in words)


def about_datasets(text: str) -> bool:
    """Whether ``text`` names datasets (``DATASET_NOUNS``): what a look-up the catalogue can serve is
    about."""
    return _has(text, DATASET_NOUNS)


# What a look-up the catalogue serves is about: datasets, or a dataset's versions, files or columns.
_DATA_DETAIL = (*DATASET_NOUNS, "file", "version", "column", "文件", "版本", "列名")


def lookup_about_data(text: str) -> bool:
    """Whether a look-up (with the change it feeds) is about data the catalogue lists. One that is not
    ("find the DE genes between KO and WT and add a volcano plot") asks for analysis, and is handled
    as the rest of the reply, never refused for want of a catalogue."""
    return _has(text, _DATA_DETAIL)


def _asks_for_datasets(sentence: str) -> bool:
    return (about_datasets(sentence) and _has(sentence, _LOOKUP_WORDS)
            and not _ABOUT_THE_PLAN.search(sentence.lower()))


# Sentence ends: a question and the change after it ("Which datasets does step 2 use? Use X instead")
# are read one sentence at a time. An English full stop ends a sentence only before a capital, so
# "0.5" and "e.g. this" stay whole.
_SENTENCE_END = re.compile(r"(?<=[?？!！。；;])\s*|(?<=\.)\s+(?=[A-Z])")


def _sentences(text: str) -> "list[str]":
    return [s.strip() for s in _SENTENCE_END.split(text) if s and s.strip()]


def fallback_reading(text: str) -> ReplyReading:
    """The reply read by the word lists, one sentence at a time: a question, a change, or a look-up
    and the change it feeds. Neighbouring sentences of one kind make one part."""
    # Imported here: the lab package imports this module, and its plan_parse needs nothing of it.
    from .research_lab.plan_parse import classify_plan_reply

    t = (text or "").strip()
    parts: list[ReplyPart] = []

    def add(kind: str, said: str) -> None:
        if parts and parts[-1].kind == kind:
            parts[-1] = ReplyPart(kind, f"{parts[-1].text} {said}")
        else:
            parts.append(ReplyPart(kind, said))

    for sentence in _sentences(t) or [t]:
        if _asks_for_datasets(sentence):
            add("lookup", sentence)
            if _has(sentence, _INTO_PLAN) or classify_plan_reply(sentence) == "change":
                add("change", sentence)
        else:
            add(classify_plan_reply(sentence), sentence)
    return ReplyReading(tuple(parts), "words")


# --- the model's reading --------------------------------------------------------------------------

def _history_block(history: "Iterable[Mapping[str, Any]]") -> str:
    turns = [h for h in history if isinstance(h, Mapping)][-HISTORY_TURNS:]
    if not turns:
        return ""
    lines = ["Earlier in this review:"]
    for h in turns:
        if h.get("reply"):
            lines.append(f"Researcher: {str(h['reply'])[:600]}")
        if h.get("answer"):
            lines.append(f"You: {str(h['answer'])[:900]}")
    return "\n".join(lines)


def reply_prompt(reply: str, plan_text: str, history: "Iterable[Mapping[str, Any]]" = ()) -> str:
    """The user message of the reading call: the plan, the review so far and the reply."""
    parts = [f"The plan waiting for approval:\n{plan_text.strip()}"]
    earlier = _history_block(history)
    if earlier:
        parts.append(earlier)
    parts.append(f"The researcher's reply:\n{reply.strip()}")
    parts.append("Return the JSON object now.")
    return "\n\n".join(parts)


def parse_reading(raw: str, reply: str) -> "ReplyReading | None":
    """The model's reading, or ``None`` when its reply is not one: not JSON, no parts, an unknown kind,
    or a part with no text."""
    text = (raw or "").strip()
    obj: Any = None
    for candidate in (text, *(m.group(0) for m in [re.search(r"\{.*\}", text, re.S)] if m)):
        try:
            obj = json.loads(candidate)
            break
        except (ValueError, TypeError):
            continue
    if isinstance(obj, Mapping):
        obj = obj.get("parts")
    if not isinstance(obj, list) or not obj:
        return None
    parts: list[ReplyPart] = []
    for item in obj:
        if not isinstance(item, Mapping):
            return None
        kind = str(item.get("kind") or "").strip().lower()
        said = " ".join(str(item.get("text") or "").split())
        if kind not in REPLY_KINDS:
            return None
        parts.append(ReplyPart(kind, said or reply.strip()))
    return ReplyReading(tuple(parts), "model")


def read_plan_reply(reply: str, *, plan_text: str, complete: "Callable[[list[dict[str, Any]]], str] | None",
                    system: str, history: "Iterable[Mapping[str, Any]]" = (),
                    messages: "list[dict[str, Any]] | None" = None) -> ReplyReading:
    """Read ``reply`` with one model call; the word lists when there is no model or its answer
    cannot be used. ``messages`` are the conversation the call continues (the plan phase's own,
    #122); without them the call is a conversation of its own under ``system``."""
    if complete is None:
        return fallback_reading(reply)
    ask = {"role": "user", "content": reply_prompt(reply, plan_text, history)}
    convo = [*messages, ask] if messages else [{"role": "system", "content": system}, ask]
    try:
        raw = complete(convo)
    except Exception:  # noqa: BLE001 - reading a reply must never cost the plan
        return fallback_reading(reply)
    return parse_reading(raw, reply) or fallback_reading(reply)


# --- a revision that only repeats the reply ------------------------------------------------------

_CJK = re.compile(r"[㐀-鿿]")


def _tokens(text: str) -> "list[str]":
    """Words, and each CJK character as a word of its own (Chinese is not written with spaces)."""
    flat = re.sub(r"[*_`#>\[\]()\"'“”‘’]", " ", (text or "").lower())
    out: list[str] = []
    for word in re.findall(r"[\w㐀-鿿-]+", flat):
        if _CJK.search(word):
            out.extend(ch for ch in word if not ch.isspace())
        else:
            out.append(word)
    return out


# Words a researcher says TO the planner, which never describe an analysis: a step that holds them
# repeats the request instead of carrying it out ("look for the datasets again and write them into the
# plan"). A step dictated word for word ("add a step: run `run_de` comparing KO with WT in the rods")
# holds none of them once the "add a step" is left out, and is the change asked for.
_TO_THE_PLANNER = (
    "look for", "look up", "search for", "find out", "write them", "write it", "put them", "put it",
    "into the plan", "in the plan", "to the plan", "the plan", "again", "please", "could you", "can you",
    "找一下", "找找", "查一下", "查查", "搜一下", "再去", "重新找", "写进", "写入", "写到", "放进", "计划",
    "帮我", "请",
)


def _words(tokens: "list[str]") -> str:
    """Tokens joined back for a phrase search: words with spaces, CJK characters without."""
    out = ""
    for tok in tokens:
        out += tok if _CJK.search(tok) or not out or _CJK.search(out[-1]) else " " + tok
    return out


def copies_reply(step_text: str, reply: str, *, share: float = 0.8, min_words: int = 8) -> bool:
    """Whether ``step_text`` repeats the researcher's ``reply`` instead of carrying it out: one
    unbroken run of the reply's words, in order, covers ``share`` of the reply, and that run holds
    words said to the planner (``_TO_THE_PLANNER``). A reply shorter than ``min_words`` words
    (characters, for Chinese) is never judged: a step that carries out "add a dot plot of the
    markers" may well say "a dot plot of the markers"; and a step the reply dictated is the change it
    asked for."""
    said = _tokens(reply)
    if len(said) < min_words:
        return False
    step = _tokens(step_text)
    if not step:
        return False
    match = difflib.SequenceMatcher(None, said, step, autojunk=False).find_longest_match(
        0, len(said), 0, len(step))
    if match.size < max(min_words, share * len(said)):
        return False
    return _has(_words(said[match.a:match.a + match.size]), _TO_THE_PLANNER)
