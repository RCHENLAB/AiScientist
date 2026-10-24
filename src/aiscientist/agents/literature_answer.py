"""The literature-search skill: a general question answered from the lab's corpus and PubMed, with citations.

What a request routed to ``literature`` (``agents/request_router.py``) runs once its one-step plan
is approved:

1. the model writes two to four keyword queries for PubMed and the exact words a passage that
   answers would contain (:func:`plan_search`);
2. each query is searched in PubMed through Europe PMC, abstracts included (:func:`collect_papers`),
   and the hits are merged and de-duplicated; at the same time, when the session reaches HPC3, the
   question and those words search the full text of the lab's RetiGene corpus (``search_corpus``,
   the hybrid ranking) and its passages join the sources, merged by PMID (:func:`add_corpus`);
3. the model answers from those passages and abstracts only, citing them by number
   (:func:`answer_messages`);
4. code writes the reference list from the numbers the answer cites (:func:`finish_answer`). A
   number that is not one of the papers is removed, so every reference is a paper the search
   returned and the model cannot add one.

A question about the RetiGene papers themselves searches the corpus alone, and PubMed only when it
asks for it (``plan_step(..., pubmed=False)``, ``request_router.corpus_only``; RCHENLAB/AiScientist
issue 46). A list question about them is not this skill: it is the corpus curation
(``gateway/corpus_scan.py``), which reads the whole corpus.

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
CORPUS_TOOL = "search_corpus"
MAX_QUERIES = 4
MAX_TERMS = 12
CORPUS_PASSAGES = 12
PER_QUERY = 12      # kept per query; each search reads 100 hits and ranks them itself
MAX_PAPERS = 12
ABSTRACT_CHARS = 1400

QUERY_SYSTEM = (
    "You write search queries for PubMed (searched through Europe PMC) that find papers answering "
    "a researcher's question. Write 2 to 4 DIFFERENT keyword queries in English. Every query keeps "
    "the question's main subject (the gene, protein, cell type or disease it is about) and adds one "
    "angle of the question (the disease, the tissue, the species, the mechanism); never write a "
    "query without the subject. Every term of a query must appear in a matching paper, so keep each query "
    "SHORT: 2 to 4 keywords, at most one gene symbol, no boolean operators, no quotes, no "
    "instruction words. Translate a question in another language into English keywords.\n"
    "Also list up to 12 `terms`: the exact words or short phrases (1 to 4 words) a passage that "
    "answers would contain, with their usual variants (gene symbols, disease names, technical "
    "terms; noun and adjective forms). They search the full text of the lab's paper collection.\n"
    'Reply with ONLY a JSON object, e.g. {"queries": ["DDX41 retina", "DDX41 photoreceptor '
    'degeneration", "DEAD-box helicase retinal disease"], "terms": ["DDX41", "DEAD-box helicase", '
    '"photoreceptor degeneration", "retina"]}.'
)

ANSWER_SYSTEM = (
    "You are AiScientist answering a researcher's question from the published literature. You are "
    "given the question and numbered papers: passages from the full text of papers in the lab's "
    "corpus, and abstracts of papers found in PubMed. Answer in English, from these texts only:\n"
    "- Open with a direct answer in one or two sentences, then explain in a few short paragraphs "
    "(at most about 350 words in all).\n"
    "- After every statement drawn from a paper, cite it by its number in square brackets, e.g. "
    "[2] or [1, 4]. Cite only the numbers given; never invent a paper, an author or a number.\n"
    "- Do not state findings the texts do not contain. Say plainly what they do not answer, "
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
    passages: list[str] = field(default_factory=list)   # full-text passages from the lab's corpus
    sources: list[str] = field(default_factory=list)    # "corpus", "pubmed"

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
    corpus: "dict[str, Any] | None" = None  # the corpus search: status, passages, papers, terms, error

    @property
    def errors(self) -> list[str]:
        return [str(s["error"]) for s in self.searched if s.get("error")]


def plan_step(question: str, corpus: bool = False, pubmed: bool = True) -> str:
    """The one step of a literature plan, as the plan card shows it. ``corpus``: the session also
    searches the lab's RetiGene corpus (a short HPC3 job). ``pubmed=False``: the corpus alone, for a
    question about the RetiGene papers (RCHENLAB/AiScientist issue 46)."""
    q = " ".join(str(question or "").split())
    if len(q) > 300:
        q = q[:297].rstrip() + "…"
    if corpus and not pubmed:
        return (f"**RetiGene corpus search** — Search the full text of the lab's RetiGene corpus only "
                f"(`{CORPUS_TOOL}`, a short HPC3 job) for what answers: “{q}”. Read the best passages (up "
                f"to {CORPUS_PASSAGES}) and answer with numbered citations to them. PubMed is not searched, "
                "because the question asks about the RetiGene papers; say “also search PubMed” to add it. "
                "It takes about a minute.")
    if corpus:
        return (f"**Literature search** — Search the full text of the lab's RetiGene corpus (`{CORPUS_TOOL}`, "
                f"a short HPC3 job) and PubMed (through Europe PMC, `{TOOL}`) for what answers: “{q}”. Read "
                f"the best corpus passages (up to {CORPUS_PASSAGES}) and the abstracts of the most relevant "
                f"PubMed papers (up to {MAX_PAPERS}), and answer with numbered citations to them. It takes "
                "one to a few minutes.")
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
    """Two to four keyword queries for ``question`` (:func:`plan_search` without the terms)."""
    return plan_search(complete, question, fallback=fallback)[0]


def _clean_term(text: str) -> str:
    t = re.sub(r'["“”`]', " ", str(text or ""))
    words = t.split()
    return " ".join(words).strip() if 0 < len(words) <= 4 and len(t) <= 60 else ""


def plan_search(complete: "Callable[[list[dict]], str] | None", question: str, *,
                fallback: "Callable[[str], str] | None" = None) -> "tuple[list[str], list[str]]":
    """Two to four keyword queries for ``question`` and up to ``MAX_TERMS`` exact words for the
    corpus, from one model call. When the model gives no usable query, ``fallback(question)`` (the
    literature tool's own query focusing) gives one; no terms is fine (the corpus search uses the
    question's own words anyway)."""
    queries: list[str] = []
    terms: list[str] = []
    if complete is not None:
        try:
            raw = str(complete([{"role": "system", "content": QUERY_SYSTEM},
                                {"role": "user", "content": f"Question: {question}\n\nThe JSON:"}]) or "")
        except Exception:  # noqa: BLE001 - query writing must never fail the step
            raw = ""
        items: Any = None
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        obj = safe_json_loads(text)
        if not isinstance(obj, dict) and "{" in text and "}" in text:
            obj = safe_json_loads(text[text.find("{"):text.rfind("}") + 1])
        if isinstance(obj, dict):
            items = obj.get("queries")
            for t in obj.get("terms") or []:
                c = _clean_term(t if isinstance(t, str) else "")
                if c and c.lower() not in {x.lower() for x in terms} and len(terms) < MAX_TERMS:
                    terms.append(c)
        else:
            m = re.search(r"\[.*\]", text, re.DOTALL)
            if m:
                try:
                    import json
                    items = json.loads(m.group(0))
                except ValueError:
                    items = None
        if not isinstance(items, list):
            items = []
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
    return queries, terms


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


def _corpus_metadata(context: dict[str, Any], citation: str) -> dict[str, str]:
    """Prefer structured manifest fields; recover missing columns from older corpus results.

    Work from the ends: identifiers, optional year, then the final journal segment. Splitting
    title/journal from the right preserves periods inside a title. Keep the original citation
    separately: these fields are for the prompt/CSV, never for rebuilding the reference line.
    """
    fields = {key: str(context.get(key) or "").strip()
              for key in ("title", "authors", "year", "journal", "doi")}
    doi = re.search(r"\bDOI:\s*(\S+)", citation, re.IGNORECASE)
    parsed = {"doi": doi.group(1).rstrip(".") if doi else "", "authors": "", "year": "", "journal": ""}
    body = re.split(r"\s*(?:PMID|DOI):\s*", citation, maxsplit=1, flags=re.IGNORECASE)[0].strip().rstrip(".")
    # A manifest may use only the title as its citation; it has no author/journal delimiters.
    if body == fields["title"].rstrip("."):
        return {key: value or parsed.get(key, "") for key, value in fields.items()}
    lead = re.match(r"^(.*?)\s+et al\.\s+(.+)$", body)
    if lead:
        parsed["authors"], body = lead.group(1) + " et al.", lead.group(2)
    else:
        # Single authors have no 'et al.'; their final initial ends the author segment.
        lead = re.match(r"^(.+?\s+[A-Z]{1,3})\.\s+(.+)$", body)
        if lead:
            parsed["authors"], body = lead.groups()
    year = re.search(r"\s*\((\d{4})\)\.?$", body)
    if year:
        parsed["year"], body = year.group(1), body[:year.start()].rstrip()
    known_title = fields["title"].rstrip(".")
    if known_title and body.startswith(known_title + ". "):
        parsed["title"] = fields["title"]
        parsed["journal"] = body[len(known_title) + 2:].rstrip(".")
    else:
        title, sep, journal = body.rstrip(".").rpartition(". ")
        parsed["title"] = title if sep else body.rstrip(".")
        parsed["journal"] = journal if sep else ""
    return {key: value or parsed.get(key, "") for key, value in fields.items()}


def add_corpus(collected: Collected, result: "dict[str, Any] | None", *,
               max_passages: int = CORPUS_PASSAGES) -> Collected:
    """``collected`` (the PubMed papers) with the corpus search's passages added: one source per
    corpus paper, its passages in rank order, listed first; a PubMed paper with the same PMID is the
    same source (passages and abstract together). Every source is numbered again, 1 to n.
    ``result`` is ``search_corpus``'s result (``status``, ``contexts``, ``terms``), or None when the
    session cannot reach the corpus."""
    if result is None:
        return collected
    info: dict[str, Any] = {"status": str(result.get("status") or "error"),
                            "terms": [t for t in (result.get("terms") or []) if isinstance(t, dict)]}
    if result.get("status") != "ok":
        info["error"] = str(result.get("error") or result.get("status") or "no result")
    corpus: list[Paper] = []
    by_pmid: dict[str, Paper] = {}
    n_passages = 0
    for c in result.get("contexts") or []:
        if not isinstance(c, dict) or n_passages >= max_passages:
            continue
        pmid, text = str(c.get("pmid") or "").strip(), " ".join(str(c.get("text") or "").split())
        if not pmid or not text:
            continue
        p = by_pmid.get(pmid)
        if p is None:
            cite = str(c.get("citation") or "").strip()
            metadata = _corpus_metadata(c, cite)
            metadata["title"] = metadata["title"] or f"PMID {pmid}"
            p = Paper(number=0, **metadata, pmid=pmid, citation=cite or f"PMID {pmid}",
                      url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", sources=["corpus"])
            by_pmid[pmid] = p
            corpus.append(p)
        page = c.get("page")
        p.passages.append((f"(p. {page}) " if page not in (None, "") else "") + text)
        n_passages += 1
    info.update(passages=n_passages, papers=len(corpus))
    merged = list(corpus)
    for p in collected.papers:
        same = by_pmid.get(p.pmid) if p.pmid else None
        if same is not None:
            same.abstract = same.abstract or p.abstract
            same.title, same.authors, same.year, same.journal = p.title or same.title, p.authors, p.year, p.journal
            same.doi, same.url = p.doi or same.doi, p.url or same.url
            same.citation = p.citation or same.citation
            same.queries = list(p.queries)
            same.sources.append("pubmed")
            continue
        p.sources = p.sources or ["pubmed"]
        merged.append(p)
    for i, p in enumerate(merged, 1):
        p.number = i
    return Collected(merged, collected.searched, info)


def answer_messages(question: str, papers: list[Paper]) -> list[dict]:
    """The answer-writing call: the sources, numbered, each with its corpus passages and its
    abstract."""
    blocks = []
    for p in papers:
        head = f"[{p.number}] {p.title}"
        meta = ", ".join(x for x in (p.authors.split(",")[0].strip() + (" et al." if "," in p.authors else ""),
                                     p.journal, p.year) if x)
        parts = [head] + ([meta] if meta else [])
        if p.passages:
            parts.append("Passages from the full text (lab's corpus):\n" + "\n".join(f"- {t}" for t in p.passages))
        if p.abstract or not p.passages:
            parts.append(f"Abstract: {p.abstract or '(no abstract available)'}")
        blocks.append("\n".join(parts))
    in_corpus = any(p.passages for p in papers)
    in_pubmed = any("pubmed" in p.sources or not p.passages for p in papers)
    where = ("the lab's corpus and PubMed" if in_corpus and in_pubmed else
             "the lab's corpus" if in_corpus else "PubMed")
    user = (f"Question: {question}\n\nPapers found in {where}:\n\n" + "\n\n".join(blocks)
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
    if collected.corpus is not None and not collected.searched:
        # The corpus alone (a question about the RetiGene papers): PubMed was not searched.
        why = f" The corpus search failed: {collected.corpus['error']}." if collected.corpus.get("error") else ""
        return (f"The lab's RetiGene corpus returned no passage to answer this from.{why} Nothing was "
                "answered from memory, and PubMed was not searched because the question asks about the "
                "RetiGene papers. Name the gene, disease or tissue more specifically, or say “also search "
                "PubMed”.")
    tried = ", ".join(f"“{s['query']}”" for s in collected.searched) or "no query"
    why = (" The search itself failed: " + "; ".join(collected.errors[:2]) + ".") if collected.errors else ""
    where = "PubMed returned no papers"
    if collected.corpus is not None:
        where = "Neither the lab's corpus nor PubMed returned a paper"
        if collected.corpus.get("error"):
            why += f" The corpus search failed: {collected.corpus['error']}."
    return (f"{where} to answer this from (searched {tried}).{why} Nothing was "
            "answered from memory. Try naming the gene, disease or tissue more specifically, or ask for "
            "an analysis of your data instead.")


def method_md(collected: Collected, date: str) -> str:
    """How the answer was made, for the saved summary."""
    lines: list[str] = []
    c = collected.corpus
    if c is not None:
        if c.get("error") and not c.get("passages"):
            lines.append(f"The lab's RetiGene corpus was searched on {date}, but the search failed "
                         f"({c['error']}); the answer rests on PubMed alone.\n")
        else:
            used = [t for t in c.get("terms") or [] if t.get("use") in ("filter", "rank")]
            lines.append(f"Searched the full text of the lab's RetiGene corpus on {date} with `{CORPUS_TOOL}` "
                         "(the question's meaning and its exact words, merged by rank): "
                         f"{c.get('passages', 0)} passage(s) from {c.get('papers', 0)} paper(s)."
                         + (" Words used: " + ", ".join(f"{t['term']} ({t['n_passages']})" for t in used[:16]) + "."
                            if used else "") + "\n")
    if c is not None and not collected.searched:
        lines.append("PubMed was not searched: the question asks about the RetiGene papers.")
        lines.append(f"\n{len(collected.papers)} distinct paper(s) were read (corpus passages); the answer cites "
                     "them by number, and each reference was checked against the search results.")
        return "\n".join(lines)
    lines.append(f"Searched PubMed through Europe PMC on {date} with {len(collected.searched)} "
                 f"quer{'y' if len(collected.searched) == 1 else 'ies'}:")
    for s in collected.searched:
        lines.append(f"- “{s['query']}”: {s['count']} paper(s)" + (f" (error: {s['error']})" if s.get("error") else ""))
    lines.append(f"\n{len(collected.papers)} distinct paper(s) were read (corpus passages, titles and abstracts); "
                 "the answer cites them by number, and each reference was checked against the search results.")
    return "\n".join(lines)


def papers_csv(papers: list[Paper], cited: "set[int] | None" = None) -> str:
    """Every paper read, one row each: what the reader can re-check or import."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["number", "cited", "source", "title", "authors", "year", "journal", "doi", "pmid", "url",
                "queries", "corpus_passages"])
    for p in papers:
        w.writerow([p.number, "yes" if cited and p.number in cited else "no", "+".join(p.sources) or "pubmed",
                    p.title, p.authors, p.year, p.journal, p.doi, p.pmid, p.url, " | ".join(p.queries),
                    len(p.passages)])
    return buf.getvalue()


def passages_md(papers: list[Paper]) -> str:
    """Every corpus passage read, verbatim, under its source number: what a reader checks a cited
    statement against."""
    out = []
    for p in papers:
        if p.passages:
            out.append(f"## [{p.number}] {p.citation or p.title}\n\n" + "\n\n".join(f"> {t}" for t in p.passages))
    return ("# Corpus passages read\n\n" + "\n\n".join(out) + "\n") if out else ""
