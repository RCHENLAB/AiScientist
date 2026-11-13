"""Reading the model's and the user's plan text: agendas, verdict JSON, plan replies, plan diffs."""

from __future__ import annotations

import json
import re
from typing import Any
from ..dag import TaskNode
from ..loop_utils import safe_json_loads
from ..plan_doc import split_step


def _node_step_text(node: TaskNode) -> str:
    """The scoped brief for one DAG node: the goal PLUS explicit reuse/produce hints so the Scientist
    does ONLY this task and reuses upstream checkpoints instead of recomputing them (the structural
    cure for the 'step 1 runs the whole pipeline' / double-QC failure modes)."""
    parts = [node.goal]
    if node.consumes:
        parts.append("Reuse these existing inputs/checkpoints as-is (do NOT recompute them): "
                     + ", ".join(node.consumes) + ".")
    if node.produces:
        parts.append("This task is expected to produce: " + ", ".join(node.produces) + ".")
    if node.suggested_tool:
        parts.append(f"Suggested tool: {node.suggested_tool} (use it if it fits the task).")
    parts.append("Do ONLY this task — earlier tasks already ran and their outputs exist; do not "
                 "repeat or re-run them.")
    return " ".join(parts)


# A step's own ordinal, echoed back into its text. The plan reaches the PI NUMBERED
# ("1. **QC & normalization** — …") on the patch path and in the Critic's read-back, and the model
# copies that "1. " prefix into the step it hands back. Nothing stripped it, so the stored step
# became "1. **QC …** — …" while its neighbours stayed "**Title** — …" — and the console's step
# renderer, which reads a step's title off a LEADING ``**bold**``, fell through to its untitled
# branch: the reviewer got a bare "1" heading followed by raw markdown, for exactly the step they
# had just asked to change. Measured in Ziyao's plan_mode_report_v2_5 as 6/6 on the in-place
# revision path and 0/4 on add/delete, which never round-trips a numbered plan. Stripped at the
# SOURCE so every consumer — the plan card, the progress feed, plan.md, and the agenda the
# Scientist executes — sees ONE step shape.
_STEP_ORDINAL_RE = re.compile(r"^\(?\d{1,2}[.)、]\s+")


def _strip_step_ordinal(text: str) -> str:
    """Drop a leading numeric list marker ("3. ", "3) ", "(3) ") from one step's text.

    Deliberately narrow. A separator AND a following space are both required, so prose that opens
    with a number keeps it — "0.25 is the single-cell convention" and "2,000 highly-variable genes"
    both survive — and a prefix that is real CONTENT rather than a list marker ("Step 3: …") is left
    for the model to own."""
    return _STEP_ORDINAL_RE.sub("", (text or "").strip(), count=1).strip()


def _parse_agenda(raw: str, max_steps: int) -> list[str] | None:
    """Parse the PI's step list from a JSON array (tolerating code fences, a
    surrounding sentence, or a ``{"steps": [...]}`` / ``{"agenda": [...]}`` object)."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].strip()
    parsed: Any = None
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", s, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
    if isinstance(parsed, dict):
        parsed = parsed.get("steps") or parsed.get("agenda")
    if isinstance(parsed, list) and parsed:
        steps = [s for s in (_strip_step_ordinal(str(x)) for x in parsed) if s]
        return steps[:max_steps] or None
    return None


# The PI's disclosure of what it decided on its OWN knowledge rather than from the research-path
# guidance or the dataset profile. The draft prompt asks for it as the reply's `self_sourced` field
# (`_parse_self_sourced`). It used to be the agenda's last string, prefixed `SELF-SOURCED:`; with
# `later` after `agenda`, models wrote it after `later`, outside the array, and the reply was not
# JSON (prod run 74873590ca7b on Haiku 5.5, and DSV4 on HPC3). The prefixed string is still split
# off wherever it appears, for replies in the old form.
_SELF_SOURCED_PREFIX = "SELF-SOURCED:"


def _parse_self_sourced(raw: str) -> str:
    """The ``self_sourced`` field of the PI's planning reply, ``''`` when it has none. A list of
    strings is joined, and a ``SELF-SOURCED:`` prefix the model repeated inside it is dropped."""
    obj = _parse_verdict(raw or "")
    value = obj.get("self_sourced") if isinstance(obj, dict) else None
    if isinstance(value, list):
        value = " ".join(v for v in value if isinstance(v, str))
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    bare = text.lstrip("*-# ").strip()
    if bare.upper().startswith(_SELF_SOURCED_PREFIX):
        text = bare[len(_SELF_SOURCED_PREFIX):].lstrip("* ").strip()
    if text.startswith("<") and text.endswith(">"):
        return ""            # the prompt's placeholder, copied instead of filled in
    return text


# Telling a QUESTION about the plan from a REQUEST to change it. Plan mode had three actions —
# approve / revise / cancel — so anything a reviewer typed became "revise": asking "why is step 3 a
# Wilcoxon test?" did not get an answer, it triggered a re-plan. And a whole-plan redraft damages
# the steps nobody mentioned (measured: 100% of trials silently lost an unmentioned step), so the
# cost of asking a question was losing part of the plan.
#
# Deterministic first, and biased toward QUESTION on purpose. The two errors are not symmetric:
# reading a change request as a question wastes one exchange, while reading a question as a change
# request rewrites work the reviewer never asked to touch.
_QUESTION_MARKS = ("?", "？")
# English fronts its question words, so matching at the START is right and avoids catching
# "use wilcoxon, and tell me what it does".
_QUESTION_OPENERS = (
    "why", "what", "what's", "whats", "how", "how's", "which", "who", "where", "when",
    "is ", "are ", "does ", "do ", "did ", "can ", "could ", "should ", "will ", "would ",
    "explain", "tell me",
)
# Chinese does not: the question word sits mid-sentence ("第3步为什么用 Wilcoxon"), and a
# startswith rule sent exactly that reply down the re-plan path.
_QUESTION_ANYWHERE = (
    "为什么", "为啥", "怎么", "如何", "哪个", "哪些", "是不是", "能不能", "会不会",
    "有没有", "多少", "解释", "说明一下", "讲一下", "什么意思", "是什么", "干什么", "做什么",
    "到底", "了什么", "过什么", "算什么", "指什么",
)
_CHANGE_VERBS = (
    "change", "use ", "add ", "remove", "delete", "drop ", "replace", "instead", "rerun",
    "re-run", "swap", "set ", "make it", "don't", "do not", "skip ", "merge", "split",
    "改", "换", "加上", "加一", "删", "去掉", "不要", "改成", "换成", "增加", "移除", "跳过",
)
# The change verbs are checked first, which is right for "could you use 0.5 instead?" and wrong for
# "why do you use wilcoxon here?" — the same verb, one a request and one a question ABOUT the
# request's subject. These openers cannot front a request in either language, so they settle it
# before the verb scan: asking why a step removes cells must not be read as "remove that step".
# The modal openers ("could you", "can you", "should we") deliberately stay behind the verb scan,
# because those DO front polite requests.
_PURE_INTERROGATIVES = (
    "why", "what", "what's", "whats", "how", "how's", "which", "who", "where", "when",
    "explain", "tell me",
)
# ...except when the interrogative fronts a PROPOSAL rather than a question.
_PROPOSAL_OPENERS = ("how about", "what about")
_PURE_INTERROGATIVES_ZH = ("为什么", "为啥")


def classify_plan_reply(text: str) -> str:
    """``"question"`` or ``"change"`` for a reviewer's reply in plan mode.

    Pure and deterministic so the common cases never cost a model call, and so the rule is
    testable. A reply carrying a change verb is a change even if it is phrased as a question
    ("could you use 0.5 instead?"); everything else that looks interrogative is a question;
    anything left over is a change, since a bare imperative ("stratify by majorclass") is the
    normal way to ask for one."""
    t = (text or "").strip()
    if not t:
        return "change"
    low = t.lower()
    if ((low.startswith(_PURE_INTERROGATIVES) and not low.startswith(_PROPOSAL_OPENERS))
            or any(z in t for z in _PURE_INTERROGATIVES_ZH)):
        return "question"
    if low.startswith(_PROPOSAL_OPENERS):
        return "change"      # "how about 0.5" proposes one; it does not ask about one
    if any(v in low for v in _CHANGE_VERBS):
        return "change"
    if (t.endswith(_QUESTION_MARKS) or low.startswith(_QUESTION_OPENERS)
            or any(m in t for m in _QUESTION_ANYWHERE)):
        return "question"
    return "change"


# The prompt's "nothing to disclose" sentence, written into the agenda without the prefix: "none —
# every choice in this plan comes from the guidance and the dataset profile." It ran nothing and named
# no tool, so a scope limit dropped it as a step, and the feed said "Left out 1 step(s) your request
# ruled out: none" (RCHENLAB/AiScientist issue 75, the #56 re-test's A2). "None of the …" stays a step.
_BARE_DISCLOSURE_RE = re.compile(
    r"^none\b\s*(?:[—–:;.,-]|$)|^every choice in this plan comes from the guidance", re.IGNORECASE)


def _split_self_sourced(steps: "list[str]") -> "tuple[list[str], str]":
    """Separate the ``SELF-SOURCED:`` disclosure from the executable agenda.

    Returns ``(agenda, disclosure_text)``. The disclosure is a statement about the plan, not work
    to run — left in the agenda it would be dispatched to the Scientist as a step. Empty string
    when the PI made no disclosure. Tolerates the prefix appearing on any line, since models put it
    first about as often as last, and the prompt's "none — every choice …" sentence put in the
    agenda without it."""
    agenda: list[str] = []
    notes: list[str] = []
    for s in steps:
        t = (s or "").strip().lstrip("*-# ").strip()
        bare = re.sub(r"[*`]", "", t).strip()
        if t.upper().startswith(_SELF_SOURCED_PREFIX):
            body = t[len(_SELF_SOURCED_PREFIX):].strip()
            if body:
                notes.append(body)
        elif _BARE_DISCLOSURE_RE.match(bare):
            notes.append(bare)
        else:
            agenda.append(s)
    return agenda, " ".join(notes).strip()


def _drop_question_echo(steps: "list[str]", question: str) -> "tuple[list[str], list[str]]":
    """Remove agenda items that are just the research question (or a trivial restatement of it)
    echoed back as a "step". Seen in production (run 97dfc89dc5aa): the PI's reply led with the
    question as item 1; it parsed as a step, could never be executed, failed three rounds and
    raised a decision card. Deterministic: an item is an echo when, after stripping markdown and
    punctuation, it equals the question or is a near-copy (>=90% of its words are the question's
    words and it adds none of its own beyond three). Returns (kept, dropped)."""
    import string

    def _norm_words(text: str) -> list[str]:
        t = re.sub(r"[*_`#]", " ", text.lower())
        t = t.translate(str.maketrans("", "", string.punctuation))
        return t.split()

    qw = _norm_words(question)
    qset = set(qw)
    kept: list[str] = []
    dropped: list[str] = []
    for step in steps:
        w = _norm_words(step)
        if not w:
            dropped.append(step)
            continue
        wset = set(w)
        covers_question = sum(1 for x in qset if x in wset) >= 0.9 * max(1, len(qset))
        extra = sum(1 for x in w if x not in qset)
        # An echo RESTATES the question: it contains essentially the whole question and adds
        # almost nothing. A short legitimate step whose words happen to appear in the question
        # ("Annotate the cells") covers only part of it and is kept.
        if w == qw or (covers_question and extra <= 3 and len(w) <= len(qw) + 3):
            dropped.append(step)
            continue
        kept.append(step)
    # Never empty the agenda: a plan that was ONLY an echo falls back to the raw list.
    return (kept, dropped) if kept else (steps, [])


def _parse_plan(raw: str, max_steps: int, allow_clarify: bool) -> tuple[str, Any] | None:
    """Parse the PI's planning reply into ``("agenda", [steps])`` or, when ``allow_clarify``
    and the PI asked, ``("clarify", [{"question", "options"}])``. Returns ``None`` if neither
    can be recovered (the caller falls back to a trivial agenda)."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].strip()
    parsed: Any = None
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL) or re.search(r"\[.*\]", s, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None

    if allow_clarify and isinstance(parsed, dict) and isinstance(parsed.get("clarify"), list):
        questions = []
        for q in parsed["clarify"][:3]:
            if not isinstance(q, dict):
                continue
            text = str(q.get("question", "")).strip()
            options = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()][:4]
            if text and options:
                questions.append({"question": text, "options": options})
        if questions:
            return ("clarify", questions)

    agenda = _parse_agenda(raw, max_steps)
    if agenda:
        return ("agenda", agenda)
    return None


#: How many "ask for this next" suggestions a plan may carry. They are shown to the user, not run.
MAX_LATER = 4
#: How long a proposed step waits for the user's answer. No answer is a no.
NEW_STEP_ASK_SECONDS = 300
# What counts as "yes, add it" when the user types instead of clicking. Anything else is a no.
_APPROVAL_WORDS = ("add", "yes", "approve", "ok", "sure", "加", "可以", "同意", "批准", "好", "行")


def _step_title(step: str) -> str:
    """``**QC & normalization** — Measure…`` → ``QC & normalization``."""
    text = str(step or "").strip()
    return (text.split(" — ")[0].replace("**", "").strip() or text)[:90]


def _parse_plan_extras(raw: str) -> "tuple[str, list[str]]":
    """``(breadth, later)`` from the PI's planning reply: how much the request asked for
    (``"whole"`` / ``"specific"``, ``""`` when the reply does not say) and the analyses it left
    out because nobody asked for them. Both are optional, so a reply that is only
    ``{"agenda": [...]}`` — or not an object at all — parses exactly as it did before."""
    obj = _parse_verdict(raw or "")
    if not isinstance(obj, dict):
        return "", []
    breadth = str(obj.get("breadth") or "").strip().lower()
    breadth = breadth if breadth in ("whole", "specific") else ""
    later: list[str] = []
    items = obj.get("later")
    for item in (items if isinstance(items, list) else []):
        text = " ".join(str(item or "").split())
        if text and text.lower() not in {x.lower() for x in later}:
            later.append(text[:240])
        if len(later) >= MAX_LATER:
            break
    return breadth, later


def _parse_verdict(raw: str) -> dict[str, Any] | None:
    """Parse the Critic's JSON object, tolerating a chatty/thinking model that wraps
    the ``{...}`` in prose or code fences (safe_json_loads needs the whole string to
    be JSON; this extracts the first object block as a fallback)."""
    parsed = safe_json_loads(raw)
    if parsed is None:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
    if parsed is None:
        parsed = _close_unterminated_json(raw)
    return parsed if isinstance(parsed, dict) else None


def _close_unterminated_json(raw: str) -> "dict[str, Any] | None":
    """A JSON object whose only fault is missing closing brackets — the model stopped after the
    last complete value. Measured on the plan review of an 18-step DDX41 plan: the Critic's ~15k-
    character reply (issues + a verbatim revised agenda) ended ``…"]`` without the final ``}``, the
    parse failed, and the review fell through as "no issues" with five correct ones in the text.

    Only that case is repaired: the text must open with ``{`` and must NOT end inside a string. A
    reply cut mid-string is left unparsed — closing it would invent the end of a step's text."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:].strip() if text.lower().startswith("json") else text
    start = text.find("{")
    if start < 0:
        return None
    text = text[start:]
    stack: list[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack.pop() != ch:
                return None
    if in_string or not stack:
        return None
    try:
        parsed = json.loads(text.rstrip().rstrip(",") + "".join(reversed(stack)))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _norm_step(step: str) -> str:
    """Bag-of-words key for "is this the same step?". Used to stop exploration re-adding a step the
    plan already has under slightly different wording — the model's most common failure mode."""
    return " ".join(re.findall(r"[a-z0-9]+", (step or "").lower()))


# Input carrying no change request. "hmm" was sent to the PI as a revision and came back with the
# ORA step's padj quietly moved from 0.05 to 0.01 — a statistical threshold changed by a filler
# word, with nothing on screen to say it had happened (report v2_5, B-4). A redraft is destructive
# by nature (measured: it damages steps nobody mentioned in 100% of trials), so it needs an actual
# instruction to justify it.
_NOISE_REPLY_RE = re.compile(
    r"^(?:hmm+|hm+|uh+|um+|er+|ah+|oh+|ok(?:ay)?|k|yeah?|yep|yes|no|nope|sure|fine|right|"
    r"thanks?|thank you|ty|cool|nice|good|great|\.{1,3}|\?+|!+|"
    r"嗯+|额+|呃+|哦+|噢+|好+的?|行|是|对|不|没有|谢谢|可以)$",
    re.I)


def is_noise_reply(text: str) -> bool:
    """True when a reviewer's reply asks for nothing — empty, punctuation, or a bare interjection.

    Only whole-string matches count: "ok, drop the GSEA step" is an instruction that happens to
    start with "ok", and must not be swallowed here."""
    t = (text or "").strip().strip(",.;:!?，。；：！？ ")
    return not t or bool(_NOISE_REPLY_RE.match(t))


# ``padj=0.05`` and ``padj 0.05`` / ``padj of 0.05`` — how plans state a setting either way.
_SETTING_RE = re.compile(
    r"\b([a-z_][a-z0-9_]{2,})\s*(?:=|:|\bof\b|\bto\b)?\s*[\"']?(-?\d+(?:\.\d+)?)[\"']?", re.I)


def _settings_in(step: str) -> "dict[str, str]":
    """``{parameter: value}`` for the numeric settings a step states."""
    return {m.group(1).lower(): m.group(2) for m in _SETTING_RE.finditer(step or "")}


def _similarity(a: str, b: str) -> float:
    """Token-overlap (Jaccard) between two steps — "is this the same step, reworded?"."""
    ta, tb = set(_norm_step(a).split()), set(_norm_step(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def diff_plans(before: "list[str]", after: "list[str]") -> "dict[str, list[Any]]":
    """What a redraft actually did: ``{"dropped", "added", "changed"}``.

    A whole-plan redraft is the one revision path that rewrites steps the reviewer never mentioned.
    Measured on three production redrafts: "also run GSEA and add a literature review step" deleted
    two unrelated steps and added a tool nobody asked for; "make the whole plan simpler" deleted the
    cross-cell-type synthesis; "hmm" moved a significance threshold. None of the three was reported
    anywhere in the console, so the only way to find out was to diff two plans by eye — which is
    what this does instead, deterministically, so the reviewer can accept or reject the collateral
    with the same look they give the plan (report v2_5, B-4).

    Steps are matched by token overlap, so a reworded step counts as the SAME step (its settings are
    then compared) rather than as one deletion plus one addition."""
    unmatched = list(range(len(after)))
    dropped: list[str] = []
    changed: list[dict[str, str]] = []
    for step in before:
        best, score = None, 0.45          # below this, two steps are different work, not a rewording
        for j in unmatched:
            if (s := _similarity(step, after[j])) > score:
                best, score = j, s
        if best is None:
            dropped.append(step)
            continue
        unmatched.remove(best)
        old_settings, new_settings = _settings_in(step), _settings_in(after[best])
        for key, old in old_settings.items():
            new = new_settings.get(key)
            if new is not None and new != old:
                changed.append({"step": after[best], "param": key, "before": old, "after": new})
    return {"dropped": dropped, "added": [after[j] for j in unmatched], "changed": changed}


def stale_downstream_settings(agenda: "list[str]", step_index: int,
                              param: str, old: str, new: str) -> "list[int]":
    """1-based steps AFTER ``step_index`` that still state ``param`` at its pre-change value.

    Changing a threshold in one step does not change the step that consumes its output. Asked to
    use ``padj 0.01``, the revision changed the DE step and left the ORA step below it saying it
    filters input genes at ``padj=0.05`` — while reading a table that by then contained only genes
    at padj<0.01. The plan contradicted itself, on paper, in front of the reviewer, and nothing
    said so (report v2_5, B-7). Reported rather than auto-propagated: whether the downstream number
    should follow is a methodological choice, and the reviewer is right there."""
    if old == new:
        return []
    return [i + 1 for i, step in enumerate(agenda)
            if i > step_index and _settings_in(str(step)).get(param.lower()) == old]


def _parse_query_list(raw: str, max_n: int) -> list[str]:
    """Parse the planner's JSON array of query strings; sanitize each through ``focus_literature_query``
    (strips instruction/file words the model may still slip in) and dedupe. Returns [] on any parse
    failure so the caller falls back to the deterministic single query."""
    from ...literature_contract import focus_literature_query
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].strip()
    parsed: Any = None
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", s, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, str):
            continue
        q = focus_literature_query(item).strip()
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        out.append(q)
        if len(out) >= max(1, max_n):
            break
    return out


def _hand_edit_note(changes: "list[dict[str, Any]]") -> str:
    """The researcher's own edit of the plan, in words, for the revision history.

    A later change asked for in words is made on the edited document. But when that cannot be done
    the plan is patched or redrafted from the feedback so far, and a redraft that was never told
    about the edit could put back a step the researcher removed or a value they changed."""
    def title(text: str) -> str:
        return split_step(text)[0] or text[:80]

    said: list[str] = []
    for c in changes:
        kind, n = c.get("kind"), c.get("step")
        if kind == "removed":
            said.append(f'removed the step "{title(str(c.get("before") or ""))}"')
        elif kind == "added":
            said.append(f'added the step "{title(str(c.get("after") or ""))}" as step {n}')
        elif kind == "title":
            said.append(f'renamed step {n} from "{c.get("before")}" to "{c.get("after")}"')
        elif kind == "setting":
            said.append(f"removed the setting {c.get('param')} from step {n}" if c.get("after") is None
                        else f"set {c.get('param')} to {json.dumps(c.get('after'))} in step {n}")
        elif kind == "tool":
            said.append(f"changed the tool of step {n} to {c.get('after') or 'none'}")
        elif kind == "skill":
            said.append(f"took the skill off step {n}" if not c.get("after")
                        else f"set the skill of step {n} to {c.get('after')}")
        elif kind == "moved":
            said.append(f"moved step {c.get('before')} to position {n}")
        elif kind == "text":
            said.append(f'rewrote step {n} as: {str(c.get("after") or "")[:300]}')
    return ("The researcher edited the plan by hand; keep these changes: " + "; ".join(said) + "."
            if said else "")


def _parse_plan_patch(raw: str, n_steps: int) -> "tuple[int, str] | None":
    """``(0-based index, new step text)`` from the patch reply, or ``None`` when it is unusable or
    the model declined (step 0). ``None`` means "fall back to the redraft" — never "do nothing"."""
    obj = safe_json_loads(raw)
    if not isinstance(obj, dict):
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        try:
            obj = json.loads(m.group(0)) if m else None
        except (ValueError, TypeError):
            obj = None
    if not isinstance(obj, dict):
        return None
    try:
        n = int(obj.get("step", 0))
    except (TypeError, ValueError):
        return None
    text = _strip_step_ordinal(str(obj.get("new_text") or ""))
    if not (1 <= n <= n_steps) or not text:
        return None
    return n - 1, text
