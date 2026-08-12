"""Context awareness + **compaction** for the fast chat path.

Why this exists
---------------
``quick_chat.run_quick_chat`` used to manage its context with exactly one lever: keep the
last ``max_history_messages`` (12) messages and silently drop everything older. On a long
conversation that means the assistant forgets turn 7 while the served window still has
~100K tokens free — the user gets amnesia with no warning, no report, and no way to see it
coming. This module replaces that blunt cutoff with the pattern the RESEARCH path already
uses (``research_harness._budget_messages``), adapted to a conversation instead of a
tool-calling transcript:

    system prompt + [rolling summary of older turns] + the last N exchanges VERBATIM + question

Two decisions are worth stating, because both look "wrong" until you know why.

1. **Chat targets a BOUNDED budget, not the raw window.** The served model runs with
   ``--max-model-len`` 131072 in prod, but chat self-limits to ``max_prompt_tokens``
   (24K by default). Prefill time scales with prompt length: a 100K-token prompt costs
   seconds of GPU before the FIRST token appears, which destroys the "opening sentence
   lands immediately" property that is the entire point of the fast path. A research step
   can afford that latency; a chat turn cannot. We still never exceed the real window —
   the budget is clamped to ``min(max_prompt_tokens, max_model_len - output_reserve)``.

2. **Older turns are SUMMARIZED, not dropped.** When the budget is exceeded the turns
   that fall out of the verbatim window are folded into a short model-written briefing
   that rides along as one compact message, so the chat keeps its memory of what was
   discussed. The summary is maintained INCREMENTALLY (previous summary + only the
   newly-evicted turns), so summarization cost stays flat as the conversation grows
   instead of re-reading the whole history every turn.

Degradation is deliberate and total: if there is no summarizer, or it errors, or it
returns junk, compaction falls back to dropping the oldest turns — i.e. exactly today's
behaviour. Context management must never be the thing that breaks a chat turn.

Testability mirrors ``quick_chat``: the exact token counter and the summarizer are both
INJECTED, so this module imports nothing from the gateway and runs offline.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

# The research path's calibrated estimator primitives, reused rather than re-derived so the
# two paths cannot drift apart (``_CHARS_PER_TOKEN`` is deliberately low = overcounts, and
# ``_default_max_model_len`` reads the same BIOAGENT_VLLM_MAX_MODEL_LEN the serve job uses).
from .research_harness import (  # noqa: F401 - _approx_tokens is used below
    _approx_tokens,
    _default_max_model_len,
    _msg_tokens,
)

# count_tokens_fn(messages, tools) -> EXACT prompt tokens, or None when the served backend
# cannot tokenize (remote API / older server). Same contract as the research harness.
CountTokensFn = Callable[[list[dict[str, Any]], list[dict[str, Any]]], "int | None"]
# summarize_fn(messages, max_tokens) -> assistant text. One cheap, non-streaming,
# thinking-OFF completion; the gateway binds it to vllm_client.complete.
SummarizeFn = Callable[[list[dict[str, Any]], int], str]
EventFn = Callable[[dict[str, Any]], None]

_DEFAULT_MAX_PROMPT_TOKENS = 24_000
_DEFAULT_KEEP_EXCHANGES = 6
_MIN_PROMPT_TOKENS = 512          # floor so a pathological config still leaves working room
_MIN_SUMMARY_CHARS = 15           # shorter than this is not a summary, it is noise → degrade
# Extra reserve for the ONE re-tighten pass when the char estimate undershot the exact count
# (dense JSON tokenizes denser than prose). Mirrors HarnessConfig.context_retry_extra_tokens.
_OVERFLOW_RESERVE = 3072
_SUMMARY_MSG_HEAD = 1200          # chars kept per evicted message when writing the summarizer prompt
_SUMMARY_PROMPT_CHARS = 12_000    # total transcript handed to the summarizer (keeps the call cheap)

# Prefixes the injected memory message. Phrased so the model treats it as background it
# already knows, not as a new instruction or a user question to answer.
_SUMMARY_PREFIX = (
    "CONVERSATION MEMORY — a compacted summary of earlier turns in this same chat that are no "
    "longer shown verbatim. Treat it as things you and the user already established. Do not "
    "mention that a summary exists, and do not answer it; answer the user's latest message.\n\n"
)

_SUMMARY_SYSTEM = (
    "You maintain a running memory of a chat between a user and a bioinformatics research "
    "assistant. Rewrite the material below into ONE compact briefing (at most 200 words) that "
    "lets the assistant continue the conversation without seeing the older messages.\n"
    "Keep the concrete specifics: gene symbols, variant and accession identifiers, file and "
    "dataset names, HPO terms, numbers, what the user is trying to achieve, and any decision or "
    "conclusion already reached. Drop pleasantries, restated boilerplate, and anything the newer "
    "messages already supersede. Never invent facts that are not in the material.\n"
    "Reply with the briefing text only — no preamble, no headings, no bullet characters."
)


def _default_max_prompt_tokens() -> int:
    """Chat's self-imposed prompt ceiling (NOT the served window). Env-overridable so an
    operator can trade first-token latency for memory without a redeploy."""
    try:
        return int(os.environ.get("BIOAGENT_CHAT_MAX_PROMPT_TOKENS", str(_DEFAULT_MAX_PROMPT_TOKENS)))
    except ValueError:
        return _DEFAULT_MAX_PROMPT_TOKENS


def _default_keep_exchanges() -> int:
    """How many recent exchanges survive compaction VERBATIM. Recent wording matters (the
    user says "that gene" / "the second one"), so the tail is never summarized."""
    try:
        return max(1, int(os.environ.get("BIOAGENT_CHAT_KEEP_EXCHANGES", str(_DEFAULT_KEEP_EXCHANGES))))
    except ValueError:
        return _DEFAULT_KEEP_EXCHANGES


@dataclass
class ChatContextLimits:
    """The context knobs. ``QuickChatConfig`` inherits these, so the fast path exposes them
    as ``QuickChatConfig.max_prompt_tokens`` etc. while they are DEFINED once, here."""

    # Chat's own ceiling — see the module docstring for why it is far below the served window.
    max_prompt_tokens: int = field(default_factory=_default_max_prompt_tokens)
    # The real served window (BIOAGENT_VLLM_MAX_MODEL_LEN); only ever used to CLAMP the above.
    max_model_len: int = field(default_factory=_default_max_model_len)
    output_reserve_tokens: int = 2048        # room for the reply (matches vllm_client defaults)
    keep_last_exchanges: int = field(default_factory=_default_keep_exchanges)
    # Outer sanity bound on how many raw history messages are even considered. This is NOT
    # the memory limit any more (compaction is) — it only stops a pathological 10k-message
    # history from costing a pile of estimator work. Anything older falls off unsummarized.
    max_history_messages: int = 200
    summary_max_tokens: int = 512            # reply budget for the summarizer call (cheap)
    summary_max_chars: int = 2400            # hard cap on the injected memory message

    @property
    def allowed_prompt_tokens(self) -> int:
        """The prompt budget actually enforced: chat's own ceiling, never above what the
        served window leaves once the reply is reserved."""
        hard = self.max_model_len - self.output_reserve_tokens
        return max(_MIN_PROMPT_TOKENS, min(self.max_prompt_tokens, hard))


@dataclass
class ChatContextReport:
    """What the compactor built, plus everything the UI and the next turn need."""

    messages: list[dict[str, Any]]
    used_tokens: int = 0
    allowed_tokens: int = 0
    exact: bool = False               # True when used_tokens came from the server tokenizer
    compacted: bool = False           # True when anything was summarized or dropped
    summary: str = ""                 # the rolling memory to hand back on the NEXT turn
    summary_through: int = 0          # how many history messages it accounts for
    summarized_messages: int = 0      # folded into the summary THIS turn
    dropped_messages: int = 0         # discarded without summarization THIS turn


# --- internals ----------------------------------------------------------------


def _clean_history(history: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """Only real user/assistant prose survives — the same filter the loop always applied."""
    out: list[dict[str, str]] = []
    for message in history or []:
        role, content = message.get("role"), message.get("content")
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": str(content)})
    return out


def _split_exchanges(body: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    """Group messages into exchanges — a new one starts at each ``user`` message, so a
    question and the answer(s) it produced are kept, summarized, or evicted TOGETHER."""
    groups: list[list[dict[str, str]]] = []
    for message in body:
        if message.get("role") == "user" or not groups:
            groups.append([message])
        else:
            groups[-1].append(message)
    return groups


def _assemble(system: str, summary: str, body: list[dict[str, str]], question: str) -> list[dict[str, Any]]:
    """system (+ memory) → verbatim tail → the new question.

    The memory is SPLICED INTO the system message instead of riding as its own ``system``
    message. That is not a style choice: the served model's OWN chat template rejects it.
    Qwen3.6's ``chat_template.jinja`` contains

        {%- if message.role == "system" %}{%- if not loop.first %}
            {{- raise_exception('System message must be at the beginning.') }}

    so a SECOND system message raises at template-render time and vLLM 400s the request —
    i.e. every chat turn that compacted would fail outright, exactly when memory matters
    most. Verified 2026-07-27 by rendering the real staged template both ways: two system
    messages → ``TemplateError: System message must be at the beginning``; spliced → renders
    clean. ``_SUMMARY_PREFIX`` keeps the memory delimited and greppable inside the prompt.
    """
    head = f"{system}\n\n{_SUMMARY_PREFIX}{summary}" if summary else system
    messages: list[dict[str, Any]] = [{"role": "system", "content": head}]
    messages.extend({"role": m["role"], "content": m["content"]} for m in body)
    messages.append({"role": "user", "content": question})
    return messages


def _estimate(messages: list[dict[str, Any]], schemas: list[dict[str, Any]]) -> int:
    """Cheap char→token estimate, INCLUDING the tool catalog (it is resent every turn and
    counts toward the prompt, so leaving it out would understate occupancy)."""
    total = sum(_msg_tokens(m) for m in messages)
    if schemas:
        total += _approx_tokens(json.dumps(schemas))
    return total


def _measure(messages: list[dict[str, Any]], schemas: list[dict[str, Any]],
             count_tokens_fn: CountTokensFn | None) -> tuple[int, bool]:
    """``(tokens, exact)``. Prefers the served model's own tokenizer (vLLM ``/tokenize``,
    the SAME one the server enforces its window with); falls back to the estimate on any
    failure or when no counter is wired. Never raises — counting must not break a turn."""
    if count_tokens_fn is not None:
        try:
            counted = count_tokens_fn(messages, schemas)
        except Exception:  # noqa: BLE001 - token counting is best-effort by contract
            counted = None
        if isinstance(counted, int) and not isinstance(counted, bool) and counted >= 0:
            return counted, True
    return _estimate(messages, schemas), False


def _transcript(messages: list[dict[str, str]]) -> str:
    lines = [f"{m.get('role', 'user')}: {str(m.get('content') or '')[:_SUMMARY_MSG_HEAD]}"
             for m in messages]
    text = "\n".join(lines)
    # Keep the NEWEST slice if the evicted batch is enormous — the older half is already
    # represented by the previous summary we are folding into.
    return text[-_SUMMARY_PROMPT_CHARS:] if len(text) > _SUMMARY_PROMPT_CHARS else text


def _write_summary(prior: str, evicted: list[dict[str, str]], cfg: ChatContextLimits,
                   summarize_fn: SummarizeFn | None) -> str:
    """The INCREMENTAL re-summarization: previous summary + only the newly-evicted turns.
    Returns "" for every failure mode (no summarizer, exception, empty/junk reply) so the
    caller degrades to dropping the oldest turns."""
    if summarize_fn is None or not evicted:
        return ""
    parts = []
    if prior:
        parts.append("PREVIOUS SUMMARY:\n" + prior)
    parts.append("NEW MESSAGES TO FOLD IN:\n" + _transcript(evicted))
    try:
        raw = summarize_fn(
            [{"role": "system", "content": _SUMMARY_SYSTEM},
             {"role": "user", "content": "\n\n".join(parts)}],
            cfg.summary_max_tokens,
        )
    except Exception:  # noqa: BLE001 - a failed summarizer degrades, never propagates
        return ""
    text = str(raw or "").strip()
    if len(text) < _MIN_SUMMARY_CHARS:
        return ""
    if len(text) > cfg.summary_max_chars:
        text = text[:cfg.summary_max_chars].rstrip() + "…"
    return text


# --- the public entry point ---------------------------------------------------


def build_chat_messages(
    *,
    system: str,
    question: str,
    history: list[dict[str, str]] | None = None,
    tool_schemas: list[dict[str, Any]] | None = None,
    config: ChatContextLimits | None = None,
    prior_summary: str = "",
    prior_summary_through: int = 0,
    count_tokens_fn: CountTokensFn | None = None,
    summarize_fn: SummarizeFn | None = None,
    on_event: EventFn | None = None,
) -> ChatContextReport:
    """Build the prompt for ONE chat turn, compacting the conversation if it overruns.

    ``prior_summary`` / ``prior_summary_through`` are the rolling memory returned by the
    PREVIOUS turn's report — the caller (the gateway, per conversation) hands them back so
    re-summarization only ever reads the newly-evicted turns. ``prior_summary_through``
    indexes into the CLEANED history (user/assistant messages with content, in order); an
    out-of-range value resets the memory rather than corrupting the window, which is what
    happens if a client edits or reorders its history.

    Emits ``context_measured`` and — only when something was actually folded away —
    ``context_trimmed``, using the SAME event vocabulary the research path emits so the
    gateway's existing renderers work unchanged.
    """
    cfg = config or ChatContextLimits()
    emit = on_event or (lambda _event: None)
    schemas = list(tool_schemas or [])
    allowed = cfg.allowed_prompt_tokens

    clean = _clean_history(history)
    total = len(clean)
    window_start = max(0, total - max(1, cfg.max_history_messages))
    through = prior_summary_through
    if not isinstance(through, int) or isinstance(through, bool) or through < 0 or through > total:
        through, prior_summary = 0, ""       # history was edited/reordered → rebuild memory
    summary = (prior_summary or "").strip() if through > 0 else ""
    start = max(window_start, through)
    dropped = max(0, window_start - through)  # fell off the outer cap, never summarized

    # Pass 1: carry everything we are allowed to carry and see whether it fits. The common
    # case (a short chat) ends here — one measurement, no summarizer call, nothing dropped.
    messages = _assemble(system, summary, clean[start:], question)
    used, exact = _measure(messages, schemas, count_tokens_fn)
    if used <= allowed:
        emit({"type": "context_measured", "exact_tokens": used, "allowed": allowed,
              "exact": exact, "compacted": False, "path": "chat"})
        return ChatContextReport(messages=messages, used_tokens=used, allowed_tokens=allowed,
                                 exact=exact, compacted=False, summary=summary,
                                 summary_through=start, dropped_messages=dropped)

    # Pass 2: over budget. Keep the last N exchanges verbatim; fold the rest into the memory.
    exchanges = _split_exchanges(clean[start:])
    keep = max(1, cfg.keep_last_exchanges)
    older = exchanges[:-keep] if len(exchanges) > keep else []
    verbatim = exchanges[-keep:] if len(exchanges) > keep else list(exchanges)
    evicted = [m for group in older for m in group]

    summarized = 0
    if evicted:
        written = _write_summary(summary, evicted, cfg, summarize_fn)
        if written:
            summary, summarized = written, len(evicted)
        else:
            dropped += len(evicted)          # degraded: the oldest turns simply fall off
        start += len(evicted)

    # Even the verbatim tail can overrun (one pasted 30K-char message). Shrink it by the
    # CHEAP estimate — choosing what to keep must not cost a round trip per candidate —
    # then verify once against the server tokenizer.
    def _fit(reserve: int) -> tuple[list[dict[str, Any]], list[list[dict[str, str]]]]:
        kept = list(verbatim)
        while True:
            candidate = _assemble(system, summary, [m for g in kept for m in g], question)
            if len(kept) <= 1 or _estimate(candidate, schemas) <= allowed - reserve:
                return candidate, kept
            kept = kept[1:]

    messages, kept = _fit(0)
    used, exact = _measure(messages, schemas, count_tokens_fn)
    if exact and used > allowed:
        # The estimate undershot the real tokenizer (dense JSON/CJK). Tighten ONCE against a
        # real reserve rather than looping round trips; mirrors the harness's retry reserve.
        tighter, tighter_kept = _fit(_OVERFLOW_RESERVE)
        if len(tighter_kept) < len(kept):
            messages, kept = tighter, tighter_kept
            used, exact = _measure(messages, schemas, count_tokens_fn)

    # Messages shed from the verbatim tail are NOT marked as summarized: they stay eligible
    # on the next turn, where they will have aged into the older half and get folded into
    # the memory properly instead of being lost.
    shed = sum(len(group) for group in verbatim[:len(verbatim) - len(kept)])
    dropped += shed

    emit({"type": "context_trimmed",
          "compressed_turns": len(older) if summarized else 0,
          "dropped_turns": (0 if summarized else len(older)) + (len(verbatim) - len(kept)),
          "approx_tokens": used, "path": "chat"})
    emit({"type": "context_measured", "exact_tokens": used, "allowed": allowed,
          "exact": exact, "compacted": True, "path": "chat"})
    return ChatContextReport(messages=messages, used_tokens=used, allowed_tokens=allowed,
                             exact=exact, compacted=True, summary=summary,
                             summary_through=start, summarized_messages=summarized,
                             dropped_messages=dropped)
