"""Literature steps: the search text, the query, the findings digest, the agenda's literature step."""

from __future__ import annotations

import re
from typing import Any, Callable

from .grounding import _term_label
from .models import LabRound
from .step_kinds import _LOW_PRIORITY_FOR_LITERATURE_RE, _is_literature_step, _literature_step_wanted


# Tokens that look like biological ENTITIES: gene/protein symbols (DDX41, Trp53, ABCA4), cell types
# spelled as symbols (RGC, MG), pathway/complex names. Case-sensitive on purpose — an all-caps or
# Capitalized-plus-digit token in a question is almost always a symbol; a plain lowercase word is
# almost always instruction.
_ENTITY_TOKEN_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9-]{1,}\d[A-Za-z0-9-]*|[A-Z]{2,}[A-Za-z0-9-]*|[A-Z][a-z]{2,}\d+[a-z0-9]*)\b")
_LITERATURE_GENERIC = "Literature search for the key genes and pathways found"
# All-caps tokens that are METHOD / FORMAT names, not biology. Kept separate from the literature
# tool's stopword set because that set is lowercase-keyed and this one guards the symbol regex.
_LITERATURE_NOT_ENTITIES = {
    "WT", "KO", "DE", "QC", "PCA", "UMAP", "GO", "RNA", "DNA", "PDF", "CSV", "OK", "AND", "OR",
    "THE", "PLEASE", "TODO", "VCF", "VEP", "PASS", "GRCH37", "GRCH38", "HG19", "HG38", "BAM",
    "FASTQ", "H5AD", "TSV", "JSON", "API", "REST", "HPC", "GPU", "CPU", "LLM", "AI", "ORA", "GSEA",
    "FDR", "BH", "HVG", "MT", "SNP", "SNV", "INDEL", "CNV", "SV", "CADD", "REVEL", "SPLICEAI",
    "CLINVAR", "GNOMAD", "OMIM", "HPO", "ACMG", "DOI", "PMID", "PUBMED", "SCVI", "SCGPT",
    "SEURAT", "SCANPY", "DESEQ2", "EDGER", "LIMMA", "IRD", "WGS", "WES",
    "ONLY", "NOT", "DO", "MUST", "NEVER", "ALWAYS", "ALL", "ANY", "EACH", "EVERY", "TRUE",
    "FALSE", "NONE", "NULL", "NOTE", "TBD", "IMPORTANT", "REQUIRED", "STRICTLY", "EXACT",
    "EXACTLY", "FIRST", "LAST", "BEFORE", "AFTER", "WITH", "WITHOUT", "USE", "USING", "RUN",
    "SEE", "ABOVE", "BELOW", "THIS", "THAT", "THESE", "THOSE", "FROM", "INTO", "ONTO", "PER",
}


def _literature_step_text(question: str) -> str:
    """The literature step's LABEL: the question's biological SUBJECT, or a clean generic form.

    The old builder printed whatever survived the stopword filter — for a real question ("This
    dataset has 3 donors — please run pseudobulk…") that was the residue "has donors", for a
    Chinese question the sentence with a token punched out of its middle, for "help me analyze…"
    the single word "help". Every one reached a production plan card. The rule now: the focused
    query is used ONLY if it still reads as a subject — it names an entity symbol (DDX41, Abca4) or
    a real biological word (retina, knockout, glia), and it is not a leftover of function words.
    Otherwise the label says nothing specific rather than something wrong. (The per-query terms are
    planned from the ACCEPTED FINDINGS at run time, so nothing scientific rides on this label.)"""
    try:
        from ...literature_contract import QUERY_STOPWORDS as _QUERY_STOPWORDS, focus_literature_query
        query = focus_literature_query(question or "")
        toks = [w for w in query.split() if w.lower().strip("-") not in _QUERY_STOPWORDS]
    except Exception:  # noqa: BLE001 - planning guard should never break planning
        toks = []
    # Drop CJK-bearing tokens: a Chinese sentence minus one word is not a subject, it is damage.
    toks = [t for t in toks if not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", t)]
    # Drop method / format all-caps tokens, question words, and bare function words that the
    # focus filter leaves behind between two content tokens ("What in Rod cells in DDX41").
    _junk = {"what", "which", "how", "why", "when", "where", "who", "in", "of", "on", "at", "to",
             "by", "vs", "and", "or", "the", "a", "an", "is", "are", "does", "do", "did", "there",
             "this", "that", "these", "those", "it", "its", "with", "between", "among", "for",
             "they", "them", "we", "you", "me", "my", "our", "change", "changed", "changes",
             "changing", "differ", "differs", "differed", "different", "differences", "affect",
             "affected", "affects", "types", "type", "cells", "cell", "please", "help", "run"}
    toks = [t for t in toks if t.strip("-").upper() not in _LITERATURE_NOT_ENTITIES
            and t.lower().strip("-?.,;:") not in _junk]
    toks = [t.strip("?.,;:") for t in toks if t.strip("?.,;:")]
    entities = [t for t in toks if _ENTITY_TOKEN_RE.fullmatch(t.strip("-"))]
    # A biological content word: lowercase, >= 5 letters, not a function/instruction residue.
    # ("has", "help", "run", "for", "me" all fail the length gate; "retina", "knockout", "glia"
    # passes on the entity-adjacency below.)
    content = [t for t in toks if t.islower() and len(t.strip("-")) >= 5]
    if not entities and not content:
        return _LITERATURE_GENERIC
    if not entities and len(content) < 2 and len(toks) <= 2:
        # one lonely lowercase word with nothing around it ("donors") is residue, not a subject
        return _LITERATURE_GENERIC
    label = " ".join(toks[:6])
    return f"Literature search for {label}"


def _literature_query(question: str, step: str, rounds: "list[LabRound]") -> str:
    """Build a FOCUSED Europe PMC query for a literature-grounding step from the ACCEPTED findings
    (top marker genes + enriched pathway terms) plus the step's topic — instead of the raw run
    question, which pollutes the query with instruction words (e.g. "finish research"). Falls back
    to a focused (question+step) only when there are no findings yet."""
    from ...literature_contract import focus_literature_query
    genes: list[str] = []
    terms: list[str] = []
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            res = s.get("result") if isinstance(s.get("result"), dict) else {}
            for lst in (res.get("top_genes_by_group") or {}).values():
                for g in (lst or [])[:3]:
                    if g and g not in genes:
                        genes.append(g)
            for lst in (res.get("top_terms_by_group") or {}).values():
                for t in (lst or [])[:1]:
                    label = _term_label(t)
                    if label and label not in terms:
                        terms.append(label)
            # Variant-annotation results carry their genes as a list of variant dicts (not the scanpy
            # top_genes_by_group map) — pull the pathogenic / high-priority variant genes so a VCF
            # run's query is the REAL genes, not a garbled fallback from the raw question.
            for key in ("pathogenic_variants", "high_priority_variants"):
                for v in (res.get(key) or []):
                    g = v.get("gene") if isinstance(v, dict) else None
                    if g and g not in genes:
                        genes.append(g)
    # Keep the query BROAD: Europe PMC ANDs every term, so a long mash-up returns nothing. One pathway
    # term + a few genes is already at the edge of useful.
    bits: list[str] = []
    if terms:
        bits.append(terms[0])
    if genes:
        bits.append(" ".join(genes[:3]))
    q = " ".join(bits).strip()
    if q:
        return q
    # No findings yet → the QUESTION's biological subject first (the step now carries the PI's own
    # wording — "Summarize findings with literature context" — which names no biology), then the
    # step, then both. Only used pre-findings; once we HAVE genes, question words just pollute it.
    subj = _literature_step_text(question)
    if subj != _LITERATURE_GENERIC:
        return subj[len("Literature search for "):]
    return focus_literature_query(step) or focus_literature_query(f"{question} {step}") or step


# Citations listed under a corpus-grounded answer. These are the papers the CHECKED answer already
# cites, so cutting them drops references the answer makes: a step whose plan pinned a larger
# ``search_corpus`` top_k read more than twelve passages and still listed twelve
# (RCHENLAB/AiScientist issue 60, review of PR #135). The ceiling is ``search_corpus``'s own.
MAX_LISTED_CITATIONS = 30

# PaperQA's own refusal, at the start of its answer ("I cannot answer.", "I cannot answer this
# question due to having no papers").
_CANNOT_ANSWER = re.compile(r"^\s*(?:I\s+)?(?:cannot|can't|am unable to) answer\b", re.IGNORECASE)


def _deep_literature_fallback_question(step: str) -> str:
    """The step brief as a plain question when no model writes one: title and markup stripped."""
    text = re.sub(r"^\s*\*\*[^*]+\*\*\s*[—–:-]\s*", "", step or "")
    text = re.sub(r"`[^`]*`", "", text)
    text = re.sub(r"\s+", " ", text.replace("*", "")).strip()
    sentences = re.split(r"(?<=[.?!])\s+", text)
    body = " ".join(sentences[:2])[:400].strip()
    return f"What does the published literature say about the following: {body}" if body else ""


def _literature_findings_digest(rounds: "list[LabRound]") -> str:
    """A compact per-cell-class digest of accepted findings (top markers + enriched pathways) that
    the LLM query planner turns into several angle-specific literature queries. Empty when there are
    no structured findings yet (planner then falls back to the question/step)."""
    genes_by: dict[str, list[str]] = {}
    terms_by: dict[str, list[str]] = {}
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            res = s.get("result") if isinstance(s.get("result"), dict) else {}
            for grp, lst in (res.get("top_genes_by_group") or {}).items():
                bucket = genes_by.setdefault(str(grp), [])
                for g in (lst or [])[:5]:
                    if g and g not in bucket:
                        bucket.append(g)
            for grp, lst in (res.get("top_terms_by_group") or {}).items():
                bucket = terms_by.setdefault(str(grp), [])
                for t in (lst or [])[:2]:
                    label = _term_label(t)
                    if label and label not in bucket:
                        bucket.append(label)
    lines: list[str] = []
    for grp in dict.fromkeys([*genes_by, *terms_by]):
        parts: list[str] = []
        if genes_by.get(grp):
            parts.append("markers " + ", ".join(genes_by[grp][:5]))
        if terms_by.get(grp):
            parts.append("pathways " + ", ".join(terms_by[grp][:2]))
        if parts:
            lines.append(f"- {grp}: " + "; ".join(parts))
    return "\n".join(lines)


# If the user requested literature support:
# 1. If the literature_search tool is unavailable, leave the agenda unchanged.
# 2. If the agenda already contains literature-related steps, collapse them into one canonical step.
# 3. If the agenda has room, append the literature step.
# 4. If the agenda is full, replace a lower-priority step with the literature step.
def _ensure_literature_agenda(
    agenda: list[str],
    *,
    question: str,
    guidance: str | None,
    feedback: str,
    max_steps: int,
    has_literature_tool: bool,
    author: "Callable[[str], str] | None" = None,
    prior_agenda: "list[str] | None" = None,
    latest_feedback: str = "",
) -> list[str]:
    """Deterministically preserve a literature step when the user requested one.

    The guard decides THAT the step exists. WHAT it says comes from ``author`` (the PI writing one
    step from the full question + dataset profile) when one is wired; the string template
    ``_literature_step_text`` is only the offline fallback. A PI-written literature step that is
    already in the plan is kept as written — it is not replaced by anything.

    Whether the user requested one is :func:`_literature_step_wanted`: their own words, and on a
    revision the plan they reviewed (``prior_agenda``) and the note they just typed
    (``latest_feedback``). ``guidance`` is not read: a protocol names literature, or a "reference
    panel", in every pipeline, which says nothing about what this user asked for."""
    if not has_literature_tool or not _literature_step_wanted(
            agenda, question=question, feedback=feedback,
            prior_agenda=prior_agenda, latest_feedback=latest_feedback):
        return agenda
    existing_lit = [existing for existing in agenda if _is_literature_step(existing)]
    if existing_lit:
        # Keep the PI's own wording; only collapse duplicates. Of several, keep the most
        # informative one (a bare tool name like "literature_search" loses to a real sentence);
        # a step that is ONLY a tool name is not a step, so fall back to the label for it.
        keep = max(existing_lit, key=len)
        if len(keep.strip()) < 30:
            keep = author(_literature_step_text(question)) if author is not None \
                else _literature_step_text(question)
        deduped: list[str] = []
        inserted = False
        for existing in agenda:
            if _is_literature_step(existing):
                if not inserted:
                    deduped.append(keep)
                    inserted = True
                continue
            deduped.append(existing)
        return deduped[:max_steps]
    # A step the redraft lost goes back as the user reviewed it, not as a newly written one.
    reviewed = max((s for s in prior_agenda or [] if _is_literature_step(s)), key=len, default="")
    fallback = _literature_step_text(question)
    step = reviewed or (author(fallback) if author is not None else fallback)
    if len(agenda) < max_steps:
        return [*agenda, step]
    if not agenda:
        return [step]
    replace_idx = len(agenda) - 1
    for idx in range(len(agenda) - 1, -1, -1):
        if _LOW_PRIORITY_FOR_LITERATURE_RE.search(agenda[idx]):
            replace_idx = idx
            break
    out = list(agenda)
    out[replace_idx] = step
    return out

# Summarize the literature_search result into a DOI/PMID-backed answer that the Critic can accept
# and the final References section can reuse.
def _literature_answer(query: "str | list[str]", result: dict[str, Any]) -> str:
    queries = [query] if isinstance(query, str) else [q for q in (query or []) if q]
    disp = " | ".join(queries) or "(none)"
    noun = "query" if len(queries) <= 1 else "queries"
    citations = [
        c for c in (result.get("results") or [])
        if isinstance(c, dict) and (c.get("doi") or c.get("pmid"))
    ]
    if not citations:
        return (
            f"No DOI/PMID-backed literature citations were returned for {noun} `{disp}`. "
            "State this limitation in the report rather than inventing references."
        )
    lines = [
        f"Literature search {noun}: `{disp}`",
        "",
        "DOI/PMID-backed citations found (merged, de-duplicated across queries):",
    ]
    for i, c in enumerate(citations[:12], 1):
        lines.append(f"{i}. {c.get('citation') or c.get('title')}")
    return "\n".join(lines)
