"""Answers written from ``search_corpus`` passages, and the check that they rest on them.

``search_corpus`` returns passages only. The session's model writes the queries and then the answer
(this module holds both prompts), and :func:`verify_answer` replaces what PaperQA's own grounding did:
a PMID the answer cites must be one of the returned passages, and a sentence the answer quotes must
occur verbatim in one of them. What fails is removed and reported, never kept.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

MAX_QUESTIONS = 6
# A quotation shorter than this many words is a term, not a quoted sentence; it is not checked.
MIN_QUOTE_WORDS = 5

QUESTIONS_SYSTEM = (
    "You are writing search requests for a retrieval index of full-text biomedical papers (embedding "
    "search, not keyword search). Given the research question, the literature step and the study's "
    "accepted findings, write 2-5 DISTINCT requests, each a full sentence that a paper passage could "
    "answer, each from a different angle (a gene, a cell class, a pathway, the disease mechanism). A "
    "bare list of gene symbols retrieves poorly; put a symbol inside a sentence. "
    'Reply with ONLY a JSON array of strings, e.g. ["Does loss of one copy of PRPF31 cause autosomal '
    'dominant retinitis pigmentosa?", "How do rod photoreceptors degenerate in RHO P23H models?"].'
)

ANSWER_SYSTEM = (
    "You answer a research question from literature passages and from nothing else. Each passage is "
    "labelled with its PMID. Rules: (1) use only facts the passages state; if they do not cover part of "
    "the question, say so; (2) cite every claim with its PMID in square brackets, as [PMID:12345678], "
    "using ONLY PMIDs of the passages given; (3) if you quote, copy the sentence exactly, in double "
    "quotation marks, from a single passage; (4) write 1-3 short paragraphs, no headings. A citation "
    "or quotation that is not in the passages is deleted from your answer by a checker."
)

GENE_TABLE_SYSTEM = (
    "Extract a gene evidence table answering the question using only the supplied corpus passages. "
    "Return ONLY a JSON array of objects with gene (one gene symbol), evidence (a short verbatim "
    "quote containing that symbol and supporting its inclusion), and passage (the 1-based passage "
    "number). Copy evidence exactly, including case and punctuation, from that one passage. "
    "Do not infer genes, combine quotes or add facts. Return [] if the passages do not answer this."
)


def gene_table_messages(question: str, contexts: list[dict[str, Any]]) -> list[dict]:
    """Ask the session model for rows grounded in individually numbered search passages."""
    return [{"role": "system", "content": GENE_TABLE_SYSTEM},
            {"role": "user", "content": f"Question: {question}\n\n" + passages_block(contexts)}]


def verify_gene_table(raw: str, contexts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Keep only exact quotes with their gene symbol and source PMID/page; never trust model citations."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        rows = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    kept: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        gene, quote, passage = row.get("gene"), row.get("evidence"), row.get("passage")
        if (not isinstance(gene, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{0,39}", gene)
                or not isinstance(quote, str) or not quote.strip()
                or type(passage) is not int or not 1 <= passage <= len(contexts)):
            continue
        context = contexts[passage - 1]
        pmid, page = str(context.get("pmid") or "").strip(), context.get("page")
        if (not pmid or page in (None, "") or quote not in str(context.get("text") or "")
                or not re.search(r"(?<![A-Za-z0-9])" + re.escape(gene) + r"(?![A-Za-z0-9])", quote)):
            continue
        key = (gene, quote, pmid, str(page))
        if key in seen:
            continue
        seen.add(key)
        citation = f"PMID {pmid}, p. {page}"
        if context.get("citation"):
            citation += "; " + str(context["citation"])
        kept.append({"gene": gene, "evidence": quote, "citation": citation, "pmid": pmid, "page": str(page)})
    return kept

_PMID_TOKEN = re.compile(r"PMID\s*:?\s*(\d{4,9})", re.IGNORECASE)
_QUOTE = re.compile(r"[\"“]([^\"“”]{8,}?)[\"”]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[\(\"“])")
_WS = re.compile(r"\s+")
_QUOTE_CHARS = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-"})


def parse_questions(raw: str, max_n: int = MAX_QUESTIONS) -> list[str]:
    """The model's JSON array of requests, or [] when it gave nothing usable."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].strip()
    parsed: Any = None
    found = re.search(r"\[.*\]", s, re.DOTALL)
    for candidate in (s, found.group(0) if found else ""):
        try:
            parsed = json.loads(candidate)
            break
        except (json.JSONDecodeError, TypeError):
            continue
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for item in parsed:
        q = _WS.sub(" ", item).strip() if isinstance(item, str) else ""
        if len(q.split()) >= 4 and len(q) <= 400 and q.lower() not in (x.lower() for x in out):
            out.append(q)
    return out[:max_n]


def passages_block(contexts: list[dict[str, Any]], max_chars: int = 1500) -> str:
    """The passages as the answering prompt shows them."""
    lines = []
    for i, c in enumerate(contexts, 1):
        lines.append(f"[{i}] PMID:{c.get('pmid')} p.{c.get('page')} — {c.get('citation', '')}\n"
                     f"{str(c.get('text', ''))[:max_chars]}")
    return "\n\n".join(lines)


def _norm(text: str) -> str:
    return _WS.sub(" ", text.translate(_QUOTE_CHARS)).strip().lower()


@dataclass
class Verified:
    """The checked answer: its text, the PMIDs it still cites and what was taken out."""

    text: str
    cited: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def verify_answer(answer: str, contexts: list[dict[str, Any]]) -> Verified:
    """Remove from ``answer`` every PMID that is not among ``contexts`` and every sentence that quotes
    something no passage says. Deterministic; the model is not asked again.

    A sentence whose quotation fails is deleted whole, because the claim is built on it. A citation
    whose PMID fails is deleted from the sentence and the claim stays, now uncited; the caller treats an
    answer left with no citation at all as no answer."""
    allowed = {str(c.get("pmid")) for c in contexts}
    haystack = [_norm(str(c.get("text", ""))) for c in contexts]
    removed: list[str] = []
    kept: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(answer.strip()):
        bad_quote = next((m.group(1) for m in _QUOTE.finditer(sentence)
                          if len(m.group(1).split()) >= MIN_QUOTE_WORDS
                          and not any(_norm(m.group(1)).strip(" .,;") in h for h in haystack)), None)
        if bad_quote is not None:
            removed.append(f"sentence with a quotation that is in no passage: \"{bad_quote[:120]}\"")
            continue

        def _drop_unknown(m: "re.Match[str]") -> str:
            if m.group(1) in allowed:
                return m.group(0)
            removed.append(f"citation PMID:{m.group(1)} is not among the returned passages")
            return ""

        cleaned = _PMID_TOKEN.sub(_drop_unknown, sentence)
        # a bracket or parenthesis the removal emptied, and the separators left around the gap
        cleaned = re.sub(r"\[\s*[,;]*\s*\]|\(\s*[,;]*\s*\)", "", cleaned)
        cleaned = re.sub(r"\[\s*[,;]\s*|\s*[,;]\s*\]", lambda m: "[" if m.group(0).startswith("[") else "]", cleaned)
        cleaned = re.sub(r"(?<=\])\s*[,;]\s*(?=\[)", " ", cleaned)
        cleaned = re.sub(r"\s+([.,;])", r"\1", cleaned)
        kept.append(_WS.sub(" ", cleaned).strip())
    text = " ".join(k for k in kept if k)
    cited = list(dict.fromkeys(m.group(1) for m in _PMID_TOKEN.finditer(text)))
    return Verified(text=text.strip(), cited=cited, removed=removed)
