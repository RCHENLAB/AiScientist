"""Free clinical text -> HPO terms, for the IRD line.

THE PROBLEM: LIRICAL/Exomiser need the patient's phenotype as HPO IDs, but clinicians write free text —
"10 岁男孩，自幼夜盲，视野缩窄，ERG 呈熄灭型，无听力障碍" or "Stargardt disease, no hearing loss". Asking an
LLM for "the HPO ID of night blindness" is the tempting shortcut and the dangerous one: HP:0000662 is
Nyctalopia, HP:0000622 is Blurred vision — a transposition apart, both real eye phenotypes. A
fabricated-but-real ID therefore fails SILENTLY: nothing errors, LIRICAL just scores the wrong
phenotype and returns a confident, wrong differential.

THE DESIGN — the LLM does language, the ontology does identity. Neither job crosses over:

  1. EXTRACT (LLM)      free text -> clinical phrases + negation. This is the part only an LLM does well:
                        translating (中文 -> English), expanding jargon ("ERG 熄灭型" -> "nonrecordable
                        electroretinogram"), splitting compounds, catching "no hearing loss" as NEGATED and
                        "her mother had RP" as FAMILY HISTORY (dropped — it is not the patient's phenotype).
  2. RETRIEVE (code)    each phrase -> real candidate terms from :mod:`.index` (the HPO release, ~19k terms
                        + ~24k synonyms), plus the curated IRD alias table for disease-name shorthand the
                        ontology has no synonym for ("Stargardt" -> Macular dystrophy, "LCA", "RP").
  3. SELECT (LLM)       the LLM picks a CANDIDATE NUMBER from that closed list, or 0 for none. It never
                        types an ID, so it cannot invent one. Exact lexical hits skip this step entirely.
  4. VALIDATE (code)    every surviving ID is re-checked against the index; the canonical name is taken
                        from the ONTOLOGY, never from the model. Unknown/obsolete/malformed -> dropped or
                        replaced, and reported.

Every mapped term carries the phrase it came from and the ``method`` that produced it, so a clinician can
audit the mapping ("HP:0000662 Nyctalopia <- '夜盲', llm_closed_set") instead of trusting it. With no LLM
reachable the module degrades to deterministic lexical mapping (still grounded, lower recall) rather than
failing — same posture as the rest of the variant line.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from . import load_hpo_table
from .index import HpoHit, HpoIndex, get_index, normalize

ChatFn = Callable[[list[dict]], str]

# How good a lexical-only match must be to be accepted with no LLM adjudication. 1.0 is an exact
# name/synonym hit; 0.8 still demands near-complete IDF-weighted overlap ("cone dystrophies" ->
# "Cone dystrophy"). Below this a phrase is left UNMAPPED rather than guessed — an unmapped phrase is
# visible and recoverable, a wrong term is neither.
_LEXICAL_ACCEPT = 0.80
_MAX_CANDIDATES = 8          # candidates shown to the LLM per phrase (a closed set it must choose from)
_MAX_PHRASES = 40            # sanity bound on one clinical note

# Cues that make an UNSEGMENTED lexical scan unsafe. The no-LLM fallback substring-scans the whole
# text, so it cannot see WHOSE finding it is or whether it was DENIED — on real prose it happily reads
# "a cousin has retinitis pigmentosa" and "denies night blindness" as the patient's OWN present
# findings (both observed on this fixture before this guard existed). Those are not low recall, they are
# WRONG, and a wrong phenotype is the exact silent failure this module exists to prevent. So when a cue
# appears we refuse to map lexically rather than guess. A bare diagnosis label ("Stargardt", "BBS", "RP
# with macular involvement" — how the lab's sheet is written) carries no cue and is still mapped.
_LEXICAL_UNSAFE = (
    # negation
    r"\bno\b", r"\bnot\b", r"\bnone\b", r"\bdenie[sd]\b", r"\bdenying\b", r"\babsent\b",
    r"\bwithout\b", r"\bnegative for\b", r"\bnormal\b", r"\bunremarkable\b", r"\brule[sd]? out\b",
    "无", "否认", "未见", "没有", "阴性", "排除", "正常",
    # someone else's phenotype
    r"\bfamily history\b", r"\bmother\b", r"\bfather\b", r"\bparents?\b", r"\bsister\b",
    r"\bbrother\b", r"\bcousin\b", r"\baunt\b", r"\buncle\b", r"\bsibling\b", r"\bgrand(mother|father)\b",
    r"\bmaternal\b", r"\bpaternal\b", r"\bproband'?s? (mother|father|sister|brother)\b",
    "家族史", "母亲", "父亲", "父母", "姐", "妹", "兄", "弟", "表", "堂", "祖", "外婆", "外公", "亲属",
)


def _lexical_unsafe_reason(text: str) -> str:
    """Non-empty when ``text`` contains negation / family-history cues that an unsegmented lexical scan
    cannot honour — i.e. when mapping it WITHOUT an LLM would risk asserting the wrong phenotype."""
    low = (text or "").lower()
    for pat in _LEXICAL_UNSAFE:
        if re.search(pat, low) if pat.startswith(("\\", "(", "[")) or "\\b" in pat else (pat in low):
            return pat.replace("\\b", "").strip()
    return ""


_EXTRACT_SYSTEM = (
    "You are a clinical phenotyping assistant. Extract the PATIENT's phenotypic abnormalities from the "
    "clinical text and return STRICT JSON — an array, no prose, no code fence:\n"
    '[{"phrase": "<English clinical term>", "negated": false, "source": "<verbatim span from the text>"}]\n'
    "Rules:\n"
    "- Translate to standard English clinical terminology (the input may be Chinese or mixed).\n"
    "- Expand abbreviations and shorthand ('ERG 熄灭型' -> 'nonrecordable electroretinogram'; "
    "'RP' -> 'retinitis pigmentosa').\n"
    "- Split compound descriptions into ONE finding per entry.\n"
    "- `phrase` is ALWAYS the finding ITSELF, named POSITIVELY. NEVER put a negation word inside it — "
    "that goes in the `negated` flag. The ontology names findings, not their absence, so a phrase like "
    '"no hearing loss" matches nothing and the exclusion is lost. Convert:\n'
    '    "无听力障碍" / "no hearing loss" / "hearing is normal"\n'
    '      -> {"phrase": "hearing impairment", "negated": true, "source": "无听力障碍"}   CORRECT\n'
    '      -> {"phrase": "no hearing loss",    "negated": true}                          WRONG\n'
    "- negated=true ONLY for findings the text explicitly states are ABSENT or normal.\n"
    "- SKIP: family history of others ('her mother had RP'), treatments, genes/variants, test names with "
    "no abnormal finding, age/sex, and anything not a phenotype of THIS patient.\n"
    "- A disease name given as the diagnosis IS extractable as a phrase (e.g. 'Stargardt disease').\n"
    "- Do NOT output HPO IDs. Do NOT invent findings absent from the text. Empty array if none."
)

_SELECT_SYSTEM = (
    "You map one clinical phrase to the single best matching HPO term from a numbered candidate list.\n"
    "Answer with STRICT JSON only: {\"choice\": <number>, \"reason\": \"<short>\"}\n"
    "- <number> MUST be one of the listed candidate numbers, or 0 if NONE of them means the same thing.\n"
    "- Choose the term that means the SAME finding as the phrase; prefer the most specific candidate that "
    "is still fully supported by the phrase (do not add specificity the phrase does not state).\n"
    "- Answer 0 rather than forcing a wrong match. Never write an HPO ID."
)


@dataclass
class MappedTerm:
    """One mapped phenotype — an ontology-validated ID plus the audit trail that produced it."""

    hpo_id: str
    name: str                    # canonical label from the INDEX (never the model's wording)
    phrase: str                  # the normalized clinical phrase that mapped
    source: str = ""             # the verbatim span of the clinician's text it came from
    negated: bool = False
    method: str = ""             # curated_alias | ontology_exact | lexical_only | llm_closed_set
    score: float = 0.0
    matched_name: str = ""       # which of the term's names matched (label or a synonym)

    def as_dict(self) -> dict[str, Any]:
        return {"hpo_id": self.hpo_id, "name": self.name, "phrase": self.phrase, "source": self.source,
                "method": self.method, "score": round(self.score, 3), "matched_name": self.matched_name}


@dataclass
class Phrase:
    text: str
    negated: bool = False
    source: str = ""


def _loads(raw: str) -> Any:
    """Tolerant JSON parse of a model reply (strips code fences / surrounding prose); None on failure."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"(\[.*\]|\{.*\})", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# --- 1. EXTRACT --------------------------------------------------------------


def extract_phrases(text: str, chat_fn: "ChatFn | None") -> list[Phrase]:
    """Clinical free text -> :class:`Phrase` list (LLM). Returns ``[]`` when no LLM is reachable or the
    reply is unusable — the caller then falls back to whole-text lexical mapping."""
    text = (text or "").strip()
    if not text or chat_fn is None:
        return []
    try:
        reply = chat_fn([{"role": "system", "content": _EXTRACT_SYSTEM},
                         {"role": "user", "content": f"Clinical text:\n{text}"}])
    except Exception:                                   # noqa: BLE001 - an LLM outage degrades, never fails
        return []
    data = _loads(reply)
    if not isinstance(data, list):
        return []
    out: list[Phrase] = []
    for item in data[:_MAX_PHRASES]:
        if not isinstance(item, dict):
            continue
        phrase = str(item.get("phrase") or "").strip()
        if phrase:
            out.append(Phrase(text=phrase, negated=bool(item.get("negated")),
                              source=str(item.get("source") or "").strip()))
    return out


# --- 2. RETRIEVE -------------------------------------------------------------


_ALIASES: "list[tuple[str, str, str]] | None" = None      # (normalized keyword, hpo_id, name)


def _aliases() -> list[tuple[str, str, str]]:
    """The curated table, normalized once per process (it is read per phrase otherwise)."""
    global _ALIASES
    if _ALIASES is None:
        _ALIASES = [(" ".join(normalize(kw)), t["id"], t["name"])
                    for t in load_hpo_table() for kw in t["keywords"] if " ".join(normalize(kw))]
    return _ALIASES


def _alias_hit(phrase: str) -> "tuple[str, str] | None":
    """The curated IRD alias table (``ird_hpo.tsv``) -> ``(hpo_id, name)`` for domain shorthand the HPO's
    own synonyms do not carry: disease names used as phenotype shorthand ("Stargardt", "Usher syndrome")
    and field abbreviations ("LCA", "CORD"). Matched on WORD BOUNDARIES so a short alias cannot fire
    inside another word ('ird' in 'third'). First table row wins (the table is ordered general -> specific
    per concept)."""
    low = " ".join(normalize(phrase))
    if not low:
        return None
    for key, hpo_id, name in _aliases():
        if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", low):
            return hpo_id, name
    return None


# --- 3. SELECT ---------------------------------------------------------------


def _select_candidate(phrase: str, hits: list[HpoHit], chat_fn: ChatFn) -> "HpoHit | None":
    """Ask the LLM to pick ONE candidate by NUMBER from the retrieved closed set (0 = none fits).

    The model cannot type an ID, so it cannot invent one; an out-of-range or unparseable answer is
    discarded (the caller falls back to the lexical rule) and an explicit 0 is RESPECTED — 'none of these'
    is a correct, useful answer that keeps a bad term out of LIRICAL."""
    listing = "\n".join(
        f"{i}. {h.term.label} [{h.term.id}]" + (f" — matched on synonym '{h.matched_name}'"
                                                if h.matched_name != h.term.label else "")
        for i, h in enumerate(hits, start=1))
    try:
        reply = chat_fn([{"role": "system", "content": _SELECT_SYSTEM},
                         {"role": "user", "content": f"Phrase: {phrase}\n\nCandidates:\n{listing}\n\n"
                                                     f"0. none of these"}])
    except Exception:                                   # noqa: BLE001
        return None
    data = _loads(reply)
    choice: Any = None
    if isinstance(data, dict):
        choice = data.get("choice")
    elif isinstance(data, (int, float)):
        choice = data
    try:
        n = int(choice)                                 # tolerate "2" as well as 2
    except (TypeError, ValueError):
        return None
    if n <= 0 or n > len(hits):
        return None
    return hits[n - 1]


# --- the mapper --------------------------------------------------------------


def map_phrase(phrase: str, *, index: "HpoIndex | None" = None, chat_fn: "ChatFn | None" = None,
               k: int = _MAX_CANDIDATES) -> "MappedTerm | None":
    """One clinical phrase -> one validated HPO term, or None if nothing trustworthy matches.

    Order is most-precise first:

    1. an EXACT ontology name (the phrase IS an HPO label/synonym) — unambiguous, no adjudication;
    2. the curated IRD alias, but ONLY when the ontology has nothing good to offer (see below);
    3. the LLM's pick from the retrieved candidates;
    4. a high-confidence lexical top-1 (only when no LLM is reachable).

    **Why the alias is gated on a weak ontology hit** (measured 2026-07-15): the alias table matches a
    KEYWORD anywhere in the phrase, so "central vision loss" hit its ``vision loss`` keyword and returned
    the generic HP:0000505 *Visual impairment* — pre-empting both the LLM and the ontology's own
    HP:0000572 *Visual loss* (0.82) and HP:0000529 *Progressive visual loss* (0.87), which are strictly
    more specific and which the disease annotations actually carry. LIRICAL's likelihood ratio rewards
    specificity, so that one hijack moved a posterior 8x (94.78% -> 12.36%) on an identical case. The
    table is hand-written and coarse BY CONSTRUCTION — it exists for shorthand the ontology cannot name
    ("Stargardt", "BBS", "RP"), not to out-vote the ontology on terms HPO names better. So it now fires
    only when the ontology retrieval is weak, i.e. exactly when it is the only thing that can help.
    """
    ix = index or get_index()
    phrase = (phrase or "").strip()
    if not phrase:
        return None

    hits = ix.search(phrase, k=k)
    top = hits[0] if hits else None
    if top is not None and top.score >= 1.0:            # exact label/synonym — no adjudication needed
        return MappedTerm(hpo_id=top.term.id, name=top.term.label, phrase=phrase,
                          method="ontology_exact", score=top.score, matched_name=top.matched_name)

    if top is None or top.score < _LEXICAL_ACCEPT:      # the ontology has nothing solid → the table may help
        alias = _alias_hit(phrase)
        if alias:
            check = ix.validate(alias[0])              # the curated table is still checked against HPO
            if check["status"] == "ok":
                return MappedTerm(hpo_id=check["hpo_id"], name=check["name"], phrase=phrase,
                                  method="curated_alias", score=1.0, matched_name=alias[1])

    if not hits:
        return None

    if chat_fn is not None:
        picked = _select_candidate(phrase, hits, chat_fn)
        if picked is not None:
            check = ix.validate(picked.term.id)         # re-validate even a closed-set pick
            if check["status"] == "ok":
                return MappedTerm(hpo_id=check["hpo_id"], name=check["name"], phrase=phrase,
                                  method="llm_closed_set", score=picked.score,
                                  matched_name=picked.matched_name)
        else:
            return None                                 # the LLM saw the candidates and rejected them

    if top.score >= _LEXICAL_ACCEPT:
        return MappedTerm(hpo_id=top.term.id, name=top.term.label, phrase=phrase,
                          method="lexical_only", score=top.score, matched_name=top.matched_name)
    return None


def map_text_to_hpo(text: str, *, chat_fn: "ChatFn | None" = None, index: "HpoIndex | None" = None,
                    k: int = _MAX_CANDIDATES) -> dict[str, Any]:
    """Clinical free text -> ``{observed: [...], excluded: [...], unmapped: [...]}`` of validated HPO terms.

    ``observed`` / ``excluded`` are ready to hand to ``run_lirical`` as ``hpo_terms`` / ``excluded_hpo``.
    ``unmapped`` lists phrases no trustworthy term was found for — surfaced, never silently dropped.
    With ``chat_fn=None`` (no LLM) the whole text is mapped lexically: much lower recall on real prose,
    which the result labels via ``mode='lexical_only'`` so a reader knows what they are looking at."""
    ix = index or get_index()
    phrases = extract_phrases(text, chat_fn)
    mode = "llm" if phrases else ("lexical_only" if chat_fn is None else "llm_extract_empty")

    if not phrases:
        # No LLM (or it returned nothing usable). The fallback substring-scans the raw text, which is
        # only sound while every mention is a PRESENT finding of THIS patient — so refuse outright when
        # the text says otherwise. Returning nothing is recoverable; asserting a denied symptom or a
        # relative's disease as the patient's phenotype is not.
        unsafe = _lexical_unsafe_reason(text)
        if unsafe:
            return _result(ix, [], [], [], mode="needs_llm", text=text, unsafe_cue=unsafe)
        from . import infer_hpo_terms
        observed: list[MappedTerm] = []
        for hpo_id, name in infer_hpo_terms(text, default=False):
            check = ix.validate(hpo_id)
            if check["status"] == "ok":
                observed.append(MappedTerm(hpo_id=check["hpo_id"], name=check["name"], phrase=text[:120],
                                           method="curated_alias", score=1.0, matched_name=name))
        return _result(ix, observed, [], [], mode=mode, text=text)

    observed: list[MappedTerm] = []
    excluded: list[MappedTerm] = []
    unmapped: list[dict[str, Any]] = []
    for ph in phrases:
        term = map_phrase(ph.text, index=ix, chat_fn=chat_fn, k=k)
        if term is None:
            unmapped.append({"phrase": ph.text, "source": ph.source, "negated": ph.negated})
            continue
        term.source, term.negated = ph.source, ph.negated
        (excluded if ph.negated else observed).append(term)
    return _result(ix, observed, excluded, unmapped, mode=mode, text=text)


def _dedupe(terms: list[MappedTerm]) -> list[MappedTerm]:
    """One row per HPO ID (first wins — earlier phrases are the clinician's own ordering), merging the
    later phrases into the kept row's ``phrase`` so no source text is lost from the audit trail."""
    out: list[MappedTerm] = []
    seen: dict[str, MappedTerm] = {}
    for t in terms:
        prev = seen.get(t.hpo_id)
        if prev is None:
            seen[t.hpo_id] = t
            out.append(t)
        elif t.phrase and t.phrase not in prev.phrase:
            prev.phrase = f"{prev.phrase}; {t.phrase}"
    return out


def _result(ix: HpoIndex, observed: list[MappedTerm], excluded: list[MappedTerm],
            unmapped: list[dict[str, Any]], *, mode: str, text: str,
            unsafe_cue: str = "") -> dict[str, Any]:
    observed, excluded = _dedupe(observed), _dedupe(excluded)

    # A term extracted as BOTH present and absent means the text (or the extraction) contradicts itself.
    # Keep it observed and drop the exclusion: a spurious "excluded" actively pushes LIRICAL AWAY from the
    # right disease (absent findings lower a disease's likelihood), so the asymmetry favours dropping it.
    obs_ids = {t.hpo_id for t in observed}
    conflicts = [t.hpo_id for t in excluded if t.hpo_id in obs_ids]
    excluded = [t for t in excluded if t.hpo_id not in obs_ids]

    warnings: list[str] = []
    if conflicts:
        warnings.append(f"{len(conflicts)} term(s) were extracted as both present and absent "
                        f"({', '.join(conflicts)}); kept as OBSERVED, exclusion dropped — check the text.")
    if unmapped:
        warnings.append(f"{len(unmapped)} phrase(s) could not be mapped to an HPO term and were left out: "
                        + "; ".join(u["phrase"] for u in unmapped[:5]))
    if mode == "lexical_only":
        warnings.append("No LLM was available: mapped by the curated IRD keyword table only, so free-text "
                        "phrasing outside that table was not recognized.")
    elif mode == "llm_extract_empty":
        warnings.append("The LLM returned no usable phenotype phrases; fell back to the curated IRD "
                        "keyword table.")
    elif mode == "needs_llm":
        warnings.append(
            f"NOT MAPPED: no LLM was reachable, and this text contains negation / family-history "
            f"wording (matched '{unsafe_cue}'). The keyword fallback scans the whole text and cannot "
            f"tell a DENIED finding, or a RELATIVE's disease, from the patient's own — on this text it "
            f"would assert a phenotype the note does not claim. Returning nothing instead. Bring the "
            f"served model up and re-run, or pass the findings as an explicit phrase list.")

    return {
        "status": "ok",
        "tool": "map_phenotype_to_hpo",
        "mode": mode,
        "hpo_version": ix.version,
        "text_chars": len(text or ""),
        "observed": [t.as_dict() for t in observed],
        "excluded": [t.as_dict() for t in excluded],
        "unmapped": unmapped,
        "hpo_terms": [t.hpo_id for t in observed],          # ready for run_lirical(hpo_terms=…)
        "excluded_hpo": [t.hpo_id for t in excluded],       # ready for run_lirical(excluded_hpo=…)
        "n_observed": len(observed),
        "n_excluded": len(excluded),
        "warnings": warnings,
        # Every ID above was retrieved from the HPO release named in hpo_version and re-validated against
        # it; the LLM only ever chose among retrieved candidates.
        "grounding": "closed_set_hpo_index",
        "raw_data_to_llm": False,
    }


def validate_hpo_ids(hpo_ids: "list[str] | tuple[str, ...]",
                     index: "HpoIndex | None" = None) -> dict[str, Any]:
    """Deterministic gate for IDs that did NOT come from this mapper (an LLM's direct guess, a spreadsheet,
    a paper). -> ``{valid: [ids], rejected: [{hpo_id, status, note}], remapped: [{from, to}]}``.

    Obsolete IDs are auto-forwarded to their replacement (that is what the ontology's ``replaced_by``
    means); unknown/malformed ones are rejected with a reason. Used by ``run_lirical`` so a hallucinated
    ID cannot silently define a patient's phenotype."""
    ix = index or get_index()
    valid: list[str] = []
    rejected: list[dict[str, Any]] = []
    remapped: list[dict[str, str]] = []
    labels: dict[str, str] = {}
    for raw in hpo_ids or []:
        check = ix.validate(raw)
        if check["status"] == "ok":
            if check["hpo_id"] not in valid:
                valid.append(check["hpo_id"])
                labels[check["hpo_id"]] = check["name"]
            continue
        if check["status"] == "obsolete" and check.get("replaced_by"):
            fwd = ix.validate(check["replaced_by"])
            if fwd["status"] == "ok":
                if fwd["hpo_id"] not in valid:
                    valid.append(fwd["hpo_id"])
                    labels[fwd["hpo_id"]] = fwd["name"]
                remapped.append({"from": check["hpo_id"], "to": fwd["hpo_id"], "name": fwd["name"]})
                continue
        rejected.append(check)
    return {"valid": valid, "labels": labels, "rejected": rejected, "remapped": remapped,
            "hpo_version": ix.version}


# --- the Scientist tool ------------------------------------------------------


def make_hpo_mapping_tool() -> Any:
    """The ``map_phenotype_to_hpo`` tool: the ONLY sanctioned way for the orchestrator to turn a clinical
    description into HPO IDs for ``run_lirical``. Runs in-process (no HPC3): the ontology index is bundled
    and the LLM calls go to this session's served model via ``ctx.tunnel_port``."""
    from ...agents.research_harness import HarnessTool

    def _exec(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # An ATTACHED case note (LabRequest.case_note) is AUTHORITATIVE and takes precedence over a
        # `text` argument. The user attached that note specifically for this tool; the model, by
        # contrast, routinely calls the tool with a `text` that is the TASK description rather than the
        # note ("map the phenotype for this patient's variants"), which extracts to nothing and makes
        # the attachment look like it "didn't work" (observed 2026-07-17: the note reached decisions but
        # a non-empty junk `text` shadowed it → n_observed:0). So the attachment wins; `text` is used
        # only when there is no attachment (the note was pasted into the question instead).
        decisions = getattr(ctx, "decisions", None) or {}
        case_note = str(decisions.get("case_note") or "").strip()
        arg_text = str(args.get("text") or "").strip()
        if case_note:
            text, source = case_note, "attached_case_note"
        elif arg_text:
            text, source = arg_text, "argument"
        else:
            return {"status": "error", "observed": [], "excluded": [],
                    "error": "map_phenotype_to_hpo needs the clinical text to map — pass `text`, or "
                             "attach a case note to the run. Do NOT invent a phenotype."}

        chat_fn: "ChatFn | None" = None
        port = getattr(ctx, "tunnel_port", None)
        if port is not None:
            from ...gateway import vllm_client        # deferred: keep tools decoupled from the gateway

            def chat_fn(messages: list[dict]) -> str:      # noqa: F811 - bound only when a model is served
                # think=False is LOAD-BEARING: the served Qwen3.6 is a reasoning model, and with the
                # thinking trace ON it exhausts max_tokens BEFORE emitting the JSON -> empty content ->
                # zero phrases -> mode=needs_llm (the exact prod failure: a correctly-attached case note
                # mapped to n_observed:0). Extraction/selection are structured JSON tasks that need no
                # chain-of-thought.
                #
                # max_tokens is a CEILING, not a target: with thinking off the model emits the JSON and
                # STOPS (a dense real note used ~520 tok), so a generous ceiling costs nothing on normal
                # notes and only guarantees a rich one is never truncated. The nominal output bound is
                # _MAX_PHRASES(40) objects of ~80 tok (~3.2k), but an extreme note can quote long verbatim
                # `source` spans, so we keep 2-3x margin: 10000 clears any realistic extraction while
                # still capping runaway generation. (The original 800 was an unjustified default that
                # pre-dated this reasoning and truncated real notes.)
                return vllm_client.complete(port, getattr(ctx, "model", ""), messages,
                                            max_tokens=10000, timeout=120.0, think=False)

        out = map_text_to_hpo(text, chat_fn=chat_fn)
        out["text_source"] = source          # which text was mapped — the model's, or the attachment
        return out

    return HarnessTool(
        "map_phenotype_to_hpo",
        "Convert a patient's clinical description in FREE TEXT (any language — a referral note, a "
        "diagnosis line, a symptom list) into validated HPO term IDs for run_lirical. ALWAYS use this "
        "instead of writing HPO IDs yourself: HPO IDs one digit apart are different real phenotypes, so a "
        "guessed ID silently produces a wrong differential. This tool extracts each finding, matches it "
        "against the real HPO release, and returns ONLY IDs that exist in the ontology, each with the "
        "phrase it came from. Returns `hpo_terms` (observed, pass straight to run_lirical) and "
        "`excluded_hpo` (findings the text says are ABSENT, e.g. 'no hearing loss'), plus `unmapped` for "
        "phrases it could not match — report those rather than substituting your own IDs.",
        {"type": "object", "properties": {
            "text": {"type": "string",
                     "description": "the patient's clinical description / diagnosis / symptom list, "
                                    "VERBATIM — copy the clinician's own words, do not paraphrase or "
                                    "translate them yourself (e.g. 'night blindness since childhood, "
                                    "constricted visual fields, nonrecordable ERG, no hearing loss'). "
                                    "Omit ONLY when a case note is attached to the run, in which case "
                                    "the attached note is mapped."}}},
        _exec,
        reads_private_data=True, category="annotation",
    )
