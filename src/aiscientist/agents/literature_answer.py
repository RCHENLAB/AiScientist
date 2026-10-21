"""The literature-search skill: a general question answered from PubMed, with citations.

What a request routed to ``literature`` (``agents/request_router.py``) runs once its one-step plan
is approved. Everything happens in the gateway over Europe PMC's public REST API, with no HPC3 job:

1. the model writes two to four keyword queries for the question (:func:`plan_queries`);
2. each query is searched in PubMed through Europe PMC, abstracts included (:func:`collect_papers`),
   and the hits are merged and de-duplicated;
3. the model answers from those abstracts only, citing them by number (:func:`answer_messages`);
4. code writes the reference list from the numbers the answer cites (:func:`finish_answer`). A
   number that is not one of the papers is removed, so every reference is a paper the search
   returned and the model cannot add one.

The functions take their model and search callables as arguments, so the skill is tested offline.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .loop_utils import safe_json_loads

TOOL = "literature_search"
MAX_QUERIES = 4
PER_QUERY = 8
MAX_PAPERS = 12
ABSTRACT_CHARS = 1400

QUERY_SYSTEM = (
    "You write search queries for PubMed (searched through Europe PMC) that find papers answering "
    "a researcher's question. Write 2 to 4 DIFFERENT keyword queries in English. Every query keeps "
    "the question's main subject (the gene, protein, cell type or disease it is about) and adds one "
    "angle of the question (the disease, the tissue, the species, the mechanism); never write a "
    "query without the subject. Every term of a query must appear in a matching paper, so keep each query "
    "SHORT: 2 to 4 keywords, at most one gene symbol, no boolean operators, no quotes, no "
    "instruction words. Translate a question in another language into English keywords. "
    'Reply with ONLY a JSON array of strings, e.g. ["DDX41 retina", "DDX41 photoreceptor '
    'degeneration", "DEAD-box helicase retinal disease"].'
)

ANSWER_SYSTEM = (
    "You are AiScientist answering a researcher's question from the published literature. You are "
    "given the question and numbered papers found in PubMed, each with its abstract. Answer in "
    "English, from these abstracts only:\n"
    "- Open with a direct answer in one or two sentences, then explain in a few short paragraphs "
    "(at most about 350 words in all).\n"
    "- After every statement drawn from a paper, cite it by its number in square brackets, e.g. "
    "[2] or [1, 4]. Cite only the numbers given; never invent a paper, an author or a number.\n"
    "- Do not state findings the abstracts do not contain. Say plainly what they do not answer, "
    "and when the papers found do not address the question, say so instead of answering from "
    "memory.\n"
    "- No reference list, no title, no Methods/Results sections: the references are added below "
    "your answer from the numbers you cite."
)

_CITE = re.compile(r"\[(\d+(?:\s*[-–,]\s*\d+)*)\]")
_QUERY_JUNK = re.compile(r'["“”`]|\b(?:AND|OR|NOT)\b')


@dataclass
class Paper:
    """One paper the search returned, numbered as the answer cites it."""

    number: int
    title: str
    authors: str = ""
    year: str = ""
    journal: str = ""
    doi: str = ""
    pmid: str = ""
    url: str = ""
    citation: str = ""
    abstract: str = ""
    queries: list[str] = field(default_factory=list)

    def key(self) -> str:
        return (self.doi.lower() or self.pmid or self.title.lower()).strip()

    def line(self) -> str:
        """The reference line: the citation, then a link to the paper."""
        text = self.citation or f"{self.title} ({self.year})"
        return f"{text} {self.url}".strip() if self.url else text


@dataclass
class Collected:
    papers: list[Paper]
    searched: list[dict[str, Any]]          # per query: query, count, error

    @property
    def errors(self) -> list[str]:
        return [str(s["error"]) for s in self.searched if s.get("error")]


def plan_step(question: str) -> str:
    """The one step of a literature plan, as the plan card shows it."""
    q = " ".join(str(question or "").split())
    if len(q) > 300:
        q = q[:297].rstrip() + "…"
    return (f"**Literature search** — Search PubMed (through Europe PMC) for papers that answer: “{q}”. "
            f"Read the abstracts of the most relevant papers (up to {MAX_PAPERS}) and answer with "
            f"numbered citations to them, using `{TOOL}`. It runs in the gateway in about a minute, "
            "with no HPC3 job.")


def _clean_query(text: str) -> str:
    q = _QUERY_JUNK.sub(" ", str(text or ""))
    q = re.sub(r"[()\[\]{}:;]", " ", q)
    words = q.split()
    return " ".join(words[:8]).strip()


def plan_queries(complete: "Callable[[list[dict]], str] | None", question: str, *,
                 fallback: "Callable[[str], str] | None" = None) -> list[str]:
    """Two to four keyword queries for ``question``. The model writes them; when it gives nothing
    usable, ``fallback(question)`` (the literature tool's own query focusing) gives one."""
    queries: list[str] = []
    if complete is not None:
        try:
            raw = str(complete([{"role": "system", "content": QUERY_SYSTEM},
                                {"role": "user", "content": f"Question: {question}\n\nThe JSON array:"}]) or "")
        except Exception:  # noqa: BLE001 - query writing must never fail the step
            raw = ""
        items: Any = None
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                import json
                items = json.loads(m.group(0))
            except ValueError:
                items = None
        if items is None:
            obj = safe_json_loads(text)
            items = (obj or {}).get("queries") if isinstance(obj, dict) else None
        for item in items or []:
            q = _clean_query(item if isinstance(item, str) else "")
            if q and q.lower() not in {x.lower() for x in queries}:
                queries.append(q)
            if len(queries) >= MAX_QUERIES:
                break
    if not queries:
        q = _clean_query(fallback(question) if fallback else question)
        if q:
            queries.append(q)
    return queries


def collect_papers(search: "Callable[..., dict[str, Any]]", queries: list[str], *,
                   per_query: int = PER_QUERY, max_papers: int = MAX_PAPERS) -> Collected:
    """Search each query and merge the hits, taking them in turn from each query's ranking so every
    angle is represented, de-duplicated by DOI, PMID or title. ``search(query, limit)`` returns the
    literature tool's result dict (``status``, ``results``)."""
    searched: list[dict[str, Any]] = []
    rankings: list[list[dict[str, Any]]] = []
    for q in queries:
        try:
            out = search(q, per_query)
        except Exception as exc:  # noqa: BLE001 - one failed query never fails the step
            out = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        out = out if isinstance(out, dict) else {"status": "error", "error": "no result"}
        hits = [h for h in (out.get("results") or []) if isinstance(h, dict)]
        searched.append({"query": q, "count": len(hits),
                         **({"error": str(out.get("error") or out.get("status"))}
                            if out.get("status") != "ok" else {})})
        rankings.append(hits)
    papers: list[Paper] = []
    by_key: dict[str, Paper] = {}
    for rank in range(max((len(r) for r in rankings), default=0)):
        for q, hits in zip(queries, rankings):
            if rank >= len(hits):
                continue
            h = hits[rank]
            p = Paper(number=0, title=str(h.get("title") or "").strip(),
                      authors=str(h.get("authors") or ""), year=str(h.get("year") or ""),
                      journal=str(h.get("journal") or ""), doi=str(h.get("doi") or ""),
                      pmid=str(h.get("pmid") or ""), url=str(h.get("url") or ""),
                      citation=str(h.get("citation") or ""),
                      abstract=" ".join(str(h.get("abstract") or "").split())[:ABSTRACT_CHARS])
            if not p.title or not (p.doi or p.pmid):
                continue
            k = p.key()
            if k in by_key:
                if q not in by_key[k].queries:
                    by_key[k].queries.append(q)
                continue
            if len(papers) >= max_papers:
                continue
            p.queries.append(q)
            p.number = len(papers) + 1
            by_key[k] = p
            papers.append(p)
    return Collected(papers, searched)


def answer_messages(question: str, papers: list[Paper]) -> list[dict]:
    """The answer-writing call: the papers, numbered, each with its abstract."""
    blocks = []
    for p in papers:
        head = f"[{p.number}] {p.title}"
        meta = ", ".join(x for x in (p.authors.split(",")[0].strip() + (" et al." if "," in p.authors else ""),
                                     p.journal, p.year) if x)
        blocks.append(f"{head}\n{meta}\nAbstract: {p.abstract or '(no abstract available)'}")
    user = (f"Question: {question}\n\nPapers found in PubMed:\n\n" + "\n\n".join(blocks)
            + "\n\nWrite the answer now.")
    return [{"role": "system", "content": ANSWER_SYSTEM}, {"role": "user", "content": user}]


def _numbers(group: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"\s*,\s*", group):
        if re.search(r"[-–]", part):
            a, _, b = re.split(r"\s*([-–])\s*", part, maxsplit=1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if 0 < hi - lo <= 20:
                out.extend(range(lo, hi + 1))
        elif part.strip().isdigit():
            out.append(int(part))
    return out


def finish_answer(text: str, papers: list[Paper]) -> "tuple[str, list[Paper]]":
    """The answer with its citations checked, and the papers it cites. A bracketed number that is
    not a paper is removed; the reference list is written here, from the papers cited."""
    known = {p.number: p for p in papers}
    cited: list[int] = []

    def fix(m: "re.Match[str]") -> str:
        nums = [n for n in _numbers(m.group(1)) if n in known]
        for n in nums:
            if n not in cited:
                cited.append(n)
        return f"[{', '.join(str(n) for n in dict.fromkeys(nums))}]" if nums else ""

    body = _CITE.sub(fix, str(text or "")).strip()
    # A reference list the model wrote anyway is dropped: the one below is the checked one.
    body = re.split(r"\n#{0,3}\s*\**(?:References|Reference list|Sources)\**:?\s*\n", body, maxsplit=1)[0].rstrip()
    body = re.sub(r"[ \t]+([.,;:])", r"\1", body)
    used = [known[n] for n in sorted(cited)]
    return body, used


def references_md(cited: list[Paper], found: list[Paper]) -> str:
    """The reference list under the answer: the papers cited, each under the number the answer
    cites it by; when the answer cites none, the papers found, so the reader still sees what was
    read. Bulleted, not a numbered list: Markdown renumbers a numbered list from 1, and the
    numbers must stay the answer's ([1], [4], [5])."""
    if cited:
        return "**References**\n\n" + "\n".join(f"- [{p.number}] {p.line()}" for p in cited)
    if found:
        return ("**Papers read** (the answer cites none of them)\n\n"
                + "\n".join(f"- [{p.number}] {p.line()}" for p in found))
    return ""


def no_papers_answer(question: str, collected: Collected) -> str:
    """The reply when the search returned nothing to read."""
    tried = ", ".join(f"“{s['query']}”" for s in collected.searched) or "no query"
    why = (" The search itself failed: " + "; ".join(collected.errors[:2]) + ".") if collected.errors else ""
    return (f"PubMed returned no papers to answer this from (searched {tried}).{why} Nothing was "
            "answered from memory. Try naming the gene, disease or tissue more specifically, or ask for "
            "an analysis of your data instead.")


def method_md(collected: Collected, date: str) -> str:
    """How the answer was made, for the saved summary."""
    lines = [f"Searched PubMed through Europe PMC on {date} with {len(collected.searched)} "
             f"quer{'y' if len(collected.searched) == 1 else 'ies'}:"]
    for s in collected.searched:
        lines.append(f"- “{s['query']}”: {s['count']} paper(s)" + (f" (error: {s['error']})" if s.get("error") else ""))
    lines.append(f"\n{len(collected.papers)} distinct paper(s) were read (title and abstract); the answer "
                 "cites them by number, and each reference was checked against the search results.")
    return "\n".join(lines)


def papers_csv(papers: list[Paper], cited: "set[int] | None" = None) -> str:
    """Every paper read, one row each: what the reader can re-check or import."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["number", "cited", "title", "authors", "year", "journal", "doi", "pmid", "url", "queries"])
    for p in papers:
        w.writerow([p.number, "yes" if cited and p.number in cited else "no", p.title, p.authors, p.year,
                    p.journal, p.doi, p.pmid, p.url, " | ".join(p.queries)])
    return buf.getvalue()
