"""Native Anthropic transport for bring-your-own-key sessions (``POST /v1/messages``).

Every other provider in :mod:`llm_providers` speaks OpenAI's ``/chat/completions`` with a Bearer
token, so :mod:`vllm_client` serves them all. Anthropic's own API does not: it is ``/v1/messages``
with ``x-api-key``, a top-level ``system``, content BLOCKS instead of strings, ``tool_use`` /
``tool_result`` blocks instead of ``tool_calls`` / ``role: tool``, and thinking that is always on
for the current models. A user holding an Anthropic key (Jin, 2026-10-09) could only reach Claude
through OpenRouter; this module lets the key itself be used, through the official SDK.

It mirrors the two :mod:`vllm_client` contracts the gateway already consumes, so ``_lab_llm`` only
chooses which module to call:

* :func:`complete_ex` — ``(messages) -> (text, usage)`` for the PI / Critic / writer roles, with
  ``usage`` in the OpenAI keys ``_record_lab_usage`` reads and ``finish_reason == "length"`` on a
  ``max_tokens`` stop, so the truncation retry keeps working.
* :func:`chat_tools` — ``(messages, tools) -> {"content", "tool_calls", "finish_reason"}`` for the
  Scientist's tool loop. A session whose only endpoint is an Anthropic key has no GPU, so the tool
  loop has to run on Claude too or the first analysis step fails.
* :func:`chat_tools_stream` — the fast chat's streamed turn, same tuple protocol as
  :func:`vllm_client.chat_tools_stream`.

**Thinking blocks in the tool loop.** Claude's thinking blocks must go back unchanged in later
turns, and for accounts created on or after 2026-08-31 a block whose conversation prefix was
edited is a 400. The harness DOES edit: it compresses older turns and drops the oldest ones to
stay inside the window. So each response carries its raw content blocks under
``provider_blocks``; the harness echoes them on the assistant turn, this module replays every
thinking block it still has, and each request sets ``prefix_mismatch_behavior: "drop_block"`` —
after an edit the API drops the invalidated blocks instead of failing the turn. Blocks are never
stripped here: removing some of them can itself cause ordering / signature 400s.

The SDK is imported lazily: the gateway runs, and the offline tests pass, without it installed.
Tests replace :func:`_client` with a stub.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from typing import Any, Iterator
from urllib.parse import urlparse

from .errors import GatewayError

API_HOST = "api.anthropic.com"
DEFAULT_BASE_URL = f"https://{API_HOST}"
#: Suggested when a key is added and no model is chosen yet. The live list comes from GET /v1/models.
DEFAULT_MODEL = "claude-opus-5-5"

_TIMEOUT = 600.0
_VERIFY_TIMEOUT = 30.0

#: Betas, sent only on requests that use them.
_BETA_FALLBACK = "server-side-fallback-2026-07-01"
_BETA_BINDING = "thinking-binding-controls-2026-08-01"

#: Models that accept server-side ``fallbacks: "default"`` on the Claude API: when a safety
#: classifier declines, the API re-runs the request on a fallback model inside the same call.
_FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})

EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: Effort per lab role. Opus 5.5 defaults to "medium" when none is sent, so it is always sent.
#: Planning, critique and the write-up are where depth pays; classification is a one-word answer.
_ROLE_EFFORT = {"plan": "high", "critic": "high", "writer": "high", "audit": "medium",
                "classify": "low", "reason": "medium", "scientist": "medium", "chat": "low"}

_MODEL_RE = re.compile(r"^claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?:$|[-@])")


# --- endpoint + model capabilities --------------------------------------------------------------


def is_anthropic_endpoint(base_url: str | None) -> bool:
    """True when ``base_url`` is Anthropic's own API. The provider key is the primary signal (see
    :func:`llm_providers.transport_for`); the host is the fallback for rows saved without one."""
    host = (urlparse((base_url or "").strip()).hostname or "").lower()
    return host == API_HOST


def normalize_base_url(base_url: str | None) -> str:
    """The SDK wants the API ROOT (it appends ``/v1/messages`` itself), so a pasted
    ``…/v1`` or ``…/v1/messages`` would double up. Empty means the public API."""
    url = (base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
    for suffix in ("/v1/messages", "/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def _version(model: str) -> tuple[str, int, int] | None:
    m = _MODEL_RE.match((model or "").strip().lower())
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3) or 0)


def _modern(model: str) -> bool:
    """Adaptive thinking and ``output_config.effort`` exist from the 4.6 generation on (Haiku from
    5.5). Older ids — or an alias a proxy invented — get neither, rather than a 400."""
    v = _version(model)
    if v is None:
        return False
    family, major, minor = v
    if family == "haiku":
        return major >= 5
    return major >= 5 or (major == 4 and minor >= 6)


def _effort_for(model: str, effort: str | None) -> str | None:
    if not effort or not _modern(model):
        return None
    effort = effort.strip().lower()
    if effort not in EFFORTS:
        return None
    v = _version(model)
    if effort == "xhigh" and v and v[1] == 4 and v[2] < 7:   # xhigh arrived with Opus 4.7
        return "high"
    return effort


def role_effort(role: str | None) -> str:
    """``AISCIENTIST_ANTHROPIC_EFFORT_<ROLE>``, else ``AISCIENTIST_ANTHROPIC_EFFORT``, else the
    table above. Separate from the vLLM effort variables: those are tuned to what Qwen accepts."""
    key = (role or "reason").strip().lower()
    for env in (f"AISCIENTIST_ANTHROPIC_EFFORT_{key.upper()}", "AISCIENTIST_ANTHROPIC_EFFORT"):
        val = os.environ.get(env, "").strip().lower()
        if val in EFFORTS:
            return val
    return _ROLE_EFFORT.get(key, "medium")


def _int_env(name: str, default: int) -> int:
    try:
        return max(256, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def lab_max_tokens(role: str | None, base_cap: int) -> int:
    """Output ceiling for a lab role. Thinking cannot be switched off on the current models and it
    counts against ``max_tokens``, so the OpenAI-path caps (4096 / 16384, sized for the visible
    reply) would truncate a plan mid-thought. Four times that, at least 16000, at most
    ``AISCIENTIST_ANTHROPIC_MAX_TOKENS`` (64000). It is a ceiling, not a spend."""
    return min(_int_env("AISCIENTIST_ANTHROPIC_MAX_TOKENS", 64000), max(16000, 4 * base_cap))


def scientist_max_tokens() -> int:
    """Output reservation for one Scientist turn: thinking plus a tool call. The vLLM default
    (2048) is far too small once thinking cannot be disabled."""
    return _int_env("AISCIENTIST_ANTHROPIC_SCIENTIST_MAX_TOKENS", 32000)


def _fallbacks_on(model: str) -> bool:
    return (model or "").strip().lower() in _FALLBACK_MODELS and \
        os.environ.get("AISCIENTIST_ANTHROPIC_FALLBACKS", "1").strip().lower() not in ("0", "false", "no", "off")


# --- the SDK seam -------------------------------------------------------------------------------


def available() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _client(api_key: str, base_url: str | None, timeout: float):
    """One SDK client per call, with the key passed EXPLICITLY. The SDK would otherwise fall back
    to an ambient ``ANTHROPIC_API_KEY`` or a login profile on the gateway host — a different
    account than the user whose key this is."""
    try:
        import anthropic
    except ImportError as exc:
        raise GatewayError(
            "The Anthropic SDK is not installed on this server (pip install anthropic). "
            "An administrator needs to install it before Claude keys can be used.",
            stage="anthropic_endpoint") from exc
    if not api_key:
        raise GatewayError("No Anthropic API key was given.", stage="anthropic_auth")
    return anthropic.Anthropic(api_key=api_key, base_url=normalize_base_url(base_url),
                               timeout=timeout, max_retries=2)


# --- errors -------------------------------------------------------------------------------------


def _scrub(text: str, api_key: str | None) -> str:
    from .errors import scrub_secrets
    return scrub_secrets(text, api_key)


def classify_error(exc: BaseException) -> tuple[str, str]:
    """(cause, message) on the same causes the credential dialog already explains:
    auth | credit | rate | model | endpoint | network | context."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    body = str(getattr(exc, "message", "") or exc)
    low = body.lower()
    if name in ("APIConnectionError", "APITimeoutError") or (status is None and "connect" in low):
        return "network", "Could not reach the Anthropic API."
    if "prompt is too long" in low or "too many tokens" in low or name == "RequestTooLargeError":
        # Worded so research_harness._is_context_overflow recognises it and recompacts.
        return "context", "The prompt is longer than the maximum context length for this model."
    if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        return "auth", "Anthropic rejected this API key (unauthorized)."
    if "credit balance" in low or "billing" in low or status == 402:
        return "credit", "The key is valid but the Anthropic account has no credit left."
    if status == 404 or name == "NotFoundError" or ("model" in low and "not found" in low):
        return "model", "Anthropic does not serve that model id for this key."
    if status == 429 or name == "RateLimitError":
        # Not credit: the account has money, but this key sent more requests or tokens per minute
        # than its usage tier allows (several chats running at once reach it sooner).
        return "rate", "Anthropic is rate-limiting this key (requests or tokens per minute for its usage tier)."
    if status in (500, 502, 503, 529) or name in ("InternalServerError", "OverloadedError",
                                                   "ServiceUnavailableError"):
        return "endpoint", "The Anthropic API is overloaded or unavailable; try again shortly."
    if "data retention" in low:
        return "model", "This model needs 30-day data retention enabled on the Anthropic account."
    return "endpoint", f"The Anthropic API answered HTTP {status}." if status else "The Anthropic API call failed."


def _raise(exc: BaseException, api_key: str | None) -> None:
    cause, message = classify_error(exc)
    detail = _scrub(str(getattr(exc, "message", "") or exc), api_key)[:400]
    raise GatewayError(f"{message} ({detail})" if detail and detail not in message else message,
                       stage=f"anthropic_{cause}", detail=detail) from exc


def _param_rejected(exc: BaseException, *names: str) -> bool:
    """A 400 that names one of the optional parameters we sent — retried without them, so a model
    that predates them still answers instead of failing the run."""
    if getattr(exc, "status_code", None) != 400:
        return False
    low = str(getattr(exc, "message", "") or exc).lower()
    return any(n in low for n in names)


# --- OpenAI-shaped chat -> Messages API ----------------------------------------------------------


def _text(content: Any) -> str:
    """Flatten an OpenAI ``content`` (a string, or a list of parts) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(str(p.get("text") or p.get("content") or ""))
            else:
                parts.append(str(p))
        return "\n".join(x for x in parts if x)
    return str(content)


def fingerprint(content: str, tool_calls: list[dict] | None) -> str:
    """What the assistant turn said when it was produced. The harness compresses older turns in
    place; a changed fingerprint means the stored blocks no longer match what the turn says now."""
    calls = [((c or {}).get("id"), ((c or {}).get("function") or {}).get("name"),
              ((c or {}).get("function") or {}).get("arguments")) for c in (tool_calls or [])]
    # Whitespace-blind: a streamed turn is the concatenated deltas, the stored one the joined blocks.
    blob = json.dumps(["".join((content or "").split()), calls], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _tool_input(arguments: Any) -> dict:
    """A ``tool_use.input`` must be a JSON object. Compressed history cuts long arguments to a
    head plus "…" (no longer JSON); the model still sees what it was."""
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
    except ValueError:
        return {"_truncated_arguments": str(arguments)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _assistant_blocks(msg: dict) -> list[dict]:
    content = _text(msg.get("content"))
    calls = msg.get("tool_calls") or []
    stored = msg.get("provider_blocks") if isinstance(msg.get("provider_blocks"), dict) else None
    raw = [b for b in (stored or {}).get("blocks") or [] if isinstance(b, dict)]
    if stored and stored.get("fp") == fingerprint(content, calls) and raw:
        blocks = [dict(b) for b in raw]               # unchanged since it was produced: verbatim
    else:
        # Edited (compressed) or never stored: keep every thinking block it had, ahead of the turn's
        # CURRENT text and calls. drop_block lets the API discard any that no longer validate.
        blocks = [dict(b) for b in raw if b.get("type") in ("thinking", "redacted_thinking")]
        if content.strip():
            blocks.append({"type": "text", "text": content})
        for c in calls:
            fn = (c or {}).get("function") or {}
            blocks.append({"type": "tool_use", "id": str((c or {}).get("id") or ""),
                           "name": str(fn.get("name") or ""), "input": _tool_input(fn.get("arguments"))})
    # A turn that was ONLY thinking (cut off at max_tokens before acting) says nothing the model
    # can continue from, and an assistant turn of bare thinking is not valid input: leave it out.
    if not any(b.get("type") not in ("thinking", "redacted_thinking") for b in blocks):
        return []
    return blocks


def _append(out: list[dict], role: str, blocks: list[dict]) -> None:
    if not blocks:
        return
    if out and out[-1]["role"] == role:
        out[-1]["content"].extend(blocks)
    else:
        out.append({"role": role, "content": list(blocks)})


def _pair_tools(out: list[dict]) -> list[dict]:
    """Enforce the Messages API pairing rules the OpenAI history does not guarantee once the
    harness has dropped or compressed turns:

    * every ``tool_result`` answers a ``tool_use`` in the assistant turn right before it —
      an orphan becomes plain text, so the content survives;
    * every ``tool_use`` gets a ``tool_result`` in the next user turn — a missing one (the harness
      stops mid-list on ``finish`` or cancel, or the result was dropped) gets a stub;
    * ``tool_result`` blocks come first in their user turn.
    """
    fixed: list[dict] = []
    for i, msg in enumerate(out):
        if msg["role"] != "user":
            fixed.append(msg)
            continue
        prev = fixed[-1] if fixed and fixed[-1]["role"] == "assistant" else None
        ids = [b.get("id") for b in (prev or {}).get("content", []) if b.get("type") == "tool_use"]
        results, rest = [], []
        for b in msg["content"]:
            if b.get("type") == "tool_result":
                if b.get("tool_use_id") in ids:
                    results.append(b)
                else:
                    rest.append({"type": "text", "text": f"Tool result: {b.get('content') or ''}"})
            else:
                rest.append(b)
        answered = {b.get("tool_use_id") for b in results}
        for tid in ids:
            if tid not in answered:
                results.append({"type": "tool_result", "tool_use_id": tid,
                                "content": "(no result: this call was not run)"})
        fixed.append({"role": "user", "content": results + rest})
    # An assistant turn whose tool_use has NO following user turn at all (history ends on it).
    if fixed and fixed[-1]["role"] == "assistant":
        ids = [b.get("id") for b in fixed[-1]["content"] if b.get("type") == "tool_use"]
        stubs = [{"type": "tool_result", "tool_use_id": t, "content": "(no result: this call was not run)"}
                 for t in ids]
        fixed.append({"role": "user", "content": stubs or [{"type": "text", "text": "Continue."}]})
    return fixed


def to_anthropic(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """OpenAI chat messages -> (system blocks, Messages-API messages).

    Leading ``system`` messages become the top-level system prompt; a ``system`` message later in
    the history becomes a bracketed note in the user turn it sits in, which keeps the top-level
    prompt stable (a changed system prompt invalidates every later thinking block)."""
    system: list[dict] = []
    out: list[dict] = []
    for msg in messages or []:
        role = msg.get("role")
        if role == "system":
            text = _text(msg.get("content")).strip()
            if not text:
                continue
            if not out:
                system.append({"type": "text", "text": text})
            else:
                _append(out, "user", [{"type": "text", "text": f"[System note] {text}"}])
        elif role == "assistant":
            _append(out, "assistant", _assistant_blocks(msg))
        elif role == "tool":
            _append(out, "user", [{"type": "tool_result", "tool_use_id": str(msg.get("tool_call_id") or ""),
                                   "content": _text(msg.get("content")) or "(empty)"}])
        else:
            text = _text(msg.get("content"))
            _append(out, "user", [{"type": "text", "text": text if text.strip() else "(empty)"}])
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continuing)"}]})
    out = _pair_tools(out)
    if not out:
        out = [{"role": "user", "content": [{"type": "text", "text": "(empty)"}]}]
    return system, out


def tools_to_anthropic(tools: list[dict] | None) -> list[dict]:
    """OpenAI function tools -> Anthropic tool definitions (``parameters`` -> ``input_schema``)."""
    out = []
    for t in tools or []:
        fn = (t or {}).get("function") if (t or {}).get("type") == "function" else t
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        schema = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
        schema = dict(schema) or {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        out.append({"name": str(fn["name"]), "description": str(fn.get("description") or ""),
                    "input_schema": schema})
    return out


def _blocks_of(message: Any) -> list[dict]:
    blocks = []
    for b in getattr(message, "content", None) or []:
        if isinstance(b, dict):
            blocks.append(dict(b))
        elif hasattr(b, "model_dump"):
            blocks.append(b.model_dump(mode="json", exclude_none=True))
    return blocks


def _replayable(blocks: list[dict]) -> tuple[list[dict], list[dict]]:
    """(blocks to echo back, blocks that count as this turn's output).

    After a mid-output server-side fallback, the declined model's thinking and ``tool_use`` blocks
    before the last ``fallback`` marker must not be echoed or acted on; its text is kept."""
    last = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    if last < 0:
        return blocks, blocks
    kept = [b for i, b in enumerate(blocks) if i > last or (i < last and b.get("type") == "text")]
    return kept, kept


def _usage(message: Any, model: str) -> dict:
    u = getattr(message, "usage", None)
    get = (lambda k: getattr(u, k, None) or 0) if u is not None else (lambda k: 0)
    cache_read, cache_write = int(get("cache_read_input_tokens")), int(get("cache_creation_input_tokens"))
    # The cache writes split by lifetime (they are billed at 1.25x and 2x input); requests here only
    # write 5-minute entries, but a proxy or a server tool may add 1-hour ones.
    by_ttl = get("cache_creation") if u is not None else None
    write_1h = int(getattr(by_ttl, "ephemeral_1h_input_tokens", 0) or 0) if by_ttl else 0
    prompt = int(get("input_tokens")) + cache_read + cache_write
    stop = str(getattr(message, "stop_reason", "") or "")
    return {"prompt_tokens": prompt, "completion_tokens": int(get("output_tokens")),
            "total_tokens": prompt + int(get("output_tokens")),
            "input_tokens": int(get("input_tokens")),
            "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write,
            "cache_creation_1h_input_tokens": write_1h,
            "finish_reason": {"max_tokens": "length", "tool_use": "tool_calls",
                              "end_turn": "stop", "stop_sequence": "stop"}.get(stop, stop),
            "stop_reason": stop, "model": str(getattr(message, "model", "") or model)}


def _check_refusal(message: Any) -> None:
    if str(getattr(message, "stop_reason", "") or "") != "refusal":
        return
    details = getattr(message, "stop_details", None)
    category = getattr(details, "category", None) if details is not None else None
    explanation = getattr(details, "explanation", None) if details is not None else None
    why = f" (category: {category})" if category else ""
    raise GatewayError(f"Claude declined this request{why}. {explanation or ''}".strip(),
                       stage="anthropic_refusal", detail={"category": category, "explanation": explanation})


# --- calls --------------------------------------------------------------------------------------


def _prepare(*, model: str, system: list[dict], messages: list[dict], max_tokens: int,
             effort: str | None, tools: list[dict] | None, binding: bool,
             cache: str = "auto") -> tuple[dict, list[str]]:
    """The request body and the betas it needs.

    ``cache``: ``"auto"`` caches the whole prompt (a conversation re-reads it on its next turn);
    ``"system"`` caches only the system prompt, for many one-off calls that share their
    instructions and differ in the rest (the corpus scan reads 1,500 different passages under one
    set of rules: auto-caching wrote every prompt to cache at 1.25x and never read one back)."""
    eff = _effort_for(model, effort)
    betas: list[str] = []
    kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if cache == "auto":
        # Auto-caching: the system prompt, the tool catalogue and the history so far are re-read
        # from cache on the next turn of the loop.
        kwargs["cache_control"] = {"type": "ephemeral"}
    if system and cache == "system":
        system = [*system[:-1], {**system[-1], "cache_control": {"type": "ephemeral"}}]
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
    if eff:
        kwargs["output_config"] = {"effort": eff}
    if binding and _modern(model):
        kwargs["thinking"] = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        betas.append(_BETA_BINDING)
    if _fallbacks_on(model):
        kwargs["fallbacks"] = "default"
        betas.append(_BETA_FALLBACK)
    return kwargs, betas


def _without_rejected(exc: BaseException, kwargs: dict, betas: list[str]) -> tuple[dict, list[str]] | None:
    """The same request minus the optional parameter a 400 named, or None when the 400 is about
    something else. Lets a model that predates a parameter still answer instead of failing the run."""
    kw, bs = dict(kwargs), list(betas)
    if "output_config" in kw and _param_rejected(exc, "effort", "output_config"):
        kw.pop("output_config")
    elif "thinking" in kw and _param_rejected(exc, "thinking", "block_binding"):
        kw.pop("thinking")
        bs = [b for b in bs if b != _BETA_BINDING]
    elif "fallbacks" in kw and _param_rejected(exc, "fallback"):
        kw.pop("fallbacks")
        bs = [b for b in bs if b != _BETA_FALLBACK]
    elif "cache_control" in kw and _param_rejected(exc, "cache_control"):
        kw.pop("cache_control")
    else:
        return None
    return kw, bs


def _enter(stack: contextlib.ExitStack, client, kwargs: dict, betas: list[str]):
    if betas:
        return stack.enter_context(client.beta.messages.stream(betas=betas, **kwargs))
    return stack.enter_context(client.messages.stream(**{k: v for k, v in kwargs.items() if k != "fallbacks"}))


def _open_stream(stack: contextlib.ExitStack, client, kwargs: dict, betas: list[str], api_key: str):
    """Open the stream (the request is sent here, so a 400 surfaces here, before any output),
    retrying once without a parameter the model rejected. Large ``max_tokens`` must stream."""
    try:
        return _enter(stack, client, kwargs, betas)
    except Exception as exc:  # noqa: BLE001 - classified below
        variant = _without_rejected(exc, kwargs, betas)
        if variant is None:
            _raise(exc, api_key)
        try:
            return _enter(stack, client, *variant)
        except Exception as exc2:  # noqa: BLE001
            _raise(exc2, api_key)


def _final(client, kwargs: dict, betas: list[str], api_key: str):
    with contextlib.ExitStack() as stack:
        stream = _open_stream(stack, client, kwargs, betas, api_key)
        try:
            return stream.get_final_message()
        except Exception as exc:  # noqa: BLE001 - a mid-stream failure
            _raise(exc, api_key)


def _turn(final: Any, model: str) -> dict:
    """A final message -> the :func:`vllm_client.chat_tools` shape plus ``usage`` and
    ``provider_blocks``."""
    echo, out = _replayable(_blocks_of(final))
    content = "\n".join(b.get("text") or "" for b in out if b.get("type") == "text").strip()
    calls = [{"id": str(b.get("id") or ""), "type": "function",
              "function": {"name": str(b.get("name") or ""),
                           "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
             for b in out if b.get("type") == "tool_use"]
    usage = _usage(final, model)
    return {"content": content, "tool_calls": calls, "finish_reason": usage["finish_reason"],
            "usage": usage,
            "provider_blocks": {"provider": "anthropic", "model": usage["model"],
                                "fp": fingerprint(content, calls), "blocks": echo}}


def complete_ex(model: str, messages: list[dict], *, api_key: str, base_url: str | None = None,
                max_tokens: int = 16000, effort: str | None = None,
                timeout: float = _TIMEOUT, cache: str = "auto") -> tuple[str, dict]:
    """A plain completion for a lab role: ``(text, usage)`` like :func:`vllm_client.complete_ex`.
    These are fresh conversations with no tools, so no thinking is replayed. ``cache``: see
    :func:`_prepare`."""
    client = _client(api_key, base_url, timeout)
    system, msgs = to_anthropic(messages)
    kwargs, betas = _prepare(model=model, system=system, messages=msgs, max_tokens=max_tokens,
                             effort=effort, tools=None, binding=False, cache=cache)
    final = _final(client, kwargs, betas, api_key)
    _check_refusal(final)
    _, blocks = _replayable(_blocks_of(final))
    text = "\n".join(b.get("text") or "" for b in blocks if b.get("type") == "text").strip()
    return text, _usage(final, model)


def chat_tools(model: str, messages: list[dict], tools: list[dict], *, api_key: str,
               base_url: str | None = None, max_tokens: int | None = None,
               effort: str | None = None, timeout: float = _TIMEOUT) -> dict:
    """One Scientist turn: the :func:`vllm_client.chat_tools` shape, plus ``provider_blocks`` (the
    raw content blocks, for the next turn's replay) and ``usage``."""
    client = _client(api_key, base_url, timeout)
    system, msgs = to_anthropic(messages)
    kwargs, betas = _prepare(model=model, system=system, messages=msgs,
                             max_tokens=max_tokens or scientist_max_tokens(), effort=effort,
                             tools=tools_to_anthropic(tools), binding=True)
    final = _final(client, kwargs, betas, api_key)
    _check_refusal(final)
    return _turn(final, model)


def chat_tools_stream(model: str, messages: list[dict], tools: list[dict], *, api_key: str,
                      base_url: str | None = None, max_tokens: int = 16000,
                      effort: str | None = None, timeout: float = _TIMEOUT) -> Iterator[tuple[str, Any]]:
    """The :func:`vllm_client.chat_tools_stream` protocol for the fast chat: ``("content", text)``
    (and ``("thinking", text)`` when a summary is returned) as they arrive, then one
    ``("tool_calls", calls)``, one ``("provider_blocks", blocks)`` and one ``("usage", usage)``."""
    client = _client(api_key, base_url, timeout)
    system, msgs = to_anthropic(messages)
    kwargs, betas = _prepare(model=model, system=system, messages=msgs, max_tokens=max_tokens,
                             effort=effort, tools=tools_to_anthropic(tools), binding=True)
    with contextlib.ExitStack() as stack:
        stream = _open_stream(stack, client, kwargs, betas, api_key)
        try:
            for event in stream:
                if getattr(event, "type", "") != "content_block_delta":
                    continue
                delta = getattr(event, "delta", None)
                kind = getattr(delta, "type", "")
                if kind == "text_delta" and getattr(delta, "text", ""):
                    yield "content", delta.text
                elif kind == "thinking_delta" and getattr(delta, "thinking", ""):
                    yield "thinking", delta.thinking
            final = stream.get_final_message()
        except GatewayError:
            raise
        except Exception as exc:  # noqa: BLE001 - a mid-stream failure
            _raise(exc, api_key)
    _check_refusal(final)
    turn = _turn(final, model)
    yield "tool_calls", turn["tool_calls"]
    yield "provider_blocks", turn["provider_blocks"]
    if turn.get("usage"):
        yield "usage", turn["usage"]


def count_tokens(model: str, messages: list[dict], tools: list[dict] | None, *, api_key: str,
                 base_url: str | None = None) -> int | None:
    """Exact input token count, or None (the harness then estimates from characters)."""
    try:
        client = _client(api_key, base_url, _VERIFY_TIMEOUT)
        system, msgs = to_anthropic(messages)
        kw: dict[str, Any] = {"model": model, "messages": msgs}
        if system:
            kw["system"] = system
        if tools:
            kw["tools"] = tools_to_anthropic(tools)
        return int(client.messages.count_tokens(**kw).input_tokens)
    except Exception:  # noqa: BLE001 - best effort by contract
        return None


def probe_models(api_key: str, base_url: str | None = None,
                 timeout: float = _VERIFY_TIMEOUT) -> tuple[bool, str, str, list[str]]:
    """``GET /v1/models`` with the cause attached: (ok, cause, message, model ids)."""
    if not api_key:
        return False, "auth", "Listing models needs the API key.", []
    try:
        client = _client(api_key, base_url, timeout)
        ids = [str(m.id) for m in client.models.list(limit=100)]
    except GatewayError as exc:
        return False, (exc.stage or "anthropic_endpoint").removeprefix("anthropic_"), exc.message, []
    except Exception as exc:  # noqa: BLE001
        cause, message = classify_error(exc)
        return False, cause, message, []
    if not ids:
        return False, "unsupported", "The Anthropic API listed no models for this key.", []
    return True, "ok", f"{len(ids)} models available.", ids


def verify(api_key: str, model: str | None, base_url: str | None = None,
           timeout: float = _VERIFY_TIMEOUT) -> tuple[bool, str, str, list[str], str | None]:
    """Prove the key works for ``model``: (ok, cause, message, models, verified_model).

    The model list proves the key authenticates; a one-word message proves the account can be
    billed and the model answers — the same call path a run uses. Thinking is left at the model's
    default (sending ``disabled`` is a 400 on the current models) and effort at low."""
    ok, cause, message, models = probe_models(api_key, base_url, timeout)
    if not ok and cause != "unsupported":
        return False, cause, message, models, None
    if not model:
        if models:
            return True, "ok", f"Key accepted; {len(models)} models available.", models, None
        return False, "model", "Choose a model id so the key can be tested.", models, None
    try:
        client = _client(api_key, base_url, timeout)
        kw: dict[str, Any] = {"model": model, "max_tokens": 64,
                              "messages": [{"role": "user", "content": "Reply with the word: ok"}]}
        if _effort_for(model, "low"):
            kw["output_config"] = {"effort": "low"}
        try:
            client.messages.create(**kw)
        except Exception as exc:  # noqa: BLE001
            if not _param_rejected(exc, "effort", "output_config"):
                raise
            kw.pop("output_config", None)
            client.messages.create(**kw)
    except GatewayError as exc:
        return False, (exc.stage or "anthropic_endpoint").removeprefix("anthropic_"), exc.message, models, None
    except Exception as exc:  # noqa: BLE001
        cause, message = classify_error(exc)
        detail = _scrub(str(getattr(exc, "message", "") or exc), api_key)[:300].strip()
        return False, cause, f"{message} ({detail})" if detail else message, models, None
    return True, "ok", f"{model} answered.", models, model
