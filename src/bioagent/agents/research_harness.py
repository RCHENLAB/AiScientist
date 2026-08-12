"""Autonomous research-orchestration harness — local Qwen3.6 picks & sequences tools.

This is the orchestration layer that replaces the upstream Kosmos CLI's research
loop. Instead of a fixed staged loop, the local model *decides which tool to call
next* (QC, DE/markers, Biomni, the native deep-research loop) via **Ollama's native
tool-calling** (`/api/chat tools=` → `message.tool_calls`), which Qwen3.6 supports.

Two hard requirements drove the design:

1. **vLLM (OpenAI /v1) tool-calling compatibility, hardened.** Native tool-calling is primary;
   every call is validated (known tool + parseable args + required keys). If the
   model emits no/malformed `tool_calls`, we fall back to parsing a JSON
   ``{"tool": ..., "args": ...}`` object out of its text (reusing the autonomous-loop
   JSON helpers). The Kosmos failure was Kosmos's *own* function-call format, not
   the OpenAI-compatible /v1 tool API (which Qwen3.6 supports) is the compatible path.
2. **Prompt tool-error feedback.** A tool failure is (a) emitted to ``on_event``
   immediately, (b) fed back to the model as a ``{"error": ...}`` tool message so the
   loop can adapt or report it, and (c) recorded in ``HarnessResult.errors`` — never
   silently swallowed or faked as success (the failed step is ``ok=False``).

The tool executors WRAP existing code (lab QC/DE builders) — nothing is reimplemented.
``DataBoundaryGuard`` gates the brief before any model call. A ``chat_fn`` can be injected so the whole loop runs
offline in tests with scripted tool calls; the default calls ``gateway.vllm_client.chat_tools``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .loop_utils import CheckpointWriter, safe_json_loads
from ..integrations.biomni_adapter import BiomniAdapter
from ..integrations.safety import DataBoundaryGuard, DataBoundaryPolicy
from ..tools.execution import build_de_marker_execution, build_single_cell_qc_execution

# chat_fn(messages, tools) -> {"content": str, "tool_calls": list}
ChatToolFn = Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]]
# count_tokens_fn(messages, tools) -> exact prompt token count, or None when the served
# backend can't tokenize (remote API / older server). EXACT counting runs server-side on
# the GPU node (vLLM /tokenize), so the harness senses the real window instead of guessing.
CountTokensFn = Callable[[list[dict[str, Any]], list[dict[str, Any]]], "int | None"]
# on_event(event_dict) -> None  (live progress/error stream for a UI)
EventFn = Callable[[dict[str, Any]], None]

_HARNESS_SYSTEM = (
    "You are AiScientist's autonomous biology research orchestrator. You plan by CALLING TOOLS, "
    "one or more per turn, then reading their results. Available tools let you run lightweight "
    "single-cell QC, a differential-expression / marker screen, an optional Biomni biomedical "
    "backend, and a deep multi-stage research-reasoning loop. Sequence them as needed, then call "
    "`finish` with your final answer. Rules: treat every conclusion as a hypothesis to validate; "
    "never invent dataset numbers, gene symbols, or statistics that a tool did not return; if a "
    "tool returns an error, adapt or report it honestly rather than pretending it succeeded.\n\n"
    "Your tools are not black boxes. A tool's description states what it is FOR; only its source "
    "states what it DOES — the caps, thresholds and defaults that decide the numbers you will "
    "report. When `read_tool_source` is available, read the implementation before you rely on a "
    "result that hinges on a parameter you did not set yourself, and before you conclude that a "
    "step's output is complete. Its `defaults` field lists the values nobody chose; judge each "
    "against THIS dataset. If one looks wrong, recompute the quantity independently in the code "
    "sandbox and compare rather than asserting it — and report the discrepancy either way. A "
    "silent cap that makes a result look clean is worse than an error, because nothing downstream "
    "can detect it."
)

_NUDGE = (
    "You did not call a tool. Either call one of the available tools, or — if you are done — "
    "call `finish` with your final answer. If you cannot use native tool-calling, reply with a "
    'single JSON object: {"tool": "<tool_name>", "args": {...}}.'
)


@dataclass(frozen=True)
class HarnessTool:
    """One callable tool: an OpenAI/vLLM function schema + a Python executor."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON-schema for the args object
    executor: Callable[[dict[str, Any], "HarnessContext"], dict[str, Any]]
    reads_private_data: bool = False
    category: str = "general"            # registry metadata: qc | analysis | figure | codeact | backend | control
    requires: tuple[str, ...] = ()       # capability deps (e.g. "scanpy", "gseapy", "graphviz", "biomni")

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


@dataclass
class HarnessContext:
    """Everything the tools need, threaded through one harness run.

    ``decisions`` carries the analyzed dataset (the lab QC/DE builders read
    ``decisions['dataset_result']``). ``tunnel_port`` / ``model`` point the default
    live ``chat_fn`` at this session's Qwen3.6 tunnel. ``biomni`` is a legacy optional
    backend field (the run_biomni/deep_research tools were retired in favour of a
    dedicated literature tool); kept only so existing integration code still imports.
    """

    decisions: dict[str, Any] = field(default_factory=dict)
    workspace: Path | None = None
    tunnel_port: int | None = None
    model: str = "qwen3.6:35b-a3b"
    biomni: BiomniAdapter | None = None
    # Does the prompt LEAVE this host? Set by whoever binds the LLM (the gateway's ``_lab_llm``),
    # because only that code knows which endpoint the calls actually go to. The data-boundary guard
    # keys its raw-data decision on this. It CANNOT be inferred from ``tunnel_port``: a session can
    # hold an open vLLM tunnel while the lab's completions are routed to a remote API, and reading
    # the tunnel as proof of locality would send raw tables off-site. Defaults False so every
    # existing caller (tests, scripts, the all-local production path) behaves exactly as before.
    llm_is_remote: bool = False


def _default_max_model_len() -> int:
    """The served context window. Mirrors ``HPCSettings.vllm_max_model_len`` /
    ``--max-model-len`` (see gpu.py) so the harness budgets against the SAME window the
    serve job enforces — read from the env override when the operator narrowed it.

    Default = the model's native 262144. This MUST track the serve-job value: budgeting
    against a smaller number than vLLM enforces silently throws away usable context, which is
    exactly what the old 32768 default did on cards that were serving 262144 all along."""
    try:
        return int(os.environ.get("BIOAGENT_VLLM_MAX_MODEL_LEN", "262144"))
    except ValueError:
        return 262144


@dataclass(frozen=True)
class HarnessConfig:
    max_steps: int = 8          # model turns before we force-stop
    max_bad_calls: int = 3      # consecutive invalid/empty turns before giving up
    # Two cheap early-outs so a step doesn't grind all the way to ``max_steps`` (which wastes
    # GPU + context on a stuck loop — see the 5bd05b3f5880 post-mortem). NOTE the first is on
    # REPEATED IDENTICAL errors (the model is stuck, not learning) — DIFFERENT errors are the
    # model debugging/converging and are allowed to run up to ``max_steps``, so a legitimately
    # iterating step is never cut off after a few distinct failures.
    max_repeated_errors: int = 3    # same tool error N times in a row → bail (stuck)
    max_wasted_after_success: int = 2  # once a tool has succeeded, this many non-productive turns
                                       # (a repeated identical call, or an error) → stop with the win
    checkpoint_seconds: float = 60.0  # heartbeat/stall cadence (only if workspace is set)
    # Context-window budgeting for the running history (the served model caps prompt+output
    # at ``max_model_len``; the full tool catalog is resent every turn and the tool results
    # pile up, so without trimming a long step overflows the window and vLLM 400s).
    max_model_len: int = field(default_factory=_default_max_model_len)
    output_reserve_tokens: int = 2048   # room left for the model's reply (matches vllm_client.chat_tools)
    context_safety_margin: int = 2048   # slack for tokenizer drift vs the char→token estimate
    # Reactive self-compaction: the char→token estimate can still undershoot for dense
    # JSON (schemas/result payloads tokenize denser than prose), landing 1-2% over the
    # hard window. When vLLM 400s on context length we re-trim with this much EXTRA
    # reserve and retry, up to ``context_retries`` times, instead of failing the run.
    context_retries: int = 3
    context_retry_extra_tokens: int = 3072


@dataclass
class HarnessResult:
    status: str                       # "ok" | "incomplete" | "blocked_by_guard"
    stop_reason: str | None
    final_answer: str | None
    steps: list[dict[str, Any]]       # each: {tool, args, ok, summary|error}
    errors: list[dict[str, Any]]      # each: {tool, error}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- tool executors (wrap existing code; never reimplement) ------------------


def _exec_qc(args: dict[str, Any], ctx: HarnessContext) -> dict[str, Any]:
    return build_single_cell_qc_execution(ctx.decisions)


def _exec_de(args: dict[str, Any], ctx: HarnessContext) -> dict[str, Any]:
    return build_de_marker_execution(ctx.decisions)


def _exec_finish(args: dict[str, Any], ctx: HarnessContext) -> dict[str, Any]:  # pragma: no cover - loop intercepts
    return {"final_answer": str(args.get("answer", ""))}


def default_catalog() -> list[HarnessTool]:
    return [
        HarnessTool(
            "run_qc",
            "Run lightweight single-cell QC on the already-loaded dataset and return cell/gene counts, "
            "mean UMI, mean mito fraction, and QC flags.",
            {"type": "object", "properties": {}},
            _exec_qc,
            reads_private_data=True, category="qc",
        ),
        HarnessTool(
            "run_de_markers",
            "Run a lightweight differential-expression / marker effect-size screen on the loaded dataset "
            "and return ranked candidate genes (effect sizes only, no statistical test).",
            {"type": "object", "properties": {}},
            _exec_de,
            reads_private_data=True, category="de",
        ),
        HarnessTool(
            "finish",
            "Finish the research and return the final answer to the user. Call this exactly once when done.",
            {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
            _exec_finish, category="control",
        ),
    ]


class ResearchHarness:
    """The agentic loop: model picks tools, harness validates/executes, errors stream back."""

    def __init__(
        self,
        catalog: list[HarnessTool] | None = None,
        config: HarnessConfig | None = None,
        chat_fn: ChatToolFn | None = None,
        count_tokens_fn: CountTokensFn | None = None,
    ) -> None:
        self.catalog = catalog or default_catalog()
        self._by_name = {tool.name: tool for tool in self.catalog}
        self.config = config or HarnessConfig()
        self._chat_fn = chat_fn
        # Optional EXACT token counter (vLLM /tokenize, server-side on the GPU node). When
        # present, _budget_messages senses the real boundary instead of estimating; when
        # None (offline tests / remote API), it falls back to the char heuristic.
        self._count_tokens_fn = count_tokens_fn

    def add_tools(self, *tools: "HarnessTool") -> None:
        """Attach tools not already present (by name), updating BOTH the catalog (→ tool schemas
        sent to the model) AND the dispatch map (``_by_name``, built once at construction). Lets a
        caller add tools to an ALREADY-built harness — e.g. the lab attaching the progressive-
        disclosure skill tools to the gateway's injected scientist, so ``read_skill_reference`` /
        ``search_skills`` actually exist at call time instead of erroring as 'unknown tool'."""
        for tool in tools:
            if tool.name not in self._by_name:
                self.catalog.append(tool)
                self._by_name[tool.name] = tool

    # -- public ---------------------------------------------------------------

    def run(self, brief: str, ctx: HarnessContext, on_event: EventFn | None = None,
            should_cancel: Callable[[], bool] | None = None,
            untrusted_text: str | None = None) -> HarnessResult:
        emit: EventFn = on_event or (lambda _event: None)
        steps: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        # 1) Privacy boundary first — inspect the brief before any model call.
        #    SECRETS (api keys) are ALWAYS blocked. Raw TABULAR data is only a leak risk
        #    when the LLM is REMOTE: when this run targets the session's LOCAL tunneled
        #    Qwen (``ctx.tunnel_port`` set), the prompt never leaves the box, so raw tables
        #    are allowed through — the Scientist legitimately passes small result tables
        #    (predictions, marker rows) between steps, and a hard block there was killing
        #    real steps (e.g. run_de) for no privacy gain. Set
        #    ``BIOAGENT_GUARD_BLOCK_RAW_DATA_ALWAYS=1`` to force the strict (block-always)
        #    behavior regardless of endpoint.
        #    Raw-data detection is SOURCE-SCOPED: when the caller passes ``untrusted_text``
        #    (the user-provided spans — question + mid-run notes), only that is scanned for a
        #    raw matrix; the system-built brief scaffolding (personas, step text, artifact
        #    paths, reference code) is trusted by construction and never sniffed. Secrets are
        #    always scanned across the whole brief.
        #    "Local" requires POSITIVE evidence that the prompt stays on the box: a tunnel to this
        #    session's vLLM AND no remote endpoint bound over the top of it. ``llm_is_remote`` is
        #    authoritative and wins — a session can hold an open tunnel (the GPU is still allocated)
        #    while the lab's completions are routed to a paid API, and treating the tunnel as proof
        #    of locality would put raw tables into an off-site prompt.
        local_llm = (getattr(ctx, "tunnel_port", None) is not None
                     and not getattr(ctx, "llm_is_remote", False))
        if os.environ.get("BIOAGENT_GUARD_BLOCK_RAW_DATA_ALWAYS", "").lower() in ("1", "true", "yes"):
            local_llm = False
        guard = DataBoundaryGuard()
        report = guard.inspect_prompt(brief, None, DataBoundaryPolicy(allow_raw_data_to_llm=local_llm),
                                      raw_data_scope=untrusted_text)
        try:
            guard.assert_safe_for_prompt(report)
        except ValueError as exc:
            emit({"type": "blocked", "reason": str(exc)})
            return HarnessResult("blocked_by_guard", "blocked_by_guard", None, steps, [{"tool": None, "error": str(exc)}])
        # Audit trail: when raw tabular data passed through ONLY because the endpoint is
        # local, surface it so the transcript records the boundary decision.
        if report.prompt_contains_raw_data_risk and local_llm:
            emit({"type": "note", "reason": "raw table data allowed into the prompt — endpoint is the local tunneled Qwen (no data leaves UCI)."})

        chat = self._resolve_chat(ctx)
        tool_schemas = [tool.schema() for tool in self.catalog]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _HARNESS_SYSTEM},
            {"role": "user", "content": brief},
        ]

        checkpoint = CheckpointWriter(ctx.workspace, self.config.checkpoint_seconds) if ctx.workspace else None
        if checkpoint:
            checkpoint.start()

        final_answer: str | None = None
        stop_reason: str | None = None
        bad_calls = 0
        repeated_errors = 0          # consecutive IDENTICAL tool errors (the model is stuck)
        last_error_sig: str | None = None
        wasted_after_success = 0     # non-productive turns AFTER a tool already succeeded
        had_success = False
        succeeded_keys: set[str] = set()   # (tool,args) that already ran ok — repeats are redundant
        try:
            for step_index in range(1, self.config.max_steps + 1):
                # Cooperative cancel: the user can stop a long step between model turns,
                # not just between agenda steps (a single scanpy step can take a while).
                if should_cancel is not None and should_cancel():
                    stop_reason = "cancelled"
                    emit({"type": "cancelled", "where": "scientist", "step": step_index})
                    break
                emit({"type": "model_call", "step": step_index})
                # Keep the prompt inside the served context window: the history grows every
                # step (assistant turns + piled-up tool results) and the full tool catalog is
                # resent each call, so a long step would otherwise overflow max_model_len and
                # vLLM would reject the whole request. Trim before the call, not after a 400.
                messages = self._budget_messages(messages, tool_schemas, emit)
                # Reactive self-compaction: the proactive char→token budget can still
                # undershoot for dense JSON and land just over the served window. Rather
                # than fail the whole run on a 400, recompact HARDER (more reserve) and
                # retry — each pass drops/compresses more old turns.
                message = None
                for attempt in range(self.config.context_retries + 1):
                    try:
                        message = chat(messages, tool_schemas)
                        break
                    except Exception as exc:  # noqa: BLE001 - inspect, retry only on overflow
                        if attempt >= self.config.context_retries or not _is_context_overflow(exc):
                            raise
                        extra = (attempt + 1) * self.config.context_retry_extra_tokens
                        messages = self._budget_messages(messages, tool_schemas, emit, extra_reserve=extra)
                        emit({"type": "context_overflow_retry", "attempt": attempt + 1, "extra_reserve": extra})
                native_calls = message.get("tool_calls") or []
                # Echo the assistant turn back; include tool_calls only when present
                # (an empty ``tool_calls: []`` trips strict OpenAI-compatible servers).
                assistant_msg: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
                if native_calls:
                    assistant_msg["tool_calls"] = native_calls
                messages.append(assistant_msg)

                calls = native_calls
                native = bool(native_calls)
                if not calls:
                    fallback = self._fallback_tool_call(message.get("content") or "")
                    if fallback is None:
                        text = (message.get("content") or "").strip()
                        if text:
                            final_answer, stop_reason = text, "model_final_text"
                            break
                        bad_calls += 1
                        if bad_calls >= self.config.max_bad_calls:
                            stop_reason = "no_tool_calls"
                            break
                        messages.append({"role": "user", "content": _NUDGE})
                        continue
                    calls = [fallback]
                    native = False

                for call in calls:
                    # Stop pressed during the model call (before this tool runs): break now,
                    # don't launch another (possibly long) tool call.
                    if should_cancel is not None and should_cancel():
                        stop_reason = "cancelled"
                        break
                    name, args, validation_error = self._validate_call(call)
                    if validation_error:
                        bad_calls += 1
                        errors.append({"tool": name, "error": validation_error})
                        emit({"type": "tool_error", "tool": name, "error": validation_error})
                        self._feed_result(messages, native, call.get("id"), {"error": validation_error})
                        continue
                    bad_calls = 0

                    if name == "finish":
                        final_answer = str(args.get("answer", "")).strip() or "(no answer provided)"
                        steps.append({"tool": "finish", "args": args, "ok": True})
                        emit({"type": "finish", "answer_preview": final_answer[:200]})
                        stop_reason = "finished"
                        break

                    # Redundant repeat: the model re-issued a call that ALREADY succeeded this
                    # step (e.g. re-running clustering mid-DE). Don't re-execute — nudge it to
                    # finish and count the wasted turn toward the correct-early-stop below.
                    call_key = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
                    if call_key in succeeded_keys:
                        wasted_after_success += 1
                        self._feed_result(messages, native, call.get("id"),
                                          {"note": "duplicate call — this exact tool+args already "
                                                   "succeeded and nothing changed. Call `finish` if "
                                                   "the step is complete."})
                        continue

                    emit({"type": "tool_start", "tool": name, "args": args})
                    if checkpoint:
                        checkpoint.log("tool_start", tool=name)
                    try:
                        output = self._by_name[name].executor(args, ctx)
                    except Exception as exc:  # noqa: BLE001 - any tool failure must be reported, never fatal
                        error_text = f"{type(exc).__name__}: {exc}"
                        errors.append({"tool": name, "error": error_text})
                        steps.append({"tool": name, "args": args, "ok": False, "error": error_text})
                        emit({"type": "tool_error", "tool": name, "error": error_text})  # immediate feedback
                        if checkpoint:
                            checkpoint.log("tool_error", tool=name, error=error_text)
                        # Signature = error text with numbers masked (line nos / counters / paths
                        # vary) so "the SAME failure again" is detected, while a genuinely different
                        # error resets the streak (that's the model debugging, not stuck).
                        sig = re.sub(r"\d+", "#", error_text)[:200]
                        repeated_errors = repeated_errors + 1 if sig == last_error_sig else 1
                        last_error_sig = sig
                        if had_success:
                            wasted_after_success += 1
                        # Feed the error back so the model can adapt or report it.
                        self._feed_result(messages, native, call.get("id"), {"error": error_text})
                        continue

                    # A cancellable tool (run_code) scancelled/killed itself because Stop fired —
                    # end the step now instead of treating it as a normal result and pressing on.
                    if isinstance(output, dict) and output.get("status") == "cancelled":
                        steps.append({"tool": name, "args": args, "ok": False, "result": output})
                        stop_reason = "cancelled"
                        break

                    # A real (non-finish) tool succeeded → reset the error streak; repeats now redundant.
                    repeated_errors, last_error_sig = 0, None
                    had_success = True
                    succeeded_keys.add(call_key)
                    summary = _summarize(output)
                    # Keep the FULL structured tool return on the step (not just a one-line
                    # status): the Critic and synthesis need to see what was actually
                    # produced (artifact paths, counts, status) to judge/ground a step,
                    # instead of a hand-maintained field projection. ``summary`` stays for
                    # the live event stream.
                    steps.append({"tool": name, "args": args, "ok": True, "summary": summary, "result": output})
                    # Carry ``args`` on success so the UI can surface a step's FINAL successful
                    # code (run_code) as a formatted block, without pasting the raw snippet into
                    # the always-visible progress feed.
                    emit({"type": "tool_result", "tool": name, "summary": summary, "args": args})
                    self._feed_result(messages, native, call.get("id"), output)

                if stop_reason in ("finished", "cancelled"):
                    break
                # Cheap early-outs (before burning the whole max_steps budget on a stuck loop):
                if repeated_errors >= self.config.max_repeated_errors:
                    stop_reason = "repeated_tool_errors"   # same error N times → stuck, not learning
                    emit({"type": "early_stop", "reason": stop_reason, "count": repeated_errors})
                    break
                if had_success and wasted_after_success >= self.config.max_wasted_after_success:
                    # The step already produced a usable result; the model is spinning (repeats/
                    # errors) instead of finishing. Stop with the win — the Critic accepts on the
                    # artifact even without a textual finish (produced_artifact floor).
                    stop_reason = "done_early"
                    emit({"type": "early_stop", "reason": stop_reason, "count": wasted_after_success})
                    break
            else:
                stop_reason = stop_reason or "max_steps"
        finally:
            if checkpoint:
                checkpoint.stop()

        status = "ok" if final_answer is not None else "incomplete"
        return HarnessResult(status, stop_reason or "max_steps", final_answer, steps, errors)

    # -- internals ------------------------------------------------------------

    def _resolve_chat(self, ctx: HarnessContext) -> ChatToolFn:
        if self._chat_fn is not None:
            return self._chat_fn

        def _live(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
            from ..gateway import vllm_client  # deferred: keep agents decoupled from the gateway

            return vllm_client.chat_tools(ctx.tunnel_port, ctx.model, messages, tools)

        return _live

    def _validate_call(self, call: dict[str, Any]) -> tuple[str | None, dict[str, Any], str | None]:
        function = (call or {}).get("function") or {}
        name = function.get("name")
        if name not in self._by_name:
            return name, {}, f"unknown tool '{name}'"
        raw = function.get("arguments")
        if isinstance(raw, str):
            args = safe_json_loads(raw) if raw.strip() else {}
            if args is None:
                return name, {}, f"arguments for '{name}' were not valid JSON"
        elif isinstance(raw, dict):
            args = raw
        elif raw is None:
            args = {}
        else:
            return name, {}, f"arguments for '{name}' had unexpected type {type(raw).__name__}"
        required = self._by_name[name].parameters.get("required", [])
        missing = [key for key in required if key not in args]
        if missing:
            return name, args, f"missing required args for '{name}': {missing}"
        return name, args, None

    def _fallback_tool_call(self, content: str) -> dict[str, Any] | None:
        """Parse a JSON ``{"tool": ..., "args": ...}`` object out of the model's text.

        The hardening path for models that don't emit native ``tool_calls`` reliably.
        """
        parsed = safe_json_loads(content)
        if isinstance(parsed, dict) and parsed.get("tool"):
            return {"function": {"name": parsed["tool"], "arguments": parsed.get("args") or {}}}
        return None

    @staticmethod
    def _feed_result(messages: list[dict[str, Any]], native: bool, call_id: str | None, payload: dict[str, Any]) -> None:
        """Feed a tool result back to the model. A *native* tool-call requires an
        OpenAI ``role:tool`` message carrying the matching ``tool_call_id`` — strict
        OpenAI-compatible servers (vLLM, OpenRouter) return 400 without it. The
        JSON-text fallback path has no id, so its result goes back as a user message."""
        text = json.dumps(payload)[:4000]
        if native and call_id:
            messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
        else:
            messages.append({"role": "user", "content": f"Tool result: {text}"})

    def _exact_token_count(
        self, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]]
    ) -> int | None:
        """EXACT prompt token count from the served model's own tokenizer (vLLM
        ``/tokenize``, computed server-side on the GPU node), or ``None`` when no exact
        counter is wired / it fails — the budgeter then trusts the char estimate."""
        if self._count_tokens_fn is None:
            return None
        try:
            return self._count_tokens_fn(messages, tool_schemas)
        except Exception:  # noqa: BLE001 - token counting must never break a run
            return None

    def _budget_messages(
        self,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        emit: EventFn,
        extra_reserve: int = 0,
    ) -> list[dict[str, Any]]:
        """Fit the running history to the served window — EXACTLY when the server can
        tokenize, else by char estimate.

        First trim by the cheap char estimate (picks what to drop without a round-trip).
        Then, if an exact server-side counter is available, VERIFY the real token count
        and, while it still overruns the true input cap (``max_model_len − output_reserve
        − extra_reserve``), tighten and re-measure — so we sense the actual boundary
        instead of trusting the estimate. With no counter (offline tests / remote API)
        the estimate stands, exactly as before.
        """
        candidate = self._budget_by_estimate(messages, tool_schemas, emit, extra_reserve)
        exact = self._exact_token_count(candidate, tool_schemas)
        if exact is None:
            return candidate                       # no server tokenizer → estimate is all we have
        allowed = self.config.max_model_len - self.config.output_reserve_tokens - extra_reserve
        emit({"type": "context_measured", "exact_tokens": exact, "allowed": allowed})
        bump = 0
        # Tighten until the EXACT count fits (or we can't shrink further). Converges in
        # 1-2 passes in practice; the cap stops a pathological non-shrinking loop.
        while exact > allowed and bump < self.config.max_model_len:
            bump += self.config.context_retry_extra_tokens
            tighter = self._budget_by_estimate(messages, tool_schemas, emit, extra_reserve + bump)
            new_exact = self._exact_token_count(tighter, tool_schemas)
            if new_exact is None or new_exact >= exact:   # can't measure or not shrinking → stop
                break
            candidate, exact = tighter, new_exact
        return candidate

    def _budget_by_estimate(
        self,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        emit: EventFn,
        extra_reserve: int = 0,
    ) -> list[dict[str, Any]]:
        """Trim the running history to fit the served model's context window.

        ``max_model_len`` caps prompt+output together; the full tool catalog schema is
        resent on every call and counts toward the prompt, so the room left for the
        message history is ``window − output_reserve − safety_margin − schema``. We keep
        the system message and the initial brief (indices 0–1) verbatim, then walk the
        turns NEWEST→OLDEST: a turn that still fits is kept verbatim; an older one is
        COMPRESSED (a succeeded tool result collapses to a ``result_digest`` stub; a
        failed/retried one is elided to a one-line marker — the failures the user does
        not need re-fed); once even the compressed turn won't fit, it and everything
        older are DROPPED. Whole turns (an assistant's ``tool_calls`` + its ``role:tool``
        replies) are kept, compressed, or evicted as a unit, so native tool-call pairing
        is never broken. The dropped detail still lives in ``HarnessResult.steps`` for
        the Critic and synthesis — only the model's working context is trimmed.
        """
        token_budget = (
            self.config.max_model_len
            - self.config.output_reserve_tokens
            - self.config.context_safety_margin
            - extra_reserve
            - _approx_tokens(json.dumps(tool_schemas))
        )
        # Degenerate (tiny window / huge schema): keep only the preamble + last turn.
        token_budget = max(token_budget, _MIN_BODY_TOKENS)

        preamble = messages[:2]
        used = sum(_msg_tokens(m) for m in preamble)
        turns = _group_turns(messages[2:])
        if not turns or used + sum(_msg_tokens(m) for t in turns for m in t) <= token_budget:
            return messages  # already fits — no copy, no churn

        kept_newest_first: list[list[dict[str, Any]]] = []
        compressed = dropped = 0
        ordered = list(reversed(turns))  # newest first
        for position, turn in enumerate(ordered):
            verbatim = sum(_msg_tokens(m) for m in turn)
            if used + verbatim <= token_budget:
                kept_newest_first.append(turn)
                used += verbatim
                continue
            squeezed = [_compress_message(m) for m in turn]
            squeezed_cost = sum(_msg_tokens(m) for m in squeezed)
            if used + squeezed_cost <= token_budget:
                kept_newest_first.append(squeezed)
                used += squeezed_cost
                compressed += 1
                continue
            # Even compressed it overflows: drop this turn and all older ones, so the
            # kept window stays contiguous (no holes in the conversation).
            dropped = len(ordered) - position
            break
        kept_newest_first.reverse()
        emit({"type": "context_trimmed", "compressed_turns": compressed,
              "dropped_turns": dropped, "approx_tokens": used})
        return preamble + [m for turn in kept_newest_first for m in turn]


# --- context-window budgeting helpers ----------------------------------------
#
# We have no served-model tokenizer in-process, so token counts are ESTIMATED from
# characters. The ratio is deliberately low (≈ overcount tokens) so we under-fill the
# window rather than risk a 400 — JSON tool payloads tokenize denser than prose.
_CHARS_PER_TOKEN = 2.6   # JSON tool schemas/results tokenize denser than prose; overcount to stay safe
_PER_MESSAGE_OVERHEAD = 8       # role/formatting tokens the chat template adds per message
_MIN_BODY_TOKENS = 2048         # floor so a pathological schema/window still leaves working room
_COMPRESS_CONTENT_HEAD = 400    # chars of assistant prose kept when compressing an old turn
_COMPRESS_ARGS_HEAD = 300       # chars of a tool-call's arguments kept when compressing
_TOOL_RESULT_PREFIX = "Tool result: "


# Substrings vLLM / OpenAI-compatible servers put in a context-length 400. Matched
# case-insensitively against the raised error so the harness can react WITHOUT importing
# the gateway's error type (keeps agents→gateway decoupled). English only — the served
# vLLM emits English; any UI translation happens downstream of this check.
_CONTEXT_OVERFLOW_SIGNATURES = (
    "maximum context length",
    "context length",
    "context_length_exceeded",
    "reduce the length",
    "longer than the maximum",
)


def _is_context_overflow(exc: Exception) -> bool:
    """True when an exception looks like a served-model context-length rejection."""
    text = str(exc).lower()
    detail = str(getattr(exc, "detail", "") or "").lower()
    blob = text + " " + detail
    return any(sig in blob for sig in _CONTEXT_OVERFLOW_SIGNATURES)


def _approx_tokens(text: str) -> int:
    return int(len(text) / _CHARS_PER_TOKEN) + _PER_MESSAGE_OVERHEAD


def _msg_tokens(message: dict[str, Any]) -> int:
    """Estimated token cost of one chat message (content + any tool-call arguments)."""
    total = len(message.get("content") or "")
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += len(str(function.get("arguments") or "")) + len(str(function.get("name") or ""))
    return _approx_tokens_from_len(total)


def _approx_tokens_from_len(char_len: int) -> int:
    return int(char_len / _CHARS_PER_TOKEN) + _PER_MESSAGE_OVERHEAD


def _group_turns(body: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split the post-preamble history into turns. A turn starts at an ``assistant``
    message and runs up to (not including) the next ``assistant`` — so an assistant's
    ``tool_calls`` and the ``role:tool`` replies that answer them stay in ONE group and
    are only ever kept/compressed/evicted together (native tool-call pairing intact)."""
    turns: list[list[dict[str, Any]]] = []
    for message in body:
        if message.get("role") == "assistant" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


def _is_failure_payload(payload: Any) -> bool:
    """A tool result the model does not need re-fed verbatim: an error or a failed status."""
    if not isinstance(payload, dict):
        return False
    if payload.get("error"):
        return True
    status = str(payload.get("status", "")).lower()
    return status in _FAILED_TOOL_STATUSES or status.startswith(("error", "blocked", "fail"))


def _compress_message(message: dict[str, Any]) -> dict[str, Any]:
    """A size-reduced COPY of an older message, preserving role/ids (so pairing holds).

    Tool results: a success collapses to a ``result_digest`` stub (paths/counts/status
    kept); a failure elides to a one-line marker. Assistant turns keep a head of their
    prose and truncate each tool-call's argument blob (e.g. a long ``run_code`` body)."""
    role = message.get("role")
    out = dict(message)

    if role in ("tool", "user"):
        content = message.get("content") or ""
        raw = content
        is_tool_result = role == "tool" or content.startswith(_TOOL_RESULT_PREFIX)
        if not is_tool_result:
            return out  # a nudge or plain note — already tiny
        if content.startswith(_TOOL_RESULT_PREFIX):
            raw = content[len(_TOOL_RESULT_PREFIX):]
        parsed = safe_json_loads(raw)
        if parsed is None:                       # truncated/un-parseable: keep a head
            new = raw[:_COMPRESS_ARGS_HEAD] + "…"
        elif _is_failure_payload(parsed):
            new = json.dumps({"_elided": "earlier failed tool attempt",
                              "status": parsed.get("status") or "error"})
        else:
            new = json.dumps(result_digest(parsed))
        out["content"] = new if role == "tool" else _TOOL_RESULT_PREFIX + new
        return out

    if role == "assistant":
        text = message.get("content") or ""
        if len(text) > _COMPRESS_CONTENT_HEAD:
            out["content"] = text[:_COMPRESS_CONTENT_HEAD] + "…"
        calls = message.get("tool_calls")
        if calls:
            trimmed = []
            for call in calls:
                copy = dict(call)
                function = dict(call.get("function") or {})
                args = function.get("arguments")
                if isinstance(args, str) and len(args) > _COMPRESS_ARGS_HEAD:
                    function["arguments"] = args[:_COMPRESS_ARGS_HEAD] + "…"
                copy["function"] = function
                trimmed.append(copy)
            out["tool_calls"] = trimmed
        return out

    return out


def _summarize(output: Any) -> str:
    """A short one-line summary of a tool result for the live event stream."""
    if isinstance(output, dict):
        status = output.get("status")
        if status:
            return str(status)
        return ", ".join(list(output)[:6])
    text = str(output)
    return text[:160]


# Tool ``status`` values that mean the tool did NOT produce a usable result. Anything
# else (``ok``, ``completed_lightweight_qc``, ``not_applicable``, …) counts as a real
# output — a denylist is more robust than an allowlist because each tool names its own
# success states, but they all share this small set of explicit failure states.
_FAILED_TOOL_STATUSES = frozenset(
    {"error", "failed", "timeout", "unavailable", "not_enabled", "dependency_missing", "cancelled"}
)


def step_succeeded(step: dict[str, Any]) -> bool:
    """True if a step ran a real (non-``finish``) tool that produced a usable result.

    Lets the lab accept a step that genuinely produced output even when the harness loop
    ended ``incomplete`` or without a textual ``final_answer`` (the artifact IS the result,
    not the prose). A tool that reported a failure status (``error``/``blocked_*``/
    ``dependency_missing``/…) does NOT count, even though the step is ``ok`` at the harness
    level (the failure was caught, not raised)."""
    if not step.get("ok") or step.get("tool") == "finish":
        return False
    result = step.get("result")
    if isinstance(result, dict):
        status = str(result.get("status", "ok")).lower()
        if status in _FAILED_TOOL_STATUSES or status.startswith(("error", "blocked", "fail")):
            return False
        return True
    return result is not None


def result_digest(value: Any, _depth: int = 0) -> Any:
    """A size-bounded, type-AGNOSTIC copy of a tool result for the Critic prompt: long
    strings are truncated, long lists capped, dicts recurse (keys kept). The Critic sees
    WHAT was produced (paths, counts, status) without a per-artifact field list and
    without dumping a huge payload into the prompt."""
    if isinstance(value, str):
        return value if len(value) <= 300 else value[:300] + "…"
    if isinstance(value, dict):
        if _depth >= 4:
            return "{…}"
        return {k: result_digest(v, _depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, (list, tuple)):
        if _depth >= 4:
            return "[…]"
        head = [result_digest(v, _depth + 1) for v in list(value)[:10]]
        if len(value) > 10:
            head.append(f"… (+{len(value) - 10} more)")
        return head
    return value


# Keys whose value is a LIST of concrete artifact paths (scrna_pack emits these).
_EVIDENCE_LIST_KEYS = frozenset({"figures", "tables", "artifacts", "outputs"})
# Intermediate report/render scaffolding — produced, but never a step's evidence.
_EVIDENCE_SKIP_KEYS = frozenset({"header_path", "lua_path", "dot_path", "src_path"})


def evidence_pointers(value: Any, _limit: int = 50) -> list[str]:
    """Deterministically collect the on-disk artifact paths a tool result points at — the
    concrete files (figures / tables / result files) THIS step actually wrote. Lets a
    Critic bind its verdict to WHAT was produced, not only the scientist's prose, and lets
    a claim with no backing artifact be spotted. Type-agnostic + bounded, mirroring
    ``result_digest``: recurses into nested dicts/lists (tools nest under ``result``), keys
    the repo-wide ``*_path`` convention plus the ``figures``/``tables`` lists, skips hidden
    scaffolding (dotfiles, report build helpers), and returns an order-preserving dedup.
    """
    out: list[str] = []

    def _add(p: Any) -> None:
        if p is None:
            return
        s = str(p).strip()
        if not s or s.lower() == "none" or os.path.basename(s).startswith("."):
            return
        if s not in out:
            out.append(s)

    def _walk(v: Any, depth: int) -> None:
        if depth >= 6 or len(out) >= _limit:
            return
        if isinstance(v, dict):
            for k, item in v.items():
                key = str(k)
                if key in _EVIDENCE_SKIP_KEYS:
                    continue
                if (key == "path" or key.endswith("_path")) and isinstance(item, (str, os.PathLike)):
                    _add(item)
                elif key in _EVIDENCE_LIST_KEYS and isinstance(item, (list, tuple)):
                    for x in item:
                        _add(x) if isinstance(x, (str, os.PathLike)) else _walk(x, depth + 1)
                else:
                    _walk(item, depth + 1)
        elif isinstance(v, (list, tuple)):
            for x in v:
                _walk(x, depth + 1)

    _walk(value, 0)
    return out
