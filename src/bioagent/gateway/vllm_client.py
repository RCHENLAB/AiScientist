"""OpenAI-compatible ``/v1`` client for the tunneled vLLM (or SGLang) server.

vLLM and SGLang both expose an OpenAI-compatible API at ``/v1`` — so this single
client serves either backend; only the *provisioning* (how the serve job is
launched) differs per backend (see ``gpu.py``). This mirrors the public surface
of ``ollama.py`` (``get_models``/``ensure_model``/``warmup``/``chat_stream``/
``chat_tools``) so ``app.py`` can dispatch on ``settings.llm_backend`` and the
agentic harness's ``chat_tools`` contract is unchanged.

Endpoint differences vs Ollama's native API:
  - model list:  GET ``/v1/models``           (vs ``/api/tags``)
  - chat:        POST ``/v1/chat/completions`` (vs ``/api/chat``); SSE ``data:``
                 chunks (vs NDJSON). Qwen3 "thinking" arrives as
                 ``delta.reasoning_content`` when the server is launched with
                 ``--reasoning-parser qwen3``.
  - no model pull: vLLM/SGLang load the model at serve-launch, so ``ensure_model``
                 only *verifies* the model is being served (no download step).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterator

from .errors import GatewayError, VLLMNetworkError, error_detail
from .settings import HPCSettings


# --- Served-model dialect knobs (env-gated; empty = today's Qwen behaviour, byte-for-byte) ----------
# The Qwen3 chat template thinks by default and is switched OFF with
# ``chat_template_kwargs={"enable_thinking": False}`` (the only kwarg this module ever sent).
# Other served models invert that: vLLM's ``--tokenizer-mode deepseek_v4`` runs DeepSeek-V4 in
# "chat" (no-reasoning) mode unless the request carries ``{"enable_thinking": true}``, and picks
# the effort from ``reasoning_effort``. So a role that WANTS thinking must be able to say so
# explicitly, and the Scientist's bounded tool turns must be able to carry a larger budget when
# the served model reasons before it calls a tool. Both are deployment facts about the served
# model, hence env vars next to BIOAGENT_VLLM_MODEL, not code constants.
def _think_on_kwargs() -> dict | None:
    """``chat_template_kwargs`` to send on think=True calls (PI / Critic / writer), from
    ``BIOAGENT_VLLM_THINK_ON_KWARGS`` (JSON object). None = send nothing (Qwen default: thinks)."""
    import os
    raw = os.environ.get("BIOAGENT_VLLM_THINK_ON_KWARGS", "").strip()
    if not raw:
        return None
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) and val else None
    except json.JSONDecodeError:
        return None


def _scientist_kwargs() -> dict | None:
    """``chat_template_kwargs`` for the Scientist's tool turns (``chat_tools``), from
    ``BIOAGENT_SCIENTIST_CHAT_TEMPLATE_KWARGS`` (JSON object). None = send nothing (today)."""
    import os
    raw = os.environ.get("BIOAGENT_SCIENTIST_CHAT_TEMPLATE_KWARGS", "").strip()
    if not raw:
        return None
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) and val else None
    except json.JSONDecodeError:
        return None


def scientist_max_tokens(default: int = 2048) -> int:
    """Output reservation for one Scientist tool turn: ``BIOAGENT_SCIENTIST_MAX_TOKENS`` or the
    historical 2048 (enough for a tool call; too small for a model that reasons first)."""
    import os
    try:
        return int(os.environ.get("BIOAGENT_SCIENTIST_MAX_TOKENS", "") or default)
    except ValueError:
        return default


def _lab_remote_reasoning() -> dict | None:
    """Optional ``reasoning`` payload for REMOTE (base_url) completions — OpenRouter-style
    ``{"effort": "low"}`` — from ``BIOAGENT_LAB_LLM_REASONING`` (JSON object). A reasoning model
    left at its provider-default effort can think for many minutes per PI/Critic/writer call
    (measured: one DSV4-Flash plan call = 858 s at default vs ~60 s at effort=low, with BETTER
    judged output), so deployments that route lab roles to such a model should set this."""
    import os
    raw = os.environ.get("BIOAGENT_LAB_LLM_REASONING", "").strip()
    if not raw:
        return None
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) and val else None
    except json.JSONDecodeError:
        return None


# What a served model may accept as ``reasoning_effort``. Models disagree: Qwen3.8 takes ONLY
# low / medium / xhigh (xhigh is its default) and answers "high" with HTTP 400, so an effort the
# model rejects is retried without the field (see complete_ex) rather than failing the run.
REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "none", "minimal"})


def _reasoning_effort() -> str | None:
    """Reasoning strength for LOCAL (tunnelled vLLM/SGLang) completions, from
    ``BIOAGENT_VLLM_REASONING_EFFORT`` (``low``/``medium``/``high``).

    ``_think_on_kwargs`` only turns the thinking trace ON or OFF via the chat template; it cannot
    say HOW HARD to think. Models served with a reasoning parser (DeepSeek-V4 under vLLM's
    ``deepseek_v4`` tokenizer mode) read the strength from the top-level ``reasoning_effort``
    field instead, which this module never sent — so the local path had no effort control at all.
    Unset = send nothing, i.e. today's behaviour byte-for-byte, because a server whose model has
    no reasoning parser can reject an unknown field.
    """
    import os
    raw = os.environ.get("BIOAGENT_VLLM_REASONING_EFFORT", "").strip().lower()
    return raw if raw in REASONING_EFFORTS else None


#: Output ceiling for one REMOTE lab-role completion, per role. Two tiers because the roles are
#: not the same shape: planning / critique / scheduling / classification emit a short verdict or a
#: JSON object, while the synthesis node writes the whole manuscript.
_LAB_MAX_TOKENS_DEFAULTS = {"reason": 4096, "writer": 16384}


def lab_max_tokens(role: str | None = None) -> int:
    """Output cap for one lab-role call on a METERED endpoint, by role.

    On the cluster's own GPU an output budget is a CONTEXT-FITTING number — ``max_model_len``
    caps prompt+output together, so "let the reply use whatever the window leaves" is both correct
    and free. Pointed at a per-token API that same number stops being a fitting constraint and
    becomes a SPENDING limit, and the lab's computed ``max_model_len - prompt - margin`` (≈240k on
    a 262144 window) is no limit at all. Worse, the injected ``complete_fn`` dropped it entirely,
    so the request carried no ``max_tokens`` key and the endpoint was free to reason without
    bound. This is the remote-only ceiling, per role, env-tunable:
    ``BIOAGENT_LAB_MAX_TOKENS`` / ``BIOAGENT_LAB_MAX_TOKENS_WRITER``.
    """
    import os
    key = "writer" if (role or "").strip().lower() == "writer" else "reason"
    env = "BIOAGENT_LAB_MAX_TOKENS_WRITER" if key == "writer" else "BIOAGENT_LAB_MAX_TOKENS"
    try:
        val = int(os.environ.get(env, "") or _LAB_MAX_TOKENS_DEFAULTS[key])
    except ValueError:
        val = _LAB_MAX_TOKENS_DEFAULTS[key]
    return max(256, val)


def _base(local_port: int) -> str:
    return f"http://127.0.0.1:{local_port}/v1"


def _endpoint(local_port: int, base_url: str | None) -> str:
    """The /v1 base — the session's tunnel by default, or an explicit ``base_url``
    (e.g. OpenRouter ``https://openrouter.ai/api/v1`` for off-cluster testing)."""
    return base_url.rstrip("/") if base_url else _base(local_port)


def _headers(api_key: str | None) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _model_matches(served: str, wanted: str) -> bool:
    """vLLM serves under the HF repo id (or ``--served-model-name``). Match either
    the full id or its last path segment, so ``Qwen/Qwen3.6-...`` matches a config
    that names just ``Qwen3.6-...``."""
    served, wanted = served.strip(), wanted.strip()
    return served == wanted or served.rsplit("/", 1)[-1] == wanted.rsplit("/", 1)[-1]


def get_models(local_port: int, timeout: float = 10.0) -> list[str]:
    """Return the model ids the server reports (OpenAI ``GET /v1/models``)."""
    url = f"{_base(local_port)}/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - local tunnel
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as exc:
        raise GatewayError(
            "Could not reach the vLLM /v1 endpoint through the tunnel.",
            stage="vllm_http",
            detail=error_detail(exc),
        ) from exc
    return [m.get("id", "") for m in data.get("data", [])]


# Interface alias so callers can use the same name as ollama.get_tags.
get_tags = get_models


def ensure_installed(executor, settings: HPCSettings, emit) -> dict:
    """vLLM runs from a prebuilt Singularity image — there is NO per-user binary to
    install (unlike Ollama). Just verify the ``.sif`` is present so a missing image
    is surfaced at provisioning, not mid-serve. Returns an info dict for the UI."""
    image = settings.vllm_image
    result = executor.exec(f"test -f {image} && echo __SIF_OK__")
    if "__SIF_OK__" in (result.stdout or ""):
        emit("success", "vllm_image", f"vLLM container image present: {image}")
        return {"installed": True, "engine": "vllm", "image": image, "version": None}
    raise GatewayError(
        f"vLLM container image not found at {image}. Pull it once on the cluster with: "
        f"singularity pull {image} docker://vllm/vllm-openai:latest",
        stage="vllm_image",
        detail={"image": image, "stderr": result.stderr},
    )


def ensure_model(local_port: int, settings: HPCSettings, emit) -> None:
    """Verify the served model is up. Unlike Ollama there is NO pull: vLLM/SGLang
    load the model when the serve job launches, so a missing model here means the
    serve command's ``--model``/``--model-path`` is wrong — surfaced, never faked."""
    model = settings.serving_model()
    served = get_models(local_port)
    if any(_model_matches(s, model) for s in served):
        emit("success", "vllm_model", f"Model '{model}' is being served.")
        return
    raise GatewayError(
        f"The vLLM server is up but is not serving '{model}'. It reports: {served or '[]'}. "
        "Check the serve job's --model / --served-model-name.",
        stage="vllm_model",
        detail={"requested": model, "served": served},
    )


def warmup(local_port: int, model: str, emit, timeout: float = 900.0) -> None:
    """Confirm the model answers (it is already loaded at serve-launch). A tiny
    non-streaming completion; non-fatal on failure (matches ollama.warmup)."""
    emit("step", "warmup", f"Pinging '{model}' to confirm it is loaded and responsive ...")
    url = f"{_base(local_port)}/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 1,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local tunnel
            resp.read()
    except (urllib.error.URLError, OSError) as exc:
        emit("warning", "warmup", f"Warmup ping failed ({exc}); the first message will confirm load instead.")
        return
    emit("success", "warmup", f"'{model}' is loaded and responsive.")


def chat_stream(
    local_port: int,
    model: str,
    messages: list[dict],
    timeout: float = 600.0,
    think: bool = True,
) -> Iterator[tuple[str, str]]:
    """Stream ``(kind, chunk)`` pairs from ``/v1/chat/completions`` (SSE).

    ``kind`` is ``"thinking"`` for the Qwen3 reasoning trace (server launched with
    ``--reasoning-parser qwen3`` surfaces it as ``delta.reasoning_content``) or
    ``"content"`` for the answer. Same contract as ``ollama.chat_stream``.
    """
    url = f"{_base(local_port)}/chat/completions"
    payload: dict = {"model": model, "messages": messages, "stream": True}
    # Qwen3 hybrid reasoning: toggle the thinking trace via the chat template.
    payload["chat_template_kwargs"] = {"enable_thinking": bool(think)}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local tunnel
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if chunk == "[DONE]":
                    break
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("error"):
                    raise GatewayError(f"vLLM chat error: {event['error']}", stage="vllm_chat", detail=event)
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                thinking = delta.get("reasoning_content") or ""
                if thinking:
                    yield ("thinking", thinking)
                content = delta.get("content") or ""
                if content:
                    yield ("content", content)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GatewayError(f"vLLM chat error: {detail[:300]}", stage="vllm_chat", detail=detail) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise VLLMNetworkError("Network error during vLLM chat.", stage="vllm_chat", detail=error_detail(exc)) from exc


def _accumulate_tool_deltas(acc: dict[int, dict], deltas: list[dict]) -> None:
    """Fold one SSE chunk's ``delta.tool_calls`` fragments into ``acc`` (keyed by the
    OpenAI ``index``). A streamed tool call arrives in pieces: the first chunk usually
    carries ``id`` + ``function.name``, and ``function.arguments`` dribbles in as JSON
    string fragments across later chunks — so the name is SET once and the arguments are
    CONCATENATED. Pure (no I/O), so the streaming assembly is unit-testable offline."""
    for d in deltas or []:
        if not isinstance(d, dict):
            continue
        idx = d.get("index")
        idx = int(idx) if isinstance(idx, int) else len(acc)
        slot = acc.setdefault(idx, {"id": None, "type": "function",
                                    "function": {"name": "", "arguments": ""}})
        if d.get("id"):
            slot["id"] = d["id"]
        fn = d.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]


def chat_tools_stream(
    local_port: int,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    timeout: float = 600.0,
    think: bool = False,
    base_url: str | None = None,
    api_key: str | None = None,
    max_tokens: int | None = 2048,
) -> Iterator[tuple[str, Any]]:
    """Stream ONE ``/v1/chat/completions`` turn that may ALSO call tools.

    This is ``chat_stream`` (token-by-token SSE) and ``chat_tools`` (function schemas)
    combined — the primitive the answer-first ReAct fast path needs, which neither
    existing function provides: ``chat_stream`` can't call tools, and ``chat_tools`` is
    non-streaming, so its first token only lands after the whole turn is generated.

    Yields, in arrival order:
      * ``("thinking", chunk)`` — reasoning trace, only when ``think=True``
      * ``("content", chunk)``  — answer text, as it is generated
      * ``("tool_calls", [call, ...])`` — ONCE at the end of the turn, iff the model
        asked for tools. Each call is the assembled OpenAI shape
        ``{"id", "type", "function": {"name", "arguments"}}`` with ``arguments`` as a
        JSON *string* — the same shape ``chat_tools`` returns, so callers parse it
        identically.

    ``think`` defaults to **False** here (the opposite of ``chat_stream``): the whole
    point of the fast path is that the first sentence appears immediately, and a Qwen3
    reasoning trace front-loads seconds of tokens the user cannot read yet. Turn it on
    only where the trace is wanted.
    """
    url = f"{_endpoint(local_port, base_url)}/chat/completions"
    payload: dict = {"model": model, "messages": messages, "stream": True}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    payload["chat_template_kwargs"] = {"enable_thinking": bool(think)}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(api_key), method="POST")
    calls: dict[int, dict] = {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local tunnel
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if chunk == "[DONE]":
                    break
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("error"):
                    raise GatewayError(f"vLLM chat error: {event['error']}", stage="vllm_chat", detail=event)
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                thinking = delta.get("reasoning_content") or ""
                if thinking:
                    yield ("thinking", thinking)
                content = delta.get("content") or ""
                if content:
                    yield ("content", content)
                if delta.get("tool_calls"):
                    _accumulate_tool_deltas(calls, delta["tool_calls"])
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GatewayError(f"vLLM tool-stream error: {detail[:300]}", stage="vllm_chat", detail=detail) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise VLLMNetworkError("Network error during vLLM tool-stream.",
                               stage="vllm_chat", detail=error_detail(exc)) from exc
    if calls:
        yield ("tool_calls", [calls[i] for i in sorted(calls)])


def complete_ex(
    local_port: int,
    model: str,
    messages: list[dict],
    timeout: float = 600.0,
    max_tokens: int | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    think: bool = True,
    reasoning_effort: str | None = None,
    idle_timeout: float | None = None,
) -> tuple[str, dict]:
    """One ``/v1/chat/completions`` — the assistant text AND the provider's usage.

    ``reasoning_effort`` overrides ``BIOAGENT_VLLM_REASONING_EFFORT`` for this call (the gateway sets
    it PER ROLE: planning and the write-up think harder than a routing classifier).

    ``idle_timeout`` (local path only) switches to STREAMING with no limit on total time: the call
    fails only when the server sends NOTHING for that many seconds. A reasoning model streams its
    thinking too, so an hour of thinking never trips it, while a dead node or dropped tunnel still
    does. Without it the call is the historical non-streaming request with a total ``timeout`` —
    which is what cut a Qwen3.8 plan off at 600 s mid-thought.

    ``complete`` (below) is the text-only form every caller used before; this is the same request
    with the ``usage`` block kept instead of discarded. The provider returns exactly what the call
    cost — prompt/completion/total tokens, and on OpenRouter a ``cost`` in USD — and dropping it
    was why a metered endpoint could only be measured from the monthly bill. The dict is whatever
    the endpoint sent (``{}`` when it sent nothing), plus the ``model`` actually billed.

    For plain reasoning turns (no tools): the PI/Critic roles in the research lab,
    which just read context and emit text/JSON. Thinking traces (if the server runs
    a reasoning parser) are dropped here; only the final ``content`` is returned.
    ``base_url``/``api_key`` override the session tunnel (e.g. OpenRouter for tests).

    ``think=False`` turns OFF the Qwen3 thinking trace (the vLLM-native switch, same as
    ``stream_chat``). Use it for bounded structured-output calls: with thinking ON the
    reasoning trace can consume the whole ``max_tokens`` budget and return EMPTY ``content``
    (``finish_reason=length``) — which is exactly how ``map_phenotype_to_hpo`` was silently
    getting zero HPO terms. A JSON-extraction call needs no chain-of-thought, so disable it.
    """
    url = f"{_endpoint(local_port, base_url)}/chat/completions"
    payload: dict = {"model": model, "messages": messages, "stream": False}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if base_url and _lab_remote_reasoning():
        payload["reasoning"] = _lab_remote_reasoning()
    if not think:
        # Only injected on the opt-out path so the default (orchestrator) call is byte-for-byte
        # unchanged. The phenotype mapper uses the session tunnel, never base_url, so this vLLM-only
        # chat-template kwarg never reaches an OpenRouter endpoint that wouldn't understand it.
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    elif not base_url and _think_on_kwargs():
        # Served models whose template does NOT think by default (DeepSeek-V4 under vLLM's
        # deepseek_v4 tokenizer mode) need the opt-IN spelled out; configured per deployment.
        payload["chat_template_kwargs"] = _think_on_kwargs()
    effort = (reasoning_effort or "").strip().lower() or _reasoning_effort()
    if think and not base_url and effort in REASONING_EFFORTS:
        # HOW HARD to think on the local path. chat_template_kwargs only toggles the trace on/off;
        # a model served with a reasoning parser reads the strength from this field.
        payload["reasoning_effort"] = effort
    if idle_timeout and not base_url:
        try:
            return _complete_streaming(url, payload, model, api_key, idle_timeout)
        except GatewayError as exc:
            if "reasoning_effort" not in payload or not _effort_rejected(exc):
                raise
            # The model does not take this effort level (e.g. "high" on Qwen3.8). Its own default
            # is the right fallback — never fail a plan or a manuscript over a tuning knob.
            payload = {k: v for k, v in payload.items() if k != "reasoning_effort"}
            return _complete_streaming(url, payload, model, api_key, idle_timeout)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(api_key), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local tunnel
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GatewayError(f"vLLM completion error: {detail[:300]}", stage="vllm_chat", detail=detail) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise VLLMNetworkError("Network error during vLLM completion.", stage="vllm_chat", detail=error_detail(exc)) from exc
    choices = (body or {}).get("choices") or [{}]
    content = (choices[0].get("message") or {}).get("content") or ""
    usage = (body or {}).get("usage")
    usage = dict(usage) if isinstance(usage, dict) else {}
    usage.setdefault("model", (body or {}).get("model") or model)
    # Surface WHY generation stopped. "length" means the reply was cut at max_tokens, and a cut
    # reply is not a short reply: a truncated agenda is unparseable JSON, and the PI's plan then
    # falls back to an agenda made of the user's own question with nothing saying so. Measured on
    # claude-sonnet against the 4096 `reason` cap — six truncations in one day, one of which
    # reached a reviewer as a two-step "plan" whose first step was the question.
    usage.setdefault("finish_reason", (choices[0].get("finish_reason") or ""))
    return content, usage


def _effort_rejected(exc: GatewayError) -> bool:
    text = f"{getattr(exc, 'message', '')} {getattr(exc, 'detail', '')}".lower()
    return "reasoning effort" in text or "reasoning_effort" in text


def _complete_streaming(url: str, payload: dict, model: str, api_key: str | None,
                        idle_timeout: float) -> tuple[str, dict]:
    """``complete_ex`` over SSE: the socket timeout is per READ, so it bounds silence, not length."""
    payload = dict(payload, stream=True, stream_options={"include_usage": True})
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=_headers(api_key), method="POST")
    parts: list[str] = []
    usage: dict = {}
    finish = ""
    served_model = ""
    try:
        with urllib.request.urlopen(req, timeout=idle_timeout) as resp:  # noqa: S310 - local tunnel
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    err = chunk["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise GatewayError(f"vLLM completion error: {str(msg)[:300]}", stage="vllm_chat", detail=err)
                served_model = chunk.get("model") or served_model
                if isinstance(chunk.get("usage"), dict):
                    usage = dict(chunk["usage"])
                for ch in chunk.get("choices") or []:
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        parts.append(delta["content"])
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GatewayError(f"vLLM completion error: {detail[:300]}", stage="vllm_chat", detail=detail) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise VLLMNetworkError(f"vLLM sent nothing for {idle_timeout:.0f}s (or the connection dropped).",
                               stage="vllm_chat", detail=error_detail(exc)) from exc
    usage.setdefault("model", served_model or model)
    usage.setdefault("finish_reason", finish)
    return "".join(parts), usage


def complete(
    local_port: int,
    model: str,
    messages: list[dict],
    timeout: float = 600.0,
    max_tokens: int | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    think: bool = True,
) -> str:
    """``complete_ex`` without the usage — the historical ``-> str`` contract, unchanged."""
    return complete_ex(local_port, model, messages, timeout=timeout, max_tokens=max_tokens,
                       base_url=base_url, api_key=api_key, think=think)[0]


def chat_tools(
    local_port: int,
    model: str,
    messages: list[dict],
    tools: list[dict],
    timeout: float = 600.0,
    fmt: dict | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """One **non-streaming** ``/v1/chat/completions`` call WITH tool schemas.

    Returns ``{"content": str, "tool_calls": list, "finish_reason": str}`` — the SAME shape as
    ``ollama.chat_tools`` plus why generation stopped (the OpenAI ``tool_calls`` items carry
    ``function.name`` and ``function.arguments`` as a JSON *string*, which
    ``ResearchHarness._parse_call`` already accepts), so the agentic harness is backend-agnostic.

    ``reasoning_effort`` sets how hard a LOCAL model thinks before it acts, falling back to
    ``BIOAGENT_VLLM_REASONING_EFFORT``. Without it Qwen3.8 thinks at its default, xhigh, and on
    2026-09-30 a Scientist turn spent its whole 8192-token reservation reasoning: finish_reason
    "length", no tool call, a wasted two-minute turn, step after step. A model that rejects the
    level is asked again without the field, as ``complete_ex`` does.

    ``fmt`` (a JSON schema) maps to vLLM's structured-output ``response_format`` on
    the harness's constrained-decoding fallback path. ``base_url``/``api_key`` override
    the session tunnel (e.g. OpenRouter for tests).

    ``max_tokens`` RESERVES output room: vLLM caps prompt+output at ``--max-model-len``
    and, when ``max_tokens`` is unset, defaults the output budget to ``max_model_len −
    prompt`` — so a near-full prompt yields a *0-token* budget and a hard 400. Always
    reserving a slice keeps that from happening; the harness budgets its history to
    leave at least this much room (see ``ResearchHarness._budget_messages``).
    """
    url = f"{_endpoint(local_port, base_url)}/chat/completions"
    payload: dict = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "stream": False,
    }
    if max_tokens is None:
        max_tokens = scientist_max_tokens()      # 2048 unless BIOAGENT_SCIENTIST_MAX_TOKENS
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if not base_url and _scientist_kwargs():
        payload["chat_template_kwargs"] = _scientist_kwargs()
    if fmt is not None:
        payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "tool_selection", "schema": fmt}}
    effort = (reasoning_effort or "").strip().lower() or _reasoning_effort()
    if not base_url and effort in REASONING_EFFORTS:
        payload["reasoning_effort"] = effort

    def _post(body_payload: dict) -> dict:
        data = json.dumps(body_payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=_headers(api_key), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local tunnel
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GatewayError(f"vLLM tool-chat error: {detail[:300]}", stage="vllm_chat", detail=detail) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise VLLMNetworkError("Network error during vLLM tool-chat.", stage="vllm_chat", detail=error_detail(exc)) from exc
        if isinstance(body, dict) and body.get("error"):
            raise GatewayError(f"vLLM tool-chat error: {body['error']}", stage="vllm_chat", detail=body)
        return body

    try:
        body = _post(payload)
    except GatewayError as exc:
        if "reasoning_effort" not in payload or not _effort_rejected(exc):
            raise
        body = _post({k: v for k, v in payload.items() if k != "reasoning_effort"})
    choices = (body or {}).get("choices") or [{}]
    message = choices[0].get("message") or {}
    return {"content": message.get("content") or "", "tool_calls": message.get("tool_calls") or [],
            "finish_reason": str(choices[0].get("finish_reason") or "")}


def _tokenize_root(local_port: int, base_url: str | None) -> str | None:
    """Root for vLLM's ``/tokenize`` (it lives at the server ROOT, not under ``/v1``).
    Returns None for a non-vLLM ``base_url`` (e.g. OpenRouter) — exact server-side
    tokenization is a vLLM feature, so callers fall back to the char estimate there."""
    if base_url:
        return None                                   # remote OpenAI-compatible API: no /tokenize
    return f"http://127.0.0.1:{local_port}"


def count_tokens(
    local_port: int,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    timeout: float = 30.0,
    base_url: str | None = None,
    api_key: str | None = None,
) -> int | None:
    """EXACT prompt token count from vLLM's server-side ``/tokenize`` — the count is
    computed by the SAME tokenizer + chat template the server enforces ``--max-model-len``
    with, INSIDE the Singularity container on the GPU node. The gateway only ships the
    messages over the tunnel and reads back an integer; no tokenizer/model files land on
    eye-server.

    Returns the token count, or ``None`` when exact counting is unavailable (remote
    base_url, an older server without ``/tokenize``, or any transport error) so the
    caller transparently falls back to the character estimate. Never raises — token
    counting must never be the thing that breaks a run.
    """
    root = _tokenize_root(local_port, base_url)
    if root is None:
        return None
    url = f"{root}/tokenize"

    def _post(payload: dict) -> int | None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=_headers(api_key), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local tunnel
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        count = body.get("count") if isinstance(body, dict) else None
        if count is None and isinstance(body, dict):       # older shape: just a token list
            toks = body.get("tokens")
            count = len(toks) if isinstance(toks, list) else None
        return int(count) if isinstance(count, int) else None

    # add_generation_prompt mirrors a real chat call; send tools when present so the
    # count includes the tool-schema injection. If this server build rejects `tools` on
    # /tokenize, retry without them (the caller adds the schema estimate separately).
    base_payload = {"model": model, "messages": messages, "add_generation_prompt": True}
    if tools:
        got = _post({**base_payload, "tools": tools})
        if got is not None:
            return got
    return _post(base_payload)


# The tools ask the session's served model through ``tools.sdk.session_chat_fn`` instead of
# importing this module (the tools must not import the gateway). Register the backend here, at
# import time; the lambda looks ``complete`` up on every call, so tests that monkeypatch
# ``vllm_client.complete`` still intercept the tools' calls.
from ..tools import sdk as _tool_sdk  # noqa: E402

_tool_sdk.register_llm_backend(lambda *a, **k: complete(*a, **k))
