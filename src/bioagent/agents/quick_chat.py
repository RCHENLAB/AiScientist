"""The **fast path**: an answer-first, streaming, tool-capable ReAct loop.

Why this exists
---------------
Until now the console had exactly ONE execution path. Every composer message —
including "what does a CADD score of 25 mean?" — went to ``POST /api/lab`` and ran the
full research lab: the PI builds an agenda, the plan is reviewed, steps execute, a report
is assembled. A one-line question paid a multi-minute pipeline, and the user stared at a
"Working…" spinner through all of the up-front planning before a single word appeared.

This module is the other path. It is deliberately NOT a smaller lab: there is no PI, no
agenda, no Critic, no report bundle. It is one loop:

    stream an answer  →  (if the model asked for a tool) run it  →  stream again

with the **first turn's tokens pushed to the client as they are generated**. That is the
whole point of "ReAct like Claude's": the opening sentence lands immediately and the
reasoning/tool work happens in the turns after it, rather than behind an opaque
planning phase. ``think`` is off by default for the same reason — a Qwen3 reasoning
trace front-loads seconds of tokens the reader cannot use yet.

Routing (fast path vs lab) is an EXPLICIT user choice, not a classifier — see
``LabRequest.route`` in the gateway and the design note in
``reports/2026-07-20/fast-chat-path-and-inline-mermaid.md``. A silent classifier that
misroutes a genuine study into a 4-turn chat loop would quietly produce a confident
answer with no analysis behind it, which is exactly the failure mode this codebase has
spent a lot of effort designing out.

Testability
-----------
Everything is injected: ``stream_fn`` (one streamed model turn), the tool list, the
event sink, and the cancel predicate. Nothing here imports the gateway, so the loop is
exercised offline against a scripted fake model — no GPU, no SSH, no ``paramiko``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .chat_context import (
    ChatContextLimits,
    CountTokensFn,
    SummarizeFn,
    build_chat_messages,
)

# The answer-first contract, stated to the model. The ordering instruction is load-bearing:
# without it Qwen opens with "Let me look that up…" and the streamed first sentence carries
# no information, which defeats the entire fast path.
QUICK_CHAT_SYSTEM = (
    "You are AiScientist, a bioinformatics research assistant, answering in a live chat.\n"
    "\n"
    "ANSWER FIRST. Your opening sentence must be a direct, substantive response to what was "
    "asked — never a preamble like 'Let me check' or 'I'll look into that'. Elaborate after it.\n"
    "\n"
    "You may call tools when you genuinely need external facts (literature, ontology lookups). "
    "Prefer answering from your own knowledge when you reliably can: a tool call costs the user "
    "seconds of waiting. When you do call a tool, say what you already know first, then call it.\n"
    "\n"
    "For a gene-disease association, a variant/phenotype link, or what the literature/evidence "
    "says, deep_literature (the indexed corpus) is the right tool — call it to ground the answer. "
    "If an earlier turn in THIS conversation already got that answer from deep_literature, reuse "
    "it instead of calling again. HARD RULE: if you did NOT call deep_literature this turn, do NOT "
    "say you retrieved, searched, or used the corpus, and do NOT emit paper citations or a "
    "references list — answer from general knowledge and state plainly it is not corpus-grounded.\n"
    "\n"
    "You are in CHAT mode, not analysis mode. You cannot run the analysis pipeline here — no VCF "
    "annotation, no clustering, no report. If the request actually needs a dataset analysed, say "
    "so plainly and tell the user to switch the composer to Research mode; do NOT improvise an "
    "answer that pretends an analysis happened.\n"
    "\n"
    "Never invent citations, accession numbers, gene-disease links, or statistics. If you are not "
    "sure, say you are not sure. And never claim you searched, looked up, retrieved, or found "
    "something unless you ACTUALLY called a tool this turn — answer from your own knowledge first, "
    "and only describe a lookup that really happened.\n"
    "\n"
    "Keep chat answers CONCISE — this is a live QA, not a manuscript. When a literature tool "
    "(deep_literature) returns evidence, answer the question directly in a few short paragraphs "
    "with inline citations, then a brief numbered reference list. Do NOT wrap it as a formal "
    "report: no 'Final Research Report' heading and no Methods / Results / Discussion sections.\n"
    "\n"
    "When a diagram genuinely helps explain a workflow, mechanism, or decision path, emit a "
    "```mermaid fenced block (flowchart TD / sequenceDiagram / graph LR). It renders inline in "
    "the chat. Keep it small — under ~12 nodes — and use it to clarify structure, not to decorate. "
    "Write every node label as plain, single-line text: no line breaks, no literal \\n, and no HTML "
    "tags (<br>, <b>, <i>) inside a label. The renderer draws labels as plain text for safety, so "
    "those show up as literal characters or break the diagram. Keep each label short and avoid "
    "punctuation-heavy text — parentheses and slashes inside a label can fail to parse."
)


#some limatations on the speed
@dataclass
class QuickChatConfig(ChatContextLimits):
    """Bounds. The fast path must stay fast: an unbounded ReAct loop is just the lab again,
    minus the safeguards. ``max_turns`` counts MODEL turns (the first answer included), so the
    default allows one answer, up to two tool rounds, and a final synthesis.

    The CONTEXT knobs (``max_prompt_tokens``, ``max_model_len``, ``output_reserve_tokens``,
    ``keep_last_exchanges``, ``max_history_messages``, ``summary_*``) are inherited from
    :class:`~bioagent.agents.chat_context.ChatContextLimits`, which is where they are
    documented — chat targets a BOUNDED 24K prompt rather than the whole served window,
    because prefill time is what the fast path is buying down."""

    max_turns: int = 4
    max_tool_calls: int = 6          # total across the whole exchange
    max_result_chars: int = 4000     # per tool result fed back into the context
    verify_citations: bool = False   # tier-3: re-read sources & append a caution footer for
    #                                  inheritance claims the evidence does not support (off
    #                                  until validated live — needs one extra model call)

#package the result for frontend to get
@dataclass
class QuickChatResult:
    text: str = ""
    thinking: str = ""
    turns: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stopped: bool = False            # cancelled cooperatively (Stop button)
    hit_turn_limit: bool = False
    # Context accounting for THIS turn — what the console's occupancy indicator shows, and
    # (summary/summary_through) the rolling memory the caller hands back on the next turn so
    # re-summarization stays incremental. See agents/chat_context.py.
    context_used: int = 0
    context_allowed: int = 0
    context_exact: bool = False      # True when counted by the served model's own tokenizer
    context_compacted: bool = False  # True when older turns were summarized and/or dropped
    summary: str = ""
    summary_through: int = 0
    citation_check: str = ""         # tier-3 citation-check footer appended to text, if any


def _parse_args(raw: Any) -> dict[str, Any]:
    """OpenAI streams ``function.arguments`` as a JSON *string*; some servers hand back an
    object. Accept both, and treat unparseable arguments as empty rather than raising — a
    malformed tool call should degrade to a tool error the model can read and recover from,
    not kill the user's chat turn."""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _summarize(payload: dict[str, Any], limit: int = 160) -> str:
    """A one-line human summary of a tool result for the activity feed."""
    if not isinstance(payload, dict):
        return str(payload)[:limit]
    for key in ("summary", "note", "error", "status"):
        if payload.get(key):
            return str(payload[key])[:limit]
    return json.dumps(payload)[:limit]


_LIT_KEYWORDS = (
    "literature", "evidence", "paper", "publication", "study", "studies", "cite", "citation",
    "reference", "gene", "genes", "mutation", "variant", "allele", "pathogenic", "phenotype",
    "genotype", "association", "associated", "cause", "causes", "causing", "link", "linked",
    "disease", "syndrome", "dystrophy", "atrophy", "retinitis", "retinopathy", "macular",
    "photoreceptor", "inherit", "mechanism", "clinical", "prognosis", "onset",
)


def _is_literature_question(question: str) -> bool:
    """Is this a gene/disease/evidence question that MUST be grounded in the corpus?

    Deliberately inclusive for this ocular-genetics domain (most substantive questions qualify),
    but skips greetings / meta / tooling chatter so trivial turns stay instant. Any gene-symbol-like
    token (CRB1, ABCA4, RPE65, IRD, RP) or literature/clinical keyword flips it on."""
    q = (question or "").lower()
    if any(k in q for k in _LIT_KEYWORDS):
        return True
    return bool(re.search(r"\b[A-Z][A-Z0-9]{2,}\b", question or ""))


def _schema_name(schema: "Any") -> str:
    fn = (schema.get("function") or {}) if isinstance(schema, dict) else {}
    return str(fn.get("name") or (schema.get("name") if isinstance(schema, dict) else "") or "")


def _clean_text(s: str) -> str:
    """Strip HTML tags (corpus citation titles carry <i>/<b>) and collapse whitespace to one line."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(s))).strip()


#(1) make the paperqa drafts clean (2)pack every line to paper name and evidence(formatting the paper)
def _format_grounding(payload: dict) -> str:
    """Render deep_literature's REAL result as the evidence the model must answer from."""
    if not isinstance(payload, dict):
        return "deep_literature returned nothing usable."
    status = str(payload.get("status") or "")
    contexts = payload.get("contexts") or []
    answer = payload.get("formatted_answer") or payload.get("answer") or ""
    if status not in ("", "ok") and not contexts and not answer:
        note = payload.get("note") or payload.get("error") or status
        return f"deep_literature returned NO relevant papers ({note}). The corpus has nothing on this."
    parts = []
    if answer:
        # [1](#ref1) -> [1] (the bubble renders raw #ref anchors as noise) and strip the <i>/<b>
        # HTML the corpus citation titles carry, so neither leaks into the answer.
        ans = re.sub(r"\]\(#[^)]*\)", "]", str(answer))
        ans = re.sub(r"<[^>]+>", "", ans).strip()
        # Drop PaperQA's OWN inline [n] markers: they use PaperQA's source ordering, which differs
        # from the "Sources" numbering built below from contexts. Handing the model BOTH numberings
        # is what makes its inline [n] and final References list disagree (drifted/duplicated/dangling
        # numbers). Strip them so the model cites by exactly ONE numbering — the Sources list below.
        from ..tools.literature_references import strip_intext_citation_markers
        ans = strip_intext_citation_markers(ans)
        parts.append(ans)
    cites, seen = [], set()
    for c in contexts:
        if not isinstance(c, dict):
            continue
        cit = _clean_text(c.get("citation") or "")
        key = cit.lower()
        if cit and key not in seen:
            seen.add(key)
            snippet = _clean_text(c.get("summary") or "")
            if len(snippet) > 500:
                snippet = snippet[:497].rstrip() + "\u2026"
            cites.append((cit, snippet))
    if cites:
        lines = [f"{i}. {cit}" + (f"\n   Evidence: {snip}" if snip else "")
                 for i, (cit, snip) in enumerate(cites, 1)]
        parts.append(
            "Sources \u2014 cite ONLY these (one number each, no repeats). Attach a source to a "
            "gene's inheritance mode ONLY if that source's Evidence text actually states it; if no "
            "Evidence states a claim, do not make the claim:\n" + "\n".join(lines))
    return "\n\n".join(parts) if parts else "deep_literature returned no relevant papers for this question."

#tell qwen how to write properly with the formatted answer
def _grounding_note(payload: dict) -> str:
    return (
        "deep_literature (the indexed corpus) was just run for the question above. Answer using ONLY "
        "the evidence below. Cite inline as plain bracketed numbers — [1] or [1, 2] — with NO markdown "
        "links and NO #ref anchors, then end with a plain numbered \"References:\" list giving each "
        "unique source ONCE in full — never list the same paper under two numbers, and no HTML "
        "tags in titles. If the evidence says no relevant papers were found, tell the user the corpus "
        "has nothing on this rather than answering from memory. Keep it concise: a direct answer plus "
        "the references, not a formal report.\n\n"
        "=== deep_literature result ===\n" + _format_grounding(payload)
    )

#this is the part of checker,which ask ai to see whether the chunks and the citations have noise
def _verify_evidence_block(payload: dict) -> str:
    """Numbered evidence for the citation verifier, in the SAME dedup order as the Sources list in
    _format_grounding, so a source's number here matches the number the model cited. Prefers the
    per-context evidence snippet (what actually grounds a claim), falling back to the citation."""
    if not isinstance(payload, dict):
        return ""
    lines, seen = [], set()
    for c in payload.get("contexts") or []:
        if not isinstance(c, dict):
            continue
        cit = _clean_text(c.get("citation") or "")
        key = cit.lower()
        if not cit or key in seen:
            continue
        seen.add(key)
        snippet = _clean_text(c.get("summary") or "")
        lines.append(f"{len(lines) + 1}. {cit}\n   Evidence: {snippet or '(no snippet)'}")
    return "\n".join(lines)


def _parse_verifier_json(text: str) -> list[dict]:
    """Tolerantly pull a JSON array of flagged claims out of the verifier reply. Never raises;
    returns [] on anything unparseable, so a flaky verifier can only ADD a footer, never crash."""
    import json
    s = str(text or "")
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        data = json.loads(s[i:j + 1])
    except Exception:
        return []
    return [d for d in data if isinstance(d, dict) and d.get("gene")] if isinstance(data, list) else []


def _claim_unsupported(d: dict) -> bool:
    """True only when the verifier CLEARLY marks a claim as not supported. Defaults to NOT flagging
    on a missing/ambiguous verdict, so a supported claim is never listed under the caution header
    (the failure we must avoid). Accepts a boolean ``supported`` or a string verdict."""
    v = d.get("supported")
    if isinstance(v, bool):
        return v is False
    token = str(d.get("supported") if v is not None else (d.get("verdict") or d.get("support") or "")).strip().lower()
    return token in {"false", "no", "0", "unsupported", "not_supported", "not supported", "unclear"}


_VERIFY_SYSTEM = (
    "You are a strict citation checker. You are given the user's QUESTION, the answer, and the "
    "numbered source evidence the answer was built from. For EACH gene the answer associates with "
    "the question\u2019s topic and backs with a citation, identify the SPECIFIC claim the answer makes "
    "about that gene (e.g. 'associated with macular atrophy', 'has both AD and AR forms', 'causes "
    "electronegative ERG') and judge whether the cited source\u2019s Evidence text DIRECTLY states "
    "THAT claim. Check the claim the answer actually makes \u2014 do NOT invent a different property "
    "(e.g. do not demand an inheritance mode the answer never asserted). A source that only lists the "
    "gene without stating the claimed relationship, is about a DIFFERENT gene, only questions/refutes "
    "the association, or never names the gene, does NOT support it. CRITICAL: the gene\u2019s OWN name "
    "(or a well-known synonym, e.g. RHO/rhodopsin, BEST1/VMD2) must actually appear in the cited "
    "Evidence stating that claim; if it does not, mark it NOT supported \u2014 do not assume a review or "
    "cohort list covers it. When the claim is that a gene has BOTH dominant and recessive (AD and AR) forms, a GENUINELY dominant form is required: pseudo-dominance, recessive variants that only mimic dominant transmission, or an AD report the source itself calls misclassified, questioned, or reclassified as recessive do NOT count as a true dominant form — mark such a gene NOT supported. Reply with ONLY a JSON array (no prose, no markdown), one object per "
    "checked claim: {\"gene\":\"SYM\",\"claim\":\"<=8 words\",\"supported\":true or false,"
    "\"reason\":\"<=12 words\"}. Set supported=false whenever the Evidence does not directly state the claim."
)

# 1. Build the numbered evidence block; bail out if there's no evidence or answer.
# 2. Assemble the verifier prompt: question + answer + numbered evidence.
# 3. Run ONE non-streaming Qwen pass with the strict-checker system prompt.
# 4. Parse the JSON verdicts, keeping only claims clearly marked unsupported.
# 5. Return a Markdown caution footer for the flagged claims (or '' if none / on any error).
def _verify_citations(stream_fn, question: str, answer: str, payload: dict) -> str:
    """Second-pass citation check (the 're-read the source' step). Runs ONE non-streaming model
    call over (answer, numbered evidence) and returns a short Markdown footer flagging claims the
    evidence does not directly support — or '' when everything checks out or the check cannot run.
    Non-destructive: the answer already streamed to the client, so we APPEND a note rather than
    editing it. Never raises — on any failure it returns '' and the answer is unchanged."""
    try:
        evidence = _verify_evidence_block(payload)
        if not evidence or not (answer or "").strip():
            return ""
        user = (
            f"Question:\n{question}\n\nAnswer to check:\n{answer}\n\n"
            f"Numbered source evidence:\n{evidence}\n\n"
            "For each cited gene claim in the answer, judge it against the evidence as instructed. "
            "Return the JSON array now."
        )
        collected = []
        for kind, chunk in stream_fn(
            [{"role": "system", "content": _VERIFY_SYSTEM},
             {"role": "user", "content": user}], []):
            if kind == "content":
                collected.append(chunk)
        rows = []
        for d in _parse_verifier_json("".join(collected)):
            gene = _clean_text(str(d.get("gene") or "")).strip()
            if not gene or not _claim_unsupported(d):
                continue
            detail = _clean_text(str(d.get("claim") or d.get("mode") or "")).strip()
            reason = _clean_text(str(d.get("reason") or "")).strip()
            if len(reason) > 160:
                reason = reason[:157].rstrip() + "\u2026"
            tag = f"{gene} ({detail})" if detail else gene
            rows.append(f"- **{tag}** — {reason or 'the cited source does not directly state this.'}")
        if not rows:
            return ""
        return ("\n\n---\n**Citation check (automated):** the statements below are not directly "
                "supported by their cited source and should be treated with caution:\n\n"
                + "\n".join(rows))
    except Exception:
        return ""

#run the main function
def run_quick_chat(
    *,
    stream_fn: Callable[..., Iterator[tuple[str, Any]]],
    tools: list[Any],
    question: str,
    history: list[dict[str, str]] | None = None,
    context: Any = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    config: QuickChatConfig | None = None,
    should_cancel: Callable[[], bool] | None = None,
    system: str = QUICK_CHAT_SYSTEM,
    count_tokens_fn: CountTokensFn | None = None,
    summarize_fn: SummarizeFn | None = None,
    prior_summary: str = "",
    prior_summary_through: int = 0,
) -> QuickChatResult:
    """Run the answer-first ReAct loop and return what was produced.

    ``stream_fn(messages, tool_schemas)`` yields ``("thinking"|"content", str)`` pairs plus at
    most one ``("tool_calls", [call, ...])`` — the contract of
    ``gateway.vllm_client.chat_tools_stream``. ``tools`` are ``HarnessTool``s (duck-typed here:
    anything with ``.name`` / ``.schema()`` / ``.executor``), executed with ``context``.

    ``on_event`` receives, in order: ``context_measured`` (+ ``context_trimmed`` when the
    conversation had to be compacted), then ``content``/``thinking`` token events (push these
    to the client the moment they arrive — that is the latency win), then ``tool_start`` /
    ``tool_result`` / ``tool_error``. It must not raise.

    ``should_cancel`` is polled between tokens and between turns, so Stop lands mid-answer.

    Context management (``count_tokens_fn`` / ``summarize_fn`` / ``prior_summary*``) is
    INJECTED for the same reason ``stream_fn`` is: this module must import cleanly without the
    gateway. With neither injected the loop still runs — occupancy is estimated from characters
    and compaction degrades to dropping the oldest turns. See ``agents/chat_context.py``.
    """
    cfg = config or QuickChatConfig()
    emit = on_event or (lambda _ev: None)
    cancelled = should_cancel or (lambda: False)
    by_name = {t.name: t for t in tools}
    schemas = [t.schema() for t in tools]

    # Context awareness + compaction: system prompt + rolling memory of older turns + the last
    # N exchanges verbatim + the question, fitted to a BOUNDED prompt budget (not the whole
    # served window — prefill latency is what this path is buying down).
    ctx_report = build_chat_messages(
        system=system, question=question, history=history, tool_schemas=schemas, config=cfg,
        prior_summary=prior_summary, prior_summary_through=prior_summary_through,
        count_tokens_fn=count_tokens_fn, summarize_fn=summarize_fn, on_event=emit,
    )
    messages: list[dict[str, Any]] = ctx_report.messages
    grounding_payload: dict[str, Any] | None = None

    result = QuickChatResult(
        context_used=ctx_report.used_tokens, context_allowed=ctx_report.allowed_tokens,
        context_exact=ctx_report.exact, context_compacted=ctx_report.compacted,
        summary=ctx_report.summary, summary_through=ctx_report.summary_through,
    )

    # FORCED GROUNDING. For a gene/disease/evidence question, run deep_literature UP FRONT —
    # deterministically, not at the model's discretion — and inject its REAL result. The model then
    # synthesises from actual corpus evidence and CANNOT fabricate a "retrieved via corpus" answer
    # (the failure mode when it silently skips the tool). Skipped when deep_literature is absent (HPC
    # down -> executor is None -> tool not in the catalog), so chat still degrades to a plain answer.
    # deep_literature is dropped from the schemas afterwards so the model cannot fire the (minutes-
    # long) job a second time.
    lit_tool = by_name.get("deep_literature")
    if lit_tool is not None and not cancelled() and _is_literature_question(question):
        emit({"type": "tool_start", "tool": "deep_literature", "args": {"question": question}})
        try:
            forced = lit_tool.executor({"question": question}, context)
            if not isinstance(forced, dict):
                forced = {"status": "ok", "result": forced}
            emit({"type": "tool_result", "tool": "deep_literature", "summary": _summarize(forced)})
        except Exception as exc:  # noqa: BLE001 - a grounding failure is data for the model, not the
            forced = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}   # end of the turn
            emit({"type": "tool_error", "tool": "deep_literature", "error": forced["error"]})
        result.tool_calls.append({"tool": "deep_literature", "args": {"question": question}})
        messages.append({"role": "user", "content": _grounding_note(forced)})
        grounding_payload = forced
        schemas = [sc for sc in schemas if _schema_name(sc) != "deep_literature"]

    for turn in range(cfg.max_turns):
        if cancelled():
            result.stopped = True
            return result
        result.turns = turn + 1
        turn_text, turn_thinking, calls = "", "", []
        for kind, chunk in stream_fn(messages, schemas):
            if cancelled():
                result.stopped = True
                return result
            if kind == "content":
                turn_text += chunk
                result.text += chunk
                emit({"type": "content", "token": chunk})
            elif kind == "thinking":
                turn_thinking += chunk
                result.thinking += chunk
                emit({"type": "thinking", "token": chunk})
            elif kind == "tool_calls":
                calls = list(chunk or [])

        # Only the LAST turn's text is the answer the user keeps; earlier turns' prose is the
        # model narrating before a tool call ("PDE6B is a rod phosphodiesterase subunit — let me
        # confirm the disease association"). Keep it in the transcript (the client already
        # streamed it) but record it in the message history so the model does not repeat itself.
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": turn_text}
        if calls:
            assistant_msg["tool_calls"] = calls
        messages.append(assistant_msg)

        if not calls:
            # Final answer produced. Optional tier-3 citation check: re-read the sources and
            # append a caution footer for any inheritance claim the evidence does not directly
            # support. Non-destructive (the answer already streamed); off unless cfg enables it.
            if cfg.verify_citations and grounding_payload is not None and result.text.strip():
                footer = _verify_citations(stream_fn, question, result.text, grounding_payload)
                if footer:
                    result.citation_check = footer
                    result.text += footer
                    emit({"type": "content", "token": footer})
            return result           # the model answered and asked for nothing — done

        budget_left = cfg.max_tool_calls - len(result.tool_calls)
        if budget_left <= 0:
            # Out of tool budget: tell the model in-band so it wraps up with what it has,
            # rather than silently dropping its request and looking like it ignored the user.
            messages.append({"role": "user", "content":
                             "Tool budget exhausted. Answer now with what you already have, "
                             "and say plainly what you could not verify."})
            continue

        for call in calls[:budget_left]:
            fn = (call.get("function") or {}) if isinstance(call, dict) else {}
            name = str(fn.get("name") or "")
            args = _parse_args(fn.get("arguments"))
            call_id = call.get("id") if isinstance(call, dict) else None
            result.tool_calls.append({"tool": name, "args": args})
            emit({"type": "tool_start", "tool": name, "args": args})
            tool = by_name.get(name)
            if tool is None:
                payload: dict[str, Any] = {
                    "status": "error",
                    "error": f"unknown tool {name!r}; available: {sorted(by_name)}",
                }
                emit({"type": "tool_error", "tool": name, "error": payload["error"]})
            else:
                try:
                    payload = tool.executor(args, context)
                    if not isinstance(payload, dict):
                        payload = {"status": "ok", "result": payload}
                    emit({"type": "tool_result", "tool": name, "summary": _summarize(payload)})
                except Exception as exc:  # noqa: BLE001 - a tool failure is data for the model,
                    # never the end of the user's chat turn. The model reads the error and either
                    # retries differently or answers without it.
                    payload = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                    emit({"type": "tool_error", "tool": name, "error": payload["error"]})
            _feed_result(messages, call_id, payload, cfg.max_result_chars)

    result.hit_turn_limit = True
    return result


def _feed_result(messages: list[dict[str, Any]], call_id: str | None,
                 payload: dict[str, Any], limit: int) -> None:
    """Append a tool result in the shape the server expects. A NATIVE tool call requires an
    OpenAI ``role:tool`` message carrying the matching ``tool_call_id`` — strict
    OpenAI-compatible servers (vLLM, OpenRouter) 400 without it. Mirrors
    ``ResearchHarness._feed_result``; the fallback keeps a missing id from breaking the turn."""
    text = json.dumps(payload)[:limit]
    if call_id:
        messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
    else:
        messages.append({"role": "user", "content": f"Tool result: {text}"})
